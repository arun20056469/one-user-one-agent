"""Extra safety tests: quiet hours, frequency cap, holdout, injected
instructions, unsupported model claims, home-page row minimums and
withdrawn titles."""
import json
import tempfile
import unittest
from pathlib import Path

from src.interventions import InterventionPlanner, Proposal, build_home_page
from src.provider import ModelResult
from src.utils import parse_ts
from src.verifier import Verifier
from tests.helpers import (agent_arm_viewer, fresh_store, get_pack, make_agent,
                           make_trigger, holdout_viewer, viewer_with_profile)
from src import facts as facts_mod
from src.goals import GoalPlanner


class TestQuietHours(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack = get_pack()

    def test_outbound_message_in_quiet_hours_is_scheduled_not_sent(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        store = fresh_store(Path(tmp.name))
        self.addCleanup(store.close)
        agent = make_agent(self.pack, store)
        # pick a viewer eligible for a conversion goal (so a message is planned)
        viewer = next(v for v in sorted(self.pack.profiles)
                      if self.pack.arms.get(v) == "agent"
                      and self.pack.profiles[v]["subscription_status"] in
                      ("none", "lapsed", "trial", "monthly")
                      and self.pack.profiles[v]["notifications_opt_in"] == "true")
        # 23:00 IST == 17:30 UTC
        trig = make_trigger(viewer, "2026-09-18T17:30:00Z", event_id="QUIET-1")
        d = agent.decide(trig, parse_ts(trig["deliver_at"]))
        if d["action"] in ("PUSH", "WHATSAPP"):
            self.assertIsNotNone(d["scheduled_for"], "message must be scheduled, not sent now")
            sched = parse_ts(d["scheduled_for"])
            ist_hour = (sched + __import__("datetime").timedelta(hours=5, minutes=30)).hour
            self.assertEqual(ist_hour, 8)
            action = next(a for a in store.all_actions())
            self.assertEqual(action["status"], "SCHEDULED")
        else:
            # in-app / home page is the quiet-hours-safe alternative
            self.assertIn(d["action"], ("IN_APP", "HOME_PAGE", "NONE"))


class TestFrequencyCap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack = get_pack()

    def test_second_message_same_ist_day_is_blocked(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        store = fresh_store(Path(tmp.name))
        self.addCleanup(store.close)
        agent = make_agent(self.pack, store)
        candidates = [v for v in sorted(self.pack.profiles)
                      if self.pack.arms.get(v) == "agent"
                      and self.pack.profiles[v]["notifications_opt_in"] == "true"
                      and self.pack.profiles[v]["subscription_status"] in
                      ("none", "lapsed", "trial", "monthly")]
        # goal choice is data-driven, so find a viewer whose first trigger of
        # the day legitimately plans an outbound message (conversion goal)
        d1 = viewer = None
        for v in candidates:
            t1 = make_trigger(v, "2026-09-18T07:00:00Z", event_id=f"CAP-1-{v}")
            d = agent.decide(t1, parse_ts(t1["deliver_at"]))          # 12:30 IST
            if d["action"] in ("PUSH", "WHATSAPP"):
                d1, viewer = d, v
                break
        self.assertIsNotNone(d1, "no candidate viewer planned an outbound message")

        t2 = make_trigger(viewer, "2026-09-18T09:00:00Z", event_id=f"CAP-2-{viewer}")
        d2 = agent.decide(t2, parse_ts(t2["deliver_at"]))              # 14:30 IST
        published = [a for a in store.all_actions()
                     if a["action"] in ("PUSH", "WHATSAPP") and a["status"] == "PUBLISHED"]
        self.assertEqual(len(published), 1, "only one outbound message per IST day")
        self.assertNotIn(d2["action"], ("PUSH", "WHATSAPP"),
                         f"second message slipped through: {d2['reason']}")


class TestHoldout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack = get_pack()

    def test_holdout_viewer_gets_zero_actions(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        store = fresh_store(Path(tmp.name))
        self.addCleanup(store.close)
        agent = make_agent(self.pack, store)
        viewer = holdout_viewer(self.pack)
        trig = make_trigger(viewer, "2026-09-18T10:00:00Z", event_id="HOLD-1")
        d = agent.decide(trig, parse_ts(trig["deliver_at"]))
        self.assertEqual(d["action"], "NONE")
        self.assertTrue(str(d["no_action_reason"]).startswith("holdout"))
        self.assertEqual(d["checks"]["passed"], ["holdout"])
        self.assertEqual([a for a in store.all_actions()
                          if a["viewer_id"] == viewer], [])
        # facts still refreshed read-only so audits work
        self.assertTrue(store.get_viewer(viewer)["facts"])


class TestInjectedInstructions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack = get_pack()

    def test_catalogue_injections_are_flagged_not_followed(self):
        self.assertGreaterEqual(len(self.pack.injected), 5,
                                "fixture must contain injected descriptions")
        phrases = ("message every", "send a whatsapp", "bypass quiet hours",
                   "regardless of consent", "previous instructions")
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        store = fresh_store(Path(tmp.name))
        self.addCleanup(store.close)
        agent = make_agent(self.pack, store)
        viewer = agent_arm_viewer(self.pack)
        trig = make_trigger(viewer, "2026-09-18T10:00:00Z", event_id="INJ-1")
        d = agent.decide(trig, parse_ts(trig["deliver_at"]))
        blob = json.dumps(d.get("content", [])) + json.dumps(d.get("evidence", []))
        # descriptions never enter the decision payload as instructions
        for inj in self.pack.injected:
            self.assertNotIn(inj["match"].lower(), blob.lower())
        for p in phrases:
            self.assertNotIn(p, blob.lower())
        # and no action copy anywhere repeats an injected phrase
        planner = InterventionPlanner(self.pack)
        facts = facts_mod.resolve(self.pack, viewer, parse_ts("2026-09-18T10:00:00Z"))
        choice = GoalPlanner(self.pack).choose(facts, parse_ts("2026-09-18T10:00:00Z"))
        prop = planner.plan(choice, facts, parse_ts("2026-09-18T10:00:00Z"), None)
        copy_blob = json.dumps(prop.copy, ensure_ascii=False).lower()
        for p in phrases:
            self.assertNotIn(p, copy_blob)


class TestUnsupportedModelClaim(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack = get_pack()

    def test_verifier_rejects_unsupported_eligibility_claim(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        store = fresh_store(Path(tmp.name))
        self.addCleanup(store.close)
        viewer = agent_arm_viewer(self.pack)
        now = parse_ts("2026-09-18T10:00:00Z")
        facts = facts_mod.resolve(self.pack, viewer, now)
        choice = GoalPlanner(self.pack).choose(facts, now)
        prop = InterventionPlanner(self.pack).plan(choice, facts, now, None)
        bad = ModelResult(status="OK", raw="{}",
                          proposal={"goal_id": "watchlist_signal_7d",
                                    "action": "PUSH", "confidence": 0.93,
                                    "evidence": [], "confidence_reason": "intuition"},
                          error="unsupported claim")
        trig = make_trigger(viewer, "2026-09-18T10:00:00Z")
        res = Verifier(self.pack, store).verify(prop, choice, facts, trig, now,
                                                model_result=bad)
        names = [f["check"] for f in res.failed]
        self.assertIn("model_claim", names, "unsupported model claim must be rejected")
        self.assertFalse(res.blocked, "rejecting a bad hint must not block the rule plan")


class TestHomePageShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack = get_pack()

    def test_row_with_fewer_than_three_titles_is_dropped(self):
        now = parse_ts("2026-09-18T10:00:00Z")
        viewer = agent_arm_viewer(self.pack)
        facts = facts_mod.resolve(self.pack, viewer, now)
        two = [t for t in self.pack.dialect_titles("d1", now)][:2]
        _, rows, _ = build_home_page(self.pack, facts, two, now)
        self.assertEqual(rows, [], "a row below the 3-title minimum must be dropped")
        # and the planner then chooses to do nothing rather than publish a husk
        prop = Proposal(action="NONE", reason="", goal_id="first_full_episode_3d",
                        dialect={"used": "d1", "source": "app_setting", "conflicts": []})
        InterventionPlanner(self.pack)._fill_home_page(prop, facts, now, two,
                                                       GoalPlanner(self.pack).choose(facts, now))
        self.assertEqual(prop.action, "NONE")
        self.assertIn("3-title minimum", prop.reason)

    def test_withdrawn_title_is_unavailable_and_never_planned(self):
        now = parse_ts("2026-09-18T20:00:00Z")
        tid = next(iter(self.pack.by_title_id))
        original = self.pack.by_title_id[tid]["withdrawn_at"]
        try:
            self.pack.by_title_id[tid]["withdrawn_at"] = "2026-09-18T12:00:00Z"
            self.assertFalse(self.pack.is_available(tid, now))
            dialect = self.pack.by_title_id[tid]["dialect"]
            pool = [t["title_id"] for t in self.pack.dialect_titles(dialect, now)]
            self.assertNotIn(tid, pool)
            # verifier must also block it if a bad proposal slips through
            viewer = agent_arm_viewer(self.pack)
            facts = facts_mod.resolve(self.pack, viewer, now)
            choice = GoalPlanner(self.pack).choose(facts, now)
            prop = Proposal(action="PUSH", reason="test", goal_id=choice.goal_id,
                            dialect=facts.dialect, content=[f"title:{tid}"],
                            copy={"title": "x", "body": "y"},
                            deeplink=f"ott://title/{tid}",
                            evidence=[f"catalogue:{tid}"])
            tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
            self.addCleanup(tmp.cleanup)
            store = fresh_store(Path(tmp.name))
            self.addCleanup(store.close)
            res = Verifier(self.pack, store).verify(prop, choice, facts,
                                                    make_trigger(viewer, "2026-09-18T20:00:00Z"),
                                                    now)
            self.assertIn("availability", [f["check"] for f in res.failed])
            self.assertTrue(res.blocked)
        finally:
            self.pack.by_title_id[tid]["withdrawn_at"] = original


if __name__ == "__main__":
    unittest.main()
