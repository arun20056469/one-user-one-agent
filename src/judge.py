"""
judge.py - close the loop.

When each goal's deadline has passed, judge it SUCCEEDED / FAILED /
INCONCLUSIVE from attributable signals only:

  * simulator outcome events (14 days after the replay day) plus real pack
    events that arrived inside the goal window;
  * an event only counts when its occurred_at falls inside
    [goal_start, deadline] - late arrivals cannot borrow success from
    outside the window;
  * a corrected rule (control_events goal_rule_correction) is applied ONLY to
    open goals of that goal id; every other goal keeps its original rule and
    every closed goal stays closed (no mass reprocessing);
  * holdout viewers get no actions, but for a fair comparison the judge runs
    the SAME planner on their trigger in shadow mode (no action, evidence
    only), so both arms are scored on identical criteria;
  * learnings are written back into each viewer's durable state.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .facts import resolve
from .goals import GoalPlanner
from .pack import Pack
from .state import StateStore
from .utils import ist, parse_ts, read_jsonl, write_jsonl, z


def _as_trigger_event(t: dict) -> dict:
    e = dict(t)
    e["_received"] = parse_ts(t["received_at"])
    e["_occurred"] = parse_ts(t["occurred_at"])
    return e


class OutcomeJudge:
    def __init__(self, pack: Pack, run_dir: str | Path):
        self.pack = pack
        self.run = Path(run_dir)
        self.store = StateStore(self.run / "viewer_state" / "state.db")
        self.planner = GoalPlanner(pack)

    def close(self) -> None:
        """Release the underlying SQLite handle (see StateStore.close)."""
        self.store.close()

    # ------------------------------------------------------------------ #
    def run_judgement(self) -> list[dict]:
        decisions = read_jsonl(self.run / "decisions.jsonl")
        outcomes = read_jsonl(self.run / "outcome_events.jsonl") \
            if (self.run / "outcome_events.jsonl").exists() else []
        outcomes_by_viewer: dict[str, list[dict]] = {}
        for e in outcomes:
            outcomes_by_viewer.setdefault(e["viewer_id"], []).append(e)

        corrections = {c["goal_id"]: c for c in self.pack.control_events
                       if c["type"] == "goal_rule_correction"}
        # observation horizon = the simulator's global end, not the viewer's
        # own last event (no events after X just means they did nothing)
        horizon = max((parse_ts(e["received_at"]) for e in outcomes), default=None)

        assignments: dict[str, dict] = {}
        for d in sorted(decisions, key=lambda d: d["deliver_at"]):
            vid = d["viewer_id"]
            if vid in assignments:
                continue
            if self.pack.arms.get(vid) == "holdout":
                continue  # shadow-planned below (same planner, no action)
            if d.get("goal"):
                assignments[vid] = {
                    "source": "agent_decision", "goal": d["goal"],
                    "started_at": d["deliver_at"], "decision_id": d["decision_id"]}

        assignments.update(self._shadow_assignments(decisions))

        rows: list[dict] = []
        for vid, a in sorted(assignments.items()):
            rows.append(self._judge_one(vid, a, outcomes_by_viewer.get(vid, []),
                                       corrections, horizon))

        write_jsonl(self.run / "outcomes.jsonl", rows)
        return rows

    # ------------------------------------------------------------------ #
    def _shadow_assignments(self, decisions: list[dict]) -> dict:
        """Same planner, holdout viewers, zero actions: fair scoring only."""
        out: dict[str, dict] = {}
        first_trigger: dict[str, dict] = {}
        for d in sorted(decisions, key=lambda d: d["deliver_at"]):
            if self.pack.arms.get(d["viewer_id"]) != "holdout":
                continue
            first_trigger.setdefault(d["viewer_id"], d)
        for vid, d in first_trigger.items():
            trig = next((t for t in self.pack.triggers if t["viewer_id"] == vid), None)
            if trig is None:
                continue
            when = parse_ts(trig["deliver_at"])
            facts = resolve(self.pack, vid, when, extra_events=[_as_trigger_event(trig)])
            choice = self.planner.choose(facts, when, active_goal=None)
            if choice.goal_id:
                out[vid] = {
                    "source": "shadow_plan", "decision_id": d.get("decision_id"),
                    "started_at": trig["deliver_at"],
                    "goal": {"id": choice.goal_id, "deadline": choice.deadline,
                             "success_signal": choice.success_signal,
                             "rule_version": choice.rule_version}}
        return out

    # ------------------------------------------------------------------ #
    def _judge_one(self, viewer_id: str, a: dict, outcomes: list[dict],
                   corrections: dict, horizon: datetime | None) -> dict:
        goal_id = a["goal"]["id"]
        g = self.pack.goals[goal_id]
        start = parse_ts(a["started_at"])
        deadline = parse_ts(a["goal"]["deadline"])
        rule = dict(g["success"])
        rule_version = a["goal"].get("rule_version", self.pack.rule_version)
        corrected = corrections.get(goal_id)
        applied_correction = False
        if corrected and parse_ts(corrected["at"]) <= deadline and parse_ts(corrected["at"]) >= start:
            # ONLY this open goal id is affected; other goals are untouched
            rule = dict(corrected["new_rule"])
            rule_version = rule_version + "+rulefix"
            applied_correction = True

        events = self._window_events(viewer_id, start, deadline, outcomes)
        ok, matched = self._matches(rule, events, start, deadline)
        horizon_ok = horizon is None or horizon >= deadline
        if ok:
            status = "SUCCEEDED"
        elif not horizon_ok:
            status = "INCONCLUSIVE"
        else:
            status = "FAILED"

        row = {
            "viewer_id": viewer_id,
            "arm": self.pack.arms.get(viewer_id, "?"),
            "goal_id": goal_id,
            "source": a["source"],
            "decision_id": a.get("decision_id"),
            "started_at": a["started_at"],
            "deadline": a["goal"]["deadline"],
            "success_signal": a["goal"].get("success_signal"),
            "rule_version": rule_version,
            "rule_correction_applied": applied_correction,
            "status": status,
            "matched_events": [{k: e.get(k) for k in
                                ("event_id", "event_type", "occurred_at", "received_at", "source")}
                               if e.get("event_id") else
                               {"event_type": e.get("event_type"),
                                "occurred_at": e.get("occurred_at"), "source": e.get("source", "simulator")}
                               for e in matched[:5]],
            "events_in_window": len(events),
            "judged_at": z(datetime.now(timezone.utc)),
            "assumptions": (g.get("underspecified") or []),
        }
        # feed the result back into durable viewer state
        viewer = self.store.get_viewer(viewer_id)
        active = viewer.get("active_goal")
        if active and active.get("id") == goal_id:
            active["status"] = "succeeded" if status == "SUCCEEDED" else \
                ("failed" if status == "FAILED" else "inconclusive")
            active["judged_at"] = row["judged_at"]
            viewer["active_goal"] = active
        viewer["learnings"].append({
            "at": row["judged_at"], "type": "goal_judged",
            "detail": f"{goal_id} -> {status} (window {a['started_at']}..{a['goal']['deadline']}, "
                      f"{len(events)} attributable events, rule {rule_version})"})
        self.store.put_viewer(viewer)
        return row

    # ------------------------------------------------------------------ #
    def _window_events(self, viewer_id: str, start: datetime, deadline: datetime,
                       outcomes: list[dict]) -> list[dict]:
        """Simulator outcomes AND real pack events inside the goal window."""
        evs = []
        for e in outcomes:
            if e["viewer_id"] != viewer_id:
                continue
            occ = parse_ts(e["occurred_at"])
            if start <= occ <= deadline:
                evs.append(e)
        for e in self.pack.events_by_viewer.get(viewer_id, []):
            if e["event_type"] not in ("episode_play", "episode_complete", "purchase",
                                       "app_open", "consent_updated", "search"):
                continue
            occ = e["_occurred"]
            rec = e["_received"]
            if start <= occ <= deadline and rec <= deadline + timedelta(days=1):
                evs.append({**e, "source": "pack"})
        evs.sort(key=lambda e: e.get("occurred_at", ""))
        return evs

    def _matches(self, rule: dict, events: list[dict], start, deadline) -> tuple[bool, list]:
        want = rule["event"]
        min_count = rule.get("min_count", 1)
        match = rule.get("match", {}) or {}
        matched = []
        for e in events:
            if e["event_type"] != want:
                continue
            props = e.get("props", {}) or {}
            if match:
                okk = True
                for k, allowed in match.items():
                    if isinstance(allowed, list):
                        if props.get(k) not in allowed:
                            okk = False
                            break
                    elif props.get(k) != allowed:
                        okk = False
                        break
                if not okk:
                    continue
            matched.append(e)
        if rule.get("distinct_days"):
            if rule.get("requires_play_same_day"):
                open_days = {ist(parse_ts(e["occurred_at"])).date().isoformat()
                             for e in matched if e["event_type"] == "app_open"}
                play_days = {ist(parse_ts(e["occurred_at"])).date().isoformat()
                             for e in events if e["event_type"] == "episode_play"}
                n = len(open_days & play_days)
            else:
                n = len({ist(parse_ts(e["occurred_at"])).date().isoformat() for e in matched})
            return n >= rule["distinct_days"], matched
        return len(matched) >= min_count, matched
