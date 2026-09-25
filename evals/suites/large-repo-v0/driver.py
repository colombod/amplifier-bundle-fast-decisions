"""Standalone screen driver for large-repo-v0 (does not touch evals/run.py).

For each (rep, task, config) it clones the pinned repository into a scratch
workspace, applies the task's setup, writes a bundle profile with the same
builders the battery uses (scripts/forge_e2e.py: ``_side_profile`` for plain,
``_composed_profile`` for fast-decisions configs), runs

    amplifier run --bundle <profile> --mode single --provider anthropic
                  --model <host> --output-format json <prompt>

and records wall time, exec time (first llm:request -> last llm:response in
the native session events), provider-reported cost, the model that served
every request (native llm:response ``model``), the router's receipts
(difficulty_judged / model_routed / slow_end served_model) and the task's
automatic pass/fail. The workspace's ``.amplifier/settings.local.yaml``
disables the user's app bundles (as the battery does); global settings are
never modified.

Run order: within a rep, each task's configs are rotated by the task index
(and reversed on odd reps), so no config always runs first or last.

    PYTHONPATH=src python3 evals/suites/large-repo-v0/driver.py \\
        --pinned /tmp/ampup/routing-levers/pinned --out /tmp/ampup/routing-levers/runs \\
        --configs plain,default,L2-both --reps 1 --concurrency 3
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "scripts"))
import tasks as T  # noqa: E402
from configs import OVERRIDES  # noqa: E402
from keys import load_keys  # noqa: E402
import forge_e2e  # noqa: E402

LOCK = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat()


def schedule(task_names, configs, reps):
    out = []
    for rep in range(1, reps + 1):
        for i, task in enumerate(task_names):
            k = (i + rep - 1) % len(configs)
            order = configs[k:] + configs[:k]
            if rep % 2 == 0:
                order = list(reversed(order))
            out.extend({"rep": rep, "task": task, "config": c} for c in order)
    return out


def build_workspace(run_dir: Path, pinned: Path, task: dict) -> Path:
    ws = run_dir / "workspace"
    subprocess.run(["git", "clone", "-q", str(pinned), str(ws)], check=True)
    subprocess.run(["git", "remote", "remove", "origin"], cwd=ws, check=True)
    (ws / ".amplifier").mkdir(exist_ok=True)
    (ws / ".amplifier/settings.local.yaml").write_text("bundle:\n  app: []\n")
    with open(ws / ".git/info/exclude", "a") as f:
        f.write("\n.amplifier/\n")
    if task.get("setup"):
        task["setup"](ws)
    return ws


def build_profile(run_dir: Path, ws: Path, config: str, source: Path) -> dict:
    overrides = OVERRIDES[config]
    fe_config = {
        "limits": {"timeout_seconds": 600, "max_iterations": 30, "extended_thinking": True},
        "events_dir": str(run_dir / "events"), "amplifier_bundle": "foundation",
        "upstream_loop_source": forge_e2e.UPSTREAM_LOOP_SOURCE,
    }
    if overrides is None:
        side = {"source_root": str(source), "mode": "off"}
    else:
        side = {"source_root": str(source), "mode": "active", "composition": "composed",
                "decision_overrides": overrides}
    profile = forge_e2e._side_profile(config, side, "large-repo-v0", ws, fe_config)
    profile["bundle"]["name"] = "afast-levers-screen"
    (run_dir / "profile.md").write_text("---\n" + json.dumps(profile, indent=2) + "\n---\n")
    return profile


def native_metrics(session_dir: Path | None) -> dict:
    out = {"session_dir": str(session_dir) if session_dir else None, "requests": 0, "served_models": [],
           "cost_usd": None, "exec_time_ms": None, "final": None, "input_tokens": 0, "output_tokens": 0,
           "cache_read_tokens": 0, "cache_write_tokens": 0, "request_efforts": [], "execution_completed": False}
    if not session_dir or not (session_dir / "events.jsonl").exists():
        return out
    first_req = last_resp = None
    cost, cost_known = 0.0, True
    for line in (session_dir / "events.jsonl").read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        name, d = e.get("event"), e.get("data") or {}
        ts = forge_e2e_ts(e.get("ts"))
        if name == "llm:request":
            out["requests"] += 1
            first_req = first_req or ts
            out["request_efforts"].append([d.get("model"), d.get("thinking_enabled"), d.get("thinking_budget")])
        elif name == "llm:response":
            last_resp = ts or last_resp
            out["served_models"].append(d.get("model"))
            usage = d.get("usage") or {}
            for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
                try:
                    out[k] += int(usage.get(k) or 0)
                except (TypeError, ValueError):
                    pass
            try:
                cost += float(usage["cost_usd"])
            except (KeyError, TypeError, ValueError):
                cost_known = False
            raw = d.get("raw") or {}
            content = raw.get("content") if isinstance(raw, dict) else None
            if isinstance(content, list):
                texts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"
                         and b.get("text")]
                if texts:
                    out["final"] = "\n".join(texts)
        elif name == "execution:end":
            out["execution_completed"] = True
    out["cost_usd"] = round(cost, 6) if cost_known and out["requests"] else None
    if first_req and last_resp:
        out["exec_time_ms"] = (last_resp - first_req).total_seconds() * 1000
    return out


def forge_e2e_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def fd_receipts(events_dir: Path) -> dict:
    out = {"difficulty_judged": [], "model_routed": [], "slow_end_served": [], "jev_ms": []}
    if not events_dir.is_dir():
        return out
    for path in sorted(events_dir.glob("**/*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            name, d = e.get("event", ""), e.get("data") or {}
            if name.endswith("difficulty_judged"):
                out["difficulty_judged"].append({k: d.get(k) for k in (
                    "reason_code", "choice", "probabilities", "tier", "requested_model", "requested_effort",
                    "duration_ms", "candidate_count", "profile")})
                if d.get("duration_ms"):
                    out["jev_ms"].append(d["duration_ms"])
            elif name.endswith("model_routed"):
                out["model_routed"].append({k: d.get(k) for k in ("reason_code", "requested_model",
                                                                  "requested_effort")})
            elif name.endswith("slow_end") and d.get("status") == "ok":
                out["slow_end_served"].append([d.get("served_model"), d.get("served_model_source")])
    return out


def run_one(item, args, source: Path, pinned: Path):
    task = T.TASKS[item["task"]]
    run_dir = Path(args.out) / f"r{item['rep']}" / item["task"] / item["config"]
    if (run_dir / "result.json").exists():
        return json.loads((run_dir / "result.json").read_text())
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    ws = build_workspace(run_dir, pinned, task)
    build_profile(run_dir, ws, item["config"], source)
    slug = str(ws.resolve()).replace("/", "-").replace("\\", "-").replace(":", "")
    sessions = Path.home() / ".amplifier/projects" / slug / "sessions"
    env = dict(os.environ, AFAST_OBSERVATORY="off", AMPLIFIER_NO_BROWSER="1", PYTHONPATH=str(source / "src"))
    cmd = ["amplifier", "run", "--bundle", (run_dir / "profile.md").as_uri(), "--mode", "single",
           "--provider", "anthropic", "--model", args.model, "--output-format", "json", task["prompt"]]
    started_at = _now()
    t0 = time.perf_counter()
    timed_out = False
    with open(run_dir / "stdout.txt", "w") as so, open(run_dir / "stderr.txt", "w") as se:
        proc = subprocess.Popen(cmd, cwd=ws, env=env, stdout=so, stderr=se)
        try:
            code = proc.wait(timeout=args.deadline)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.terminate()
            try:
                code = proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = proc.wait()
    wall_ms = (time.perf_counter() - t0) * 1000
    found = sorted(sessions.iterdir(), key=lambda p: p.stat().st_mtime) if sessions.exists() else []
    found = [p for p in found if (p / "events.jsonl").exists()]
    native = native_metrics(found[-1] if found else None)
    final = native.pop("final")
    if final is None:
        try:
            final = json.loads((run_dir / "stdout.txt").read_text()).get("response")
        except (ValueError, OSError, AttributeError):
            final = None
    try:
        passed, detail = task["check"](ws, final)
    except Exception as exc:  # noqa: BLE001
        passed, detail = False, f"check_error: {type(exc).__name__}: {exc}"
    result = {
        **item, "kind": task["kind"], "started_at": started_at, "wall_time_ms": wall_ms, "exit_code": code,
        "timed_out": timed_out, "passed": bool(passed) and code == 0 and not timed_out, "check_detail": detail,
        "infrastructure_failure": not found or native["requests"] == 0,
        **native, "fd": fd_receipts(run_dir / "events"), "final_message": (final or "")[:2000],
        "sessions_found": len(found),
    }
    (run_dir / "result.json").write_text(json.dumps(result, indent=2))
    with LOCK:
        with open(Path(args.out) / "results.jsonl", "a") as f:
            f.write(json.dumps(result) + "\n")
    # Keep the run small on disk: the workspace is reproducible from pinned + setup.
    if not args.keep_workspaces:
        shutil.rmtree(ws, ignore_errors=True)
    mix = {}
    for m in result["served_models"]:
        mix[m] = mix.get(m, 0) + 1
    print(f"[{_now()[11:19]}] r{item['rep']} {item['task']:22s} {item['config']:11s} "
          f"pass={result['passed']} wall={wall_ms/1000:.0f}s exec={(result['exec_time_ms'] or 0)/1000:.0f}s "
          f"cost={result['cost_usd']} models={mix}", flush=True)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pinned", required=True)
    ap.add_argument("--source", help="frozen fast-decisions source tree (default: git archive of HEAD)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--configs", default=",".join(OVERRIDES))
    ap.add_argument("--tasks", default="all")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--first-rep", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--deadline", type=int, default=600)
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--keep-workspaces", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    configs = [c.strip() for c in args.configs.split(",") if c.strip()]
    for c in configs:
        if c not in OVERRIDES:
            raise SystemExit(f"unknown config {c}")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", c):
            # The run dir becomes a file:// bundle URI; '+' etc. get
            # percent-encoded and Amplifier then cannot find profile.md.
            raise SystemExit(f"config name {c!r} is not URI-safe")
    task_names = list(T.TASKS) if args.tasks == "all" else [t.strip() for t in args.tasks.split(",")]
    items = [i for i in schedule(task_names, configs, args.first_rep + args.reps - 1) if i["rep"] >= args.first_rep]
    source = Path(args.source) if args.source else out / "candidate-src"
    if not source.exists():
        source.mkdir(parents=True)
        archive = subprocess.run(["git", "archive", "HEAD"], cwd=REPO, capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", str(source)], input=archive, check=True)
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
        (source / "SOURCE_SHA").write_text(sha + "\n")
    (out / f"schedule-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.json").write_text(json.dumps({"configs": configs, "tasks": task_names, "items": items,
                                                    "model": args.model, "source": str(source),
                                                    "pinned": args.pinned, "created": _now()}, indent=2))
    if args.dry_run:
        for i in items:
            print(i)
        return
    load_keys()
    with cf.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(run_one, i, args, source, Path(args.pinned)) for i in items]
        for f in cf.as_completed(futures):
            try:
                f.result()
            except Exception as exc:  # noqa: BLE001
                print(f"run failed: {type(exc).__name__}: {exc}", flush=True)
    subprocess.run(["amplifier", "bundle", "remove", "afast-levers-screen"], capture_output=True, text=True)


if __name__ == "__main__":
    main()
