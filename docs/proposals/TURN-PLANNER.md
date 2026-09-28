# Turn planner: cache- and price-aware model choice per turn (spec)

Status: specification for implementation (2026-09-25). Opt-in until a benchmark confirms it.

## Why

The shipped router asks Jev once per turn "easy or hard?" and sends easy turns to a fixed cheap model
(`model_routing.start_model`, Sonnet 5). Measurements on 2026-09-25 (docs/evidence/2026-09-25/holdout2,
tuning/, and the multi-turn `s1m` suite) show that whether that pays depends on the host model and on the
provider prompt cache, which is per model:

| Host | Single-turn session (fresh cache) | Multi-turn session (cache warm) |
|---|---|---|
| Fable 5.1 | 0.52x time, 0.49x cost (confirmed) | 0.45x time, 0.66-0.73x cost (dev) |
| Opus 5.5 | 0.80x time, 0.94-0.97x cost (confirmed) | 0.89x time, **1.34-1.41x cost** (dev) |

Opus 5.5 reads cached tokens at $0.20/M, below Sonnet 5's $0.30/M, so once a session's context is cached,
every easy turn moved to Sonnet costs more. A fresh session (most sub-agent sessions are single-turn) pays a
full cache write either way, and Sonnet's write price is lower, so there Sonnet is cost-neutral and faster.

Each extra round trip also costs 2-3.5 s here (large prompt prefill), a cold cache write adds about
1.95 s per 100k tokens on Opus and 0.65 s on Sonnet, and cheaper models take more calls per turn.

So the right choice is a small optimisation per turn, not a fixed rule: "cheap, fast, right: pick two",
with the judge guarding "right" and the planner trading cost against time.

## Behaviour

Config lives under `model_routing.planner` (all keys optional):

```yaml
planner:
  enabled: false                 # off = today's behaviour, byte for byte
  objective: balanced            # speed | cost | balanced | value
  cost_tolerance: 0.05           # balanced: allowed expected-cost increase vs staying on the host
  candidates: []                 # cheap models to consider for easy turns; empty = [start_model]
  cache_ttl_seconds: 300
  expected_calls: 3.5            # provider calls in an easy turn, on the host
  output_tokens_per_call: 170    # fallback for a model with no priors[model].output_tokens_per_call
  value_of_time_usd_per_hour: 36 # "value" objective only; see "Objectives" below
  priors:                        # per model family (prefix match on the model id); measured 2026-09-25
    claude-fable-5-1: { latency_s: 5.4, calls_factor: 1.0,  cold_s_per_100k: 2.0,  output_tokens_per_call: 260 }
    claude-opus-5-5:  { latency_s: 3.3, calls_factor: 1.0,  cold_s_per_100k: 1.95, output_tokens_per_call: 180 }
    claude-sonnet-5:  { latency_s: 2.4, calls_factor: 1.15, cold_s_per_100k: 0.65, output_tokens_per_call: 165 }
    claude-haiku-4-5: { latency_s: 2.2, calls_factor: 1.35, cold_s_per_100k: 0.4,  output_tokens_per_call: 185 }
  continue_probability:          # P(a LATER turn runs on the host); see "Lookahead" below
    sub_session: 0.06
    first_turn: 0.25
    later_turn: 0.8
```

`output_tokens_per_call` moved from one global assumption to a per-model measured median (2026-09-25 dev
runs) after a Fable-host multi-turn dev regression: with everything defaulting to 170, a warm Fable looked
marginally cheaper than a warm Sonnet under `balanced`; in reality Fable writes roughly 2x the output
tokens per call of Sonnet/Opus at Fable's own $50/M output rate, which the single global default was
silently hiding. A model without its own `priors[model].output_tokens_per_call` still falls back to the
global `output_tokens_per_call`.

Prices come from `savings.DEFAULT_RATES` (input, output, cache_read, cache_write per M tokens); a model with
no price or no prior is not a candidate. If the host itself has no price or prior, the planner abstains and
today's behaviour applies.

1. Turns judged hard, turns scope-gated to the host (large workspace), and turns where the user picked a
   model are unchanged: they run on the host, as today. The planner only decides easy turns.
2. For an easy turn, the options are the host plus each candidate. For each option `m`:
   - `ctx` = estimated prompt tokens of the turn's first request (system + tools + messages; use the
     provider-reported prompt size of the previous call when available, else characters / 4).
   - `m` is warm if it served a call in this session less than `cache_ttl_seconds` ago; then
     `cold = max(0, ctx - cached[m])` where `cached[m]` is the prompt size of `m`'s last call; else
     `cold = ctx`.
   - `n = expected_calls * calls_factor[m]`
   - `cost = cold * write[m] + (ctx - cold) * read[m] + (n - 1) * ctx * read[m] + n * out * output[m]`
   - `time = n * latency_s[m] + cold / 100000 * cold_s_per_100k[m]`
3. Choice (see "Objectives" below for the full table): `speed` = least time; `cost` = least cost;
   `balanced` = least time among options whose cost is at most `host_cost * (1 + cost_tolerance)` (the
   host always qualifies); `value` = least `cost + value_of_time_usd_per_hour / 3600 * time` (a single
   number, no separate budget step). Ties go to the host.
4. The chosen model is used for the whole turn (decide once). Choosing the host behaves exactly like a
   hard turn (no model override, host effort) with reason code `planner_host`; choosing a candidate behaves
   like today's easy turn with that model as the start model and reason code `planner_<objective>`.
5. After every provider response, record per model: `last_used_at` and `cached[m]` = the prompt size the
   provider reported (input + cache_read + cache_write tokens).
6. Emit one receipt per planned turn, `fast_decisions:turn_planned`, with the objective, `ctx`, the
   resolved `session_kind` and `p_continue` (see "Lookahead" below), each option's
   `{model, warm, cold, cost, time, lookahead_cost, lookahead_time}` (plus `utility` per option and a
   top-level `value_of_time_usd_per_hour` when `objective: value` -- see "Objectives") and the choice
   (add it to `EVENT_NAMES`, the privacy allowlist of fields, `schemas/event.schema.json` and
   docs/EVENTS.md).

## Objectives

| Objective | Minimises | Notes |
|---|---|---|
| `speed` | `time + lookahead_time` (TOTAL time) | Least time, full stop. |
| `cost` | `cost + lookahead_cost` (TOTAL cost) | Least cost, full stop. |
| `balanced` (default) | `time + lookahead_time`, among options whose TOTAL cost is within `cost_tolerance` of the host's own TOTAL cost | The host always qualifies. A cost BUDGET, not a single number -- can leave a much faster option unchosen if it is even slightly over budget (see the Fable-warm/Sonnet-warm worked example below). |
| `value` | `(cost + lookahead_cost) + value_of_time_usd_per_hour / 3600 * (time + lookahead_time)` | Converts TOTAL time into a dollar figure at `value_of_time_usd_per_hour` and adds it to TOTAL cost, so the whole speed/cost/lookahead triangle collapses to ONE number to minimise -- no separate budget step. `value_of_time_usd_per_hour: 0` degenerates to exactly `cost`; an arbitrarily large value degenerates to exactly `speed`. |

`value` exists because `balanced`'s cost-tolerance budget is myopic about time: a candidate that is
*slightly* over budget on cost is rejected outright even if it is dramatically faster, and a candidate
that is *within* budget is accepted even if it is dramatically slower -- there is no dial between those
two extremes. Live-smoke evidence: a Fable-host multi-turn session kept choosing a warm Fable under
`balanced` (0.117774 USD for Sonnet was just outside Fable's 0.111125 * 1.05 == 0.116681 budget), even
though Fable is ~2x slower per call (18.9s vs 9.7s) -- `balanced` cannot see that trade at all, because
its only lever is a cost ceiling, never a price on time. `value` prices that same trade explicitly in one
number: at `value_of_time_usd_per_hour: 36` ($0.01/s), Fable's utility (0.111125 + 0.01*18.9 == 0.300125)
is nearly 1.4x Sonnet's (0.117774 + 0.01*9.6925 == 0.214699) -- Sonnet wins outright.

## Lookahead

The greedy per-turn choice above is myopic: on a fresh session, a candidate's cache write is usually
cheaper than the host's (this is exactly why `balanced` picks Sonnet over a cold Opus), but if a LATER
turn in the same session ends up back on the host anyway (a turn judged hard, scope-gated, or
user-pinned bypass the planner entirely and always run on the host), that later turn now pays a FULL
cold write on the host -- a second cold write instead of the single one the host would have paid if
chosen from the start. Live-smoke evidence: an Opus-host session cost 1.42x plain Opus (worse than the
1.23x the old fixed-`start_model` router already cost) because turn 1 picked Sonnet, then a hard turn 2
paid a full cold Opus write anyway.

For every candidate option `m` (never the host, whose lookahead is always 0), using the HOST option's
OWN `cold` THIS turn (`cold_host`) and the host's own rates/prior:

- `lookahead_cost = p_continue * cold_host * (write_host - read_host)` (divided by 1e6 as `cost` is)
- `lookahead_time = p_continue * cold_host / 100000 * cold_s_per_100k_host`

These are recorded as their own `lookahead_cost`/`lookahead_time` fields on each option (`0` for the
host) so receipts show the term explicitly; step 3's `speed`/`cost`/`balanced` rules compare
`cost + lookahead_cost` and `time + lookahead_time` -- the TOTALS -- never the bare fields alone. When
the host is already warm this turn (`cold_host == 0`), every lookahead term is exactly 0: there is
no stale cache to eventually pay for, so lookahead never penalizes a candidate once the host has
already been warmed (e.g. by an earlier hard turn) -- see the "both warm" case in the worked example
below.

`p_continue` is the probability that a LATER turn in this session runs on the host, resolved per turn
from `planner.continue_probability` by session kind (`sub_session` > `first_turn` > `later_turn`, in
that priority order -- a sub-session is classified `sub_session` regardless of whether this happens to
be its first turn, since sub-sessions essentially never get a second one):

- **sub_session**: this session has a parent (an explicit parent session id when the coordinator
  exposes one; otherwise a best-effort fallback on Amplifier's sub-session id naming convention, an
  underscore followed by an agent-name-like suffix -- documented as a fallback precisely because it
  cannot fully rule out an unrelated underscore in a session id).
- **first_turn**: no parent, and nothing has been recorded yet for ANY model in this session (the
  turn planner's own persisted per-model cache state -- see the cross-process persistence section --
  is empty).
- **later_turn**: no parent, and something has already been recorded.

Defaults (`DEFAULT_PLANNER_CONTINUE_PROBABILITY`) are measured from this user's own last 14 days of
local sessions (`~/.amplifier/projects/*/sessions/*/events.jsonl`, counting `prompt:submit` and
`llm:response` events): of all provider calls, sub-session first turns were 74%, root first turns 7%,
root later turns 12%, sub-session later turns 6%; 94% of sub-sessions and 77% of root sessions ended
after exactly one turn. `sub_session` is deliberately the LOWEST continue probability (a sub-session
essentially never gets a second turn); `later_turn` is the HIGHEST (a root session that already
survived past its first turn is disproportionately likely to keep going, since the 77% one-turn-only
mass has already been excluded by definition). Each value is validated to `[0, 1]`; the whole dict or
individual keys are overridable, and an override for one session kind keeps the defaults for the
others (no per-field merge required to just tune one number).

**Worked example** (Opus host, balanced, `cost_tolerance: 0.05`, default priors/rates, ctx=70000,
both cold): Sonnet's base numbers are cheaper and faster than Opus's (cost 0.336289 vs 0.3969 USD;
time 10.115 vs 12.915 s). At `p_continue = 0.25` (a root session's first turn), Sonnet's lookahead
penalty is `0.25 * 70000 * (5.0 - 0.2) / 1e6 = 0.084`, pushing its TOTAL cost to 0.420289 USD --
above balanced's budget (`0.3969 * 1.05 = 0.416745`) -- so it is no longer eligible and the HOST
(Opus) is chosen instead. At `p_continue = 0.06` (a sub-session), the same penalty is only 0.02016,
Sonnet's total (0.356449) stays under budget, and it wins on time as before. If the host is already
warm this turn (`cold_host = 0`, e.g. because an intervening hard turn already paid the host's cold
write), the lookahead penalty is 0 regardless of `p_continue`, and the pre-lookahead choice applies
unchanged.

## Implementation notes

- Keep the math a pure function (e.g. `planner.plan_turn(host, candidates, state, ctx, now, config,
  rates, p_continue=0.0)` returning the options table and the choice) in a new module
  `src/amplifier_fast_decisions/planner.py`; the orchestrator only gathers inputs (including resolving
  `p_continue` from session kind), records cache state and applies the choice. Validate the config in
  `contracts.py` like the other `model_routing` keys.
- Unit tests for the pure function: fresh session on an Opus host, balanced -> Sonnet (both cold; Sonnet
  writes cheaper and is faster); same session later with Opus warm and 80k tokens cached -> Opus; Fable
  host warm with Sonnet cold at 30k tokens: speed -> Sonnet, cost -> Fable; unknown host price -> abstain;
  TTL expiry makes a model cold; the lookahead term at each `continue_probability` tier (root first turn,
  sub-session, later turn with the host warm/cold). Orchestrator tests: disabled = byte-identical
  requests and receipts; hard/pinned/scope-gated turns untouched; cache state updated from responses;
  receipt emitted once per turn; session-kind detection (explicit parent id, naming-convention fallback,
  first vs later turn from persisted state).
- Turn-planner cache state (`Runtime.planner_state`/`planner_last_ctx`) is persisted per session under
  `<events_dir>/planner-state/<session_id>.json` (atomic write, best-effort, never fails a turn) so a
  resumed session (a fresh process per turn) does not start every model cold again; `ctx` prefers this
  persisted `last_ctx` plus the new turn's own user message over a characters/4 estimate.
- `value` objective and per-model `output_tokens_per_call` tests: Fable host later turn (Fable warm 75k,
  Sonnet warm 70k, `p_continue: 0.8`) -- `balanced` still picks Fable (Sonnet's cost is just outside the
  cost-tolerance budget) but `value` picks Sonnet (Fable's much larger per-call time and output-token
  cost now price in); Opus host later turn (Opus warm 90k, Sonnet cold) -- `value` keeps Opus; Opus fresh
  sub-session at 70k -- `value` picks Sonnet; `value_of_time_usd_per_hour: 0` reproduces the `cost`
  objective's choice exactly; an arbitrarily large `value_of_time_usd_per_hour` reproduces `speed`'s
  choice exactly.
- Run the full suite: `PYTHONPATH=src python3 -m unittest discover -s tests`.

## How it will be judged

Benchmark cells with `planner.enabled: true, objective: balanced` on the Opus and Fable hosts, on the S1
single-turn dev split and the `s1m` multi-turn dev split, then a preregistered fresh holdout. Success on the
Opus host: multi-turn cost ratio <= 1.00 vs plain Opus with time <= 0.95, and single-turn no worse than the
shipped default; on the Fable host, no loss against the shipped default.
