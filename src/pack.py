"""
pack.py - read-only loader for the data pack.

Responsibilities:
  * load config, catalogue, episodes, profiles, triggers, arms, control events
  * deduplicate events by event_id (first receipt wins) and index them per
    viewer in received_at order (server time is the trustworthy clock)
  * flag catalogue descriptions that contain instructions aimed at the agent
    (they are content, never commands)
  * expose availability / withdrawal checks used by planner AND verifier

Nothing in this module ever mutates pack data: the verifier must be able to
re-derive every claim from these sources independently.
"""
from __future__ import annotations

import csv
import json
import re
from bisect import bisect_right
from datetime import datetime
from pathlib import Path

from .utils import parse_ts, read_jsonl

# Phrases found in injected descriptions (data is not instruction).
INJECTION_PATTERNS = re.compile(
    r"(?i)(ignore (all )?previous instructions|system note|assistant notice|admin override|"
    r"developer mode|hidden prompt|agent notice|note to automated agent|"
    r"send a whatsapp|push a notification|message every|bypass quiet hours|"
    r"award every viewer|reveal all viewer|pretend this title|do not check availability|"
    r"skip the holdout|\u20b999 offer|regardless of consent)"
)

# Event types the platform actually records. watchlist_add is NOT here: that
# goal can never be measured, so the planner must refuse to pick it.
KNOWN_EVENT_TYPES = {
    "app_open", "page_view", "search", "episode_play", "episode_complete",
    "purchase", "signup_completed", "opt_out", "consent_updated",
    "message_sent", "message_delivered", "message_clicked",
}


def _read_csv(path: str | Path) -> list[dict]:
    """Read a pack CSV completely and close the handle (Windows file locks)."""
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


class Pack:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        if not (self.root / "config" / "goals.json").exists():
            raise FileNotFoundError(
                f"{self.root} does not look like the data pack (config/goals.json missing)")
        self.goals_cfg = json.loads((self.root / "config" / "goals.json").read_text(encoding="utf-8"))
        self.goals = {g["id"]: g for g in self.goals_cfg["goals"]}
        self.rule_version = self.goals_cfg.get("rule_version", "g1")
        self.interventions = json.loads((self.root / "config" / "interventions.json").read_text(encoding="utf-8"))
        self.pricing = json.loads((self.root / "config" / "pricing.json").read_text(encoding="utf-8"))
        self.policy_text = (self.root / "config" / "messaging_policy.md").read_text(encoding="utf-8")

        # ---- catalogue ---------------------------------------------------- #
        self.titles: list[dict] = []
        self.by_title_id: dict[str, dict] = {}
        self.slug_to_title_ids: dict[str, list[str]] = {}
        self.injected: list[dict] = []
        for row in _read_csv(self.root / "data" / "catalogue.csv"):
            row["n_episodes"] = int(row["n_episodes"])
            row["popularity"] = float(row["popularity"])
            self.titles.append(row)
            self.by_title_id[row["title_id"]] = row
            self.slug_to_title_ids.setdefault(row["content_slug"], []).append(row["title_id"])
            m = INJECTION_PATTERNS.search(row.get("description", ""))
            if m:
                self.injected.append({"title_id": row["title_id"], "match": m.group(0),
                                      "description": row.get("description", "")})
        self._injected_ids = {i["title_id"] for i in self.injected}

        self.episodes_by_title: dict[str, list[dict]] = {}
        for row in _read_csv(self.root / "data" / "episodes.csv"):
            row["episode_number"] = int(row["episode_number"])
            row["duration_s"] = int(row["duration_s"])
            self.episodes_by_title.setdefault(row["title_id"], []).append(row)
        for eps in self.episodes_by_title.values():
            eps.sort(key=lambda e: e["episode_number"])

        # ---- profiles (duplicate rows: freshest row_updated_at wins) ------ #
        self.profiles: dict[str, dict] = {}
        self.profile_duplicates: dict[str, list[dict]] = {}
        for row in _read_csv(self.root / "data" / "profiles_snapshot.csv"):
            prev = self.profiles.get(row["viewer_id"])
            if prev is None:
                self.profiles[row["viewer_id"]] = row
                self.profile_duplicates[row["viewer_id"]] = [row]
            else:
                self.profile_duplicates[row["viewer_id"]].append(row)
                if row["row_updated_at"] > prev["row_updated_at"]:
                    self.profiles[row["viewer_id"]] = row

        # ---- events: dedupe by event_id, index per viewer ------------------ #
        self.events_by_viewer: dict[str, list[dict]] = {}
        self.event_index: dict[str, dict] = {}
        self.duplicate_event_ids = 0
        self.late_events = 0
        seen: set[str] = set()
        with open(self.root / "data" / "events.jsonl", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                e = json.loads(line)
                eid = e["event_id"]
                if eid in seen:
                    self.duplicate_event_ids += 1
                    continue
                seen.add(eid)
                e["_received"] = parse_ts(e["received_at"])
                e["_occurred"] = parse_ts(e["occurred_at"])
                if (e["_received"] - e["_occurred"]).total_seconds() > 6 * 3600:
                    self.late_events += 1
                self.event_index[eid] = e
                self.events_by_viewer.setdefault(e["viewer_id"], []).append(e)
        for evs in self.events_by_viewer.values():
            evs.sort(key=lambda e: e["_received"])

        # ---- replay ------------------------------------------------------- #
        self.triggers = read_jsonl(self.root / "replay" / "triggers.jsonl")
        self.live_events = read_jsonl(self.root / "replay" / "live_events.jsonl")
        self.control_events = read_jsonl(self.root / "replay" / "control_events.jsonl")
        self.model_faults = read_jsonl(self.root / "replay" / "model_faults.jsonl")
        self.arms: dict[str, str] = {
            r["viewer_id"]: r["arm"]
            for r in _read_csv(self.root / "replay" / "arms.csv")
        }

    # ------------------------------------------------------------------ #
    # query helpers (planner AND verifier use these)
    # ------------------------------------------------------------------ #
    def events_until(self, viewer_id: str, until) -> list[dict]:
        """Events for a viewer that the server had received by `until`."""
        evs = self.events_by_viewer.get(viewer_id, [])
        return [e for e in evs if e["_received"] <= until]

    def events_of_type(self, viewer_id: str, until, etype: str) -> list[dict]:
        return [e for e in self.events_until(viewer_id, until) if e["event_type"] == etype]

    def is_available(self, title_id: str, when: datetime) -> bool:
        t = self.by_title_id.get(title_id)
        if t is None:
            return False
        if t.get("withdrawn_at"):
            if parse_ts(t["withdrawn_at"]) <= when:
                return False
        if parse_ts(t["available_from"]) > when:
            return False
        if parse_ts(t["available_to"]) <= when:
            return False
        return True

    def injection_for(self, title_id: str) -> dict | None:
        if title_id in self._injected_ids:
            return next(i for i in self.injected if i["title_id"] == title_id)
        return None

    def profile_rows(self, viewer_id: str) -> list[dict]:
        return self.profile_duplicates.get(viewer_id, [])

    def success_event_known(self, goal: dict) -> bool:
        return goal["success"]["event"] in KNOWN_EVENT_TYPES

    def title_name(self, title_id: str) -> str:
        t = self.by_title_id.get(title_id)
        return t["content_slug"] if t else title_id

    def dialect_titles(self, dialect: str, when: datetime) -> list[dict]:
        return [t for t in self.titles
                if t["dialect"] == dialect and self.is_available(t["title_id"], when)]
