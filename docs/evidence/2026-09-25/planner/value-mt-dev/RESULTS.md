# Results -- s1m m-dev (reps=3)

## Verdict table (vs anchor)

| Cell | Anchor | Passed/Total | Ratio (95% CI) | sign-test p | Cost ratio | Quality delta | Gate | Verdict |
|---|---|---|---|---|---|---|---|---|
| orch-default | plain | 3/3 | 0.40 [0.32, 0.45] | 0.25 | 0.65 | +0 | green | **screen** |
  - orch-default vs `plain-sonnet` (secondary): ratio=0.86 [0.76, 0.93]
| orch-default-opus | plain-opus | 3/3 | 1.03 [0.85, 1.19] | 1 | 1.25 | +0 | green | **screen** |
  - orch-default-opus vs `plain-sonnet` (secondary): ratio=0.85 [0.80, 0.89]
| orch-planner-value-opus | plain-opus | 3/3 | 1.05 [0.96, 1.14] | 1 | 1.17 | +0 | green | **screen** |
| orch-planner-value-fable | plain | 3/3 | 0.37 [0.30, 0.48] | 0.25 | 0.61 | +0 | green | **screen** |

## Evidence limits

- bootstrap_ci: 2000 resamples, seed=20260919
- confirmed requires >= 3 reps, >= 8 paired passing tasks, and split=holdout
- external_harness_exec_time_may_still_include_its_own_startup
- models_not_necessarily_matched_across_campaigns_or_harnesses
- models_not_necessarily_matched_across_harnesses
- n_tasks=3
- non_contemporaneous_runs_across_campaigns
- single_repetition_per_task_per_harness

