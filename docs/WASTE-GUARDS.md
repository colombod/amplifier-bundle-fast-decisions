# Waste guards

Deterministic per-tool-call checks that remove wasted model steps, in Amplifier (the `loop-fast-decisions`
orchestrator) and in Claude Code (a PreToolUse/PostToolUse hook). Code: `src/amplifier_fast_decisions/guards.py`
(shared core), `waste.py` (classifiers shared with the census), `claude_hook.py`, `hooks_install.py`.
What they target and why: `docs/evidence/2026-09-25/waste-census/`.

## The guards

| Guard | Fires when | What the model gets | Lever / mechanism |
|---|---|---|---|
| Repeat stop | the same call returned byte-identical output its last 3 runs, nothing changed in between (no write, no mutating command, no helper, no new user message) | the call is not run; "use the earlier result" | `loop_stop` / `guard:identical_repeat` |
| Retry stop | the same command failed twice in a row with the same error, nothing changed in between; errors that read as transient (lock, rate limit, timeout, network) are exempt | the command is not run; "change approach" + the error line | `loop_stop` / `guard:error_retry` |
| Poll wait | the 2nd `sleep N && <read-only check>` of the same check within 3 model steps (or, with a background job running, a 4th identical time-varying check) | Amplifier: the check is re-run in place at the model's own interval until its output changes, at most 240 s (the prompt cache stays warm); Claude Code: the call is denied with "wait on the condition in one command" | `loop_stop` / `guard:poll_wait` |
| Unchanged output | a read, search or command returns exactly the output of the same call that is still in context, or a file read asks for lines an earlier read of the unchanged file (same size, mtime, content hash) already returned; at least 600 chars | a one-line pointer instead of a second copy (the call still runs first) | `context_rightsize` / `guard:identical_result` |

Override: every guard yields to the model. A blocked call runs normally if the model issues it again right
away; after a pointer, the next identical call returns the full output. Guards reset on every new user turn,
never raise (an internal error means the call runs as usual), and never bypass approvals: they act after the
native `tool:pre` hooks, and poll wait only re-runs a read-only command that was already approved.

Must-not-fire cases covered by tests (`tests/test_waste_guards.py`): a test re-run after an edit, a command
re-run after any mutating command, a file re-read after it changed, a check whose output changed, three
explicitly requested runs, a blocking `until ...; do sleep N; done` wait, transient errors, results that left
the context (compaction), small outputs.

## Configuration

On by default in `behaviors/fast-decisions.yaml`, in every session and every repo (the scope gate controls model
routing only). The code default is off, so old profiles and tests are unchanged.

```yaml
session:
  orchestrator:
    config:
      waste_guards: {enabled: true, repeat_stop: true, retry_stop: true, poll_wait: true,
                     identical_results: true, poll_max_wait_s: 240}
```

Turn all off with `waste_guards: {enabled: false}` (e.g. in `~/.amplifier/settings.yaml` overrides for
`loop-fast-decisions`), or one at a time. Every field of `guards.GuardConfig` can be set here.

## Receipts (exactly recomputable)

Each firing emits a `fast_decisions:efficiency` receipt (plus a `fast_decisions:waste_guard` event with the tool
name and reason code, never arguments or output). `usd_saved`, `calls_saved` and `seconds_saved` are fixed in
the receipt, and `detail` carries the numeric inputs, so `afast efficiency` totals are plain sums:

* **Blocks** (repeat, retry, Claude Code poll): baseline = the waste-census median of further repeats after the
  point where the guard fires (repeats 2, retries 1, polls 1; the smaller of the last-30-days and all-history
  medians) x the cost and seconds of the live model call that issued the blocked call. Claimed only once the
  model has made two more calls without re-issuing it. An override (the model re-issues and it runs) is
  charged as one extra model call at the live step cost (negative savings).
* **Poll wait** (Amplifier): polls run in place minus one, x the live model call's cost and seconds.
* **Pointers**: elided characters / 4 tokens, priced as one cache write on the next model call plus a cache
  read on each later model call of the same turn, at list prices for the model that served them. Nothing is
  claimed if no model call followed.

Receipts carry `harness` (`Amplifier` or `Claude Code`), `project` and `traffic`; the dashboard's Efficiency
ledger shows them by lever, project and harness.

## Claude Code

```bash
afast hooks show claude-code                     # print the hooks it would add
afast hooks install claude-code                  # edits ~/.claude/settings.json (keeps everything else)
afast hooks install claude-code --settings .claude/settings.json   # one project only
afast hooks uninstall claude-code                # removes only these hooks
```

Or add it by hand (use the interpreter that can import `amplifier_fast_decisions`; from a checkout, prefix the
command with `PYTHONPATH=<checkout>/src`):

```json
{
  "hooks": {
    "PreToolUse":         [{"matcher": "Bash|Read|Grep|Glob", "hooks": [{"type": "command", "command": "python3 -m amplifier_fast_decisions.claude_hook pre",  "timeout": 10}]}],
    "PostToolUse":        [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 -m amplifier_fast_decisions.claude_hook post", "timeout": 10}]}],
    "PostToolUseFailure": [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 -m amplifier_fast_decisions.claude_hook post", "timeout": 10}]}],
    "Stop":               [{"hooks": [{"type": "command", "command": "python3 -m amplifier_fast_decisions.claude_hook stop", "timeout": 10}]}],
    "SubagentStop":       [{"hooks": [{"type": "command", "command": "python3 -m amplifier_fast_decisions.claude_hook stop", "timeout": 10}]}]
  }
}
```

Differences from Amplifier: a hook can only deny a call or replace a finished Bash output, so poll wait becomes a
denial with guidance, and the unchanged-output pointer applies to Bash only (via `updatedToolOutput`; Claude
Code already shortens unchanged file re-reads itself). Model-call costs come from the session transcript's usage
records. State (hashes, counts, the first line of an error) lives in `~/.amplifier/fast-decisions/claude-code-state`
(0600); receipts go to `~/.amplifier/fast-decisions/events/claude-code-<session>.jsonl`. Environment:
`AFAST_WASTE_GUARDS=off` (or a JSON GuardConfig), `AFAST_CC_POINTER=off`, `AFAST_EVENTS_DIR`,
`AFAST_TRAFFIC=test`. Each hook run is a short Python start (about 50-100 ms per tool call).

## Measuring

* Census: `PYTHONPATH=src python3 evals/waste_census.py --out docs/evidence/<date>/waste-census/summary.json`
* Live A/B: `python3 evals/waste_guards_live.py run --keys-env ~/.amplifier/keys.env --out <dir outside repos> --reps 3`
  then `... report --out <dir>` (always `AFAST_TRAFFIC=test`).
