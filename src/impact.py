"""
impact.py - prove impact the right way (and re-analyse the old pilot).

Primary analysis: intention-to-treat over replay-day visitors (everyone who
got a trigger, randomized into agent or holdout BEFORE any action), success
= the viewer's goal succeeded, including viewers the agent chose not to act
on. Non-visiting arm members are reported as a secondary, fully-randomized
sensitivity analysis.

Also reported:
  * per-goal breakdown with sample sizes;
  * 95% confidence intervals on the difference (Wald) plus a note that a
    small n means "no detectable effect yet", not "no effect";
  * the NAIVE served-vs-everyone-else comparison next to ITT on our own run
    (why it flatters us: the agent serves the already-engaged);
  * the pilot report's biased headline recomputed honestly from randomized
    arms;
  * latency report summarised (from harness).

Writes impact_report.md and impact_summary.json next to latency_report.json.
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

from .pack import Pack
from .utils import read_jsonl


def _rate(k: int, n: int) -> float:
    return k / n if n else 0.0


def _ci_diff(k1: int, n1: int, k2: int, n2: int) -> tuple[float, float, float]:
    """Returns (difference, low, high) for p1 - p2 with a Wald 95% CI."""
    if not n1 or not n2:
        return 0.0, 0.0, 0.0
    p1, p2 = k1 / n1, k2 / n2
    d = p1 - p2
    se = math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
    return d, d - 1.96 * se, d + 1.96 * se


def analyze(pack: Pack, run_dir: str | Path, pilot_dir: str | Path | None = None) -> dict:
    run = Path(run_dir)
    outcomes = read_jsonl(run / "outcomes.jsonl")
    decisions = read_jsonl(run / "decisions.jsonl")
    actions = read_jsonl(run / "actions.jsonl")
    arms = pack.arms
    triggered = {t["viewer_id"] for t in pack.triggers}
    triggered = {v for v in triggered if v in arms}

    by_viewer = {o["viewer_id"]: o for o in outcomes}
    served = {a["viewer_id"] for a in actions if a.get("status") in ("PUBLISHED", "SCHEDULED")
              and a.get("action") in ("PUSH", "WHATSAPP", "IN_APP", "HOME_PAGE")}

    # ---------------- intention-to-treat over triggered viewers ------------ #
    agent_pop = [v for v in triggered if arms[v] == "agent"]
    hold_pop = [v for v in triggered if arms[v] == "holdout"]

    def successes(pop):
        return sum(1 for v in pop if by_viewer.get(v, {}).get("status") == "SUCCEEDED")

    a_k, a_n = successes(agent_pop), len(agent_pop)
    h_k, h_n = successes(hold_pop), len(hold_pop)
    itt_d, itt_lo, itt_hi = _ci_diff(a_k, a_n, h_k, h_n)

    # ---------------- secondary: every randomized viewer ------------------- #
    all_agent = [v for v, a in arms.items() if a == "agent"]
    all_hold = [v for v, a in arms.items() if a == "holdout"]
    aa_k, aa_n = successes(all_agent), len(all_agent)
    ah_k, ah_n = successes(all_hold), len(all_hold)
    s_d, s_lo, s_hi = _ci_diff(aa_k, aa_n, ah_k, ah_n)

    # ---------------- naive served-vs-unserved (biased, for contrast) ------ #
    naive_served = [v for v in triggered if v in served]
    naive_rest = [v for v in triggered if v not in served]
    ns_k, ns_n = successes(naive_served), len(naive_served)
    nr_k, nr_n = successes(naive_rest), len(naive_rest)
    n_d, n_lo, n_hi = _ci_diff(ns_k, ns_n, nr_k, nr_n)

    # ---------------- per goal --------------------------------------------- #
    per_goal = {}
    for o in outcomes:
        g = per_goal.setdefault(o["goal_id"], {"agent": [0, 0], "holdout": [0, 0]})
        arm = "agent" if o["arm"] == "agent" else "holdout"
        g[arm][1] += 1
        if o["status"] == "SUCCEEDED":
            g[arm][0] += 1
    goal_rows = []
    for gid, g in sorted(per_goal.items()):
        d, lo, hi = _ci_diff(g["agent"][0], g["agent"][1], g["holdout"][0], g["holdout"][1])
        goal_rows.append({
            "goal_id": gid, "agent_k": g["agent"][0], "agent_n": g["agent"][1],
            "hold_k": g["holdout"][0], "hold_n": g["holdout"][1],
            "diff": d, "lo": lo, "hi": hi})

    # ---------------- pilot re-analysis ------------------------------------ #
    pilot = _pilot_reanalysis(pack, pilot_dir)

    # ---------------- decisions / safety counters -------------------------- #
    action_counts: dict[str, int] = {}
    for a in actions:
        action_counts[a["action"]] = action_counts.get(a["action"], 0) + 1
    holdout_action_leaks = [a["action_id"] for a in actions
                            if arms.get(a["viewer_id"]) == "holdout"
                            and a.get("status") in ("PUBLISHED", "SCHEDULED")]
    no_action_reasons: dict[str, int] = {}
    for d in decisions:
        if d["action"] == "NONE":
            r = str(d.get("no_action_reason") or d.get("reason") or "?").split(":")[0]
            no_action_reasons[r] = no_action_reasons.get(r, 0) + 1

    summary = {
        "itt_triggered": {"agent": {"k": a_k, "n": a_n, "rate": _rate(a_k, a_n)},
                          "holdout": {"k": h_k, "n": h_n, "rate": _rate(h_k, h_n)},
                          "diff": itt_d, "ci95": [itt_lo, itt_hi]},
        "sensitivity_all_randomized": {"agent": {"k": aa_k, "n": aa_n, "rate": _rate(aa_k, aa_n)},
                                       "holdout": {"k": ah_k, "n": ah_n, "rate": _rate(ah_k, ah_n)},
                                       "diff": s_d, "ci95": [s_lo, s_hi]},
        "naive_served_vs_rest": {"served": {"k": ns_k, "n": ns_n, "rate": _rate(ns_k, ns_n)},
                                 "rest": {"k": nr_k, "n": nr_n, "rate": _rate(nr_k, nr_n)},
                                 "diff": n_d, "ci95": [n_lo, n_hi]},
        "per_goal": goal_rows,
        "pilot": pilot,
        "actions": action_counts,
        "no_action_reasons": no_action_reasons,
        "holdout_action_leaks": holdout_action_leaks,
        "decisions": len(decisions),
        "served_viewers": len(served),
    }
    (run / "impact_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (run / "impact_report.md").write_text(_render(summary), encoding="utf-8")
    from .report import write_final_report
    write_final_report(pack, run, summary, decisions, actions, outcomes)
    return summary


def _pilot_reanalysis(pack: Pack, pilot_dir) -> dict:
    pilot_dir = Path(pilot_dir) if pilot_dir else pack.root / "pilot"
    with open(pilot_dir / "pilot_outcomes.csv", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    served = [r for r in rows if r["served"] == "true"]
    rest = [r for r in rows if r["served"] == "false"]
    s_k = sum(r["success"] == "true" for r in served)
    r_k = sum(r["success"] == "true" for r in rest)
    naive_d, naive_lo, naive_hi = _ci_diff(s_k, len(served), r_k, len(rest))
    agent = [r for r in rows if r["arm"] == "agent"]
    hold = [r for r in rows if r["arm"] == "holdout"]
    a_k = sum(r["success"] == "true" for r in agent)
    h_k = sum(r["success"] == "true" for r in hold)
    itt_d, itt_lo, itt_hi = _ci_diff(a_k, len(agent), h_k, len(hold))
    return {
        "headline_claim": "served vs everyone else (the report's number)",
        "naive_diff": naive_d, "naive_ci95": [naive_lo, naive_hi],
        "naive_n": [len(served), len(rest)],
        "itt_diff": itt_d, "itt_ci95": [itt_lo, itt_hi],
        "itt_n": [len(agent), len(hold)],
        "report_path": "pilot/pilot_report.md",
    }


def _fmt_pp(x: float) -> str:
    return f"{x * 100:+.2f} pp"


def _render(s: dict) -> str:
    itt = s["itt_triggered"]
    sen = s["sensitivity_all_randomized"]
    naive = s["naive_served_vs_rest"]
    pilot = s["pilot"]
    lines = []
    A = lines.append
    A("# Impact report: agent vs holdout (honest measurement)\n")
    A("## 1. Primary: intention-to-treat over replay-day visitors\n")
    A("Everyone below was randomized into an arm *before* the agent touched "
      "anything. Viewers the agent decided not to act on stay in the agent "
      "arm (that is the point of ITT).\n")
    A("| arm | succeeded | n | rate |")
    A("|---|---|---|---|")
    A(f"| agent | {itt['agent']['k']} | {itt['agent']['n']} | {itt['agent']['rate'] * 100:.2f}% |")
    A(f"| holdout | {itt['holdout']['k']} | {itt['holdout']['n']} | {itt['holdout']['rate'] * 100:.2f}% |")
    A("")
    A(f"**Difference: {_fmt_pp(itt['diff'])} (95% CI [{_fmt_pp(itt['ci95'][0])}, {_fmt_pp(itt['ci95'][1])}]).**\n")
    lo, hi = itt["ci95"]
    if lo <= 0 <= hi:
        A("The interval crosses zero: with this sample size we **cannot claim a "
          "detectable effect yet**. Reporting that honestly is the point - a "
          "wide interval is not evidence of impact, and we will not pretend "
          "otherwise.\n")
    else:
        A("The interval excludes zero at the 95% level for this sample.\n")

    A("## 2. Secondary: every randomized viewer (visitors and non-visitors)\n")
    A("Non-visitors had no trigger, so the agent could not act; diluting with "
      "them shows the realistic fleet-wide effect.\n")
    A(f"| arm | succeeded | n | rate |")
    A(f"|---|---|---|---|")
    A(f"| agent | {sen['agent']['k']} | {sen['agent']['n']} | {sen['agent']['rate'] * 100:.2f}% |")
    A(f"| holdout | {sen['holdout']['k']} | {sen['holdout']['n']} | {sen['holdout']['rate'] * 100:.2f}% |")
    A("")
    A(f"Difference: {_fmt_pp(sen['diff'])} (95% CI [{_fmt_pp(sen['ci95'][0])}, {_fmt_pp(sen['ci95'][1])}]).\n")

    A("## 3. Why 'served vs everyone else' misleads\n")
    A("| comparison | rate gap | 95% CI |")
    A("|---|---|---|")
    A(f"| naive: served vs not served (our run) | {_fmt_pp(naive['diff'])} | "
      f"[{_fmt_pp(naive['ci95'][0])}, {_fmt_pp(naive['ci95'][1])}] |")
    A(f"| correct: agent arm vs holdout arm (our run) | {_fmt_pp(itt['diff'])} | "
      f"[{_fmt_pp(itt['ci95'][0])}, {_fmt_pp(itt['ci95'][1])}] |")
    A("")
    gap = naive["diff"] - itt["diff"]
    A("The naive gap is not a treatment effect: the agent preferentially serves "
      "viewers it believes can be moved - the already-engaged, the consented, "
      "the reachable - and those viewers would have done better anyway. Here it "
      f"lands {_fmt_pp(gap)} {'above' if gap >= 0 else 'below'} the randomized "
      "estimate, which shows the direction is not even stable. Randomisation is "
      "what breaks the link between targeting and outcome, and only the arm "
      "comparison is causal.\n")

    A("## 4. Pilot report re-analysis\n")
    A(f"The internal pilot report's headline (**{pilot['headline_claim']}: "
      f"{_fmt_pp(pilot['naive_diff'])}**, CI "
      f"[{_fmt_pp(pilot['naive_ci95'][0])}, {_fmt_pp(pilot['naive_ci95'][1])}], "
      f"n={pilot['naive_n'][0]}/{pilot['naive_n'][1]}) is a served-vs-unserved "
      "comparison and is biased upward.\n")
    A(f"Recomputed from the randomized arms (all treatment vs all holdout, "
      f"served or not): **{_fmt_pp(pilot['itt_diff'])}** "
      f"(95% CI [{_fmt_pp(pilot['itt_ci95'][0])}, {_fmt_pp(pilot['itt_ci95'][1])}], "
      f"n={pilot['itt_n'][0]}/{pilot['itt_n'][1]}).\n")

    A("## 5. Per-goal breakdown\n")
    A("| goal | agent | holdout | diff | 95% CI |")
    A("|---|---|---|---|---|")
    for g in s["per_goal"]:
        A(f"| {g['goal_id']} | {g['agent_k']}/{g['agent_n']} | {g['hold_k']}/{g['hold_n']} | "
          f"{_fmt_pp(g['diff'])} | [{_fmt_pp(g['lo'])}, {_fmt_pp(g['hi'])}] |")
    A("")
    A("Per-goal cells are small (one replay day): several intervals look large "
      "only because n is tiny, and none of them is individually conclusive. "
      "They are reported for honesty about where any signal would come from, "
      "not as a result.\n")

    A("## 6. Safety counters\n")
    A(f"- actions by type: {json.dumps(s['actions'])}")
    A(f"- holdout action leaks (must be 0): **{len(s['holdout_action_leaks'])}**")
    A(f"- decisions with recorded no-action reasons: {json.dumps(s['no_action_reasons'])}")
    A("")
    A("## 7. Latency\n")
    A("See `latency_report.json` (p50/p95/max decided_in_ms, budget breaches, "
      "fallbacks used).")
    return "\n".join(lines) + "\n"
