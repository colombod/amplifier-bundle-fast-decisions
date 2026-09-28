# Evidence: re-validation of the corrected default (2026-09-25)

Summaries only: no prompts, no patches, no final assistant messages, no secrets; home paths shown as `~`.
`MANIFEST.json` holds a SHA-256 for every file here.

| Path | What | Used for |
|---|---|---|
| `s1/reval-s1-dev/` | S1 dev, 12 tasks x 2 reps: plain, plain-sonnet, orch-default (Fable host), plain-opus, orch-default-opus; per-run results, comparisons, gates, `mechanism-summary.json` | README / report tuning-task numbers (0.559x/0.484x Fable; 0.825x/0.895x Opus) |
| `s1/reval-s1-dev-batch2/` | Replicate, 1 rep (launched as a reversed-order check; ran in declared order -- see NOTE.md) | 0.574x/0.460x Fable; 0.757x/0.822x Opus |
| `s1/prereg2-s1-holdout/` | Holdout (criteria per STUDY-DESIGN §8; see Limits on the preregistration timestamp), 8 reused tasks x 3 reps, same five cells, fixed order, with `PREREGISTRATION.md` | Superseded by `holdout2`. Fable 0.42x [0.35-0.50] / 0.50x, 8/8, p = 0.0078 (passed all 6 criteria on a reused holdout; not a confirmation); Opus 1.01x / 0.98x, no measurable gain |
| `holdout2/` | Fresh preregistered holdout: 12 never-run tasks (4 longer multi-file) x 3 reps, single-turn, cell order shuffled per rep; seven cells (the five above plus `orch-haiku-shaped-opus` / `-fable`); `PREREGISTRATION.md`, `RESULTS.md`, independent `VERIFICATION.md` | Headline: Fable 0.52x [0.42-0.65] / 0.49x, 11/12, p = 0.0063, 36/36, **confirmed**; Opus 0.80x [0.67-0.92] / 0.97x, 10/12, p = 0.039, 36/36, **confirmed**; Haiku preset disqualified (§8 critical failure on `repair_roman_to_int`, 4 of 6 runs) |
| `multiturn/mt-dev/` | Multi-turn suite `s1m`, split `m-dev`: one session with 4 turns, 3 scenarios x 3 reps, same seven cells, shuffled order | Screen: Fable 0.45x / 0.66-0.73x cost; Opus 0.89x / 1.34-1.41x cost (pipeline / per-task estimators) |
| `tuning/` | S1 dev tuning screens: `tune-opus-dev` (1 rep, comparisons and runs only), `tune-opus-dev-b2` (1 rep), `tune-fable-dev` (2 reps), `shape-opus-dev` (2 reps): easy-tier effort, Opus-low, Haiku, Haiku with easy-turn shaping | RESULTS update: lower effort / Opus-low did not beat Sonnet; Haiku ~1.2x slower on the Opus host; shaping not enough |
| `difficulty/difficulty-report-*.json` | Judge re-test on the same 110-item dataset (ids, labels, p(complex), latency; no texts) | "each landed within a point or two" |
| `routing-cost-check.json` | Provider cost of every live call vs Sonnet 5 / Opus 5.5 list prices (identifies the served model) | RESULTS follow-ups |
| `surface-checks.json` | One easy prompt each in the CLI, amplifier-runtime serve and the TUI | README/report surface claims |
| `startup/startup-profile.json` | cProfile of one trivial request, top items, plus two unprofiled runs | Report Fig. 8 |

Candidate commits (frozen snapshots; see each `manifest.json`): `52e0d29` for `s1/`; `99c3562` for `holdout2/`;
`4460d90` for `multiturn/mt-dev/`; `b1012e2` for `tuning/tune-fable-dev/` and `tune-opus-dev-b2/`, `c778a47` for
`tuning/shape-opus-dev/` (`tuning/tune-opus-dev/` has no manifest). `MANIFEST.json` predates `holdout2/`,
`multiturn/` and `tuning/` and does not cover them.

## Recomputing

- Time/cost ratio: geometric mean over tasks of (median candidate / median anchor) using `runs/**/result.json` (holdout2/, tuning/, multiturn/: `runs/<experiment>/<task>-<harness>-a<N>.json`, flattened for Windows path limits)
  `exec_time_ms` and `cost_usd`, excluding `infrastructure_failure` runs. The pipeline's own `results.json` uses a
  different cost estimator (holdout Fable: 0.54x vs 0.50x here).
- Sign test: two-sided binomial over per-task faster/slower. CI: bootstrap over tasks.
- Mechanism: `mechanism-summary.json` counts `difficulty_judged` and `model_routed` reasons in the per-run event files.

## Limits

- One Apple-silicon Mac, provider `anthropic`, cheap model `claude-sonnet-5`.
- Holdout reuse (`s1/` only): `prereg2-s1-holdout` used the same 8 task IDs as the 2026-09-24 `prereg-s1-holdout`,
  and the corrected default was designed after that holdout's results were seen, so it is not a confirmation. It is
  superseded by `holdout2`, whose preregistration (commit 99c3562) was pushed at 13:42:23 UTC, 27 s before the first
  run (13:42:50 UTC); none of its 12 task names appear in an earlier campaign.
- Scope of `holdout2`: single-turn sessions only; 4 of its 12 tasks are longer. Multi-turn (`multiturn/mt-dev/`) and
  `tuning/` are dev screens, not confirmations.
- Cell order (`s1/` only): the three `s1/` batches ran cells in the fixed order plain → plain-sonnet → orch-default →
  plain-opus → orch-default-opus, back to back (~10 min apart). Runs share the provider's prompt cache across cells
  (native events show large `cache_read` on the first call of a run), so order can bias time and cost. `holdout2/`,
  `multiturn/` and `tuning/` ran with a seeded per-rep shuffle (manifest `invocation.cell_order`); on holdout2 the
  per-rep ratios show no trend with position.
- `holdout2` preregistration: criterion 5 was restated more narrowly (protected-file violations only) than
  STUDY-DESIGN §8; the standing §8 rule is used, which disqualifies the Haiku cells (see `holdout2/VERIFICATION.md`).
- One raw run failed on infrastructure and was retried per R6: `reval-s1-dev` plain-opus r2 `edit_dequeue_validation`
  attempt a1 (`infrastructure_failure: true`, `no_result_json`); the counted result is attempt a2 (passed).
- `s1/` `result.json` files do not record which model served each call; only per-batch counts (e.g. Opus holdout: 18
  of 24 turns judged easy and started on Sonnet). `holdout2/`, `multiturn/` and `tuning/` record `routing` (turn
  decisions, per-call requested model/effort, provider-reported model and cost under `native_served_model_counts` /
  `native_cost_usd_by_model`). The comparison files' `served_model_counts` show the host as `provider-default`
  because they count the requested model.
- Provider-reported cost is an estimate, not billing.
- `s1/prereg2-s1-holdout/PREREGISTRATION.md` states 03:35 UTC; the first holdout run started at 03:29:22 UTC. The local original was created at
  03:29:08 UTC (filesystem birth time, unchanged since); it was committed together with the results. The decision
  criteria are the standing rule in evals/STUDY-DESIGN.md §8, committed before this study.
- `routing-cost-check.json` and `surface-checks.json` come from live sessions on the author's machine (event
  summaries only).
