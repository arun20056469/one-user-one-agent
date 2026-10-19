"""
main.py - CLI for the One User, One Agent build.

One command (no API key, mock model by default):

    python -m src.main run --pack data_pack --out sample_run

which is equivalent to replay -> simulate -> judge -> impact. Individual
stages are also available (useful for evaluators rerunning a stage):

    python -m src.main replay  --pack data_pack --out sample_run
    python -m src.main simulate --pack data_pack --out sample_run --seed 7
    python -m src.main judge    --pack data_pack --out sample_run
    python -m src.main impact   --pack data_pack --out sample_run
    python -m src.main audit    --pack data_pack --out sample_run
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .harness import ReplayHarness
from .impact import analyze
from .judge import OutcomeJudge
from .pack import Pack


def cmd_replay(args) -> dict:
    pack = Pack(args.pack)
    harness = ReplayHarness(pack, args.out, provider_name=args.provider)
    lat = harness.run()
    print(f"[replay] {lat['decisions']} decisions | p50={lat['p50_ms']}ms "
          f"p95={lat['p95_ms']}ms max={lat['max_ms']}ms | over60s={lat['over_60s']} "
          f"| breaches={len(lat['budget_breaches'])}")
    print(f"[replay] actions={lat['action_counts']} statuses={lat['status_counts']}")
    harness.store.close()   # release the SQLite handle (Windows file locks)
    return lat


def cmd_simulate(args) -> None:
    pack_root = Path(args.pack)
    sim = pack_root / "simulator" / "simulate_outcomes.py"
    if not sim.exists():
        sys.exit(f"simulator not found at {sim}")
    cmd = [sys.executable, str(sim), "--pack", str(pack_root),
           "--actions", str(Path(args.out) / "actions.jsonl"),
           "--out", str(args.out), "--seed", str(args.seed)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        sys.exit("simulator failed")
    print("[simulate] " + " ".join(proc.stdout.split())[:400])


def cmd_judge(args) -> list:
    pack = Pack(args.pack)
    judge = OutcomeJudge(pack, args.out)
    try:
        rows = judge.run_judgement()
    finally:
        judge.close()
    s = sum(r["status"] == "SUCCEEDED" for r in rows)
    f = sum(r["status"] == "FAILED" for r in rows)
    i = sum(r["status"] == "INCONCLUSIVE" for r in rows)
    corr = sum(r["rule_correction_applied"] for r in rows)
    print(f"[judge] {len(rows)} goals judged: {s} succeeded / {f} failed / "
          f"{i} inconclusive | rule corrections applied to {corr} open goals")
    return rows


def cmd_impact(args) -> dict:
    pack = Pack(args.pack)
    s = analyze(pack, args.out)
    itt = s["itt_triggered"]
    print(f"[impact] ITT (triggered): agent {itt['agent']['rate'] * 100:.2f}% "
          f"(n={itt['agent']['n']}) vs holdout {itt['holdout']['rate'] * 100:.2f}% "
          f"(n={itt['holdout']['n']}) -> {itt['diff'] * 100:+.2f} pp "
          f"CI [{itt['ci95'][0] * 100:+.2f}, {itt['ci95'][1] * 100:+.2f}]")
    print(f"[impact] holdout action leaks: {len(s['holdout_action_leaks'])} (must be 0)")
    print("[impact] reports: impact_report.md, impact_summary.json")
    return s


def cmd_audit(args) -> None:
    """Quick safety audit of a completed run (used in README final checks)."""
    pack = Pack(args.pack)
    out = Path(args.out)
    from .utils import read_jsonl
    decisions = read_jsonl(out / "decisions.jsonl")
    actions = read_jsonl(out / "actions.jsonl")
    leaks = [a for a in actions if pack.arms.get(a["viewer_id"]) == "holdout"
             and a.get("status") in ("PUBLISHED", "SCHEDULED")]
    dup_actions: dict[str, int] = {}
    for a in actions:
        dup_actions[a["idempotency_key"]] = dup_actions.get(a["idempotency_key"], 0) + 1
    dupes = {k: v for k, v in dup_actions.items() if v > 1}
    missing = [d["decision_id"] for d in decisions
               if not (d.get("goal") or d.get("no_action_reason") or d.get("reason"))]
    print(json.dumps({
        "decisions": len(decisions),
        "actions": len(actions),
        "holdout_leaks": len(leaks),
        "duplicate_actions_from_same_trigger": len(dupes),
        "decisions_without_goal_or_reason": len(missing),
        "injected_catalogue_instructions_found": len(pack.injected),
        "injection_flagged_and_ignored": all(
            d.get("action") != "INJECTION" for d in decisions),
    }, indent=2))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="src.main", description="One User, One Agent")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(p):
        p.add_argument("--pack", required=True, help="path to the data pack")
        p.add_argument("--out", required=True, help="run output directory")
        p.add_argument("--provider", default="mock", help="model provider (mock = default, no key)")

    p_replay = sub.add_parser("replay", help="run the timed replay")
    add(p_replay)
    p_sim = sub.add_parser("simulate", help="run the 14-day outcome simulator")
    add(p_sim)
    p_sim.add_argument("--seed", type=int, default=7)
    p_judge = sub.add_parser("judge", help="judge every goal window")
    add(p_judge)
    p_imp = sub.add_parser("impact", help="impact + reports")
    add(p_imp)
    p_audit = sub.add_parser("audit", help="safety audit of a run")
    add(p_audit)
    p_run = sub.add_parser("run", help="replay + simulate + judge + impact (ONE command)")
    add(p_run)
    p_run.add_argument("--seed", type=int, default=7)

    args = ap.parse_args(argv)
    if args.cmd == "replay":
        cmd_replay(args)
    elif args.cmd == "simulate":
        cmd_simulate(args)
    elif args.cmd == "judge":
        cmd_judge(args)
    elif args.cmd == "impact":
        cmd_impact(args)
    elif args.cmd == "audit":
        cmd_audit(args)
    elif args.cmd == "run":
        cmd_replay(args)
        cmd_simulate(args)
        cmd_judge(args)
        cmd_impact(args)
        cmd_audit(args)


if __name__ == "__main__":
    main()
