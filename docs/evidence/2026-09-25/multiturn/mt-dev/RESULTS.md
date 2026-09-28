# Results -- s1m m-dev (reps=3)

## Verdict table (vs anchor)

| Cell | Anchor | Passed/Total | Ratio (95% CI) | sign-test p | Cost ratio | Quality delta | Gate | Verdict |
|---|---|---|---|---|---|---|---|---|
| orch-default | plain | 3/3 | 0.45 [0.33, 0.61] | 0.25 | 0.66 | +0 | green | **screen** |
  - orch-default vs `plain-sonnet` (secondary): ratio=0.81 [0.62, 1.00]
| orch-default-opus | plain-opus | 3/3 | 0.89 [0.82, 0.98] | 0.25 | 1.34 | +0 | green | **screen** |
  - orch-default-opus vs `plain-sonnet` (secondary): ratio=0.86 [0.69, 0.97]
| orch-haiku-shaped-opus | plain-opus | 2/3 | 1.19 [1.18, 1.19] | 0.5 | 0.72 | -1 | green | **screen** |
| orch-haiku-shaped-fable | plain | 3/3 | 0.78 [0.57, 1.01] | 1 | 0.51 | +0 | green | **screen** |

## Evidence limits

- bootstrap_ci: 2000 resamples, seed=20260919
- confirmed requires >= 3 reps, >= 8 paired passing tasks, and split=holdout
- external_harness_exec_time_may_still_include_its_own_startup
- models_not_necessarily_matched_across_campaigns_or_harnesses
- models_not_necessarily_matched_across_harnesses
- n_tasks=3
- non_contemporaneous_runs_across_campaigns
- single_repetition_per_task_per_harness

