"""
harness.py - replay harness.

Feeds the recorded day back to the agents in time order:
  * triggers in deliver_at order (duplicate deliveries included, so
    idempotency actually gets exercised)
  * live events at their arrival time (purchase 2 minutes after a decision,
    opt-outs, ...) -> state refresh + immediate re-plan / pending cancellation
  * control events at their timestamp (title withdrawal, goal-rule
    correction, traffic surge, datastore slowness) -> system response logged
  * model faults come from replay/model_faults.jsonl via the provider

Latency is measured for real (perf_counter) and published_at =
deliver_at + measured processing time. The harness writes decisions.jsonl,
actions.jsonl, control_log.jsonl, latency_report.json and the durable
viewer_state/ store.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

from .agent import Agent, Ctx
from .facts import resolve
from .pack import Pack
from .provider import get_provider
from .state import StateStore
from .utils import parse_ts, write_jsonl, z


def _as_event(obj: dict, kind: str) -> dict:
    e = dict(obj)
    if kind == "trigger":
        e.setdefault("props", {})
    e["_received"] = parse_ts(e["received_at"])
    e["_occurred"] = parse_ts(e["occurred_at"])
    return e


#: artefacts a replay (and the steps that follow it) own inside the run dir.
#: A replay must start from a clean slate: the idempotency ledger lives in
#: viewer_state/state.db, so re-running into an old directory would see every
#: trigger as "already decided" and silently produce zero actions.
_RUN_ARTEFACTS = (
    "decisions.jsonl", "actions.jsonl", "control_log.jsonl", "latency_report.json",
    "outcomes.jsonl", "simulator_report.json", "impact_report.md",
    "impact_summary.json", "final_report.md",
)


def reset_run_dir(out: str | Path) -> None:
    """Remove this run's own generated artefacts so `run` is re-runnable."""
    out = Path(out)
    for name in _RUN_ARTEFACTS:
        p = out / name
        try:
            if p.is_file():
                p.unlink()
        except OSError:
            pass
    state = out / "viewer_state"
    if state.exists():
        import shutil
        try:
            shutil.rmtree(state)
        except OSError:
            pass


class ReplayHarness:
    def __init__(self, pack: Pack, out_dir: str | Path, provider_name: str = "mock"):
        self.pack = pack
        self.out = Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        reset_run_dir(self.out)
        self.store = StateStore(self.out / "viewer_state" / "state.db")
        self.ctx = Ctx()
        self.provider = get_provider(pack, provider_name)
        self.agent = Agent(pack, self.store, self.provider, self.ctx)
        self.provider.timeout_delay_s = 1.0  # realistic fault latency in replay mode

        self.decisions: list[dict] = []
        self.control_log: list[dict] = []
        self.live_buffer: dict[str, list[dict]] = {}
        self.corrections: list[dict] = []
        self._surge_until: datetime | None = None
        self._slow_until: datetime | None = None

    # ------------------------------------------------------------------ #
    def run(self) -> dict:
        queue: list[tuple[datetime, int, str, dict]] = []
        for t in self.pack.triggers:
            queue.append((parse_ts(t["deliver_at"]), 1, "trigger", t))
        for ev in self.pack.live_events:
            queue.append((parse_ts(ev["arrive_at"]), 0, "live", ev))
        for c in self.pack.control_events:
            queue.append((parse_ts(c["at"]), 2, "control", c))
        queue.sort(key=lambda x: (x[0], x[1]))

        for when, _, kind, obj in queue:
            if kind == "control":
                self._apply_control(when, obj)
            elif kind == "live":
                self._apply_live(when, obj)
            else:
                self._apply_trigger(when, obj)
        self.store.flush_cache()
        return self.finalize()

    # ------------------------------------------------------------------ #
    def _sync_flags(self, now: datetime) -> None:
        surge = self._surge_until is not None and now < self._surge_until
        slow = self._slow_until is not None and now < self._slow_until
        if self.ctx.surge != surge:
            self.ctx.surge = surge
        if self.store.slow_mode != slow:
            self.store.slow_mode = slow
            self.store.slow_latency_s = 0.4 if slow else 0.0

    def _apply_control(self, when: datetime, c: dict) -> None:
        ctype = c["type"]
        entry = {"at": c["at"], "type": ctype, "note": c.get("note", ""), "response": []}
        if ctype == "title_withdrawal":
            ids = set(c.get("title_ids", []))
            for tid in ids:
                if tid in self.pack.by_title_id:
                    self.pack.by_title_id[tid]["withdrawn_at"] = c["at"]
            cancelled = self.store.cancel_pending_for_titles(ids, c["at"],
                                                             "title withdrawn during replay")
            entry["response"] = [
                f"marked {len(ids)} titles withdrawn from {c['at']}",
                f"scanned pending actions; cancelled {len(cancelled)} containing them",
                f"affected_action_ids={[a['action_id'] for a in cancelled]}",
            ]
        elif ctype == "goal_rule_correction":
            gid = c["goal_id"]
            if gid in self.pack.goals:
                self.pack.goals[gid]["success"] = c["new_rule"]
                self.pack.goals[gid]["corrected_at"] = c["at"]
            self.pack.rule_version = self.pack.rule_version + "+rulefix"
            self.corrections.append(c)
            affected = self._mark_affected_open_goals(gid, c)
            entry["response"] = [
                f"rule for {gid} updated at {c['at']}",
                f"open goals affected and queued for re-judgement: {affected}",
                "all other viewers untouched (no full reprocessing)",
            ]
        elif ctype == "traffic_surge":
            self._surge_until = parse_ts(c["until"])
            entry["response"] = [f"surge mode until {c['until']}: optional model calls skipped, "
                                 "deterministic rule path only, latency budget enforced"]
        elif ctype == "datastore_slow":
            self._slow_until = parse_ts(c["until"])
            entry["response"] = [f"slow store until {c['until']}: writes lag 400ms, "
                                 "in-memory snapshot used as fallback, recorded per decision"]
        self.control_log.append(entry)

    def _mark_affected_open_goals(self, goal_id: str, correction: dict) -> int:
        n = 0
        for row in self.store.conn.execute(
                "SELECT viewer_id, active_goal_json FROM viewers WHERE active_goal_json IS NOT NULL").fetchall():
            import json as _json
            g = _json.loads(row[1]) if row[1] else None
            if g and g.get("id") == goal_id and g.get("status") == "open":
                g["rule_corrected_at"] = correction["at"]
                g["rule_version"] = (g.get("rule_version", "") + "+rulefix")
                v = self.store.get_viewer(row[0])
                v["active_goal"] = g
                v["learnings"].append({"at": correction["at"], "type": "goal_rule_correction",
                                       "detail": f"rule for {goal_id} corrected; this open goal "
                                                 "will be re-judged under the new rule only"})
                self.store.put_viewer(v)
                n += 1
        return n

    # ------------------------------------------------------------------ #
    def _apply_live(self, when: datetime, obj: dict) -> None:
        ev = _as_event(obj["event"], "live")
        vid = ev["viewer_id"]
        self.live_buffer.setdefault(vid, []).append(ev)
        viewer = self.store.get_viewer(vid)
        facts = resolve(self.pack, vid, when, extra_events=self.live_buffer.get(vid, []))
        viewer["facts"] = facts.to_dict()
        viewer["updated_at"] = z(when)
        note = {"at": obj["arrive_at"], "viewer_id": vid, "event": ev["event_type"]}
        active = viewer.get("active_goal")
        if active and active.get("status") == "open":
            goal = self.pack.goals.get(active["id"])
            if goal and ev["event_type"] == goal["success"]["event"]:
                active["status"] = "success_observed"
                active["observed_at"] = obj["arrive_at"]
                viewer["active_goal"] = active
                cancelled = self.store.cancel_pending_for_goal(vid, obj["arrive_at"],
                                                               f"{ev['event_type']} already achieved goal {active['id']}")
                note["response"] = [
                    f"goal {active['id']} success signal arrived; marked success_observed",
                    f"cancelled {len(cancelled)} pending message(s) that no longer made sense",
                    "planning for this viewer restarts from the refreshed state",
                ]
                viewer["learnings"].append({"at": obj["arrive_at"], "type": "early_success",
                                            "detail": f"{active['id']} achieved by {ev['event_id']}"})
            elif ev["event_type"] == "opt_out":
                note["response"] = [f"consent override: {ev['props'].get('channel')} opt-out "
                                    "now beats the profile snapshot"]
            else:
                note["response"] = ["state refreshed; no open goal affected"]
        else:
            note["response"] = ["state refreshed (no open goal)"]
        self.store.put_viewer(viewer)
        self.control_log.append(note)

    # ------------------------------------------------------------------ #
    def _apply_trigger(self, when: datetime, trig: dict) -> None:
        self._sync_flags(when)
        extra = [_as_event(trig, "trigger")]
        extra += self.live_buffer.get(trig["viewer_id"], [])
        t = dict(trig)
        t["_deliver"] = parse_ts(trig["deliver_at"])
        started = time.perf_counter()
        d = self.agent.decide(t, now=parse_ts(trig["deliver_at"]), extra_events=extra)
        d["wall_ms"] = int((time.perf_counter() - started) * 1000)
        d["surge"] = self.ctx.surge
        d["datastore_slow"] = self.store.slow_mode
        if d["status"] in ("PUBLISHED", "BLOCKED") and d["action"] != "NONE":
            pub = parse_ts(trig["deliver_at"]) + timedelta(milliseconds=d["decided_in_ms"])
            d["published_at"] = z(pub)
        else:
            d["published_at"] = z(parse_ts(trig["deliver_at"]) + timedelta(milliseconds=d["decided_in_ms"]))
        self.decisions.append(d)

    # ------------------------------------------------------------------ #
    def finalize(self) -> dict:
        # align action timestamps with measured processing time
        by_decision = {d["decision_id"]: d for d in self.decisions}
        actions = self.store.all_actions()
        for a in actions:
            d = by_decision.get(a.get("decision_id"))
            if d and a.get("status") == "PUBLISHED":
                a["published_at"] = d["published_at"]
                self.store.add_action(a)

        write_jsonl(self.out / "decisions.jsonl", self.decisions)
        write_jsonl(self.out / "actions.jsonl", actions)
        write_jsonl(self.out / "control_log.jsonl", self.control_log)

        # durable, auditable per-viewer state export
        rows = [self.store.get_viewer(r[0]) for r in
                self.store.conn.execute("SELECT viewer_id FROM viewers ORDER BY viewer_id")]
        write_jsonl(self.out / "viewer_state" / "viewers.jsonl", rows)

        lat = self._latency_report()
        (self.out / "latency_report.json").write_text(
            __import__("json").dumps(lat, indent=2), encoding="utf-8")
        return lat

    def _latency_report(self) -> dict:
        ms = sorted(d["decided_in_ms"] for d in self.decisions)

        def pct(p: int) -> int:
            if not ms:
                return 0
            i = min(len(ms) - 1, int(round(p / 100.0 * (len(ms) - 1))))
            return ms[i]

        over = [d["decision_id"] for d in self.decisions if d["decided_in_ms"] > 60_000]
        hard = [d["decision_id"] for d in self.decisions if d["decided_in_ms"] > 300_000]
        budget_breaches = [d["decision_id"] for d in self.decisions
                           if d.get("model_calls", 0) > 8 or d.get("tool_calls", 0) > 20]
        statuses: dict[str, int] = {}
        actions: dict[str, int] = {}
        for d in self.decisions:
            statuses[d["status"]] = statuses.get(d["status"], 0) + 1
            actions[d["action"]] = actions.get(d["action"], 0) + 1
        return {
            "decisions": len(self.decisions),
            "p50_ms": pct(50), "p90_ms": pct(90), "p95_ms": pct(95), "p99_ms": pct(99),
            "max_ms": ms[-1] if ms else 0,
            "over_60s": len(over), "over_5min": len(hard),
            "over_60s_ids": over[:20], "over_5min_ids": hard[:20],
            "budget_breaches": budget_breaches,
            "status_counts": statuses,
            "action_counts": actions,
            "latency_budget_s": 60,
            "model_budget_per_decision": 8,
            "tool_budget_per_decision": 20,
            "fallbacks": [d.get("fallback_used") for d in self.decisions if d.get("fallback_used")],
            "stale_suppressed": [d["decision_id"] for d in self.decisions
                                 if str(d.get("no_action_reason", "")).startswith("stale")],
        }
