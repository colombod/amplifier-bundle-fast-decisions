"""One continuous-eval run (launched inside a Forge terminal by harness.py).

    python3 evals/continuous/worker.py <run_dir>

``<run_dir>/spec.json`` (written by the harness) names the task, config,
frozen source tree, mode, overrides, model, events dir and deadline. The
worker clones the pinned repository into ``<run_dir>/workspace``, applies the
task setup, writes a bundle profile (plain: upstream loop-streaming with
fast-decisions off; others: the bundle composed exactly as a user installs it,
module sources repointed at the frozen tree), runs every turn with

    amplifier run --bundle <profile> --mode single --provider anthropic \
        --model <model> --output-format json [--resume <sid>] <prompt>

with ``PYTHONPATH=<source>/src`` and ``AFAST_TRAFFIC=test``, then records
wall/exec time, provider-reported cost, every model call with its served
model (parent and helper-agent sessions), the task's automatic pass/fail and
the run's ``fast_decisions:efficiency`` receipts, into ``result.json``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tasks as T  # noqa: E402

UPSTREAM_LOOP_SOURCE = ("git+https://github.com/microsoft/amplifier-module-loop-streaming@"
                        "4cc86dd4eae36b40af38b4e2e70b9045649d2903")
BUNDLE_NAME = "afast-continuous-eval"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


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
    else:
        T._mark_base(ws)
    return ws


def build_profile(spec: dict, ws: Path) -> dict:
    src = Path(spec["source_root"])
    upstream = {"max_iterations": spec.get("max_iterations", 40), "extended_thinking": True}
    label = f"continuous-eval {spec['task']} / {spec['config']}"
    tool = {"module": "tool-fast-workspace", "source": (src / "modules/tool-fast-workspace").as_uri(),
            "config": {"root": str(ws)}}
    if spec["mode"] == "off":
        loop = {"module": "loop-streaming", "source": UPSTREAM_LOOP_SOURCE, "config": upstream}
        hooks = [{"module": "hooks-fast-decisions", "source": (src / "modules/hooks-fast-decisions").as_uri(),
                  "config": {"mode": "off", "events_dir": spec["events_dir"], "session_label": label,
                             "observatory": {"enabled": False}}}]
        return {"bundle": {"name": BUNDLE_NAME, "version": "0.1.0"}, "includes": [{"bundle": src.as_uri()}],
                "session": {"orchestrator": loop}, "tools": [tool], "hooks": hooks}
    orch = {**(spec.get("overrides") or {}), **upstream, "events_dir": spec["events_dir"],
            "observatory": {"enabled": False}}
    return {
        "bundle": {"name": BUNDLE_NAME, "version": "0.1.0"},
        "includes": [{"bundle": src.as_uri()}],
        "session": {"orchestrator": {"source": (src / "modules/loop-fast-decisions").as_uri(), "config": orch}},
        "tools": [tool],
        "hooks": [{"module": "hooks-fast-decisions", "source": (src / "modules/hooks-fast-decisions").as_uri(),
                   "config": {"events_dir": spec["events_dir"], "session_label": label,
                              "observatory": {"enabled": False}}}],
    }


def session_metrics(session_dir: Path, host_model: str) -> dict:
    out = {"session": session_dir.name, "calls": [], "cost_usd": 0.0, "cost_known": True,
           "exec_windows": [], "execution_end": 0}
    first = last = None
    for line in (session_dir / "events.jsonl").read_text(errors="replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        name, d = e.get("event"), e.get("data") or {}
        ts = parse_ts(e.get("ts") or e.get("timestamp"))
        if name == "llm:request":
            first = first or ts
            if ts and "first_request" not in out:
                out["first_request"] = ts
        elif name == "llm:response":
            last = ts or last
            usage = d.get("usage") or {}
            try:
                cost = float(usage["cost_usd"])
            except (KeyError, TypeError, ValueError):
                cost = None
                out["cost_known"] = False
            out["cost_usd"] += cost or 0.0
            out["calls"].append({
                "model": d.get("model"), "ts": str(e.get("ts") or e.get("timestamp")), "cost_usd": cost,
                "duration_ms": e.get("duration_ms"),
                **{k: usage.get(k) for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                                              "cache_write_tokens")}})
        elif name == "execution:end":
            out["execution_end"] += 1
            if first and last:
                out["exec_windows"].append((last - first).total_seconds())
            first = last = None
    if first and last:
        out["exec_windows"].append((last - first).total_seconds())
    out["cost_usd"] = round(out["cost_usd"], 6)
    return out


def fd_receipts(events_dir: Path, session_ids: set[str], since: float) -> dict:
    eff, other = [], Counter()
    seen = set()
    for path in sorted(events_dir.glob("*.jsonl")) if events_dir.is_dir() else []:
        try:
            if path.stat().st_mtime < since - 5:
                continue
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            sid, psid = e.get("session_id"), e.get("parent_session_id")
            if sid not in session_ids and psid not in session_ids:
                continue
            if e.get("event_id") in seen:
                continue
            seen.add(e.get("event_id"))
            name = e.get("event", "")
            other[name] += 1
            if name == "fast_decisions:efficiency":
                d = e.get("data") or {}
                eff.append({"event_id": e.get("event_id"), "session_id": sid, **{k: d.get(k) for k in (
                    "lever", "mechanism", "decision", "calls_saved", "usd_saved", "seconds_saved", "traffic",
                    "baseline", "actual", "method")}})
    by_lever: dict[str, dict] = {}
    for r in eff:
        b = by_lever.setdefault(r["lever"] or "?", {"n": 0, "calls_saved": 0.0, "usd_saved": 0.0,
                                                     "seconds_saved": 0.0})
        b["n"] += 1
        for k in ("calls_saved", "usd_saved", "seconds_saved"):
            b[k] += float(r.get(k) or 0)
    return {"efficiency": eff, "by_lever": by_lever, "event_counts": dict(other),
            "traffic": sorted({r.get("traffic") for r in eff if r.get("traffic")})}


def collect(spec: dict, sessions: Path, sid: str | None, started_at: str, t_start: float) -> dict:
    """Native metrics for one run: every session (main + helpers) under the run's project slug
    whose first model call is at or after ``started_at`` (older ones are stale attempts)."""
    t0 = parse_ts(started_at)
    all_sessions = [p for p in (sessions.iterdir() if sessions.exists() else []) if (p / "events.jsonl").exists()]
    per_session, stale = [], []
    for p in sorted(all_sessions):
        s = session_metrics(p, spec["model"])
        first = parse_ts(s["calls"][0]["ts"]) if s["calls"] else None
        if first is not None and t0 is not None and first < t0:
            stale.append(p.name)
            continue
        per_session.append(s)
    parent = next((s for s in per_session if s["session"] == sid), None)
    calls = [c for s in per_session for c in s["calls"]]
    served = Counter(c["model"] for c in calls)
    full_calls = sum(n for m, n in served.items() if m and m.startswith(spec["model"]))
    first_main = parent.get("first_request") if parent else None
    receipts = fd_receipts(Path(spec["events_dir"]).expanduser(), {s["session"] for s in per_session}, t_start)
    return {"per_session": per_session, "calls": calls, "served": served, "full_calls": full_calls,
            "stale": stale, "receipts": receipts,
            "exec_time_s": round(sum(parent["exec_windows"]) if parent else 0.0, 3),
            # Worker start -> the main session's first llm:request (CLI start, bundle load, mount).
            "startup_s": round((first_main - t0).total_seconds(), 3) if first_main and t0 else None}


def parse_stdout(path: Path):
    try:
        text = path.read_text()
    except OSError:
        return None
    try:
        v = json.loads(text).get("response")
        return v if isinstance(v, str) else None
    except (ValueError, AttributeError):
        pass
    # Last JSON object in the stream (defensive).
    idx = text.rfind('{"')
    while idx >= 0:
        try:
            v = json.loads(text[idx:]).get("response")
            return v if isinstance(v, str) else None
        except (ValueError, AttributeError):
            idx = text.rfind('{"', 0, idx)
    return None


def main(run_dir: Path) -> int:
    spec = json.loads((run_dir / "spec.json").read_text())
    task = T.TASKS[spec["task"]]
    for stale in ("workspace", "result.json"):
        p = run_dir / stale
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    ws = build_workspace(run_dir, Path(spec["pinned"]), task)
    profile = build_profile(spec, ws)
    (run_dir / "profile.md").write_text("---\n" + json.dumps(profile, indent=2) + "\n---\n")
    slug = str(ws.resolve()).replace("/", "-").replace("\\", "-").replace(":", "")
    sessions = Path.home() / ".amplifier/projects" / slug / "sessions"
    env = dict(os.environ, AFAST_TRAFFIC="test", AFAST_OBSERVATORY="off", AMPLIFIER_NO_BROWSER="1",
               AMPLIFIER_MEMORY_CAPTURE="off", PYTHONPATH=str(Path(spec["source_root"]) / "src"))
    env.pop("VIRTUAL_ENV", None)
    if sessions.exists():
        # Sessions from an earlier (killed) attempt at this same run dir share the project slug.
        # Kept (renamed) so total_spend.py still counts what the killed attempt cost.
        sessions.rename(sessions.parent / f"sessions-stale-{int(time.time())}")
    started_at, t_start = now(), time.time()
    (run_dir / "running.json").write_text(json.dumps({"pid": os.getpid(), "started_at": started_at}))
    sid, turns, failed = None, [], False
    for i, prompt in enumerate(task["turns"], start=1):
        if failed:
            turns.append({"index": i, "skipped": True, "final": None})
            continue
        cmd = ["amplifier", "run", "--bundle", (run_dir / "profile.md").as_uri(), "--mode", "single",
               "--provider", "anthropic", "--model", spec["model"], "--output-format", "json"]
        if i > 1:
            cmd += ["--resume", sid]
        cmd.append(prompt)
        before = set(sessions.iterdir()) if sessions.exists() else set()
        t0 = time.perf_counter()
        timed_out = False
        out_path = run_dir / f"turn{i}-stdout.txt"
        with open(out_path, "w") as so, open(run_dir / f"turn{i}-stderr.txt", "w") as se:
            proc = subprocess.Popen(cmd, cwd=ws, env=env, stdout=so, stderr=se)
            try:
                code = proc.wait(timeout=spec["deadline"])
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.terminate()
                try:
                    code = proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    code = proc.wait()
        wall = time.perf_counter() - t0
        if i == 1:
            new = [p for p in (sessions.iterdir() if sessions.exists() else []) if p not in before
                   and "_" not in p.name and (p / "events.jsonl").exists()]
            new.sort(key=lambda p: p.stat().st_mtime)
            sid = new[0].name if new else None
        final = parse_stdout(out_path)
        turns.append({"index": i, "exit_code": code, "timed_out": timed_out, "wall_s": round(wall, 3),
                      "final": final})
        if code != 0 or timed_out or sid is None:
            failed = True
    wall_total = sum(t.get("wall_s") or 0 for t in turns)
    m = collect(spec, sessions, sid, started_at, t_start)
    calls, served, full_calls = m["calls"], m["served"], m["full_calls"]
    per_session, receipts = m["per_session"], m["receipts"]
    finals = [t.get("final") for t in turns]
    try:
        passed, detail = task["check"](ws, finals)
    except Exception as exc:  # noqa: BLE001
        passed, detail = False, f"check_error: {type(exc).__name__}: {exc}"
    run_ok = all(t.get("exit_code") == 0 and not t.get("timed_out") for t in turns if not t.get("skipped"))
    infra = sid is None or not calls
    result = {
        **{k: spec.get(k) for k in ("batch", "round", "task", "config", "source_sha", "model", "mode")},
        "kind": task["kind"], "started_at": started_at, "ended_at": now(),
        "wall_time_s": round(wall_total, 3),
        "exec_time_s": m["exec_time_s"], "startup_s": m["startup_s"], "tier": task.get("tier"),
        "launcher": spec.get("launcher", "forge"), "stale_sessions_ignored": m["stale"],
        "cost_usd": round(sum(s["cost_usd"] for s in per_session), 6),
        "cost_known": all(s["cost_known"] for s in per_session),
        "model_calls": len(calls), "full_model_calls": full_calls, "served_models": dict(served),
        "helper_sessions": len([s for s in per_session if s["session"] != sid]),
        "helper_cost_usd": round(sum(s["cost_usd"] for s in per_session if s["session"] != sid), 6),
        "passed": bool(passed) and run_ok and not infra, "check_passed": bool(passed), "check_detail": detail,
        "infrastructure_failure": infra, "session_id": sid,
        "turns": [{**t, "final": (t.get("final") or "")[:1500] if not t.get("skipped") else None} for t in turns],
        "sessions": [{"session": s["session"], "calls": len(s["calls"]), "cost_usd": s["cost_usd"],
                      "served": dict(Counter(c["model"] for c in s["calls"]))} for s in per_session],
        "calls": calls,
        "receipts": {"by_lever": receipts["by_lever"], "event_counts": receipts["event_counts"],
                     "traffic": receipts["traffic"], "n": len(receipts["efficiency"])},
    }
    (run_dir / "receipts.json").write_text(json.dumps(receipts["efficiency"], indent=1))
    for fname, gitargs in (("diff.patch", ["diff", T._base(ws)]), ("status.txt", ["status", "--porcelain"])):
        p = subprocess.run(["git", *gitargs], cwd=ws, capture_output=True, text=True)
        (run_dir / fname).write_text(p.stdout[-200000:])
    tmp = run_dir / "result.json.tmp"
    tmp.write_text(json.dumps(result, indent=2))
    tmp.rename(run_dir / "result.json")
    if not spec.get("keep_workspace"):
        shutil.rmtree(ws, ignore_errors=True)
    print(f"CEVAL_DONE {spec['task']} {spec['config']} pass={result['passed']} wall={wall_total:.0f}s "
          f"cost={result['cost_usd']} calls={len(calls)} full={full_calls} served={dict(served)}", flush=True)
    return 0


def detach(rd: Path) -> None:
    """Re-exec this worker in its own session (no controlling terminal), so a
    Forge daemon restart -- which kills every PTY child -- does not kill the
    run. The Forge terminal then only tails the worker's log."""
    log = open(rd / "worker.log", "a")
    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(rd)], stdin=subprocess.DEVNULL,
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True, cwd=str(rd))
    (rd / "worker.pid").write_text(str(proc.pid))
    print(f"CEVAL detached worker pid {proc.pid}", flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "--detach":
        detach(Path(sys.argv[2]))
        sys.exit(0)
    rd = Path(sys.argv[1])
    try:
        sys.exit(main(rd))
    except Exception as exc:  # noqa: BLE001
        import traceback
        (rd / "worker-error.txt").write_text(traceback.format_exc())
        (rd / "result.json").write_text(json.dumps({"error": f"{type(exc).__name__}: {exc}",
                                                   "infrastructure_failure": True, "passed": False,
                                                   **json.loads((rd / "spec.json").read_text())}))
        raise
