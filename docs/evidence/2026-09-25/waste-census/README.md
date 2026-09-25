# Waste census (2026-09-25)

`summary.json` classifies and prices wasted model steps in the owner's real sessions. Aggregates only: no
prompts, commands, file contents, project names or paths. Recompute:

    PYTHONPATH=src python3 evals/waste_census.py --out docs/evidence/2026-09-25/waste-census/summary.json

Sources: Claude Code transcripts (`~/.claude/projects/*/*.jsonl` and `subagents/`; usage per assistant message,
deduplicated by message id), Amplifier session logs (`~/.amplifier/projects/*/sessions/*/events.jsonl`:
`llm:response` usage, `tool:pre`/`tool:post`), and the fast-decisions events dir (routing reason codes, receipt
count). Test traffic excluded: project dirs or session cwd matching tmp/private/worktree/afast/experiment/eval/
holdout/bench/swebench/smoke/pilot/test-session, `test-session-*` files, Amplifier projects without a path-derived
name. Prices per MTok (input/output/cache read/cache write): opus-5-5 4/20/0.20/5, sonnet-5 3/15/0.30/3.75,
haiku-4-5 1/5/0.10/1.25, fable-5-1 10/50/0.25/12.5 (plus older Claude models at list price; GPT/open-weight
models use the provider's recorded cost when present, else count as unpriced: 1,157 of 67,555 calls in the last
30 days).

## Last 30 days (2026-08-26 to 2026-09-25): $13,924, 67,555 model calls, 1,540 sessions

Claude Code $5,399 (207 sessions), Amplifier $8,526 (1,333). Helper sub-sessions: $7,790 (56%).

| Category | Occurrences | Wasted steps | $ | % of spend | Median steps / occurrence |
|---|---:|---:|---:|---:|---:|
| a identical call repeated, nothing changed | 23 | 1,235 | 554.15 | 3.98 | 1 |
| b same failing command retried | 13 | 34 | 30.17 | 0.22 | 1 |
| c sleep/poll loops | 63 | 144 | 12.80 | 0.09 | 2 |
| d re-read of a file still in context | 420 | 420 | 48.66 | 0.35 | 1 |
| e helper launched for a trivial lookup (net of doing it inline) | 126 | 229 | 38.99 | 0.28 | 2 |
| f tool call failed validation | 86 | 86 | 12.35 | 0.09 | 1 |
| f tool call denied | 10 | 10 | 1.09 | 0.01 | 1 |
| g sequential read-only calls that could be one step | 2,095 | 2,095 | 152.96 | 1.10 | 1 |
| **deterministic step waste (a-g)** | | | **851.17** | **6.12** | |
| h helper steps over 150k prompt tokens (excess, upper bound) | 700 sessions | 36,881 | 1,678.73 | 12.06 | 19 |
| x tool outputs over 20k chars (excess carried, upper bound) | 2,347 | – | 188.71 | 1.36 | – |
| y prompt cache rebuilt after a tool/helper wait > 5 min | 669 | 669 | 1,832.91 | 13.16 | 1 |
| y prompt cache rebuilt after user idle > 5 min | 445 | 445 | 1,240.07 | 8.91 | 1 |
| y cache rewritten within 5 min (prefix changed) | 3,100 | 3,100 | 578.75 | 4.16 | 1 |

Findings:

* Deterministic step waste is ~6% of recent spend, and two thirds of it ($548) is one runaway Claude Code helper
  that ran `true` 1,219 times. Without that loop it is ~2%. Error loops, sleep polling and re-reads are each well
  under 1%: current models (and Claude Code's own sleep blocker) already avoid most of it.
* The large recoverable items are not tool-call waste: cache rebuilds after long tool/helper waits (~13%, the
  keep-alive lever), after user idle (~9%), and oversized helper context (up to ~12%).
* All history (2025-12 to 2026-09, $65,232): deterministic waste 2.4%; Amplifier then rewrote its prompt cache
  on most calls (y churn 54% of all-history spend, mostly June-August; cause not diagnosed here); in the last 30
  days that is 4%.
* Routing (fast-decisions events): `start_strong` 3,419 vs `start_model` 2,018 turns; 11 efficiency receipts in
  1,391 session files (2,346 `test-session-*` files skipped; those came from `tests/test_kernel_validation.py`,
  now fixed to write to a temp dir).

Distributions used by the guards' counterfactuals (`summary.json` -> `distributions`): further identical repeats
after the 2nd identical run (all history median 2, n=9), further error retries after 2 failures (median 1, n=17),
further polls after the 2nd (median 1 last 30 days, 2 all history).

Method notes: each wasted tool call is priced at its issuing model step's list-price cost split over the calls
that step issued; g excludes the step's cache writes; "nothing changed" means no write, no non-read-only command,
no helper and (for a) byte-identical output; e counts helpers with <= 3 model calls and no edits, net of doing
their tool calls inline at the parent's median step cost; h, x and y are upper bounds and are not added to the
a-g total.
