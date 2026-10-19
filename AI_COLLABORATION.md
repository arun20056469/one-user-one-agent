# AI Collaboration

The build was done with an AI coding assistant (Claude Code / this agent)
driving most of the typing, under a simple working agreement:

> **AI proposes; the test suite and the replay dispose.** No AI-written line was
> treated as correct because it looked right. Claims were checked against the
> test suite, the delivered run, and the assignment's own rules — and several
> plausible-looking AI outputs were rejected.

This log is intentionally specific. Each entry is a real change in this repo,
with the symptom that exposed it.

---

## 1. The trigger was not treated as evidence (dialect broke)

**AI produced.** `Agent.decide` resolved facts from the events the harness
passed in (`extra_events`), the viewer's history, and the profile — but *not*
from the trigger that woke the agent.

**Why it was wrong.** The trigger *is* an `app_open` event carrying the current
`app_dialect`. When a viewer had no other recent event, `facts.resolve` saw no
app-setting dialect and fell back to the profile (or to nothing), so content
could be chosen in the wrong dialect for the one event we were actually
responding to.

**How it was caught.** A unit test asserting the dialect came from the app
setting on the triggering event failed.

**Correction.** `agent.decide` now folds the trigger itself into the evidence
list, de-duplicated by `event_id` (`evidence_events`), so the app-setting
precedence always sees it. This also required importing `parse_ts` correctly in
the module.

---

## 2. Injected faults that could never fire

**AI produced.** `model_faults` windows at fixed clock times (e.g. 20:40–20:45),
and a "traffic surge" window at the evening peak.

**Why it was wrong — twice.** First, one fault window sat *inside* the surge
window. During a surge the agent correctly skips optional model calls, so the
fault could never be observed — the timeout/fallback path was silently
untested. Second, after moving it, the fixed 5-minute windows only worked
because the full pack has ~900 triggers; at the smaller test/fixture scale (60
triggers) **no trigger landed inside any fault window**, so the fallback path
was again never exercised.

**How it was caught.** An end-to-end test asserting "a provider fault must fall
back to the rule plan" failed on the fixture pack, and the `final_report.md`
line *"model faults handled with a rule fallback: []"* stayed empty even on the
full run — the empty list was the tell.

**Correction.** Faults are now **anchored to real triggers**: the generator
picks deterministic, freshly-arriving, agent-arm triggers *outside* the surge
window and places each fault window on one of them. Coverage is now guaranteed
at every pack size. This immediately surfaced a second bug (see #3).

---

## 3. A timezone bug that made the new fault anchors miss

**AI produced.** The trigger-anchoring code parsed `received_at` into a *naive*
`datetime` and then passed it through the module's `z()` helper.

**Why it was wrong.** `z()` treats a naive datetime as machine-local time and
converts to UTC. On the dev machine (IST, +5:30) every anchor was shifted by
5 h 30 m, so the fault windows no longer matched any trigger — the fault file
*looked* right but hit nothing.

**How it was caught.** A direct check (`is the anchor in triggers.jsonl?`)
returned `MISS` for all six anchors, and the run again showed zero fallbacks.

**Correction.** Anchors are parsed as timezone-**aware** UTC
(`.replace(tzinfo=timezone.utc)`). The check now prints `HIT` for every anchor,
and the delivered run reports `{"provider_timeout->rules": 6,
"provider_parse_error->rules": 3}` plus 7 rejected hallucinated claims.

---

## 4. The ageing-trigger rule leaked a scheduled message

**AI produced.** A downgrade for 5–30-minute-old ("ageing") triggers that
replaced an outbound action with the in-session home page.

**Why it was wrong.** It only rewrote `proposal.action` when it was a message;
it did not clear `schedule_for`. A quiet-hours push/WhatsApp had already been
*planned* as a deferred send, so a stale trigger could still leave a message
scheduled for 08:00 IST — an outbound message the policy said not to send.

**How it was caught.** A test that walks every persisted action caught a
`SCHEDULED` WHATSAPP action on a viewer with an ageing trigger.

**Correction.** The downgrade now routes through `_home_only`, which clears any
pending schedule and annotates the decision with `downgraded_trigger`; it covers
the scheduled case explicitly, not just the immediate one.

---

## 5. The report told a story the data did not support

**AI produced.** `report.py` asserted that the naive served-vs-rest number is
*positive and flattering* versus the honest arm difference.

**Why it was wrong.** On the delivered run the naive number is **−0.29 pp**
and the ITT difference is **−0.38 pp** — the naive comparison is actually
*lower*. The template would have printed a false claim next to real numbers.
(The same review found the stale/ageing counts in the report were hardcoded
rather than read from the pack, and a `_pp` / `_fmt_pp` `NameError` in the
impact renderer.)

**How it was caught.** Reading the generated `final_report.md` against
`impact_summary.json`, and a test that asserts the report contains the ITT
figures rather than a fixed conclusion sentence.

**Correction.** The report now states the **direction from the data**
("lands +0.09 pp above / below the randomized estimate") and sources every trap
count from the pack. The run's conclusion sentence is generated
("no detectable effect yet" when the interval crosses zero), and the test
asserts the *presence of the numbers*, not a hardcoded phrase.

---

## 6. Re-running the one command silently produced an empty run

**AI produced.** A replay harness that opened `viewer_state/state.db` in the run
directory with `CREATE TABLE IF NOT EXISTS`.

**Why it was wrong.** Because the ledger survives, a second `run` into the same
`sample_run/` saw every `event_id` as already decided: all 912 decisions became
`SUPPRESSED`, **zero actions**, and the latency report read `p50 = 0 ms`. It
"worked" exactly once — fatal for an assignment whose headline is a one-command
replay an evaluator will re-run.

**How it was caught.** Running the same command twice by hand and watching the
action counts collapse to zero.

**Correction.** Replay now resets its own artefacts (state DB + generated
files) before it starts, so `run` is idempotent. A regression test replays the
fixture twice into the same directory and asserts identical
decisions/actions/status counts.

---

## 7. Smaller corrections

- **Holdout decisions lost their provenance.** The holdout shortcut path called
  `_finish` without `evidence`/`conflicts`, so control-viewer decisions were
  under-documented. Fixed to pass the resolved facts.
- **Windows file locks.** The assistant's tests left SQLite handles open, so
  `TemporaryDirectory` cleanup raised `ResourceWarning`/`PermissionError`.
  Fixed with `StateStore.close()` + context manager + `__del__`,
  `OutcomeJudge.close()`, explicit closes, and
  `TemporaryDirectory(ignore_cleanup_errors=True)`; pack CSV handles are closed
  in a `_read_csv` helper.
- **Frequency cap and who gets messaged.** An early draft let a viewer's
  "cap" be consumed by a *scheduled* (quiet-hours) message, blocking a
  legitimate in-session action on the same day. The cap now applies on the
  **send day** for scheduled messages, and the verifier checks both the
  immediate and the scheduled path.
- **Fixed-conclusion test.** The first end-to-end test asserted the literal
  string "no detectable effect yet". On the fixture pack the interval excludes
  zero, so the assertion was testing our template, not our behaviour. It now
  asserts the report contains the ITT rate, difference and CI.

---

## What this says about the collaboration

The assistant was genuinely useful: it wrote the bulk of the code, generated the
synthetic pack, and suggested the module boundaries. But nearly every subtle
defect above was a *plausible-looking* AI output that only the tests, the replay
artefacts, or a second pair of human eyes exposed. The pattern we enforced was:

1. every claim has to be reproducible by a command, not by prose;
2. the generated `final_report.md` is machine-derived, so a "nice story" cannot
   survive a contradicting number;
3. when a test failed, we fixed the behaviour — we did not weaken the test
   (except where the test itself encoded a wrong assumption, as in #7).
