"""
goals.py - goal planner.

For each viewer: evaluate EVERY goal in config/goals.json against resolved
facts and deduplicated evidence, then pick exactly one active goal (highest
priority that is eligible and measurable), or return an explicit abstention
with a reason.

Guards:
  * a goal whose success event does not exist in the data (watchlist_signal_7d)
    is never chosen - we refuse to run goals we could not judge.
  * goals marked `underspecified` carry their interpretation as an assumption
    on every decision that uses them.
  * eligibility claims must be backed by evidence refs; anything the model
    asserts without evidence is dropped later by the verifier anyway.

Deadlines: goal_start (the trigger time) + window_days. One active goal per
viewer; the goal is revised only when the situation changes (purchase, new
evidence, rule correction), never on a fixed schedule.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .facts import ViewerFacts
from .pack import Pack
from .utils import parse_ts, z


@dataclass
class GoalChoice:
    goal_id: str | None
    deadline: str | None
    success_signal: str | None
    rule_version: str
    evidence: list = field(default_factory=list)
    assumptions: list = field(default_factory=list)
    reason: str = ""
    eligible: list = field(default_factory=list)
    ineligible: list = field(default_factory=list)
    skipped: list = field(default_factory=list)


class GoalPlanner:
    def __init__(self, pack: Pack):
        self.pack = pack

    # ------------------------------------------------------------------ #
    def _check(self, goal: dict, facts: ViewerFacts, now: datetime) -> tuple[bool, list[str], str]:
        """Returns (eligible, evidence_refs, why_not)."""
        el = goal.get("eligibility", {})
        ev: list[str] = []
        f = facts
        signup = f.signup.get("value")
        age_days = None
        if signup:
            age_days = (now - parse_ts(signup)).total_seconds() / 86400.0

        if "min_age_days" in el:
            if age_days is None or age_days < el["min_age_days"]:
                return False, [], f"viewer age unknown or < {el['min_age_days']}d"
            ev.append(f"signup:{signup}")
        if "subscription_in" in el:
            if f.subscription["value"] not in el["subscription_in"]:
                return False, [], f"subscription={f.subscription['value']} not in {el['subscription_in']}"
            ev.append(f.subscription["source"])
        if el.get("never_completed_any_episode"):
            if f.engagement["completes"] > 0:
                return False, [], "already completed an episode"
            if f.engagement["opens"] < el.get("min_opens", 0):
                return False, [], "not enough opens"
            ev.append(f"engagement:completes=0,opens={f.engagement['opens']}")
        if el.get("has_play_no_complete"):
            if f.engagement["plays"] == 0 or f.engagement["completes"] > 0:
                return False, [], "no started-but-unfinished episode"
            ev.append(f"event:plays={f.engagement['plays']},completes=0")
        if "completed_episode_count_in" in el:
            if f.engagement["completed_episode_count"] not in el["completed_episode_count_in"]:
                return False, [], f"completed={f.engagement['completed_episode_count']}"
            ev.append(f"engagement:completed={f.engagement['completed_episode_count']}")
        if "recent_search_no_play_hours" in el:
            hours = el["recent_search_no_play_hours"]
            last_search = f.engagement.get("last_search_at")
            last_play = f.engagement.get("last_play_at")
            if not last_search:
                return False, [], "no recent search"
            if (now - parse_ts(last_search)).total_seconds() > hours * 3600:
                return False, [], "search older than window"
            if last_play and parse_ts(last_play) > parse_ts(last_search):
                return False, [], "already played after the search"
            ev.append(f"search:{last_search}")
        if "dormant_days" in el:
            last_open = f.engagement.get("last_open_at")
            if last_open and (now - parse_ts(last_open)).total_seconds() < el["dormant_days"] * 86400:
                return False, [], "opened recently - not dormant"
            ev.append(f"last_open:{last_open}")
        if "whatsapp_opt_in" in el:
            if f.consent["whatsapp"]["value"] != el["whatsapp_opt_in"]:
                return False, [], "whatsapp consent state does not match"
            ev.append(f.consent["whatsapp"]["source"])
        if "notifications_opt_in" in el:
            if f.consent["notifications"]["value"] != el["notifications_opt_in"]:
                return False, [], "notification consent state does not match"
            ev.append(f.consent["notifications"]["source"])
        return True, ev, ""

    # ------------------------------------------------------------------ #
    def choose(self, facts: ViewerFacts, now: datetime, active_goal: dict | None = None) -> GoalChoice:
        eligible: list[tuple[dict, list[str]]] = []
        ineligible: list[str] = []
        skipped: list[str] = []
        for g in sorted(self.pack.goals.values(), key=lambda g: -g.get("priority", 0)):
            if not self.pack.success_event_known(g):
                skipped.append(f"{g['id']}(unmeasurable: success event "
                               f"'{g['success']['event']}' not in event stream)")
                continue
            ok, ev, why = self._check(g, facts, now)
            if ok:
                eligible.append((g, ev))
            else:
                ineligible.append(f"{g['id']}:{why}")

        # keep the active goal while it is still eligible (ownership: one goal)
        if active_goal and active_goal.get("id") in {g["id"] for g, _ in eligible}:
            g = self.pack.goals[active_goal["id"]]
            ev = next(e for gg, e in eligible if gg["id"] == g["id"])
            return GoalChoice(
                goal_id=g["id"], deadline=active_goal["deadline"],
                success_signal=active_goal.get("success_signal") or self._signal(g),
                rule_version=active_goal.get("rule_version", self.pack.rule_version),
                evidence=ev, assumptions=self._assumptions(g),
                reason="kept existing active goal (still eligible)",
                eligible=[x["id"] for x, _ in eligible], ineligible=ineligible,
                skipped=skipped)

        if not eligible:
            return GoalChoice(goal_id=None, deadline=None, success_signal=None,
                              rule_version=self.pack.rule_version,
                              reason=("no eligible goal: " + ("; ".join(ineligible) or "none")
                                      + (f" | skipped: {'; '.join(skipped)}" if skipped else "")),
                              ineligible=ineligible, skipped=skipped)

        g, ev = eligible[0]
        start = now
        deadline = start + timedelta(days=g["window_days"])
        return GoalChoice(
            goal_id=g["id"], deadline=z(deadline), success_signal=self._signal(g),
            rule_version=self.pack.rule_version, evidence=ev,
            assumptions=self._assumptions(g),
            reason=f"highest-priority eligible goal ({len(eligible)} eligible)",
            eligible=[x["id"] for x, _ in eligible], ineligible=ineligible,
            skipped=skipped)

    def _signal(self, goal: dict) -> str:
        s = goal["success"]
        extra = f" (distinct_days={s['distinct_days']})" if s.get("distinct_days") else ""
        return f"{s['event']} x{s.get('min_count', 1)}{extra} within {goal['window_days']}d"

    def _assumptions(self, goal: dict) -> list[str]:
        out = []
        if goal.get("underspecified"):
            out.append(f"underspecified rule interpreted: {goal['underspecified']}")
        return out
