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
  objective: balanced            # speed | cost | balanced
  cost_tolerance: 0.05           # balanced: allowed expected-cost increase vs staying on the host
  candidates: []                 # cheap models to consider for easy turns; empty = [start_model]
  cache_ttl_seconds: 300
  expected_calls: 3.5            # provider calls in an easy turn, on the host
  output_tokens_per_call: 170
  priors:                        # per model family (prefix match on the model id); measured 2026-09-25
    claude-fable-5-1: { latency_s: 5.4, calls_factor: 1.0,  cold_s_per_100k: 2.0 }
    claude-opus-5-5:  { latency_s: 3.3, calls_factor: 1.0,  cold_s_per_100k: 1.95 }
    claude-sonnet-5:  { latency_s: 2.4, calls_factor: 1.15, cold_s_per_100k: 0.65 }
    claude-haiku-4-5: { latency_s: 2.2, calls_factor: 1.35, cold_s_per_100k: 0.4 }
```

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
3. Choice: `speed` = least time; `cost` = least cost; `balanced` = least time among options whose cost is at
   most `host_cost * (1 + cost_tolerance)` (the host always qualifies). Ties go to the host.
4. The chosen model is used for the whole turn (decide once). Choosing the host behaves exactly like a
   hard turn (no model override, host effort) with reason code `planner_host`; choosing a candidate behaves
   like today's easy turn with that model as the start model and reason code `planner_<objective>`.
5. After every provider response, record per model: `last_used_at` and `cached[m]` = the prompt size the
   provider reported (input + cache_read + cache_write tokens).
6. Emit one receipt per planned turn, `fast_decisions:turn_planned`, with the objective, `ctx`, each
   option's `{model, warm, cold, cost, time}` and the choice (add it to `EVENT_NAMES`, the privacy
   allowlist of fields, `schemas/event.schema.json` and docs/EVENTS.md).

## Implementation notes

- Keep the math a pure function (e.g. `planner.plan_turn(host, candidates, state, ctx, now, config,
  rates)` returning the options table and the choice) in a new module
  `src/amplifier_fast_decisions/planner.py`; the orchestrator only gathers inputs, records cache state
  and applies the choice. Validate the config in `contracts.py` like the other `model_routing` keys.
- Unit tests for the pure function: fresh session on an Opus host, balanced -> Sonnet (both cold; Sonnet
  writes cheaper and is faster); same session later with Opus warm and 80k tokens cached -> Opus; Fable
  host warm with Sonnet cold at 30k tokens: speed -> Sonnet, cost -> Fable; unknown host price -> abstain;
  TTL expiry makes a model cold. Orchestrator tests: disabled = byte-identical requests and receipts;
  hard/pinned/scope-gated turns untouched; cache state updated from responses; receipt emitted once per
  turn.
- Run the full suite: `PYTHONPATH=src python3 -m unittest discover -s tests`.

## How it will be judged

Benchmark cells with `planner.enabled: true, objective: balanced` on the Opus and Fable hosts, on the S1
single-turn dev split and the `s1m` multi-turn dev split, then a preregistered fresh holdout. Success on the
Opus host: multi-turn cost ratio <= 1.00 vs plain Opus with time <= 0.95, and single-turn no worse than the
shipped default; on the Fable host, no loss against the shipped default.
