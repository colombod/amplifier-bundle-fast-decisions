# holdout2: independent verification and verdicts

An independent session recomputed every statistic from the 252 raw `result.json` records (not with `evals/run.py`).

**Preregistration.** `PREREGISTRATION.md` (commit 99c3562) was pushed to `origin/feat/iron-triangle-tuning` at
13:42:23 UTC (remote-tracking reflog); the first holdout2 run started at 13:42:50 UTC. The frozen candidate
worktree is exactly 99c3562. None of the 12 task names appear in any earlier campaign.

**Verdicts (STUDY-DESIGN §8 as the standing rule):**

| Cell | vs | Time [95% CI] | Sign test | Cost (pipeline / per-task geomean) | Pass | Verdict |
|---|---|---|---|---|---|---|
| orch-default (Fable host) | plain | 0.52 [0.42-0.65] | 11/12, p=0.0063 | 0.49 / 0.48 | 36/36 | **confirmed** (H1) |
| orch-default-opus (Opus host) | plain-opus | 0.80 [0.67-0.92] | 10/12, p=0.039 | 0.97 / 0.94 | 36/36 | **confirmed** (H2) |
| orch-haiku-shaped-opus | plain-opus | 1.16 [0.92-1.52] | 4/12 | 0.45 / 0.36 | 34/36 | **disqualified** (H3) |
| orch-haiku-shaped-fable | plain | 0.66 [0.53-0.83] | 10/12, p=0.039 | 0.30 / 0.18 | 34/36 | **disqualified** (H4) |

**H3/H4 correction.** The preregistration restated criterion 5 as "zero critical failures (protected-file
violations)", narrower than STUDY-DESIGN §8, which also counts "an independent check the fd arm failed that plain
passed on the same task and rep". Haiku failed `repair_roman_to_int` in 4 of 6 runs (reps where the anchor passed):
its validator accepts out-of-order numerals such as `XCCDMM`. This is a genuine model error, not a grader bug (the
grader uses a fixed seed; the passing Haiku attempts and all anchor attempts raise `ValueError` on the same inputs).
Under the standing rule H3 and H4 are disqualified; the narrower preregistration wording was a drafting error and
is not used. `evals/run.py` now detects this case automatically and labels such cells "disqualified (critical
failure)"; it also accepts `holdout*` splits as confirmation-eligible (it previously required the literal
`holdout`).

**Order.** Cell order was shuffled per rep (manifest `invocation.cell_order`). Per-rep ratios show no trend with
position (H1: positions 1/4/2 -> 0.52/0.41/0.49; H2: 5/1/1 -> 0.94/0.78/0.98).

**Served model.** In every routed run, turns judged easy were served by the intended cheap model and turns judged
hard by the host (provider-reported per call). The comparison files' `served_model_counts` show the host as
`provider-default` because they count the requested model from receipts.

**Scope.** Single-turn sessions only (one prompt per fresh session). A multi-turn check is separate.
