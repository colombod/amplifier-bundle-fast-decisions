---
{
  "smart_tool_format": 1,
  "name": "amplifier-fast-decisions",
  "version": "0.1.0",
  "description": "Use Jev for bounded read/list decisions, source relevance search, and proposals on observed UI controls. The host keeps execution and approval authority; uncertain decisions abstain.",
  "use_cases": [
    "Choose among caller-validated workspace read targets",
    "Measure a local decision scorer independently of an agent harness",
    "Record advisory decisions alongside parent and child session metadata",
    "Find relevant source windows in an unfamiliar directory",
    "Propose an action on caller-observed UI controls without executing it"
  ],
  "platforms": [
    "macos",
    "linux",
    "windows"
  ],
  "requires": [
    {
      "name": "TypeSafe Jev",
      "purpose": "Default remote judge; TYPESAFE_API_KEY and external-state consent required. Search also requires jg.",
      "optional": true,
      "install": "https://github.com/michaeljabbour/amplifier-bundle-fast-decisions/blob/main/docs/MODEL-SETUP.md"
    },
    {
      "name": "Jevgrep 0.4.0 (jg)",
      "purpose": "Default bounded source retrieval; experimental Laya retrieval separately requires ripgrep.",
      "optional": true,
      "install": "https://github.com/dzhng/jevgrep"
    }
  ]
}
---

The library `amplifier_fast_decisions.smart_tool` holds every capability. The
CLI works from any coding-agent harness (Claude Code, Codex, OpenCode,
Amplifier, ...) with shell access. No Amplifier runtime is imported by the
portable path. The CLI and deterministic/fixture contracts are checked on
macOS, Linux, and Windows. Real local-model latency has been exercised on
macOS; other hardware and Ollama installations need their own measurements.

Use this when the caller already has eligible read/list targets. It does not
decide arbitrary commands, invent tool arguments, perform compaction, or
intercept a harness's provider loop. It returns a suggestion; the caller owns
eligibility checks, approvals, execution and evaluation of the result.

## Installation and setup

Install the local dependency with the tool:

```bash
uv tool install 'amplifier-fast-decisions[local] @ git+https://github.com/michaeljabbour/amplifier-bundle-fast-decisions@main'
# Set TYPESAFE_API_KEY privately in the harness environment.
amplifier-fast-decisions manifest
amplifier-fast-decisions install-skill --host all
amplifier-fast-decisions select --help
```

During development use `uv tool install --editable '.[local]'` from this checkout.
`install-skill --host codex|claude|amplifier|opencode|all` adds a minimal discovery skill
to the selected user catalogs, with no overwrite of modified existing files.
Laya is experimental and requires explicit --backend laya and a separately started server. The optional
Ollama backend requires native token log probabilities. Loopback calls need no API key;
remote Laya requires HTTPS and explicit external-state consent. A missing model is an explicit
failure, never a scripted substitute. No web server starts as a side effect.

For a shared authenticated Laya endpoint, set `FAST_DECISIONS_LAYA_URL` and
`FAST_DECISIONS_LAYA_TOKEN` in the harness environment, and pass
`--allow-external-state` to each operation. See
[hosted deployment and onboarding](../../docs/HOSTED-LAYA.md).

Jev uses the existing TypeSafe backend and requires explicit external-state
consent. Set `TYPESAFE_API_KEY` in the process environment; never put it in a
request file. The optional SDK is not required: the stdlib transport is supported.

```bash
amplifier-fast-decisions select --backend jev --allow-external-state --input request.json
```

`--backend` overrides `FAST_DECISIONS_JUDGE` (`laya`, `local`/`ollama` or `jev`; default
jev). `--allow-external-state` and `--no-allow-external-state` override
`FAST_DECISIONS_ALLOW_EXTERNAL_STATE` (default false). Jev sends the bounded task,
context and candidate descriptions to TypeSafe; without consent no external
backend is constructed. The key is used only by the backend's authorization
header. `--model` defaults to `qwen3:0.6b` locally, or `TYPESAFE_DEFAULT_MODEL` /
`jev-1.13.0` for Jev. Actual returned model identity is preserved, including when
the requested name is an alias. Backend failures never switch to another model.

## Calling from any harness

Ask for `describe` to obtain the full input contract. For example, `request.json`:

```json
{"task":"Read README.md, not LICENSE.md.","candidates":[{"id":"readme","operation":"read","path":"README.md"},{"id":"license","operation":"read","path":"LICENSE.md"}],"session_id":"example-parent","harness":"other"}
```

```bash
amplifier-fast-decisions select --allow-external-state --input request.json
```

`select` returns JSON with `status: selected` and an offered `choice`, or
`status: abstain`. Check `ok`: a false value means failure and a nonzero CLI
exit. Ordinary model/policy abstention is a valid result. No target is read or
executed. Only invoke the model when that judgment is useful; calling it from
an already-running reasoning model adds a call and does not by itself save one.

For library composition, pass data directly:

```python
from amplifier_fast_decisions.smart_tool import select
result = await select({"task": "Read README.md", "candidates": [
    {"id": "readme", "operation": "read", "path": "README.md"}
]}, allow_external_state=True)
```

Optional `context` is a bounded string containing caller-provided material.
Do not have an agent rewrite it to inflate confidence. No target existence,
file permission, or symlink claim is established by this advisory tool.

## Evidence and limits

The default judge is Jev 1.13.0 with explicit external-state consent. Selection uses a 500 ms scoring deadline,
score threshold 0.90 and margin 0.20. Results preserve each backend's
`probability_kind` and `confidence_kind`; the numbers are not calibrated
correctness probabilities or necessarily comparable statistics across backends.
The current workload is bounded workspace-action selection.

Each invocation appends metadata to a new JSONL file under
`~/.amplifier/fast-decisions/events`, `AFAST_EVENTS_DIR`, or explicit `--events`
(the explicit argument wins). Task/context/target
paths are not logged. Caller-supplied candidate IDs and session IDs are logged:
use opaque identifiers, not secrets. Provide `session_id`, optional
`parent_session_id`, and `harness` to preserve caller lineage. These are caller
assertions, not independently verified session identity. There is no default
claim that the caller is an active session of any particular harness.

Events use `mode: advisory` and `event_source: portable-smart-tool`. A model
score proves scoring occurred. It proves neither execution nor a provider call
avoided. No fast-submission or tool-execution events are invented. For automatic
Amplifier interception use the separately configured active bundle and native
approval path. The portable interface is an advisory library/CLI surface.

Laya preserves its model-reported confidence when present; Ollama reports `not_reported`. Jev preserves
its own reported confidence semantics. `option_set_hash` is an order-sensitive
digest of the backend's presented options and ID bindings. The digest records neither actual execution nor correctness, and
is not a hash of the complete request.

## Operational evidence

Use `diagnose` to inspect installation, effective session configuration, local model
and authenticated viewer health. Use `measure --session PARENT_ID` for execution
counts including children. Use `compare --input runs.json` for paired baseline/enabled
runs with explicit outcome checks. These capabilities invoke no model.
`diagnose --offline` also disables loopback health probes. Each command's `--help`
describes its arguments; JSON statuses, coverage and eligibility determine what
its results establish. A successful command exit alone does not prove improvement.

## Local retrieval and computer-use decisions

`search --allow-external-state --input query.json --root PUBLIC_WORKSPACE` uses
upstream Jevgrep (`jg`) with bounded source sharing. Ignore rules, source/output
limits, hidden/sensitive-path exclusions and containment checks apply. Install
`jg` as described in docs/JEVGREP.md. Experimental `--backend laya` uses a
separate local relevance implementation and requires `rg`.

`cua --allow-external-state --input ui.json` batches operation and target selection through Jev.
It returns a proposal or a reason to hand back to the host; it never clicks, types,
or verifies completion itself. Fresh state and native approvals remain mandatory.
Use `search --help` and `cua --help` for exact inputs and bounds.

Both capabilities are Python library calls in `smart_tool` and need no Amplifier runtime.
`install-skill --host opencode` also supports OpenCode's ~/.config/opencode/skills directory.
