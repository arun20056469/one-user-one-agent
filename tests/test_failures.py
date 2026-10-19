"""Required test 3: a tool failure forces a safe fallback.

- model provider timeout (and its retry) -> deterministic rule plan still
  published, decision records fallback_used;
- provider returns broken JSON -> parse safely, retry once, fall back;
- state store raises -> decision recorded as FALLBACK with no action;
- traffic surge -> optional model call skipped entirely (latency protected).
"""
import tempfile
import unittest
from pathlib import Path

from src.agent import Ctx
from src.utils import parse_ts
from tests.helpers import (agent_arm_viewer, fresh_store, get_pack, make_agent,
                           make_trigger)
from src.provider import MockModelProvider


class TestFailures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack = get_pack()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = fresh_store(Path(self.tmp.name))
        self.viewer = agent_arm_viewer(self.pack)

    def tearDown(self):
        self.store.close()   # release the SQLite handle before deleting files
        self.tmp.cleanup()

    def _decide_with_fault(self, mode: str):
        provider = MockModelProvider(self.pack)
        provider.faults = [{"from": "2000-01-01T00:00:00Z",
                            "to": "2100-01-01T00:00:00Z", "mode": mode,
                            "note": "test fault"}]
        agent = make_agent(self.pack, self.store, provider=provider)
        trig = make_trigger(self.viewer, "2026-09-18T10:00:00Z", event_id=f"FAULT-{mode}")
        return agent.decide(trig, parse_ts(trig["deliver_at"]))

    def test_provider_timeout_falls_back_to_rule_plan(self):
        d = self._decide_with_fault("timeout")
        self.assertEqual(d["status"], "PUBLISHED")
        self.assertEqual(d["fallback_used"], "provider_timeout->rules")
        self.assertIn(d["model_status"], ("TIMEOUT",))
        self.assertIsNotNone(d["goal"], "the rule plan must still pick a goal")
        self.assertTrue(self.store.all_actions(), "safe action must still be published")

    def test_broken_json_falls_back_after_parse_failure(self):
        d = self._decide_with_fault("broken_json")
        self.assertEqual(d["status"], "PUBLISHED")
        self.assertEqual(d["fallback_used"], "provider_parse_error->rules")
        self.assertEqual(d["model_status"], "PARSE_ERROR")
        self.assertTrue(self.store.all_actions())

    def test_state_store_failure_yields_no_action_with_reason(self):
        agent = make_agent(self.pack, self.store)

        def boom(viewer_id):
            raise RuntimeError("simulated store outage")

        self.store.get_viewer = boom
        trig = make_trigger(self.viewer, "2026-09-18T10:00:00Z", event_id="STORE-1")
        d = agent.decide(trig, parse_ts(trig["deliver_at"]))
        self.assertEqual(d["status"], "FALLBACK")
        self.assertEqual(d["action"], "NONE")
        self.assertIn("state_store_failure", d["reason"])
        self.assertEqual(len(self.store.all_actions()), 0)

    def test_traffic_surge_skips_optional_model_call(self):
        ctx = Ctx()
        ctx.surge = True
        agent = make_agent(self.pack, self.store, ctx=ctx)
        trig = make_trigger(self.viewer, "2026-09-18T14:00:00Z", event_id="SURGE-1")
        d = agent.decide(trig, parse_ts(trig["deliver_at"]))
        self.assertEqual(d["model_status"], "SKIPPED")
        self.assertEqual(d["model_calls"], 0)
        self.assertIn("model call skipped", str(d["model_error"]))

    def test_budgets_are_recorded_and_bounded(self):
        d = self._decide_with_fault("ok")
        self.assertLessEqual(d["model_calls"], 8)
        self.assertLessEqual(d["tool_calls"], 20)


if __name__ == "__main__":
    unittest.main()
