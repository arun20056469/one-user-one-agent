"""
agent.py - one agent per viewer: decide -> verify -> publish, within budget.

Flow for a single trigger:
  1 idempotency (same trigger event_id twice => one decision, zero extra actions)
  2 staleness (offline-buffered opens: received_at vs occurred_at)
  3 facts (provenance + conflicts)        [tool call]
  4 goal (keep/rechoose, or abstain)      [tool call]
  5 intervention plan (rule-based)
  6 model provider hint, only if budget and fault window allow (advisory)
  7 independent verification (can block or downgrade)
  8 publish with measured latency (published_at = deliver_at + decided_in_ms)

Budgets (configurable): max_model_calls=8, max_tool_calls=20 per decision,
latency target 60 s (hard fail at 5 min). On provider/store failure the agent
falls back to the rule plan or to no action - never to a blind guess.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from . import facts as facts_mod
from .goals import GoalPlanner
from .interventions import InterventionPlanner, Proposal
from .pack import Pack
from .provider import ModelResult, ProviderError
from .state import StateStore
from .utils import in_quiet_hours, ist, parse_ts, z
from .verifier import Verifier

MAX_MODEL_CALLS = 8
MAX_TOOL_CALLS = 20
STALE_SKIP_S = 1800          # offline open older than 30 min => do not act
STALE_DOWNGRADE_S = 300      # 5-30 min late => in-session surfaces only


@dataclass
class Budget:
    max_model_calls: int = MAX_MODEL_CALLS
    max_tool_calls: int = MAX_TOOL_CALLS
    model_calls: int = 0
    tool_calls: int = 0

    def tool(self) -> bool:
        if self.tool_calls >= self.max_tool_calls:
            return False
        self.tool_calls += 1
        return True

    def model(self) -> bool:
        if self.model_calls >= self.max_model_calls:
            return False
        self.model_calls += 1
        return True


@dataclass
class Ctx:
    surge: bool = False
    datastore_slow: bool = False


class Agent:
    def __init__(self, pack: Pack, store: StateStore, provider, ctx: Ctx | None = None):
        self.pack = pack
        self.store = store
        self.provider = provider
        self.ctx = ctx or Ctx()
        self.goal_planner = GoalPlanner(pack)
        self.intervention_planner = InterventionPlanner(pack)
        self.verifier = Verifier(pack, store)

    # ------------------------------------------------------------------ #
    def decide(self, trigger: dict, now: datetime, extra_events: list[dict] | None = None) -> dict:
        t0 = time.perf_counter()
        budget = Budget()
        deliver_at = z(trigger["_deliver"]) if isinstance(trigger.get("_deliver"), datetime) else trigger["deliver_at"]
        viewer_id = trigger["viewer_id"]
        decision_id = "D-" + uuid.uuid4().hex[:10].upper()

        base = {
            "decision_id": decision_id,
            "viewer_id": viewer_id,
            "trigger": {
                "event": trigger["event_type"],
                "event_id": trigger["event_id"],
                "received_at": trigger["received_at"],
                "occurred_at": trigger["occurred_at"],
                "event_age_s": int((datetime.fromisoformat(
                    trigger["received_at"].replace("Z", "+00:00")) -
                    datetime.fromisoformat(trigger["occurred_at"].replace("Z", "+00:00"))).total_seconds()),
            },
            "model_calls": 0, "tool_calls": 0,
            "rule_version": self.pack.rule_version,
            "fallback_used": None,
            "skipped_goals": [],
        }

        # ---- trigger as evidence (deduplicated by event_id) ---------------- #
        # The trigger itself carries facts (its own app_dialect, the app_open
        # that fired it), so it is always folded into the evidence - callers
        # cannot forget it, and harness-passed copies are de-duplicated.
        trig_ev = dict(trigger)
        trig_ev.setdefault("props", {})
        trig_ev["_received"] = parse_ts(trigger["received_at"])
        trig_ev["_occurred"] = parse_ts(trigger["occurred_at"])
        extra_map = {e["event_id"]: e for e in (extra_events or []) if e.get("event_id")}
        extra_map[trig_ev["event_id"]] = trig_ev
        evidence_events = list(extra_map.values())

        # ---- 1. idempotency ---------------------------------------------- #
        if self.store.trigger_seen(trigger["event_id"]):
            d = _finish(base, t0, goal=None, action="NONE",
                        reason="duplicate_trigger: already decided from this event_id",
                        checks={"passed": ["idempotency", "holdout"], "failed": []},
                        status="SUPPRESSED", confidence=1.0,
                        confidence_reason="identical trigger re-delivered; suppressed by idempotency key",
                        deliver_at=deliver_at)
            self._persist(d, None, store_action=False)
            return d

        # ---- 2. staleness -------------------------------------------------- #
        age_s = base["trigger"]["event_age_s"]
        stale_reason = None
        if age_s > STALE_SKIP_S:
            stale_reason = (f"stale_trigger: occurred_at is {age_s}s before receipt "
                            "(offline-buffered, not a live session)")
        elif age_s > STALE_DOWNGRADE_S:
            stale_reason = f"downgraded_trigger: {age_s}s old -> no outbound messaging"

        # ---- holdout shortcut (read-only, zero actions) -------------------- #
        arm = self.pack.arms.get(viewer_id)
        if arm == "holdout":
            budget.tool()
            facts = facts_mod.resolve(self.pack, viewer_id, now, evidence_events) if budget.tool() else None
            d = _finish(base, t0, goal=None, action="NONE",
                        reason="holdout: randomized control viewer receives zero actions",
                        checks={"passed": ["holdout"], "failed": []},
                        status="PUBLISHED", confidence=1.0,
                        confidence_reason="holdout assignment is authoritative",
                        deliver_at=deliver_at, dialect=facts.dialect if facts else None,
                        evidence=facts.evidence if facts else None,
                        conflicts=facts.conflicts if facts else None)
            if facts is not None:
                viewer = self.store.get_viewer(viewer_id)
                viewer["facts"] = facts.to_dict()
                viewer["active_goal"] = None
                viewer["updated_at"] = z(now)
                self.store.put_viewer(viewer)
            self._persist(d, None, store_action=False)
            return d

        # ---- 3. facts ------------------------------------------------------ #
        if not budget.tool():
            return self._degraded(base, t0, viewer_id, now, deliver_at, "tool_budget_exhausted")
        try:
            facts = facts_mod.resolve(self.pack, viewer_id, now, evidence_events)
        except Exception as exc:  # data store failure -> safe fallback
            return self._degraded(base, t0, viewer_id, now, deliver_at, f"data_failure:{exc}")

        try:
            viewer = self.store.get_viewer(viewer_id)
        except Exception as exc:  # state store down -> no action, recorded
            return self._degraded(base, t0, viewer_id, now, deliver_at,
                                  f"state_store_failure:{exc}", facts=facts)
        viewer["facts"] = facts.to_dict()
        viewer["updated_at"] = z(now)

        # ---- 4. goal ------------------------------------------------------- #
        if not budget.tool():
            return self._degraded(base, t0, viewer_id, now, deliver_at, "tool_budget_exhausted", facts=facts)
        choice = self.goal_planner.choose(facts, now, active_goal=viewer.get("active_goal"))
        base["skipped_goals"] = choice.skipped

        # ---- 5. intervention plan ------------------------------------------ #
        proposal = self.intervention_planner.plan(choice, facts, now, viewer.get("last_msg_ist_day"))
        if age_s > STALE_SKIP_S:
            # offline-buffered open, days/minutes old: not a live session
            proposal.action = "NONE"
            proposal.reason = stale_reason
            proposal.confidence = 0.9
            proposal.confidence_reason = "stale trigger must not create a live action"
        elif stale_reason:
            # ageing trigger (5-30 min): no outbound messaging, in-session only
            if proposal.action in ("PUSH", "WHATSAPP"):
                # (scheduled or not: _home_only clears any pending schedule)
                proposal = self._home_only(proposal, facts, now, choice, stale_reason)
            elif proposal.action != "NONE":
                # already an in-session action: keep it, but the decision must
                # show that the trigger was downgraded
                proposal.reason = f"{stale_reason}; {proposal.reason}"
                proposal.confidence_reason = "ageing trigger: outbound messaging suppressed"

        # ---- 6. model hint (budget + faults) -------------------------------- #
        model_result: ModelResult | None = None
        if self.ctx.surge:
            model_result = ModelResult(status="SKIPPED", mode="surge",
                                       error="evening peak: optional model call skipped to protect latency")
        elif budget.model():
            model_result = self.provider.propose({
                "viewer_id": viewer_id, "now": now, "deliver_at": deliver_at,
                "rule_goal": choice.goal_id, "rule_action": proposal.action,
                "rule_evidence": proposal.evidence,
                "ineligible_goals": [i.split(":", 1)[0] for i in choice.ineligible][:3],
            })
            if model_result.status == "TIMEOUT":
                budget.model()  # one retry inside budget
                model_result = self.provider.propose({
                    "viewer_id": viewer_id, "now": now, "deliver_at": deliver_at,
                    "rule_goal": choice.goal_id, "rule_action": proposal.action,
                    "rule_evidence": proposal.evidence,
                    "ineligible_goals": [i.split(":", 1)[0] for i in choice.ineligible][:3],
                })
            if model_result.status in ("TIMEOUT", "PARSE_ERROR"):
                # safe fallback: the deterministic rule plan, no model content
                base["fallback_used"] = f"provider_{model_result.status.lower()}->rules"
        if model_result and model_result.status == "OK" and model_result.proposal:
            # advisory only: a model goal we are not eligible for is ignored here
            # and recorded as an unsupported claim by the verifier.
            pass

        # ---- 7. verify ------------------------------------------------------ #
        checks = self.verifier.verify(proposal, choice, facts, trigger, now,
                                      model_result=model_result)
        status = "PUBLISHED"
        no_action_reason = None
        if checks.blocked:
            if proposal.action != "NONE":
                status = "BLOCKED"
                no_action_reason = f"blocked_by_verifier: {checks.block_reason}"
            proposal_copy_action = proposal.action
            proposal.action = "NONE"
            proposal.reason = no_action_reason or proposal.reason
            checks = self.verifier.verify(proposal, choice, facts, trigger, now,
                                          model_result=model_result)
            proposal.blocked_action = proposal_copy_action  # type: ignore[attr-defined]

        # ---- 8. publish ------------------------------------------------------ #
        decided_ms = int((time.perf_counter() - t0) * 1000)
        goal_out = None
        if choice.goal_id:
            goal_out = {"id": choice.goal_id, "deadline": choice.deadline,
                        "success_signal": choice.success_signal,
                        "rule_version": choice.rule_version}
            viewer["active_goal"] = {"id": choice.goal_id, "deadline": choice.deadline,
                                     "success_signal": choice.success_signal,
                                     "rule_version": choice.rule_version,
                                     "started_at": z(now), "status": "open",
                                     "evidence": choice.evidence,
                                     "assumptions": choice.assumptions}
        d = _finish(base, t0, goal=goal_out, action=proposal.action,
                    reason=proposal.reason, checks=checks, status=status,
                    confidence=proposal.confidence,
                    confidence_reason=proposal.confidence_reason,
                    deliver_at=deliver_at, dialect=facts.dialect,
                    content=proposal.content, evidence=proposal.evidence,
                    assumptions=proposal.assumptions + choice.assumptions,
                    no_action_reason=no_action_reason,
                    conflicts=facts.conflicts, decided_ms=decided_ms,
                    model_status=model_result.status if model_result else "not_called",
                    model_error=model_result.error if model_result else "",
                    schedule_for=getattr(proposal, "schedule_for", None),
                    budget=budget)
        if status == "PUBLISHED" and proposal.action != "NONE":
            action = self._action_record(d, proposal, trigger, now, decided_ms)
            d["action_id"] = action["action_id"]
            self._persist(d, action, store_action=True, viewer=viewer,
                          proposal=proposal, now=now, last_msg_ist_day=viewer.get("last_msg_ist_day"))
        else:
            self._persist(d, None, store_action=False, viewer=viewer)
        return d

    # ------------------------------------------------------------------ #
    def _home_only(self, proposal: Proposal, facts, now, choice, why: str) -> Proposal:
        """Degrade an outbound message to the in-session home page."""
        fresh = Proposal(action="HOME_PAGE", reason=why, goal_id=choice.goal_id,
                         dialect=dict(facts.dialect), evidence=list(proposal.evidence),
                         assumptions=list(proposal.assumptions))
        if facts.dialect.get("used"):
            avail = self.pack.dialect_titles(facts.dialect["used"], now)
            self.intervention_planner._fill_home_page(fresh, facts, now, avail, choice)
        fresh.schedule_for = None
        if fresh.action == "NONE":
            fresh.reason = f"{why}; no safe in-session fallback ({fresh.reason})"
        else:
            fresh.reason = f"{why}; in-session home page instead of a message"
        fresh.confidence = proposal.confidence
        fresh.confidence_reason = "stale/ageing trigger: outbound messaging suppressed"
        return fresh

    def _degraded(self, base, t0, viewer_id, now, deliver_at, reason, facts=None) -> dict:
        d = _finish(base, t0, goal=None, action="NONE",
                    reason=f"degraded_no_action: {reason}",
                    checks={"passed": ["degraded_safe_state"], "failed": []},
                    status="FALLBACK", confidence=1.0,
                    confidence_reason="dependency failed; doing nothing is the safe action",
                    deliver_at=deliver_at, dialect=facts.dialect if facts else None)
        self._persist(d, None, store_action=False)
        return d

    def _action_record(self, decision: dict, proposal, trigger: dict, now: datetime,
                       decided_ms: int) -> dict:
        scheduled = getattr(proposal, "schedule_for", None)
        status = "SCHEDULED" if scheduled else "PUBLISHED"
        published_at = scheduled or z(now)
        blocked_action = getattr(proposal, "blocked_action", None)
        return {
            "action_id": "A-" + uuid.uuid4().hex[:10].upper(),
            "decision_id": decision["decision_id"],
            "viewer_id": decision["viewer_id"],
            "trigger_event_id": trigger["event_id"],
            "action": proposal.action,
            "status": status,
            "blocked_action": blocked_action,
            "goal_id": decision.get("goal", {}).get("id") if decision.get("goal") else None,
            "dialect": decision["dialect"]["used"],
            "content": proposal.content,
            "copy": proposal.copy,
            "deeplink": proposal.deeplink,
            "idempotency_key": trigger["event_id"],
            "published_at": published_at,
            "scheduled_for": scheduled,
            "decided_in_ms": decided_ms,
            "reason": proposal.reason,
        }

    def _persist(self, decision, action, store_action: bool, viewer=None,
                 proposal=None, now=None, last_msg_ist_day=None):
        self.store.mark_trigger(decision["trigger"]["event_id"], decision["decision_id"])
        self.store.add_decision(decision)
        if store_action and action is not None:
            self.store.add_action(action)
            if viewer is not None:
                v = dict(viewer)
                if action["action"] in ("PUSH", "WHATSAPP"):
                    if action["status"] == "SCHEDULED":
                        pass  # cap applies on the send day
                    else:
                        v["last_msg_ist_day"] = ist(now).date().isoformat()
                elif action["action"] == "IN_APP":
                    v["last_msg_ist_day"] = f"inapp:{ist(now).date().isoformat()}"
                v["last_action"] = {"action_id": action["action_id"], "action": action["action"],
                                    "status": action["status"], "at": action["published_at"]}
                self.store.put_viewer(v)
        elif viewer is not None:
            self.store.put_viewer(viewer)


def _finish(base: dict, t0: float, *, goal, action, reason, checks, status,
            confidence, confidence_reason, deliver_at, dialect=None, content=None,
            evidence=None, assumptions=None, no_action_reason=None, conflicts=None,
            decided_ms=None, model_status=None, model_error=None, schedule_for=None,
            budget=None) -> dict:
    d = dict(base)
    d.update({
        "goal": goal,
        "action": action,
        "dialect": dialect or {"used": None, "source": "unknown", "conflicts": []},
        "content": content or [],
        "confidence": confidence,
        "confidence_reason": confidence_reason,
        "evidence": evidence or [],
        "assumptions": assumptions or [],
        "conflicting_evidence": conflicts or [],
        "checks": checks if isinstance(checks, dict) else {"passed": checks.passed, "failed": checks.failed},
        "no_action_reason": no_action_reason or (reason if action == "NONE" else None),
        "decided_in_ms": decided_ms if decided_ms is not None else int((time.perf_counter() - t0) * 1000),
        "status": status,
        "reason": reason,
        "deliver_at": deliver_at,
        "published_at": None,
        "model_status": model_status,
        "model_error": model_error,
        "scheduled_for": schedule_for,
    })
    if budget is not None:
        d["model_calls"] = budget.model_calls
        d["tool_calls"] = budget.tool_calls
    return d
