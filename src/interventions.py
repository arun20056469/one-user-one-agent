"""
interventions.py - intervention planner (decide what, if anything, to change).

Options: HOME_PAGE, PUSH, WHATSAPP, IN_APP, or NONE with a recorded reason.

Proportionality policy (documented, deterministic):
  * A session-active viewer gets the home page first: it is in-session,
    needs no consent and cannot spam anyone.
  * A message (push/WhatsApp/in-app) is only planned when the goal needs a
    behaviour the page cannot deliver, at most one message per viewer per
    IST day, from the correct consent state.
  * In quiet hours outbound messages are not sent now; they are SCHEDULED
    for 08:00 IST (policy: "schedule for later").
  * WhatsApp uses only the three approved templates; variables come from the
    catalogue or pricing - never free text.
  * If nothing is safe or evidenced, action = NONE with a reason (a real
    decision, not a missing row).

Every content item chosen here is checked again - independently - by the
verifier. The planner never treats its own selection as proof.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .facts import ViewerFacts
from .pack import Pack
from .utils import IST, in_quiet_hours, ist, parse_ts, z

CONVERSION_GOALS = {"first_paid_7d", "trial_to_paid_7d", "quarterly_upgrade_10d", "whatsapp_consent_5d"}
ENGAGEMENT_GOALS = {"first_full_episode_3d", "finish_first_episode_5d", "second_episode_7d",
                    "search_to_play_2d", "deepen_first_session_3d", "weekly_active_3d",
                    "reactivation_14d"}


@dataclass
class Proposal:
    action: str                      # HOME_PAGE | PUSH | WHATSAPP | IN_APP | NONE
    reason: str
    goal_id: str | None = None
    dialect: dict = field(default_factory=dict)
    content: list = field(default_factory=list)   # ["title:Txxxx", ...]
    copy: dict = field(default_factory=dict)      # title/body/template/vars
    deeplink: str = ""
    schedule_for: str | None = None  # set when quiet hours force deferral
    evidence: list = field(default_factory=list)
    assumptions: list = field(default_factory=list)
    confidence: float = 0.5
    confidence_reason: str = ""


class InterventionPlanner:
    def __init__(self, pack: Pack):
        self.pack = pack

    # ------------------------------------------------------------------ #
    def plan(self, goal_choice, facts: ViewerFacts, now: datetime,
             last_msg_ist_day: str | None) -> Proposal:
        p = Proposal(action="NONE", reason="", goal_id=goal_choice.goal_id,
                     dialect=dict(facts.dialect))
        if not goal_choice.goal_id:
            p.reason = "no_active_goal:" + goal_choice.reason
            p.confidence = 0.9
            p.confidence_reason = "no eligible goal is a sufficient reason to do nothing"
            return p
        p.evidence = list(goal_choice.evidence)
        p.assumptions = list(goal_choice.assumptions)

        dialect = facts.dialect.get("used")
        if not dialect:
            p.reason = "no_dialect_signal: cannot pick dialect-bound content safely"
            return p
        available = self.pack.dialect_titles(dialect, now)
        if not available:
            p.reason = f"no_available_titles_for_{dialect}"
            return p

        today = ist(now).date().isoformat()
        capped_today = last_msg_ist_day == today
        quiet = in_quiet_hours(now)
        want_msg = goal_choice.goal_id in CONVERSION_GOALS or goal_choice.goal_id == "reactivation_14d"

        # ---- surface choice ------------------------------------------------ #
        if want_msg and not capped_today:
            push_ok = facts.consent["notifications"]["value"]
            wa_ok = facts.consent["whatsapp"]["value"] and goal_choice.goal_id != "whatsapp_consent_5d"
            if quiet:
                # allowed to plan now, deliver at 08:00 IST
                when = _next_0800_ist(now)
                if push_ok:
                    self._fill_push(p, facts, now, available, goal_choice)
                    p.schedule_for = z(when)
                    p.reason = f"quiet_hours: push scheduled for {p.schedule_for}"
                    return p
                if wa_ok:
                    self._fill_whatsapp(p, facts, now, available, goal_choice)
                    p.schedule_for = z(when)
                    p.reason = f"quiet_hours: whatsapp scheduled for {p.schedule_for}"
                    return p
                self._fill_in_app(p, facts, now, available, goal_choice)
                p.reason = "quiet_hours_outbound_blocked: in-app instead (consent exempt, in-session)"
                return p
            if push_ok:
                self._fill_push(p, facts, now, available, goal_choice)
                p.reason = "conversion goal + push consent + no message today"
                return p
            if wa_ok:
                self._fill_whatsapp(p, facts, now, available, goal_choice)
                p.reason = "conversion goal + whatsapp consent + no message today"
                return p
            self._fill_in_app(p, facts, now, available, goal_choice)
            p.reason = "conversion goal but no messaging consent -> in-app (consent exempt)"
            return p

        # default surface for the open session
        if want_msg and capped_today:
            p.assumptions.append("already messaged today; only non-messaging surfaces allowed")
        self._fill_home_page(p, facts, now, available, goal_choice)
        p.reason = p.reason or "in-session home page is the proportionate action"
        return p

    # ------------------------------------------------------------------ #
    def _fill_home_page(self, p: Proposal, facts: ViewerFacts, now: datetime,
                        available: list[dict], goal_choice) -> None:
        page, rows, hero = build_home_page(self.pack, facts, available, now)
        if not rows or not hero:
            p.action = "NONE"
            p.reason = "home_page_impossible: no row reached the 3-title minimum"
            return
        p.action = "HOME_PAGE"
        p.content = ([f"title:{hero['title_id']}"] if hero else []) + \
                    [f"title:{t['title_id']}" for r in rows for t in r["titles"]]
        p.copy = {"hero": hero, "rows": rows, "ttl_minutes": self.pack.interventions["HOME_PAGE"]["ttl_minutes"]}
        p.evidence += [f"catalogue:{cid.split(':', 1)[1]}" for cid in p.content[:6]]
        p.confidence = 0.72
        p.confidence_reason = ("rules-built page in the viewer's own dialect; "
                               "content availability re-checked by the verifier")

    def _pick_title(self, facts: ViewerFacts, available: list[dict], now: datetime) -> dict | None:
        unfinished = set(facts.engagement.get("started_not_completed_slugs", []))
        for t in sorted(available, key=lambda t: -t["popularity"]):
            if t["content_slug"] in unfinished:
                return t
        watched = set(facts.engagement.get("watched_slugs", []))
        fresh = [t for t in available if t["content_slug"] not in watched]
        pool = fresh or available
        return max(pool, key=lambda t: t["popularity"])

    def _fill_push(self, p: Proposal, facts: ViewerFacts, now: datetime,
                   available: list[dict], goal_choice) -> None:
        t = self._pick_title(facts, available, now)
        if t is None:
            p.action, p.reason = "NONE", "no_title_for_push"
            return
        price_bits = " ₹1 trial (3 days), ₹149 monthly, ₹399 quarterly" if goal_choice.goal_id in CONVERSION_GOALS else ""
        title = f"Watch {t['content_slug']}"[:40]
        body = f"{t['language']}: new episodes of {t['content_slug']} are streaming.{price_bits}"[:120]
        p.action = "PUSH"
        p.copy = {"title": title, "body": body}
        p.deeplink = f"ott://title/{t['title_id']}"
        p.content = [f"title:{t['title_id']}"]
        p.evidence.append(f"catalogue:{t['title_id']}")
        p.confidence = 0.62
        p.confidence_reason = "goal needs a return visit; consent and cap are clear; copy is catalogue/pricing-backed"

    def _fill_whatsapp(self, p: Proposal, facts: ViewerFacts, now: datetime,
                       available: list[dict], goal_choice) -> None:
        templates = {t["name"]: t for t in self.pack.interventions["WHATSAPP"]["templates"]}
        t = self._pick_title(facts, available, now)
        if t is None:
            p.action, p.reason = "NONE", "no_title_for_whatsapp"
            return
        if goal_choice.goal_id in CONVERSION_GOALS:
            tpl = templates["plan_reminder"]
            variables = {}
        else:
            unfinished = set(facts.engagement.get("started_not_completed_slugs", []))
            if t["content_slug"] in unfinished:
                eps = self.pack.episodes_by_title.get(t["title_id"], [])
                tpl = templates["finish_episode"]
                variables = {"title_name": t["content_slug"],
                             "episode_number": str(eps[0]["episode_number"] if eps else 1)}
            else:
                tpl = templates["new_in_dialect"]
                variables = {"title_name": t["content_slug"], "dialect_language": t["language"]}
        p.action = "WHATSAPP"
        p.copy = {"template": tpl["name"], "text": tpl["text"], "variables": variables}
        p.deeplink = ""
        p.content = [f"title:{t['title_id']}"]
        p.evidence.append(f"catalogue:{t['title_id']}|template:{tpl['name']}")
        p.confidence = 0.6
        p.confidence_reason = "approved template only; every variable sourced from catalogue/pricing"

    def _fill_in_app(self, p: Proposal, facts: ViewerFacts, now: datetime,
                     available: list[dict], goal_choice) -> None:
        t = self._pick_title(facts, available, now)
        if t is None:
            p.action, p.reason = "NONE", "no_title_for_in_app"
            return
        price_bits = " ₹1 trial (3 days), ₹149 monthly, ₹399 quarterly." if goal_choice.goal_id in CONVERSION_GOALS else ""
        p.action = "IN_APP"
        p.copy = {"body": f"{t['content_slug']} - {t['language']} series you may like.{price_bits}"[:200]}
        p.content = [f"title:{t['title_id']}"]
        p.evidence.append(f"catalogue:{t['title_id']}")
        p.confidence = 0.58
        p.confidence_reason = "in-app needs no consent and fits an open session"


def _next_0800_ist(now: datetime) -> datetime:
    nxt = now.astimezone(IST).replace(hour=8, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt = nxt + timedelta(days=1)
    return nxt


# --------------------------------------------------------------------------- #
# home page builder: rules only, no model
# --------------------------------------------------------------------------- #
def build_home_page(pack: Pack, facts: ViewerFacts, available: list[dict], now: datetime):
    cfg = pack.interventions["HOME_PAGE"]
    min_t, max_t = cfg["min_titles_per_row"], cfg["max_titles_per_row"]
    by_slug = {t["content_slug"]: t for t in available}
    used_slugs: set[str] = set()
    rows: list[dict] = []

    def take(cands: list[dict], k: int, reserved: set[str]) -> list[dict]:
        """Pick up to k titles avoiding slugs already committed to the page.
        The row is only committed by the caller if it reaches the minimum."""
        picked = []
        seen = set(reserved)
        for t in cands:
            if t["content_slug"] in seen:
                continue
            picked.append(t)
            seen.add(t["content_slug"])
            if k and len(picked) >= k:
                break
        return picked

    watched = set(facts.engagement.get("watched_slugs", []))
    unfinished = set(facts.engagement.get("started_not_completed_slugs", []))
    watched_genres = set(facts.engagement.get("watched_genres", []))

    row_specs: list[tuple[str, list[dict]]] = []
    # next_episode: titles the viewer started but never finished
    unfinished_titles = [by_slug[s] for s in unfinished if s in by_slug]
    row_specs.append(("next_episode", unfinished_titles))
    # because_you_watched: same genre as what they watched, unwatched titles
    if watched_genres:
        row_specs.append(("because_you_watched",
                          sorted([t for t in available if t["genre"] in watched_genres
                                  and t["content_slug"] not in watched],
                                 key=lambda t: -t["popularity"])))
    row_specs.append(("popular_in_dialect", sorted(available, key=lambda t: -t["popularity"])))
    row_specs.append(("new_releases",
                      sorted([t for t in available if (now - parse_ts(t["available_from"])).days <= 60],
                             key=lambda t: -t["popularity"])))
    for genre in ("micro_drama", "comedy", "drama", "thriller"):
        row_specs.append(("genre", [t for t in available if t["genre"] == genre]))

    hero = None
    # hero: most popular available title, excluded from rows (slug uniqueness)
    hero_pool = sorted(available, key=lambda t: -t["popularity"])
    if hero_pool:
        hero = hero_pool[0]
        used_slugs.add(hero["content_slug"])

    for kind, cands in row_specs:
        if len(rows) >= cfg["max_rows"]:
            break
        if kind not in cfg["allowed_row_types"]:
            continue
        titles = take(cands, max_t, used_slugs)
        if len(titles) < min_t:      # row with fewer than 3 valid titles is dropped
            continue
        used_slugs.update(t["content_slug"] for t in titles)
        rows.append({"type": kind, "titles": [_slim(t) for t in titles]})
    if hero is not None:
        hero = _slim(hero)
    return {"hero": hero}, rows, hero


def _slim(t: dict) -> dict:
    """Action payloads carry no free-form catalogue text: descriptions are
    content (sometimes hostile) and never become instruction or copy."""
    return {"title_id": t["title_id"], "content_slug": t["content_slug"],
            "genre": t["genre"], "language": t["language"], "dialect": t["dialect"]}
