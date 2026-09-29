# Per-delegation routing — live end-to-end check, September 29, 2026

**Two real `amplifier run` sessions delegated once each through this branch's
`delegation.py`. In `enforce`, the policy moved a `claude-opus-5-5` delegation
from `reasoning_effort: high` to `low`, and the pin appears on the delegate call.
The child confirms the provider/model, but its effort is undetermined. In
`shadow`, the same judge answers produced the
same proposal and the child still started on the untouched anchor. The delegated
instruction appears in no receipt. Two arms, one delegation each — this is a
mechanism check, not a measurement of whether the policy's moves are good.**

[Machine-readable evidence](summary.json) · [Environment](environment.json) ·
Harness: [`scripts/delegation_routing_live.py`](../../../../scripts/delegation_routing_live.py)

## Why this exists

`tests/test_delegation_routing.py` covers the decision in isolation, and the
paid A/B behind `docs/DELEGATION-ROUTING.md` measured a standalone prototype.
Neither runs the code that actually ships in the delegate tool mapping. This
does: a real parent session, a real `delegate` call, a real judge, and a real
child session, with every assertion read back from the sessions' own
`events.jsonl` rather than from configuration.

## What ran

One `delegate` call per arm, `agent="foundation:explorer"`,
`model_role="reasoning"`, on a synthetic four-document corpus created inside the
run workspace (`docs/retries.md`, `docs/cache.md`, `docs/limits.md`,
`docs/logging.md`, `settings.json`) with a 615-character cross-checking
instruction. Nothing outside the run workspace was read. The turn router was off
(`mode: off`), so the only decision under test is the delegation one. Roles
resolved through the routing matrix pinned to its `anthropic` matrix, making the
anchor a single provider's ladder.

| | shadow | enforce |
|---|---|---|
| judge answers | `answer_depth: thorough` (p 0.94), `min_tier: mid` (p 0.95) | `answer_depth: thorough` (p 0.93), `min_tier: mid` (p 0.95) |
| anchor | anthropic `claude-opus-5-5`, effort `high` | anthropic `claude-opus-5-5`, effort `high` |
| lever / move | `effort` / `down` | `effort` / `down` |
| proposed | effort `low` | effort `low` |
| **actual** | **`null`** | **effort `low`** |
| action | `shadow` | `adjust` |
| judge call | 215.6 ms | 297.1 ms |
| exit / wall / cost | 0 / 57.9 s / $0.3240 | 0 / 41.0 s / $0.2820 |

The two arms agreeing on the proposal and differing only in `actual_preference`
is the point: it separates "the policy decided" from "the decision was applied".

## Assertions

Every check below is computed in `analyze_run` from the parent's and child's
`events.jsonl`, and the evidence quoted here is the `evidence` block of the
corresponding check in `summary.json`.

| Check | shadow | enforce |
|---|---|---|
| `a_receipt` — exactly one `fast_decisions:delegation_routed`, carrying the judge's answers and per-question probabilities | PASS | PASS |
| `b_shadow_records_only` — `action: shadow`, `actual_preference: null` | PASS | n/a |
| `b_child_unchanged` — child resolved to the anchor; `session:config` present | PASS | n/a |
| `c_pin_on_call` — `delegate:agent_spawned` leading preference **is** `actual_preference` | n/a | PASS |
| `c_pin_in_child` — child's own `provider:resolve` reports that provider/model | n/a | PASS |
| `c_effort_in_child` — child's records show the pinned `reasoning_effort` | n/a | **UNDETERMINED** |
| `d_no_instruction_text` — marker absent from every receipt; `instruction_chars` equals the real length | PASS | PASS |
| `e_source_is_checkout` — the orchestrator that decided is this worktree | PASS | PASS |

Load-bearing evidence, verbatim from `summary.json`:

```
enforce  actual_preference          {"provider":"anthropic","model":"claude-opus-5-5","config":{"reasoning_effort":"low"}}
         spawned_provider_preference{"provider":"anthropic","model":"claude-opus-5-5","config":{"reasoning_effort":"low"}}
         child_provider_resolve     {"provider":"anthropic","model":"claude-opus-5-5"}

shadow   actual_preference          null
         anchor                     {"provider":"anthropic","model":"claude-opus-5-5","config":{"reasoning_effort":"high"}}
         spawned_provider_preference{"provider":"anthropic","model":"claude-opus-5-5","config":{"reasoning_effort":"high"}}
         child_provider_resolve     {"provider":"anthropic","model":"claude-opus-5-5"}

both     instruction_chars 615, instruction_len 615, marker_present false
         source_git_sha cb5fb0085bf234a48434cfbb9f936c784d68345c, source_kind worktree
```

## Honest limits

- **`c_effort_in_child` is UNDETERMINED, not a pass.** v3's lever at thorough
  depth is effort, which moves `config.reasoning_effort` and leaves
  provider/model alone — so `provider:resolve`, which records provider and model
  only, cannot by itself distinguish a pinned child from an unpinned one. The
  pin is proven **on the call** (`delegate:agent_spawned` carries
  `reasoning_effort: low`) and the child is proven to start on that
  provider/model; that the child's *own* records never echo the effort is
  recorded as undetermined rather than counted as verified.
- **n = 1 delegation per arm.** Two sessions. This demonstrates the mechanism
  works end to end; it says nothing about how often the policy adjusts in real
  use, or whether its moves preserve answer quality. The quality question is
  `docs/DELEGATION-ROUTING.md`'s A/B, which failed its own pre-registered
  measure.
- **Jev was the judge.** The measured checkout predates #49's restoration of
  Jev as the default. Laya was configured in that checkout but was not
  reachable on the measurement host: no `laya`/`laya_mlx`/`torch` importable,
  and nothing serving `/v1/decide` on loopback port 8090. These two runs used Jev
  with `allow_external_state` enabled in the run-local profile only. No Laya
  quality claim follows from these runs.
- **One task, one role, one anchor.** `model_role="reasoning"` on the
  `anthropic` matrix, so only the effort lever was exercised. The model-down,
  provider-down, up, guard and capability-conflict paths were not.
- **Costs are provider-reported estimates**, summed from `llm:response` usage
  across both sessions — not billing. $0.6060 for the two reported runs;
  $0.9501 including one discarded run (below).
- **The child's answer was not graded.** Whether the explorer's audit was
  correct is out of scope here; the question was whether the routing decision
  reached it.

## One discarded run

The first live run ($0.3441) abstained `judge_unavailable` on every delegation.
Cause: `afast configure` leaves the local decision model in the orchestrator
config (`model: qwen3:0.6b`), and `runtime.py` hands `config["model"]` straight
to `JevBackend` as the Jev model, which Jev answers with HTTP 400. The harness
now drops that key for the Jev backend. The run is not reported above because
its delegation never reached the policy; it is counted in the cost total.

## Reproducing

The harness mounts this checkout's modules, which editable-installs them into
whichever interpreter runs the CLI. On a host whose
`amplifier-module-loop-streaming` is itself an editable install, that resolution
**replaces it**, so the run here was pointed at a throwaway venv and a throwaway
`AMPLIFIER_HOME`; the host's own Amplifier was verified byte-identical
afterwards.

```sh
uv venv /tmp/afast-dr-venv
uv pip install --python /tmp/afast-dr-venv/bin/python \
    "amplifier @ git+https://github.com/microsoft/amplifier@main"
set -a; . ~/.amplifier/keys.env; set +a
PYTHONPATH=src python3 scripts/delegation_routing_live.py run \
    --out /tmp/afast-delrouting --cli /tmp/afast-dr-venv/bin/amplifier \
    --amplifier-home /tmp/afast-dr-home
PYTHONPATH=src python3 scripts/delegation_routing_live.py report \
    --out /tmp/afast-delrouting --amplifier-home /tmp/afast-dr-home
```
