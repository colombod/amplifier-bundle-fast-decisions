# Registry mode: fast-decisions without replacing the orchestrator

`behaviors/fast-decisions.yaml` replaces the session orchestrator with `loop-fast-decisions`. Hosts that own their
loop cannot accept that. Amplifier Unified runs `loop-live` and refuses a custom orchestrator ("custom
orchestrators are not silently replaced"), so every Unified chat failed to start with that behavior installed.

`behaviors/fast-decisions-registry.yaml` keeps the host's loop and attaches the same decisions through the
`hooks-fast-decisions-router` hook module (`src/amplifier_fast_decisions/registry.py`).

## Why nothing is lost

`HybridOrchestrator.execute()` does not steer the loop itself. It opens a turn, hands the upstream loop a
`RoutedProvider` around each provider and an `ObservedTool` around each tool, and closes the turn. Every lever
lives in those two facades:

| Lever | Facade |
|---|---|
| Turn-start model and effort routing, cheaper-model steps, cache keep-alive | `RoutedProvider` |
| Prepared actions (answering a step without a model call), easy-turn shaping | `RoutedProvider` |
| Repeat stop, retry stop, poll wait, unchanged-output pointers, loop-stop notes | `ObservedTool` + `tool:post` |

Registry mode mounts the same facades once, in the coordinator's live `providers` and `tools` registries. Every
host passes those registries to its loop (`amplifier_core` `session.execute()`), and tool dispatch looks tools up
in that dict, so the facades see every loop step. Hook modules mount after providers and tools (the kernel's
documented load order), and re-mounting a name replaces the entry, as verified on the Rust coordinator.

## Turn boundaries

| Loop event | Router |
|---|---|
| `execution:start` | End any turn left open, wrap anything mounted since the last turn, begin a turn |
| `orchestrator:complete` | End the turn. A `/goal` pursuit ends on its final emission (`goal_final`), like one `execute()` |
| `execution:end` | Noted; backfilled at turn end when the loop skipped it (early return, cancellation, error) |
| `session:end`, module cleanup | End any open turn |

`loop-live` keeps one `execute()` open for a whole chat and starts an upstream turn per message, so these events
bound each message. In orchestrator mode a Unified chat would have been one "turn".

## What is routed

Only loop steps are routed. Under registry mode `RoutedProvider` passes these calls to the provider unchanged:

- calls outside a turn, such as session naming between turns;
- calls without tools, such as session naming and compaction summaries (every loop step advertises its tools);
- calls with a caller-supplied `model` keyword, such as Unified's model picker. **An explicit selection wins**:
  neither model nor effort changes. Tool guards still apply.

A hook cannot mark which task a call comes from: the Rust hook dispatcher runs Python handlers in a separate
context, so a `ContextVar` set in a hook is not visible to the loop.

## Differences from orchestrator mode

- `fast_decisions:turn_start` follows `execution:start` instead of preceding it (docs/UPSTREAM_CONTRACT.md).
- A provider a host captured before the router wrapped it stays unrouted, which fails safe to plain Amplifier.
  Unified builds its model selection after hooks mount, so it wraps the routed facade.
- Host wrappers added after the router (loop-live's `AsyncTool`, Unified's `PersistentDelegate`) are detected and
  not wrapped twice.

## Verification

- `tests/test_registry_mode.py`: attachment, pass-through rules, turn lifecycle, and config parity with
  `behaviors/fast-decisions.yaml`. Its real-kernel lane runs the installed `loop-streaming` and `loop-live`
  unmodified, including a repeated `git status` that the repeat stop blocks on its fourth run.
- End to end in Amplifier Unified 0.20.35 (`work` bundle, `loop-live`, isolated data directory): one tool-using
  turn produced `turn_start` with `engine: registry:amplifier_module_loop_live`, a Jev difficulty judgment, both
  model calls served by `claude-sonnet-5`, the `bash` call observed, efficiency receipts, and `turn_end: ok`.
