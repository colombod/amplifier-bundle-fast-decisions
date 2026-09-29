# Jev advice in this Codex project

Codex can call `scripts/jev-route` before a useful bounded decision. Normal task
prompts are sufficient: the project `AGENTS.md` instructs the agent to consider
the router. This is an instruction-driven integration, not interception of every
Codex model call. The router never executes commands or grants permission.

## Installed components

- Official skill: `.agents/skills/typesafe-ai/SKILL.md`, complete directory copied
  from [TypeSafe's repository](https://github.com/typesafe-ai/skills/tree/65a39f393687675ce170e6094757de20370365b9/skills/typesafe-ai),
  revision `65a39f393687675ce170e6094757de20370365b9`. This is the only installed
  copy of this skill for the setup. Provenance is in `.agents/typesafe-install.json`.
- Official Python SDK: `typesafe-sdk==0.7.2` in this project's `.venv`.
- Callable adapter: `scripts/jev-route` and `scripts/jev_router.py`.
- Persistent instructions: the appended **Project Jev advice (Codex)** section of
  `AGENTS.md`; previous instructions are preserved.
- Private decision log: `.amplifier/jev-router/decisions.jsonl` (gitignored).

The SDK environment is separate from Amplifier, Laya, the standalone smart tool,
and the pinned SWE-bench runtime. It does not add a treatment to benchmark arms.
For a fresh checkout, create the environment and install the pinned SDK:

```sh
python3 -m venv .venv
.venv/bin/pip install -r scripts/jev-router-requirements.txt
```

Install the complete official skill directory into `.agents/skills/typesafe-ai`
using the [official manual method](https://docs.typesafe.ai/agent-skill).
Do not also install a duplicate global copy. Skill files and the virtual
environment are local installation artifacts, not committed bundle dependencies.

## Credential handling

The existing TypeSafe key was reused without displaying or copying it. The wrapper
prefers `TYPESAFE_API_KEY` already in its environment; otherwise it sources the
existing `~/.amplifier/keys.env` privately in its child process. It does not modify
that file. No action is needed while that key remains valid.

If replacing the key, create it at <https://console.typesafe.ai/keys>. In a private
macOS zsh terminal, use a hidden prompt rather than putting the value in a command:

```sh
read -rs 'TYPESAFE_API_KEY?TypeSafe API key: '; printf '\n'
export TYPESAFE_API_KEY
```

Launch Codex from that terminal to inherit it, or update the existing private
credential file with an editor. Never paste the key into chat or commit it.

## Calling and interpreting the router

First decide whether advice could change the next step. Skip simple answers,
exact calculations, routine edits, and requests containing `bypass jev`. For
example, answer “2 + 2” directly as **4**; no router process or API is needed.

For a real choice, summarize only the necessary public facts. Supply two to six
named alternatives; the script adds `abstain`. It sends at most 6,000 bytes and
one narrow Choice question, with no automatic retry. It never reads workspace
files to construct model state. Its credential-pattern check is only a backstop,
not a substitute for the caller removing secrets and private source content.

```sh
scripts/jev-route --input - <<'JSON'
{
  "task": "Choose the next diagnostic step for a stopped benchmark",
  "context": "Agent execution succeeded, but one judge request has no usage receipt.",
  "options": {
    "inspect_receipts": "Inspect recorded judge calls and accounting logic",
    "check_docker": "Check Docker availability",
    "repeat_run": "Repeat the whole paid agent run"
  }
}
JSON
```

Add `--dry-run` to validate without network access. The output includes the actual
model, choice, probabilities, confidence, duration, token usage and receipt ID.
Choice confidence describes the returned distribution; it is not measured
correctness or authorization. SDK failure, invalid output or missing credentials
returns `unavailable`, not a fabricated decision. Continue ordinary reasoning.

Review the shadow recommendation independently, perform only the authorized
host action, then attach a short evidence reference:

```sh
scripts/jev-route --record-outcome RECEIPT_ID \
  --action inspect_receipts --evidence campaign-cost-reconciliation
```

The log retains metadata, option IDs and a state hash, not the task/context,
option descriptions, API key or raw errors. Host outcome entries are explicitly
host-reported; they do not prove that the router executed anything. Actual shell
invocations in the Codex session provide the independent action evidence.

## Verification performed

- Offline dry run: state validated, zero API calls.
- Simple-task path: deterministic answer and a separate audit-only `--skip simple`
  receipt, zero API calls.
- Real diagnostic task: Jev selected `inspect_receipts` (confidence 1.0,
  545 input tokens, 359 ms including SDK startup). Codex then inspected the actual
  stopped run and accounting function. It identified a decision timeout whose
  unknown usage triggered the campaign guard. No paid run was retried.
- Repeated documentation-reader failure: Jev selected `direct_official`
  (confidence 0.99, 500 input tokens, 314 ms). Codex fetched the known official
  response-contract page with Python and retained a local evidence copy.
- Six unit tests cover skip/bypass/dry-run, bounded input, failure redaction,
  metadata-only receipts and private append behavior. These are contract tests,
  not evidence of model accuracy.
- Full repository suite: 1,492 tests run, successful, two optional skips;
  isolated SDK dependency check also passed.
- Fresh Codex session through Forge: an ordinary diagnostic prompt did not mention
  Jev. The session answered 2 + 2 before any tool use, discovered the new `AGENTS.md`
  instructions, invoked the router, received `review_cost_receipts` (probability
  0.88, confidence 0.83, 345 ms), inspected the campaign's root receipts, and
  recorded its host outcome. The private transcript is
  `.amplifier/jev-router/fresh-session.jsonl`; decision ID
  `d1c6448e-3869-4783-af94-e8361a03c2a6`. This documentation file was still being
  written during that test, so the child inspected the router scripts for the
  contract. It did discover and follow the persistent instructions independently.

Live calls returned `jev-1.13.0` using the installed official SDK. These results
establish that this agent invoked Jev and continued a real task. They do not
establish net speed or cost savings. SDK import time and a separate shell call
can outweigh a tiny decision's benefit.

## When to consider active integration

Keep this adapter in shadow mode. It deliberately has no active execution flag.
Moving a specific decision into an automatic workflow requires representative
held-out quality checks, measured end-to-end savings including overhead, explicit
abstention/failure behavior, and native approval/execution controls. Preserve
user authorization and `bypass jev`; do not globally replace reasoning or tune
thresholds on the running SWE-bench results.

Three ordinary prompts to try:

1. “Investigate why the benchmark stopped and choose the next diagnostic step.”
2. “This test has failed twice. Find the cause before repeating it.”
3. “Find where prepared-action eligibility is decided and choose the most useful
   tests for a change there.”

For a fresh-session check, start Codex in this repository and use the first
prompt without mentioning Jev. Inspect the session's actual tool calls and the
new decision/outcome receipts. Adding an instruction alone cannot guarantee that
every future agent turn will use a tool.
