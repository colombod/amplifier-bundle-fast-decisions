"""Positive selftest for the heavy tasks: with a reference fix and the scripts'
real output, every check passes (complements tasks.selftest, which shows each
check fails on the untouched checkout).

    python3 evals/continuous/selftest_positive.py /tmp/ampup/continuous-eval/pinned
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tasks as T  # noqa: E402


def fresh(pinned: Path, name: str) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="ceval-pos-")) / "ws"
    subprocess.run(["git", "clone", "-q", str(pinned), str(ws)], check=True)
    t = T.TASKS[name]
    (t.get("setup") or T._mark_base)(ws)
    return ws


def fix_rates(ws):
    T._replace(ws, "src/amplifier_fast_decisions/savings.py", "sorted(rates, key=len)",
               "sorted(rates, key=len, reverse=True)")


def main(pinned: Path) -> int:
    out = {}
    # heavy_multi_bug: revert all three regressions.
    ws = fresh(pinned, "heavy_multi_bug")
    T._replace(ws, "src/amplifier_fast_decisions/contracts.py", '.get("escalate_min_probability", 0.07)',
               '.get("escalate_min_probability", 0.7)')
    T._replace(ws, "src/amplifier_fast_decisions/orchestrator.py", "if d in _SCOPE_SKIP_DIRS]",
               "if d not in _SCOPE_SKIP_DIRS]")
    fix_rates(ws)
    out["heavy_multi_bug"] = T.TASKS["heavy_multi_bug"]["check"](
        ws, ["simplify workspace walk; tune escalation gate; savings: tidy rate lookup"])
    shutil.rmtree(ws.parent, ignore_errors=True)
    # heavy_event_map: a map naming every event and orchestrator.py.
    ws = fresh(pinned, "heavy_event_map")
    names = T._event_names(ws)
    (ws / "docs/EVENT-MAP.md").write_text("| event | file |\n|---|---|\n" + "".join(
        f"| {n} | src/amplifier_fast_decisions/orchestrator.py |\n" for n in names))
    out["heavy_event_map"] = T.TASKS["heavy_event_map"]["check"](ws, ["done"])
    shutil.rmtree(ws.parent, ignore_errors=True)
    # heavy_nightly_fix: quick run red before the fix, green after.
    ws = fresh(pinned, "heavy_nightly_fix")
    # Full run with the stage sleeps shortened (selftest only; the agent gets the real 5.5-minute script).
    script = (ws / "scripts/nightly_check.sh").read_text()
    (ws / "scripts/nightly_check.sh").write_text(script.replace("sleep 30", "sleep 0"))
    red = subprocess.run(["scripts/nightly_check.sh"], cwd=ws, capture_output=True, text=True).stdout.splitlines()[-1]
    (ws / "scripts/nightly_check.sh").write_text(script)
    fix_rates(ws)
    green = subprocess.run(["scripts/nightly_check.sh", "--quick"], cwd=ws, capture_output=True, text=True).stdout
    out["heavy_nightly_fix"] = (T.TASKS["heavy_nightly_fix"]["check"](ws, [red + "\n" + green]), red.strip(),
                                green.strip())
    shutil.rmtree(ws.parent, ignore_errors=True)
    # heavy_publish_poll: fast-forward the indexer, then retry the lock until it opens.
    ws = fresh(pinned, "heavy_publish_poll")
    (ws / "build").mkdir(exist_ok=True)
    (ws / "build/index.done").write_text("indexed\n")
    outs = [subprocess.run(["scripts/publish_check.sh"], cwd=ws, capture_output=True, text=True) for _ in range(3)]
    out["heavy_publish_poll"] = (T.TASKS["heavy_publish_poll"]["check"](ws, [outs[-1].stdout]),
                                 [o.returncode for o in outs], outs[-1].stdout.strip())
    shutil.rmtree(ws.parent, ignore_errors=True)
    for k, v in out.items():
        print(k, v)
    ok = all((v[0][0] if isinstance(v[0], tuple) else v[0]) for v in out.values())
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
