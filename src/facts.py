"""
facts.py - fact resolver.

Assembles every fact we hold about a viewer with source, observed time and
freshness, then resolves conflicts with explicit precedence rules (never
"biggest source wins"):

  subscription  purchase events (received_at order) > profile snapshot row
                > unknown. A purchase that arrives after the (day-stale)
                snapshot therefore overrides it.
  consent       a later opt_out event overrides profile opt-in; the freshest
                consent event of that channel wins; profile is the default.
  dialect       the app setting carried on the freshest received event wins
                (it is what the app will actually request); campaign deeplink
                second; profile preference last. Every disagreement is kept
                visible as a conflict on the output.
  signup        signup_completed event > profile > unknown.
  engagement    counted from deduplicated events with received_at <= now.

Freshness: every fact carries observed_at + freshness_s and a `stale` flag
(profile snapshots are one day stale by construction).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta

from .pack import Pack
from .utils import z

PROFILE_MAX_AGE_S = 24 * 3600


@dataclass
class Fact:
    key: str
    value: object
    source: str
    observed_at: str
    stale: bool = False
    note: str = ""

    def age_s(self, now: datetime) -> float:
        return (now - parse(self.observed_at)).total_seconds()


def parse(s: str) -> datetime:
    from .utils import parse_ts
    return parse_ts(s)


@dataclass
class ViewerFacts:
    viewer_id: str
    dialect: dict
    subscription: dict
    consent: dict
    signup: dict
    engagement: dict
    conflicts: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    assumptions: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def resolve(pack: Pack, viewer_id: str, now: datetime, extra_events: list[dict] | None = None) -> ViewerFacts:
    evs = list(pack.events_until(viewer_id, now))
    if extra_events:
        evs.extend(extra_events)
        evs.sort(key=lambda e: e["_received"])
    profile = pack.profiles.get(viewer_id)
    conflicts: list[str] = []
    evidence: list[str] = []
    assumptions: list[str] = []

    # ---------------- dialect ------------------------------------------- #
    app_dialects = [e for e in evs if e.get("app_dialect")]
    app_setting = app_dialects[-1]["app_dialect"] if app_dialects else None
    campaign = next((e["props"].get("campaign_dialect") for e in reversed(evs)
                     if e.get("props", {}).get("campaign_dialect")), None)
    pref = profile["preferred_dialect"] if profile else None
    seen = {d for d in (app_setting, campaign, pref) if d}
    if len(seen) > 1:
        conflicts.append(f"dialect app_setting:{app_setting} campaign:{campaign} profile:{pref}")
    if app_setting:
        dconf = []
        if pref and pref != app_setting:
            dconf.append(f"profile:{pref}")
        if campaign and campaign != app_setting:
            dconf.append(f"campaign:{campaign}")
        dialect = {"used": app_setting, "source": "app_setting", "conflicts": dconf}
        evidence.append(f"event:{app_dialects[-1]['event_id']}(app_dialect)")
    elif pref:
        dialect = {"used": pref, "source": "profile", "conflicts": []}
        evidence.append(f"profile:{viewer_id}(preferred_dialect)")
    else:
        dialect = {"used": None, "source": "none", "conflicts": []}
        assumptions.append("viewer has no dialect signal; no dialect-bound content may be used")

    # ---------------- subscription -------------------------------------- #
    purchases = [e for e in evs if e["event_type"] == "purchase"]
    sub_value, sub_source, sub_observed = "none", "derived_default", z(now)
    if profile:
        sub_value = profile["subscription_status"]
        sub_source = f"profile:{profile['snapshot_date']}"
        sub_observed = profile["row_updated_at"]
        evidence.append(f"profile:{viewer_id}(subscription_status)")
    if purchases:
        last = purchases[-1]
        plan = last["props"].get("plan")
        # a newer purchase event beats the (day-stale) profile snapshot
        if parse(last["received_at"]) >= parse(sub_observed):
            new_val = plan if plan in ("trial", "monthly", "quarterly") else "lapsed"
            if plan in ("monthly", "quarterly"):
                new_val = plan
            if new_val != sub_value:
                conflicts.append(f"subscription profile:{sub_value} event:{new_val}")
            sub_value, sub_source, sub_observed = new_val, f"event:{last['event_id']}", last["received_at"]
            evidence.append(f"event:{last['event_id']}(purchase {plan})")
    subscription = {"value": sub_value, "source": sub_source, "observed_at": sub_observed,
                    "stale": (now - parse(sub_observed)).total_seconds() > PROFILE_MAX_AGE_S and sub_source.startswith("profile")}

    # ---------------- consent -------------------------------------------- #
    consent = {}
    for ch, prof_key, evt_types in (
        ("notifications", "notifications_opt_in", ("opt_out", "opt_in")),
        ("whatsapp", "whatsapp_opt_in", ("opt_in", "opt_out")),
    ):
        if profile:
            val = profile[prof_key].lower() == "true"
            src = f"profile:{profile['snapshot_date']}"
            obs = profile["row_updated_at"]
        else:
            val, src, obs = False, "default_deny", z(now)
        for e in reversed(evs):
            if e["event_type"] == "opt_out" and e["props"].get("channel") == ch:
                if parse(e["received_at"]) >= parse(obs):
                    if val:
                        conflicts.append(f"{ch} consent profile:{val} event:False")
                    val, src, obs = False, f"event:{e['event_id']}", e["received_at"]
                break
            if e["event_type"] == "consent_updated" and e["props"].get("channel") == ch:
                if parse(e["received_at"]) >= parse(obs):
                    val, src, obs = bool(e["props"].get("opt_in")), f"event:{e['event_id']}", e["received_at"]
                break
        consent[ch] = {"value": val, "source": src, "observed_at": obs,
                       "stale": (now - parse(obs)).total_seconds() > 7 * 24 * 3600}
        evidence.append(f"{src}({ch} consent)")

    # ---------------- signup --------------------------------------------- #
    signups = [e for e in evs if e["event_type"] == "signup_completed"]
    if signups:
        signup = {"value": signups[0]["occurred_at"], "source": f"event:{signups[0]['event_id']}",
                  "observed_at": signups[0]["received_at"], "stale": False}
    elif profile:
        signup = {"value": profile["signup_at"], "source": f"profile:{profile['snapshot_date']}",
                  "observed_at": profile["row_updated_at"], "stale": True}
    else:
        # brand-new viewer with no profile row and no signup event yet: build
        # a provisional signup from the earliest event we have.
        if evs:
            signup = {"value": evs[0]["occurred_at"], "source": f"event:{evs[0]['event_id']}(earliest)",
                      "observed_at": evs[0]["received_at"], "stale": False}
            assumptions.append("signup time inferred from earliest event (no profile row, no signup event)")
        else:
            signup = {"value": None, "source": "unknown", "observed_at": z(now), "stale": False}
    if profile is None:
        assumptions.append("viewer has no profile row (new or missing export row); facts built from events")

    # ---------------- engagement counts ----------------------------------- #
    opens = [e for e in evs if e["event_type"] == "app_open"]
    plays = [e for e in evs if e["event_type"] == "episode_play"]
    completes = [e for e in evs if e["event_type"] == "episode_complete"]
    searches = [e for e in evs if e["event_type"] == "search"]
    started_slugs = {e["props"].get("content_slug") for e in plays}
    completed_slugs = {e["props"].get("content_slug") for e in completes}
    started_not_completed = started_slugs - completed_slugs
    watched_slugs = started_slugs | completed_slugs
    slug_to_genre = {t["content_slug"]: t["genre"] for t in pack.titles}
    engagement = {
        "opens": len(opens), "plays": len(plays), "completes": len(completes),
        "searches": len(searches),
        "last_open_at": opens[-1]["received_at"] if opens else None,
        "last_play_at": plays[-1]["received_at"] if plays else None,
        "last_search_at": searches[-1]["received_at"] if searches else None,
        "started_not_completed_slugs": sorted(s for s in started_not_completed if s),
        "completed_episode_count": len(completes),
        "distinct_slugs_played": len({s for s in started_slugs if s}),
        "watched_slugs": sorted(s for s in watched_slugs if s),
        "watched_genres": sorted({slug_to_genre[s] for s in watched_slugs if s in slug_to_genre}),
    }
    if profile is not None:
        dup_rows = pack.profile_rows(viewer_id)
        if len(dup_rows) > 1:
            conflicts.append(f"profile duplicate rows:{len(dup_rows)} (kept freshest row_updated_at)")

    return ViewerFacts(
        viewer_id=viewer_id,
        dialect=dialect,
        subscription=subscription,
        consent=consent,
        signup=signup,
        engagement=engagement,
        conflicts=conflicts,
        evidence=evidence,
        assumptions=assumptions,
    )
