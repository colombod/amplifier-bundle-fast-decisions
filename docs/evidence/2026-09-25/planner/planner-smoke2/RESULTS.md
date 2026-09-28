# Results -- s1m m-dev (reps=1)

## Verdict table (vs anchor)

| Cell | Anchor | Passed/Total | Ratio (95% CI) | sign-test p | Cost ratio | Quality delta | Gate | Verdict |
|---|---|---|---|---|---|---|---|---|
| orch-default-opus | plain-opus | 3/3 | 1.07 [0.95, 1.23] | 1 | 1.22 | +0 | green | **screen** |
  - orch-default-opus vs `plain-sonnet` (secondary): ratio=0.99 [0.89, 1.07]
| orch-planner-opus | plain-opus | 3/3 | 1.01 [0.98, 1.07] | 1 | 1.41 | +0 | green | **screen** |
| orch-planner-fable | plain | 3/3 | 0.50 [0.42, 0.65] | 0.25 | 0.61 | +0 | green | **screen** |

## Evidence limits

- bootstrap_ci: 2000 resamples, seed=20260919
- confirmed requires >= 3 reps, >= 8 paired passing tasks, and split=holdout
- external_harness_exec_time_may_still_include_its_own_startup
- models_not_necessarily_matched_across_campaigns_or_harnesses
- models_not_necessarily_matched_across_harnesses
- n_tasks=3
- non_contemporaneous_runs_across_campaigns
- single_repetition_per_task_per_harness

