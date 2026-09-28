# Preregistration: turn planner (value objective, $12/hour) on fresh `holdout3` and `m-holdout3`

Written 2026-09-25 ~21:25 UTC, committed and pushed to `origin/feat/turn-planner` BEFORE either batch is launched
(the remote push time is the independent timestamp). Candidate: the commit that adds this file (frozen snapshot,
recorded in each manifest.json).

**Candidate policy ("planner v12").** Jev judge (unchanged). On turns judged easy, the turn planner
(docs/proposals/TURN-PLANNER.md) picks the host or Sonnet 5 by minimising expected cost + $12/hour x expected time.
It uses per-model prices, measured per-model priors, prompt-cache state and a one-step lookahead. Hard turns, large
workspaces and user-picked models stay on the host, as today. The policy was chosen on the dev splits only (S1 dev,
s1m m-dev). At $12/hour it matched the old default on the Fable host and removed the old default's multi-turn cost
penalty on the Opus host (dev, Opus multi-turn: planner 0.95x time / 0.93x cost vs plain Opus, old default 0.92x / 1.36x).

**Splits.** `holdout3` is 12 new single-turn tasks, 3 per family, one per family longer. `m-holdout3` is 3 new 4-turn
scenarios built from those 12 tasks: turns run repair -> answer -> edit -> bugfix, one session each. Authored and
graded by tests only; no agent run has seen them. Both batches run concurrently.

**Design.** Single-turn: suite s1, split holdout3, 3 reps, base seed 20261010. Multi-turn: suite s1m, split
m-holdout3, 3 reps, base seed 20261011. `--parallel 3` each; cell order shuffled per rep (recorded). Statistics
follow STUDY-DESIGN §8. evals/run.py detects §8 critical failures automatically: a check the candidate failed that
the anchor passed on the same task and rep, a protected-file change, or an evaluator crash.

Single-turn cells: `plain` (Fable 5.1), `plain-opus` (Opus 5.5), `plain-sonnet`, `orch-default`, `orch-default-opus`
(old default), `orch-planner-v12-sub-fable`, `orch-planner-v12-sub-opus`. Single-turn benchmark sessions stand in for
sub-agent sessions, which make up 74% of this user's provider calls. These planner cells therefore set the
first-turn continuation probability to the measured sub-session value (0.06).

Multi-turn cells: `plain`, `plain-opus`, `plain-sonnet`, `orch-default`, `orch-default-opus`,
`orch-planner-v12-fable`, `orch-planner-v12-opus` (default continuation priors: first turn 0.25, later turns 0.8).

**P1 (Opus host, single-turn, `orch-planner-v12-sub-opus` vs `plain-opus`).** All six STUDY-DESIGN §8 criteria:
1. gate green on every rep
2. time ratio <= 0.90
3. sign test p <= 0.05 (>= 10 of 12 faster)
4. cost ratio <= 1.00
5. successes >= anchor - 1, with zero critical failures
6. >= 3 reps and >= 8 paired passing tasks

Prediction (dev): time ~0.83-0.94, cost ~0.94. The time and sign criteria may fail; reported either way.

**P2 (Fable host, single-turn, `orch-planner-v12-sub-fable` vs `plain`).** Same six criteria. Prediction: time ~0.52,
cost ~0.48.

**P3 (Opus host, multi-turn, `orch-planner-v12-opus`).** vs `plain-opus`: cost ratio <= 1.05, zero critical
failures, successes >= anchor - 1. vs `orch-default-opus` (old default): cost ratio <= 0.85. Time is reported, not
claimed. With 3 scenarios no sign test can reach p <= 0.05, so this is a preregistered screen, not a confirmation.

**P4 (Fable host, multi-turn, `orch-planner-v12-fable` vs `plain`).** time ratio <= 0.60, cost ratio <= 0.85, zero
critical failures, successes >= anchor - 1. A screen, like P3.

**Secondary, descriptive:**
- old default vs anchors in both batches (a replication of holdout2 and of the multi-turn cost penalty)
- every cell vs `plain-sonnet`
- per-turn served models (result.json `routing`, `turns`)
- planner receipts (`turn_planned`)

**What would falsify:** any listed criterion failing. A failed criterion is reported as failed. No re-run on these
splits with a changed configuration.
