# Architecture

One agent per viewer. The design goal is a **trustworthy decide → act →
measure loop**, not a recommender. The deterministic core makes every decision;
the model is an optional advisor that can be ignored, rejected, or unavailable.

## 1. Component map

| module | job |
|---|---|
| `src/pack.py` | read-only pack loader. Dedupes events by `event_id` (first receipt wins), indexes them per viewer in `received_at` order, flags prompt-injection descriptions, exposes availability/withdrawal checks used by **both** planner and verifier. |
| `src/facts.py` | fact resolver. Produces facts with `source`, `observed_at`, `freshness_s`, `stale`; resolves conflicts by explicit precedence; records every conflict. |
| `src/goals.py` | goal planner. Evaluates all goals, refuses unmeasurable ones, keeps the active goal while it is still eligible, picks the highest-priority eligible goal or abstains. |
| `src/interventions.py` | intervention planner. Home page builder (rules only) and push/WhatsApp/in-app choice; "nothing" is a first-class outcome with a reason. |
| `src/provider.py` | model provider interface. Default **mock/replay** provider reproduces the pack's `model_faults.jsonl`; a real provider can be swapped in. |
| `src/verifier.py` | independent checks that can block or downgrade a decision. Never trusts model text. |
| `src/agent.py` | one agent per viewer: the 8-step decide pipeline, budget accounting, publish and persist. |
| `src/state.py` | durable per-viewer SQLite store: facts, one active goal, learnings, caps, decisions, actions, idempotency ledger. |
| `src/harness.py` | replay: feeds triggers in `deliver_at` order, interleaves live and control events, injects faults and slowness, measures latency, writes the run bundle. |
| `src/judge.py` | outcome judge: judges every goal window from attributable events, applies rule corrections to affected open goals only, writes learnings back. |
| `src/impact.py` | impact reporter: ITT, sensitivity, naive contrast, per-goal, pilot re-analysis, 95% CIs. |
| `src/report.py` | `final_report.md`, generated from the run's own artefacts. |
| `src/main.py` | CLI: `run`, `replay`, `simulate`, `judge`, `impact`, `audit`. |

## 2. Ownership

There is **exactly one agent object per viewer**, and it owns the single active
goal. No two components chase different goals for the same user:

- The goal planner enforces "one active goal per viewer" and *keeps* the
  existing goal while it remains eligible instead of flip-flopping on every
  trigger.
- The goal is revised only when the situation changes (a purchase arrives, a
  new success signal appears, a rule is corrected) — never on a schedule.
- State lives in `viewers.active_goal_json`; the judge and the live-event
  handler both mutate the *same* record, so the agent's belief and its history
  cannot diverge.

## 3. Memory and provenance

State is durable (SQLite) with three layers:

1. **Facts** — each with source and freshness. Precedence rules are explicit
   (never "biggest source wins"):

   | fact | precedence |
   |---|---|
   | subscription | purchase events (in `received_at` order) → profile snapshot → unknown. A purchase received after the day-stale snapshot overrides it. |
   | consent | a later `opt_out` event overrides the profile opt-in; freshest consent event per channel wins; profile is the default (deny if absent). |
   | dialect | the app setting on the freshest received event (what the app will actually request) → campaign deeplink → profile preference. All disagreements are kept as `conflicts`. |
   | signup | `signup_completed` event → profile → earliest event (provisional, recorded as an assumption). |
   | engagement | counted from **deduplicated** events with `received_at <= now`. |

2. **Active goal** — id, deadline, success signal, rule version, evidence,
   assumptions, status, learnings.

3. **Ledger** — `decisions`, `actions`, and `triggers_seen` tables, so any moment
   can be audited: what the agent knew, decided and observed.

`viewer_state/viewers.jsonl` is a flat export of the store at the end of the run.

## 4. Trust boundaries

- **Pack data vs instruction.** Text inside catalogue descriptions (and any
  other field) is *content*, never a command. `pack.INJECTION_PATTERNS` flags
  descriptions that try to instruct the agent; they are logged and never copied
  into copy or actions.
- **Model vs decision.** The model may only *propose*. Its output is not
  evidence and not approval. The verifier re-derives eligibility independently;
  an unsupported model claim is rejected and recorded as `model_claim` failure.
- **Planner vs verifier.** The verifier re-checks the planner's own choice
  (dialect, availability, consent, cap, policy, home-page shape, evidence). Any
  failed blocking check downgrades (scheduled message → in-session) or blocks
  (action `NONE`, status `BLOCKED`).
- **Holdout.** Holdout viewers never receive any action, and the harness/simulator
  assert zero leaks.

## 5. Decision pipeline (`src/agent.py`)

For each trigger, in order: idempotency → staleness → facts → goal →
intervention plan → model hint (advisory, budget/fault-gated) → verify →
publish. Constants: `STALE_SKIP_S = 1800` (skip), `STALE_DOWNGRADE_S = 300`
(in-session only), `max_model_calls = 8`, `max_tool_calls = 20`, latency target
60 s and hard fail 5 min.

`published_at = deliver_at + decided_in_ms`, where `decided_in_ms` is measured
with `time.perf_counter`. Every decision records `model_calls` and `tool_calls`.

## 6. Latency and load

- The replay processes triggers in `deliver_at` order and records real measured
  processing time per decision.
- **Datastore-slow window:** writes are delayed; reads fall back to the
  in-memory snapshot (`state.cache`), and each decision records whether it used
  the fallback.
- **Traffic surge:** optional model calls are skipped (`model_status = SKIPPED`,
  `mode = surge`); the deterministic rule path still publishes inside budget.
- Delivered run: p50 8 ms, p95 18 ms, max 2013 ms (a real provider-timeout
  retry), 0 budget breaches.

**Scaling note.** The pipeline is single-process and ordered (a faithful replay
of one day). For 1,000 opens in the same minute the same design applies —
partition by viewer, keep per-decision work O(facts), skip model calls under
load — but the current harness does not model a parallel fleet. This is stated
in `KNOWN_LIMITATIONS.md`.

## 7. Failure recovery

| failure | response |
|---|---|
| duplicate trigger `event_id` | idempotency ledger → one decision, zero extra actions |
| offline-buffered / stale open | no live action; recorded reason; no push |
| ageing open (5–30 min) | in-session home page only; any scheduled message is cleared |
| model timeout | retry once within budget, then rule fallback (`provider_timeout->rules`) |
| malformed model JSON | parse safely, retry once, then rule fallback |
| unsupported model claim | verifier rejects it; decision proceeds from rules |
| data-store or state-store failure | `_degraded`: no action, status `FALLBACK`, reason recorded |
| withdrawn title | excluded by planner and verifier; pending actions containing it cancelled |
| goal rule corrected | new rule applied to the affected **open** goals only; closed goals stay closed; no mass reprocessing |
| early success signal (e.g. purchase) | goal marked `success_observed`; pending messages that no longer make sense are cancelled; planning restarts from refreshed state |

## 8. Closing the loop and measuring

- **Judge:** for each goal assignment, count only events whose `occurred_at`
  falls inside `[goal_start, deadline]`; late arrivals cannot borrow success
  from outside the window. If the observation horizon has not reached the
  deadline, the result is `INCONCLUSIVE`, not `FAILED`.
- **Fair arms:** holdout viewers get no action, but the judge runs the **same
  planner** on their trigger in shadow mode, so both arms are scored on
  identical criteria.
- **ITT:** compare all agent-arm vs all holdout-arm triggered viewers,
  *including viewers the agent chose not to act on*. Report the difference with
  a 95% Wald CI, plus a fully-randomized sensitivity check over all arm members.
- **Naive contrast:** the served-vs-rest comparison is reported next to ITT on
  our own run to show how far it can move — and the pilot report's biased
  headline is recomputed from the randomized arms.

## 9. Why this answers "did the agent cause it?"

Only randomization breaks the link between *who the agent chooses to serve* and
*who would have converted anyway*. "Served vs not served" is selection-biased:
the agent serves the already-engaged, the consented, the reachable — precisely
the viewers who would have done better regardless. The arm difference is the
causal quantity; everything else is descriptive.
