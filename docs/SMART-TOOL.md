> Current default: local Laya. `select`, `search`, and `cua` are callable from
> Codex, Claude Code, Amplifier and OpenCode. See the
> [live checks and limitations](evidence/2026-09-29-laya/VERIFICATION.md).
> `search` is bounded local relevance scoring, not upstream Jevgrep on Laya.
> `cua` is proposal-only; host observation, approval and execution are still required.

# Portable Fast Decisions Smart Tool

The portable surface is a Python library plus CLI that any coding-agent
harness (Claude Code, Codex, OpenCode, Amplifier, ...) can call. It asks a
real local model or Jev to select among caller-supplied read/list candidates and
returns a typed suggestion or abstention. It does not require Amplifier or
any other specific harness to be running.

This implements the library/CLI Smart Tool boundary now. Automatic interception
remains harness-specific: the Amplifier active bundle can substitute a prepared
tool envelope at a provider boundary. A CLI suggestion invoked from an existing
Claude or Codex model turn does not by itself remove that turn or save a call.

## Install and discover

From a checkout containing this implementation:

```bash
uv tool install --editable '.[local]'
amplifier-fast-decisions --help
amplifier-fast-decisions manifest
amplifier-fast-decisions install-skill --host all
amplifier-fast-decisions describe
```

After this revision is published, the equivalent git installation is:

```bash
uv tool install 'amplifier-fast-decisions[local] @ git+https://github.com/michaeljabbour/amplifier-bundle-fast-decisions@main'
```

The installer supports `--host codex`, `claude`, `amplifier`, `opencode`, or `all`. It writes
the minimal skill to `~/.agents/skills`, `~/.claude/skills`, or
`~/.amplifier/skills` respectively, beneath `amplifier-fast-decisions/`.
All destinations are checked before writing; changed existing skills cause a
failure and are preserved. Identical skills are unchanged. Symlinked aliases
that resolve to one destination are deduplicated.
The Codex and Claude locations follow their
[Codex skill docs](https://developers.openai.com/codex/skills) and
[Claude Code skill docs](https://code.claude.com/docs/en/skills); Amplifier uses
its installed user skill catalog location.

The repository also includes `skills/amplifier-fast-decisions/SKILL.md` for
manual import using a harness's Agent Skill installation mechanism. It only
introduces the command and directs the agent to its current `--help`; the full
guidance ships inside the installed tool. Skill discovery and command
availability are separate: ensure the harness can find `amplifier-fast-decisions`
on PATH, then start a fresh session if its skill catalog is cached.

| Caller | Available integration |
|---|---|
| Claude Code | Load the included Agent Skill, then invoke the CLI through its permitted shell tool. |
| Codex | Load the included Agent Skill, then invoke the same CLI through its permitted command tool. |
| Amplifier | Use the skill/CLI for explicit advisory calls, or configure the active bundle for native provider-boundary acceleration. |
| Python application | Import `manifest`, `describe`, `skill`, and async `select` from `amplifier_fast_decisions.smart_tool`. |
| Another harness | Call the CLI with JSON on stdin or a file; MCP is not required or currently shipped. |

Harness approvals still apply. Installing this skill does not modify a host's
tool-selection, pruning, or provider pipeline. End-to-end Claude/Codex task
acceptance must be checked in those hosts; shell/library compatibility alone is
not evidence that their models chose to use it.

### Verified harnesses (2026-09-20)

The published package (`git+https://github.com/michaeljabbour/amplifier-bundle-fast-decisions@main`,
commit `56799fb`) was installed with `uv tool install` exactly as a user would,
then driven one at a time through each harness below, launched via Forge, with
an identical prompt asking the harness to run `manifest` then the deterministic
`describe` capability and echo the result. This confirms callable Smart Tool
access -- the harness runs the CLI through its own shell/bash tool -- which is
distinct from Amplifier's native active-bundle provider-boundary integration
described elsewhere on this page.

| Harness | Invoked tool? | Manifest name/version echoed? | Capability JSON valid? | Exit code |
|---|---|---|---|---|
| Claude Code | Yes | `amplifier-fast-decisions` / `0.1.0` | Yes | 0 |
| Codex | Yes | `amplifier-fast-decisions` / `0.1.0` | Yes | 0 |
| OpenCode | Yes | `amplifier-fast-decisions` / `0.1.0` | Yes | 0 |
| Amplifier (`amplifier run --mode single`) | Yes | `amplifier-fast-decisions` / `0.1.0` | Yes | 0 |

Full commands, raw outputs, and per-harness notes (model-availability retries,
Forge terminal polling) are in the harness-smoke evidence directory.

## A bounded call

The public `select` interface supports `--backend local` (alias `ollama`) and
`--backend jev`. The default comes from `FAST_DECISIONS_JUDGE`, otherwise Laya.
Jev requires `--allow-external-state` or `FAST_DECISIONS_ALLOW_EXTERNAL_STATE=true`
and `TYPESAFE_API_KEY` in the environment. Explicit flags override environment
defaults; `--no-allow-external-state` denies an external call even when the
environment permits it. The consent check happens before backend construction.
No repository key file is read. All ordinary input bounds and advisory-only
semantics are unchanged. Failure never silently uses the other backend.

```bash
amplifier-fast-decisions select --backend jev --allow-external-state --input request.json
```

Both backends use their existing prompts and return their native probability
semantics. A Jev response may resolve `jev-latest` to a versioned model; that
actual identity and usage are retained. Model scores are not calibrated accuracy.
`AFAST_EVENTS_DIR` supplies the default event directory. `FAST_DECISIONS_OLLAMA_URL`
and `FAST_DECISIONS_LOCAL_MODEL` configure the local backend when corresponding
explicit arguments are absent. These options enable ordinary harness deployment;
they do not add automatic Codex interception or guarantee useful adoption.

Start Ollama, pull `qwen3:0.6b`, and warm it as described in
[model setup](MODEL-SETUP.md). Save this public example as `request.json`:

```json
{
  "task": "Read README.md, not LICENSE.md. Give a one-sentence summary.",
  "candidates": [
    {"id": "readme", "operation": "read", "path": "README.md"},
    {"id": "license", "operation": "read", "path": "LICENSE.md"}
  ],
  "session_id": "example-child",
  "parent_session_id": "example-parent",
  "harness": "other"
}
```

```bash
amplifier-fast-decisions select --input request.json --timeout-ms 500
```

The targets need not exist for scoring because the portable tool never opens
them. The caller must offer only eligible candidates and independently validate
the selected target before performing an action. Arbitrary commands, write
operations, hidden/absolute/traversing paths, extra fields, and oversized input
produce failed abstention. Optional `context` contains actual bounded text; it
is not a path to read.

Inspect `ok` and `status`. A selection returns an offered ID, native token
probabilities, model identity, timing and correlation IDs. Low confidence or
model abstention is a valid result. A missing backend, invalid response or
recording failure returns `ok: false`, `status: abstain`, a reason and remedy,
and a nonzero CLI exit. No deterministic fallback pretends to be a model.

For chaining capabilities, use the library's typed result:

```python
from amplifier_fast_decisions.smart_tool import select

result = await select(payload, timeout_ms=500)
if result.ok and result.status == "selected":
    candidate_id = result.choice  # advisory; validate/approve in your harness
```

## What the Observatory can establish

Each invocation writes an append-only metadata file in the usual events
directory, or the explicit `--events` directory. Caller-provided session and
parent IDs preserve lineage; use opaque IDs rather than private names. These
identities are caller assertions. A one-shot CLI call does not mark a parent
session started, alive, or closed.

The events carry `mode: advisory` and `event_source: portable-smart-tool`:

- `requested` records the count and opaque IDs of the offered alternatives.
- `scored` records an actual validated model response and its timing.
- `health` with `phase: advisory_result` records the selected/abstain outcome.
- `cancelled` records caller cancellation when scoring was underway.

Task/context text and target paths are omitted. No `routed:fast`, `tool_start`,
or `tool_end` event is invented; there is no claim of an avoided provider call,
successful tool action, or measured savings from these advisory receipts.

## Verification and limits

The implementation follows the
[Amplifier Smart Tools specification](https://github.com/microsoft/amplifier-smart-tools/tree/70432044f26e2094b5894516adab68aa14f88592/spec)
at commit `70432044f26e2094b5894516adab68aa14f88592`.
Its installed CLI passed all 16 conformance checks with provider configuration
scrubbed, using the console entry installed by `uv tool install`. The editable
installation includes the packaged `SMART_TOOL.md`; deterministic help and schema
work without Amplifier or a configured model. Discovery skills were installed
locally for Codex, Claude Code, and Amplifier without overwriting existing skills.

Eleven portable contract tests cover metadata privacy and lineage, unsupported
input, uncertainty, backend failure, invalid model output, deadlines,
cancellation, recorder failure, deterministic help/description/manifest, and
skill-install idempotence, conflict preflight and symlink deduplication.
On September 17, 2026, a real local Qwen call on the public example selected
`readme`: 211.75 ms scoring wall time, 318.19 ms for the full library invocation
including recorder shutdown. This is one Mac observation, not a p95 estimate;
no file was read and no provider call was demonstrated to be avoided.
Three later calls through the installed executable, with one parent and two
child identities, selected the expected public README target. Full process wall
times were 493.2, 251.9, and 266.9 ms, including CLI startup and recorder shutdown;
scoring took 260.8, 74.3, and 81.8 ms respectively. These are three observations,
not a workload p95 or an accuracy guarantee. Their advisory telemetry appeared
in the shared Observatory without incrementing the bypass counter.

The manifest supports macOS, Linux, and Windows. CI installs the package and
runs its deterministic and fixture-backed contracts on all three operating
systems; actual local-model timings were measured on macOS. Real task use and
model placement in each host still need workload-specific validation. The 500 ms deadline covers scoring/queue
wait, not process launch or recorder shutdown. Token scores remain uncalibrated.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p test_smart_tool.py -v
```

To rerun the upstream conformance kit, obtain the pinned spec checkout and run
its `conformance/run.py` against this distribution root with the installed CLI
available on PATH. The kit checks packaging and invocation contracts; it does
not establish model accuracy or automatic acceleration.

The spec does not require a full agent runtime inside every smart tool. Its
[overview](https://github.com/microsoft/amplifier-smart-tools/blob/70432044f26e2094b5894516adab68aa14f88592/README.md)
permits tools to bundle harnesses or agents, and its
[structure chapter](https://github.com/microsoft/amplifier-smart-tools/blob/70432044f26e2094b5894516adab68aa14f88592/spec/structure.md)
leaves the model and code/inference mix to the implementation. Here the tool
owns the classifier prompt, candidate encoding, Ollama call, response
validation, thresholds, abstention and receipts. The caller states a task and
offers allowed targets. No calling agent has to load the classifier's domain
prompt or conduct its reasoning on the tool's behalf.

## Decision evidence and shutdown

Successful local selections include `confidence_kind: not_reported` and an
`option_set_hash` of the backend's ordered option presentation, including abstention
and the mapping from letters to caller IDs. Reordering or changing a rendered target
changes the hash. The hash is metadata, not a disclosure of paths or a complete
request fingerprint. Token mass is not calibrated correctness.

Recorder shutdown now wakes an idle writer immediately instead of waiting for a
100 ms queue poll. The library flushes in a worker thread so other asynchronous
callers remain responsive. This retains the existing recording-failure behavior;
it does not turn best-effort telemetry into a durable audit log. Native Amplifier
hook delivery and optional Emitter callbacks still run inline; isolating those
observers is a separate integration change.
