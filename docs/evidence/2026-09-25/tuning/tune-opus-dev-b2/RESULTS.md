# Results -- s1 dev (reps=1)

## Verdict table (vs anchor)

| Cell | Anchor | Passed/Total | Ratio (95% CI) | sign-test p | Cost ratio | Quality delta | Gate | Verdict |
|---|---|---|---|---|---|---|---|---|
| orch-default-opus | plain-opus | 12/12 | 0.82 [0.71, 0.94] | 0.146 | 0.98 | +0 | green | **screen** |
  - orch-default-opus vs `plain-sonnet` (secondary): ratio=0.78 [0.62, 0.94]
| orch-haiku-opus | plain-opus | 12/12 | 1.24 [1.03, 1.47] | 0.146 | 0.46 | +0 | green | **screen** |
| orch-haiku-low-opus | plain-opus | 12/12 | 1.17 [0.97, 1.40] | 0.3877 | 0.44 | +0 | green | **screen** |
| orch-opuslow-opus | plain-opus | 12/12 | 0.97 [0.79, 1.23] | 1 | 0.95 | +0 | green | **screen** |
| orch-sonnetlow-opus | plain-opus | 12/12 | 0.83 [0.71, 0.99] | 0.03857 | 1.00 | +0 | green | **screen** |

## Evidence limits

- bootstrap_ci: 2000 resamples, seed=20260919
- confirmed requires >= 3 reps, >= 8 paired passing tasks, and split=holdout
- external_harness_exec_time_may_still_include_its_own_startup
- models_not_necessarily_matched_across_campaigns_or_harnesses
- models_not_necessarily_matched_across_harnesses
- n_tasks=12
- non_contemporaneous_runs_across_campaigns
- single_repetition_per_task_per_harness

