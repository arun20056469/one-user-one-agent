"""
provider.py - model provider abstraction.

Default is MOCK / REPLAY mode: no API key, deterministic, reproducible. It
reproduces every fault listed in replay/model_faults.jsonl:

  timeout      -> ProviderError (decision must fall back to rules)
  broken_json  -> unparseable payload (parse safely, retry once, then fall back)
  hallucinate  -> a confident goal claim with NO supporting evidence
                  (the verifier must reject it as an unsupported claim)
  (the harness separately reads control_events for datastore slowness,
   traffic surges and goal-rule corrections)

The provider is only ever an ADVISOR: its proposal is a hint compared with
the rule-based plan. Model output is never evidence and never approval - the
verifier decides whether anything may be published.

Swap-in real models: implement ModelProvider.propose() against any hosted or
local model and pass --provider real. Budgets (max model calls per decision)
are enforced by the agent, not the provider.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime

from .pack import Pack
from .utils import parse_ts


class ProviderError(Exception):
    """Model provider timed out or is unavailable."""


@dataclass
class ModelResult:
    status: str                      # OK | TIMEOUT | PARSE_ERROR | SKIPPED
    raw: str = ""
    proposal: dict | None = None     # {goal_id, action, confidence, evidence}
    error: str = ""
    mode: str = "ok"


def _det(seed_src: str) -> int:
    return int.from_bytes(hashlib.sha256(seed_src.encode()).digest()[:8], "big")


class MockModelProvider:
    """Deterministic replay-mode provider."""

    def __init__(self, pack: Pack):
        self.pack = pack
        self.faults = sorted(pack.model_faults, key=lambda f: f["from"])

    def mode_at(self, now: datetime) -> tuple[str, dict | None]:
        for f in self.faults:
            if parse_ts(f["from"]) <= now < parse_ts(f["to"]):
                return f["mode"], f
        return "ok", None

    # ------------------------------------------------------------------ #
    def propose(self, ctx: dict) -> ModelResult:
        """ctx: {viewer_id, now, rule_goal, rule_action, ineligible_goals, deliver_at}"""
        now = ctx["now"]
        mode, fault = self.mode_at(now)

        if mode == "timeout":
            delay = getattr(self, "timeout_delay_s", 0.0)
            if delay:
                import time as _time
                _time.sleep(delay)  # the provider really is slow right now
            return ModelResult(status="TIMEOUT", mode=mode, error=fault.get("note", "provider timeout"))
        if mode == "broken_json":
            return ModelResult(status="PARSE_ERROR", mode=mode, raw="{goal_id: 'x', confidence: ",
                               error="malformed JSON from provider")
        if mode == "hallucinate":
            # confident claim for a goal the viewer is clearly NOT eligible for
            bad = (ctx.get("ineligible_goals") or ["watchlist_signal_7d"])[0]
            return ModelResult(
                status="OK", mode=mode,
                raw=json.dumps({"goal_id": bad, "action": "PUSH", "confidence": 0.93}),
                proposal={"goal_id": bad, "action": "PUSH", "confidence": 0.93,
                          "evidence": [], "confidence_reason": "model intuition"},
                error="unsupported eligibility claim (no backing event)")

        # healthy mode: mostly agrees with the rule plan, with a wobble of noise
        d = _det(f"{ctx['viewer_id']}|{ctx['deliver_at']}")
        conf = round(0.55 + (d % 35) / 100.0, 2)
        proposal = {
            "goal_id": ctx.get("rule_goal"),
            "action": ctx.get("rule_action"),
            "confidence": conf,
            "evidence": ctx.get("rule_evidence", [])[:3],
            "confidence_reason": "mock model: rules echo with calibrated noise (replay mode)",
        }
        return ModelResult(status="OK", mode=mode, raw=json.dumps(proposal), proposal=proposal)


def get_provider(pack: Pack, name: str = "mock"):
    if name in ("mock", "replay", "mock_replay"):
        return MockModelProvider(pack)
    raise ValueError(
        f"provider '{name}' is not configured. Implement ModelProvider.propose() "
        "for your hosted/local model and keep --provider mock for evaluators.")
