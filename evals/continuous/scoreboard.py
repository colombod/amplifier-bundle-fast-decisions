"""Scoreboard for the continuous eval: every config against plain, per GOAL.md targets.

    python3 evals/continuous/scoreboard.py [--out /tmp/ampup/continuous-eval]

Reads ``<out>/runs/r*/<task>/<config>/result.json`` and writes
``<out>/scoreboard.md`` and ``<out>/scoreboard.json``.

Ratios (config / plain, same default model): per task, the median over the
config's valid runs divided by the median over plain's valid runs; aggregated
as the geometric mean over tasks both have run (STUDY-DESIGN 8). A run is
valid unless it is an infrastructure failure (no session or no model call).
"Pooled" ratios are sum(config) / sum(plain) over (round, task) pairs where
both have a valid run.

Receipts reconciliation: for every (round, task) pair, the measured saving is
plain's value minus the config's (cost, model calls, wall seconds). The
receipts' claimed saving is the sum of the config run's ``usd_saved`` /
``calls_saved`` / ``seconds_saved``. They reconcile when the per-pair mean
receipt saving lies inside the measured mean saving +/- 2 standard errors
(run-to-run noise); a single pair is reported without a verdict.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

TARGETS = {"cost": 0.50, "full_model_calls": 0.60, "wall": 0.70}
METRICS = {"cost": "cost_usd", "full_model_calls": "full_model_calls", "wall": "wall_time_s",
           "exec": "exec_time_s", "model_calls": "model_calls"}
LEVERS = ("prepared_action", "cache_keepalive", "cheaper_model", "launch_blocked", "loop_stop",
          "context_rightsize")


def load(out: Path) -> list[dict]:
    rows = []
    for p in sorted(out.glob("runs/r*/*/*/result.json")):
        try:
            r = json.loads(p.read_text())
        except ValueError:
            continue
        r["_path"] = str(p.parent)
        rows.append(r)
    # A config whose source moved between rounds (a candidate branch got new
    # commits) is split per source sha, so one ratio never mixes code versions.
    shas: dict[str, set] = defaultdict(set)
    for r in rows:
        shas[r["config"]].add(r.get("source_sha"))
    for r in rows:
        if r["config"] != "plain" and len(shas[r["config"]]) > 1 and r.get("source_sha"):
            r["config"] = f"{r['config']}@{r['source_sha'][:8]}"
    return rows


def _valid(r):
    return not r.get("infrastructure_failure") and r.get(METRICS["cost"]) is not None


def _gmean(xs):
    xs = [x for x in xs if x and x > 0]
    return math.exp(sum(math.log(x) for x in xs) / len(xs)) if xs else None


def _med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _mean_se(xs):
    if not xs:
        return None, None
    m = statistics.fmean(xs)
    se = statistics.stdev(xs) / math.sqrt(len(xs)) if len(xs) > 1 else None
    return m, se


def _reconcile(measured, claimed):
    m, se = _mean_se(measured)
    c = statistics.fmean(claimed) if claimed else 0.0
    if m is None:
        return {"measured_mean": None, "receipts_mean": round(c, 4), "verdict": "no pairs"}
    out = {"measured_mean": round(m, 4), "measured_se": round(se, 4) if se is not None else None,
           "receipts_mean": round(c, 4), "pairs": len(measured)}
    if se is None:
        out["verdict"] = "one pair (no noise estimate)"
    else:
        lo, hi = m - 2 * se, m + 2 * se
        out["band"] = [round(lo, 4), round(hi, 4)]
        out["verdict"] = "reconciles (within noise)" if lo <= c <= hi else "does not reconcile"
    return out


def compute(rows: list[dict]) -> dict:
    by = defaultdict(list)            # (config, task) -> runs
    pair = {}                         # (round, task, config) -> run
    for r in rows:
        by[(r["config"], r["task"])].append(r)
        pair[(r.get("round"), r["task"], r["config"])] = r
    configs = sorted({r["config"] for r in rows}, key=lambda c: (c != "plain", c != "current", c))
    tasks = sorted({r["task"] for r in rows})
    rounds = sorted({r.get("round") for r in rows if r.get("round") is not None})
    board = {}
    for cfg in configs:
        runs = [r for r in rows if r["config"] == cfg]
        valid = [r for r in runs if _valid(r)]
        entry = {
            "runs": len(runs), "valid_runs": len(valid), "infra_failures": len(runs) - len(valid),
            "passed": sum(1 for r in runs if r.get("passed")),
            "pass_rate": round(sum(1 for r in runs if r.get("passed")) / len(runs), 3) if runs else None,
            "source_shas": sorted({r.get("source_sha") for r in runs if r.get("source_sha")}),
            "total_cost_usd": round(sum(r.get("cost_usd") or 0 for r in runs), 4),
            "mean_cost_usd": round(statistics.fmean([r["cost_usd"] for r in valid]), 4) if valid else None,
            "mean_wall_s": round(statistics.fmean([r["wall_time_s"] for r in valid]), 1) if valid else None,
            "mean_full_model_calls": round(statistics.fmean([r["full_model_calls"] for r in valid]), 2)
            if valid else None,
            "served_models": {},
            "receipts_by_lever": {lv: {"n": 0, "calls_saved": 0.0, "usd_saved": 0.0, "seconds_saved": 0.0}
                                  for lv in LEVERS},
            "receipt_traffic": sorted({t for r in runs for t in (r.get("receipts") or {}).get("traffic", [])}),
        }
        for r in runs:
            for m, n in (r.get("served_models") or {}).items():
                entry["served_models"][m] = entry["served_models"].get(m, 0) + n
            for lv, v in ((r.get("receipts") or {}).get("by_lever") or {}).items():
                b = entry["receipts_by_lever"].setdefault(lv, {"n": 0, "calls_saved": 0.0, "usd_saved": 0.0,
                                                               "seconds_saved": 0.0})
                for k in b:
                    b[k] += v.get(k) or 0
        for b in entry["receipts_by_lever"].values():
            for k in ("calls_saved", "usd_saved", "seconds_saved"):
                b[k] = round(b[k], 4)
        entry["receipts_total"] = {k: round(sum(b[k] for b in entry["receipts_by_lever"].values()), 4)
                                   for k in ("n", "calls_saved", "usd_saved", "seconds_saved")}
        if cfg != "plain":
            ratios, per_task = {}, {}
            for key, field in METRICS.items():
                rs = []
                for t in tasks:
                    a = _med([r[field] for r in by.get((cfg, t), []) if _valid(r)])
                    b = _med([r[field] for r in by.get(("plain", t), []) if _valid(r)])
                    if a is not None and b:
                        rs.append(a / b)
                        per_task.setdefault(t, {})[key] = round(a / b, 3)
                ratios[key] = round(_gmean(rs), 3) if rs else None
            entry["ratio_vs_plain"] = ratios
            entry["per_task_ratio"] = per_task
            pooled = {}
            pairs = [(pair[(rd, t, "plain")], pair[(rd, t, cfg)]) for rd in rounds for t in tasks
                     if (rd, t, "plain") in pair and (rd, t, cfg) in pair
                     and _valid(pair[(rd, t, "plain")]) and _valid(pair[(rd, t, cfg)])]
            for key, field in METRICS.items():
                sp = sum(p[field] for p, _ in pairs)
                sc = sum(c[field] for _, c in pairs)
                pooled[key] = round(sc / sp, 3) if sp else None
            entry["pooled_ratio_vs_plain"] = pooled
            entry["pairs"] = len(pairs)
            # Quality: non-inferiority (successes >= plain's - 1 over the same pairs) + critical failures.
            all_pairs = [(pair[(rd, t, "plain")], pair[(rd, t, cfg)]) for rd in rounds for t in tasks
                         if (rd, t, "plain") in pair and (rd, t, cfg) in pair]
            ps = sum(1 for p, _ in all_pairs if p.get("passed"))
            cs = sum(1 for _, c in all_pairs if c.get("passed"))
            crit = [f"r{c.get('round')}/{c['task']}" for p, c in all_pairs if p.get("passed") and not c.get("passed")]
            entry["quality"] = {"paired_runs": len(all_pairs), "plain_successes": ps, "config_successes": cs,
                                "critical_failures": crit,
                                "non_inferior": cs >= ps - 1 and not crit}

            def rsum(run, k):
                return sum((v.get(k) or 0) for v in ((run.get("receipts") or {}).get("by_lever") or {}).values())
            cheap_steps = lambda run: ((run.get("receipts") or {}).get("by_lever") or {}).get(  # noqa: E731
                "cheaper_model", {}).get("n", 0)
            entry["reconciliation"] = {
                "cost_usd": _reconcile([p["cost_usd"] - c["cost_usd"] for p, c in pairs],
                                       [rsum(c, "usd_saved") for _, c in pairs]),
                "model_calls": _reconcile([p["model_calls"] - c["model_calls"] for p, c in pairs],
                                          [rsum(c, "calls_saved") for _, c in pairs]),
                "full_model_calls_vs_calls_saved_plus_cheap_steps": _reconcile(
                    [p["full_model_calls"] - c["full_model_calls"] for p, c in pairs],
                    [rsum(c, "calls_saved") + cheap_steps(c) for _, c in pairs]),
                "wall_s": _reconcile([p["wall_time_s"] - c["wall_time_s"] for p, c in pairs],
                                     [rsum(c, "seconds_saved") for _, c in pairs]),
            }
            r = entry["ratio_vs_plain"]
            entry["meets_goal"] = {
                "cost<=0.50x": r.get("cost") is not None and r["cost"] <= TARGETS["cost"],
                "full_calls<=0.60x": r.get("full_model_calls") is not None
                and r["full_model_calls"] <= TARGETS["full_model_calls"],
                "wall<=0.70x": r.get("wall") is not None and r["wall"] <= TARGETS["wall"],
                "quality_non_inferior": entry["quality"]["non_inferior"],
            }
        board[cfg] = entry
    return {"generated": datetime.now(timezone.utc).isoformat(), "rounds": rounds, "tasks": tasks,
            "configs": configs, "total_cost_usd": round(sum(r.get("cost_usd") or 0 for r in rows), 3),
            "runs": len(rows), "board": board, "targets": TARGETS}


def _f(x, suffix=""):
    return "-" if x is None else f"{x}{suffix}"


def render(sb: dict) -> str:
    L = [f"# Continuous eval scoreboard", "",
         f"Generated {sb['generated'][:19]}Z. Rounds: {sb['rounds']}. Runs: {sb['runs']}. "
         f"Spend so far: ${sb['total_cost_usd']} (provider-reported estimate). Default model claude-opus-5-5; "
         "all runs AFAST_TRAFFIC=test, events in ~/.amplifier/fast-decisions/events-eval.", "",
         "Targets vs plain on the same default model: cost <= 0.50x, full-model calls <= 0.60x, "
         "wall <= 0.70x, quality non-inferior (successes >= plain - 1 and no critical failure, STUDY-DESIGN 8).",
         "", "Ratios are geometric means over tasks of per-task median(config)/median(plain). "
         "This is a screen (few rounds, shared machine), not a confirmation.", "",
         "| config | pass | cost x | full-model calls x | wall x | exec x | pooled cost x | mean $ | mean full calls "
         "| mean wall s | goal met |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for cfg in sb["configs"]:
        e = sb["board"][cfg]
        if cfg == "plain":
            L.append(f"| plain | {e['passed']}/{e['runs']} | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | "
                     f"{_f(e['mean_cost_usd'])} | {_f(e['mean_full_model_calls'])} | {_f(e['mean_wall_s'])} | anchor |")
            continue
        r, p = e["ratio_vs_plain"], e["pooled_ratio_vs_plain"]
        met = [k for k, v in e["meets_goal"].items() if v]
        L.append(f"| {cfg} | {e['passed']}/{e['runs']} | {_f(r['cost'])} | {_f(r['full_model_calls'])} | "
                 f"{_f(r['wall'])} | {_f(r['exec'])} | {_f(p['cost'])} | {_f(e['mean_cost_usd'])} | "
                 f"{_f(e['mean_full_model_calls'])} | {_f(e['mean_wall_s'])} | "
                 f"{'ALL' if len(met) == 4 else (', '.join(met) or 'none')} |")
    L += ["", "## Quality", "", "| config | paired runs | plain successes | config successes | critical failures "
          "| non-inferior |", "|---|---|---|---|---|---|"]
    for cfg in sb["configs"]:
        if cfg == "plain":
            continue
        q = sb["board"][cfg]["quality"]
        L.append(f"| {cfg} | {q['paired_runs']} | {q['plain_successes']} | {q['config_successes']} | "
                 f"{', '.join(q['critical_failures']) or 'none'} | {q['non_inferior']} |")
    L += ["", "## Efficiency receipts by lever (sums over the config's runs)", "",
          "| config | lever | receipts | calls saved | $ saved | s saved |", "|---|---|---|---|---|---|"]
    for cfg in sb["configs"]:
        e = sb["board"][cfg]
        rows = [(lv, b) for lv, b in e["receipts_by_lever"].items() if b["n"]]
        if not rows:
            L.append(f"| {cfg} | (none) | 0 | 0 | 0 | 0 |")
        for lv, b in rows:
            L.append(f"| {cfg} | {lv} | {b['n']} | {b['calls_saved']} | {b['usd_saved']} | {b['seconds_saved']} |")
    L += ["", "## Do receipts reconcile with measured differences? (per paired run, plain minus config)", "",
          "| config | quantity | measured mean saving (+/-2 SE) | receipts mean claim | verdict |",
          "|---|---|---|---|---|"]
    for cfg in sb["configs"]:
        if cfg == "plain":
            continue
        for q, v in sb["board"][cfg]["reconciliation"].items():
            band = v.get("band")
            L.append(f"| {cfg} | {q} | {_f(v.get('measured_mean'))}"
                     f"{f' [{band[0]}, {band[1]}]' if band else ''} | {v.get('receipts_mean')} | {v['verdict']} |")
    L += ["", "## Per-task ratios (cost / full calls / wall)", ""]
    for cfg in sb["configs"]:
        if cfg == "plain":
            continue
        pt = sb["board"][cfg]["per_task_ratio"]
        L.append(f"- **{cfg}**: " + "; ".join(
            f"{t} {_f(v.get('cost'))}/{_f(v.get('full_model_calls'))}/{_f(v.get('wall'))}" for t, v in sorted(pt.items())))
    L += ["", "## Served models (all calls, incl. helper agents)", ""]
    for cfg in sb["configs"]:
        e = sb["board"][cfg]
        L.append(f"- **{cfg}** (sources {', '.join(s[:8] for s in e['source_shas'])}): "
                 + ", ".join(f"{m} {n}" for m, n in sorted(e["served_models"].items(), key=lambda x: -x[1])))
    return "\n".join(L) + "\n"


def write(out: Path) -> dict:
    sb = compute(load(out))
    (out / "scoreboard.json").write_text(json.dumps(sb, indent=2))
    (out / "scoreboard.md").write_text(render(sb))
    return sb


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/ampup/continuous-eval")
    a = ap.parse_args()
    s = write(Path(a.out))
    print((Path(a.out) / "scoreboard.md").read_text())
