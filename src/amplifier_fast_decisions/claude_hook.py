"""Claude Code hook: the same deterministic waste guards as the Amplifier
orchestrator (guards.py), writing the same efficiency receipts into the
fast-decisions events dir, tagged ``harness: "Claude Code"``.

Install with ``afast hooks install claude-code`` (see docs/WASTE-GUARDS.md);
Claude Code then runs, per tool call::

    python -m amplifier_fast_decisions.claude_hook pre    # PreToolUse
    python -m amplifier_fast_decisions.claude_hook post   # PostToolUse / PostToolUseFailure
    python -m amplifier_fast_decisions.claude_hook stop   # Stop / SubagentStop

What differs from Amplifier: a Claude Code hook cannot run a command in
place, so here the block guards *deny* a call (the model sees the reason):
repeated identical calls, repeated identical failures, and sleep-poll loops.
The unchanged-output pointer applies to Bash only, replacing the finished
output via ``updatedToolOutput`` (Claude Code already shortens unchanged file
re-reads itself). Each model call's cost is read from the session
transcript's usage records.

Privacy: state files keep only hashes, counts and the first line of an error
(0600, under ``~/.amplifier/fast-decisions/claude-code-state``); events carry
tool names, reason codes and receipt numbers, never commands or outputs.
Never blocks on its own failure: any error prints nothing and exits 0.
"""
from __future__ import annotations

import json
import os
import pickle
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import efficiency
from .guards import GuardConfig, WasteGuard
from .privacy import safe_data

HARNESS = "Claude Code"
_TAIL_BYTES = 768 * 1024


def _home() -> Path:
    return Path(os.environ.get("AFAST_HOME") or Path.home() / ".amplifier" / "fast-decisions")


def events_dir() -> Path:
    return Path(os.environ.get("AFAST_EVENTS_DIR") or _home() / "events").expanduser()


def state_dir() -> Path:
    return Path(os.environ.get("AFAST_CC_STATE_DIR") or _home() / "claude-code-state").expanduser()


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name or "unknown")[:80]


def _project(cwd: str | None) -> str:
    if not cwd:
        return "(unknown)"
    try:
        from .observer import repo_context
        return repo_context(Path(cwd)).get("repo") or Path(cwd).name
    except Exception:  # noqa: BLE001
        return Path(cwd).name or "(unknown)"


def _config() -> GuardConfig:
    raw = os.environ.get("AFAST_WASTE_GUARDS")
    value: Any = None
    if raw:
        if raw.strip().lower() in ("0", "off", "false", "no"):
            value = False
        else:
            try:
                value = json.loads(raw)
            except ValueError:
                value = None
    config = GuardConfig.from_config(value)
    config.identical_results = False  # switched on per call for Bash only (see handle)
    return config


def _pointer_enabled() -> bool:
    return os.environ.get("AFAST_CC_POINTER", "on").strip().lower() not in ("0", "off", "false", "no")


def load_guard(key: str, cwd: str | None) -> tuple[WasteGuard, dict]:
    path = state_dir() / f"{_safe(key)}.pkl"
    try:
        with path.open("rb") as handle:
            guard, meta = pickle.load(handle)
        if isinstance(guard, WasteGuard) and isinstance(meta, dict):
            guard.config = _config()
            return guard, meta
    except Exception:  # noqa: BLE001
        pass
    guard = WasteGuard(_config(), harness=HARNESS, project=_project(cwd),
                       traffic=efficiency.classify_traffic(cwd), cwd=cwd)
    guard.assume_followup_call = True
    return guard, {"seen_ids": [], "last_user": None, "seq": 0}


def save_guard(key: str, guard: WasteGuard, meta: dict) -> None:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{_safe(key)}.pkl"
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        pickle.dump((guard, meta), handle)
    os.replace(tmp, path)


def sync_transcript(guard: WasteGuard, meta: dict, transcript: str | None) -> None:
    """Feed the guard the model calls (and new user turns) recorded in the
    transcript since the last hook run."""
    if not transcript:
        return
    try:
        with open(transcript, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _TAIL_BYTES))
            data = handle.read().decode("utf-8", "replace")
    except OSError:
        return
    seen = meta.setdefault("seen_ids", [])
    seen_set = set(seen)
    first_sync = not seen and meta.get("last_user") is None
    for line in data.splitlines():
        if '"assistant"' not in line and '"user"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        msg = entry.get("message") if isinstance(entry.get("message"), dict) else None
        if not msg:
            continue
        if entry.get("type") == "user" and _is_human(msg) and not entry.get("isMeta"):
            uid = entry.get("uuid")
            if uid and uid != meta.get("last_user"):
                meta["last_user"] = uid
                if not first_sync:
                    guard.new_turn()
            continue
        if entry.get("type") != "assistant":
            continue
        mid = msg.get("id")
        if not mid or mid in seen_set or msg.get("model") == "<synthetic>":
            continue
        seen_set.add(mid)
        seen.append(mid)
        if first_sync:
            continue  # history before the hook was installed is not replayed
        u = msg.get("usage") or {}
        cache_read = int(u.get("cache_read_input_tokens") or 0)
        usage = {"input_tokens": int(u.get("input_tokens") or 0) + cache_read,
                 "output_tokens": int(u.get("output_tokens") or 0), "cache_read_tokens": cache_read,
                 "cache_write_tokens": int(u.get("cache_creation_input_tokens") or 0)}
        guard.note_model_call(model=msg.get("model"), usage=usage, cost_usd=None, seconds=None)
    if first_sync and seen:
        # count the steps so far without pricing them
        guard.step = len(seen)
    del seen[:-400]


def _is_human(msg: dict) -> bool:
    content = msg.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") == "text" for b in content) and not any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return False


def write_events(session_id: str, meta: dict, events: list[tuple[str, dict]]) -> None:
    if not events:
        return
    directory = events_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"claude-code-{_safe(session_id)}.jsonl"
    lines = []
    for kind, data in events:
        meta["seq"] = meta.get("seq", 0) + 1
        lines.append(json.dumps({
            "schema_version": "1.0", "event_id": uuid4().hex, "event": "fast_decisions:" + kind,
            "session_id": session_id, "parent_session_id": None, "turn_id": None, "decision_id": None,
            "seq": meta["seq"], "timestamp": datetime.now(timezone.utc).isoformat(),
            "monotonic_ns": time.monotonic_ns(), "synthetic": False, "data": safe_data(data),
        }, sort_keys=True, separators=(",", ":")))
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _response_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if "stdout" in value or "stderr" in value:
            return "\n".join(str(value.get(k) or "") for k in ("stdout", "stderr"))
        if isinstance(value.get("file"), dict):
            return str(value["file"].get("content") or "")
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _failed(payload: dict) -> bool:
    if payload.get("hook_event_name") == "PostToolUseFailure" or payload.get("error"):
        return True
    resp = payload.get("tool_response")
    if isinstance(resp, dict):
        if resp.get("is_error") or resp.get("interrupted"):
            return True
        code = resp.get("exit_code", resp.get("returncode"))
        if isinstance(code, int) and code != 0:
            return True
    return False


def handle(mode: str, payload: dict) -> dict | None:
    """Process one hook invocation; returns the JSON to print (or None)."""
    session_id = str(payload.get("session_id") or "unknown")
    agent = payload.get("agent_id")
    key = session_id + (f"-{agent}" if agent else "")
    cwd = payload.get("cwd")
    guard, meta = load_guard(key, cwd)
    if not guard.config.enabled:
        return None
    sync_transcript(guard, meta, payload.get("agent_transcript_path") or payload.get("transcript_path"))
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    out = None
    events: list[tuple[str, dict]] = []
    if mode == "pre" and tool:
        decision = guard.before(tool, tool_input)
        if decision.action == "wait":
            decision.message = (
                "[fast-decisions] Not run: this is a repeated sleep-poll of the same check; each poll costs a full "
                "model step. Wait on the condition in ONE command instead, e.g. "
                "`timeout 600 bash -c 'until <check>; do sleep 10; done'; <check>` (or run the job in the "
                "background and wait for it). (If you really need this exact poll, issue it once more and it will run.)")
            guard._pend_block(decision.key, "poll_wait", 0)
            decision.action = "block"
        if decision.action == "block":
            events.append(("waste_guard", {"action": "block", "reason_code": decision.guard, "tool": tool,
                                           "harness": HARNESS}))
            out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                          "permissionDecisionReason": decision.message}}
    elif mode == "post" and tool:
        resp = payload.get("tool_response")
        # The unchanged-output pointer only for Bash, whose output shape
        # (stdout/stderr) can be replaced in place via updatedToolOutput.
        guard.config.identical_results = tool == "Bash" and isinstance(resp, dict) and "stdout" in resp \
            and payload.get("hook_event_name", "PostToolUse") == "PostToolUse" and _pointer_enabled()
        replacement = guard.after(tool, tool_input, _response_text(resp or payload.get("error")),
                                  _failed(payload), call_id=payload.get("tool_use_id"))
        guard.config.identical_results = False
        if replacement:
            events.append(("waste_guard", {"action": "pointer", "reason_code": "identical_result", "tool": tool,
                                           "harness": HARNESS}))
            out = {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                          "updatedToolOutput": {**resp, "stdout": replacement, "stderr": ""}}}
    elif mode == "stop":
        flushed = guard.end_turn()
        guard.receipts.extend(flushed)
    for data in guard.take_receipts():
        events.append(("efficiency", data))
    write_events(session_id, meta, events)
    save_guard(key, guard, meta)
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = argv[0] if argv else "pre"
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        out = handle(mode, payload if isinstance(payload, dict) else {})
        if out:
            sys.stdout.write(json.dumps(out))
    except Exception:  # noqa: BLE001 -- a hook failure must never block the agent
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
