# Results -- s1m m-dev (reps=3)

## Verdict table (vs anchor)

| Cell | Anchor | Passed/Total | Ratio (95% CI) | sign-test p | Cost ratio | Quality delta | Gate | Verdict |
|---|---|---|---|---|---|---|---|---|
| orch-default | plain | 3/3 | 0.39 [0.29, 0.54] | 0.25 | 0.61 | +0 | green | **screen** |
  - orch-default vs `plain-sonnet` (secondary): ratio=0.83 [0.74, 0.94]
| orch-default-opus | plain-opus | 3/3 | 0.98 [0.94, 1.07] | 1 | 1.51 | +0 | green | **screen** |
  - orch-default-opus vs `plain-sonnet` (secondary): ratio=0.91 [0.74, 1.23]
| orch-planner-opus | plain-opus | 3/3 | 0.99 [0.96, 1.02] | 1 | 1.00 | +0 | green | **screen** |
| orch-planner-fable | plain | 3/3 | 0.56 [0.43, 0.71] | 0.25 | 0.81 | +0 | green | **screen** |

## Evidence limits

- bootstrap_ci: 2000 resamples, seed=20260919
- confirmed requires >= 3 reps, >= 8 paired passing tasks, and split=holdout
- external_harness_exec_time_may_still_include_its_own_startup
- models_not_necessarily_matched_across_campaigns_or_harnesses
- models_not_necessarily_matched_across_harnesses
- n_tasks=3
- non_contemporaneous_runs_across_campaigns
- single_repetition_per_task_per_harness

