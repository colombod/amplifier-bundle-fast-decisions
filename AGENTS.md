# AGENTS.md

**Read `docs/GOAL.md` first: it states what this project must achieve and how success is measured.**

Coding-agent instructions for this repo live in `docs/AGENT-HANDOFF.md` --
read that file before making changes here.

Quick pointers:
- Layout: shared package in `src/amplifier_fast_decisions/`; thin module shims in `modules/*`; bundle composition in `bundle.md`, `behaviors/`, `bundles/`.
- Setup: `uv pip install -e '.[test]'` (or prefix commands with `PYTHONPATH=src`).
- Test command: `python3 -m unittest discover -s tests -v` (or `scripts/smoke_test.sh`, which sets the path for you)
- Health check: `python3 -m amplifier_fast_decisions doctor`
- Bench (offline telemetry analysis + synthetic suite): `afast bench replay <events-dir-or-jsonl> --json`, `afast bench suite [suites/v1.jsonl] --json` -- see `docs/BENCH.md`
- Auto-observatory (session-start viewer bootstrap): stop a running one with `afast serve --stop` (reads `~/.amplifier/fast-decisions/serve.json` by default; `--state-file` to override) -- do this before a manual `afast serve` in the same events dir/port to avoid two servers
- Clean build/cache artifacts (egg-info, __pycache__, build/, .ruff_cache, .DS_Store): `git clean -fdX`
- Key docs: `README.md`, `docs/COMPATIBILITY.md`, `docs/PRIVACY.md`, `docs/ARCHITECTURE.md`

## Project Jev advice (Codex)

Before substantial research, repeating a failed approach, loading several tools or
skills, spawning agents, choosing between materially different execution routes,
or proposing a consequential action, consider whether a small bounded Jev decision
would change the next step. If yes, build a compact state with no secrets, call the
installed router, interpret its action, and continue the original task. Skip Jev
for simple answers, deterministic calculations, routine file edits, and situations
where the call adds no useful decision. Respect `bypass jev`. Keep irreversible
actions behind human confirmation; an existing explicit authorization still applies.

Use `scripts/jev-route --input -` with JSON containing `task`, `context`, and
`options` (two to six named, bounded alternatives). Start in **shadow** mode: this
command gives advice, never executes actions, and never overrides native permission
checks. Independently review the result, do the authorized next step, then record
the host's choice with `--record-outcome ID --action OPTION --evidence REFERENCE`.
For simple tasks, answer directly without invoking any router process; `--skip
simple` exists for auditing that path during setup. On unavailable/uncertain advice,
continue ordinary reasoning; do not retry automatically. No setup calls enter the
SWE-bench arms. Read [docs/CODEX-JEV.md](docs/CODEX-JEV.md) for setup, schema, logs,
privacy and the conditions for considering active integration. The official
TypeSafe skill is installed at `.agents/skills/typesafe-ai/SKILL.md`.
