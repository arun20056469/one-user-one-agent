# Final report - One User, One Agent

Data pack: `data_pack` | run directory: `sample_run` | model provider: mock (deterministic, no API key).

Every number below is generated from this run's own artefacts by `python -m src.main run --pack data_pack --out sample_run`. Re-running the command reproduces the bundle; re-running paper never changes the conclusions.

## 1. Replay at a glance

- decisions: **912** (incl. 37 stale offline-buffered triggers, 22 ageing triggers, 12 duplicate deliveries suppressed)
- actions: **698** -> {"PUSH": 382, "IN_APP": 96, "HOME_PAGE": 148, "NONE": 214, "WHATSAPP": 72}
- decisions with no action: 214 (holdout=181, stale_trigger=21, duplicate_trigger=12)
- latency: p50 **8 ms**, p95 **16 ms**, max 2013 ms; budget breaches: 0; over 60 s: 0
- model faults handled with a rule fallback: {"provider_timeout->rules": 6, "provider_parse_error->rules": 3}

## 2. Data traps detected and how they were handled

| trap | found in pack | handled by |
|---|---|---|
| duplicate events (same event_id) | 8113 | first receipt wins, duplicates dropped at load |
| duplicate trigger deliveries | 12 | idempotency key -> one action |
| offline-buffered late events (>6 h) | 7816 | received_at is the clock; stale triggers produce no action at all |
| stale triggers (>30 min old) | 37 triggers | no live action; the 21 non-holdout ones record a stale_trigger reason |
| ageing triggers (5-30 min) | 22 triggers | in-session surface only, never a push (22 decided in this run) |
| duplicate profile rows | 60 viewers | freshest `row_updated_at` wins, conflict recorded |
| profile snapshot vs purchase event | day-stale snapshot | a purchase received after the snapshot overrides it |
| dialect conflicts (app/campaign/profile) | detected per decision | app setting wins, every disagreement kept on the decision |
| instructions hidden in catalogue descriptions | 10 titles | content is not command; never copied into copy or actions |
| withdrawn titles | 3 | excluded by planner AND verifier |
| unmeasurable goal | watchlist_signal_7d | refused: success event does not exist in the stream |
| underspecified goals | weekly_active_3d, deepen_first_session_3d | interpreted, and the interpretation is an assumption on every decision |
| control events | {"title_withdrawal": 1, "goal_rule_correction": 1, "traffic_surge": 1, "datastore_slow": 1} | applied at their timestamp, response logged in control_log.jsonl |
| injected model faults | {"timeout": 1, "broken_json": 1, "hallucinate": 1} | retry once, then rule fallback; unsupported model claims rejected by the verifier |

## 3. Rules that had to hold (machine-checked)

| rule | result |
|---|---|
| holdout viewers receive zero actions | **0 leaks** (must be 0) |
| one action per trigger event_id | **0 duplicates** (must be 0) |
| no outbound message sent inside quiet hours | **0** (must be 0); quiet-hour plans are scheduled for 08:00 IST instead |
| at most one outbound message per viewer per IST day | **0 violations** (must be 0) |
| every decision records a goal or a no-action reason | **0** missing (must be 0) |
| simulator saw no agent action on a holdout viewer | **0** (must be 0) |

Run `python -m src.main audit --pack data_pack --out sample_run` for the standalone version of this table.

## 4. Goal judgement (the loop closed)

- goals judged: **900** -> 47 succeeded / 853 failed / 0 inconclusive
- goals re-judged under a corrected rule: **35** (only open goals of the corrected goal id; closed goals stay closed)
- an event only counts inside `[goal_start, deadline]`; late arrivals cannot borrow success from outside the window
- holdout viewers are scored by the SAME planner in shadow mode, so both arms are judged on identical criteria

## 5. Impact (honest measurement)

| population | agent | holdout | difference | 95% CI |
|---|---|---|---|---|
| primary: replay-day visitors (ITT) | 37/719 (5.15%) | 10/181 (5.52%) | **-0.38 pp** | [-4.08 pp, +3.32 pp] |
| secondary: every randomized viewer | 37/4000 | 10/1000 | -0.08 pp | [-0.76 pp, +0.61 pp] |
| naive (biased, for contrast): served vs rest | 36/698 | 11/202 | -0.29 pp | [-3.82 pp, +3.25 pp] |

**Conclusion: no detectable effect yet.** The confidence interval on the arm difference crosses zero, so the correct statement is "we cannot show the agent moved the goal here", not "the agent works".

Naive vs honest on our own run: the served-vs-rest gap is -0.29 pp against the arm difference of -0.38 pp (gap +0.09 pp). The pilot report shows how large that gap can get from the same arithmetic: its headline +8.94 pp against an honest recomputation of +3.44 pp (gap +5.50 pp, 95% CI [+0.57 pp, +6.31 pp]). The naive number moves with who the agent chooses to serve, so it is descriptive, not causal - on either run, only the arm comparison answers "did the agent move the goal". That is why the assignment's "prove impact" step is answered with an experiment rather than a dashboard.

Pilot report detail: headline (+8.94 pp, served vs everyone else (the report's number)) vs randomized recomputation +3.44 pp (95% CI [+0.57 pp, +6.31 pp]), from n=3994/1006 randomized viewers.

## 6. Where to look next

- `decisions.jsonl` - one line per trigger: facts used, goal, dialects and conflicts, action, evidence, checks, latency
- `actions.jsonl` - what was actually published or scheduled
- `outcomes.jsonl` - the judgement for every goal window
- `control_log.jsonl` - how each trap/control event was handled live
- `viewer_state/viewers.jsonl` - the durable per-viewer state at the end
- `impact_report.md` - full impact analysis with per-goal breakdown
- `latency_report.json` - measured latency distribution and budgets
- `KNOWN_LIMITATIONS.md` - what is synthetic, what is weak, what we would fix with the real pack
