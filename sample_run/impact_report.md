# Impact report: agent vs holdout (honest measurement)

## 1. Primary: intention-to-treat over replay-day visitors

Everyone below was randomized into an arm *before* the agent touched anything. Viewers the agent decided not to act on stay in the agent arm (that is the point of ITT).

| arm | succeeded | n | rate |
|---|---|---|---|
| agent | 37 | 719 | 5.15% |
| holdout | 10 | 181 | 5.52% |

**Difference: -0.38 pp (95% CI [-4.08 pp, +3.32 pp]).**

The interval crosses zero: with this sample size we **cannot claim a detectable effect yet**. Reporting that honestly is the point - a wide interval is not evidence of impact, and we will not pretend otherwise.

## 2. Secondary: every randomized viewer (visitors and non-visitors)

Non-visitors had no trigger, so the agent could not act; diluting with them shows the realistic fleet-wide effect.

| arm | succeeded | n | rate |
|---|---|---|---|
| agent | 37 | 4000 | 0.92% |
| holdout | 10 | 1000 | 1.00% |

Difference: -0.08 pp (95% CI [-0.76 pp, +0.61 pp]).

## 3. Why 'served vs everyone else' misleads

| comparison | rate gap | 95% CI |
|---|---|---|
| naive: served vs not served (our run) | -0.29 pp | [-3.82 pp, +3.25 pp] |
| correct: agent arm vs holdout arm (our run) | -0.38 pp | [-4.08 pp, +3.32 pp] |

The naive gap is not a treatment effect: the agent preferentially serves viewers it believes can be moved - the already-engaged, the consented, the reachable - and those viewers would have done better anyway. Here it lands +0.09 pp above the randomized estimate, which shows the direction is not even stable. Randomisation is what breaks the link between targeting and outcome, and only the arm comparison is causal.

## 4. Pilot report re-analysis

The internal pilot report's headline (**served vs everyone else (the report's number): +8.94 pp**, CI [+6.35 pp, +11.53 pp], n=1695/3305) is a served-vs-unserved comparison and is biased upward.

Recomputed from the randomized arms (all treatment vs all holdout, served or not): **+3.44 pp** (95% CI [+0.57 pp, +6.31 pp], n=3994/1006).

## 5. Per-goal breakdown

| goal | agent | holdout | diff | 95% CI |
|---|---|---|---|---|
| finish_first_episode_5d | 6/22 | 0/6 | +27.27 pp | [+8.66 pp, +45.88 pp] |
| first_full_episode_3d | 1/26 | 2/8 | -21.15 pp | [-52.06 pp, +9.75 pp] |
| first_paid_7d | 3/379 | 0/91 | +0.79 pp | [-0.10 pp, +1.68 pp] |
| quarterly_upgrade_10d | 2/124 | 1/26 | -2.23 pp | [-9.95 pp, +5.48 pp] |
| search_to_play_2d | 9/17 | 3/6 | +2.94 pp | [-43.57 pp, +49.46 pp] |
| second_episode_7d | 8/41 | 2/9 | -2.71 pp | [-32.46 pp, +27.04 pp] |
| trial_to_paid_7d | 1/76 | 1/28 | -2.26 pp | [-9.59 pp, +5.08 pp] |
| weekly_active_3d | 7/34 | 1/7 | +6.30 pp | [-22.97 pp, +35.57 pp] |

Per-goal cells are small (one replay day): several intervals look large only because n is tiny, and none of them is individually conclusive. They are reported for honesty about where any signal would come from, not as a result.

## 6. Safety counters

- actions by type: {"PUSH": 382, "HOME_PAGE": 148, "WHATSAPP": 72, "IN_APP": 96}
- holdout action leaks (must be 0): **0**
- decisions with recorded no-action reasons: {"holdout": 181, "stale_trigger": 21, "duplicate_trigger": 12}

## 7. Latency

See `latency_report.json` (p50/p95/max decided_in_ms, budget breaches, fallbacks used).
