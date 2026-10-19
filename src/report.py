"""
report.py - the run's final report, generated from the run itself.

`sample_run/final_report.md` is written by `python -m src.main run ...` (via
impact.analyze), so every number in it is re-derived from decisions.jsonl,
actions.jsonl, outcomes.jsonl, latency_report.json and simulator_report.json
of that same run. A hand-written summary would drift; this one cannot.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .pack import Pack
from .utils import in_quiet_hours, ist_date, parse_ts


def _pp(x: float) -> str:
    return f"{x * 100:+.2f} pp"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_final_report(pack: Pack, run: Path, summary: dict,
                       decisions: list[dict], actions: list[dict],
                       outcomes: list[dict]) -> Path:
    run = Path(run)
    lat = _load(run / "latency_report.json")
    sim = _load(run / "simulator_report.json")
    itt = summary["itt_triggered"]

    # ---- safety counters straight from the run artefacts ------------------ #
    outbound = [a for a in actions if a.get("action") in ("PUSH", "WHATSAPP")
                and a.get("status") == "PUBLISHED"]
    quiet_sends = [a for a in outbound
                   if a.get("published_at") and in_quiet_hours(parse_ts(a["published_at"]))]
    per_day = Counter((a["viewer_id"], ist_date(parse_ts(a["published_at"])))
                      for a in outbound if a.get("published_at"))
    cap_violations = {f"{k[0]}@{k[1]}": v for k, v in per_day.items() if v > 1}
    idem = Counter(a["idempotency_key"] for a in actions)
    dup_actions = {k: v for k, v in idem.items() if v > 1}
    stale_trigger = [d for d in decisions if d["trigger"]["event_age_s"] > 1800]
    stale_reason_n = sum(1 for d in stale_trigger
                         if str(d.get("no_action_reason") or "").startswith("stale_trigger"))
    ageing_trigger = [d for d in decisions if 300 < d["trigger"]["event_age_s"] <= 1800]
    silent = [d for d in decisions if d["action"] == "NONE"]
    stale_in_pack = _trigger_ages_in_pack(pack, 1800, None)
    ageing_in_pack = _trigger_ages_in_pack(pack, 300, 1800)

    # ---- traps we found in the pack --------------------------------------- #
    dup_profile_viewers = [v for v, rows in pack.profile_duplicates.items() if len(rows) > 1]
    dup_triggers = len(pack.triggers) - len({t["event_id"] for t in pack.triggers})
    withdrawn = [t for t in pack.titles if (t.get("withdrawn_at") or "").strip()]
    unmeasurable = [g["id"] for g in pack.goals.values() if not pack.success_event_known(g)]
    underspecified = [g["id"] for g in pack.goals.values() if g.get("underspecified")]
    control_types = Counter(c["type"] for c in pack.control_events)
    fault_modes = Counter(f.get("mode", "?") for f in pack.model_faults)

    j = Counter(o["status"] for o in outcomes)
    corrections = [o for o in outcomes if o.get("rule_correction_applied")]

    L: list[str] = []
    A = L.append
    A("# Final report - One User, One Agent\n")
    A(f"Data pack: `{Path(pack.root).name}` | run directory: `{run.name}` | "
      f"model provider: mock (deterministic, no API key).\n")
    A("Every number below is generated from this run's own artefacts by "
      "`python -m src.main run --pack data_pack --out sample_run`. "
      "Re-running the command reproduces the bundle; re-running paper never "
      "changes the conclusions.\n")

    A("## 1. Replay at a glance\n")
    A(f"- decisions: **{len(decisions)}** (incl. {len(stale_trigger)} stale "
      f"offline-buffered triggers, {len(ageing_trigger)} ageing triggers, "
      f"{sum(1 for d in decisions if d['status'] == 'SUPPRESSED')} duplicate deliveries suppressed)")
    A(f"- actions: **{len(actions)}** -> {json.dumps(lat.get('action_counts', {}))}")
    A(f"- decisions with no action: {len(silent)} ({_reason_mix(silent)})")
    A(f"- latency: p50 **{lat.get('p50_ms')} ms**, p95 **{lat.get('p95_ms')} ms**, "
      f"max {lat.get('max_ms')} ms; budget breaches: {len(lat.get('budget_breaches', []))}; "
      f"over 60 s: {lat.get('over_60s', 0)}")
    _fb = dict(Counter(lat.get("fallbacks", [])))
    A(f"- model faults handled with a rule fallback: "
      f"{json.dumps(_fb) if _fb else '0 (no provider fault hit a model call in this run)'}\n")

    A("## 2. Data traps detected and how they were handled\n")
    A("| trap | found in pack | handled by |")
    A("|---|---|---|")
    A(f"| duplicate events (same event_id) | {pack.duplicate_event_ids} | first receipt wins, "
      "duplicates dropped at load |")
    A(f"| duplicate trigger deliveries | {dup_triggers} | idempotency key -> one action |")
    A(f"| offline-buffered late events (>6 h) | {pack.late_events} | received_at is the clock; "
      f"stale triggers produce no action at all |")
    A(f"| stale triggers (>30 min old) | {stale_in_pack} triggers | no live action; the "
      f"{stale_reason_n} non-holdout ones record a stale_trigger reason |")
    A(f"| ageing triggers (5-30 min) | {ageing_in_pack} triggers | in-session surface only, "
      f"never a push ({len(ageing_trigger)} decided in this run) |")
    A(f"| duplicate profile rows | {len(dup_profile_viewers)} viewers | freshest `row_updated_at` wins, "
      "conflict recorded |")
    A(f"| profile snapshot vs purchase event | day-stale snapshot | a purchase received after the "
      "snapshot overrides it |")
    A(f"| dialect conflicts (app/campaign/profile) | detected per decision | app setting wins, every "
      "disagreement kept on the decision |")
    A(f"| instructions hidden in catalogue descriptions | {len(pack.injected)} titles | content is "
      "not command; never copied into copy or actions |")
    A(f"| withdrawn titles | {len(withdrawn)} | excluded by planner AND verifier |")
    A(f"| unmeasurable goal | {', '.join(unmeasurable) or 'none'} | refused: success event does not "
      "exist in the stream |")
    A(f"| underspecified goals | {', '.join(underspecified) or 'none'} | interpreted, and the "
      "interpretation is an assumption on every decision |")
    A(f"| control events | {json.dumps(dict(control_types))} | applied at their timestamp, response "
      "logged in control_log.jsonl |")
    A(f"| injected model faults | {json.dumps(dict(fault_modes))} | retry once, then rule fallback; "
      "unsupported model claims rejected by the verifier |\n")

    A("## 3. Rules that had to hold (machine-checked)\n")
    A("| rule | result |")
    A("|---|---|")
    A(f"| holdout viewers receive zero actions | **{len(summary['holdout_action_leaks'])} leaks** "
      "(must be 0) |")
    A(f"| one action per trigger event_id | **{len(dup_actions)} duplicates** (must be 0) |")
    A(f"| no outbound message sent inside quiet hours | **{len(quiet_sends)}** (must be 0); "
      "quiet-hour plans are scheduled for 08:00 IST instead |")
    A(f"| at most one outbound message per viewer per IST day | **{len(cap_violations)} violations** "
      "(must be 0) |")
    A(f"| every decision records a goal or a no-action reason | "
      f"**{sum(1 for d in decisions if not (d.get('goal') or d.get('no_action_reason') or d.get('reason')))}** "
      "missing (must be 0) |")
    A(f"| simulator saw no agent action on a holdout viewer | "
      f"**{len(sim.get('holdout_violations', []))}** (must be 0) |\n")
    A("Run `python -m src.main audit --pack data_pack --out sample_run` for the "
      "standalone version of this table.\n")

    A("## 4. Goal judgement (the loop closed)\n")
    A(f"- goals judged: **{len(outcomes)}** -> {j.get('SUCCEEDED', 0)} succeeded / "
      f"{j.get('FAILED', 0)} failed / {j.get('INCONCLUSIVE', 0)} inconclusive")
    A(f"- goals re-judged under a corrected rule: **{len(corrections)}** "
      "(only open goals of the corrected goal id; closed goals stay closed)")
    A("- an event only counts inside `[goal_start, deadline]`; late arrivals "
      "cannot borrow success from outside the window")
    A("- holdout viewers are scored by the SAME planner in shadow mode, so both "
      "arms are judged on identical criteria\n")

    A("## 5. Impact (honest measurement)\n")
    A("| population | agent | holdout | difference | 95% CI |")
    A("|---|---|---|---|---|")
    A(f"| primary: replay-day visitors (ITT) | {itt['agent']['k']}/{itt['agent']['n']} "
      f"({itt['agent']['rate'] * 100:.2f}%) | {itt['holdout']['k']}/{itt['holdout']['n']} "
      f"({itt['holdout']['rate'] * 100:.2f}%) | **{_pp(itt['diff'])}** | "
      f"[{_pp(itt['ci95'][0])}, {_pp(itt['ci95'][1])}] |")
    sen = summary["sensitivity_all_randomized"]
    A(f"| secondary: every randomized viewer | {sen['agent']['k']}/{sen['agent']['n']} | "
      f"{sen['holdout']['k']}/{sen['holdout']['n']} | {_pp(sen['diff'])} | "
      f"[{_pp(sen['ci95'][0])}, {_pp(sen['ci95'][1])}] |")
    naive = summary["naive_served_vs_rest"]
    A(f"| naive (biased, for contrast): served vs rest | {naive['served']['k']}/{naive['served']['n']} | "
      f"{naive['rest']['k']}/{naive['rest']['n']} | {_pp(naive['diff'])} | "
      f"[{_pp(naive['ci95'][0])}, {_pp(naive['ci95'][1])}] |")
    A("")
    lo, hi = itt["ci95"]
    gap = naive["diff"] - itt["diff"]
    pilot = summary["pilot"]
    pilot_gap = pilot["naive_diff"] - pilot["itt_diff"]
    if lo <= 0 <= hi:
        A("**Conclusion: no detectable effect yet.** The confidence interval on "
          "the arm difference crosses zero, so the correct statement is "
          "\"we cannot show the agent moved the goal here\", not \"the agent "
          "works\".\n")
    else:
        A("**Conclusion:** the arm difference excludes zero at 95% for this "
          "sample - still one day of data, so this is evidence, not proof.\n")
    A(f"Naive vs honest on our own run: the served-vs-rest gap is {_pp(naive['diff'])} "
      f"against the arm difference of {_pp(itt['diff'])} (gap {_pp(gap)}). "
      f"The pilot report shows how large that gap can get from the same arithmetic: "
      f"its headline {_pp(pilot['naive_diff'])} against an honest recomputation of "
      f"{_pp(pilot['itt_diff'])} (gap {_pp(pilot_gap)}, 95% CI "
      f"[{_pp(pilot['itt_ci95'][0])}, {_pp(pilot['itt_ci95'][1])}]). The naive number "
      "moves with who the agent chooses to serve, so it is descriptive, not causal - on "
      "either run, only the arm comparison answers \"did the agent move the goal\". "
      "That is why the assignment's \"prove impact\" step is answered with an "
      "experiment rather than a dashboard.\n")
    A(f"Pilot report detail: headline ({_pp(pilot['naive_diff'])}, "
      f"{pilot['headline_claim']}) vs randomized recomputation "
      f"{_pp(pilot['itt_diff'])} (95% CI "
      f"[{_pp(pilot['itt_ci95'][0])}, {_pp(pilot['itt_ci95'][1])}]), from "
      f"n={pilot['itt_n'][0]}/{pilot['itt_n'][1]} randomized viewers.\n")

    A("## 6. Where to look next\n")
    A("- `decisions.jsonl` - one line per trigger: facts used, goal, dialects "
      "and conflicts, action, evidence, checks, latency")
    A("- `actions.jsonl` - what was actually published or scheduled")
    A("- `outcomes.jsonl` - the judgement for every goal window")
    A("- `control_log.jsonl` - how each trap/control event was handled live")
    A("- `viewer_state/viewers.jsonl` - the durable per-viewer state at the end")
    A("- `impact_report.md` - full impact analysis with per-goal breakdown")
    A("- `latency_report.json` - measured latency distribution and budgets")
    A("- `KNOWN_LIMITATIONS.md` - what is synthetic, what is weak, what we would "
      "fix with the real pack\n")

    path = run / "final_report.md"
    path.write_text("\n".join(L), encoding="utf-8")
    return path


def _trigger_ages_in_pack(pack: Pack, low_s: int, high_s: int | None) -> int:
    """Count pack triggers whose occurred_at predates receipt by a given age."""
    n = 0
    for t in pack.triggers:
        age = (parse_ts(t["received_at"]) - parse_ts(t["occurred_at"])).total_seconds()
        if age > low_s and (high_s is None or age <= high_s):
            n += 1
    return n


def _reason_mix(silent: list[dict]) -> str:
    c = Counter(str(d.get("no_action_reason") or d.get("reason") or "?").split(":")[0]
                for d in silent)
    return ", ".join(f"{k}={v}" for k, v in c.most_common())
