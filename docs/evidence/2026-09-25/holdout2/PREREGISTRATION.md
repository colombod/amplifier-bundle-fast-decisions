# Preregistration: S1 `holdout2` (fresh, never-run split), shipped default and a cost preset, Fable and Opus hosts

Written 2026-09-25 ~13:45 UTC, committed and pushed to the remote branch `feat/iron-triangle-tuning` BEFORE the
holdout2 batch is launched (the remote push time is the independent timestamp). Candidate: the commit that adds this
file (frozen snapshot recorded in manifest.json).

**Why a new split.** The 8-task `holdout` was used twice (2026-09-24 and 2026-09-25) and the default was changed
between the two, so it is spent (STUDY-DESIGN §3/§8). `holdout2` is 12 new tasks (3 per family; one per family is
about 2-3x longer), authored and graded by tests only; no agent run has seen them.

**Design.** Suite S1, split holdout2 (12 tasks), 3 reps, base seed 20260929, `--parallel 3`, cell order shuffled per
rep (seeded; recorded in manifest `invocation.cell_order`). Cells:
- anchors: `plain` (host claude-fable-5-1), `plain-opus` (host claude-opus-5-5), `plain-sonnet` (control)
- `orch-default` (Fable host; shipped default: Jev judge, decide once, easy turns on Sonnet 5 at medium effort)
- `orch-default-opus` (Opus host; same shipped default)
- `orch-haiku-shaped-fable`, `orch-haiku-shaped-opus` (cost preset: easy turns on Haiku 4.5 with easy-turn
  guidance and the `todo` tool hidden)

Statistics per STUDY-DESIGN §8 (per task: median over reps; ratio = geometric mean of per-task ratios vs the anchor;
two-sided exact sign test on per-task median time).

**H1 (Fable, `orch-default` vs `plain`): faster at non-inferior quality.** All of:
1. mechanism gate green on every rep
2. time ratio <= 0.90
3. sign test p <= 0.05 (with 12 tasks: >= 10 of 12 faster)
4. cost ratio <= 1.00
5. successes >= anchor - 1, zero critical failures (protected-file violations)
6. >= 3 reps and >= 8 paired passing tasks
Prediction (dev, 3 batches): time ~0.51-0.57, cost ~0.46-0.49.

**H2 (Opus, `orch-default-opus` vs `plain-opus`).** Same six criteria. Prediction (dev, 4 batches 0.76-0.90; spent
holdout 1.01): time ~0.86, cost ~0.97-0.99. Expected to fail criterion 3 and possibly 2; reported either way.

**H3 (cost preset, Opus, `orch-haiku-shaped-opus` vs `plain-opus`): cheaper at non-inferior quality.** All of:
criteria 1, 5, 6 above, and cost ratio <= 0.60. Time is reported, not claimed. Prediction (dev, 1 batch): cost
~0.36, time ~1.1 (slower).

**H4 (cost preset, Fable, `orch-haiku-shaped-fable` vs `plain`).** Criteria 1, 5, 6 and cost ratio <= 0.60.
Prediction (dev, unshaped Haiku, 1 batch): cost ~0.20, time ~0.82.

**Secondary (descriptive, no claim):** every cell vs `plain-sonnet`; the 4 long tasks separately; calls per task
and provider-reported model per call (result.json `routing`).

**What would falsify:** any listed criterion failing. A failed criterion is reported as failed; no re-run on this
split with a changed configuration.
