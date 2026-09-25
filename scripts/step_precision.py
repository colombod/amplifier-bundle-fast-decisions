"""Offline precision of the per-step action set on recorded Amplifier sessions.

Replays every recorded model call (native ``llm:request`` raw messages and the
matching ``llm:response``): classifies the step as the orchestrator would,
builds the prepared candidates it would offer (turn start / routine read-only
continuation) and the sleep-poll repeat it would issue, and checks them
against what the model actually did next. Reports, per step class, calls and
provider cost, and per action:

* poll repeat (rule, no judge): fires, and precision = the model's actual next
  call was exactly the repeated call (a call really saved);
* prepared candidates (judged by Jev live): offered, and the ceiling hit rate =
  the model's actual next call read one of the offered files / asked git
  status (Jev can only be right when the set contains the right action).

Candidates are built against the workspace as it is NOW (files may have
moved since the session), so file-candidate numbers are approximate.

    PYTHONPATH=src python3 scripts/step_precision.py ~/.amplifier/projects/<project>/sessions [...] [--json out]
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from amplifier_fast_decisions import step_actions as sa
from amplifier_fast_decisions.workspace import WorkspaceTool


def _workspace(cache: dict, path: str | None):
    if not path:
        return None
    if path not in cache:
        try:
            cache[path] = WorkspaceTool(path)
        except (OSError, ValueError):
            cache[path] = None
    return cache[path]


def _read_target(name: str, args: dict, ws) -> str | None:
    if name == "read_file":
        target = args.get("file_path")
    elif name == "fast_workspace" and args.get("operation") == "read":
        target = args.get("path")
    elif name in sa.SHELL_TOOLS:
        m = re.search(r"\b(?:cat|head|tail|sed|nl|less)\b[^|;&]*?\s((?:/|\./)?[\w./@+-]+\.\w+)", str(args.get("command", "")))
        target = m.group(1) if m else None
    else:
        target = None
    return ws.relative(target) if (isinstance(target, str) and ws is not None) else target


def replay(session_dirs: list[Path], default_cwd: str | None = None) -> dict:
    by_class = defaultdict(lambda: {"calls": 0, "cost_usd": 0.0})
    poll = Counter()
    offered = Counter()
    cache: dict = {}
    for sdir in session_dirs:
        path = sdir / "events.jsonl"
        if not path.exists():
            continue
        cwd, pending = default_cwd, None
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"llm:request"' not in line and '"llm:response"' not in line and '"session:start"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                d = e.get("data") or {}
                if e.get("event") == "session:start":
                    cwd = d.get("working_dir") or d.get("cwd") or cwd
                    continue
                if e.get("event") == "llm:request":
                    raw = d.get("raw") or {}
                    pending = {"messages": raw.get("messages") or []} if raw.get("messages") else None
                    continue
                if pending is None:
                    continue
                req, pending = pending, None
                raw = d.get("raw") or {}
                actual = sa.calls_of({"content": raw.get("content") or []})
                usage = d.get("usage") or {}
                try:
                    cost = float(usage.get("cost_usd") or 0)
                except (TypeError, ValueError):
                    cost = 0.0
                view = sa.analyze(req)
                kind, _ = sa.classify(view)
                by_class[kind]["calls"] += 1
                by_class[kind]["cost_usd"] += cost
                if len(view.calls) == 1 and view.calls[0][0] in sa.SHELL_TOOLS and sa._SLEEP.search(
                        str(view.calls[0][1].get("command", ""))):
                    poll["after_sleep_poll_calls"] += 1
                    poll["after_sleep_poll_usd"] += cost
                    if "timed out" in "\n".join(view.results)[:400].lower():
                        poll["after_sleep_poll_tool_timeout"] += 1
                    if len(actual) == 1 and actual[0][0] in sa.SHELL_TOOLS and sa._SLEEP.search(
                            str(actual[0][1].get("command", ""))):
                        poll["next_is_sleep_poll_again"] += 1
                        poll["next_is_sleep_poll_again_usd"] += cost
                    if len(actual) == 1 and actual[0][1].get("command") == view.calls[0][1].get("command"):
                        poll["next_is_identical"] += 1
                repeat = sa.poll_repeat(view, {"repeat_polls": True})
                if repeat is not None:
                    poll["fires"] += 1
                    poll["cost_of_fired_calls"] += cost
                    if len(actual) == 1 and actual[0][0] == repeat[0] and actual[0][1].get("command") == repeat[1].get("command"):
                        poll["exact_hits"] += 1
                        poll["usd_hit"] += cost
                if kind in (sa.TURN_START, sa.ROUTINE):
                    ws = _workspace(cache, cwd)
                    cands = sa.candidates_for(view, kind, ws) if ws is not None else []
                    if cands:
                        key = kind
                        offered[key + ":offered"] += 1
                        paths = {c.arguments.get("path") for c in cands if c.arguments.get("operation") == "read"}
                        wants_git = any(c.arguments.get("operation") == "git" for c in cands)
                        hit = False
                        for name, args in actual:
                            if _read_target(name, args, ws) in paths:
                                hit = True
                            if wants_git and name in sa.SHELL_TOOLS and re.search(
                                    r"\bgit\s+(status|log|branch|rev-list|diff)\b", str(args.get("command", ""))):
                                hit = True
                        if hit:
                            offered[key + ":ceiling_hits"] += 1
                            offered[key + ":usd_hit"] += cost
    total = sum(v["calls"] for v in by_class.values()) or 1
    total_usd = sum(v["cost_usd"] for v in by_class.values()) or 1.0
    return {
        "sessions": len(session_dirs), "calls": total, "cost_usd": round(total_usd, 2),
        "by_class": {k: {"calls": v["calls"], "pct_calls": round(100 * v["calls"] / total, 1),
                         "cost_usd": round(v["cost_usd"], 2), "pct_cost": round(100 * v["cost_usd"] / total_usd, 1)}
                     for k, v in sorted(by_class.items(), key=lambda kv: -kv[1]["cost_usd"])},
        "poll_repeat": {**{k: (round(v, 2) if isinstance(v, float) else v) for k, v in poll.items()}, "precision": round(poll["exact_hits"] / poll["fires"], 3) if poll["fires"] else None},
        "prepared_candidates": {**{k: (round(v, 2) if isinstance(v, float) else v) for k, v in offered.items()},
                                **{f"{k}:ceiling_precision": round(offered[k + ':ceiling_hits'] / offered[k + ':offered'], 3)
                                   for k in (sa.TURN_START, sa.ROUTINE) if offered[k + ':offered']}},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="+")
    ap.add_argument("--json")
    ap.add_argument("--cwd", help="workspace root for file candidates (sessions do not record it)")
    args = ap.parse_args()
    dirs = []
    for root in args.roots:
        p = Path(root).expanduser()
        dirs += [d for d in sorted(p.iterdir()) if d.is_dir()] if (p / "events.jsonl").exists() is False else [p]
    report = replay(dirs, args.cwd)
    text = json.dumps(report, indent=1)
    if args.json:
        Path(args.json).write_text(text)
    print(text)


if __name__ == "__main__":
    main()
