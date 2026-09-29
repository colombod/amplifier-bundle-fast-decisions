> The selector now defaults to local Laya with external state disabled. Use
> `backend: jev` plus consent to reproduce the historical Jev measurements below.
> The portable CLI is `amplifier-fast-decisions cua --input ui.json`.
> Laya snapshots exceeding its state bound abstain instead of silently truncating controls.

# Jev computer-use decisions

Experimental, opt-in. The configured judge (local Laya by default) selects an operation and a compatible observed control
in one batched request. The host supplies current UI evidence, preserves its
approval rules, executes the action, and independently verifies completion.

## Enable in an Amplifier bundle

```yaml
includes:
  - bundle: fast-decisions:behaviors/jev-cua
```

This registers `jev_cua` through the normal native tool path. Including the
behavior sends sanitized, scoped UI text to the local Laya service. Explicitly
configuring `backend: jev` and `allow_external_state: true` permits the TypeSafe
path using `TYPESAFE_API_KEY`. It is separate from the default Jevgrep behavior because browser
and desktop state need their own scope. No Cua package or browser extension is
installed by this behavior. The host must already have a computer-use tool.

Call with an observation from that host:

```json
{
  "goal": "Open the weekly report",
  "snapshot": {
    "surface_id": "reports-tab",
    "revision": "page-state-17",
    "text": "Reports. Nothing selected.",
    "elements": [
      {"id": "weekly", "role": "button", "label": "Weekly report", "operations": ["CLICK"]},
      {"id": "monthly", "role": "button", "label": "Monthly report", "operations": ["CLICK"]}
    ]
  }
}
```

The tool returns a typed proposal and usage/latency receipt. It never executes a
click. Bind IDs to actual observed controls; do not let the model invent selectors,
coordinates, JavaScript, keypresses, or text. Snapshot revisions must change when
native target identity or state changes. IDs may be stable DOM IDs or freshly
observed native references, not guessed references. Recheck the snapshot and the
15-second expiry before execution, including after an approval wait.

The selector supports CLICK, SELECT from observed option IDs, WAIT, DONE, and
TYPE_TEXT as a request for host-generated text. Missing evidence, low selected
probability, malformed replies, unexpected model identity, and timeouts yield to
reasoning. DONE is a hypothesis until the host verifies the goal. Protected controls
are omitted, but the caller must also sanitize the goal and summary text.

## Skip generative calls between steps

Calling a selector from a reasoning loop can add overhead. The `run` driver can
instead handle a bounded sequence directly:

```python
from amplifier_fast_decisions.jev_cua import CuaSelector, run

selector = CuaSelector()  # local Laya; external sharing disabled
try:
    result = await run(selector, goal, host, max_steps=12, record=record_receipt)
finally:
    await selector.close()
```

`host` implements four async methods:

| Method | Responsibility |
| --- | --- |
| `observe()` | Return a scoped snapshot with fresh, visible control references. |
| `approve(action)` | Apply native authorization; return exactly `True` to allow. |
| `execute(action, observation)` | Revalidate native references and execute through the host; return `True` only on success. Bound I/O and preserve cancellation. |
| `verify(goal, observation)` | Independently establish every goal requirement; return exactly `True` for completion. |

The driver makes no generative calls. It returns to its caller on unsupported
actions, text generation, denial, stale observations, failed verification, repeated
unchanged actions, or the step limit. Application errors propagate; cancellation
does not become a retry. The caller decides how to continue with its regular model.

For trycua, `cua_host.TryCuaHost` bridges an existing `Computer.interface` to
CLICK/WAIT. Supply `observe_controls()` returning `(snapshot, {id: (x, y)})`, plus
the host's `approve` and `verify` callbacks. Coordinates must come from visible,
unobscured controls in the authorized surface. They stay local; their hash is bound
to the revision to catch movement. The adapter calls the documented
`interface.left_click(x=..., y=...)` API. It currently rejects SELECT and TYPE_TEXT.
There is no universal desktop accessibility-tree parser: OS-specific visibility,
occlusion, focus and secret filtering remain the observer's responsibility.

## Evidence and measurement boundary

The native tool emits metadata-only `fast_decisions:cua_decided` events, including
model, selected operation, probability, status, duration, and reported usage. The
driver's `record` callback receives metadata only. Neither claims a counterfactual
cost or saved model call. `judge_calls` counts logical invocations; the underlying
transport may reconnect. A timeout leaves usage unknown.

Live check on the Field study website: Jev 1.13.0 selected the “Repair with the
local Laya judge” tab from four observed tabs in **236.891 ms**, using 1,009 input
and 111 output tokens. The native browser tool executed its choice, and fresh
accessibility state confirmed the selected tab and its visible result panel.
Two earlier proposals were not executed: one freshness check incorrectly expected
a full observation from a diff, and the next detected replaced native references
after dashboard refresh. The check was corrected to use full observations and
resolve stable observed DOM IDs to fresh native references. Those extra calls are
validation overhead, not claimed savings.

That first check has now been followed by [real Amplifier sessions through
Forge](evidence/2026-09-29-forge/VERIFICATION.md). A bounded three-click Chromium
workflow used 3 generative calls versus 9, with correct outcomes in two repetitions
per arm. Its measured mean wall time was 23.82 s versus 28.96 s. Native approval,
staleness, timeout, repeated-action and verification-failure paths were exercised.

The full Foundation selector session also exposed a limitation: relaying the
proposal through another reasoning turn exceeded its 15-second expiry. The host
correctly refused execution. Prefer the bounded host driver for sequences;
do not lengthen the validity window to accommodate unnecessary reasoning delays.

The acceptance host used real Playwright Chromium through `TryCuaHost`'s interface,
not a trycua VM. A real trycua VM and general desktop workflows remain unverified.
Jev charges were not available, so lower generative-provider estimates are not a
total-dollar savings claim. Jev-CUA is not added to the frozen SWE-bench campaign.

## Patterns adopted

- [browser-use/jev-ultrafast at 1231850](https://github.com/browser-use/jev-ultrafast/tree/1231850a0bf1a0c0341fe408ef1668dbbfdfac46): observed action spaces, batched operation/target questions, consuming only the selected target answer, and a separate text-generation path.
- [trycua/cua at 22456aa](https://github.com/trycua/cua/tree/22456aa5913aba58d90028709ede571a369f15e5): a separate computer interface that owns observation and execution.
- [TypeSafe speculative fan-out](https://docs.typesafe.ai/patterns/fan-out): ask compatible branch questions together and consume the relevant answer in code.

Both repositories are MIT licensed. This is an original implementation of those
patterns, with no upstream source vendored and no upstream speed claims inherited.
