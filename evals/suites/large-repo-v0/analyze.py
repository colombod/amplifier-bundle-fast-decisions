"""Summarize a large-repo-v0 screen: per config, geometric-mean time and cost
ratios vs an anchor config over tasks (median over reps per task first),
pass rate, and the served-model mix (native llm:response model per request).
Infrastructure failures are excluded from ratios and counted separately.

    python3 evals/suites/large-repo-v0/analyze.py /tmp/ampup/routing-levers/runs [--anchor plain] [--json out.json]
"""
from __future__ import annotations

import argparse
import ast
import re
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path


def _rescore_escalation_gate(r):
    """Same rule as the revised tasks._check_gate_fix, applied to the recorded
    check detail of runs scored with the original (stricter) check."""
    m = re.search(r"value=(\S+); changed=(\[.*\])", r.get("check_detail", ""))
    if not m:
        return r["passed"]
    changed = ast.literal_eval(m.group(2))
    ok = m.group(1) == "0.7" and all(
        c == "src/amplifier_fast_decisions/contracts.py" or c.startswith("tests/") for c in changed)
    return ok and r["exit_code"] == 0 and not r["timed_out"]


def _rescore_rates_prefix(r):
    """Revised tasks._check_rates_fix: regression tests allowed."""
    d = r.get("check_detail", "")
    ok = "lookup=True" in d and "test_savings.py: OK" in d
    return ok and r["exit_code"] == 0 and not r["timed_out"]


RESCORE = {"b_escalation_gate": _rescore_escalation_gate, "b_rates_prefix": _rescore_rates_prefix}


def load(root: Path):
    rows = []
    for path in sorted(root.glob("r*/*/*/result.json")):
        r = json.loads(path.read_text())
        if r["task"] in RESCORE:
            r["passed_original"] = r["passed"]
            r["passed"] = RESCORE[r["task"]](r)
        rows.append(r)
    return rows


def geomean(xs):
    xs = [x for x in xs if x and x > 0]
    return math.exp(sum(math.log(x) for x in xs) / len(xs)) if xs else None


def summarize(rows, anchor, kinds=None):
    by = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if kinds and r["kind"] not in kinds:
            continue
        by[r["config"]][r["task"]].append(r)
    configs = sorted(by, key=lambda c: (c != anchor, c))
    out = {}
    for c in configs:
        ratios = {"exec": [], "wall": [], "cost": []}
        runs = [r for t in by[c].values() for r in t]
        ok_runs = [r for r in runs if not r["infrastructure_failure"]]
        for task, rs in by[c].items():
            base = [r for r in by[anchor].get(task, []) if not r["infrastructure_failure"]]
            mine = [r for r in rs if not r["infrastructure_failure"]]
            if not base or not mine:
                continue
            for key, field in (("exec", "exec_time_ms"), ("wall", "wall_time_ms"), ("cost", "cost_usd")):
                a = [r[field] for r in base if r[field]]
                b = [r[field] for r in mine if r[field]]
                if a and b:
                    ratios[key].append(statistics.median(b) / statistics.median(a))
        mix = Counter(m for r in ok_runs for m in r["served_models"])
        total = sum(mix.values()) or 1
        faster = sum(1 for x in ratios["exec"] if x < 1)
        out[c] = {
            "runs": len(runs), "infra_failures": len(runs) - len(ok_runs),
            "passed": sum(r["passed"] for r in ok_runs), "scored": len(ok_runs),
            "exec_ratio": geomean(ratios["exec"]), "wall_ratio": geomean(ratios["wall"]),
            "cost_ratio": geomean(ratios["cost"]), "tasks_compared": len(ratios["exec"]),
            "tasks_faster": faster,
            "mean_cost_usd": statistics.mean([r["cost_usd"] for r in ok_runs if r["cost_usd"]] or [0]),
            "mean_exec_s": statistics.mean([(r["exec_time_ms"] or 0) / 1000 for r in ok_runs] or [0]),
            "served_mix": {m: round(n / total, 3) for m, n in mix.most_common()},
            "requests": total,
            "reasons": dict(Counter(d["reason_code"] for r in ok_runs for d in r["fd"]["difficulty_judged"])),
        }
    return out


def table(summary, title):
    lines = [f"### {title}", "",
             "| config | pass | exec ratio | wall ratio | cost ratio | faster tasks | mean exec s | mean $ | served-model mix (requests) |",
             "|---|---|---|---|---|---|---|---|---|"]
    f = lambda x: f"{x:.2f}x" if x else "-"
    for c, s in summary.items():
        mix = ", ".join(f"{m.replace('claude-', '')} {v:.0%}" for m, v in s["served_mix"].items())
        lines.append(f"| {c} | {s['passed']}/{s['scored']} | {f(s['exec_ratio'])} | {f(s['wall_ratio'])} | "
                     f"{f(s['cost_ratio'])} | {s['tasks_faster']}/{s['tasks_compared']} | {s['mean_exec_s']:.1f} | "
                     f"{s['mean_cost_usd']:.3f} | {mix} ({s['requests']}) |")
    return "\n".join(lines)


def per_task(rows):
    by = defaultdict(dict)
    for r in rows:
        key = r["config"]
        cell = by[r["task"]].setdefault(key, [])
        mix = Counter(m.replace("claude-", "") for m in r["served_models"])
        cell.append(f"{'P' if r['passed'] else 'F'} {(r['exec_time_ms'] or 0)/1000:.0f}s ${r['cost_usd'] or 0:.2f} "
                    + "+".join(f"{m}:{n}" for m, n in mix.items()))
    configs = sorted({r["config"] for r in rows})
    lines = ["| task | " + " | ".join(configs) + " |", "|---|" + "---|" * len(configs)]
    for task, cells in by.items():
        lines.append(f"| {task} | " + " | ".join("<br>".join(cells.get(c, ["-"])) for c in configs) + " |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--anchor", default="plain")
    ap.add_argument("--json")
    ap.add_argument("--per-task", action="store_true")
    args = ap.parse_args()
    rows = load(Path(args.root))
    result = {"all": summarize(rows, args.anchor),
              "read_only": summarize(rows, args.anchor, {"question", "git"}),
              "edits": summarize(rows, args.anchor, {"edit", "bugfix"})}
    print(table(result["all"], f"All tasks (anchor {args.anchor})"))
    print()
    print(table(result["read_only"], "Read-only tasks (question + git)"))
    print()
    print(table(result["edits"], "Edit + bugfix tasks"))
    if args.per_task:
        print()
        print(per_task(rows))
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
