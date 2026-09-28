"""Summarize a per-step-actions screen (configs plain, default, steps, steps-L2).

Per config: pass rate, geometric-mean ratios vs the anchor over tasks (median
over reps first) for exec time, wall time, provider cost and cache-normalized
cost, model calls and full-model (host) calls per request, and the efficiency
receipts per lever and mechanism. Then the receipt check: for each config, the
per-run receipt totals (calls, USD, seconds saved) against the measured
difference from the comparator config (default: same bundle without the step
actions), both as per-task means summed over tasks.

Cache-normalized cost reprices the first request's cache reads as cache writes
(every run starting cold), removing cross-run prompt-cache luck: concurrent
runs of configs with the same tool/system prefix share a warm ~32k-token
prefix, which moves a run's cost by ~$0.15.

    python3 evals/suites/large-repo-v0/analyze_steps.py /tmp/afast-steps/runs [--anchor plain]
        [--comparator default] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from analyze import RESCORE  # noqa: E402

HOST = "claude-opus-5-5"
# USD per MTok: cache read, cache write (Opus 5.5 / Sonnet 5 / Haiku 4.5).
CACHE_RATES = {"claude-opus-5-5": (0.20, 5.0), "claude-sonnet-5": (0.30, 3.75), "claude-haiku-4-5": (0.10, 1.25)}


def first_request(session_dir: str | None) -> dict | None:
    if not session_dir or not Path(session_dir, "events.jsonl").exists():
        return None
    for line in Path(session_dir, "events.jsonl").read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") == "llm:response":
            return {"model": (e.get("data") or {}).get("model"), **((e.get("data") or {}).get("usage") or {})}
    return None


def load(root: Path):
    rows = []
    for path in sorted(root.glob("r*/*/*/result.json")):
        r = json.loads(path.read_text())
        if r["task"] in RESCORE:
            r["passed"] = RESCORE[r["task"]](r)
        first = first_request(r.get("session_dir")) or {}
        rates = CACHE_RATES.get(first.get("model") or "", CACHE_RATES[HOST])
        extra = (first.get("cache_read_tokens") or 0) * (rates[1] - rates[0]) / 1e6
        r["cost_norm"] = (r["cost_usd"] + extra) if r.get("cost_usd") is not None else None
        r["host_calls"] = sum(1 for m in r["served_models"] if m and m.startswith(HOST))
        eff = r.get("fd", {}).get("efficiency", [])
        r["rc_calls"] = sum(int(e.get("calls_saved") or 0) for e in eff)
        r["rc_usd"] = sum(float(e.get("usd_saved") or 0) for e in eff)
        r["rc_s"] = sum(float(e.get("seconds_saved") or 0) for e in eff)
        rows.append(r)
    return rows


def geomean(xs):
    xs = [x for x in xs if x and x > 0]
    return math.exp(sum(math.log(x) for x in xs) / len(xs)) if xs else None


def per_task(rows):
    by = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if not r["infrastructure_failure"]:
            by[r["config"]][r["task"]].append(r)
    return by


def ratio(by, config, anchor, field):
    out = []
    for task, rs in by[config].items():
        a = [r[field] for r in by[anchor].get(task, []) if r[field] is not None]
        b = [r[field] for r in rs if r[field] is not None]
        if a and b and statistics.median(a) > 0:
            out.append(statistics.median(b) / statistics.median(a))
    return geomean(out)


def mean_field(rs, field):
    vals = [r[field] for r in rs if r[field] is not None]
    return sum(vals) / len(vals) if vals else None


def summarize(rows, anchor, comparator, kinds=None):
    rows = [r for r in rows if not kinds or r["kind"] in kinds]
    by = per_task(rows)
    configs = sorted(by, key=lambda c: (c != anchor, c != comparator, c))
    out = {}
    for c in configs:
        runs = [r for t in by[c].values() for r in t]
        all_runs = [r for r in rows if r["config"] == c]
        levers = defaultdict(lambda: {"receipts": 0, "calls_saved": 0, "usd_saved": 0.0, "seconds_saved": 0.0})
        mechanisms = Counter()
        for r in runs:
            for e in r.get("fd", {}).get("efficiency", []):
                key = f"{e['lever']} | {e['mechanism']} | {e['decision']}"
                agg = levers[key]
                agg["receipts"] += 1
                agg["calls_saved"] += int(e.get("calls_saved") or 0)
                agg["usd_saved"] += float(e.get("usd_saved") or 0)
                agg["seconds_saved"] += float(e.get("seconds_saved") or 0)
            for s in r.get("fd", {}).get("step_decided", []):
                mechanisms[f"{s['step_class']} -> {s['step_action']}"] += 1
        mix = Counter(m for r in runs for m in r["served_models"])
        total = sum(mix.values()) or 1
        n = len(runs) or 1
        check = None
        if c != comparator and comparator in by:
            measured = {"calls": 0.0, "usd": 0.0, "usd_norm": 0.0, "exec_s": 0.0}
            receipts = {"calls": 0.0, "usd": 0.0, "seconds": 0.0}
            tasks = 0
            for task, rs in by[c].items():
                base = by[comparator].get(task)
                if not base:
                    continue
                tasks += 1
                measured["calls"] += mean_field(base, "requests") - mean_field(rs, "requests")
                measured["usd"] += (mean_field(base, "cost_usd") or 0) - (mean_field(rs, "cost_usd") or 0)
                measured["usd_norm"] += (mean_field(base, "cost_norm") or 0) - (mean_field(rs, "cost_norm") or 0)
                measured["exec_s"] += ((mean_field(base, "exec_time_ms") or 0) - (mean_field(rs, "exec_time_ms") or 0)) / 1000
                receipts["calls"] += mean_field(rs, "rc_calls")
                receipts["usd"] += mean_field(rs, "rc_usd")
                receipts["seconds"] += mean_field(rs, "rc_s")
            check = {"tasks": tasks, "measured_vs_" + comparator: {k: round(v, 4) for k, v in measured.items()},
                     "receipts": {k: round(v, 4) for k, v in receipts.items()}}
        out[c] = {
            "runs": len(all_runs), "infra_failures": len(all_runs) - len(runs),
            "pass": f"{sum(r['passed'] for r in all_runs)}/{len(all_runs)}",
            "exec_x": _r(ratio(by, c, anchor, "exec_time_ms")), "wall_x": _r(ratio(by, c, anchor, "wall_time_ms")),
            "cost_x": _r(ratio(by, c, anchor, "cost_usd")), "cost_norm_x": _r(ratio(by, c, anchor, "cost_norm")),
            "calls_per_req": round(sum(r["requests"] for r in runs) / n, 2),
            "host_calls_per_req": round(sum(r["host_calls"] for r in runs) / n, 2),
            "host_calls_x": _r(ratio(by, c, anchor, "host_calls")),
            "mean_cost_usd": _r(mean_field(runs, "cost_usd")), "mean_cost_norm_usd": _r(mean_field(runs, "cost_norm")),
            "mean_exec_s": _r((mean_field(runs, "exec_time_ms") or 0) / 1000),
            "served_mix": {m: f"{100 * k / total:.0f}%" for m, k in mix.most_common()},
            "steps": dict(mechanisms.most_common()),
            "receipts": {k: {**v, "usd_saved": round(v["usd_saved"], 4), "seconds_saved": round(v["seconds_saved"], 2)}
                         for k, v in sorted(levers.items())},
            "receipt_check": check,
            "jev_next_action_ms_median": _r(statistics.median(ms) if (ms := [x for r in runs for x in
                                                                         r.get("fd", {}).get("next_action_ms", [])]) else None),
        }
    return out


def _r(x):
    return None if x is None else round(x, 4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--anchor", default="plain")
    ap.add_argument("--comparator", default="default")
    ap.add_argument("--json")
    args = ap.parse_args()
    rows = load(Path(args.root))
    report = {
        "all": summarize(rows, args.anchor, args.comparator),
        "read_only (question+git)": summarize(rows, args.anchor, args.comparator, {"question", "git"}),
        "edit+bugfix": summarize(rows, args.anchor, args.comparator, {"edit", "bugfix"}),
    }
    text = json.dumps(report, indent=1)
    if args.json:
        Path(args.json).write_text(text)
    print(text)


if __name__ == "__main__":
    main()
