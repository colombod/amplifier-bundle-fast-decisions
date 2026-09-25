"""Per-step action set for the fast judge (docs/GOAL.md, "What the judge decides").

Before every model call inside a turn -- not only at turn start -- the
orchestrator classifies the step from deterministic features of the request:
the previous response's tool calls (names, read-only or not, repeats), and the
size and error signals of their results. Then it picks the cheapest action that
keeps quality:

a. **Prepared action** (no model call): at a predictable read/status step --
   the turn's first step when the prompt names a file or asks about git state,
   or a routine continuation after a search whose result points at a file --
   offer the judge a small set of read-only fast_workspace candidates (file
   window, whole file, git status summary). The judge (``next_action``) picks
   one or abstains. It is asked only when the expected saving (hit rate x a
   host call's seconds) exceeds its own latency.
b. **Cheaper model for this step only**: a routine read-only continuation runs
   on a cheaper model ONLY when the price- and cache-aware math below says the
   step is cheaper there, never on a more expensive model, never past the
   model's context window. A cheaper-model response that edits (or, by
   default, ends the turn) is discarded and the host answers instead, so
   repository edits always come from the host model.
c. **Full model** otherwise.

Everything here is pure (no Amplifier imports); the orchestrator owns I/O and
receipts. Absent ``step_actions`` config leaves the loop byte-for-byte as before.

Why cheaper models rarely win per step on a cache-heavy host
------------------------------------------------------------
Routing one step of an ongoing conversation away from the host defers, but
does not remove, the host's cache writes: the host writes the same new tokens
at its next call. What the host saves is reading the cached prompt ``P`` once
plus its own output: ``P*read_h + out*(out_h + write_h)``. The cheaper model
pays for its own cache state: ``cached*read_c + (P - cached)*write_c +
out*out_c``. With Opus 5.5 as host (read $0.20/MTok) a cold Haiku 4.5 cache
(write $1.25/MTok) costs ~6x more than it saves on a 70k-token prompt, and
Sonnet 5's cache read ($0.30) is dearer than Opus 5.5's, so only a warm
cheaper-model cache on a big prompt can win (measured: docs/evidence/2026-09-25).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .contracts import field_value

# --- step kinds ---------------------------------------------------------------
TURN_START = "turn_start"
ROUTINE = "routine_readonly"
AMBIGUOUS = "ambiguous"
AFTER_EDIT = "after_edit"
AFTER_ERROR = "after_error"
AFTER_TEST = "after_test"
DELEGATION = "delegation"
REPEAT = "repeat"
LARGE_RESULT = "large_result"
REASONING = "needs_reasoning"
STEP_KINDS = (TURN_START, ROUTINE, AMBIGUOUS, AFTER_EDIT, AFTER_ERROR, AFTER_TEST, DELEGATION, REPEAT,
              LARGE_RESULT, REASONING)

DEFAULTS: dict[str, Any] = {
    "prepared": False,
    "cheaper_model": False,
    "cheap_models": ["claude-haiku-4-5"],
    "context_windows": {"claude-haiku-4-5": 200_000},
    "cheap_may_answer": False,
    "judge_ambiguous": False,
    "min_judge_saving_usd": 0.002,
    "amortize_steps": 1,
    "routine_max_calls": 3,
    "routine_max_result_chars": 20_000,
    "prior_accept": 0.5,
    "judge_seconds_prior": 0.15,
    "host_call_seconds_prior": 3.0,
    "skipped_output_tokens": 150,
    "cache_ttl_s": 300,
}
_BOOL_KEYS = ("prepared", "cheaper_model", "cheap_may_answer", "judge_ambiguous")
_INT_KEYS = {"amortize_steps": (1, 50), "routine_max_calls": (1, 16), "routine_max_result_chars": (100, 1_000_000),
             "skipped_output_tokens": (1, 20_000), "cache_ttl_s": (1, 3600)}
_FLOAT_KEYS = {"min_judge_saving_usd": (0.0, 10.0), "prior_accept": (0.0, 1.0), "judge_seconds_prior": (0.0, 60.0),
               "host_call_seconds_prior": (0.0, 600.0)}


def validate(config: Any) -> None:
    """Fail loud on malformed ``step_actions`` config (None/empty = off)."""
    if not config:
        return
    if not isinstance(config, dict):
        raise ValueError("step_actions must be a dict")
    unknown = set(config) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"step_actions has unknown keys: {sorted(unknown)}")
    for key in _BOOL_KEYS:
        if key in config and not isinstance(config[key], bool):
            raise ValueError(f"step_actions.{key} must be a bool")
    for key, (low, high) in _INT_KEYS.items():
        value = config.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high):
            raise ValueError(f"step_actions.{key} must be an integer in [{low}, {high}]")
    for key, (low, high) in _FLOAT_KEYS.items():
        value = config.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not low <= value <= high):
            raise ValueError(f"step_actions.{key} must be a number in [{low}, {high}]")
    models = config.get("cheap_models")
    if models is not None and (not isinstance(models, list) or not all(isinstance(m, str) and m for m in models)):
        raise ValueError("step_actions.cheap_models must be a list of model names")
    windows = config.get("context_windows")
    if windows is not None and (not isinstance(windows, dict) or not all(
            isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v > 0
            for k, v in windows.items())):
        raise ValueError("step_actions.context_windows must map model names to positive token counts")


def settings(config: dict[str, Any] | None) -> dict[str, Any]:
    return {**DEFAULTS, **(config or {})}


# --- tool classification -------------------------------------------------------
READ_ONLY_TOOLS = frozenset({"read_file", "glob", "grep", "fast_workspace", "list_directory", "ls", "list_files",
                             "web_fetch", "web_search", "load_skill", "todo", "python_check_readonly"})
EDIT_TOOLS = frozenset({"write_file", "edit_file", "multi_edit", "apply_patch", "notebook_edit", "create_file",
                        "delete_file", "move_file"})
SHELL_TOOLS = frozenset({"bash", "shell", "run_command", "execute_command", "terminal"})
DELEGATION_TOOLS = frozenset({"delegate", "task", "spawn_agent", "agent", "recipes"})
_READONLY_COMMANDS = frozenset({
    "grep", "egrep", "fgrep", "rg", "ag", "cat", "head", "tail", "ls", "find", "fd", "wc", "sort", "uniq", "cut",
    "tr", "echo", "printf", "pwd", "which", "whereis", "type", "file", "stat", "du", "df", "tree", "diff", "cmp",
    "jq", "yq", "sleep", "date", "true", "false", "basename", "dirname", "realpath", "readlink", "nl", "column",
    "od", "hexdump", "md5", "md5sum", "shasum", "sha256sum", "uname", "whoami", "id", "hostname", "ps", "test",
    "[", "awk", "sed", "less", "more", "env", "printenv", "comm", "seq", "xxd", "strings",
})
_GIT_READONLY = frozenset({"status", "log", "diff", "show", "rev-list", "rev-parse", "ls-files", "ls-tree", "blame",
                           "grep", "describe", "shortlog", "cat-file", "whatchanged", "name-rev", "merge-base",
                           "for-each-ref", "count-objects", "reflog"})
_GIT_BRANCH_MUTATING = frozenset({"-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy", "-f",
                                  "--force", "-u", "--set-upstream-to", "--unset-upstream", "--edit-description"})
_TEST_RE = re.compile(r"\b(pytest|py\.test|unittest|nose2?|tox|nox|jest|vitest|mocha|rspec|phpunit|"
                      r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test|cargo\s+test|go\s+test|make\s+(?:test|check)|"
                      r"node\s+--test|ctest|gradle\w*\s+test|mvn\s+test)\b")
_SAFE_REDIRECTS = re.compile(r"(?:\d?>&\d|&?\d?>\s*/dev/null|\d<&\d)")
_SEGMENTS = re.compile(r"\|\||&&|;|\||\n")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")


def _words(segment: str) -> list[str]:
    try:
        import shlex
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _git_readonly(words: list[str]) -> bool | None:
    i = 1
    while i < len(words) and words[i].startswith("-"):
        i += 2 if words[i] in ("-C", "-c", "--git-dir", "--work-tree") else 1
    if i >= len(words):
        return True
    sub, rest = words[i], words[i + 1:]
    if sub in _GIT_READONLY:
        if sub == "reflog" and rest and rest[0] not in ("show",) and not rest[0].startswith("-"):
            return False
        return True
    if sub == "branch":
        return not any(w in _GIT_BRANCH_MUTATING for w in rest) and all(w.startswith("-") for w in rest)
    if sub in ("tag", "remote", "stash", "config", "worktree"):
        listing = {"tag": ("-l", "--list"), "remote": ("-v", "show", "get-url"), "stash": ("list", "show"),
                   "config": ("--get", "--list", "-l", "--get-all", "--get-regexp"), "worktree": ("list",)}[sub]
        return (not rest and sub != "config") or (bool(rest) and rest[0] in listing)
    return False


def bash_readonly(command: Any) -> bool | None:
    """True when every command in the pipeline is a known read-only command,
    False when any writes (redirection, tee, ``sed -i``, mutating git, ``rm``,
    ...), None when unknown (ambiguous: e.g. a script or interpreter)."""
    if not isinstance(command, str) or not command.strip():
        return None
    stripped = _SAFE_REDIRECTS.sub(" ", command)
    if ">" in stripped:
        return False
    verdict: bool | None = True
    for segment in _SEGMENTS.split(command):
        words = [w for w in _words(segment.strip())]
        while words and _ASSIGNMENT.match(words[0]):
            words = words[1:]
        if not words:
            continue
        head = words[0].rsplit("/", 1)[-1]
        if head in ("cd", "pushd", "popd", "set", "export", "time", "nice", "timeout", "command", "builtin"):
            if head in ("time", "nice", "command", "builtin") and len(words) > 1:
                words, head = words[1:], words[1].rsplit("/", 1)[-1]
            elif head == "timeout" and len(words) > 2:
                words, head = words[2:], words[2].rsplit("/", 1)[-1]
            else:
                continue
        if head in ("tee", "rm", "rmdir", "mv", "cp", "mkdir", "touch", "chmod", "chown", "ln", "dd", "truncate",
                    "patch", "install", "unlink", "shred"):
            return False
        if head == "git":
            result = _git_readonly(words)
        elif head == "sed":
            result = False if any(w == "-i" or w.startswith("-i") or w == "--in-place" for w in words[1:]) else True
        elif head == "find":
            result = False if any(w in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint") for w in words) \
                else True
        elif head in _READONLY_COMMANDS:
            result = True
        else:
            result = None
        if result is False:
            return False
        if result is None:
            verdict = None
    return verdict


def is_test_call(name: str, args: dict) -> bool:
    if name in ("run_tests", "python_check"):
        return True
    return name in SHELL_TOOLS and isinstance(args.get("command"), str) and bool(_TEST_RE.search(args["command"]))


def tool_readonly(name: str, args: dict) -> bool | None:
    if name in READ_ONLY_TOOLS:
        return True
    if name in EDIT_TOOLS or name in DELEGATION_TOOLS:
        return False
    if name in SHELL_TOOLS:
        return bash_readonly(args.get("command"))
    return None


def response_readonly(tool_calls: list[tuple[str, dict]]) -> bool:
    """True when every tool call in a model response is read-only (an empty
    list -- a final answer -- is not a tool call and returns True)."""
    return all(tool_readonly(n, a) is True and not is_test_call(n, a) for n, a in tool_calls)


# --- request parsing -----------------------------------------------------------
# Injected context: ``<system-reminders>`` envelopes (with a preamble outside
# the inner blocks) and bare ``<system-reminder ...>`` blocks.
_REMINDER = re.compile(r"<system-reminders\b.*?</system-reminders>|<system-reminder\b.*?</system-reminder>", re.S)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, (list, tuple)):
        return ""
    parts: list[str] = []
    for block in content:
        kind = field_value(block, "type", None)
        if kind in (None, "text"):
            text = field_value(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
        elif kind == "tool_result":
            output = field_value(block, "output", None)
            if output is None:
                output = field_value(block, "content", "")
            parts.append(output if isinstance(output, str) else _dump(output))
    return "\n".join(parts)


def _dump(value: Any) -> str:
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


def _has_tool_result_block(message: Any) -> bool:
    content = field_value(message, "content", None)
    return isinstance(content, (list, tuple)) and any(
        field_value(b, "type", None) == "tool_result" for b in content)


def _is_real_user(message: Any) -> bool:
    if field_value(message, "role", None) != "user" or _has_tool_result_block(message):
        return False
    return bool(_REMINDER.sub("", _text_of(field_value(message, "content", ""))).strip())


def calls_of(message: Any) -> list[tuple[str, dict]]:
    """``(tool name, arguments)`` for every tool call in an assistant message
    or a provider response (``tool_calls`` with tool/name + arguments/input,
    or ``tool_call``/``tool_use`` content blocks). Never raises."""
    out: list[tuple[str, dict]] = []
    try:
        for call in field_value(message, "tool_calls", None) or []:
            name = (field_value(call, "tool", None) or field_value(call, "name", None)
                    or field_value(field_value(call, "function", None) or {}, "name", None))
            args = field_value(call, "arguments", None)
            if args is None:
                args = field_value(call, "input", None)
            if args is None:
                args = field_value(field_value(call, "function", None) or {}, "arguments", None)
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            if isinstance(name, str) and name:
                out.append((name, args if isinstance(args, dict) else {}))
        if not out:
            content = field_value(message, "content", None)
            for block in content if isinstance(content, (list, tuple)) else []:
                if field_value(block, "type", None) in ("tool_call", "tool_use"):
                    name = field_value(block, "name", None)
                    args = field_value(block, "input", None) or field_value(block, "arguments", None) or {}
                    if isinstance(name, str) and name:
                        out.append((name, args if isinstance(args, dict) else {}))
    except Exception:  # noqa: BLE001 -- a malformed shape is simply "no calls"
        return out
    return out


@dataclass
class StepView:
    """What the loop knows right before a model call."""
    turn_start: bool
    step_index: int                     # model calls already made this turn
    prompt: str
    calls: list[tuple[str, dict]] = field(default_factory=list)   # previous response's tool calls
    results: list[str] = field(default_factory=list)              # their results (text)
    result_chars: int = 0
    error: bool = False
    repeat: bool = False


_ERROR_RE = re.compile(r'"success":\s*false|"error":\s*\{|Traceback \(most recent call last\)|'
                       r'^Error\b|^error:|Internal error executing tool|"(?:returncode|exit_code)":\s*[1-9]|'
                       r'"cancelled":\s*true', re.I | re.M)


def _result_error(text: str) -> bool:
    return bool(_ERROR_RE.search(text[:4000]))


def analyze(request: Any) -> StepView:
    """Deterministic features of the upcoming step. Never raises."""
    try:
        messages = list(field_value(request, "messages", None) or [])
    except Exception:  # noqa: BLE001
        messages = []
    user_index = next((i for i in range(len(messages) - 1, -1, -1) if _is_real_user(messages[i])), None)
    start = 0 if user_index is None else user_index + 1
    prompt = "" if user_index is None else _REMINDER.sub("", _text_of(field_value(messages[user_index], "content", ""))).strip()
    assistant_indices = [i for i in range(start, len(messages)) if field_value(messages[i], "role", None) == "assistant"]
    if not assistant_indices:
        return StepView(turn_start=True, step_index=0, prompt=prompt)
    last = assistant_indices[-1]
    calls = calls_of(messages[last])
    results = []
    for message in messages[last + 1:]:
        role = field_value(message, "role", None)
        if role == "tool" or (role == "user" and _has_tool_result_block(message)):
            results.append(_text_of(field_value(message, "content", "")))
    signature = lambda c: (c[0], _dump(c[1]))
    earlier = {signature(c) for i in assistant_indices[:-1] for c in calls_of(messages[i])}
    return StepView(
        turn_start=False, step_index=len(assistant_indices), prompt=prompt, calls=calls, results=results,
        result_chars=sum(len(r) for r in results), error=any(_result_error(r) for r in results),
        repeat=bool(calls) and any(signature(c) in earlier for c in calls),
    )


def classify(view: StepView, config: dict[str, Any] | None = None) -> tuple[str, str]:
    """``(kind, reason)``; reasons carry tool names only, never arguments."""
    cfg = settings(config)
    if view.turn_start:
        return TURN_START, "no_tool_result_yet"
    if not view.calls:
        return REASONING, "no_tool_calls"
    names = sorted({n for n, _ in view.calls})
    label = ",".join(names)[:60]
    if any(n in DELEGATION_TOOLS for n in names):
        return DELEGATION, "delegation:" + label
    verdicts = [tool_readonly(n, a) for n, a in view.calls]
    if any(v is False for v in verdicts):
        return AFTER_EDIT, "mutating:" + label
    if any(is_test_call(n, a) for n, a in view.calls):
        return AFTER_TEST, "test_run:" + label
    if view.error:
        return AFTER_ERROR, "tool_error:" + label
    if view.repeat:
        return REPEAT, "repeated_call:" + label
    if view.result_chars > cfg["routine_max_result_chars"]:
        return LARGE_RESULT, "large_result:" + label
    if len(view.calls) > cfg["routine_max_calls"]:
        return REASONING, "many_calls:" + label
    if any(v is None for v in verdicts):
        return AMBIGUOUS, "unknown_command:" + label
    return ROUTINE, "readonly:" + label


# --- prepared-action candidates -----------------------------------------------
_EXT = r"(?:md|txt|py|rs|js|ts|tsx|jsx|json|yaml|yml|toml|html|css)"
_PROMPT_PATH = re.compile(r"(?<![\w/])(?:\./)?[\w./-]+\." + _EXT + r"\b")
_JSON_HIT = re.compile(r'"(?:file|path|file_path)"\s*:\s*"([^"]+\.' + _EXT + r')"'
                       r'(?:\s*,\s*"(?:line_number|line)"\s*:\s*(\d+))?')
_TEXT_HIT = re.compile(r"(?<![\w/.-])((?:/|\./)?(?:[\w.@+-]+/)*[\w.@+-]+\." + _EXT + r")(?::(\d+))?(?![\w/])")
_IDENT = re.compile(r"\b(?:[A-Za-z_]\w*_\w*|[a-z]+[A-Z]\w*|[A-Z][a-z0-9]+[A-Z]\w*|[A-Z][A-Z0-9_]{2,})\b")
_GIT_QUESTION = re.compile(r"\b(git|branch(?:es)?|commits?|committed|uncommitted|staged|unstaged|untracked|"
                           r"working tree|stash|HEAD|changed since|modified since|what (?:have i|did i) change)\b",
                           re.I)
_SEARCH_TOOLS = frozenset({"grep", "glob", "list_directory", "ls", "list_files"})
_SEARCH_COMMANDS = re.compile(r"^\s*(?:cd\s+\S+\s*&&\s*)?(?:grep|egrep|rg|ag|find|fd|git\s+grep|git\s+ls-files|ls)\b")


def prompt_paths(prompt: str) -> list[str]:
    return list(dict.fromkeys(_PROMPT_PATH.findall(prompt or "")))


def prompt_identifiers(prompt: str, limit: int = 6) -> list[str]:
    """Code-like identifiers the prompt names (snake_case, camelCase,
    CamelCase, CONSTANTS), in order; file names excluded."""
    text = _PROMPT_PATH.sub(" ", prompt or "")
    return list(dict.fromkeys(m for m in _IDENT.findall(text) if len(m) >= 3))[:limit]


def git_question(prompt: str) -> bool:
    return bool(_GIT_QUESTION.search(prompt or ""))


def after_search(view: StepView) -> bool:
    for name, args in view.calls:
        if name in _SEARCH_TOOLS:
            return True
        if name in SHELL_TOOLS and isinstance(args.get("command"), str) and _SEARCH_COMMANDS.search(args["command"]):
            return True
    return False


def result_paths(results: list[str], limit: int = 6) -> list[tuple[str, int | None]]:
    """Files (with the first line number when given) named in tool results,
    in order of first appearance, deduplicated by path."""
    found: dict[str, int | None] = {}
    for text in results:
        for regex in (_JSON_HIT, _TEXT_HIT):
            for path, line in regex.findall(text):
                if path not in found:
                    found[path] = int(line) if line else None
                elif found[path] is None and line:
                    found[path] = int(line)
    return list(found.items())[:limit]


def _already_read(view: StepView, workspace: Any, rel: str) -> bool:
    for name, args in view.calls:
        target = args.get("file_path") if name == "read_file" else (
            args.get("path") if name == "fast_workspace" and args.get("operation") == "read" else None)
        if isinstance(target, str) and workspace.relative(target) == rel:
            return True
    return False


def candidates_for(view: StepView, kind: str, workspace: Any, max_candidates: int = 4) -> list[Any]:
    """Read-only fast_workspace candidates for a predictable step. Never raises."""
    if workspace is None or not hasattr(workspace, "candidate_for_path"):
        return []
    out: list[Any] = []
    try:
        if kind == TURN_START:
            if git_question(view.prompt) and hasattr(workspace, "git_candidate"):
                git = workspace.git_candidate()
                if git is not None:
                    out.append(git)
            identifiers = prompt_identifiers(view.prompt)
            for index, rel in enumerate(prompt_paths(view.prompt)[:max_candidates]):
                line = workspace.find_line(rel, identifiers) if identifiers and hasattr(workspace, "find_line") else None
                candidate = workspace.candidate_for_path(rel, index, line=line) if line else None
                candidate = candidate or workspace.candidate_for_path(rel, index)
                if candidate is not None:
                    out.append(candidate)
        elif kind == ROUTINE and after_search(view):
            index = 0
            for path, line in result_paths(view.results):
                rel = workspace.relative(path)
                if rel is None or _already_read(view, workspace, rel):
                    continue
                candidate = workspace.candidate_for_path(rel, index, line=line, origin="last_tool_result",
                                                         source="named in the last tool result")
                if candidate is not None:
                    out.append(candidate)
                    index += 1
    except Exception:  # noqa: BLE001 -- candidates are optional
        return out[:max_candidates]
    return out[:max_candidates]


def repeats_prepared(arguments: dict, response_calls: list[tuple[str, dict]], workspace: Any) -> bool:
    """Whether the model's next response re-fetched what a prepared action
    already fetched (the same file read, or git status/log/branch again)."""
    operation, path = arguments.get("operation"), arguments.get("path")
    for name, args in response_calls:
        if operation == "git":
            if name in SHELL_TOOLS and re.search(r"\bgit\s+(status|log|branch|rev-list)\b", str(args.get("command", ""))):
                return True
            if name == "fast_workspace" and args.get("operation") == "git":
                return True
            continue
        target = None
        if name == "read_file":
            target = args.get("file_path")
        elif name == "fast_workspace" and args.get("operation") == "read":
            target = args.get("path")
        elif name in SHELL_TOOLS and isinstance(path, str):
            if re.search(r"\b(cat|head|tail|sed|less|more|nl)\b[^|;&]*" + re.escape(path), str(args.get("command", ""))):
                return True
        if isinstance(target, str) and workspace is not None and workspace.relative(target) == path:
            return True
    return False


# --- price- and cache-aware per-step model choice -----------------------------
def _rates(model: str | None, rates: dict) -> tuple | None:
    from .savings import _rates_for
    return _rates_for(model, rates)


def step_cost(model: str, *, prompt_tokens: int, cached_tokens: int, output_tokens: int, rates: dict) -> float | None:
    """USD for one call of ``prompt_tokens`` on ``model`` with
    ``cached_tokens`` of its prefix already in that model's cache."""
    r = _rates(model, rates)
    if r is None:
        return None
    cached = max(0, min(int(cached_tokens), int(prompt_tokens)))
    return (cached * r[2] + (int(prompt_tokens) - cached) * r[3] + int(output_tokens) * r[1]) / 1_000_000


def host_step_saving(host: str, *, prompt_tokens: int, output_tokens: int, cheap_output_tokens: int,
                     rates: dict) -> float | None:
    """What the host side saves when ONE step runs elsewhere: one read of the
    cached prompt plus its own output (which it would also have cache-written
    at its next call), minus writing the cheaper model's output instead. The
    host's writes of new tokens are deferred to its next call, not saved."""
    r = _rates(host, rates)
    if r is None:
        return None
    return (int(prompt_tokens) * r[2] + int(output_tokens) * (r[1] + r[3]) - int(cheap_output_tokens) * r[3]) / 1_000_000


def cheaper_step(*, host: str | None, cheap_models: list[str], prompt_tokens: int, cache_prefix: dict[str, int],
                 output_tokens: int, windows: dict[str, int], rates: dict, amortize_steps: int = 1,
                 step_growth_tokens: int = 2_000) -> dict[str, Any]:
    """Pick the cheaper model for this step, or none.

    ``cache_prefix[m]`` is the prompt prefix (tokens) currently warm in model
    ``m``'s cache (0/absent = cold). A model is considered only when its list
    input AND output prices are both below the host's (never route up) and the
    prompt fits 90% of its context window. With ``amortize_steps`` k > 1 a
    cold cache's first write is spread over k routine steps (the later ones
    warm). Returns ``{"model", "reason", "host_saving_usd", "cheap_cost_usd",
    "saving_usd"}``; ``model`` None keeps the host."""
    result: dict[str, Any] = {"model": None, "reason": "no_cheaper_model", "host_saving_usd": None,
                              "cheap_cost_usd": None, "saving_usd": None}
    host_r = _rates(host, rates)
    if host_r is None:
        result["reason"] = "host_unpriced"
        return result
    saving = host_step_saving(host, prompt_tokens=prompt_tokens, output_tokens=output_tokens,
                              cheap_output_tokens=output_tokens, rates=rates)
    result["host_saving_usd"] = round(saving, 8)
    best = None
    reasons = []
    for model in cheap_models:
        r = _rates(model, rates)
        if model == host or r is None:
            continue
        if not (r[0] < host_r[0] and r[1] < host_r[1]):
            reasons.append("not_cheaper_tier")
            continue
        window = windows.get(model)
        if window and prompt_tokens > 0.9 * window:
            reasons.append("context_window")
            continue
        cached = int(cache_prefix.get(model) or 0)
        cost = step_cost(model, prompt_tokens=prompt_tokens, cached_tokens=cached, output_tokens=output_tokens,
                         rates=rates)
        if cached <= 0 and amortize_steps > 1:
            warm = step_cost(model, prompt_tokens=prompt_tokens + step_growth_tokens, cached_tokens=prompt_tokens,
                             output_tokens=output_tokens, rates=rates)
            cost = (cost + (amortize_steps - 1) * warm) / amortize_steps
        if cost >= saving:
            reasons.append("cold_cache_costs_more" if cached <= 0 else "costs_more")
            continue
        if best is None or cost < best[1]:
            best = (model, cost)
    if best is None:
        result["reason"] = reasons[0] if reasons else "no_cheaper_model"
        return result
    result.update(model=best[0], reason="cheaper_warm_cache" if cache_prefix.get(best[0]) else "cheaper_amortized",
                  cheap_cost_usd=round(best[1], 8), saving_usd=round(saving - best[1], 8))
    return result


def expected_prepared_saving_s(*, asked: int, accepted: int, prior_accept: float, host_call_s: float,
                               judge_s: float) -> float:
    """Expected seconds saved by asking the judge for a prepared action:
    smoothed hit rate x a host call's seconds, minus the judge's own time."""
    p_hit = (accepted + 2 * prior_accept) / (asked + 2)
    return p_hit * host_call_s - judge_s


# Typed question for an ambiguous step (an unknown command's result): is the
# next step a read-only continuation? Positive polarity on "read_only".
NEXT_STEP_QUESTION = "next_step_kind"
NEXT_STEP_INSTRUCTIONS = ("Given the task and the latest tool result, classify what the agent's NEXT step "
                          "will be.")
NEXT_STEP_CRITERIA = {
    "read_only": "Another read-only step: read a file, search, list, or check status; nothing is changed.",
    "changes_code": "The next step edits or creates files, runs a command that changes state, runs tests, "
                    "or writes the final answer to the user.",
}
