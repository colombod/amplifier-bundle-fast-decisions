# Results -- s1 dev (reps=2)

## Verdict table (vs anchor)

| Cell | Anchor | Passed/Total | Ratio (95% CI) | sign-test p | Cost ratio | Quality delta | Gate | Verdict |
|---|---|---|---|---|---|---|---|---|
| orch-default | plain | 12/12 | 0.51 [0.40, 0.69] | 0.03857 | 0.54 | +0 | green | **screen** |
  - orch-default vs `plain-sonnet` (secondary): ratio=1.05 [0.92, 1.30]
| orch-haiku-fable | plain | 11/12 | 0.81 [0.67, 0.99] | 0.3877 | 0.36 | -1 | green | **screen** |
| orch-fablelow | plain | 12/12 | 0.93 [0.81, 1.08] | 0.7744 | 0.95 | +0 | green | **screen** |

## Evidence limits

- bootstrap_ci: 2000 resamples, seed=20260919
- confirmed requires >= 3 reps, >= 8 paired passing tasks, and split=holdout
- external_harness_exec_time_may_still_include_its_own_startup
- models_not_necessarily_matched_across_campaigns_or_harnesses
- models_not_necessarily_matched_across_harnesses
- n_tasks=12
- non_contemporaneous_runs_across_campaigns
- single_repetition_per_task_per_harness

