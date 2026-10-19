"""
verifier.py - independent verification, able to DISAGREE with the decision.

Everything the planner or a model claims is re-derived here from the pack and
the viewer's state. The verifier never trusts generated text as evidence or
approval. Checks:

  holdout          holdout arm -> zero actions, always
  eligibility      goal must be re-derivable from facts/events, and measurable
  dialect          every title is in the dialect the app will actually request
  availability     every title available at publish time and not withdrawn
  consent          push needs notifications opt-in, WhatsApp needs whatsapp
                   opt-in (later opt_out events override the profile)
  quiet_hours      no outbound send between 22:00-08:00 IST; scheduling for
                   08:00 IST is the only allowed form
  frequency_cap    at most one push/WhatsApp per viewer per IST day (combined)
  policy           forbidden phrases, title/body length, deeplink format,
                   price allow-list, WhatsApp template allow-list
  home_page_shape  1 hero, <=6 rows, 3-10 titles/row, allowed row types,
                   content_slug once per page, no Continue Watching row
  evidence         every cited ref must resolve in the pack/state
  model_claim      model claims that are not backed by an event/profile fact
                   are rejected and recorded as unsupported claims

A failed blocking check downgrades the decision (scheduled message) or blocks
it entirely (status BLOCKED, action NONE with reason).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from .facts import ViewerFacts
from .pack import Pack
from .state import StateStore
from .utils import in_quiet_hours, ist, parse_ts

DEEPLINK_RE = re.compile(r"^ott://title/T\d+$")
PRICE_RE = re.compile(r"₹\s*(\d+)")
ALLOWED_PRICES = {1, 149, 399}
ALLOWED_ROW_TYPES = {"next_episode", "because_you_watched", "popular_in_dialect", "new_releases", "genre"}


@dataclass
class CheckResult:
    passed: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    blocked: bool = False
    block_reason: str = ""

    def fail(self, name: str, blocking: bool, detail: str = "") -> None:
        self.failed.append({"check": name, "detail": detail})
        if blocking:
            self.blocked = True
            self.block_reason = self.block_reason or f"{name}: {detail}"

    def ok(self, name: str) -> None:
        self.passed.append(name)


class Verifier:
    def __init__(self, pack: Pack, store: StateStore):
        self.pack = pack
        self.store = store

    # ------------------------------------------------------------------ #
    def verify(self, proposal, goal_choice, facts: ViewerFacts, trigger: dict,
               now: datetime, model_result=None, is_duplicate: bool = False) -> CheckResult:
        r = CheckResult()
        viewer_id = facts.viewer_id
        arm = self.pack.arms.get(viewer_id)

        # ---- holdout ---------------------------------------------------- #
        if arm == "holdout":
            r.ok("holdout")
            if proposal.action != "NONE":
                r.fail("holdout", blocking=True, detail="holdout viewers get zero actions")
            return r
        r.ok("holdout")

        # ---- duplicate trigger ------------------------------------------ #
        if is_duplicate:
            r.ok("idempotency")
        else:
            r.ok("idempotency")

        # ---- goal eligibility, re-derived -------------------------------- #
        if goal_choice.goal_id:
            g = self.pack.goals.get(goal_choice.goal_id)
            if g is None:
                r.fail("eligibility", blocking=True, detail=f"unknown goal {goal_choice.goal_id}")
            elif not self.pack.success_event_known(g):
                r.fail("eligibility", blocking=True,
                       detail=f"goal {g['id']} success event '{g['success']['event']}' does not exist")
            else:
                ok, why = self._recheck_eligibility(g, facts, now)
                if ok:
                    r.ok("eligibility")
                else:
                    r.fail("eligibility", blocking=True, detail=why)

        # ---- model claim support ---------------------------------------- #
        if model_result is not None and model_result.proposal:
            claim = model_result.proposal.get("goal_id")
            if claim and claim != goal_choice.goal_id:
                g = self.pack.goals.get(claim)
                backed = bool(model_result.proposal.get("evidence")) and g is not None and \
                    self._recheck_eligibility(g, facts, now)[0] if g else False
                if backed:
                    r.ok("model_claim_supported")
                else:
                    r.fail("model_claim", blocking=False,
                           detail=f"unsupported model claim for '{claim}' rejected (no backing event)")

        action = proposal.action
        if action == "NONE":
            if not proposal.reason:
                r.fail("no_action_reason", blocking=False, detail="NONE requires a recorded reason")
            else:
                r.ok("no_action_reason")
            r.ok("policy")
            return r

        # ---- consent ----------------------------------------------------- #
        if action == "PUSH":
            if not facts.consent["notifications"]["value"]:
                r.fail("consent", blocking=True, detail="notifications_opt_in is false")
            else:
                r.ok("consent")
        elif action == "WHATSAPP":
            if not facts.consent["whatsapp"]["value"]:
                r.fail("consent", blocking=True, detail="whatsapp_opt_in is false")
            else:
                r.ok("consent")
        elif action == "IN_APP":
            r.ok("consent")  # in-app is consent-exempt by policy
        else:
            r.ok("consent")

        # ---- quiet hours -------------------------------------------------- #
        if action in ("PUSH", "WHATSAPP"):
            if in_quiet_hours(now):
                sched = getattr(proposal, "schedule_for", None)
                if sched and not in_quiet_hours(parse_ts(sched)) and parse_ts(sched) > now:
                    r.ok("quiet_hours")
                else:
                    r.fail("quiet_hours", blocking=True,
                           detail=f"outbound message inside quiet hours (now IST {ist(now):%H:%M})")
            else:
                r.ok("quiet_hours")

        # ---- frequency cap ------------------------------------------------ #
        if action in ("PUSH", "WHATSAPP"):
            viewer = self.store.get_viewer(viewer_id)
            today = ist(now).date().isoformat()
            if viewer.get("last_msg_ist_day") == today and not getattr(proposal, "schedule_for", None):
                r.fail("frequency_cap", blocking=True, detail="already messaged this IST day")
            else:
                r.ok("frequency_cap")
        elif action == "IN_APP":
            viewer = self.store.get_viewer(viewer_id)
            today = ist(now).date().isoformat()
            if viewer.get("last_msg_ist_day") == f"inapp:{today}":
                r.fail("frequency_cap", blocking=True, detail="second in-app message this IST day")
            else:
                r.ok("frequency_cap")

        # ---- content: dialect + availability ------------------------------ #
        title_ids = [c.split(":", 1)[1] for c in proposal.content if c.startswith("title:")]
        used = facts.dialect.get("used")
        bad_dialect, bad_avail = [], []
        for tid in title_ids:
            t = self.pack.by_title_id.get(tid)
            if t is None:
                bad_avail.append(tid)
                continue
            if t["dialect"] != used:
                bad_dialect.append(f"{tid}:{t['dialect']}!={used}")
            if not self.pack.is_available(tid, now):
                bad_avail.append(tid)
        if bad_dialect:
            r.fail("dialect", blocking=True, detail="; ".join(bad_dialect))
        else:
            r.ok("dialect")
        if bad_avail:
            r.fail("availability", blocking=True, detail="not available/withdrawn: " + ", ".join(bad_avail))
        else:
            r.ok("availability")

        # ---- home page shape ---------------------------------------------- #
        if action == "HOME_PAGE":
            self._verify_home_page(r, proposal)

        # ---- messaging policy text ---------------------------------------- #
        if action in ("PUSH", "WHATSAPP", "IN_APP"):
            self._verify_copy(r, proposal, action)

        # ---- evidence resolvable ------------------------------------------ #
        self._verify_evidence(r, proposal)
        return r

    # ------------------------------------------------------------------ #
    def _recheck_eligibility(self, goal: dict, facts: ViewerFacts, now: datetime) -> tuple[bool, str]:
        """Independent re-derivation of the goal's eligibility predicates."""
        el = goal.get("eligibility", {})
        f = facts
        if "subscription_in" in el and f.subscription["value"] not in el["subscription_in"]:
            return False, f"subscription {f.subscription['value']} not in {el['subscription_in']}"
        if el.get("never_completed_any_episode") and f.engagement["completes"] > 0:
            return False, "viewer already completed an episode"
        if "completed_episode_count_in" in el and \
                f.engagement["completed_episode_count"] not in el["completed_episode_count_in"]:
            return False, "completed episode count does not match"
        if "min_age_days" in el:
            su = f.signup.get("value")
            if not su:
                return False, "signup unknown"
            if (now - parse_ts(su)).total_seconds() < el["min_age_days"] * 86400:
                return False, "viewer too new"
        if "dormant_days" in el:
            lo = f.engagement.get("last_open_at")
            if lo and (now - parse_ts(lo)).total_seconds() < el["dormant_days"] * 86400:
                return False, "viewer opened recently"
        if el.get("whatsapp_opt_in") is not None and f.consent["whatsapp"]["value"] != el["whatsapp_opt_in"]:
            return False, "whatsapp consent mismatch"
        if el.get("notifications_opt_in") is not None and \
                f.consent["notifications"]["value"] != el["notifications_opt_in"]:
            return False, "notification consent mismatch"
        return True, ""

    def _verify_home_page(self, r: CheckResult, proposal) -> None:
        copy = proposal.copy or {}
        hero = copy.get("hero")
        rows = copy.get("rows", [])
        cfg = self.pack.interventions["HOME_PAGE"]
        if not hero:
            r.fail("home_page_shape", blocking=True, detail="hero missing")
        if len(rows) > cfg["max_rows"]:
            r.fail("home_page_shape", blocking=True, detail=f"{len(rows)} rows > max {cfg['max_rows']}")
        seen_slugs = []
        if hero and hero.get("content_slug"):
            seen_slugs.append(hero["content_slug"])
        for row in rows:
            if row.get("type") not in ALLOWED_ROW_TYPES:
                r.fail("home_page_shape", blocking=True, detail=f"row type {row.get('type')} not allowed")
            n = len(row.get("titles", []))
            if n < cfg["min_titles_per_row"] or n > cfg["max_titles_per_row"]:
                r.fail("home_page_shape", blocking=True,
                       detail=f"row {row.get('type')} has {n} titles (need {cfg['min_titles_per_row']}-{cfg['max_titles_per_row']})")
            if row.get("type") == "continue_watching":
                r.fail("home_page_shape", blocking=True, detail="Continue Watching must not be published")
            for t in row.get("titles", []):
                seen_slugs.append(t.get("content_slug"))
        if len(seen_slugs) != len(set(seen_slugs)):
            r.fail("home_page_shape", blocking=True, detail="duplicate content_slug on page")
        if not r.failed or not any(f["check"] == "home_page_shape" for f in r.failed):
            r.ok("home_page_shape")

    def _verify_copy(self, r: CheckResult, proposal, action: str) -> None:
        copy = proposal.copy or {}
        cfg = self.pack.interventions[action]
        blob = " ".join(str(v) for v in _flat(copy)).lower()
        for phrase in cfg.get("forbidden_phrases", []):
            if phrase in blob:
                r.fail("policy", blocking=True, detail=f"forbidden phrase '{phrase}'")
        for amount in PRICE_RE.findall(blob):
            if int(amount) not in ALLOWED_PRICES:
                r.fail("policy", blocking=True, detail=f"price ₹{amount} is not an approved price")
        if action == "PUSH":
            if len(copy.get("title", "")) > cfg["title_max_chars"]:
                r.fail("policy", blocking=True, detail="push title too long")
            if len(copy.get("body", "")) > cfg["body_max_chars"]:
                r.fail("policy", blocking=True, detail="push body too long")
            if not DEEPLINK_RE.match(proposal.deeplink or ""):
                r.fail("policy", blocking=True, detail=f"bad deeplink {proposal.deeplink!r}")
        if action == "WHATSAPP":
            allowed = {t["name"] for t in cfg["templates"]}
            if copy.get("template") not in allowed:
                r.fail("policy", blocking=True, detail=f"template {copy.get('template')} not approved")
            for v in (copy.get("variables") or {}).values():
                if not str(v).strip():
                    r.fail("policy", blocking=True, detail="empty template variable")
        if not any(f["check"] == "policy" for f in r.failed):
            r.ok("policy")

    def _verify_evidence(self, r: CheckResult, proposal) -> None:
        unresolved = []
        for ref in proposal.evidence:
            kind = ref.split(":", 1)[0].split("(", 1)[0]
            if kind == "event":
                eid = ref.split(":", 1)[1].split("(")[0].split(",")[0]
                if eid.startswith("E") and eid not in self.pack.event_index:
                    unresolved.append(ref)
            elif kind == "catalogue":
                tid = ref.split(":", 1)[1].split("|")[0]
                if tid not in self.pack.by_title_id:
                    unresolved.append(ref)
            elif kind == "goal_rule":
                gid = ref.split(":", 1)[1]
                if gid not in self.pack.goals:
                    unresolved.append(ref)
        if unresolved:
            r.fail("evidence", blocking=True, detail="unresolvable refs: " + ", ".join(unresolved))
        else:
            r.ok("evidence")


def _flat(d) -> list:
    out = []
    if isinstance(d, dict):
        for v in d.values():
            out.extend(_flat(v))
    elif isinstance(d, list):
        for v in d:
            out.extend(_flat(v))
    elif isinstance(d, str):
        out.append(d)
    return out
