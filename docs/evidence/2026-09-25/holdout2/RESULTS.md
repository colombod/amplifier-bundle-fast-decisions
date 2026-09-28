# Results -- s1 holdout2 (reps=3)

## Verdict table (vs anchor)

| Cell | Anchor | Passed/Total | Ratio (95% CI) | sign-test p | Cost ratio | Quality delta | Gate | Verdict |
|---|---|---|---|---|---|---|---|---|
| orch-default | plain | 12/12 | 0.52 [0.42, 0.65] | 0.006348 | 0.49 | +0 | green | **confirmed** |
  - orch-default vs `plain-sonnet` (secondary): ratio=1.04 [0.84, 1.36]
| orch-default-opus | plain-opus | 12/12 | 0.80 [0.67, 0.92] | 0.03857 | 0.97 | +0 | green | **confirmed** |
  - orch-default-opus vs `plain-sonnet` (secondary): ratio=0.87 [0.74, 1.03]
| orch-haiku-shaped-opus | plain-opus | 11/12 | 1.16 [0.92, 1.52] | 0.3877 | 0.45 | -1 | green | **disqualified (critical failure)** |
  - CRITICAL FAILURE: orch-haiku-shaped-opus task=repair_roman_to_int rep=1 labels=candidate_failed_where_anchor_passed
  - CRITICAL FAILURE: orch-haiku-shaped-opus task=repair_roman_to_int rep=2 labels=candidate_failed_where_anchor_passed
| orch-haiku-shaped-fable | plain | 11/12 | 0.66 [0.53, 0.83] | 0.03857 | 0.30 | -1 | green | **disqualified (critical failure)** |
  - CRITICAL FAILURE: orch-haiku-shaped-fable task=repair_roman_to_int rep=1 labels=candidate_failed_where_anchor_passed
  - CRITICAL FAILURE: orch-haiku-shaped-fable task=repair_roman_to_int rep=3 labels=candidate_failed_where_anchor_passed

## Evidence limits

- bootstrap_ci: 2000 resamples, seed=20260919
- confirmed requires >= 3 reps, >= 8 paired passing tasks, and split=holdout
- external_harness_exec_time_may_still_include_its_own_startup
- models_not_necessarily_matched_across_campaigns_or_harnesses
- models_not_necessarily_matched_across_harnesses
- n_tasks=12
- non_contemporaneous_runs_across_campaigns
- single_repetition_per_task_per_harness

