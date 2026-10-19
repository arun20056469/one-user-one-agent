"""
state.py - durable per-viewer state (SQLite).

One row per viewer holds facts, the single active goal, learnings and the
messaging counters used by the frequency cap. Decisions, actions and trigger
idempotency keys live in their own tables so an evaluator can audit any
moment: what the agent knew, decided and observed.

The store also survives "datastore slow" windows: the harness wraps writes in
a latency injection while control_events say the store is slow, and every
decision records whether it had to fall back to the cached snapshot.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .utils import z

SCHEMA = """
CREATE TABLE IF NOT EXISTS viewers(
    viewer_id TEXT PRIMARY KEY,
    facts_json TEXT NOT NULL DEFAULT '{}',
    active_goal_json TEXT,
    learnings_json TEXT NOT NULL DEFAULT '[]',
    last_msg_ist_day TEXT,
    last_action_json TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions(
    decision_id TEXT PRIMARY KEY,
    viewer_id TEXT NOT NULL,
    decision_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions(
    action_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    viewer_id TEXT NOT NULL,
    status TEXT NOT NULL,
    action_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS triggers_seen(
    event_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    seen_at TEXT NOT NULL
);
"""


class StateStore:
    def __init__(self, path: str | Path, slow_latency_s: float = 0.0):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.slow_latency_s = slow_latency_s  # injected datastore slowness
        self.cache: dict[str, dict] = {}      # in-memory fallback snapshot
        self.slow_mode = False

    # ------------------------------------------------------------------ #
    def _lag(self):
        if self.slow_mode and self.slow_latency_s:
            time.sleep(self.slow_latency_s)

    def get_viewer(self, viewer_id: str) -> dict:
        if viewer_id in self.cache:
            cached = self.cache[viewer_id]
        else:
            self._lag()
            row = self.conn.execute(
                "SELECT viewer_id, facts_json, active_goal_json, learnings_json,"
                " last_msg_ist_day, last_action_json, updated_at FROM viewers WHERE viewer_id=?",
                (viewer_id,)).fetchone()
            cached = None if row is None else {
                "viewer_id": row[0], "facts": json.loads(row[1]),
                "active_goal": json.loads(row[2]) if row[2] else None,
                "learnings": json.loads(row[3]),
                "last_msg_ist_day": row[4],
                "last_action": json.loads(row[5]) if row[5] else None,
                "updated_at": row[6]}
        if cached is None:
            cached = {"viewer_id": viewer_id, "facts": {}, "active_goal": None,
                      "learnings": [], "last_msg_ist_day": None, "last_action": None,
                      "updated_at": None, "_cache_only": True}
        return cached

    def put_viewer(self, viewer: dict) -> None:
        v = dict(viewer)
        v.pop("_cache_only", None)
        self.cache[v["viewer_id"]] = v  # always mirror to memory (fast path)
        self._lag()
        self.conn.execute(
            "INSERT INTO viewers(viewer_id, facts_json, active_goal_json, learnings_json,"
            " last_msg_ist_day, last_action_json, updated_at) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(viewer_id) DO UPDATE SET facts_json=excluded.facts_json,"
            " active_goal_json=excluded.active_goal_json, learnings_json=excluded.learnings_json,"
            " last_msg_ist_day=excluded.last_msg_ist_day, last_action_json=excluded.last_action_json,"
            " updated_at=excluded.updated_at",
            (v["viewer_id"], json.dumps(v.get("facts", {}), ensure_ascii=False),
             json.dumps(v.get("active_goal"), ensure_ascii=False),
             json.dumps(v.get("learnings", []), ensure_ascii=False),
             v.get("last_msg_ist_day"),
             json.dumps(v.get("last_action"), ensure_ascii=False),
             z(__import__("datetime").datetime.now(__import__("datetime").timezone.utc))))
        self.conn.commit()

    # ------------------------------------------------------------------ #
    def trigger_seen(self, event_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM triggers_seen WHERE event_id=?", (event_id,)).fetchone()
        return row is not None

    def mark_trigger(self, event_id: str, decision_id: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO triggers_seen(event_id, decision_id, seen_at) VALUES(?,?,?)",
                          (event_id, decision_id, z(__import__("datetime").datetime.now(__import__("datetime").timezone.utc))))
        self.conn.commit()

    def add_decision(self, decision: dict) -> None:
        self.conn.execute("INSERT OR REPLACE INTO decisions(decision_id, viewer_id, decision_json) VALUES(?,?,?)",
                          (decision["decision_id"], decision["viewer_id"],
                           json.dumps(decision, ensure_ascii=False)))
        self.conn.commit()

    def add_action(self, action: dict) -> None:
        self.conn.execute("INSERT OR REPLACE INTO actions(action_id, decision_id, viewer_id, status, action_json)"
                          " VALUES(?,?,?,?,?)",
                          (action["action_id"], action.get("decision_id", ""), action.get("viewer_id", ""),
                           action.get("status", ""), json.dumps(action, ensure_ascii=False)))
        self.conn.commit()

    def all_decisions(self) -> list[dict]:
        return [json.loads(r[0]) for r in self.conn.execute("SELECT decision_json FROM decisions ORDER BY decision_id")]

    def all_actions(self) -> list[dict]:
        return [json.loads(r[0]) for r in self.conn.execute("SELECT action_json FROM actions ORDER BY action_id")]

    def pending_actions(self) -> list[dict]:
        return [json.loads(r[0]) for r in
                self.conn.execute("SELECT action_json FROM actions WHERE status='SCHEDULED'")]

    def cancel_pending_for_titles(self, title_ids: set[str], when: str, reason: str) -> list[dict]:
        cancelled = []
        for a in self.pending_actions():
            content = set(a.get("content", []))
            ids = {c.split(":", 1)[1] for c in content if c.startswith("title:")}
            if ids & title_ids:
                a["status"] = "CANCELLED"
                a["cancelled_at"] = when
                a["cancel_reason"] = reason
                self.add_action(a)
                cancelled.append(a)
        self.conn.commit()
        return cancelled

    def cancel_pending_for_goal(self, viewer_id: str, when: str, reason: str) -> list[dict]:
        cancelled = []
        for a in self.pending_actions():
            if a.get("viewer_id") == viewer_id and a.get("status") == "SCHEDULED":
                a["status"] = "CANCELLED"
                a["cancelled_at"] = when
                a["cancel_reason"] = reason
                self.add_action(a)
                cancelled.append(a)
        self.conn.commit()
        return cancelled

    def add_learning(self, viewer_id: str, learning: dict) -> None:
        v = self.get_viewer(viewer_id)
        v["learnings"].append(learning)
        self.put_viewer(v)

    def flush_cache(self) -> None:
        for v in list(self.cache.values()):
            self.put_viewer(v)

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        """Release the SQLite handle.

        Windows cannot delete a file that still has an open handle, so every
        long-lived process and every test must close the store explicitly
        (also available as a context manager; __del__ is a safety net).
        """
        conn = getattr(self, "conn", None)
        if conn is None:
            return
        try:
            conn.commit()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    def __del__(self):
        self.close()
