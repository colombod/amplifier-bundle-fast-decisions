"""Offline self-test of large-repo-v0: every check fails on the untouched (or
bugged) checkout and passes with a reference answer / fix. No model calls.

    python3 evals/suites/large-repo-v0/selftest.py /path/to/pinned-clone
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tasks as T  # noqa: E402

ANSWERS = {
    "q_timeout_default": "750",
    "q_orchestrator_module": "loop-fast-decisions",
    "q_explain_file_count": "Counts files up to limit, stopping early; skips .git and node_modules.",
    "q_event_count": "27",
    "q_start_tier_function": "decide_start_tier in orchestrator.py",
    "q_jev_key_env": "TYPESAFE_API_KEY",
    "g_uncommitted": "README.md, src/amplifier_fast_decisions/savings.py, notes/todo.txt",
    "g_last_commit": "savings: say why totals are flat (last faster-model request, turns since by reason)",
    "g_commit_count": "195",
    "g_branch": "feature/scope-gate-tuning",
}
FIXES = {
    "e_streak_default": lambda ws: T._replace(ws, "src/amplifier_fast_decisions/contracts.py",
                                              "max_fast_streak: int = 3", "max_fast_streak: int = 4"),
    "e_pretty_model": lambda ws: open(ws / "src/amplifier_fast_decisions/savings.py", "a").write(
        "\n\ndef pretty_model(name: str) -> str:\n    import re\n    return re.sub(r'-\\d{8}$', '', name)\n"),
    "b_escalation_gate": lambda ws: T._replace(ws, "src/amplifier_fast_decisions/contracts.py", "0.07)", "0.7)"),
    "b_scope_count": lambda ws: T._replace(ws, "src/amplifier_fast_decisions/orchestrator.py",
                                           "if d in _SCOPE_SKIP_DIRS]", "if d not in _SCOPE_SKIP_DIRS]"),
    "b_rates_prefix": lambda ws: T._replace(ws, "src/amplifier_fast_decisions/savings.py",
                                            "sorted(rates, key=len)", "sorted(rates, key=len, reverse=True)"),
}


def main(pinned: str) -> int:
    bad = 0
    for name, task in T.TASKS.items():
        tmp = Path(tempfile.mkdtemp())
        ws = tmp / "ws"
        subprocess.run(["git", "clone", "-q", pinned, str(ws)], check=True)
        if task.get("setup"):
            task["setup"](ws)
        before, _ = task["check"](ws, None)
        if name in FIXES:
            FIXES[name](ws)
            after, detail = task["check"](ws, None)
        else:
            after, detail = task["check"](ws, ANSWERS[name])
        ok = (not before) and after
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} {name:24s} before={before} after={after} {detail[:110]}")
        shutil.rmtree(tmp)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
