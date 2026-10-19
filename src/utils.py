"""Shared time and IO helpers. All pack timestamps are UTC; user-facing rules
(quiet hours, per-day caps) use IST = UTC+05:30."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
FMT = "%Y-%m-%dT%H:%M:%SZ"


def parse_ts(s: str) -> datetime:
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc)


def z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(FMT)


def ist(dt: datetime) -> datetime:
    return dt.astimezone(IST)


def ist_date(dt: datetime) -> str:
    return ist(dt).date().isoformat()


def ist_hour(dt: datetime) -> int:
    return ist(dt).hour


def in_quiet_hours(dt: datetime) -> bool:
    h = ist_hour(dt)
    return h >= 22 or h < 8


def read_jsonl(path: Path) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
