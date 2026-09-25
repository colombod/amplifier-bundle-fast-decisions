"""Recompute a finished run's native metrics from its Amplifier session files.

    python3 evals/continuous/rescore.py /tmp/ampup/continuous-eval/runs/A-r1

Uses worker.collect (sessions whose first model call precedes the run's start
are stale attempts and are ignored). Pass/fail and the check detail are kept;
the previous metric values are stored under ``original_metrics``.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import worker  # noqa: E402

FIELDS = ("cost_usd", "model_calls", "full_model_calls", "served_models", "helper_sessions", "helper_cost_usd",
          "exec_time_s", "sessions")


def rescore(run: Path) -> dict | None:
    rp = run / "result.json"
    r = json.loads(rp.read_text())
    if r.get("error"):
        return None
    spec = json.loads((run / "spec.json").read_text())
    # The run dir may have moved since (batch rename), so locate the project by its main session id.
    hits = list((Path.home() / ".amplifier/projects").glob(f"*/sessions/{r.get('session_id')}"))
    if not r.get("session_id") or not hits:
        return None
    sessions = hits[0].parent
    t_start = datetime.fromisoformat(r["started_at"]).timestamp()
    m = worker.collect(spec, sessions, r.get("session_id"), r["started_at"], t_start)
    if not m["per_session"]:
        return None
    r.setdefault("original_metrics", {k: r.get(k) for k in FIELDS})
    sid = r.get("session_id")
    r.update({
        "cost_usd": round(sum(s["cost_usd"] for s in m["per_session"]), 6),
        "model_calls": len(m["calls"]), "full_model_calls": m["full_calls"], "served_models": dict(m["served"]),
        "helper_sessions": len([s for s in m["per_session"] if s["session"] != sid]),
        "helper_cost_usd": round(sum(s["cost_usd"] for s in m["per_session"] if s["session"] != sid), 6),
        "exec_time_s": m["exec_time_s"], "startup_s": m["startup_s"], "stale_sessions_ignored": m["stale"],
        "sessions": [{"session": s["session"], "calls": len(s["calls"]), "cost_usd": s["cost_usd"],
                      "served": dict(Counter(c["model"] for c in s["calls"]))} for s in m["per_session"]],
        "calls": m["calls"], "rescored": True,
    })
    rp.write_text(json.dumps(r, indent=2))
    return r


if __name__ == "__main__":
    for root in sys.argv[1:]:
        for rp in sorted(Path(root).glob("*/*/result.json")):
            r = rescore(rp.parent)
            if r:
                o = r["original_metrics"]
                print(f"{r['task']:20s} {r['config']:18s} cost {o['cost_usd']} -> {r['cost_usd']}  calls "
                      f"{o['model_calls']} -> {r['model_calls']}  stale={r['stale_sessions_ignored']}")
