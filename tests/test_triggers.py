"""Required test 1: a late or duplicate trigger.

- the same trigger event_id delivered twice creates exactly ONE action;
- a stale (offline-buffered, days old) app open is not treated as a live
  session: recorded as no-action with a stale_trigger reason;
- an ageing trigger (5-30 min) never wakes the phone.
"""
import tempfile
import unittest
from pathlib import Path

from src.utils import parse_ts
from tests.helpers import (agent_arm_viewer, fresh_store, get_pack, make_agent,
                           make_trigger)


class TestDuplicateAndLateTriggers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack = get_pack()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = fresh_store(Path(self.tmp.name))
        self.agent = make_agent(self.pack, self.store)
        self.viewer = agent_arm_viewer(self.pack)

    def tearDown(self):
        self.store.close()   # release the SQLite handle before deleting files
        self.tmp.cleanup()

    def test_duplicate_trigger_creates_one_action(self):
        trig = make_trigger(self.viewer, "2026-09-18T10:00:00Z", event_id="DUP-1")
        d1 = self.agent.decide(dict(trig), parse_ts(trig["deliver_at"]))
        d2 = self.agent.decide(dict(trig), parse_ts(trig["deliver_at"]))
        mine = [a for a in self.store.all_actions() if a["trigger_event_id"] == "DUP-1"]
        self.assertEqual(len(mine), 1, "duplicate trigger must not create a duplicate action")
        self.assertEqual(d2["status"], "SUPPRESSED")
        self.assertIn("duplicate_trigger", d2["no_action_reason"])
        self.assertNotEqual(d1["decision_id"], d2["decision_id"])

    def test_stale_offline_trigger_is_not_a_live_session(self):
        # occurred 2 days before receipt (phone buffered it offline)
        trig = make_trigger(self.viewer, "2026-09-18T10:00:00Z", event_id="STALE-1",
                            occurred="2026-09-16T09:58:00Z")
        d = self.agent.decide(trig, parse_ts(trig["deliver_at"]))
        self.assertEqual(d["action"], "NONE")
        self.assertTrue(str(d["no_action_reason"]).startswith("stale_trigger"))
        self.assertGreater(d["trigger"]["event_age_s"], 1800)
        self.assertEqual(len(self.store.all_actions()), 0)

    def test_ageing_trigger_never_sends_a_message(self):
        # 20 minutes old: downgrade to in-session surfaces only
        trig = make_trigger(self.viewer, "2026-09-18T10:00:00Z", event_id="AGE-1",
                            occurred="2026-09-18T09:40:00Z")
        d = self.agent.decide(trig, parse_ts(trig["deliver_at"]))
        self.assertNotIn(d["action"], ("PUSH", "WHATSAPP"),
                         "ageing trigger must not send outbound messages")
        if d["action"] != "NONE":
            self.assertIn("downgraded_trigger", str(d["reason"]))


if __name__ == "__main__":
    unittest.main()
