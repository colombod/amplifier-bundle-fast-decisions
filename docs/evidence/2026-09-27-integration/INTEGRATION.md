# Main integration — 2026-09-27

All committed local branch tips were checked for ancestry and incorporated.
The integration retains the shipped Jev difficulty router and cache keepalive,
adds the waste guards enabled by their feature branch, and preserves optional
turn planning, per-step actions, local scoring, and research/evaluation tools.
AnyJev and jevgrep were already merged in PR #39 and remain optional.

| Incorporated branch | Tip |
|---|---|
| `feat/iron-triangle-tuning` | `c5ee79b9ac027aa2f59e14375365034b4cb8a3ee` |
| `feat/turn-planner` | `c09da3d161219fd3b50494cf817191c743fc22f0` |
| `resolve-review` | `4eb214e132aaf7970cf1dc3863d0bcdf9c93601d` |
| `feat/jev-step-actions` | `07047be20c149314b2023ef1e928afff14f52394` |
| `feat/waste-guards` | `664d270e0b871349d971319c05b3ca91279e1603` |
| `eval/continuous` | `348654df5d528a7ea7442c8b5ea2bec6ea8dde68` |
| `research/hosted-jev-scoring` | `84304d9fe89c7e1cbd0d6b2bff81f036a9ef929b` |
| `research/local-jev-scoring` | `03f9bb4c12c09fd2c2d5dc33af27342242d7f316` |

Conflict resolutions preserved independent policy fields and receipt metadata,
combined planner/step dispatch with the provider's usage and cache lifecycle,
and retained both escalation and planner guards around easy-turn shaping.
Event schema names were reconciled with the runtime allowlist.

Dirty worktrees were audited without overwriting them. Their code changes were
already incorporated or superseded; the unique atomic-question study is now
archived under `experiments/atomic-questions`. Runtime locks and PID files remain
in the original worktree. No API keys or raw private sessions are included.

Validation before measurement: 1,458 tests passed in the installed Amplifier
Python environment (2 skipped). The separate archived atomic study passed its
7 tests. Additional judge-usage tests cover measured/unknown usage, policy block,
cancellation, and batched attempts. Full final regression and live matched
comparison results are recorded alongside this audit when complete.

The matched example freezes implementation commit `c75499c`, alternates plain
Amplifier and the composed default bundle over four pairs, and checks identical
public duration-parser fixtures with independent grading. This is an illustrative
development task, not an unseen holdout or a general performance guarantee.
