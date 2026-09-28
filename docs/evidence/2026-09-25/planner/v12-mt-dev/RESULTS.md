# Results -- s1m m-dev (reps=3)

## Verdict table (vs anchor)

| Cell | Anchor | Passed/Total | Ratio (95% CI) | sign-test p | Cost ratio | Quality delta | Gate | Verdict |
|---|---|---|---|---|---|---|---|---|
| orch-default | plain | 3/3 | 0.44 [0.35, 0.50] | 0.25 | 0.79 | +0 | green | **screen** |
  - orch-default vs `plain-sonnet` (secondary): ratio=0.86 [0.67, 1.00]
| orch-default-opus | plain-opus | 3/3 | 0.94 [0.89, 1.01] | 1 | 1.48 | +0 | green | **screen** |
  - orch-default-opus vs `plain-sonnet` (secondary): ratio=0.78 [0.62, 0.88]
| orch-planner-opus | plain-opus | 3/3 | 1.04 [1.00, 1.09] | 0.25 | 1.05 | +0 | green | **screen** |
| orch-planner-v12-opus | plain-opus | 3/3 | 1.01 [0.94, 1.06] | 1 | 1.00 | +0 | green | **screen** |
| orch-planner-v12-fable | plain | 3/3 | 0.44 [0.33, 0.56] | 0.25 | 0.72 | +0 | green | **screen** |

## Evidence limits

- bootstrap_ci: 2000 resamples, seed=20260919
- confirmed requires >= 3 reps, >= 8 paired passing tasks, and split=holdout
- external_harness_exec_time_may_still_include_its_own_startup
- models_not_necessarily_matched_across_campaigns_or_harnesses
- models_not_necessarily_matched_across_harnesses
- n_tasks=3
- non_contemporaneous_runs_across_campaigns
- single_repetition_per_task_per_harness

