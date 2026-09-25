"""Continuous-eval workload: realistic multi-step requests in a large repository.

The repository is this bundle's own tree pinned at ``PINNED_SHA`` (1,142 tracked
files, past the shipped scope gate of 300), copied fresh per run -- the same
pinned tree as ``evals/suites/large-repo-v0`` on ``research/routing-levers``,
whose setups and checks are reused here. Each task is shaped like the owner's
real sessions: read/grep/bash chains, a small edit followed by an automatic
test run, questions about code, a helper-agent delegation, a long-running
command, and one multi-turn session (resumed turns, as in the s1m suite).

A task is ``{"kind", "turns": [prompt, ...], "setup": fn(ws) | None,
"check": fn(ws, finals) -> (passed, detail)}``. ``finals`` is the list of
per-turn final assistant messages. Checks are automatic and hidden from the
agent; ``selftest()`` shows every check fails on the untouched (or bugged)
checkout.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PINNED_SHA = "30bbb7f"


# --- helpers (as in large-repo-v0/tasks.py) ---------------------------------

def _env(ws: Path) -> dict:
    return dict(os.environ, PYTHONPATH=str(ws / "src"), PYTHONPYCACHEPREFIX=tempfile.mkdtemp())


def _py(ws: Path, code: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], cwd=ws, env=_env(ws), capture_output=True,
                          text=True, timeout=timeout)


def _git(ws: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=ws, capture_output=True, text=True, check=True).stdout


def _since(ws: Path, base: str) -> set[str]:
    return {line for line in _git(ws, "diff", "--name-only", base).splitlines() if line}


def _unittest(ws: Path, pattern: str) -> tuple[bool, str]:
    proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", pattern],
                          cwd=ws, env=_env(ws), capture_output=True, text=True, timeout=300)
    tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or [""]
    return proc.returncode == 0, f"{pattern}: {tail[0]}"


def _replace(ws: Path, rel: str, old: str, new: str) -> None:
    path = ws / rel
    text = path.read_text()
    assert text.count(old) == 1, (rel, old)
    path.write_text(text.replace(old, new))


def _commit(ws: Path, message: str, *paths: str) -> None:
    if paths:
        _git(ws, "add", *paths)
    _git(ws, "-c", "user.name=bench", "-c", "user.email=bench@example.invalid", "commit", "-qam", message)


def _has(text: str | None, *patterns: str) -> list[str]:
    return [p for p in patterns if not re.search(p, text or "", re.I | re.S)]


def _base(ws: Path) -> str:
    return (ws / ".git" / "ceval-base").read_text().strip()


def _mark_base(ws: Path) -> None:
    (ws / ".git" / "ceval-base").write_text(_git(ws, "rev-parse", "HEAD").strip() + "\n")


# --- setups -----------------------------------------------------------------

def _setup_scope_bug(ws: Path) -> None:
    # A realistic history: an unrelated commit, the regression, another unrelated commit.
    with open(ws / "docs" / "ARCHITECTURE.md", "a") as f:
        f.write("\n<!-- reviewed -->\n")
    _commit(ws, "docs: note architecture review")
    _replace(ws, "src/amplifier_fast_decisions/orchestrator.py",
             "if d not in _SCOPE_SKIP_DIRS]", "if d in _SCOPE_SKIP_DIRS]")
    _commit(ws, "simplify workspace walk")
    with open(ws / "README.md", "a") as f:
        f.write("\n<!-- badge refresh -->\n")
    _commit(ws, "readme: refresh badges")
    _mark_base(ws)


def _setup_rates_bug(ws: Path) -> None:
    _replace(ws, "src/amplifier_fast_decisions/savings.py",
             "sorted(rates, key=len, reverse=True)", "sorted(rates, key=len)")
    _commit(ws, "savings: tidy rate lookup")
    _mark_base(ws)


def _setup_triage(ws: Path) -> None:
    _git(ws, "checkout", "-q", "-b", "feature/scope-gate-tuning")
    with open(ws / "README.md", "a") as f:
        f.write("\n<!-- local note -->\n")
    with open(ws / "src/amplifier_fast_decisions/savings.py", "a") as f:
        f.write("\n# TODO: revisit rate table\n")
    (ws / "notes").mkdir(exist_ok=True)
    (ws / "notes/todo.txt").write_text("check the scope gate numbers\n")
    _mark_base(ws)


NIGHTLY = """#!/bin/sh
# Nightly consistency check. Slow on purpose: it walks every stage in order.
set -e
cd "$(dirname "$0")/.."
i=1
while [ $i -le 11 ]; do
  echo "nightly: stage $i/11 running ..."
  sleep 30
  i=$((i + 1))
done
n=$(git ls-files | wc -l | tr -d ' ')
tok=$(printf 'nightly-%s' "$n" | shasum | cut -c1-10)
echo "NIGHTLY RESULT: status=green files=$n token=$tok"
"""


def _setup_nightly(ws: Path) -> None:
    path = ws / "scripts" / "nightly_check.sh"
    path.write_text(NIGHTLY)
    path.chmod(0o755)
    _commit(ws, "scripts: nightly consistency check", "scripts/nightly_check.sh")
    _mark_base(ws)


# --- checks -----------------------------------------------------------------

def _check_orient(ws, finals):
    finals = list(finals) + [None] * (3 - len(finals))
    miss1 = _has(finals[0], r"loop-fast-decisions", r"\b750\b")
    miss2 = _has(finals[1], r"decide_start_tier", r"orchestrator\.py")
    r = _py(ws, "from amplifier_fast_decisions.contracts import Policy; print(Policy().max_fast_streak)")
    changed = _since(ws, PINNED_SHA)
    ok3 = r.stdout.strip() == "4" and changed <= {"src/amplifier_fast_decisions/contracts.py"}
    ok = not miss1 and not miss2 and ok3
    return ok, (f"t1 missing={miss1}; t2 missing={miss2}; streak={r.stdout.strip() or r.stderr[-160:]}; "
                f"changed={sorted(changed)}")


def _check_scope_hunt(ws, finals):
    tests_ok, tests = _unittest(ws, "test_orchestrator_primary.py")
    changed = _since(ws, _base(ws))
    untouched_tests = not any(c.startswith("tests/") for c in changed)
    miss = _has(finals[-1] if finals else None, r"simplify workspace walk")
    return tests_ok and untouched_tests and not miss, f"{tests}; changed={sorted(changed)}; missing={miss}"


def _check_rates(ws, finals):
    r = _py(ws, "from amplifier_fast_decisions.savings import _rates_for, DEFAULT_RATES as R; "
                "print(_rates_for('claude-fable-5-1-20260301', R) == R['claude-fable-5-1'] and "
                "_rates_for('claude-fable-5-20260101', R) == R['claude-fable-5'])")
    tests_ok, tests = _unittest(ws, "test_savings.py")
    changed = _since(ws, _base(ws))
    return (r.stdout.strip() == "True" and tests_ok,
            f"lookup={r.stdout.strip() or r.stderr[-160:]}; {tests}; changed={sorted(changed)}")


def _check_pretty_suite(ws, finals):
    code = ("from amplifier_fast_decisions.savings import pretty_model as p\n"
            "cases = {'claude-sonnet-5-20260101': 'claude-sonnet-5', 'claude-opus-5-5': 'claude-opus-5-5',\n"
            "         'claude-haiku-4-5-20251001': 'claude-haiku-4-5', 'claude-fable-5-1': 'claude-fable-5-1'}\n"
            "bad = {k: p(k) for k, v in cases.items() if p(k) != v}\n"
            "print('OK' if not bad else bad)")
    r = _py(ws, code)
    tests_ok, tests = _unittest(ws, "test_savings.py")
    added_test = "pretty_model" in (ws / "tests/test_savings.py").read_text()
    # The full suite has 1,069 tests at the pinned commit; the agent adds at least one.
    miss = _has(finals[-1] if finals else None, r"\b10[6-9]\d\b")
    ok = r.stdout.strip() == "OK" and tests_ok and added_test and not miss
    return ok, f"{r.stdout.strip() or r.stderr[-160:]}; {tests}; test_added={added_test}; missing={miss}"


def _check_triage(ws, finals):
    text = "\n".join(f or "" for f in finals)
    miss = _has(text, r"feature/scope-gate-tuning", r"README\.md", r"savings\.py", r"notes/",
                r"say why totals are flat")
    status = _git(ws, "status", "--porcelain")
    savings_clean = "savings.py" not in status
    readme_dirty = "README.md" in status
    notes_kept = (ws / "notes/todo.txt").exists()
    ok = not miss and savings_clean and readme_dirty and notes_kept
    return ok, (f"missing={miss}; savings_clean={savings_clean}; readme_dirty={readme_dirty}; "
                f"notes_kept={notes_kept}")


def _check_nightly(ws, finals):
    import hashlib
    n = len(_git(ws, "ls-files").splitlines())
    tok = hashlib.sha1(f"nightly-{n}".encode()).hexdigest()[:10]
    text = "\n".join(f or "" for f in finals)
    miss = _has(text, re.escape(tok), r"\b36\b")
    changed = _since(ws, _base(ws))
    return not miss and not changed, f"token={tok}; missing={miss}; changed={sorted(changed)}"


TASKS: dict[str, dict] = {
    # Multi-turn session (resumed turns): orientation questions, then an edit with a test run.
    "mt_orient_edit": {
        "kind": "multiturn",
        "turns": [
            "I'm new to this repo. Which module does behaviors/fast-decisions.yaml mount as the session "
            "orchestrator, and what is the default timeout_ms in the Policy dataclass?",
            "Which function decides, once per turn, whether a turn starts on the cheaper model or the host "
            "model? Give the function name and the file it is in.",
            "OK. In the Policy dataclass change the default max_fast_streak from 3 to 4 -- nothing else -- "
            "then run PYTHONPATH=src python3 -m unittest tests.test_decisions to confirm nothing broke.",
        ],
        "check": _check_orient,
    },
    # Bash/git chain: reproduce a failure, bisect-by-reading the history, fix, re-run.
    "bug_scope_hunt": {
        "kind": "bugfix",
        "setup": _setup_scope_bug,
        "turns": [
            "ScopeGateTests in tests/test_orchestrator_primary.py started failing (run it with "
            "PYTHONPATH=src python3 -m unittest tests.test_orchestrator_primary). Find which recent commit "
            "introduced the regression, fix the source without changing any tests, re-run the module, and "
            "tell me the subject line of the commit that caused it."
        ],
        "check": _check_scope_hunt,
    },
    # Helper-agent delegation, then a fix and a test run.
    "delegate_rates_fix": {
        "kind": "delegate",
        "setup": _setup_rates_bug,
        "turns": [
            "Users report dated model ids like claude-fable-5-1-20260301 are priced at the wrong rates. "
            "Delegate the investigation to a helper agent: have it review how "
            "src/amplifier_fast_decisions/savings.py matches model ids to rate-table entries and report the "
            "root cause. Then fix it yourself and run PYTHONPATH=src python3 -m unittest tests.test_savings."
        ],
        "check": _check_rates,
    },
    # Small edit + unit test + a long automatic check (full suite, ~100 s).
    "edit_full_suite": {
        "kind": "edit",
        "turns": [
            "Add a function pretty_model(name: str) -> str to src/amplifier_fast_decisions/savings.py that "
            "strips a trailing 8-digit date suffix from a model id (claude-sonnet-5-20260101 -> "
            "claude-sonnet-5) and returns other names unchanged (claude-opus-5-5 stays claude-opus-5-5). "
            "Add a unit test for it in tests/test_savings.py. Then run the whole test suite "
            "(PYTHONPATH=src python3 -m unittest discover -s tests; it takes about two minutes) and tell me "
            "how many tests ran and whether they all passed."
        ],
        "check": _check_pretty_suite,
    },
    # Git status questions plus a selective revert.
    "git_triage": {
        "kind": "git",
        "setup": _setup_triage,
        "turns": [
            "Quick status check: which branch am I on, which files have I changed or added since the last "
            "commit, and what is the subject of the last commit? Then discard my change to "
            "src/amplifier_fast_decisions/savings.py only -- keep the README and notes changes."
        ],
        "check": _check_triage,
    },
    # Long-running command (~5.5 min): cache keep-alive and wait/poll behavior.
    "long_nightly": {
        "kind": "long_command",
        "setup": _setup_nightly,
        "turns": [
            "Run scripts/nightly_check.sh -- it takes about five and a half minutes, let it finish -- and "
            "give me its NIGHTLY RESULT line. Also tell me how many .py files are under src/."
        ],
        "check": _check_nightly,
    },
}


def selftest(pinned: Path) -> dict:
    """Every check must fail on the untouched (or bugged) workspace with no answer."""
    import shutil
    out = {}
    for name, task in TASKS.items():
        ws = Path(tempfile.mkdtemp(prefix="ceval-selftest-")) / "ws"
        subprocess.run(["git", "clone", "-q", str(pinned), str(ws)], check=True)
        if task.get("setup"):
            task["setup"](ws)
        else:
            _mark_base(ws)
        ok, detail = task["check"](ws, [None] * len(task["turns"]))
        out[name] = {"passed_on_untouched": ok, "detail": detail}
        shutil.rmtree(ws.parent, ignore_errors=True)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(selftest(Path(sys.argv[1])), indent=2))
