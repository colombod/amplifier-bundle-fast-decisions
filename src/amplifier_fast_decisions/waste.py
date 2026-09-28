"""Deterministic classifiers for wasted agent steps.

Shared by the offline waste census (``evals/waste_census.py``) and the
production waste guards (``guards.py``) so the census and the guards agree on
what counts as a read-only command, a sleep-poll, a repeated failure or a
file read. Pure functions over tool names, tool inputs and result text; no
I/O, never raises.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from typing import Any

# Tool names across harnesses (Amplifier foundation tools, Claude Code tools).
READ_TOOLS = frozenset({"read_file", "Read"})
EDIT_TOOLS = frozenset({"write_file", "edit_file", "apply_patch", "Write", "Edit", "MultiEdit", "NotebookEdit",
                        "str_replace_editor", "create_file"})
SEARCH_TOOLS = frozenset({"grep", "glob", "Grep", "Glob", "LS", "list_dir", "ls"})
BASH_TOOLS = frozenset({"bash", "Bash", "shell"})
AGENT_TOOLS = frozenset({"delegate", "task", "Task", "Agent", "spawn_agent"})
# Tool calls that neither change the workspace nor launch work.
PASSIVE_TOOLS = frozenset({"todo", "TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "ToolSearch",
                           "load_skill", "Skill", "recipes", "web_search", "WebSearch", "web_fetch", "WebFetch",
                           "BashOutput", "TaskOutput", "fast_workspace"})

_READONLY_VERBS = frozenset({
    "ls", "cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "ag", "wc", "sort", "uniq", "cut", "tr", "jq",
    "yq", "echo", "printf", "pwd", "which", "type", "file", "stat", "du", "df", "tree", "less", "more", "diff",
    "cmp", "basename", "dirname", "realpath", "readlink", "date", "ps", "pgrep", "lsof", "uptime", "id", "whoami",
    "hostname", "uname", "test", "[", "true", "column", "nl", "od", "xxd", "hexdump", "md5", "md5sum", "shasum",
    "sha256sum", "comm", "fold", "rev", "seq", "env", "printenv", "jobs", "wait", "sleep", "nproc", "free",
    "vm_stat", "sw_vers", "top", "netstat", "ss", "dig", "nslookup", "host", "tac", "strings", "zcat", "bat",
})
_GIT_READONLY = frozenset({"status", "log", "diff", "show", "branch", "rev-parse", "ls-files", "blame", "describe",
                           "grep", "shortlog", "reflog", "ls-tree", "cat-file", "rev-list", "merge-base",
                           "for-each-ref", "name-rev", "whatchanged", "count-objects", "check-ignore",
                           "ls-remote", "worktree"})
_GH_READONLY = frozenset({"view", "list", "status", "checks", "diff", "search"})
# Commands whose output changes with time or external state; repeating them
# unchanged is polling, never a duplicate.
_TIME_VARYING = frozenset({"ps", "pgrep", "lsof", "tail", "date", "uptime", "jobs", "top", "gh", "curl", "docker",
                           "kubectl", "netstat", "ss", "free", "vm_stat", "wait", "sleep", "du", "df", "ls",
                           "test", "[", "stat", "wc", "cat", "grep", "find", "head"})
_SEGMENT_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")
_REDIRECT_TO_FILE = re.compile(r"(?<![0-9&])>{1,2}\s*(?!/dev/null|&)[^\s|;&]+|\b[0-9]>{1,2}\s*(?!/dev/null|&)[^\s|;&]+")
_SLEEP = re.compile(r"(?:^|[\s;&|(])sleep\s+([0-9]*\.?[0-9]+)([smh]?)\b")
_LOOP = re.compile(r"\b(until|while)\b[^\n]*\bdo\b", re.S)


def _segments(command: str) -> list[str]:
    return [s.strip() for s in _SEGMENT_SPLIT.split(command or "") if s and s.strip()]


def _words(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _verb(words: list[str]) -> tuple[str, list[str]]:
    """First real command word, skipping env assignments and wrappers."""
    i = 0
    while i < len(words) and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[i]) or words[i] in
                              ("time", "command", "nice", "nohup", "timeout", "exec", "builtin", "(", "{")):
        if words[i] == "timeout" and i + 1 < len(words):
            i += 1  # skip the duration too
        i += 1
    if i >= len(words):
        return "", []
    verb = words[i].rsplit("/", 1)[-1].lstrip("({")
    return verb, words[i + 1:]


def normalize_command(command: str) -> str:
    return re.sub(r"\s+", " ", (command or "").strip())


def _segment_readonly(segment: str) -> bool:
    words = _words(segment)
    verb, args = _verb(words)
    if not verb:
        return True
    if verb in ("cd", "export", "set"):
        return True
    if verb == "git":
        sub = next((a for a in args if not a.startswith("-")), "")
        if sub in ("remote", "config", "stash", "tag"):
            return sub == "remote" and ("-v" in args or len(args) <= 1) or sub == "config" and (
                "--get" in args or "--list" in args or "-l" in args) or sub == "tag" and (
                "-l" in args or "--list" in args or len(args) <= 1) or sub == "stash" and "list" in args
        return sub in _GIT_READONLY
    if verb == "gh":
        return any(a in _GH_READONLY for a in args[:3]) or (args[:1] == ["api"] and not any(
            a in ("-X", "--method", "-f", "-F", "--input") for a in args))
    if verb == "kill":
        return bool(args) and args[0] == "-0"  # a liveness probe, sends no signal
    if verb == "find":
        return not any(a in ("-delete", "-exec", "-execdir", "-ok", "-fprint") for a in args)
    if verb == "sed":
        return "-i" not in args and not any(a.startswith("-i") for a in args)
    if verb == "awk":
        return "system(" not in segment
    if verb in ("curl", "wget"):
        return verb == "curl" and not any(a in ("-X", "--request", "-d", "--data", "-F", "--form", "-T", "-o",
                                                "-O", "--upload-file") or a.startswith("--data") for a in args)
    if verb in ("docker", "kubectl", "podman"):
        return bool(args) and args[0] in ("ps", "logs", "inspect", "images", "get", "describe", "top", "stats",
                                          "version", "info")
    if verb in ("npm", "pnpm", "yarn", "pip", "pip3", "uv", "cargo", "go"):
        return bool(args) and args[0] in ("ls", "list", "show", "view", "outdated", "why", "freeze", "tree",
                                          "version", "env", "doctor")
    if verb in ("python", "python3") and args[:1] == ["-c"]:
        return False
    return verb in _READONLY_VERBS


def is_readonly_command(command: str) -> bool:
    """True when every segment of a shell command only reads (no redirect
    into a file, no tee, no mutating verb). Loops count as read-only when
    their body is."""
    if not command or not command.strip():
        return False
    if _REDIRECT_TO_FILE.search(command) or re.search(r"\btee\b", command):
        return False
    body = re.sub(r"\b(until|while|do|done|then|fi|if|else|elif|for|in)\b", " ", command)
    segments = _segments(body)
    return bool(segments) and all(_segment_readonly(s) for s in segments)


def sleep_seconds(command: str) -> float:
    total = 0.0
    for num, unit in _SLEEP.findall(command or ""):
        try:
            value = float(num)
        except ValueError:
            continue
        total += value * {"": 1, "s": 1, "m": 60, "h": 3600}[unit]
    return total


def is_blocking_wait(command: str) -> bool:
    """An until/while loop with a sleep: one command that waits on a condition
    (the behavior the poll guard asks for)."""
    return bool(_LOOP.search(command or "")) and sleep_seconds(command) > 0


def poll_check(command: str) -> str | None:
    """For a sleep-then-check poll (``sleep 30 && tail -5 build.log``), the
    check part with the sleep and any leading ``cd`` removed; ``""`` for a
    bare sleep; ``None`` when the command is not a poll (no sleep, a blocking
    wait loop, or a check that is not read-only)."""
    if not command or sleep_seconds(command) <= 0 or is_blocking_wait(command):
        return None
    rest = [s for s in _segments(command) if not re.match(r"^sleep\s+[0-9.]+[smh]?$", s)
            and not re.match(r"^cd\s", s)]
    check = " && ".join(rest)
    if check and not is_readonly_command(check):
        return None
    return normalize_command(check)


def is_time_varying(command: str) -> bool:
    for segment in _segments(command):
        verb, _ = _verb(_words(segment))
        if verb in _TIME_VARYING:
            return True
    return False


def error_signature(text: str) -> str:
    """Stable hash of a failure's tail: digits, hex ids and whitespace
    normalized, so the same error from two attempts hashes the same."""
    tail = (text or "")[-600:]
    tail = re.sub(r"0x[0-9a-fA-F]+|\b[0-9a-f]{7,}\b", "#", tail)
    tail = re.sub(r"[0-9]+(\.[0-9]+)?", "#", tail)
    tail = re.sub(r"\s+", " ", tail).strip()
    return hashlib.sha1(tail.encode("utf-8", "replace")).hexdigest()[:12]


def text_hash(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()[:16]


def canonical_input(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def first_line(text: str, limit: int = 160) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""


def call_kind(tool: str, tool_input: Any) -> tuple[str, str | None]:
    """``(kind, key)`` for a tool call. Kinds: ``read`` (key: path), ``edit``
    (key: path), ``bash`` (key: normalized command), ``search`` (key:
    canonical input), ``agent``, ``passive``, ``other``."""
    data = tool_input if isinstance(tool_input, dict) else {}
    if tool in READ_TOOLS:
        path = data.get("file_path") or data.get("path")
        return "read", str(path) if path else None
    if tool == "fast_workspace":
        return ("read", str(data.get("path"))) if data.get("operation") in ("read", "list") else ("passive", None)
    if tool in EDIT_TOOLS:
        path = data.get("file_path") or data.get("path") or data.get("notebook_path")
        return "edit", str(path) if path else None
    if tool in BASH_TOOLS:
        command = data.get("command")
        return "bash", normalize_command(command) if isinstance(command, str) else None
    if tool in SEARCH_TOOLS:
        return "search", canonical_input(data)
    if tool in AGENT_TOOLS:
        return "agent", None
    if tool in PASSIVE_TOOLS or tool.startswith("mcp__") and ("read" in tool or "get" in tool or "list" in tool):
        return "passive", None
    return "other", None


def read_range(tool_input: Any) -> tuple[int, int | None]:
    data = tool_input if isinstance(tool_input, dict) else {}
    offset = data.get("offset") or data.get("start_line") or 0
    limit = data.get("limit") or data.get("max_lines")
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = int(limit) if limit is not None else None
    except (TypeError, ValueError):
        limit = None
    return offset, limit


_VALIDATION_PATTERNS = re.compile(
    r"InputValidationError|<tool_use_error>|validation error|Invalid (tool )?(input|argument|parameter)|"
    r"File has not been read yet|String to replace not found|Found \d+ matches of the string|"
    r"Tool .{0,40} not found|unknown tool|missing required|is not a valid|File does not exist|"
    r"No such tool|must be read before", re.I)
_DENIED_PATTERNS = re.compile(
    r"permission (to use .{0,40})?(has been )?denied|doesn't want to proceed|was rejected|user denied|"
    r"denied by (the )?(user|hook|policy|approval)|Refusing to run|blocked by (a )?hook|not allowed|"
    r"requires approval|approval (was )?denied", re.I)


def failure_kind(text: str) -> str | None:
    """``validation`` or ``denied`` for a failed tool call whose text says so,
    else None (an ordinary failure, e.g. a command's non-zero exit)."""
    head = (text or "")[:800]
    if _DENIED_PATTERNS.search(head):
        return "denied"
    if _VALIDATION_PATTERNS.search(head):
        return "validation"
    return None


def mentions(result_text: str, tool: str, tool_input: Any) -> bool:
    """Whether the next call's primary argument appears in the previous
    call's result (the calls are then dependent, not batchable)."""
    if not result_text:
        return False
    data = tool_input if isinstance(tool_input, dict) else {}
    probes = []
    for key in ("file_path", "path", "pattern", "notebook_path"):
        value = data.get(key)
        if isinstance(value, str) and value:
            probes.append(value.rsplit("/", 1)[-1])
    command = data.get("command")
    if isinstance(command, str):
        for word in _words(command):
            if "/" in word or "." in word:
                probes.append(word.rsplit("/", 1)[-1])
    probes = [p for p in probes if len(p) >= 3]
    return any(p in result_text for p in probes)
