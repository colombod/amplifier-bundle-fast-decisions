"""Continuous evaluation harness: rounds of realistic multi-step Amplifier work, in parallel via Forge.

    python3 evals/continuous/harness.py --out /tmp/ampup/continuous-eval \
        --min-rounds 3 --max-rounds 8 --budget 60 --concurrency 3

Each round:
1. discovers configs -- ``plain`` (fast-decisions off) and ``current`` (the
   bundle as shipped on origin/main), plus every candidate branch in
   ``CANDIDATES`` whose tip has commits beyond origin/main (polled every round;
   a new tip gets a fresh frozen source tree);
2. freezes each config's source with ``git archive <sha>`` into
   ``<out>/sources/<sha>`` (runs use ``PYTHONPATH=<source>/src``, so installs
   never collide);
3. schedules every task on every config, shuffling config order per task and
   per round (seeded, recorded in ``<out>/runs/r<N>/schedule.json``);
4. launches each run as its own Forge terminal (``create_terminal`` running
   ``worker.py`` under ``zsh -lc`` with ``~/.amplifier/keys.env`` sourced --
   values never printed), at most ``--concurrency`` at a time, and waits for
   the worker's ``result.json``;
5. rewrites ``<out>/scoreboard.md`` / ``.json`` (see scoreboard.py).

Stops after ``--max-rounds`` or when the next round's projected spend would
exceed ``--budget`` (after at least ``--min-rounds``). ``touch <out>/STOP``
ends the loop after the current round. All runs: AFAST_TRAFFIC=test and events
in ``--events-dir`` (never the production events dir).
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import random
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
import scoreboard  # noqa: E402
import tasks as T  # noqa: E402

FORGE_PY = Path.home() / ".claude/skills/amplifier-skill-forge/tools/forge.py"
CANDIDATES = ["feat/jev-step-actions", "feat/keepalive-loopstop"]
# Per-config decision overrides (deep-merged by the kernel onto the frozen
# source's behaviors/fast-decisions.yaml). Empty = the branch's shipped default.
OVERRIDES: dict[str, dict] = {}
LOG_LOCK = threading.Lock()
LAUNCH_LOCK = threading.Lock()


def log(out: Path, msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    with LOG_LOCK:
        print(line, flush=True)
        with open(out / "harness.log", "a") as f:
            f.write(line + "\n")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()


def rev(ref: str) -> str | None:
    try:
        return git("rev-parse", "--verify", "-q", ref + "^{commit}")
    except subprocess.CalledProcessError:
        return None


def freeze(out: Path, sha: str) -> Path:
    dest = out / "sources" / sha
    if (dest / "SOURCE_SHA").exists():
        return dest
    tmp = out / "sources" / (sha + ".tmp")
    subprocess.run(["rm", "-rf", str(tmp)], check=True)
    tmp.mkdir(parents=True)
    archive = subprocess.run(["git", "archive", sha], cwd=REPO, capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(tmp)], input=archive, check=True)
    (tmp / "SOURCE_SHA").write_text(sha + "\n")
    tmp.rename(dest)
    return dest


def discover(out: Path) -> dict[str, dict]:
    try:
        subprocess.run(["git", "fetch", "-q", "origin"], cwd=REPO, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        pass
    main = rev("origin/main")
    configs = {"plain": {"sha": main, "mode": "off"}, "current": {"sha": main, "mode": "composed"}}
    for branch in CANDIDATES:
        # Sibling worktrees commit to the shared local refs; a pushed branch shows on origin.
        shas = [s for s in (rev(branch), rev("origin/" + branch)) if s]
        for sha in shas:
            ahead = int(git("rev-list", "--count", f"{main}..{sha}") or 0)
            if ahead > 0:
                configs[branch.split("/", 1)[1]] = {"sha": sha, "mode": "composed", "branch": branch,
                                                    "ahead": ahead}
                break
    # <out>/overrides.json ({config: {...}}) is re-read every round, so a
    # candidate's lever can be switched on without restarting the loop.
    extra = {}
    if (out / "overrides.json").exists():
        try:
            extra = json.loads((out / "overrides.json").read_text())
        except ValueError:
            extra = {}
    for name, c in configs.items():
        c["source_root"] = str(freeze(out, c["sha"]))
        c["overrides"] = {**OVERRIDES.get(name, {}), **extra.get(name, {})}
    return configs


def schedule(rnd: int, task_names: list[str], configs: list[str], seed: int) -> list[dict]:
    rng = random.Random(seed * 1000 + rnd)
    order_tasks = list(task_names)
    rng.shuffle(order_tasks)
    orders = {}
    for t in order_tasks:
        cs = list(configs)
        rng.shuffle(cs)
        orders[t] = cs
    # Interleave so runs in flight together are different tasks (no shared
    # /tmp paths or same-command contention between configs of one task).
    return [{"round": rnd, "task": t, "config": orders[t][j]} for j in range(len(configs)) for t in order_tasks]


def forge_call(tool: str, args: dict):
    sys.path.insert(0, str(FORGE_PY.parent))
    import forge  # noqa: PLC0415
    return forge.call(tool, args)


def run_item(out: Path, item: dict, cfg: dict, args) -> dict | None:
    run_dir = out / "runs" / f"r{item['round']}" / item["task"] / item["config"]
    if (run_dir / "result.json").exists():
        try:
            r = json.loads((run_dir / "result.json").read_text())
            if not r.get("error"):
                return r
        except ValueError:
            pass
    run_dir.mkdir(parents=True, exist_ok=True)
    turns = len(T.TASKS[item["task"]]["turns"])
    spec = {**item, "source_sha": cfg["sha"], "source_root": cfg["source_root"], "mode": cfg["mode"],
            "overrides": cfg.get("overrides") or {}, "model": args.model, "events_dir": args.events_dir,
            "pinned": args.pinned, "deadline": args.deadline, "max_iterations": args.max_iterations}
    (run_dir / "spec.json").write_text(json.dumps(spec, indent=2))
    (run_dir / "result.json").unlink(missing_ok=True)
    (run_dir / "worker.pid").unlink(missing_ok=True)
    # The worker detaches into its own session (survives Forge daemon restarts);
    # the Forge terminal shows its log live and exits when the result lands.
    rd = shlex.quote(str(run_dir))
    cmd = ("set -a; . ~/.amplifier/keys.env 2>/dev/null; set +a; "
           + shlex.join(["python3", str(HERE / "worker.py"), "--detach", str(run_dir)])
           + f"; tail -F {rd}/worker.log {rd}/turn1-stderr.txt & TP=$!; "
           + f"while [ ! -f {rd}/result.json ]; do sleep 5; done; kill $TP")
    term = None
    for attempt in range(4):
        try:
            with LAUNCH_LOCK:
                term = forge_call("create_terminal", {
                    "name": f"ceval r{item['round']} {item['task']} {item['config']}", "cwd": str(run_dir),
                    "command": "/bin/zsh", "args": ["-lc", cmd], "tags": ["afast-ceval", f"ceval-r{item['round']}"],
                    "cols": 160, "rows": 40})
                time.sleep(2)
            break
        except SystemExit as exc:
            log(out, f"forge launch failed ({exc}); doctor + retry {attempt + 1}")
            subprocess.run([sys.executable, str(FORGE_PY), "doctor"], capture_output=True, timeout=120)
            time.sleep(5 * (attempt + 1))
    if term is None:
        log(out, f"GIVING UP launch r{item['round']} {item['task']} {item['config']}")
        return None
    tid = term.get("id") if isinstance(term, dict) else None
    (run_dir / "forge.json").write_text(json.dumps({"terminal": tid, "launched_at": time.time()}))
    log(out, f"launched r{item['round']} {item['task']:20s} {item['config']:18s} forge={tid}")
    launched = time.time()
    deadline = launched + turns * (args.deadline + 60) + 600
    result, dead_since = None, None
    while time.time() < deadline:
        if (run_dir / "result.json").exists():
            try:
                result = json.loads((run_dir / "result.json").read_text())
                break
            except ValueError:
                pass
        alive = None
        try:
            pid = int((run_dir / "worker.pid").read_text())
            os.kill(pid, 0)
            alive = True
        except (OSError, ValueError):
            alive = False if (run_dir / "worker.pid").exists() or time.time() - launched > 120 else None
        if alive is False:
            dead_since = dead_since or time.time()
            if time.time() - dead_since > 20 and not (run_dir / "result.json").exists():
                break
        else:
            dead_since = None
        time.sleep(5)
    if tid:
        try:
            forge_call("close_terminal", {"id": tid})
        except SystemExit:
            pass
    if result is None:
        log(out, f"NO RESULT r{item['round']} {item['task']} {item['config']} (timed out waiting)")
        return None
    log(out, f"done r{item['round']} {item['task']:20s} {item['config']:18s} pass={result.get('passed')} "
             f"wall={result.get('wall_time_s')}s cost=${result.get('cost_usd')} full={result.get('full_model_calls')}/"
             f"{result.get('model_calls')} served={result.get('served_models')} "
             f"receipts={(result.get('receipts') or {}).get('by_lever')}")
    return result


def run_item_retry(out: Path, item: dict, cfg: dict, args) -> dict | None:
    for attempt in (1, 2):
        r = run_item(out, item, cfg, args)
        if r is not None and not r.get("error"):
            return r
        log(out, f"retrying r{item['round']} {item['task']} {item['config']} (attempt {attempt} gave no valid result)")
    return r


def spent(out: Path) -> float:
    total = 0.0
    for p in out.glob("runs/r*/*/*/result.json"):
        try:
            total += json.loads(p.read_text()).get("cost_usd") or 0
        except ValueError:
            pass
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/ampup/continuous-eval")
    ap.add_argument("--pinned", default="/tmp/ampup/continuous-eval/pinned")
    ap.add_argument("--events-dir", default=str(Path.home() / ".amplifier/fast-decisions/events-eval"))
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--tasks", default="all")
    ap.add_argument("--configs", default="", help="restrict to these config names (comma list)")
    ap.add_argument("--min-rounds", type=int, default=3)
    ap.add_argument("--max-rounds", type=int, default=6)
    ap.add_argument("--first-round", type=int, default=1)
    ap.add_argument("--budget", type=float, default=60.0)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--deadline", type=int, default=900, help="seconds per turn")
    ap.add_argument("--max-iterations", type=int, default=40)
    ap.add_argument("--seed", type=int, default=20260925)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    Path(args.events_dir).mkdir(parents=True, exist_ok=True)
    task_names = list(T.TASKS) if args.tasks == "all" else [t.strip() for t in args.tasks.split(",")]
    rnd = args.first_round
    while rnd < args.first_round + args.max_rounds:
        if (out / "STOP").exists():
            log(out, "STOP file present; ending")
            break
        configs = discover(out)
        if args.configs:
            keep = {c.strip() for c in args.configs.split(",")}
            configs = {k: v for k, v in configs.items() if k in keep}
        used = spent(out)
        done_rounds = rnd - args.first_round
        per_run = used / max(1, sum(1 for _ in out.glob("runs/r*/*/*/result.json"))) if used else 1.0
        projected = per_run * len(configs) * len(task_names)
        if done_rounds >= args.min_rounds and used + projected > args.budget:
            log(out, f"budget stop: spent ${used:.2f}, next round projected ${projected:.2f} > ${args.budget}")
            break
        items = schedule(rnd, task_names, list(configs), args.seed)
        rdir = out / "runs" / f"r{rnd}"
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "schedule.json").write_text(json.dumps({"round": rnd, "configs": configs, "items": items,
                                                        "model": args.model, "created": datetime.now(
                                                            timezone.utc).isoformat()}, indent=2))
        log(out, f"=== round {rnd}: configs {', '.join(f'{k}@{v['sha'][:8]}' for k, v in configs.items())}; "
                 f"{len(items)} runs; spent so far ${used:.2f}; projected ${projected:.2f}")
        if args.dry_run:
            for i in items:
                print(i)
            return
        with cf.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futs = [pool.submit(run_item_retry, out, i, configs[i["config"]], args) for i in items]
            for f in cf.as_completed(futs):
                try:
                    f.result()
                except Exception as exc:  # noqa: BLE001
                    log(out, f"run crashed: {type(exc).__name__}: {exc}")
        sb = scoreboard.write(out)
        log(out, f"=== round {rnd} complete; spend ${sb['total_cost_usd']}; scoreboard at {out / 'scoreboard.md'}")
        for cfg, e in sb["board"].items():
            if cfg != "plain":
                log(out, f"    {cfg}: pass {e['passed']}/{e['runs']} ratios {e['ratio_vs_plain']} "
                         f"receipts {e['receipts_total']}")
        rnd += 1
    subprocess.run(["amplifier", "bundle", "remove", "afast-continuous-eval"], capture_output=True, text=True)


if __name__ == "__main__":
    main()
