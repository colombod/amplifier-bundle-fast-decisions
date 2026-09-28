# Design recommendation (for human ratification)

Generated from results for s1 dev (reps=2). See evals/DESIGN-BRIDGE.md for the rules applied here.

- **mode**: keep `shadow` -- judge-local+effort verdict is 'not run', not confirmed on holdout with quality non-inferior.
- **judge backend default**: keep `ollama` -- jev verdict is 'not run', not confirmed.
- **effort_routing default**: keep the incumbent explore-only profile -- all-phase did not clear the bar without a quality regression.
- **model_routing default**: keep `off` -- routing cell not run this pass.

- **external state (privacy)**: remains opt-in regardless of any result above (docs/PRIVACY.md).

## Evidence limits
- bootstrap_ci: 2000 resamples, seed=20260919
- confirmed requires >= 3 reps, >= 8 paired passing tasks, and split=holdout
- external_harness_exec_time_may_still_include_its_own_startup
- models_not_necessarily_matched_across_campaigns_or_harnesses
- models_not_necessarily_matched_across_harnesses
- n_tasks=12
- non_contemporaneous_runs_across_campaigns
- single_repetition_per_task_per_harness

