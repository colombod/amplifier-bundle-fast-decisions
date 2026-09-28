#!/usr/bin/env python3
"""Run real Amplifier coding sessions through Forge, with independent checks.

Explicit opt-in: this invokes the configured paid generative provider and lets
agents edit only disposable workspaces. It never changes global host settings.

This module is used two ways:

- Directly, via its legacy CLI (`prepare|worker|evaluate|batch root [name]`),
  which reproduces the original three-task, two-side (baseline/fast)
  comparison against the installed bundle cache.
- As a library, via `prepare(root, config=...)`, `add_run(...)`,
  `launch_run(...)` and `wait_for_result(...)`, by `scripts/campaign.py`,
  which drives an arbitrary, resumable set of paired runs against a frozen
  candidate source tree instead of the installed cache.
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import random
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

import forge_workloads
from forge_workloads import SPECS, STARTERS, PUBLIC, evaluate

FORGE = Path.home()/'.agents/skills/amplifier-skill-forge/tools/forge.py'
CACHE = Path.home()/'.amplifier/cache/amplifier-bundle-fast-decisions-703c3edc7c970204'
EVENTS = Path.home()/'.amplifier/fast-decisions/events'
HOST_PYTHON = Path.home()/'.local/share/uv/tools/amplifier/bin/python'
BENCHMARK_BUNDLE_NAME = 'afast-benchmark-run'

PROMPT = ('Read README.md first, then repair the implementation to satisfy its full contract. '
          'Work directly in this workspace without delegating or using the network. '
          'Modify solution.py and add tests if useful, but do not change README.md or existing test_public.py. '
          'Run the tests and explain your fix and validation. Do not inspect files outside this workspace.')

# The upstream orchestrator the "off" (no fast-decisions routing) side runs.
UPSTREAM_LOOP_SOURCE = 'git+https://github.com/microsoft/amplifier-module-loop-streaming@4cc86dd4eae36b40af38b4e2e70b9045649d2903'

_UNSAFE_PATH_CHARS = re.compile(r'[^A-Za-z0-9._-]')
_PATH_SAFE_RE = re.compile(r'^[A-Za-z0-9._/-]+$')


def _slug(value):
    """Filesystem/URI-safe slug for a run name -- mirrors scripts/battery.py's
    `_slug` exactly (deliberately duplicated rather than imported: battery.py
    already imports this module, so importing battery here would be
    circular). A run name may contain '+' (inherited from an experiment/cell
    id like 'judge-jev+effort-incumbent'); '+' is outside RFC 3986's
    unreserved set, so Path.as_uri() percent-encodes it to '%2B' when this
    module turns a run directory into a `file://` bundle URI, and Amplifier's
    bundle loader does not percent-decode it back. Slugging the directory
    name up front makes that class of bug structurally impossible."""
    return _UNSAFE_PATH_CHARS.sub('-', value)


def _assert_bundle_uri_safe(path):
    """Fail loudly, before ever invoking amplifier, if `path` (about to become
    a `file://` bundle URI) contains a character the URI encoder would
    percent-escape. This is a defensive canary -- with directory names
    slugged via `_slug`, it should never fire -- rather than the primary
    fix, so a regression here surfaces as a clear pre-launch error instead of
    a silent "Bundle Error -- File not found" deep inside amplifier."""
    text = str(path)
    if not _PATH_SAFE_RE.match(text):
        raise SystemExit(f'worker: refusing to launch -- profile path is not bundle-URI-safe '
                          f'(would be percent-encoded): {text!r}')


# Forge launch resilience (defect: a bursty scheduler hammered an unreachable/
# restarting Forge daemon with back-to-back launches). MIN_LAUNCH_SPACING_SECONDS
# serializes the moment a launch is handed to Forge across all threads in this
# process; the backoff constants bound the retry-with-jitter loop for launches
# that fail because Forge itself is unreachable (as opposed to a session-limit
# or spawn-helper error, which already had their own narrower retries).
MIN_LAUNCH_SPACING_SECONDS = 2.0
BACKOFF_INITIAL_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 30.0
MAX_FORGE_UNREACHABLE_RETRIES = 6
FORGE_UNREACHABLE_MARKERS = ('cannot reach', 'empty response', 'connection refused', 'econnrefused')

_launch_spacing_lock = threading.Lock()
_last_launch_at = [0.0]


def _is_forge_unreachable_error(text):
    lowered = (text or '').lower()
    return any(marker in lowered for marker in FORGE_UNREACHABLE_MARKERS)


def _wait_for_launch_spacing(sleep_fn, now_fn):
    """Enforce a minimum spacing between handing launches to Forge, across
    every thread in this process (a bounded --parallel N battery.py run
    launches from N worker threads). Serializes only this short wait, never
    the launch itself, so N launches can still be genuinely in flight at
    once, just started at least MIN_LAUNCH_SPACING_SECONDS apart."""
    with _launch_spacing_lock:
        elapsed = now_fn() - _last_launch_at[0]
        if elapsed < MIN_LAUNCH_SPACING_SECONDS:
            sleep_fn(MIN_LAUNCH_SPACING_SECONDS - elapsed)
        _last_launch_at[0] = now_fn()

# --amplifier-bundle lean (battery.py prepare): the minimal explicit module set
# used INSTEAD OF including the fast-decisions bundle root (which transitively
# pulls in the full foundation bundle -- ~50k tokens of agents/context/behaviors).
# Sources mirror foundation's own declarations verbatim (see docs/COMPATIBILITY.md
# and AGENTS.md): foundation's bundle.md for tool-filesystem/tool-bash,
# foundation's behaviors/todo-reminder.yaml for tool-todo, and the installed
# amplifier-module-provider-anthropic package's own git remote for provider-anthropic.
LEAN_MODULE_SOURCES = {
    'tool-filesystem': 'git+https://github.com/microsoft/amplifier-module-tool-filesystem@main',
    'tool-bash': 'git+https://github.com/microsoft/amplifier-module-tool-bash@main',
    'tool-todo': 'git+https://github.com/microsoft/amplifier-module-tool-todo@main',
    'provider-anthropic': 'git+https://github.com/microsoft/amplifier-module-provider-anthropic@main',
}

# The decision policy the "active" side runs with, absent overrides.
DEFAULT_DECISION = {
    'backend': 'ollama', 'model': 'qwen3:0.6b', 'timeout_ms': 500,
    'min_probability': .90, 'min_margin': .20, 'max_fast_streak': 3,
    'max_fast_per_turn': 12, 'max_candidates': 12, 'max_state_chars': 2048,
    'allowed_tools': ['fast_workspace'],
}


def dump(path, value):
    path.write_text(json.dumps(value, indent=2)+'\n')


def hash_files(workspace):
    files={str(p.relative_to(workspace)):hashlib.sha256(p.read_bytes()).hexdigest()
           for p in sorted(workspace.rglob('*')) if p.is_file() and '.git' not in p.parts and '__pycache__' not in p.parts}
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def tree_sha256(package_dir):
    """Deterministic content hash of a Python source tree.

    MUST match `amplifier_fast_decisions.provenance.tree_sha256` byte for
    byte -- both this script (comparing a side's frozen source before/after
    a run) and the running module (emitting the `fast_decisions:source`
    event this script reads back) hash the same tree, and `source_match`
    only means anything if they agree. Algorithm: walk every `*.py` file
    under `package_dir` (skipping any `__pycache__` directory), sort by
    POSIX-style relative path, and feed sha256, per file in that order: the
    relative path (utf-8), a NUL byte, the raw file bytes, then a trailing
    NUL byte. An unreadable file is skipped, not fatal. Returns None if
    `package_dir` does not exist.
    """
    package_dir = Path(package_dir)
    if not package_dir.exists():
        return None
    candidates = [p for p in package_dir.rglob('*.py') if '__pycache__' not in p.parts]
    candidates.sort(key=lambda p: p.relative_to(package_dir).as_posix())
    digest = hashlib.sha256()
    for p in candidates:
        try:
            data = p.read_bytes()
        except OSError:
            continue
        rel = p.relative_to(package_dir).as_posix()
        digest.update(rel.encode('utf-8'))
        digest.update(b'\0')
        digest.update(data)
        digest.update(b'\0')
    return digest.hexdigest()


def git_sha(path):
    try:
        return subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True, stderr=subprocess.DEVNULL).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def available(name):
    try:metadata.version(name);return True
    except metadata.PackageNotFoundError:return False


def _battery_task(task):
    """Look up ``task`` in battery_tasks.TASKS, if that module is importable
    and knows about it. Returns None otherwise (never raises)."""
    try:
        import battery_tasks
    except ImportError:
        return None
    return getattr(battery_tasks, 'TASKS', {}).get(task)


def _task_files(task):
    """Initial workspace files for ``task``: {relpath: text}.

    Delegates to ``forge_workloads.task_files`` (the contract point where a
    task-generalizing sibling module registers new task shapes). Falls back,
    in order, to: the legacy README/solution/test_public trio (for this
    module's own SPECS/STARTERS/PUBLIC tasks), then battery_tasks.TASKS
    directly -- so this module works standalone whether or not
    forge_workloads has been generalized yet.
    """
    fn = getattr(forge_workloads, 'task_files', None)
    if fn is not None:
        return fn(task)
    if task in SPECS:
        return {'README.md': SPECS[task], 'solution.py': STARTERS[task], 'test_public.py': PUBLIC[task]}
    entry = _battery_task(task)
    if entry is not None:
        return entry.files
    raise KeyError(task)


def _task_prompt(task):
    """Per-task prompt override, or None to use the run/manifest default."""
    fn = getattr(forge_workloads, 'task_prompt', None)
    if fn is not None:
        return fn(task)
    if task in SPECS:
        return None
    entry = _battery_task(task)
    return getattr(entry, 'prompt', None) if entry is not None else None


def _task_protected(task):
    """Files the agent must not modify for ``task``."""
    fn = getattr(forge_workloads, 'task_protected', None)
    if fn is not None:
        return fn(task)
    if task in SPECS:
        return ('README.md', 'test_public.py')
    entry = _battery_task(task)
    return tuple(entry.protected) if entry is not None else ('README.md', 'test_public.py')


def _task_kind(task):
    """'code' (evaluator-checked) or 'answer' (message-checked). Legacy SPECS-only
    tasks and any task battery_tasks does not know about are 'code'."""
    try:
        import battery_tasks
    except ImportError:
        return 'code'
    entry = getattr(battery_tasks, 'TASKS', {}).get(task)
    return getattr(entry, 'kind', 'code') if entry is not None else 'code'


# A response whose data.purpose is one of these is an out-of-band background
# call the running session makes on its own (e.g. the session-naming hook's
# haiku call during cleanup, AFTER the turn's real answer) -- never the
# user-facing final message. Mirrors battery.py's BACKGROUND_CALL_PURPOSES
# (same real-world evidence: 2026-09-25 mt-smoke campaign). Kept as a
# separate constant (not imported from battery.py) because forge_e2e must
# not depend on battery.py -- battery.py already imports forge_e2e.
_BACKGROUND_CALL_PURPOSES = {'session-naming'}


def _extract_final_message(session_dir, workspace):
    """Best-effort extraction of the assistant's final response text.

    Primary source: the last ``llm:response`` event's ``data.raw.content``
    ``text``-type blocks (verified against real session events.jsonl files).
    Falls back to the last line of ``.amplifier-final-answer.txt`` in the
    workspace, if present. Returns None when neither source is usable.
    """
    if session_dir is not None:
        events_path = Path(session_dir)/'events.jsonl'
        if events_path.exists():
            last_text = None
            for line in events_path.read_text().splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get('event') != 'llm:response':
                    continue
                data = e.get('data') or {}
                if data.get('purpose') in _BACKGROUND_CALL_PURPOSES:
                    continue
                raw = data.get('raw') or {}
                content = raw.get('content')
                if not isinstance(content, list):
                    continue
                texts = [b.get('text') for b in content
                         if isinstance(b, dict) and b.get('type') == 'text' and b.get('text')]
                if texts:
                    last_text = '\n'.join(texts)
            if last_text is not None:
                return last_text
        # Hosts may omit raw provider payloads from events while preserving the
        # canonical conversation. Grade that final assistant message as well.
        transcript = Path(session_dir)/'transcript.jsonl'
        if transcript.exists():
            for line in reversed(transcript.read_text().splitlines()):
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if message.get('role') != 'assistant' or (message.get('metadata') or {}).get('purpose') in _BACKGROUND_CALL_PURPOSES:
                    continue
                content = message.get('content')
                if isinstance(content, str) and content.strip():
                    return content
                if isinstance(content, list):
                    texts = [b['text'] for b in content if isinstance(b, dict)
                             and b.get('type') == 'text' and b.get('text')]
                    if texts:
                        return '\n'.join(texts)
    marker = Path(workspace)/'.amplifier-final-answer.txt'
    if marker.exists():
        lines = marker.read_text().splitlines()
        if lines:
            return lines[-1]
    return None


def _parse_iso_ts(value):
    """Parse an event's ``ts`` field (ISO-8601) into an aware datetime, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None


def _extract_final_message_window(session_dir, start_iso, end_iso):
    """Like `_extract_final_message`, but only considers `llm:response` events
    whose `ts` falls within [start_iso, end_iso] -- used to attribute one
    turn's final message within a multi-turn (--resume) scenario session,
    whose events.jsonl otherwise mixes every turn together. Also excludes any
    response tagged with a _BACKGROUND_CALL_PURPOSES purpose (e.g. the
    session-naming hook's own response, which can land inside a turn's window
    AFTER the turn's real answer -- see BACKGROUND_CALL_PURPOSES in
    battery.py for the real-world evidence). Returns None if no in-window,
    non-background text is found (callers may fall back to the whole-session
    extractor for the last turn, or to a stdout-captured response -- see
    _parse_amplifier_stdout_response, the preferred source when available).
    """
    if session_dir is None:
        return None
    events_path = Path(session_dir)/'events.jsonl'
    if not events_path.exists():
        return None
    start = _parse_iso_ts(start_iso)
    end = _parse_iso_ts(end_iso)
    last_text = None
    for line in events_path.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get('event') != 'llm:response':
            continue
        data = e.get('data') or {}
        if data.get('purpose') in _BACKGROUND_CALL_PURPOSES:
            continue
        ts = _parse_iso_ts(e.get('ts'))
        if ts is not None:
            if start is not None and ts < start:
                continue
            if end is not None and ts > end:
                continue
        raw = data.get('raw') or {}
        content = raw.get('content')
        if not isinstance(content, list):
            continue
        texts = [b.get('text') for b in content
                 if isinstance(b, dict) and b.get('type') == 'text' and b.get('text')]
        if texts:
            last_text = '\n'.join(texts)
    return last_text


def _parse_amplifier_stdout_response(text):
    """Best-effort extraction of `--output-format json`'s clean stdout blob's
    `response` field. Amplifier redirects ALL diagnostic/progress console
    output to stderr whenever output_format is 'json' (see
    amplifier_app_cli.main.execute_single), so real stdout should be exactly
    one JSON object shaped like {"status": "success", "response": "...",
    "session_id": ..., ...} -- untouched by anything that happens after the
    turn's real response (e.g. the session-naming background call, which
    runs during cleanup and never touches this value). Returns None if
    `text` is empty, unparseable, or has no usable string 'response' field
    (e.g. an error-shaped JSON object, or the turn crashed/timed out before
    ever writing to stdout) -- callers fall back to events-window scanning.
    """
    if not text or not text.strip():
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        # Defensive: the CLI should never print more than one JSON value to
        # stdout in json mode, but if extra bytes ever land here, the last
        # line that parses as JSON is still the best available signal.
        parsed = None
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            break
    if not isinstance(parsed, dict):
        return None
    response = parsed.get('response')
    return response if isinstance(response, str) else None


def _run_scenario_turns(root, name, manifest, item, task, workspace, sessions, env, run):
    """Launch every turn of a multi-turn scenario task, resuming the same
    Amplifier session for turns 2..N. Returns (turns, sid) where `turns` is
    a list of per-turn result dicts (see `worker`'s scenario branch) in turn
    order, and `sid` is the discovered session id (None if turn 1 never
    produced one).

    On a turn failure (nonzero/None exit code, or a timeout, or -- turn 1
    only -- no session discovered), stops launching further turns and marks
    every remaining turn 'skipped' rather than guessing at their outcome.
    """
    import battery_tasks
    turn_prompts = battery_tasks.scenario_turn_prompts(task.subtasks)
    expected_prompt = battery_tasks.SCENARIO_TURN_SEPARATOR.join(turn_prompts)
    if expected_prompt != item.get('prompt'):
        raise SystemExit('worker: scenario turn prompts do not reconstruct the preregistered '
                          'scenario prompt; refusing to launch amplifier')
    deadline_seconds = item.get('deadline_seconds') or manifest['limits']['timeout_seconds']
    turn_gap_seconds = manifest.get('turn_gap_seconds') or 0
    bundle_uri = (run/'profile.md').as_uri()
    sid = None
    turns = []
    failed = False
    for i, subtask_name in enumerate(task.subtasks, start=1):
        if failed:
            turns.append({'index': i, 'subtask': subtask_name, 'started_at': None, 'ended_at': None,
                           'exit_code': None, 'timed_out': False, 'final_message': None, 'skipped': True})
            continue
        if i > 1 and turn_gap_seconds:
            time.sleep(turn_gap_seconds)
        command = ['amplifier', 'run', '--bundle', bundle_uri, '--mode', 'single',
                   '--provider', manifest['provider'], '--model', manifest['model'],
                   '--output-format', 'json']
        if i > 1:
            if sid is None:
                raise SystemExit(f'worker: cannot --resume turn {i} of scenario {name!r} -- no session id')
            command += ['--resume', sid]
        command += [turn_prompts[i-1]]
        before = set(sessions.iterdir()) if sessions.exists() else set()
        started_at = datetime.now(timezone.utc).isoformat()
        started = time.perf_counter()
        # `--output-format json` redirects Amplifier's OWN diagnostic/progress
        # output to stderr, so stdout is exactly one JSON object with a clean
        # 'response' field -- capture it to a per-turn file (never a pipe:
        # avoids any risk of a full-buffer deadlock across a long-running,
        # possibly-terminated-and-killed turn). This is the only reliable way
        # to get THIS turn's real answer: on a --resume'd turn, events.jsonl
        # also contains the session-naming background call's own llm:response
        # (fires during cleanup, AFTER the real answer), which can land after
        # the real response within the turn's own [started_at, ended_at]
        # window and get mistaken for the final message by window scanning.
        stdout_path = run/f'turn{i}-stdout.txt'
        with stdout_path.open('w') as stdout_fh:
            process = subprocess.Popen(command, cwd=workspace, env=env, stdout=stdout_fh)
            dump(run/'running.json', {'started_at': started_at, 'name': name, 'controller_pid': os.getpid(),
                                       'pid': process.pid, 'attempt': item.get('attempt', 1),
                                       'deadline_seconds': deadline_seconds, 'turn': i, 'tty': sys.stdout.isatty()})
            print('FORGE_E2E_STARTED '+name, flush=True)
            timed_out = False
            try:
                code = process.wait(timeout=deadline_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.terminate()
                try:
                    code = process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    code = process.wait()
        elapsed_ms = (time.perf_counter()-started)*1000
        ended_at = datetime.now(timezone.utc).isoformat()
        if i == 1:
            found = [p for p in sessions.iterdir() if p not in before and (p/'events.jsonl').exists()] if sessions.exists() else []
            sid = found[0].name if len(found) == 1 else None
        session_dir = (sessions/sid) if sid else None
        try:
            stdout_text = stdout_path.read_text()
        except OSError:
            stdout_text = ''
        final_message = _parse_amplifier_stdout_response(stdout_text)
        final_message_source = 'stdout_response' if final_message is not None else None
        if final_message is None:
            final_message = _extract_final_message_window(session_dir, started_at, ended_at)
            final_message_source = 'events_window' if final_message is not None else None
        if final_message is None and i == len(task.subtasks):
            final_message = _extract_final_message(session_dir, workspace)
            final_message_source = 'events_whole_session' if final_message is not None else None
        turn_ok = (code == 0) and not timed_out and (sid is not None)
        turns.append({'index': i, 'subtask': subtask_name, 'started_at': started_at, 'ended_at': ended_at,
                       'exit_code': code, 'timed_out': timed_out, 'elapsed_ms': elapsed_ms,
                       'final_message': final_message[:4000] if isinstance(final_message, str) else final_message,
                       'final_message_source': final_message_source, 'skipped': False})
        if not turn_ok:
            failed = True
    return turns, sid


def _worker_scenario(root, name, manifest, item, workspace, source_root, run):
    """worker()'s multi-turn path: launch every scenario turn (see
    `_run_scenario_turns`), fold per-turn grading, and write result.json in
    the same shape single-turn runs use, plus a `turns` breakdown.
    """
    import battery_tasks
    task = battery_tasks.TASKS[item['task']]
    _warm_local_scorer(manifest['sides'][item['side']])
    slug = str(workspace.resolve()).replace('/', '-').replace('\\', '-').replace(':', '')
    sessions = Path.home()/'.amplifier/projects'/slug/'sessions'
    env = dict(os.environ, AFAST_OBSERVATORY='off')
    env['PYTHONPATH'] = str(source_root/'src')
    _assert_bundle_uri_safe(run/'profile.md')

    turns, sid = _run_scenario_turns(root, name, manifest, item, task, workspace, sessions, env, run)

    native = native_summary(sessions/sid) if sid else None
    effort = effort_summary(sessions/sid) if sid else []
    events_dir = run/'events' if (run/'events').is_dir() else Path(manifest.get('events_dir', str(EVENTS)))
    measured = extract_receipts(source_root, events_dir, sid, run) if sid else None

    quality_parts = []
    for t in turns:
        subtask_name = t['subtask']
        turn_label = battery_tasks.scenario_turn_dir(t['index'], subtask_name)
        if t.get('skipped'):
            quality_parts.append((turn_label, {'checks': 1, 'passed': 0, 'failed': 1,
                                                 'failure_labels': ['turn_skipped_after_earlier_failure']}))
            continue
        turn_workspace = workspace/turn_label
        try:
            q = battery_tasks.evaluate_scenario_turn(subtask_name, turn_workspace, t.get('final_message'))
        except Exception as exc:  # noqa: BLE001 -- a turn evaluator must never crash the worker
            q = {'checks': 1, 'passed': 0, 'failed': 1, 'failure_labels': [f'turn_evaluate_error:{exc}']}
        quality_parts.append((turn_label, q))
        t['quality'] = q
        t['turn_passed'] = (not t['timed_out']) and t['exit_code'] == 0 and q['failed'] == 0
    quality = battery_tasks.fold_scenario_qualities(quality_parts)

    protected = _task_protected(item['task'])
    files = _task_files(item['task'])
    unchanged = {f: (workspace/f).exists() and (workspace/f).read_text() == files.get(f, '') for f in protected}
    source_observed = _source_observed(run)
    source_expected = {'git_sha': manifest['sides'][item['side']]['source_git_sha'],
                        'tree_sha256': manifest['sides'][item['side']]['source_tree_sha256']}
    source_match = None if source_observed is None else (
        source_observed.get('source_git_sha') == source_expected['git_sha']
        and source_observed.get('source_tree_sha256') == source_expected['tree_sha256'])
    mode_observed = _mode_observed(run)
    side = manifest['sides'][item['side']]
    mode_match = None if mode_observed is None else (
        (mode_observed == {'active'}) if side['mode'] == 'active' else mode_observed <= {'off'})
    infrastructure_failure = sid is None
    harness = 'amplifier-fd' if side['mode'] == 'active' else 'amplifier-plain'
    model = next((e['model'] for e in effort if e.get('model')), None)
    wall_time_ms = sum(t.get('elapsed_ms') or 0 for t in turns)
    started_at = next((t['started_at'] for t in turns if t.get('started_at')), None)
    ended_at = next((t['ended_at'] for t in reversed(turns) if t.get('ended_at')), None)
    any_timed_out = any(t['timed_out'] for t in turns)
    last_real_turn = next((t for t in reversed(turns) if not t.get('skipped')), None)
    exit_code = last_real_turn['exit_code'] if last_real_turn else None
    final_message = last_real_turn['final_message'] if last_real_turn else None
    result = {'name': name, 'task': item['task'], 'side': item['side'], 'session_id': sid,
              'exit_code': exit_code, 'timed_out': any_timed_out, 'wall_time_ms': wall_time_ms,
              'native': native, 'measurements': measured, 'quality': quality, 'public_tests_passed': None,
              'workspace_tests_passed': None, 'workspace_tests_runner': None,
              'protected_files_unchanged': unchanged, 'final_solution_sha256': None,
              'attempt': item.get('attempt', 1), 'deadline_seconds': item.get('deadline_seconds')
              or manifest['limits']['timeout_seconds'], 'started_at': started_at, 'ended_at': ended_at,
              'source_expected': source_expected, 'source_observed': source_observed, 'source_match': source_match,
              'mode_expected': side['mode'], 'mode_observed': sorted(mode_observed) if mode_observed is not None else None,
              'mode_match': mode_match, 'retry_count': native['provider_retries'] if native else None,
              'effort_receipts': effort, 'new_session_dirs': 1 if sid else 0,
              'infrastructure_failure': infrastructure_failure, 'harness': harness, 'model': model,
              'final_message': final_message, 'turns': turns}
    outcome_passed = (not infrastructure_failure and not any_timed_out and quality['failed'] == 0
                       and all(unchanged.values()) and all(t.get('turn_passed', False) for t in turns))
    result['outcome_passed'] = outcome_passed
    dump(run/'result.json', result)
    _unregister_benchmark_bundle()
    print('FORGE_E2E_FINISHED '+json.dumps({'name': name, 'outcome': result['outcome_passed'],
                                             'wall_ms': round(wall_time_ms), 'checks': quality,
                                             'session_id': sid}), flush=True)
    return 0 if result['outcome_passed'] else 1


def _default_config():
    tasks = list(SPECS)
    runs = []
    for i, task in enumerate(tasks):
        order = ['baseline', 'fast'] if i % 2 == 0 else ['fast', 'baseline']
        for side in order:
            runs.append({'name': f'{task}-{side}', 'task': task, 'side': side, 'rep': 1, 'attempt': 1, 'block': i, 'seed': None})
    return {
        'runs': runs,
        'sides': {
            'baseline': {'source_root': str(CACHE), 'mode': 'off'},
            'fast': {'source_root': str(CACHE), 'mode': 'active'},
        },
        'upstream_loop_source': UPSTREAM_LOOP_SOURCE,
        'provider': 'anthropic', 'model': 'claude-fable-5-1',
        'limits': {'timeout_seconds': 480, 'max_iterations': 30, 'extended_thinking': True},
        'events_dir': str(EVENTS), 'host_python': str(HOST_PYTHON), 'forge_py': str(FORGE),
        'prompt': PROMPT,
    }


def _side_profile(name, side, task, workspace, config):
    upstream = {'max_iterations': config['limits']['max_iterations'], 'extended_thinking': config['limits']['extended_thinking']}
    source_root = Path(side['source_root'])
    decision = {**DEFAULT_DECISION, **side.get('decision_overrides', {})}
    if side.get('composition') == 'composed':
        return _composed_profile(name, side, task, workspace, config, upstream)
    if side['mode'] == 'active':
        # allow_external_state must come from `decision` (DEFAULT_DECISION, overridable via
        # side['decision_overrides']), never a hardcoded False here -- that silently dropped
        # --allow-external-state/--fd-override allow_external_state=true on every prepare, so
        # a jev-backed profile always had allow_external_state: false and the service refused
        # every request (fast_decisions:fallback reason_code external_state_not_enabled; zero
        # fast_decisions:scored). See docs/EVENTS.md and battery.py's cmd_prepare.
        loop_config = {**decision, 'mode': 'active',
                        'allow_external_state': decision.get('allow_external_state', False),
                        'events_dir': config['events_dir'], 'upstream': upstream,
                        'observatory': {'enabled': False}}
        loop = {'module': 'loop-fast-decisions', 'source': (source_root/'modules/loop-fast-decisions').as_uri(), 'config': loop_config}
        hook_config = {**loop_config, 'session_label': f'Forge {task} / active', 'observatory': {'enabled': False}}
        hooks = [{'module': 'hooks-fast-decisions', 'source': (source_root/'modules/hooks-fast-decisions').as_uri(), 'config': hook_config}]
    else:
        loop = {'module': 'loop-streaming', 'source': config.get('upstream_loop_source', UPSTREAM_LOOP_SOURCE), 'config': upstream}
        off_config = {**decision, 'events_dir': config['events_dir'], 'upstream': upstream, 'session_label': f'Forge {task}', 'observatory': {'enabled': False}}
        hooks = [{'module': 'hooks-fast-decisions', 'source': (source_root/'modules/hooks-fast-decisions').as_uri(), 'config': {**off_config, 'mode': 'off'}}]
    # One fixed bundle name for every benchmark profile: Amplifier records each `--bundle` it loads in
    # ~/.amplifier/registry.json keyed by name, so unique per-run names left 80+ stale 'Local' entries.
    # The worker also removes the entry after the run (see _unregister_benchmark_bundle).
    fast_workspace_tool = {'module': 'tool-fast-workspace', 'source': (source_root/'modules/tool-fast-workspace').as_uri(),
                            'config': {'root': str(workspace)}}

    # --amplifier-bundle {foundation,lean} (battery.py prepare): 'foundation' (default,
    # unchanged behavior) includes the fast-decisions bundle root, which transitively
    # pulls in the full foundation bundle (agents/context/behaviors, ~50k tokens of
    # composed system instruction). 'lean' isolates that installed-context weight by
    # composing an explicit minimal root instead: no bundle include at all, just the
    # baseline tools (tool-filesystem, tool-bash, tool-todo), provider-anthropic, and
    # the fast-decisions hook/tool this profile already builds above. Orchestrator
    # selection (loop/loop-streaming above) is unaffected either way -- it never came
    # from foundation's include chain.
    amplifier_bundle = config.get('amplifier_bundle') or 'foundation'
    if amplifier_bundle not in ('foundation', 'lean'):
        raise ValueError(f"amplifier_bundle must be 'foundation' or 'lean', got {amplifier_bundle!r}")

    provider_config = {}
    amplifier_effort = config.get('amplifier_effort')
    if amplifier_effort:
        # --amplifier-effort (battery.py prepare) pins Policy-independent generative reasoning
        # effort for the harness model itself -- not to be confused with the fast-decisions
        # backend's own `decision`/`model`. Applied identically on BOTH amplifier sides (plain
        # and fd) so a paired comparison never silently compares two different effort levels.
        # `reasoning_effort` is the provider-anthropic canonical config key (see docs/EVENTS.md
        # and amplifier_module_provider_anthropic).
        provider_config['reasoning_effort'] = amplifier_effort

    if amplifier_bundle == 'lean':
        includes = []
        tools = [
            {'module': 'tool-filesystem', 'source': LEAN_MODULE_SOURCES['tool-filesystem']},
            {'module': 'tool-bash', 'source': LEAN_MODULE_SOURCES['tool-bash']},
            {'module': 'tool-todo', 'source': LEAN_MODULE_SOURCES['tool-todo']},
            fast_workspace_tool,
        ]
        # No transitively-included foundation here, so provider-anthropic needs its own
        # explicit source -- unlike the foundation-bundle case below, entry-point/foundation
        # resolution is not available to fall back on.
        providers = [{'module': 'provider-anthropic', 'source': LEAN_MODULE_SOURCES['provider-anthropic'],
                      'config': provider_config}]
    else:
        includes = [{'bundle': source_root.as_uri()}]
        tools = [fast_workspace_tool]
        # No `source` here -- this overrides the module's config on top of whatever already
        # resolved it (installed package/entry point, or the transitively-included foundation
        # bundle). Omitted entirely when there's nothing to override (unchanged behavior).
        providers = [{'module': 'provider-anthropic', 'config': provider_config}] if provider_config else None

    profile = {'bundle': {'name': BENCHMARK_BUNDLE_NAME, 'version': '0.1.0'}, 'includes': includes,
               'session': {'orchestrator': loop},
               'tools': tools,
               'hooks': hooks}
    if amplifier_bundle == 'lean':
        profile['session']['context'] = {
            'module': 'context-simple',
            'source': 'git+https://github.com/microsoft/amplifier-module-context-simple@main'}
        profile['hooks'].append({
            'module': 'hooks-logging',
            'source': 'git+https://github.com/microsoft/amplifier-module-hooks-logging@main',
            'config': {'mode': 'session-only',
                       'session_log_template': '~/.amplifier/projects/{project}/sessions/{session_id}/events.jsonl'}})
    if providers:
        profile['providers'] = providers
    return profile


def _composed_profile(name, side, task, workspace, config, upstream):
    """The product exactly as a user composes it: include the bundle root
    (foundation + behaviors/fast-decisions.yaml) and let composition pick the
    orchestrator. The profile never names an orchestrator module -- it only
    repoints module sources at the frozen candidate tree and sets run-local
    config (events dir, no dashboard, iteration limit) -- so a run whose
    receipts show loop-fast-decisions proves the bundle itself replaced
    foundation's loop-streaming. Decision policy is the shipped default plus
    only explicit --fd-override values (DEFAULT_DECISION is NOT applied).
    `upstream` limits go in at the top level, exercising the same forwarding
    path a root's loop-streaming settings take (orchestrator.upstream_config)."""
    source_root = Path(side['source_root'])
    orchestrator_config = {**side.get('decision_overrides', {}), **upstream,
                           'events_dir': config['events_dir'], 'observatory': {'enabled': False}}
    return {
        'bundle': {'name': BENCHMARK_BUNDLE_NAME, 'version': '0.1.0'},
        'includes': [{'bundle': source_root.as_uri()}],
        'session': {'orchestrator': {
            'source': (source_root/'modules/loop-fast-decisions').as_uri(),
            'config': orchestrator_config}},
        'tools': [{'module': 'tool-fast-workspace', 'source': (source_root/'modules/tool-fast-workspace').as_uri(),
                   'config': {'root': str(workspace)}}],
        'hooks': [{'module': 'hooks-fast-decisions', 'source': (source_root/'modules/hooks-fast-decisions').as_uri(),
                   'config': {'events_dir': config['events_dir'], 'session_label': f'Forge {task} / composed',
                              'observatory': {'enabled': False}}}],
    }


def _deep_merge(base, overlay):
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def composed_effective_config(source_root, overrides=None):
    """The loop-fast-decisions config a composed run actually gets: the frozen
    source's shipped behaviors/fast-decisions.yaml orchestrator config, deep-
    merged with explicit --fd-override values (the kernel's own merge rule).
    Recorded next to profile.md so battery.py's series label and mechanism
    checks describe the shipped defaults, not the (deliberately sparse)
    profile. None when the source has no orchestrator-declaring behavior."""
    import yaml
    path = Path(source_root)/'behaviors'/'fast-decisions.yaml'
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text()) or {}
    shipped = ((data.get('session') or {}).get('orchestrator') or {}).get('config')
    if not isinstance(shipped, dict):
        return None
    return _deep_merge(shipped, overrides or {})


def _build_workspace(run_dir, task):
    workspace = run_dir/'workspace'
    (workspace/'.amplifier').mkdir(parents=True, exist_ok=True)
    for relpath, content in _task_files(task).items():
        target = workspace/relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    (workspace/'.amplifier/settings.local.yaml').write_text('bundle:\n  app: []\n')
    subprocess.run(['git', 'init', '-q', str(workspace)], check=True)
    return workspace


def _build_run(root, run_spec, config, sides):
    name = run_spec['name']
    run = root/_slug(name)
    run.mkdir(parents=True, exist_ok=True)
    task = run_spec['task']
    workspace = _build_workspace(run, task)
    side = sides[run_spec['side']]
    # Per-run events dir: the shared ~/.amplifier/fast-decisions/events grows
    # past read_receipts' 100k-record window (files sorted by session-id name,
    # not time), which silently dropped whole sessions from receipts.jsonl.
    config = {**config, 'events_dir': str(run/'events')}
    profile = _side_profile(name, side, task, workspace, config)
    (run/'profile.md').write_text('---\n'+json.dumps(profile, indent=2)+'\n---\n')
    if side.get('composition') == 'composed':
        effective = composed_effective_config(side['source_root'], profile['session']['orchestrator']['config'])
        if effective is not None:
            dump(run/'effective-loop-config.json', effective)
    prompt = run_spec.get('prompt') or _task_prompt(task) or config.get('prompt', PROMPT)
    item = {
        'task': task, 'side': run_spec['side'], 'rep': run_spec.get('rep', 1),
        'attempt': run_spec.get('attempt', 1), 'block': run_spec.get('block'), 'seed': run_spec.get('seed'),
        'workspace_hash': hash_files(workspace), 'profile_sha256': hashlib.sha256((run/'profile.md').read_bytes()).hexdigest(),
        'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
    }
    if 'prompt' in run_spec:
        item['prompt'] = run_spec['prompt']
    if 'deadline_seconds' in run_spec:
        item['deadline_seconds'] = run_spec['deadline_seconds']
    return item


def prepare(root, config=None):
    root.mkdir(parents=True, exist_ok=False)
    legacy = config is None
    if legacy:
        config = _default_config()
    sides = {}
    for side_name, side in config['sides'].items():
        source_root = Path(side['source_root'])
        sides[side_name] = {**side, 'source_root': str(source_root),
                             'source_git_sha': git_sha(source_root),
                             'source_tree_sha256': tree_sha256(source_root/'src'/'amplifier_fast_decisions')}
    installed_commit = git_sha(CACHE)
    versions={name:metadata.version(name) for name in ['amplifier-app-cli','amplifier-core','amplifier-foundation','amplifier-module-loop-streaming','amplifier-provider-anthropic'] if available(name)}
    local_digest = None
    try:
        with urllib.request.urlopen('http://127.0.0.1:11434/api/tags', timeout=5) as response:
            models=json.load(response)['models']
        wanted = config.get('sides', {}).get('fast', {}).get('decision_overrides', {}).get('model', 'qwen3:0.6b')
        match = next((m for m in models if m['name']==wanted), None)
        local_digest = match.get('digest') if match else None
    except Exception:
        local_digest = None
    prompt = config.get('prompt', PROMPT)
    manifest={'schema':'forge-e2e-v1','created_at':datetime.now(timezone.utc).isoformat(),
              'installed_commit':installed_commit,'python':platform.python_version(),
              'hardware':platform.platform(),'packages':versions,'provider':config.get('provider', 'anthropic'),
              'model':config.get('model', 'claude-fable-5-1'),'provider_revision':'unknown',
              'decision_model':DEFAULT_DECISION['model'],'decision_model_digest':local_digest,
              'amplifier_effort':config.get('amplifier_effort'),
              'amplifier_bundle':config.get('amplifier_bundle', 'foundation'),
              'prompt':prompt,'prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest(),
              'evaluator_sha256':hashlib.sha256(Path(__file__).with_name('forge_workloads.py').read_bytes()).hexdigest(),
              'sides': sides, 'upstream_loop_source': config.get('upstream_loop_source', UPSTREAM_LOOP_SOURCE),
              'events_dir': config.get('events_dir', str(EVENTS)), 'host_python': config.get('host_python', str(HOST_PYTHON)),
              'forge_py': config.get('forge_py', str(FORGE)), 'task_source': config.get('task_source'),
              'turn_gap_seconds': config.get('turn_gap_seconds', 0),
              'run_order':[],'runs':{},'limits':config.get('limits', {'max_iterations':30,'extended_thinking':True,'timeout_seconds':480})}
    for run_spec in config['runs']:
        manifest['runs'][run_spec['name']] = _build_run(root, run_spec, config, sides)
        manifest['run_order'].append(run_spec['name'])
    # Pairs sharing (task, rep) must start from identical workspace content.
    groups = {}
    for name, item in manifest['runs'].items():
        groups.setdefault((item['task'], item['rep']), []).append((name, item['workspace_hash']))
    for (task, rep), members in groups.items():
        hashes = {h for _, h in members}
        assert len(hashes) == 1, f'Workspace mismatch within task={task} rep={rep}: {members}'
    dump(root/'manifest.json',manifest)
    print(json.dumps({'prepared':str(root),'runs':manifest['run_order'],'installed_commit':installed_commit}))
    return manifest


def add_run(root, run_spec):
    """Append one more run to an already-prepared root (used for retries)."""
    manifest = json.loads((root/'manifest.json').read_text())
    config = {'events_dir': manifest['events_dir'], 'upstream_loop_source': manifest['upstream_loop_source'],
              'limits': manifest['limits'], 'prompt': manifest.get('prompt', PROMPT),
              'amplifier_effort': manifest.get('amplifier_effort'),
              'amplifier_bundle': manifest.get('amplifier_bundle', 'foundation')}
    manifest['runs'][run_spec['name']] = _build_run(root, run_spec, config, manifest['sides'])
    manifest['run_order'].append(run_spec['name'])
    task, rep = run_spec['task'], run_spec.get('rep', 1)
    hashes = {item['workspace_hash'] for name, item in manifest['runs'].items() if item['task']==task and item['rep']==rep}
    assert len(hashes) == 1, f'Workspace mismatch within task={task} rep={rep}'
    dump(root/'manifest.json', manifest)
    return manifest


def native_summary(session_dir):
    counts=Counter(); usage=Counter(); usage_known=Counter(); durations=[]; tools=[]; complete=False
    path=session_dir/'events.jsonl'
    for line in path.open():
        try:e=json.loads(line)
        except ValueError:continue
        name=e.get('event');d=e.get('data',{});counts[name]+=1
        if name=='llm:response':
            if isinstance(e.get('duration_ms'),(int,float)):durations.append(e['duration_ms'])
            for key in ['input_tokens','output_tokens','cache_read_tokens','cache_write_tokens','cost_usd']:
                v=d.get('usage',{}).get(key)
                if v is not None:
                    try:usage[key]+=float(v);usage_known[key]+=1
                    except (TypeError,ValueError):pass
        if name=='tool:post':
            result=d.get('result',{})
            tools.append({'tool':d.get('tool_name'),'call_id':d.get('tool_call_id'),
                          'success':result.get('success') if isinstance(result,dict) else None})
        if name=='execution:end':complete=True
    return {'provider_requests':counts['llm:request'],'provider_responses':counts['llm:response'],
            'provider_retries':counts['provider:retry'],'provider_errors':counts['provider:error'],
            'tool_results':len(tools),'tool_names':dict(Counter(t['tool'] for t in tools)),
            'tool_failures_reported':sum(t['success'] is False for t in tools),'execution_completed':complete,
            'provider_response_duration_ms':sum(durations),
            'usage':{k:usage[k] if usage_known[k]==counts['llm:response'] and counts['llm:response'] else None for k in ['input_tokens','output_tokens','cache_read_tokens','cache_write_tokens','cost_usd']},
            'usage_known_calls':dict(usage_known),'cost_evidence':'provider_reported_estimate_not_billing'}


def effort_summary(session_dir):
    """Counter of (model, thinking_enabled, thinking_budget) over llm:request events."""
    counts = Counter()
    path = session_dir/'events.jsonl'
    for line in path.open():
        try:e=json.loads(line)
        except ValueError:continue
        if e.get('event') != 'llm:request':continue
        d = e.get('data', {})
        key = (d.get('model'), d.get('thinking_enabled'), d.get('thinking_budget'))
        counts[key] += 1
    return [{'model': m, 'thinking_enabled': t, 'thinking_budget': b, 'count': n} for (m, t, b), n in counts.items()]


def _source_observed(run):
    receipts = run/'receipts.jsonl'
    if not receipts.exists():
        return None
    for line in receipts.read_text().splitlines():
        try:e = json.loads(line)
        except ValueError:continue
        if e.get('event') == 'fast_decisions:source':
            d = e.get('data', {})
            return {k: d.get(k) for k in ['source_git_sha', 'source_tree_sha256', 'source_kind', 'module']}
    return None


RECEIPT_EXTRACTOR = """
import json, sys
from pathlib import Path
from amplifier_fast_decisions.operations import measure, read_receipts
events_dir, sid, run = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
measured = measure(events_dir, session_id=sid)
rows, _ = read_receipts(events_dir)
ids = {s['session_id'] for s in measured['sessions']}
(run/'receipts.jsonl').write_text(''.join(json.dumps(e)+'\\n' for e in rows if e['session_id'] in ids))
(run/'measurements.json').write_text(json.dumps(measured))
"""


def extract_receipts(source_root, events_dir, sid, run, python=None, timeout=120):
    """Write ``run/receipts.jsonl`` + ``run/measurements.json`` using the SIDE's source and return the measurements.

    Runs in a subprocess with ``PYTHONPATH=<source_root>/src`` so the side under test decides which event
    names and fields survive (its own allowlist), and so the calling process keeps its imported modules.
    Returns None (and writes ``receipts-error.txt``) when extraction fails; the run is still reported.
    """
    env = dict(os.environ, PYTHONPATH=str(Path(source_root)/'src'))
    try:
        proc = subprocess.run([python or sys.executable, '-c', RECEIPT_EXTRACTOR, str(events_dir), sid, str(run)],
                              env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        (run/'receipts-error.txt').write_text('receipt extraction timed out\n')
        return None
    if proc.returncode != 0 or not (run/'measurements.json').exists():
        (run/'receipts-error.txt').write_text(proc.stderr[-4000:])
        return None
    return json.loads((run/'measurements.json').read_text())


def _unregister_benchmark_bundle(name=None):
    """Remove the benchmark profile's registry entry that `amplifier run --bundle` just created (best effort)."""
    try:
        subprocess.run(['amplifier','bundle','remove',name or BENCHMARK_BUNDLE_NAME],capture_output=True,text=True,timeout=120)
    except (OSError,subprocess.TimeoutExpired):
        pass


def _mode_observed(run):
    """Distinct decision modes reported by the run's own receipts (turn_start/requested/routed/source events)."""
    receipts = run/'receipts.jsonl'
    if not receipts.exists():
        return None
    modes = set()
    for line in receipts.read_text().splitlines():
        try:e = json.loads(line)
        except ValueError:continue
        if e.get('event') in {'fast_decisions:turn_start', 'fast_decisions:requested', 'fast_decisions:routed', 'fast_decisions:source'}:
            m = (e.get('data') or {}).get('mode')
            if m is not None:
                modes.add(m)
    return modes


def _interpreter_with_pytest():
    """First of (sys.executable, python3 on PATH) that can `import pytest`, or None.

    Never imports pytest into this process -- each candidate is probed in its
    own subprocess.
    """
    candidates = []
    if sys.executable:
        candidates.append(sys.executable)
    py3 = shutil.which('python3')
    if py3 and py3 not in candidates:
        candidates.append(py3)
    for interp in candidates:
        try:
            proc = subprocess.run([interp, '-c', 'import pytest'], capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0:
            return interp
    return None


def run_workspace_tests(workspace, timeout=45, targets=None):
    """Run whatever tests exist in `workspace` with the best available runner,
    always in a fresh subprocess (never imports candidate code in-process).

    Prefers pytest (handles both pytest-style and unittest-style test files);
    falls back to `unittest discover` only when no interpreter on this host can
    import pytest. `targets`, if given, restricts the run to those paths/files
    (e.g. ['test_public.py']); otherwise the whole workspace is discovered.

    Returns (passed, runner, summary):
      passed: True (all pass), False (a failure/timeout/error), or None (no
              tests were collected -- not applicable, not a failure).
      runner: 'pytest' or 'unittest'.
      summary: tail of combined stdout+stderr, for diagnostics.
    """
    workspace = Path(workspace)
    interp = _interpreter_with_pytest()
    if interp is not None:
        argv = [interp, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', *(targets or [])]
        # Auto-loaded third-party pytest plugins from this host's environment (installed
        # via setuptools entrypoints, e.g. framework-specific pytest plugins) can crash on
        # import when the candidate workspace's PYTHONPATH doesn't include their deps --
        # that is not a test failure, so run pytest in its bare, plugin-autoload-free mode.
        env = {**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
        try:
            proc = subprocess.run(argv, cwd=workspace, capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return False, 'pytest', 'pytest timed out'
        summary = ((getattr(proc, 'stdout', None) or '') + (getattr(proc, 'stderr', None) or ''))[-4000:]
        if proc.returncode == 0:
            return True, 'pytest', summary
        if proc.returncode == 5:  # no tests collected
            return None, 'pytest', summary
        return False, 'pytest', summary
    unittest_argv = [sys.executable, '-m', 'unittest'] + (['-v', *targets] if targets else ['discover', '-v'])
    try:
        proc = subprocess.run(unittest_argv, cwd=workspace, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, 'unittest', 'unittest timed out'
    summary = ((getattr(proc, 'stdout', None) or '') + (getattr(proc, 'stderr', None) or ''))[-4000:]
    if proc.returncode == 5:  # NO TESTS RAN (Python 3.12+)
        return None, 'unittest', summary
    return proc.returncode == 0, 'unittest', summary


def _ensure_task_source_registered(manifest):
    """Register a manifest's `task_source` (if any) with forge_workloads so
    task_files/task_prompt/task_protected/evaluate can resolve tasks from it
    in THIS process. Every entry point that resolves a task from a bare
    manifest read (worker(), and the `evaluate` CLI subcommand) is a fresh
    process and must call this before touching forge_workloads' task
    accessors -- see forge_workloads.register_source."""
    task_source = manifest.get('task_source')
    if task_source:
        forge_workloads.register_source(task_source['kind'],
                                         **{k: v for k, v in task_source.items() if k != 'kind'})


def _warm_local_scorer(side):
    """Warm only an active Ollama judge, never an unused local model."""
    if side.get('mode') != 'active':
        return
    overrides = side.get('decision_overrides', {})
    if side.get('composition') == 'composed':
        decision = composed_effective_config(side['source_root'], overrides) or {}
    else:
        decision = {**DEFAULT_DECISION, **overrides}
    if decision.get('backend') != 'ollama':
        return
    origin = decision.get('ollama_url', 'http://127.0.0.1:11434').rstrip('/')
    warm = urllib.request.Request(origin + '/api/generate', data=json.dumps({
        'model': decision.get('model', 'qwen3:0.6b'), 'stream': False,
        'keep_alive': '20m', 'options': {'num_ctx': 4096},
    }).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(warm, timeout=60) as response:
        json.load(response)


def worker(root,name):
    manifest=json.loads((root/'manifest.json').read_text());_ensure_task_source_registered(manifest);run=root/_slug(name);workspace=run/'workspace';item=manifest['runs'][name]
    if hash_files(workspace)!=item['workspace_hash']:raise RuntimeError('Starting workspace changed')
    side = manifest['sides'][item['side']]
    source_root = Path(side['source_root'])
    if tree_sha256(source_root/'src'/'amplifier_fast_decisions') != side['source_tree_sha256']:
        raise RuntimeError('Source changed during the experiment')
    task = item['task']
    prompt = item.get('prompt')
    if prompt is None:
        if task not in SPECS:
            # A battery task (anything not in the legacy SPECS trio) must never silently
            # fall back to the generic legacy prompt -- that silently sends the wrong
            # instructions to the agent (see: amplifier runs getting the README-repair
            # boilerplate instead of their task-specific prompt).
            raise SystemExit(f'worker: run {name!r} (task={task!r}) has no prompt recorded; '
                              'refusing to fall back to the generic legacy prompt for a battery task')
        prompt = manifest['prompt']
    expected_prompt_sha256 = item.get('prompt_sha256')
    if expected_prompt_sha256 is not None:
        actual_prompt_sha256 = hashlib.sha256(prompt.encode()).hexdigest()
        if actual_prompt_sha256 != expected_prompt_sha256:
            raise SystemExit(f'worker: run {name!r} prompt does not match the preregistered '
                              f'prompt_sha256 (expected {expected_prompt_sha256}, got '
                              f'{actual_prompt_sha256}); refusing to launch amplifier')
    if _task_kind(task) == 'scenario':
        # Multi-turn scenario: entirely separate execution path (per-turn
        # --resume, per-turn grading fold). See _worker_scenario. The
        # single-turn path below (deadline/warm/slug/... through the rest
        # of this function) is never reached for a scenario task.
        return _worker_scenario(root, name, manifest, item, workspace, source_root, run)
    deadline_seconds = item.get('deadline_seconds') or manifest['limits']['timeout_seconds']
    _warm_local_scorer(side)
    slug=str(workspace.resolve()).replace('/','-').replace('\\','-').replace(':','')
    sessions=Path.home()/'.amplifier/projects'/slug/'sessions'
    before=set(sessions.iterdir()) if sessions.exists() else set()
    # AMPLIFIER_MEMORY_CAPTURE=off: this launches the real `amplifier run`
    # CLI as a full subprocess -- it inherits ALL of `os.environ` (unlike
    # the in-process bench/screen harness paths), so it also inherits
    # whatever memory bundle is installed for this user. Without this the
    # amplifier-bundle-memory hooks (capture/interject/briefing) treat every
    # worker run as a normal interactive session and write a capture after
    # every tool call, polluting the user's personal memory store with
    # automation noise (amplifier-bundle-memory's automation_gate module
    # honors this var; a memory bundle predating that fix is a silent no-op
    # here, not an error).
    env=dict(os.environ,AFAST_OBSERVATORY='off',AMPLIFIER_MEMORY_CAPTURE='off')
    env['PYTHONPATH'] = str(source_root/'src')
    _assert_bundle_uri_safe(run/'profile.md')
    command=['amplifier','run','--bundle',(run/'profile.md').as_uri(),'--mode','single','--provider',manifest['provider'],'--model',manifest['model'],'--output-format','json',prompt]
    started_at=datetime.now(timezone.utc).isoformat();started=time.perf_counter()
    process=subprocess.Popen(command,cwd=workspace,env=env)
    dump(run/'running.json',{'started_at':started_at,'name':name,'controller_pid':os.getpid(),'pid':process.pid,
                              'attempt':item.get('attempt',1),'deadline_seconds':deadline_seconds,
                              'tty':sys.stdout.isatty()})
    print('FORGE_E2E_STARTED '+name,flush=True)
    timed_out=False
    try:code=process.wait(timeout=deadline_seconds)
    except subprocess.TimeoutExpired:
        timed_out=True;process.terminate()
        try:code=process.wait(timeout=15)
        except subprocess.TimeoutExpired:process.kill();code=process.wait()
    elapsed=(time.perf_counter()-started)*1000
    ended_at=datetime.now(timezone.utc).isoformat()
    # events.jsonl exists during execution; metadata.json may only be written on
    # orderly shutdown. Preserve native evidence even for deadline failures.
    found=[p for p in sessions.iterdir() if p not in before and (p/'events.jsonl').exists()] if sessions.exists() else []
    sid=found[0].name if len(found)==1 else None
    native=native_summary(found[0]) if len(found)==1 else None
    effort=effort_summary(found[0]) if len(found)==1 else []
    # Extract receipts with the side's own source: the installed package's allowlist would silently drop
    # event names/fields that only the side under test emits (observed in HC00: no source event, no observation stats).
    # Done in a subprocess with PYTHONPATH=<side>/src so this process never swaps already-imported modules
    # (purging sys.modules here broke unrelated tests that patch amplifier_fast_decisions.operations).
    events_dir = run/'events' if (run/'events').is_dir() else Path(manifest.get('events_dir', str(EVENTS)))
    measured = extract_receipts(source_root, events_dir, sid, run) if sid else None
    kind = _task_kind(item['task'])
    if kind == 'answer':
        final_message = _extract_final_message(found[0] if len(found)==1 else None, workspace)
        try:
            import battery_tasks
            quality = battery_tasks.check_answer(battery_tasks.TASKS[item['task']], final_message)
        except Exception:
            quality = {'checks':0,'passed':0,'failed':1,'failure_labels':['check_answer_failed']}
        public_ok = None
        suite_ok = None
        workspace_tests_runner = None
    else:
        final_message = None
        # Execute the independent evaluator in its own process with a deadline.
        try:
            test=subprocess.run([sys.executable,str(Path(__file__).resolve()),'evaluate',str(root),name],capture_output=True,text=True,timeout=45)
            quality=json.loads(test.stdout)
        except (ValueError,subprocess.TimeoutExpired):
            quality={'checks':0,'passed':0,'failed':1,'failure_labels':['evaluator_failed_or_timed_out']}
        if (workspace/'test_public.py').exists():
            public_ok, _public_runner, _public_summary = run_workspace_tests(workspace, targets=['test_public.py'])
        else:
            public_ok=None
        suite_ok, workspace_tests_runner, _suite_summary = run_workspace_tests(workspace)
    protected = _task_protected(item['task'])
    files = _task_files(item['task'])
    unchanged={f:(workspace/f).exists() and (workspace/f).read_text()==files.get(f,'') for f in protected}
    source_observed = _source_observed(run)
    source_expected = {'git_sha': side['source_git_sha'], 'tree_sha256': side['source_tree_sha256']}
    if source_observed is None:
        source_match = None
    else:
        source_match = (source_observed.get('source_git_sha') == source_expected['git_sha']
                         and source_observed.get('source_tree_sha256') == source_expected['tree_sha256'])
    mode_observed = _mode_observed(run)
    mode_match = None if mode_observed is None else (
        (mode_observed == {'active'}) if side['mode'] == 'active' else mode_observed <= {'off'})
    infrastructure_failure = sid is None or code is None
    harness = 'amplifier-fd' if side['mode'] == 'active' else 'amplifier-plain'
    model = next((e['model'] for e in effort if e.get('model')), None)
    result={'name':name,'task':item['task'],'side':item['side'],'session_id':sid,'exit_code':code,'timed_out':timed_out,
            'wall_time_ms':elapsed,'native':native,'measurements':measured,'quality':quality,'public_tests_passed':public_ok,
            'workspace_tests_passed':suite_ok,'workspace_tests_runner':workspace_tests_runner,'protected_files_unchanged':unchanged,
            'final_solution_sha256':hashlib.sha256((workspace/'solution.py').read_bytes()).hexdigest() if (workspace/'solution.py').exists() else None,
            'attempt': item.get('attempt', 1), 'deadline_seconds': deadline_seconds,
            'started_at': started_at, 'ended_at': ended_at,
            'source_expected': source_expected, 'source_observed': source_observed, 'source_match': source_match,
            'mode_expected': side['mode'], 'mode_observed': sorted(mode_observed) if mode_observed is not None else None, 'mode_match': mode_match,
            'retry_count': native['provider_retries'] if native else None, 'effort_receipts': effort,
            'new_session_dirs': len(found), 'infrastructure_failure': infrastructure_failure,
            'harness': harness, 'model': model, 'final_message': final_message[:4000] if isinstance(final_message, str) else final_message}
    outcome_passed = code==0 and not timed_out and quality['failed']==0 and all(unchanged.values())
    if public_ok is not None:
        outcome_passed = outcome_passed and public_ok
    if suite_ok is not None:
        outcome_passed = outcome_passed and suite_ok
    result['outcome_passed']=outcome_passed
    dump(run/'result.json',result)
    _unregister_benchmark_bundle()
    print('FORGE_E2E_FINISHED '+json.dumps({'name':name,'outcome':result['outcome_passed'],'wall_ms':round(elapsed),'checks':quality,'session_id':sid}),flush=True)
    return 0 if result['outcome_passed'] else 1


def forge_self_heal(manifest):
    """Run `forge.py doctor` (fixes spawn-helper exec bits, restarts the daemon). Returns True when it reports healthy."""
    forge_py = Path(manifest.get('forge_py', str(FORGE))).expanduser()
    try:
        proc = subprocess.run([sys.executable, str(forge_py), 'doctor'], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return 'healthy' in (proc.stdout + proc.stderr)


def launch_run(root, name, forge_module=None, sleep_fn=None, now_fn=None,
                max_unreachable_retries=MAX_FORGE_UNREACHABLE_RETRIES):
    """Hand one run's worker command to Forge; return its observation dict.

    A Forge observation deadline is reported as an MCP error (SystemExit) but
    the launched process is explicitly kept alive -- that is not completion.

    Three distinct failure kinds get three distinct retries, each bounded and
    each retried WITHOUT consuming one of the run's own (attempt 1/attempt 2)
    slots -- a Forge hiccup is infrastructure, not a bad run:
      - 'Maximum sessions' (Forge's own terminal cap): reap our exited
        terminals once, then retry once.
      - 'posix_spawnp' (stale exec bit on Forge's spawn-helper): self-heal
        once, then retry once.
      - Forge unreachable/empty response (Forge itself down or restarting):
        exponential backoff with jitter (0.5s -> 30s cap), up to
        `max_unreachable_retries` times -- this is the case a bursty
        scheduler previously turned into a hammering storm.
    Every actual hand-off to Forge is also preceded by a minimum spacing wait
    (`_wait_for_launch_spacing`) shared across all threads in this process.
    """
    sleep_fn = sleep_fn or time.sleep
    now_fn = now_fn or time.monotonic
    manifest=json.loads((root/'manifest.json').read_text())
    run=root/_slug(name)
    if forge_module is None:
        forge_py = Path(manifest.get('forge_py', str(FORGE))).expanduser()
        sys.path.insert(0, str(forge_py.parent))
        import forge as forge_module
    host_python = manifest.get('host_python', str(HOST_PYTHON))
    cmd=shlex.join([str(host_python),str(Path(__file__).resolve()),'worker',str(root),name])
    # The Forge daemon's shell does not carry the user's API keys; source ~/.amplifier/keys.env (0600) into the
    # worker's environment so external decision backends (e.g. TYPESAFE_API_KEY) work. Values never appear in logs.
    cmd='set -a; . ~/.amplifier/keys.env 2>/dev/null; set +a; '+cmd
    session_attempt = 1  # 'Maximum sessions'/posix_spawnp retry gate: at most one retry each
    unreachable_retries = 0
    backoff = BACKOFF_INITIAL_SECONDS
    while True:
        _wait_for_launch_spacing(sleep_fn, now_fn)
        try:
            result=forge_module.call('run_command',{'command':'/bin/zsh','args':['-lc',cmd],
                'cwd':str(run/'workspace'),'timeoutMs':60000})
            break
        except SystemExit as exc:
            text=str(exc).removeprefix('forge: ')
            try:result=json.loads(text)
            except ValueError:
                # Forge itself is unreachable/restarting: back off with jitter and retry the
                # SAME launch attempt (never counted against the run's own attempt budget).
                if _is_forge_unreachable_error(text) and unreachable_retries < max_unreachable_retries:
                    unreachable_retries += 1
                    sleep_fn(backoff + random.uniform(0, backoff))
                    backoff = min(backoff*2, BACKOFF_CAP_SECONDS)
                    continue
                # Forge refuses new terminals once exited ones pile up. Reap only OUR exited worker
                # terminals (never live or unowned sessions) and retry once; otherwise fail loud.
                if 'Maximum sessions' in text and session_attempt == 1 and reap_exited_worker_terminals(forge_module, root):
                    session_attempt = 2
                    continue
                # A skills-cache refresh resets the exec bit on Forge's node-pty spawn-helper binaries
                # ("posix_spawnp failed"); `forge doctor` repairs it. Self-heal once, then fail loud.
                if 'posix_spawnp' in text and session_attempt == 1 and forge_self_heal(manifest):
                    session_attempt = 2
                    continue
                raise RuntimeError('forge launch failed: '+text) from None
            if result.get('timeout') is not True:
                raise RuntimeError('forge launch failed: '+text) from None
            break
    (run/'forge-output.txt').write_text(result.pop('output',''))
    dump(run/'forge-observation.json',result)
    return result


def reap_exited_worker_terminals(forge_module, root):
    """Close Forge terminals that have EXITED and were launched by this runner for ``root``.

    Live sessions and sessions that do not reference this campaign root are never touched.
    Returns True when at least one terminal was closed.
    """
    try:
        sessions=forge_module.call('list_terminals',{})
    except SystemExit:
        return False
    if isinstance(sessions,dict):
        sessions=sessions.get('sessions') or sessions.get('terminals') or []
    closed=0
    for s in sessions:
        if not isinstance(s,dict) or s.get('status')!='exited':
            continue
        if str(root) not in (s.get('name') or '')+' '+(s.get('cwd') or ''):
            continue
        try:forge_module.call('close_terminal',{'id':s['id']});closed+=1
        except SystemExit:pass
    return closed>0


def close_worker_terminal(root, name, forge_module=None):
    """Close the terminal this runner opened for ``name`` (recorded in forge-observation.json), if any."""
    obs=root/_slug(name)/'forge-observation.json'
    if not obs.exists():
        return False
    try:sid=json.loads(obs.read_text()).get('sessionId')
    except ValueError:return False
    if not sid:
        return False
    if forge_module is None:
        manifest=json.loads((root/'manifest.json').read_text())
        forge_py=Path(manifest.get('forge_py', str(FORGE))).expanduser()
        sys.path.insert(0,str(forge_py.parent))
        import forge as forge_module
    try:forge_module.call('close_terminal',{'id':sid});return True
    except SystemExit:return False


def wait_for_result(root, name, timeout_seconds):
    """Poll for result.json; bail out early if the launching controller died."""
    run = root/_slug(name)
    deadline = time.monotonic()+timeout_seconds
    dead_since = None
    while True:
        if (run/'result.json').exists():
            return True
        if time.monotonic() >= deadline:
            return (run/'result.json').exists()
        running = run/'running.json'
        if running.exists():
            try:
                pid = json.loads(running.read_text()).get('controller_pid')
            except ValueError:
                pid = None
            if pid is not None:
                try:
                    os.kill(pid, 0)
                    dead_since = None
                except ProcessLookupError:
                    if dead_since is None:
                        dead_since = time.monotonic()
                    elif time.monotonic()-dead_since >= 10:
                        return (run/'result.json').exists()
                except OSError:
                    pass
        time.sleep(2)


def batch(root):
    manifest=json.loads((root/'manifest.json').read_text())
    wait_seconds = manifest['limits']['timeout_seconds'] + 180
    for name in manifest['run_order']:
        run = root/_slug(name)
        if (run/'result.json').exists():continue
        print('LAUNCH '+name,flush=True)
        observation = launch_run(root, name)
        ok = wait_for_result(root, name, wait_seconds)
        if not ok:
            raise RuntimeError('Worker did not produce a result; inspect the owned Forge session before proceeding')
        # Only close our own completed terminal. Keep failed experiments in the report.
        if observation.get('sessionId'):
            forge_py = Path(manifest.get('forge_py', str(FORGE))).expanduser()
            sys.path.insert(0, str(forge_py.parent))
            import forge
            try:forge.call('close_terminal',{'id':observation['sessionId']})
            except SystemExit:pass
        outcome=json.loads((run/'result.json').read_text())
        print('COLLECT '+name+' '+str(outcome['outcome_passed']),flush=True)
        if outcome.get('infrastructure_failure'):
            raise RuntimeError('Infrastructure failure; fix setup before launching more paid sessions')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=['prepare','worker','evaluate','batch']);parser.add_argument('root',type=Path);parser.add_argument('name',nargs='?');args=parser.parse_args();root=args.root.expanduser().resolve()
    if args.command=='prepare':prepare(root)
    elif args.command=='worker':sys.exit(worker(root,args.name))
    elif args.command=='evaluate':
        _manifest=json.loads((root/'manifest.json').read_text());_ensure_task_source_registered(_manifest)
        print(json.dumps(evaluate(_manifest['runs'][args.name]['task'],root/_slug(args.name)/'workspace')))
    else:batch(root)
