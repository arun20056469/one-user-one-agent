"""
simulate_outcomes.py - 14-day user-behaviour simulator.

Reads the data pack plus the agent's actions.jsonl and plays out what viewers
do afterwards. Writes outcome_events.jsonl and simulator_report.json.

The simulator is deliberately seed-driven and has no knowledge of the agent's
internals: evaluators rerun it with a DIFFERENT seed and an unseen replay day.

    python simulator/simulate_outcomes.py \
        --pack . --actions ../sample_run/actions.jsonl \
        --out ../sample_run --seed 7

Assumptions (documented, kept small and honest):
  * Baseline behaviour per viewer comes from their own 30-day history.
  * An action only changes behaviour through small, channel-appropriate
    effects (home page -> more plays while the page lives; push/WhatsApp ->
    more opens that day; in-app -> more plays on that open).
  * Holdout viewers receive no effects at all. Any action aimed at a holdout
    viewer is counted as a policy violation in the report.
  * 'watchlist_add' is never emitted: the platform does not track watchlists.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import hashlib
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
FMT = "%Y-%m-%dT%H:%M:%SZ"


def z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(FMT)


def parse(s: str) -> datetime:
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc)


def stable_rng(seed: int, *parts) -> random.Random:
    h = hashlib.sha256(("|".join(str(p) for p in (seed, *parts))).encode()).digest()
    return random.Random(int.from_bytes(h[:8], "big"))


def load_pack(pack: Path):
    profiles = {}
    for row in csv.DictReader(open(pack / "data" / "profiles_snapshot.csv", encoding="utf-8")):
        profiles[row["viewer_id"]] = row  # last row wins for duplicates (documented)
    arms = {r["viewer_id"]: r["arm"] for r in csv.DictReader(open(pack / "replay" / "arms.csv", encoding="utf-8"))}
    triggers = [json.loads(l) for l in open(pack / "replay" / "triggers.jsonl", encoding="utf-8")]
    # historical behaviour: opens/plays/completes per viewer over the pack window
    hist = defaultdict(lambda: {"opens": 0, "plays": 0, "completes": 0, "trials": 0, "paid": 0})
    with open(pack / "data" / "events.jsonl", encoding="utf-8") as f:
        for line in f:
            e = json.loads(line)
            h = hist[e["viewer_id"]]
            t = e["event_type"]
            if t == "app_open":
                h["opens"] += 1
            elif t == "episode_play":
                h["plays"] += 1
            elif t == "episode_complete":
                h["completes"] += 1
            elif t == "purchase":
                p = e.get("props", {}).get("plan")
                if p == "trial":
                    h["trials"] += 1
                elif p in ("monthly", "quarterly"):
                    h["paid"] += 1
    return profiles, arms, triggers, hist


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", required=True)
    ap.add_argument("--actions", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()

    pack, actions_path, out_dir = Path(args.pack), Path(args.actions), Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    profiles, arms, triggers, hist = load_pack(pack)

    actions = [json.loads(l) for l in open(actions_path, encoding="utf-8")]
    triggered = {}
    for t in triggers:  # first (non-duplicate) trigger per viewer on replay day
        triggered.setdefault(t["viewer_id"], parse(t["deliver_at"]))
    replay_day = (max(parse(t["deliver_at"]) for t in triggers)).astimezone(IST).date()

    # ---- effects ledger: viewer -> list of (publish_time, kind, meta) -------
    ledger = defaultdict(list)
    holdout_violations = []
    served_counts = Counter()
    for a in actions:
        vid = a.get("viewer_id")
        if a.get("status") in ("BLOCKED", "SUPPRESSED") or a.get("action") in (None, "NONE"):
            continue
        if arms.get(vid) == "holdout":
            holdout_violations.append({"action_id": a.get("action_id"), "viewer_id": vid})
            continue
        if a.get("status") == "SCHEDULED":
            when = parse(a["scheduled_for"])
        else:
            when = parse(a["published_at"])
        ledger[vid].append((when, a["action"], a))
        served_counts[a["action"]] += 1

    outcomes = []
    oid = 0

    def emit(vid, dt, etype, props=None):
        nonlocal oid
        oid += 1
        outcomes.append({
            "event_id": f"O-{oid:07d}",
            "viewer_id": vid,
            "event_type": etype,
            "occurred_at": z(dt),
            "sent_at": z(dt),
            "received_at": z(dt + timedelta(seconds=2)),
            "app_dialect": profiles.get(vid, {}).get("preferred_dialect", "d1"),
            "props": props or {},
            "source": "simulator",
        })

    start = datetime(replay_day.year, replay_day.month, replay_day.day, tzinfo=timezone.utc)
    for vid, arm in arms.items():
        h = hist.get(vid, {"opens": 0, "plays": 0, "completes": 0, "trials": 0, "paid": 0})
        prof = profiles.get(vid, {})
        engaged = min(1.0, h["opens"] / 60.0)  # 60+ opens in 30 days = highly engaged
        base_open = 0.06 + 0.45 * engaged
        base_play_on_open = 0.14 + 0.28 * (h["plays"] / max(1.0, h["plays"] + 8.0)) + 0.10 * engaged
        base_complete = 0.30 + 0.30 * engaged
        acts = ledger.get(vid, [])

        # replay-day residual visit (the trigger itself): viewer is on the app
        day0_trig = triggered.get(vid)
        open_days = set()
        if day0_trig is not None:
            open_days.add(0)
        for day in range(args.days + 1):
            r = stable_rng(args.seed, vid, day)
            d0 = start + timedelta(days=day)
            # actions for this day
            home_pages = [a for (w, k, a) in acts if k == "HOME_PAGE" and (w.astimezone(IST).date() - replay_day).days == day]
            pushes = [a for (w, k, a) in acts if k == "PUSH" and (w.astimezone(IST).date() - replay_day).days == day]
            wapps = [a for (w, k, a) in acts if k == "WHATSAPP" and (w.astimezone(IST).date() - replay_day).days == day]
            inapps = [a for (w, k, a) in acts if k == "IN_APP" and (w.astimezone(IST).date() - replay_day).days == day]

            p_open = base_open
            p_open += 0.16 * len(pushes[:1]) + 0.12 * len(wapps[:1])
            if day == 0 and day0_trig is not None:
                p_open = 1.0  # they already opened
            p_open = min(0.95, p_open)
            opens_today = 1 if (day == 0 and day0_trig is not None) else (1 if r.random() < p_open else 0)
            if opens_today and r.random() < 0.35 * engaged:
                opens_today += 1  # second session
            if not opens_today:
                continue
            if day == 0 and day0_trig is not None:
                emit(vid, day0_trig, "app_open", {"replay_trigger": True})
                emit(vid, day0_trig + timedelta(minutes=2), "page_view", {"surface": "home"})
            else:
                emit(vid, d0 + timedelta(hours=r.randint(8, 21), minutes=r.randint(0, 59)), "app_open")

            p_play = base_play_on_open
            if home_pages:
                p_play += 0.16          # a relevant, dialect-matched page lifts plays
            if inapps:
                p_play += 0.07
            if r.random() < min(0.9, p_play):
                emit(vid, d0 + timedelta(hours=r.randint(9, 22), minutes=r.randint(0, 59)), "episode_play",
                     {"simulated": True})
                if r.random() < base_complete:
                    emit(vid, d0 + timedelta(hours=r.randint(9, 22), minutes=r.randint(0, 59)), "episode_complete",
                         {"simulated": True})
            # conversions
            p_buy = 0.003
            if prof.get("subscription_status") == "trial" or h["trials"]:
                p_buy += 0.012
            if pushes or wapps or home_pages:
                p_buy += 0.004
            if r.random() < p_buy:
                plan = "monthly" if (prof.get("subscription_status") in ("none", "lapsed", "trial") or h["paid"] == 0) else "quarterly"
                emit(vid, d0 + timedelta(hours=r.randint(10, 22), minutes=r.randint(0, 59)), "purchase",
                     {"plan": plan, "amount_inr": 149 if plan == "monthly" else 399, "simulated": True})
            # WhatsApp consent can flip after receiving an approved template
            if wapps and prof.get("whatsapp_opt_in") == "false":
                if r.random() < 0.10:
                    emit(vid, d0 + timedelta(hours=r.randint(10, 21), minutes=r.randint(0, 59)), "consent_updated",
                         {"channel": "whatsapp", "opt_in": True})

    outcomes.sort(key=lambda e: e["received_at"])
    with open(out_dir / "outcome_events.jsonl", "w", encoding="utf-8") as f:
        for e in outcomes:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    by_arm = Counter()
    for e in outcomes:
        if e["event_type"] in ("episode_complete", "purchase", "app_open"):
            by_arm[(arms.get(e["viewer_id"], "?"), e["event_type"])] += 1
    report = {
        "seed": args.seed,
        "days": args.days,
        "replay_day": replay_day.isoformat(),
        "viewers": len(arms),
        "arm_sizes": dict(Counter(arms.values())),
        "actions_applied": dict(served_counts),
        "holdout_violations": holdout_violations,
        "outcome_counts_by_arm": {f"{k[0]}:{k[1]}": v for k, v in sorted(by_arm.items())},
        "total_outcome_events": len(outcomes),
    }
    (out_dir / "simulator_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("seed", "total_outcome_events", "actions_applied", "holdout_violations")}, indent=2))


if __name__ == "__main__":
    main()
