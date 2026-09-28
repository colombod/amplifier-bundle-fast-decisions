# Results -- s1 dev (reps=2)

## Verdict table (vs anchor)

| Cell | Anchor | Passed/Total | Ratio (95% CI) | sign-test p | Cost ratio | Quality delta | Gate | Verdict |
|---|---|---|---|---|---|---|---|---|
| orch-default-opus | plain-opus | 12/12 | 0.85 [0.74, 0.98] | 0.146 | 0.98 | +0 | green | **screen** |
  - orch-default-opus vs `plain-sonnet` (secondary): ratio=0.92 [0.70, 1.09]
| orch-haiku-opus | plain-opus | 11/12 | 1.20 [0.96, 1.50] | 0.3877 | 0.49 | -1 | green | **screen** |
| orch-haiku-shaped-opus | plain-opus | 12/12 | 1.11 [0.89, 1.39] | 0.7744 | 0.44 | +0 | green | **screen** |
| orch-haiku-guided-opus | plain-opus | 11/12 | 1.10 [0.96, 1.25] | 0.5488 | 0.45 | -1 | green | **screen** |
| orch-sonnet-shaped-opus | plain-opus | 12/12 | 0.85 [0.74, 0.95] | 0.3877 | 0.98 | +0 | green | **screen** |

## Evidence limits

- bootstrap_ci: 2000 resamples, seed=20260919
- confirmed requires >= 3 reps, >= 8 paired passing tasks, and split=holdout
- external_harness_exec_time_may_still_include_its_own_startup
- models_not_necessarily_matched_across_campaigns_or_harnesses
- models_not_necessarily_matched_across_harnesses
- n_tasks=12
- non_contemporaneous_runs_across_campaigns
- single_repetition_per_task_per_harness

