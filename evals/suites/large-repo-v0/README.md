# large-repo-v0: routing levers screen (Opus 5.5 host)

This is a screen, not a confirmation. The run order was not preregistered, there are 2 reps and 15 tasks, and
other benchmarks shared the machine. Use it to decide which levers get a preregistered study.

## Task set

The repository is this bundle's own tree pinned at `30bbb7f` (1,142 tracked files, so the shipped scope
gate of 300 fires). Each run gets a fresh clone. Definitions, setups and hidden checks are in `tasks.py`.
`selftest.py` shows that every check fails on the untouched (or bugged) checkout and passes with a reference
answer or fix.

| kind | tasks | check |
|---|---|---|
| question (6) | Policy.timeout_ms default, orchestrator module name, explain `workspace_file_count`, count EVENT_NAMES, which function picks the start tier, Jev key env var | regex on the final answer; no tracked file changed |
| git (4) | uncommitted files (tree dirtied by setup), last commit subject, commit count, current branch (setup switches it) | regex on the final answer |
| edit (2) | change a Policy default; add `pretty_model()` to savings.py | import-level check; only the expected file changed / savings tests pass |
| bugfix (3) | injected, committed bugs: escalation gate 0.07, inverted skip-dir filter (a ScopeGateTests failure), rate prefix order | behavior check plus the relevant test module; `b_scope_count` also forbids test edits |

Check revision (disclosed): in rep 1 every agent, on every config, added a regression test for
`b_escalation_gate` and `b_rates_prefix`. The original checks rejected any `tests/` change even though the
prompts did not forbid one. Both checks now allow added tests, and `analyze.py` re-scores rep 1 from the
recorded check detail using the same rule (`passed_original` keeps the old verdict).

## Configs

All configs use host `claude-opus-5-5` (provider `anthropic`) and the composed bundle exactly as installed,
plus overrides from `configs.py`. Only `plain` differs: it runs upstream loop-streaming with fast-decisions off.

| config | what changes |
|---|---|
| plain | fast-decisions off |
| default | shipped behavior; in a >300-file repo the scope gate sends every turn to the host without asking Jev |
| L2-both | `large_repo {max_p_complex 0.3, non_editing_max_p_edit 0.5, require both}`: cheap only if confidently easy AND read-only |
| L2-either | same thresholds, `require either`: read-only OR confidently easy |
| frugal | `profile: frugal` = L1 tiers (Haiku 4.5 low < 0.2, Sonnet 5 medium < 0.5) + L2-either |
| L2-both-L3 | L2-both + `strong_effort {max_p_complex 0.8, effort medium}` (host at medium effort) |

`careful` was not run. In a large repo it behaves the same as `default` by construction: scope gate, no judge call.

## Results (2 reps, `analyze.py`; raw data in /tmp/ampup/routing-levers/runs)

How the numbers are computed: ratios are geometric means over tasks of (median of the config's runs /
median of plain's runs). "exec" runs from the first provider request to the last response. "wall" includes
about 25 s of CLI startup. "cost" is the provider-reported cost, which is an estimate, not billing.

| config | pass | exec | wall | cost | served-model mix (requests) |
|---|---|---|---|---|---|
| plain | 29/30 | 1.00x | 1.00x | 1.00x | opus 100% |
| default | 28/30 | 1.20x | 1.16x | 1.02x | opus 100% |
| L2-both | 29/30 | 0.76x | 0.90x | 0.92x | opus 66%, sonnet 34% |
| L2-either | **30/30** | **0.51x** | **0.68x** | 0.90x | sonnet 74%, opus 26% |
| frugal | 29/30 | 0.73x | 0.84x | **0.32x** | haiku 70%, opus 25%, sonnet 5% |
| L2-both-L3 | 28/30 | 0.92x | 1.05x | 0.92x | opus 64%, sonnet 36% |

Read-only tasks (question + git, 10 tasks):

| config | pass | exec | cost |
|---|---|---|---|
| default | 20/20 | 1.02x | 0.99x |
| L2-both | 20/20 | 0.70x | 0.86x |
| L2-either | 20/20 | 0.70x | 0.89x |
| frugal | 19/20 | 0.81x | 0.25x |

Edit + bugfix tasks (5 tasks):

| config | pass | exec | cost |
|---|---|---|---|
| default | 8/10 | 1.67x | 1.08x |
| L2-both | 9/10 | 0.89x | 1.04x |
| L2-either | 10/10 | 0.27x | 0.93x |
| frugal | 10/10 | 0.59x | 0.53x |
| L2-both-L3 | 9/10 | 1.46x | 0.96x |

Noise band: `default` routes exactly like `plain` in this repo, so its 1.20x (edits 1.67x) is pure noise.
Edit-task times swing from 17 s to 244 s for the same model and prompt. Read-only ratios are tight
(`default` 1.02x / 0.99x there) and are the credible part of this screen.

Router receipts:
- Jev took 143 ms at the median and 208 ms at p90 (121 calls).
- `edits_code` separated the kinds cleanly: p(edits) = 0.00 for all 10 read-only prompts and 1.00 for all 5 edit/bugfix prompts.
- p(complex) was below 0.05 for every read-only prompt except the start-tier question (0.28).
- The bugfixes scored 0.04, 0.36 and 0.78. Only the 0.78 bug is one a Sonnet run would clearly have found harder.
- `slow_end` `served_model` matched the native `llm:response` model in 149/150 runs. The mismatch was a run killed at the deadline.

Why cost moves less than the list prices suggest: every fresh session writes about 70k tokens of foundation
context to the prompt cache. Cache writes cost $5/MTok on Opus 5.5 and $3.75 on Sonnet 5, and Sonnet's
cache reads cost more than Opus's ($0.30 vs $0.20). So Sonnet saves only about 10-15% even on turns it serves
entirely. Haiku ($1.25 cache write) is where cost falls.

## Profiles: the cheap / fast / right trade

| profile | routing | trade |
|---|---|---|
| careful | cheap only when p(complex) < 0.3; large repos always use the host | right > fast > cheap. Gives up most small-repo speed to avoid any cheap-model miss. Nothing changes in large repos. |
| balanced (default) | two tiers at p(complex) 0.5; the scope gate keeps large repos on the host | Fast and cheaper in small repos with a Fable host (0.42x / 0.50x). About 1.0x with an Opus 5.5 host. In large repos it is exactly plain. |
| frugal | Haiku below 0.2, Sonnet below 0.5; in large repos, read-only or confident-easy turns take a cheap tier | cheap > fast > right. About 0.3x cost in this screen. Haiku sometimes loses the thread under the full foundation prompt (1/30 here answered "ready to help" instead of the commit count) and can take more requests on edits. Repository edits the judge is unsure about (p ≥ 0.3) still run on the host. |

## Recommendation (for a preregistered confirmation)

1. **L2 read-only routing (`large_repo` with `non_editing_max_p_edit`, `require both`)**: strongest candidate
   for a new balanced default. This is the lever that touches the user's real usage, since most requests hit
   the scope gate. It kept every repository edit on the host (0 cheap edit turns). Read-only turns got about
   0.70x exec and 0.86x cost with no quality loss (20/20). It is safe by construction: an edit judged
   read-only is the only way it can go wrong, and `edits_code` was 0.00/1.00 separable here. Confirm on a
   broader read-only set (longer questions, reviews, multi-file explanations), and include read-only prompts
   that are phrased like edits.
2. **Haiku tier for read-only/very-easy turns (frugal's L1 + L2)**: worth confirming as the cost lever.
   Read-only cost was 0.25x (0.32x overall) at 19/20. Measure the quality risk directly: Haiku's
   instruction-following under the full foundation context, and edits routed to Haiku.
3. **L2-either (confident-easy edits in large repos go to Sonnet)**: the biggest time effect (0.51x exec
   overall, 0.27x on edits, 30/30). But it routes bugfixes to Sonnet, and SWE-bench showed that loses fixes
   the judge misrates (a hard Django bug judged easy 3/3 times). Only confirm it with a hard-bug holdout.
   Here Jev kept the two harder bugs (0.36, 0.78) on the host, but these bugs are easy.
4. **L3 host effort (`strong_effort`)**: drop it. It saved no cost (0.92x, the same as L2-both, which it
   extends), was slower on edits (noisy), and failed one more run. That fits the earlier SWE-bench loss.
5. **Wall time**: CLI startup (about 25 s) caps wall savings on short turns (0.68x to 0.93x wall vs 0.51x to
   0.76x exec). Any confirmation should report both.
