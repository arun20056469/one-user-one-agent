"""Required test 2: conflicting sources are resolved with a precedence rule
and the conflict stays visible in the decision output.

- dialect: app setting beats campaign deeplink and profile preference,
  every disagreement recorded on the decision;
- subscription: a purchase event received after the day-stale snapshot
  overrides the profile row (and the override is recorded);
- duplicate profile rows: freshest row_updated_at wins, conflict recorded.
"""
import tempfile
import unittest
from pathlib import Path

from src import facts as facts_mod
from src.utils import parse_ts
from tests.helpers import (fresh_store, get_pack, make_agent, make_trigger,
                           viewer_with_profile)


class TestConflictingSources(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack = get_pack()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = fresh_store(Path(self.tmp.name))
        self.agent = make_agent(self.pack, self.store)
        self.viewer = viewer_with_profile(self.pack)

    def tearDown(self):
        self.store.close()   # release the SQLite handle before deleting files
        self.tmp.cleanup()

    def test_dialect_conflict_resolved_to_app_setting(self):
        profile_pref = self.pack.profiles[self.viewer]["preferred_dialect"]
        app_dialect = next(d for d in ("d1", "d2", "d3", "d4", "d5", "d6")
                           if d != profile_pref and d != "d5")
        trig = make_trigger(self.viewer, "2026-09-18T10:00:00Z", event_id="DIA-1",
                            dialect=app_dialect,
                            props={"campaign_dialect": "d5"})
        d = self.agent.decide(trig, parse_ts(trig["deliver_at"]))

        self.assertEqual(d["dialect"]["used"], app_dialect)
        self.assertEqual(d["dialect"]["source"], "app_setting")
        joined = " ".join(d["dialect"]["conflicts"])
        self.assertIn(profile_pref, joined, "profile dialect conflict must stay visible")
        self.assertIn("d5", joined, "campaign dialect conflict must stay visible")
        self.assertTrue(any(c.startswith("dialect ") for c in d["conflicting_evidence"]))

        # content actually follows the decision dialect
        for cid in d["content"]:
            if cid.startswith("title:"):
                t = self.pack.by_title_id[cid.split(":")[1]]
                self.assertEqual(t["dialect"], app_dialect)

    def test_fresher_purchase_event_beats_day_stale_profile(self):
        # find a viewer whose profile snapshot predates their purchase event
        target = None
        for vid, evs in self.pack.events_by_viewer.items():
            prof = self.pack.profiles.get(vid)
            if not prof:
                continue
            buys = [e for e in evs if e["event_type"] == "purchase"]
            if not buys:
                continue
            if buys[-1]["received_at"] > prof["row_updated_at"]:
                target = (vid, buys[-1], prof)
                break
        if target is None:
            self.skipTest("fixture has no profile/purchase disagreement (regenerate pack)")
        vid, buy, prof = target
        f = facts_mod.resolve(self.pack, vid, parse_ts(buy["received_at"]))
        self.assertTrue(f.subscription["source"].startswith("event:"),
                        f"purchase event must win, got {f.subscription['source']}")
        if f.subscription["value"] != prof["subscription_status"]:
            self.assertTrue(any(c.startswith("subscription ") for c in f.conflicts),
                            "override must be recorded as a visible conflict")

    def test_duplicate_profile_rows_resolved_by_freshest_row(self):
        dup = next((v for v, rows in self.pack.profile_duplicates.items() if len(rows) > 1), None)
        if dup is None:
            self.skipTest("fixture has no duplicate profile rows")
        rows = self.pack.profile_duplicates[dup]
        freshest = max(rows, key=lambda r: r["row_updated_at"])
        self.assertEqual(self.pack.profiles[dup]["row_updated_at"], freshest["row_updated_at"])
        f = facts_mod.resolve(self.pack, dup, parse_ts("2026-09-18T12:00:00Z"))
        self.assertTrue(any("profile duplicate rows" in c for c in f.conflicts),
                        "duplicate rows must be reported as a conflict")

    def test_brand_new_viewer_without_profile_row_is_built_from_events(self):
        novel = next(v for v, evs in self.pack.events_by_viewer.items()
                     if v not in self.pack.profiles)
        f = facts_mod.resolve(self.pack, novel, parse_ts("2026-09-18T12:00:00Z"))
        self.assertTrue(any("no profile row" in a for a in f.assumptions))
        self.assertNotEqual(f.signup["source"], "unknown")


if __name__ == "__main__":
    unittest.main()
