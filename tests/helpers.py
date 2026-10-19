"""Shared fixtures for the test suite.

Builds a small deterministic copy of the data pack (200 viewers, 12 days,
60 triggers) with the SAME generator as the real pack, so every trap -
duplicate events, stale triggers, profile duplicates, dialect conflicts,
injected descriptions, goal-rule corrections - is present in miniature.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "_fixture_pack"

_pack_cache: dict = {}


def fixture_pack_path() -> Path:
    marker = FIXTURE / "config" / "goals.json"
    if not marker.exists():
        subprocess.run(
            [sys.executable, str(ROOT / "tools" / "generate_datapack.py"),
             "--out", str(FIXTURE), "--seed", "4242",
             "--viewers", "200", "--days", "12", "--triggers", "60"],
            check=True, cwd=ROOT)
    return FIXTURE


def get_pack():
    if "pack" not in _pack_cache:
        from src.pack import Pack
        _pack_cache["pack"] = Pack(fixture_pack_path())
    return _pack_cache["pack"]


def get_arms() -> dict:
    return get_pack().arms


def agent_arm_viewer(pack) -> str:
    return next(v for v, a in sorted(pack.arms.items()) if a == "agent")


def holdout_viewer(pack) -> str:
    return next(v for v, a in sorted(pack.arms.items()) if a == "holdout")


def viewer_with_profile(pack) -> str:
    return next(v for v in sorted(pack.profiles) if v in pack.arms)


def make_trigger(viewer_id: str, received: str, *, event_id: str = "T-EVT-1",
                 occurred: str | None = None, dialect: str = "d1",
                 props: dict | None = None) -> dict:
    from src.utils import parse_ts, z
    rec = parse_ts(received)
    occ = occurred or z(rec - __import__("datetime").timedelta(seconds=9))
    return {
        "trigger_id": "TR-TEST",
        "event_id": event_id,
        "viewer_id": viewer_id,
        "event_type": "app_open",
        "occurred_at": occ,
        "received_at": received,
        "deliver_at": received,
        "app_dialect": dialect,
        "props": props or {},
    }


def fresh_store(tmpdir: Path):
    from src.state import StateStore
    return StateStore(tmpdir / "state.db")


def make_agent(pack, store, ctx=None, provider=None):
    from src.agent import Agent, Ctx
    from src.provider import MockModelProvider
    prov = provider or MockModelProvider(pack)
    return Agent(pack, store, prov, ctx or Ctx())
