# Goal

**Make Amplifier drastically cheaper and faster on real work, at equal quality, by putting a fast judge
(Jev, ~0.15 s per typed decision) in charge of the small decisions an agent loop makes at every step.**

"Real work" means the owner's actual sessions (large repos, long multi-step requests, helper agents), not only
small benchmark tasks. The measured baseline on 2026-09-25 (two real projects, one day): 2,882 model calls,
about $362 provider-reported cost, a median of 24 model calls per request (p90 112, max 258), 4,172 bash calls,
93 helper-agent launches, and every call re-reading a 100k+ token prompt (~$0.13 per call on Opus 5.5).

## What the judge decides (per step, not per request)

At each point where the loop would call the model, the judge chooses the cheapest action that keeps quality:

1. **Skip the model:** run a prepared, deterministic action (read the file just named, re-run the command that
   just failed, answer from a tool result) when the next step is predictable.
2. **Cheaper model for this step:** routine continuations (read a tool result, issue the obvious next call) on
   Sonnet or Haiku, accounting for each model's own prompt cache so switching does not cost more than it saves.
3. **Block or right-size waste:** a helper-agent launch that could be done inline, a repeated or looping call,
   an oversized tool output.
4. **Full model:** anything that needs real reasoning, planning, or a code change in a large repo.

Choosing one model for a whole request (what shipped first) is only a special case of (2) and, with Opus 5.5 as
the default model, saves almost nothing.

## Success criteria (all measured, all at equal quality)

On the owner's real workload (replayed from recorded sessions where possible) **and** on preregistered
benchmarks with fresh held-out tasks, alternating run order, and the answering model recorded per call:

| Metric | Target vs plain Amplifier on the same default model |
|---|---|
| Cost per request | **≤ 0.50×** |
| Model calls per request (full-model calls) | **≤ 0.60×** |
| Wall time per request | **≤ 0.70×** |
| Quality | Task pass rate and SWE-bench resolved count within run-to-run noise (non-inferiority as in `evals/STUDY-DESIGN.md` §8) |

**Every efficiency must be registered on the dashboard.** Each per-step decision leaves a receipt (what was
decided, by which judge or rule, and what it saved), and the dashboard shows, per project and per lever (calls
skipped by prepared actions, cache kept warm, cheaper-model steps, blocked launches, loop stops, context
right-sizing), the calls, dollars and seconds saved -- or lost -- against the counterfactual of the same step on the
default model. These numbers must match recomputation from the stored records and exclude test/benchmark traffic.
An efficiency that is not visible and recomputable on the dashboard does not count toward the goal.

## Method

1. Measure first: classify every model call in real sessions (routine continuation, exploration, reasoning,
   final answer, delegation, loop) and price each bucket, including the cache cost of switching models.
2. Build the per-step router behind settings, with a receipt for every decision (what was decided, by whom, what
   it saved).
3. Screen on the large-repo and multi-turn suites with the owner's default model; confirm on fresh preregistered
   holdouts; keep quality guards (large-repo edits stay on the full model unless evidence says otherwise).
4. Ship only what is confirmed; report savings honestly, including where it does not help.

## What the measurement says (2026-09-25, real sessions, `docs/evidence/2026-09-25/step-opportunity/summary.json`)

$363 across 2,884 calls: 61% re-reading cached context, 30% writing cache, 9% output. Median prompt per call 263k
tokens. Helper agents are 67% of the bill; 13 helpers with 50+ calls cost $215. Routine continuations are 51% of
cost, driven by context size, not reasoning. Routing routine steps to Sonnet 5 would ADD $17-46 (its cache read is
dearer than Opus 5.5's); Haiku saves at most $3 (200k context window). So the levers, ranked by measured upper bound:

1. **Cache keep-alive during long waits** (tool or helper running >~4.5 min): about -13% cost, near-zero risk.
2. **Remove calls:** deterministic waits instead of `sleep` polling, batched read-only chains, prepared
   status/read actions, loop and repeated-failure stops: up to -16% cost, -10% model time.
3. **Right-size context:** compaction or fresh-context hand-off for helpers past ~150-200k tokens; slimmer helper
   base prompt (84k today): up to -30% cost, highest quality risk.
4. **Price-aware model choice per step** (not tier-based): about -12% cost, +13% model time.
5. Harness overhead fixes (settings): first-request cost -84%, trivial request 25 s -> 20 s (-> ~11-12 s with the
   start-up check fix).

Levers 1-3 together are needed to reach cost <= 0.50x.

## Efficiency receipts (how the dashboard numbers are made)

Each optimization decision emits a `fast_decisions:efficiency` event (see `src/amplifier_fast_decisions/efficiency.py`):
`lever`, `mechanism` (the judge, rule or mechanism), `decision`, `baseline` and `actual` sides (model, calls, cost,
seconds) fixed when the decision is made, `calls_saved` / `usd_saved` / `seconds_saved` (baseline minus actual;
negative = cost more), `method`, `project`, and `traffic` (`production` or `test`: `AFAST_TRAFFIC` wins; temp dirs,
eval roots, agent worktrees and Forge-labeled sessions are test). The dashboard's **Efficiency ledger**,
`/api/efficiency` and `afast efficiency [--include-test] [--json]` all show `efficiency.aggregate`: plain sums by
lever and by project x lever over the stored events, deduplicated by `event_id`, production only by default.
Recompute independently by reading the event files and summing those three fields.

Mechanisms emitting receipts today: `cheaper_model` (each cheaper-model step, the host's cache rebuild after a cheap
turn as a loss, and judge time on judged turns kept on the host), `prepared_action` (when the read shortcut is
enabled), `cache_keepalive` (on by default in `behaviors/fast-decisions.yaml`: one receipt per long tool wait,
refresh costs against the avoided cache re-write, confirmed from the next call's cache reads), and the waste guards
(`docs/WASTE-GUARDS.md`, on by default): `loop_stop` (mechanisms `guard:identical_repeat`, `guard:error_retry`,
`guard:poll_wait`) and `context_rightsize` (`guard:identical_result`), in Amplifier and, through the Claude Code
hook, tagged `harness: "Claude Code"`. The older loop-stop nudges (`Policy.loop_stop`, mechanism `rule:*`) stay off
by default (their receipts did not reconcile) and stand down whenever the waste guards are on, so one loop is never
receipted twice. `launch_blocked` is reported as "not active yet"; it will emit the same receipt shape.

Waste census (2026-09-25, `docs/evidence/2026-09-25/waste-census/`): deterministic step waste (repeats, error
retries, sleep polls, re-reads, trivial helpers, rejected calls, unbatched reads) is about 6% of the last 30 days'
spend, two thirds of it one runaway loop; the large recoverable items are prompt-cache rebuilds after tool waits
over 5 minutes (~13%) and after user idle (~9%) and oversized helper context (up to ~12%).

## Status

See `docs/RESULTS-2026-09-24.md` (top update) for what has been measured so far. As of 2026-09-25 the savings targets are
**not met**: the shipped router picks one model per request and saved close to nothing on the owner's real work.
The measurement side is in place: per-decision receipts, the Efficiency ledger by project and lever, test traffic
excluded, and exact recomputation (commit `2dbe1d2`).
