"""Required test 4: a corrected goal rule re-judges ONLY the affected open
goals.

We build a tiny run: two viewers with open goals (weekly_active_3d and
first_paid_7d) on the fixture pack, which contains a
`goal_rule_correction` control event for weekly_active_3d in the middle of
the replay day.

Expected: the weekly_active goal is judged under the NEW rule (an app-open
day only counts if a play happens the same day) and the outcome changes
accordingly; the first_paid goal is judged under its original rule, with no
correction flag and an unchanged rule version - i.e. no mass reprocessing.
"""
import json
import tempfile
import unittest
from pathlib import Path

from src.judge import OutcomeJudge
from src.pack import Pack
from src.utils import write_jsonl
from tests.helpers import fixture_pack_path, get_pack


class TestCorrectedGoalRule(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack = get_pack()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.run = Path(self.tmp.name)
        self._stores = []          # every SQLite handle opened during a test
        pack = self.pack

        # viewer A: open weekly_active_3d goal, opens on 3 days, NO plays
        # anywhere inside the window (so the corrected rule fails it)
        def no_plays(vid):
            return not any(
                e["event_type"] == "episode_play"
                and e["_received"].isoformat() >= "2026-09-18T10:00:00+00:00"
                for e in pack.events_by_viewer.get(vid, []))
        agents = [v for v, a in sorted(pack.arms.items()) if a == "agent"]
        self.viewer_a = next(v for v in agents if no_plays(v))
        self.viewer_b = next(v for v in agents
                             if v != self.viewer_a and not any(
                                 e["event_type"] == "purchase"
                                 and e["_received"].isoformat() >= "2026-09-18T10:00:00+00:00"
                                 for e in pack.events_by_viewer.get(v, [])))

        decisions = [
            {"decision_id": "D-A", "viewer_id": self.viewer_a,
             "deliver_at": "2026-09-18T10:00:00Z",
             "goal": {"id": "weekly_active_3d",
                      "deadline": "2026-09-25T10:00:00Z",
                      "success_signal": "app_open x1 (distinct_days=3) within 7d",
                      "rule_version": "g1"}},
            {"decision_id": "D-B", "viewer_id": self.viewer_b,
             "deliver_at": "2026-09-18T10:00:00Z",
             "goal": {"id": "first_paid_7d",
                      "deadline": "2026-09-25T10:00:00Z",
                      "success_signal": "purchase x1 within 7d",
                      "rule_version": "g1"}},
        ]
        write_jsonl(self.run / "decisions.jsonl", decisions)

        outcomes = []
        for i, day in enumerate(("19", "20", "21")):
            for kind in ("app_open",):
                outcomes.append({"event_id": f"OA-{i}", "viewer_id": self.viewer_a,
                                 "event_type": kind,
                                 "occurred_at": f"2026-09-{day}T10:00:00Z",
                                 "received_at": f"2026-09-{day}T10:00:05Z",
                                 "props": {}, "source": "simulator"})
        outcomes.append({"event_id": "OB-1", "viewer_id": self.viewer_b,
                         "event_type": "purchase",
                         "occurred_at": "2026-09-20T12:00:00Z",
                         "received_at": "2026-09-20T12:00:05Z",
                         "props": {"plan": "monthly", "amount_inr": 149},
                         "source": "simulator"})
        # the simulator keeps observing past both deadlines: without a signal
        # after 2026-09-25 the honest answer for an unmet goal would be
        # INCONCLUSIVE ("we never observed until the deadline"), not FAILED.
        outcomes.append({"event_id": "OBS-END", "viewer_id": self.viewer_b,
                         "event_type": "app_open",
                         "occurred_at": "2026-09-26T10:00:00Z",
                         "received_at": "2026-09-26T10:00:05Z",
                         "props": {}, "source": "simulator"})
        write_jsonl(self.run / "outcome_events.jsonl", outcomes)

    def tearDown(self):
        for s in self._stores:
            s.close()
        self.tmp.cleanup()

    def test_only_affected_open_goals_are_re_judged(self):
        corrections = [c for c in self.pack.control_events
                       if c["type"] == "goal_rule_correction"]
        self.assertTrue(corrections, "fixture must contain a goal-rule correction")
        self.assertEqual(corrections[0]["goal_id"], "weekly_active_3d")

        judge = OutcomeJudge(self.pack, self.run)
        self._stores.append(judge.store)
        rows = judge.run_judgement()
        by = {(r["viewer_id"], r["goal_id"]): r for r in rows}

        a = by[(self.viewer_a, "weekly_active_3d")]
        self.assertTrue(a["rule_correction_applied"])
        self.assertIn("+rulefix", a["rule_version"])
        # old rule (3 distinct open days) would have succeeded; the new rule
        # requires a play the same day, and this viewer never played
        self.assertEqual(a["status"], "FAILED")

        b = by[(self.viewer_b, "first_paid_7d")]
        self.assertFalse(b["rule_correction_applied"])
        self.assertEqual(b["rule_version"], "g1")
        self.assertEqual(b["status"], "SUCCEEDED")

        # no other goals were touched
        self.assertEqual(len(rows), 2, "exactly the two open goals are judged")

    def test_rule_correction_marked_on_open_goal_state(self):
        """The harness flags only open goals of the corrected goal id."""
        from src.state import StateStore
        from src.harness import ReplayHarness
        store = StateStore(self.run / "viewer_state" / "state.db")
        self._stores.append(store)
        v = store.get_viewer(self.viewer_a)
        v["active_goal"] = {"id": "weekly_active_3d", "deadline": "2026-09-25T10:00:00Z",
                            "status": "open", "rule_version": "g1"}
        store.put_viewer(v)
        v2 = store.get_viewer(self.viewer_b)
        v2["active_goal"] = {"id": "first_paid_7d", "deadline": "2026-09-25T10:00:00Z",
                             "status": "open", "rule_version": "g1"}
        store.put_viewer(v2)

        harness = ReplayHarness.__new__(ReplayHarness)  # run just the hook, no replay
        harness.store = store
        harness.pack = self.pack
        n = harness._mark_affected_open_goals(
            "weekly_active_3d",
            {"goal_id": "weekly_active_3d", "at": "2026-09-18T14:00:00Z",
             "new_rule": {"event": "app_open"}})
        self.assertEqual(n, 1)
        self.assertIn("+rulefix", store.get_viewer(self.viewer_a)["active_goal"]["rule_version"])
        self.assertEqual(store.get_viewer(self.viewer_b)["active_goal"]["rule_version"], "g1")


if __name__ == "__main__":
    unittest.main()
