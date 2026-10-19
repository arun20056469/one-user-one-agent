"""End-to-end: the whole pipeline on the fixture pack.

Runs exactly what `python -m src.main run` runs - replay -> simulate -> judge
-> impact - against a private copy of the mini data pack, then asserts every
invariant the submission claims. The fixture pack is copied first because the
replay legitimately mutates the pack in memory (title withdrawal, goal-rule
correction), and other tests must not inherit that.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from src.harness import ReplayHarness
from src.impact import analyze
from src.judge import OutcomeJudge
from src.pack import Pack
from src.utils import in_quiet_hours, ist_date, parse_ts, read_jsonl
from tests.helpers import ROOT, fixture_pack_path


class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.root = Path(cls.tmp.name) / "pack"
        shutil.copytree(fixture_pack_path(), cls.root)
        cls.out = Path(cls.tmp.name) / "run"
        cls.pack = Pack(cls.root)

        harness = ReplayHarness(cls.pack, cls.out)
        cls.lat = harness.run()
        harness.store.close()

        subprocess.run(
            [sys.executable, str(cls.root / "simulator" / "simulate_outcomes.py"),
             "--pack", str(cls.root), "--actions", str(cls.out / "actions.jsonl"),
             "--out", str(cls.out), "--seed", "7"],
            check=True, cwd=ROOT, capture_output=True)

        judge = OutcomeJudge(cls.pack, cls.out)
        cls.outcomes = judge.run_judgement()
        judge.close()

        cls.summary = analyze(cls.pack, cls.out)
        cls.decisions = read_jsonl(cls.out / "decisions.jsonl")
        cls.actions = read_jsonl(cls.out / "actions.jsonl")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # ------------------------------------------------------------------ #
    def test_replay_safety_invariants(self):
        self.assertEqual(len(self.decisions), len(self.pack.triggers),
                         "every trigger must produce exactly one decision")

        leaks = [a for a in self.actions
                 if self.pack.arms.get(a["viewer_id"]) == "holdout"
                 and a.get("status") in ("PUBLISHED", "SCHEDULED")]
        self.assertEqual(leaks, [], "holdout viewers must receive zero actions")

        idem = Counter(a["idempotency_key"] for a in self.actions)
        self.assertEqual([k for k, v in idem.items() if v > 1], [],
                         "one trigger event_id must never create two actions")

        outbound = [a for a in self.actions if a["action"] in ("PUSH", "WHATSAPP")
                    and a["status"] == "PUBLISHED"]
        quiet = [a["action_id"] for a in outbound
                 if in_quiet_hours(parse_ts(a["published_at"]))]
        self.assertEqual(quiet, [], "no outbound message may be sent in quiet hours")

        per_day = Counter((a["viewer_id"], ist_date(parse_ts(a["published_at"])))
                          for a in outbound)
        self.assertEqual([k for k, v in per_day.items() if v > 1], [],
                         "at most one outbound message per viewer per IST day")

        unexplained = [d for d in self.decisions
                       if not (d.get("goal") or d.get("no_action_reason") or d.get("reason"))]
        self.assertEqual(unexplained, [], "every decision needs a goal or a no-action reason")
        self.assertTrue(self.lat["action_counts"], "the run must actually act on viewers")

    def test_rerun_into_same_dir_is_reproducible(self):
        # `python -m src.main run` must be re-runnable: a second replay into the
        # SAME directory must not treat its own past as an idempotency hit (the
        # ledger lives in viewer_state/state.db, which replay resets first).
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            root = Path(td) / "pack"
            shutil.copytree(fixture_pack_path(), root)
            out = Path(td) / "run"
            runs = []
            for _ in range(2):
                pack = Pack(root)
                harness = ReplayHarness(pack, out)
                lat = harness.run()
                harness.store.close()
                runs.append((lat["decisions"], lat["action_counts"], lat["status_counts"]))
            self.assertEqual(runs[0], runs[1],
                             "re-running into the same dir must reproduce the run")
            self.assertEqual(runs[1][0], len(Pack(root).triggers))
            self.assertGreater(sum(runs[1][1].values()), 0,
                               "the second run must still act, not be all idempotency hits")
            self.assertNotEqual(runs[1][2].get("SUPPRESSED", 0), runs[1][0])

    def test_faults_and_staleness_were_exercised(self):
        statuses = Counter(d["model_status"] for d in self.decisions)
        self.assertIn("SKIPPED", statuses, "the surge window must skip optional model calls")
        fallbacks = [d["fallback_used"] for d in self.decisions if d["fallback_used"]]
        self.assertTrue(any("provider" in f for f in fallbacks),
                        "a provider fault must fall back to the rule plan")

        ageing = [d for d in self.decisions if 300 < d["trigger"]["event_age_s"] <= 1800]
        self.assertNotIn("PUSH", {d["action"] for d in ageing},
                         "an ageing trigger must never wake the phone")
        self.assertNotIn("WHATSAPP", {d["action"] for d in ageing})
        stale = [d for d in self.decisions if d["trigger"]["event_age_s"] > 1800]
        self.assertTrue(all(d["action"] == "NONE" for d in stale),
                        "an offline-buffered stale trigger must not create an action")

    def test_judgement_and_honest_impact_outputs(self):
        self.assertTrue(self.outcomes, "the judge must return rows")
        self.assertTrue(all(o["status"] in ("SUCCEEDED", "FAILED", "INCONCLUSIVE")
                            for o in self.outcomes))
        self.assertTrue(any(o["rule_correction_applied"] for o in self.outcomes),
                        "the goal-rule correction must reach the affected open goals")

        itt = self.summary["itt_triggered"]
        self.assertGreater(itt["agent"]["n"], 0)
        self.assertGreater(itt["holdout"]["n"], 0)
        # the same comparison computed naively must be reported next to it
        self.assertIn("naive_served_vs_rest", self.summary)
        self.assertIn("pilot", self.summary)
        self.assertEqual(self.summary["holdout_action_leaks"], [])

        for name in ("latency_report.json", "impact_report.md", "impact_summary.json",
                     "outcomes.jsonl", "final_report.md", "control_log.jsonl"):
            self.assertTrue((self.out / name).exists(), f"{name} must be written")
        final = (self.out / "final_report.md").read_text(encoding="utf-8")
        # the report must state the honest arm difference and its CI (the
        # conclusion sentence itself depends on the sample, so it is not fixed)
        self.assertIn("primary: replay-day visitors (ITT)", final)
        self.assertIn("95% CI", final)
        self.assertIn("naive", final)
        summary_json = json.loads((self.out / "impact_summary.json").read_text(encoding="utf-8"))
        self.assertAlmostEqual(summary_json["itt_triggered"]["diff"], itt["diff"])


if __name__ == "__main__":
    unittest.main()
