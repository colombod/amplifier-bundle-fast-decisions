#!/usr/bin/env python3
"""Live end-to-end check of per-delegation model routing (docs/DELEGATION-ROUTING.md).

The unit tests cover the decision in isolation; this runs the real thing.
One `amplifier run` per arm, with this checkout's orchestrator
(PYTHONPATH=src) and `delegation_routing` turned on, in which the parent
makes exactly ONE `delegate` call on a synthetic task. Every assertion is
then read back out of the sessions' OWN `events.jsonl` -- the parent's and
the spawned child's -- because a pin is only real if the child says so
(docs/DELEGATION-ROUTING.md, "How to try it safely", step 3).

Per arm:

    shadow   action == shadow, actual_preference is null, and the child
             still resolves to the anchor: nothing moved.
    enforce  action == adjust -> delegate:agent_spawned's leading
             provider_preference IS the receipt's actual_preference, and the
             child's own provider:resolve reports that provider/model.
             An abstain is recorded with its reason, never as a pass.

Privacy is checked the same way, from the outside: the instruction carries a
marker token, and the marker must appear in NO delegation_routed receipt --
only `instruction_chars`, a count.

The turn router is off (`mode: off`, levers_live.py's pattern) so the only
decision under test is the delegation one. Roles resolve through the
routing matrix pinned to its `anthropic` matrix, so the anchor is one
provider's ladder and not this host's provider mix.

Live and paid: real `amplifier run`, real judge backend, normal API charges.
Always test traffic (AFAST_TRAFFIC=test). Keys come from the caller's
environment and are never read, printed or stored by this script. Nothing
here is collected by the offline lane (`unittest discover -s tests`).

`--cli` and `--amplifier-home` exist because mounting the local module
sources editable-installs them into whichever interpreter runs the CLI
(foundation's `modules/activator.py`, `install_python = sys.executable`),
and on a host whose `amplifier-module-loop-streaming` is an editable
install of its own, that resolution replaces it. Pointing both at throwaway
locations keeps a measurement run from rewriting the machine's Amplifier;
they default to the ones on PATH and `~/.amplifier`, which is the right
thing on a disposable host and the wrong thing on a working one.

    set -a; . ~/.amplifier/keys.env; set +a
    uv venv /tmp/afast-dr-venv
    uv pip install --python /tmp/afast-dr-venv/bin/python \
        "amplifier @ git+https://github.com/microsoft/amplifier@main"
    PYTHONPATH=src python3 scripts/delegation_routing_live.py run \
        --out /tmp/afast-delrouting --cli /tmp/afast-dr-venv/bin/amplifier \
        --amplifier-home /tmp/afast-dr-home
    PYTHONPATH=src python3 scripts/delegation_routing_live.py report \
        --out /tmp/afast-delrouting --amplifier-home /tmp/afast-dr-home
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
# Reused, not reimplemented: `tree_sha256` MUST match the running module's own
# `provenance.tree_sha256` byte for byte, and forge_e2e.py already carries the
# copy that is kept in step with it.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from forge_e2e import git_sha as _git_sha, tree_sha256 as _tree_sha256  # noqa: E402
ROUTING_MATRIX = "git+https://github.com/microsoft/amplifier-bundle-routing-matrix@main"
HOOKS_ROUTING = ROUTING_MATRIX + "#subdirectory=modules/hooks-routing"

# A token the delegated instruction must carry, so "the instruction never
# enters an event" is checked by searching for something that is certainly
# IN the instruction -- not by trusting the absence of text we never knew.
MARKER = "ZEBRAFIN-7742"

# The whole synthetic task: files authored here, about nothing real. Small on
# purpose (the judge reads the INSTRUCTION, not the corpus), so a thorough
# answer stays cheap.
FILES = {
    "docs/retries.md": """# Retries

`retry_max_attempts` -- how many times a failed call is retried. Default: 3.
`retry_backoff_ms` -- base backoff between attempts. Default: 250.
""",
    "docs/cache.md": """# Cache

`cache_ttl_seconds` -- how long an entry stays warm. Default: 600.
`cache_max_entries` -- eviction threshold. Default: 10000.
""",
    "docs/limits.md": """# Limits

`request_timeout_ms` -- per-request deadline. Default: 5000.
`max_concurrency` -- simultaneous in-flight calls. Default: 8.
""",
    "docs/logging.md": """# Logging

`log_level` -- one of debug, info, warn, error. Default: info.
`log_sample_rate` -- fraction of requests logged. Default: 0.1.
""",
    "settings.json": """{
  "retry_max_attempts": 3,
  "retry_backoff_ms": 250,
  "cache_ttl_seconds": 30,
  "request_timeout_ms": 5000,
  "max_concurrency": 64,
  "log_level": "info",
  "audit_trail_enabled": true
}
""",
}

# Standard/thorough depth on purpose: brief-depth delegations abstain by
# design (policy v3, "brief depth was never observed or measured"), so a
# one-lookup task could not tell an abstain-by-policy from a broken seam.
# Measured against the real judge before this was committed: thorough p=0.95.
INSTRUCTION = (
    f"{MARKER} Read every .md file in the docs/ directory of the working directory "
    "(there are four). Build a table mapping each documented setting name to the file "
    "and section that defines it. Then cross-check that table against settings.json in "
    "the working directory and report, with reasons: settings documented but absent from "
    "settings.json, settings present in settings.json but undocumented, and any setting "
    "whose documented default disagrees with the value in settings.json. Finish with a "
    "short prioritised list of the discrepancies that would most likely cause a "
    "production incident, and justify the ordering."
)
PROMPT = (
    "Do not answer this yourself and do not read any file. Make exactly one "
    "delegate tool call, with these arguments and no others: "
    'agent="foundation:explorer", model_role="reasoning", instruction="'
    + INSTRUCTION
    + '". When it returns, reply with only the delegated agent\'s answer.'
)

ARMS = ("shadow", "enforce")


def _profile(run_dir: Path, workspace: Path, events: Path, arm: str, name: str,
             deadline_ms: int, judge: str, home: str | None, max_iterations: int) -> Path:
    """The profile for one arm: `afast configure` (so this never drifts from
    the shipped bundle), then the run-local edits this check needs."""
    from amplifier_fast_decisions.cli import main as afast

    profile = run_dir / "profile.md"
    argv = ["configure", "--bundle-root", str(ROOT), "--workspace", str(workspace),
            "--mode", "active", "--backend", judge, "--events", str(events),
            "--local-sources", "--output", str(profile)]
    if judge == "jev":
        argv.append("--allow-external-state")
    with contextlib.redirect_stdout(io.StringIO()):
        if afast(argv):
            raise RuntimeError("afast configure failed")
    data = json.loads(profile.read_text().split("---")[1])
    config = data["session"]["orchestrator"]["config"]
    # Only the delegation decision is under test: no turn router, no effort
    # routing, no read shortcut (levers_live.py uses the same isolation).
    config.pop("effort_routing", None)
    config.update(mode="off", read_shortcut=False)
    config["delegation_routing"] = {"mode": arm, "policy": "v3", "deadline_ms": deadline_ms}
    # A measurement run is not an agent task: bound the loop so a confused
    # turn cannot spend the budget on iterations nobody reads.
    config["upstream"] = {**(config.get("upstream") or {}), "max_iterations": max_iterations}
    if judge == "jev":
        # `afast configure` leaves the LOCAL decision model in config
        # (DEFAULT_DECISION, e.g. "qwen3:0.6b"), and runtime.py hands
        # config["model"] straight to JevBackend as the Jev model -- which
        # Jev answers with HTTP 400, i.e. every delegation abstains
        # `judge_unavailable`. Dropping it restores the backend default.
        config.pop("model", None)
        # Naming the endpoint explicitly takes JevBackend's stdlib transport
        # (backends.py: "a configured endpoint always takes the stdlib
        # transport"), so this runs without the optional `jev` extra -- and
        # the key is read from the environment, by name, at call time.
        config["jev_url"] = os.getenv("TYPESAFE_BASE_URL", "https://api.typesafe.ai")
        config["jev_key_env"] = "TYPESAFE_API_KEY"
    # Roles have to resolve for a delegation to have an anchor at all
    # (delegation.py `_resolve`: no resolver -> abstain). Pin the matrix to
    # `anthropic` so the anchor ladder is one provider's, not this host's mix.
    data["includes"].insert(1, {"bundle": ROUTING_MATRIX})
    data["hooks"] = [*data.get("hooks", []),
                     {"module": "hooks-routing", "source": HOOKS_ROUTING,
                      "config": {"default_matrix": "anthropic"}}]
    # Every assertion below is read from the sessions' OWN events.jsonl, and
    # the composed profile writes none by default. Same module, same
    # session-only mode and same template forge_e2e.py:710-714 uses, with the
    # home made explicit so it follows --amplifier-home.
    data["hooks"].append({
        "module": "hooks-logging",
        "source": "git+https://github.com/microsoft/amplifier-module-hooks-logging@main",
        "config": {"mode": "session-only",
                   "session_log_template": str(_amplifier_home(home)
                                               / "projects/{project}/sessions/{session_id}/events.jsonl")}})
    data["bundle"]["name"] = name
    profile.write_text("---\n" + json.dumps(data, indent=2) + "\n---\n")
    return profile


def _amplifier_home(home: str | None) -> Path:
    """The same resolution order the runtime uses (AMPLIFIER_HOME, else ~/.amplifier)."""
    raw = home or os.getenv("AMPLIFIER_HOME")
    return Path(raw).expanduser().resolve() if raw else Path.home() / ".amplifier"


def _sessions_dir(workspace: Path, home: str | None = None) -> Path:
    slug = str(workspace.resolve()).replace("/", "-").replace("\\", "-").replace(":", "")
    return _amplifier_home(home) / "projects" / slug / "sessions"


def run_one(out: Path, arm: str, model: str, provider: str, timeout: int,
            deadline_ms: int, judge: str, cli: str, home: str | None,
            max_iterations: int) -> dict:
    run_dir = out / arm
    workspace, events = run_dir / "ws", run_dir / "events"
    (workspace / ".amplifier").mkdir(parents=True, exist_ok=True)
    # The same per-workspace isolation forge_e2e.py uses (forge_e2e.py:783):
    # no app bundles, so this host's installed app bundles cannot compose in.
    (workspace / ".amplifier" / "settings.local.yaml").write_text("bundle:\n  app: []\n")
    for rel, text in FILES.items():
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    name = f"afast-delrouting-{arm}-{int(time.time())}"
    profile = _profile(run_dir, workspace, events, arm, name, deadline_ms, judge, home,
                       max_iterations)
    sessions = _sessions_dir(workspace, home)
    before = set(sessions.iterdir()) if sessions.exists() else set()
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), AFAST_TRAFFIC="test",
               AFAST_OBSERVATORY="off", AMPLIFIER_MEMORY_CAPTURE="off")
    if home:
        env["AMPLIFIER_HOME"] = str(_amplifier_home(home))
    cmd = [cli, "run", "--bundle", profile.as_uri(), "--mode", "single",
           "--provider", provider, "--model", model, "--output-format", "json", PROMPT]
    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=workspace, env=env, capture_output=True,
                              text=True, timeout=timeout, check=False)
        code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        code, stdout, stderr = -1, "", "timeout"
    wall = time.time() - started
    (run_dir / "stdout.txt").write_text(stdout[-20000:])
    (run_dir / "stderr.txt").write_text(stderr[-20000:])
    subprocess.run([cli, "bundle", "remove", name], capture_output=True,
                   text=True, timeout=120, check=False, env=env)
    after = [p for p in sessions.iterdir() if p not in before and (p / "events.jsonl").exists()] \
        if sessions.exists() else []
    meta = {"arm": arm, "exit_code": code, "wall_s": round(wall, 1), "judge": judge,
            "parent_model": model, "parent_provider": provider, "deadline_ms": deadline_ms,
            "amplifier_home": str(_amplifier_home(home)), "session_dirs": sorted(p.name for p in after)}
    (run_dir / "run.json").write_text(json.dumps(meta, indent=2))
    return meta


# --- reading the sessions' own events -----------------------------------

def _events(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def _session_events(run_dir: Path) -> dict[str, list[dict]]:
    meta = json.loads((run_dir / "run.json").read_text())
    sessions = _sessions_dir(run_dir / "ws", meta.get("amplifier_home"))
    return {name: _events(sessions / name / "events.jsonl")
            for name in meta["session_dirs"] if (sessions / name / "events.jsonl").exists()}


def _payload(event: dict) -> dict:
    """The record itself.

    A fast-decisions receipt reaches `events.jsonl` through the native hook
    bridge, which nests the emitted record under its own envelope
    (`data.data`, alongside `event_id`/`decision_id`/`monotonic_ns`). Kernel
    events are already flat. Unwrap one level when that envelope is what we
    are holding, so both read the same.
    """
    data = event.get("data") or {}
    inner = data.get("data")
    if isinstance(inner, dict) and "schema_version" in data:
        return inner
    return data


def _run_source(run_dir: Path, session_id: str | None) -> dict | None:
    for path in sorted((run_dir / "events").glob("*.jsonl")):
        if session_id and not path.name.startswith(session_id):
            continue
        for event in _events(path):
            if event.get("event") == "fast_decisions:source":
                return _payload(event)
    return None


def _first(events: list[dict], name: str) -> dict | None:
    return next((_payload(e) for e in events if e.get("event") == name), None)


def _usd(events: list[dict]) -> float:
    total = 0.0
    for e in events:
        if e.get("event") == "llm:response":
            try:
                total += float(((e.get("data") or {}).get("usage") or {}).get("cost_usd") or 0)
            except (TypeError, ValueError):
                pass
    return total


def _child_effort(events: list[dict]) -> str | None:
    """The reasoning effort the CHILD actually recorded, if it recorded one."""
    for e in events:
        if e.get("event") not in ("llm:request", "provider:request", "session:config"):
            continue
        data = _payload(e)
        for value in (data.get("reasoning_effort"),
                      (data.get("config") or {}).get("reasoning_effort") if isinstance(data.get("config"), dict) else None,
                      (data.get("provider_config") or {}).get("reasoning_effort") if isinstance(data.get("provider_config"), dict) else None):
            if isinstance(value, str):
                return value
    return None


def _pref(preference: dict | None) -> dict | None:
    """The comparable {provider, model, config} shape, from either side."""
    if not isinstance(preference, dict):
        return None
    return {"provider": preference.get("provider"), "model": preference.get("model"),
            "config": dict(preference.get("config") or {})}


def analyze_run(run_dir: Path) -> dict:
    meta = json.loads((run_dir / "run.json").read_text())
    by_session = _session_events(run_dir)

    # The parent is the session that emitted the receipt; the child is the
    # one that delegation names. Both identified from the events, not guessed.
    parent_id = next((sid for sid, events in by_session.items()
                      if any(e.get("event") == "fast_decisions:delegation_routed" for e in events)), None)
    parent = by_session.get(parent_id or "", [])
    receipts = [_payload(e) for e in parent
                if e.get("event") == "fast_decisions:delegation_routed"]
    spawned = [_payload(e) for e in parent if e.get("event") == "delegate:agent_spawned"]
    child_id = spawned[0].get("sub_session_id") if spawned else None
    child = by_session.get(child_id or "", [])
    child_resolve = _first(child, "provider:resolve")
    child_config_seen = any(e.get("event") == "session:config" for e in child)

    receipt = receipts[0] if len(receipts) == 1 else None
    checks = []
    # Which source actually ran. Mounting the local modules pulls the
    # PUBLISHED `amplifier-fast-decisions` in as a dependency, so without
    # this the run could silently measure main instead of the checkout;
    # PYTHONPATH is what makes the checkout win, and the receipt is what
    # proves it did. Same event forge_e2e.py's `_source_observed` reads.
    # `fast_decisions:source` is a recorder receipt, not a bridged session
    # event, so it lands in the run's own events dir rather than
    # events.jsonl. Read the PARENT's file there -- the same receipt
    # forge_e2e.py's `_source_observed` reads.
    source = _first(parent, "fast_decisions:source") or _run_source(run_dir, parent_id) or {}

    def check(cid: str, name: str, passed: bool | None, evidence) -> None:
        checks.append({"id": cid, "name": name, "passed": passed, "evidence": evidence})

    # (a) the parent decided, and the receipt carries the judge's work.
    answers = (receipt or {}).get("answers") or {}
    probabilities = (receipt or {}).get("probabilities") or {}
    check("a_receipt", "parent emitted exactly one delegation_routed with judge answers",
          receipt is not None and bool(answers) and bool(probabilities),
          {"receipts": len(receipts), "session_is_parent": parent_id is not None,
           "answers": answers, "probability_questions": sorted(probabilities),
           "action": (receipt or {}).get("action"), "reason": (receipt or {}).get("reason"),
           "backend": (receipt or {}).get("backend"),
           "anchor": (receipt or {}).get("anchor"),
           "proposed_preference": (receipt or {}).get("proposed_preference"),
           "actual_preference": (receipt or {}).get("actual_preference"),
           "judge_duration_ms": (receipt or {}).get("duration_ms"),
           "decision_latency_ms": (receipt or {}).get("latency_ms")})

    anchor = _pref((receipt or {}).get("anchor"))
    actual = _pref((receipt or {}).get("actual_preference"))
    spawned_pref = _pref((spawned[0].get("provider_preferences") or [None])[0]) if spawned else None
    child_model = (child_resolve or {}).get("model")
    child_provider = (child_resolve or {}).get("provider")

    if meta["arm"] == "shadow":
        # (b) shadow changes nothing: no pin on the receipt, and the child
        # starts on the anchor it would have started on anyway.
        check("b_shadow_records_only", "shadow: action=shadow and actual_preference is null",
              (receipt or {}).get("action") == "shadow" and (receipt or {}).get("actual_preference") is None,
              {"action": (receipt or {}).get("action"),
               "actual_preference": (receipt or {}).get("actual_preference")})
        check("b_child_unchanged", "shadow: child resolved to the anchor, session:config present",
              bool(anchor) and child_model == anchor["model"]
              and child_provider == anchor["provider"] and child_config_seen,
              {"anchor": anchor, "child_provider_resolve": {"provider": child_provider, "model": child_model},
               "child_session_config_present": child_config_seen,
               "spawned_provider_preference": spawned_pref})
    else:
        # (c) enforce: a pin is only real if the tool got it AND the child says so.
        action = (receipt or {}).get("action")
        if action == "adjust":
            check("c_pin_on_call", "enforce/adjust: agent_spawned leading preference == actual_preference",
                  bool(actual) and spawned_pref == actual,
                  {"actual_preference": actual, "spawned_provider_preference": spawned_pref})
            check("c_pin_in_child", "enforce/adjust: child provider:resolve reports the pin",
                  bool(actual) and child_provider == actual["provider"] and child_model == actual["model"],
                  {"actual_preference": actual,
                   "child_provider_resolve": {"provider": child_provider, "model": child_model},
                   "child_session_config_present": child_config_seen})
            # v3's measured lever at thorough depth is effort, which moves
            # `config.reasoning_effort` and leaves provider/model alone -- so
            # provider:resolve alone cannot tell a pinned child from an
            # unpinned one. Whether that part is independently visible in the
            # child depends on what the child records; when it is not, this
            # says so rather than passing on the parent's word.
            pinned_effort = (actual or {}).get("config", {}).get("reasoning_effort")
            observed_effort = _child_effort(child)
            check("c_effort_in_child",
                  "enforce/adjust: child's own records show the pinned reasoning_effort",
                  None if (pinned_effort is None or observed_effort is None)
                  else observed_effort == pinned_effort,
                  {"pinned_reasoning_effort": pinned_effort,
                   "observed_in_child": observed_effort,
                   "note": None if observed_effort is not None else
                   "child recorded no reasoning_effort; pin verified on the call, not inside the child"})
        else:
            # Not a pass and not a failure of the pin path: the policy declined.
            check("c_abstained", "enforce: policy did not adjust -- reason recorded, pin path untested", None,
                  {"action": action, "reason": (receipt or {}).get("reason"),
                   "guard": (receipt or {}).get("guard"), "move": (receipt or {}).get("move"),
                   "lever": (receipt or {}).get("lever"), "anchor": anchor,
                   "proposed_preference": (receipt or {}).get("proposed_preference"),
                   "child_provider_resolve": {"provider": child_provider, "model": child_model}})

    # (e) provenance: the decision that ran is the one in this checkout.
    expected_sha = _git_sha(ROOT)
    expected_tree = _tree_sha256(ROOT / "src" / "amplifier_fast_decisions")
    check("e_source_is_checkout", "the orchestrator that decided is this checkout's source",
          bool(source) and source.get("source_git_sha") == expected_sha
          and source.get("source_tree_sha256") == expected_tree,
          {"observed": {k: source.get(k) for k in ("source_git_sha", "source_tree_sha256", "source_kind")},
           "expected": {"source_git_sha": expected_sha, "source_tree_sha256": expected_tree}})

    # (d) the instruction never enters an event.
    blob = json.dumps(receipts)
    check("d_no_instruction_text", "no instruction text in any delegation_routed event",
          MARKER not in blob and all(r.get("instruction_chars") == len(INSTRUCTION) for r in receipts),
          {"marker_present": MARKER in blob,
           "instruction_chars": [r.get("instruction_chars") for r in receipts],
           "instruction_len": len(INSTRUCTION)})

    cost = round(sum(_usd(events) for events in by_session.values()), 6)
    return {**meta, "parent_session": parent_id, "child_session": child_id,
            "child_agent": spawned[0].get("agent") if spawned else None,
            "child_model_role": spawned[0].get("model_role") if spawned else None,
            "delegations": len(spawned), "cost_usd": cost,
            "sessions_cost_usd": {sid: round(_usd(e), 6) for sid, e in by_session.items()},
            "receipt": receipt, "checks": checks,
            "passed": all(c["passed"] for c in checks if c["passed"] is not None),
            "undetermined": [c["id"] for c in checks if c["passed"] is None]}


def report(out: Path) -> dict:
    runs = [analyze_run(p) for p in sorted(out.iterdir()) if (p / "run.json").exists()]
    summary = {"schema": "delegation-routing-live-v1", "marker": MARKER,
               "instruction_chars": len(INSTRUCTION), "corpus": sorted(FILES),
               "total_cost_usd": round(sum(r["cost_usd"] for r in runs), 6),
               "runs": runs,
               "passed": all(r["passed"] for r in runs) if runs else False}
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--out", required=True)
    run.add_argument("--arms", default=",".join(ARMS))
    run.add_argument("--model", default="claude-sonnet-5", help="the PARENT session's model")
    run.add_argument("--provider", default="anthropic")
    run.add_argument("--judge", default="jev", choices=["jev", "laya", "deterministic"])
    run.add_argument("--deadline-ms", type=int, default=3000)
    run.add_argument("--max-iterations", type=int, default=20)
    run.add_argument("--timeout", type=int, default=900)
    run.add_argument("--cli", default="amplifier",
                     help="amplifier executable; use one in a throwaway venv (see above)")
    run.add_argument("--amplifier-home", default=None,
                     help="AMPLIFIER_HOME for the run; keeps cache/sessions off the real one")
    rep = sub.add_parser("report")
    rep.add_argument("--out", required=True)
    rep.add_argument("--amplifier-home", default=None)
    args = parser.parse_args()
    out = Path(args.out).expanduser().resolve()
    if args.cmd == "run":
        if os.environ.get("AFAST_TRAFFIC", "test") != "test":
            raise SystemExit("AFAST_TRAFFIC must be test for this script")
        out.mkdir(parents=True, exist_ok=True)
        # Serial on purpose: two arms, and each one's assertions are read back
        # from session directories discovered by diffing that arm's own project.
        for arm in args.arms.split(","):
            print(json.dumps(run_one(out, arm, args.model, args.provider, args.timeout,
                                     args.deadline_ms, args.judge, args.cli,
                                     args.amplifier_home, args.max_iterations)), flush=True)
    print(json.dumps(report(out), indent=2))


if __name__ == "__main__":
    main()
