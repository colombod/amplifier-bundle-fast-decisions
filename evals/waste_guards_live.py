#!/usr/bin/env python3
"""Live A/B of the waste guards (guards.py) with the real ``amplifier run``.

Each task runs in a fresh disposable workspace with this checkout's
orchestrator (PYTHONPATH=src), plain Amplifier on the default model (no
judge, no routing), once with ``waste_guards`` off and once on, each with its
own events dir. The report compares per task: model calls and
provider-reported cost (from ``fast_decisions:slow_end``), the guards'
efficiency receipts, and task quality (a deterministic check of the final
answer) -- then whether the receipts' sums match the measured OFF-ON cost
difference.

Always test traffic (AFAST_TRAFFIC=test). Uses the configured Anthropic
provider (normal API charges). Keys come from the caller's environment and
are never printed. Workspaces and outputs live under --out (outside any repo).

    set -a; . ~/.amplifier/keys.env; set +a
    python3 evals/waste_guards_live.py run --out ~/.amplifier/fast-decisions/events-eval/wg-$(date +%s) --reps 3
    python3 evals/waste_guards_live.py report --out <same dir>
"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import io
import json
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BUILD = """#!/bin/sh
# Fake build: one progress line every 15 s for 180 s.
i=0
while [ $i -lt 12 ]; do i=$((i+1)); echo "step $i/12 compiling..."; sleep 15; done
echo "BUILD OK: 12 targets"
"""
REPORT = """#!/bin/sh
echo "report: loading data (3 files)"
echo "report: 1204 rows"
echo "error: checksum mismatch in data/segment-7.bin (expected 9f3a, got 1c2e)" >&2
exit 1
"""
STATS = """import hashlib
rows = []
total = 0
for i in range(60):
    v = int(hashlib.sha1(str(i).encode()).hexdigest()[:4], 16) % 997
    total += v
    rows.append(f"| region-{i:02d} | {v:5d} | {hashlib.sha1(str(v).encode()).hexdigest()[:24]} |")
print("| region    | value | checksum                 |")
print("\\n".join(rows))
print(f"TOTAL {total}")
"""


def _stats_total() -> str:
    import hashlib
    return str(sum(int(hashlib.sha1(str(i).encode()).hexdigest()[:4], 16) % 997 for i in range(60)))


TASKS = {
    # Polling a long background build with short checks (the measured real
    # pattern: `sleep 20 && tail log` step after step).
    "monitor": {"files": {"build.sh": BUILD}, "prompt": (
        "Start ./build.sh in the background (nohup, output to build.log); it takes about 3 minutes. Check on it "
        "periodically with short commands (for example `sleep 20 && tail -3 build.log`), and when the build is "
        "done reply with only the last line of build.log."),
        "check": lambda answer: "BUILD OK: 12 targets" in answer},
    # The same failing command retried.
    "flaky": {"files": {"report.sh": REPORT}, "prompt": (
        "Run ./report.sh and reply with its last line of output. It is flaky and often succeeds on a second or "
        "third attempt, so if it fails, run it again (up to 5 attempts in total, one attempt per command, and "
        "look at each result before the next attempt) before giving up. Do not edit report.sh or look for "
        "other scripts."),
        "check": lambda answer: "checksum mismatch" in answer or "segment-7" in answer},
    # The same command re-run to confirm stability (identical output).
    "stable": {"files": {"stats.py": STATS}, "prompt": (
        "Run `python3 stats.py` three separate times to confirm its output is stable: one run per command, and "
        "look at each result before starting the next run. Then reply with one line: 'stable' or 'unstable', "
        "and the TOTAL value it prints."),
        "check": lambda answer: "unstable" not in answer.lower() and "stable" in answer.lower()
        and _stats_total() in answer},
}


_PROFILE_LOCK = __import__("threading").Lock()


def _profile(run_dir: Path, workspace: Path, events: Path, guards_on: bool, name: str) -> Path:
    sys.path.insert(0, str(ROOT / "src"))
    from amplifier_fast_decisions.cli import main as afast
    profile = run_dir / "profile.md"
    with _PROFILE_LOCK, contextlib.redirect_stdout(io.StringIO()):
        code = afast(["configure", "--bundle-root", str(ROOT), "--workspace", str(workspace), "--mode", "active",
                      "--backend", "deterministic", "--events", str(events), "--local-sources",
                      "--output", str(profile)])
    if code:
        raise RuntimeError("afast configure failed")
    data = json.loads(profile.read_text().split("---")[1])
    config = data["session"]["orchestrator"]["config"]
    # Plain Amplifier on the default model: no judge, no routing -- only the guards under test.
    config.pop("effort_routing", None)
    config.pop("model_routing", None)
    config.update(mode="off", read_shortcut=False, waste_guards={"enabled": bool(guards_on)})
    data["bundle"]["name"] = name
    profile.write_text("---\n" + json.dumps(data, indent=2) + "\n---\n")
    return profile


def run_one(out: Path, task: str, arm: str, rep: int, model: str, provider: str, timeout: int) -> dict:
    spec = TASKS[task]
    run_dir = out / f"{task}-{arm}-r{rep}"
    workspace, events = run_dir / "ws", run_dir / "events"
    (workspace / ".amplifier").mkdir(parents=True, exist_ok=False)
    (workspace / ".amplifier" / "settings.local.yaml").write_text("bundle:\n  app: []\n")
    for rel, text in spec["files"].items():
        path = workspace / rel
        path.write_text(text)
        path.chmod(0o755)
    name = f"afast-wg-{task}-{arm}-r{rep}-{int(time.time())}"
    profile = _profile(run_dir, workspace, events, arm == "on", name)
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), AFAST_TRAFFIC="test", AFAST_OBSERVATORY="off",
               AMPLIFIER_MEMORY_CAPTURE="off", AMPLIFIER_NO_BROWSER="1")
    cmd = ["amplifier", "run", "--bundle", profile.as_uri(), "--mode", "single", "--provider", provider,
           "--model", model, "--output-format", "json", spec["prompt"]]
    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=workspace, env=env, capture_output=True, text=True, timeout=timeout)
        code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        code, stdout, stderr = -1, "", "timeout"
    wall = time.time() - started
    answer = _answer(stdout)
    (run_dir / "answer.txt").write_text(answer[-4000:])
    (run_dir / "stderr.txt").write_text(stderr[-8000:])
    subprocess.run(["amplifier", "bundle", "remove", name], capture_output=True, text=True, timeout=120)
    result = {"task": task, "arm": arm, "rep": rep, "exit_code": code, "wall_s": round(wall, 1),
              "quality_pass": bool(spec["check"](answer))}
    (run_dir / "run.json").write_text(json.dumps(result, indent=2))
    return result


def _answer(stdout: str) -> str:
    for line in reversed(stdout.strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            for key in ("response", "result", "output", "content", "text"):
                if isinstance(data.get(key), str):
                    return data[key]
    try:
        data = json.loads(stdout)
        if isinstance(data, dict):
            for key in ("response", "result", "output", "content", "text"):
                if isinstance(data.get(key), str):
                    return data[key]
    except ValueError:
        pass
    return stdout


def _events(run_dir: Path) -> list[dict]:
    out = []
    for path in sorted((run_dir / "events").glob("*.jsonl")):
        for line in path.read_text(errors="replace").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    out.sort(key=lambda e: (e.get("monotonic_ns") or 0))
    return out


def analyze_run(run_dir: Path) -> dict:
    meta = json.loads((run_dir / "run.json").read_text())
    calls, receipts, guards = [], [], []
    for e in _events(run_dir):
        d, kind = e.get("data") or {}, e["event"]
        if kind == "fast_decisions:slow_end" and d.get("status") == "ok":
            calls.append({k: d.get(k) for k in ("cost_usd", "cache_read_tokens", "cache_write_tokens",
                                                "output_tokens", "duration_ms")})
        elif kind == "fast_decisions:efficiency":
            receipts.append(d)
        elif kind == "fast_decisions:waste_guard":
            guards.append(f"{d.get('action')}:{d.get('reason_code')}")
    cost = sum(c["cost_usd"] or 0 for c in calls)
    return {**meta, "model_calls": len(calls), "cost_usd": round(cost, 6), "guard_actions": guards,
            "receipts": receipts,
            "receipt_sums": {"calls_saved": sum(r.get("calls_saved") or 0 for r in receipts),
                             "usd_saved": round(sum(r.get("usd_saved") or 0 for r in receipts), 6)}}


def report(out: Path) -> dict:
    runs = [analyze_run(p) for p in sorted(out.iterdir()) if (p / "run.json").exists()]
    by_task: dict[str, dict] = {}
    for r in runs:
        by_task.setdefault(r["task"], {}).setdefault(r["arm"], []).append(r)
    comparison = {}
    tot = {"measured": 0.0, "receipts": 0.0}
    for task, arms in sorted(by_task.items()):
        off, on = arms.get("off", []), arms.get("on", [])
        if not off or not on:
            continue
        mean = lambda rs, k: statistics.fmean(r[k] for r in rs)
        sd = lambda rs, k: statistics.stdev([r[k] for r in rs]) if len(rs) > 1 else 0.0
        measured = mean(off, "cost_usd") - mean(on, "cost_usd")
        claimed = statistics.fmean(r["receipt_sums"]["usd_saved"] for r in on)
        tot["measured"] += measured
        tot["receipts"] += claimed
        comparison[task] = {
            "n_off": len(off), "n_on": len(on),
            "cost_off_mean": round(mean(off, "cost_usd"), 4), "cost_off_sd": round(sd(off, "cost_usd"), 4),
            "cost_on_mean": round(mean(on, "cost_usd"), 4), "cost_on_sd": round(sd(on, "cost_usd"), 4),
            "calls_off_mean": round(mean(off, "model_calls"), 2), "calls_on_mean": round(mean(on, "model_calls"), 2),
            "measured_cost_diff_usd(off-on)": round(measured, 4),
            "measured_call_diff(off-on)": round(mean(off, "model_calls") - mean(on, "model_calls"), 2),
            "receipts_usd_saved_mean(on)": round(claimed, 4),
            "receipts_calls_saved_mean(on)": round(statistics.fmean(r["receipt_sums"]["calls_saved"] for r in on), 2),
            "wall_off_mean_s": round(mean(off, "wall_s"), 1), "wall_on_mean_s": round(mean(on, "wall_s"), 1),
            "quality_off": f"{sum(r['quality_pass'] for r in off)}/{len(off)}",
            "quality_on": f"{sum(r['quality_pass'] for r in on)}/{len(on)}",
        }
    summary = {"runs": [{k: v for k, v in r.items() if k != "receipts"} | {
        "receipts": [{k: x.get(k) for k in ("lever", "mechanism", "decision", "calls_saved", "usd_saved", "detail")}
                     for x in r["receipts"]]} for r in runs],
               "comparison": comparison,
               "totals_per_run_set": {k: round(v, 4) for k, v in tot.items()}}
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def _load_env(path: Path) -> None:
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--out", required=True)
    run.add_argument("--tasks", default="monitor,flaky,stable")
    run.add_argument("--arms", default="off,on")
    run.add_argument("--reps", type=int, default=3)
    run.add_argument("--rep-start", type=int, default=1)
    run.add_argument("--model", default="claude-opus-5-5")
    run.add_argument("--provider", default="anthropic")
    run.add_argument("--timeout", type=int, default=1500)
    run.add_argument("--parallel", type=int, default=6)
    run.add_argument("--keys-env", default=None,
                     help="KEY=VALUE file to load into the environment (values are never printed)")
    rep = sub.add_parser("report")
    rep.add_argument("--out", required=True)
    args = parser.parse_args()
    out = Path(args.out).expanduser().resolve()
    if args.cmd == "run":
        if os.environ.get("AFAST_TRAFFIC", "test") != "test":
            raise SystemExit("AFAST_TRAFFIC must be test for this script")
        if args.keys_env:
            _load_env(Path(args.keys_env).expanduser())
        out.mkdir(parents=True, exist_ok=True)
        # Alternate arm order across reps so neither arm always starts first.
        jobs = []
        for r in range(args.rep_start, args.rep_start + args.reps):
            arms = args.arms.split(",")
            if r % 2 == 0:
                arms = arms[::-1]
            jobs += [(t, a, r) for t in args.tasks.split(",") for a in arms]
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as pool:
            futures = [pool.submit(run_one, out, t, a, r, args.model, args.provider, args.timeout) for t, a, r in jobs]
            for f in concurrent.futures.as_completed(futures):
                try:
                    print(json.dumps(f.result()), flush=True, file=sys.__stdout__)
                except Exception as exc:  # noqa: BLE001
                    print(json.dumps({"error": type(exc).__name__, "detail": str(exc)[:200]}), flush=True,
                          file=sys.__stdout__)
    print(json.dumps(report(out)["comparison"], indent=2))


if __name__ == "__main__":
    main()
