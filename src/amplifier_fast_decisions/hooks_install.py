"""Install / uninstall the Claude Code waste-guard hook in a settings.json.

``afast hooks install claude-code [--settings PATH]`` adds three hook groups
(PreToolUse, PostToolUse + PostToolUseFailure, Stop + SubagentStop) that run
``python -m amplifier_fast_decisions.claude_hook``; every other setting and
hook is kept. Idempotent: our entries are recognized by the module name and
replaced, never duplicated. ``uninstall`` removes only ours.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shlex
import sys
from pathlib import Path

MARKER = "amplifier_fast_decisions.claude_hook"
DEFAULT_SETTINGS = Path.home() / ".claude" / "settings.json"
_EVENTS = {
    "PreToolUse": ("pre", "Bash|Read|Grep|Glob"),
    "PostToolUse": ("post", "*"),
    "PostToolUseFailure": ("post", "*"),
    "Stop": ("stop", None),
    "SubagentStop": ("stop", None),
}


def hook_command(mode: str, python: str | None = None) -> str:
    python = python or sys.executable
    command = f"{shlex.quote(python)} -m {MARKER} {mode}"
    spec = importlib.util.find_spec("amplifier_fast_decisions")
    origin = Path(spec.origin).resolve() if spec and spec.origin else None
    if origin and "site-packages" not in str(origin):
        # Running from a checkout: point the hook's interpreter at it.
        command = f"PYTHONPATH={shlex.quote(str(origin.parent.parent))} {command}"
    return command


def _ours(group: dict) -> bool:
    return any(MARKER in str(h.get("command", "")) for h in group.get("hooks", []) if isinstance(h, dict))


def install(settings_path: str | os.PathLike | None = None, *, python: str | None = None,
            dry_run: bool = False) -> dict:
    path = Path(settings_path or DEFAULT_SETTINGS).expanduser()
    settings = json.loads(path.read_text()) if path.exists() and path.read_text().strip() else {}
    if not isinstance(settings, dict):
        raise ValueError(f"{path} is not a JSON object")
    hooks = settings.setdefault("hooks", {})
    for event, (mode, matcher) in _EVENTS.items():
        groups = [g for g in hooks.get(event, []) if isinstance(g, dict) and not _ours(g)]
        group = {"hooks": [{"type": "command", "command": hook_command(mode, python), "timeout": 10}]}
        if matcher:
            group = {"matcher": matcher, **group}
        groups.append(group)
        hooks[event] = groups
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".afast-tmp")
        tmp.write_text(json.dumps(settings, indent=2) + "\n")
        os.replace(tmp, path)
    return settings


def uninstall(settings_path: str | os.PathLike | None = None, *, dry_run: bool = False) -> dict:
    path = Path(settings_path or DEFAULT_SETTINGS).expanduser()
    if not path.exists():
        return {}
    settings = json.loads(path.read_text() or "{}")
    hooks = settings.get("hooks") or {}
    for event in list(hooks):
        kept = [g for g in hooks[event] if not (isinstance(g, dict) and _ours(g))]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    if not dry_run:
        tmp = path.with_suffix(path.suffix + ".afast-tmp")
        tmp.write_text(json.dumps(settings, indent=2) + "\n")
        os.replace(tmp, path)
    return settings
