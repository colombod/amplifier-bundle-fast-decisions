"""large-repo-v0: everyday requests inside a large repository.

The repository is this bundle's own tree pinned at commit ``PINNED_SHA``
(1,142 tracked files -- well past the shipped scope gate of 300), cloned fresh
per run. Each task has an optional ``setup(ws)`` that prepares the scratch
checkout (inject a bug, dirty the tree, switch branch) and a ``check(ws,
final)`` returning ``(passed, detail)`` from the workspace state and/or the
final assistant message. Checks are automatic and hidden from the agent.

Kinds: ``question`` (read-only answer about the code), ``git`` (status/history
question, read-only), ``edit`` (small change with a hidden check), ``bugfix``
(repository bug to find and fix -- the kind the scope gate exists to protect).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PINNED_SHA = "30bbb7f"


def _env(ws: Path) -> dict:
    # Fresh bytecode cache per check: never trust a .pyc the agent's own runs
    # left behind (same-second, same-size edits would read stale bytecode).
    return dict(os.environ, PYTHONPATH=str(ws / "src"), PYTHONPYCACHEPREFIX=tempfile.mkdtemp())


def _py(ws: Path, code: str, timeout: int = 60) -> subprocess.CompletedProcess:
    env = _env(ws)
    return subprocess.run([sys.executable, "-c", code], cwd=ws, env=env, capture_output=True,
                          text=True, timeout=timeout)


def _git(ws: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=ws, capture_output=True, text=True, check=True).stdout


def _changed_tracked(ws: Path) -> set[str]:
    return {line for line in _git(ws, "diff", "--name-only", PINNED_SHA).splitlines() if line}


def _only_changed(ws: Path, allowed: set[str]) -> tuple[bool, str]:
    changed = _changed_tracked(ws)
    extra = changed - allowed
    return (not extra, f"changed={sorted(changed)}")


def _unittest(ws: Path, pattern: str) -> tuple[bool, str]:
    env = _env(ws)
    proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", pattern],
                          cwd=ws, env=env, capture_output=True, text=True, timeout=300)
    tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or [""]
    return proc.returncode == 0, f"{pattern}: {tail[0]}"


def _replace(ws: Path, rel: str, old: str, new: str) -> None:
    path = ws / rel
    text = path.read_text()
    assert text.count(old) == 1, (rel, old)
    path.write_text(text.replace(old, new))


def _has(final: str | None, *patterns: str) -> tuple[bool, str]:
    text = final or ""
    missing = [p for p in patterns if not re.search(p, text, re.I | re.S)]
    return (not missing, f"missing={missing}" if missing else "all patterns found")


def _read_only(ws: Path, final: str | None, *patterns: str) -> tuple[bool, str]:
    ok, detail = _has(final, *patterns)
    unchanged, changed = _only_changed(ws, set())
    return ok and unchanged, f"{detail}; {changed}"


# --- setups -----------------------------------------------------------------

def _dirty_tree(ws: Path) -> None:
    with open(ws / "README.md", "a") as f:
        f.write("\n<!-- local note -->\n")
    with open(ws / "src/amplifier_fast_decisions/savings.py", "a") as f:
        f.write("\n# TODO: revisit rate table\n")
    (ws / "notes").mkdir(exist_ok=True)
    (ws / "notes/todo.txt").write_text("check the scope gate numbers\n")


def _feature_branch(ws: Path) -> None:
    _git(ws, "checkout", "-q", "-b", "feature/scope-gate-tuning")


def _bug_effective_gate(ws: Path) -> None:
    _replace(ws, "src/amplifier_fast_decisions/contracts.py",
             '.get("escalate_min_probability", 0.7)', '.get("escalate_min_probability", 0.07)')
    _commit(ws, "tune escalation gate")


def _bug_scope_count(ws: Path) -> None:
    _replace(ws, "src/amplifier_fast_decisions/orchestrator.py",
             "if d not in _SCOPE_SKIP_DIRS]", "if d in _SCOPE_SKIP_DIRS]")
    _commit(ws, "simplify workspace walk")


def _bug_rates_prefix(ws: Path) -> None:
    _replace(ws, "src/amplifier_fast_decisions/savings.py",
             "sorted(rates, key=len, reverse=True)", "sorted(rates, key=len)")
    _commit(ws, "savings: tidy rate lookup")


def _commit(ws: Path, message: str) -> None:
    # Bugs are committed so the diff check below is relative to the bugged
    # tree, and `git status` shows the agent a clean start.
    _git(ws, "-c", "user.name=bench", "-c", "user.email=bench@example.invalid",
         "commit", "-qam", message)


# --- checks -----------------------------------------------------------------

def _check_edit_streak(ws, final):
    r = _py(ws, "from amplifier_fast_decisions.contracts import Policy; print(Policy().max_fast_streak)")
    only, changed = _only_changed(ws, {"src/amplifier_fast_decisions/contracts.py"})
    return r.stdout.strip() == "4" and only, f"value={r.stdout.strip() or r.stderr[-200:]}; {changed}"


def _check_pretty_model(ws, final):
    code = ("from amplifier_fast_decisions.savings import pretty_model as p\n"
            "cases = {'claude-sonnet-5-20260101': 'claude-sonnet-5', 'claude-opus-5-5': 'claude-opus-5-5',\n"
            "         'claude-haiku-4-5-20251001': 'claude-haiku-4-5', 'claude-fable-5-1': 'claude-fable-5-1'}\n"
            "bad = {k: p(k) for k, v in cases.items() if p(k) != v}\n"
            "print('OK' if not bad else bad)")
    r = _py(ws, code)
    tests_ok, tests = _unittest(ws, "test_savings.py")
    return r.stdout.strip() == "OK" and tests_ok, f"{r.stdout.strip() or r.stderr[-200:]}; {tests}"


def _since_setup(ws: Path) -> set[str]:
    base = _git(ws, "rev-parse", "HEAD").strip()
    return {line for line in _git(ws, "diff", "--name-only", base).splitlines() if line}


def _check_gate_fix(ws, final):
    r = _py(ws, "from amplifier_fast_decisions.contracts import Policy, effective_gate; "
                "print(effective_gate(Policy(), 'escalation'))")
    changed = _since_setup(ws)
    ok = r.stdout.strip() == "0.7" and changed <= {"src/amplifier_fast_decisions/contracts.py"}
    return ok, f"value={r.stdout.strip() or r.stderr[-200:]}; changed={sorted(changed)}"


def _check_scope_fix(ws, final):
    tests_ok, tests = _unittest(ws, "test_orchestrator_primary.py")
    changed = _since_setup(ws)
    untouched_tests = not any(c.startswith("tests/") for c in changed)
    return tests_ok and untouched_tests, f"{tests}; changed={sorted(changed)}"


def _check_rates_fix(ws, final):
    r = _py(ws, "from amplifier_fast_decisions.savings import _rates_for, DEFAULT_RATES as R; "
                "print(_rates_for('claude-fable-5-1-20260301', R) == R['claude-fable-5-1'] and "
                "_rates_for('claude-fable-5-20260101', R) == R['claude-fable-5'])")
    tests_ok, tests = _unittest(ws, "test_savings.py")
    changed = _since_setup(ws)
    untouched_tests = not any(c.startswith("tests/") for c in changed)
    return (r.stdout.strip() == "True" and tests_ok and untouched_tests,
            f"lookup={r.stdout.strip() or r.stderr[-200:]}; {tests}; changed={sorted(changed)}")


TASKS: dict[str, dict] = {
    # --- questions about the code (read-only) ---
    "q_timeout_default": {
        "kind": "question",
        "prompt": "What is the default value of timeout_ms in the Policy dataclass in "
                  "src/amplifier_fast_decisions/contracts.py? Reply with just the number.",
        "check": lambda ws, final: _read_only(ws, final, r"\b750\b"),
    },
    "q_orchestrator_module": {
        "kind": "question",
        "prompt": "Which module does behaviors/fast-decisions.yaml mount as the session orchestrator? "
                  "Answer with the module name.",
        "check": lambda ws, final: _read_only(ws, final, r"loop-fast-decisions"),
    },
    "q_explain_file_count": {
        "kind": "question",
        "prompt": "Explain in two or three sentences what workspace_file_count in "
                  "src/amplifier_fast_decisions/orchestrator.py does, including which directories it skips.",
        "check": lambda ws, final: _read_only(ws, final, r"node_modules", r"\.git\b", r"(limit|stop|bound)"),
    },
    "q_event_count": {
        "kind": "question",
        "prompt": "How many event names does EVENT_NAMES in src/amplifier_fast_decisions/contracts.py "
                  "contain? Reply with just the number.",
        "check": lambda ws, final: _read_only(ws, final, r"\b27\b"),
    },
    "q_start_tier_function": {
        "kind": "question",
        "prompt": "Which function decides, once per turn, whether a turn starts on the cheaper model or the "
                  "host model? Give the function name and the file it is in.",
        "check": lambda ws, final: _read_only(ws, final, r"decide_start_tier", r"orchestrator\.py"),
    },
    "q_jev_key_env": {
        "kind": "question",
        "prompt": "Which environment variable does the Jev backend read its API key from by default?",
        "check": lambda ws, final: _read_only(ws, final, r"TYPESAFE_API_KEY"),
    },
    # --- git / status (read-only) ---
    "g_uncommitted": {
        "kind": "git",
        "setup": _dirty_tree,
        "prompt": "Which files have I changed or added since the last commit?",
        "check": lambda ws, final: _has(final, r"README\.md", r"savings\.py", r"notes/(todo\.txt)?"),
    },
    "g_last_commit": {
        "kind": "git",
        "prompt": "What is the subject line of the most recent commit?",
        "check": lambda ws, final: _read_only(ws, final, r"say why totals are flat"),
    },
    "g_commit_count": {
        "kind": "git",
        "prompt": "How many commits are on the current branch? Reply with just the number.",
        "check": lambda ws, final: _read_only(ws, final, r"\b195\b"),
    },
    "g_branch": {
        "kind": "git",
        "setup": _feature_branch,
        "prompt": "Which git branch am I on?",
        "check": lambda ws, final: _read_only(ws, final, r"feature/scope-gate-tuning"),
    },
    # --- small edits with a hidden check ---
    "e_streak_default": {
        "kind": "edit",
        "prompt": "In src/amplifier_fast_decisions/contracts.py, change the default max_fast_streak in the "
                  "Policy dataclass from 3 to 4. Don't change anything else.",
        "check": _check_edit_streak,
    },
    "e_pretty_model": {
        "kind": "edit",
        "prompt": "Add a function pretty_model(name: str) -> str to src/amplifier_fast_decisions/savings.py "
                  "that strips a trailing 8-digit date suffix from a model id "
                  "(claude-sonnet-5-20260101 -> claude-sonnet-5) and returns other names unchanged "
                  "(claude-opus-5-5 stays claude-opus-5-5).",
        "check": _check_pretty_model,
    },
    # --- repository bugs (what the scope gate protects) ---
    "b_escalation_gate": {
        "kind": "bugfix",
        "setup": _bug_effective_gate,
        "prompt": "Escalation fires far too eagerly: the effective_gate docstring says the escalation gate "
                  "defaults to 0.7, but that's not what it returns. Fix it.",
        "check": _check_gate_fix,
    },
    "b_scope_count": {
        "kind": "bugfix",
        "setup": _bug_scope_count,
        "prompt": "python3 -m unittest discover -s tests -p 'test_orchestrator_primary.py' (with PYTHONPATH=src) "
                  "started failing in ScopeGateTests. Find and fix the bug in the source; don't change the tests.",
        "check": _check_scope_fix,
    },
    "b_rates_prefix": {
        "kind": "bugfix",
        "setup": _bug_rates_prefix,
        "prompt": "The savings estimate prices dated claude-fable-5-1 ids (like claude-fable-5-1-20260301) at "
                  "the claude-fable-5 rates instead of claude-fable-5-1's. Find and fix the bug.",
        "check": _check_rates_fix,
    },
}
