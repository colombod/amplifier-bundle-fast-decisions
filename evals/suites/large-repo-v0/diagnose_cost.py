"""Where does a config's cost go? Per-call decomposition from native records.

For each config (runs of the given task kinds): mean per run of the first
call's cache reads/writes and cost, the cost of the remaining calls, calls per
run, and the prompt tokens added by prepared fast_workspace results. Also the
counterfactual "first call cold" cost (cache reads repriced as writes) to
separate cross-run prompt-cache luck from what a config itself does.

    python3 evals/suites/large-repo-v0/diagnose_cost.py /tmp/afast-steps/runs [--kinds question,git]
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def calls(session_dir):
    out, tools = [], []
    for line in Path(session_dir, "events.jsonl").read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        d = e.get("data") or {}
        if e.get("event") == "llm:response":
            u = d.get("usage") or {}
            out.append({"model": d.get("model"), "cr": u.get("cache_read_tokens") or 0,
                        "cw": u.get("cache_write_tokens") or 0, "out": u.get("output_tokens") or 0,
                        "cost": float(u.get("cost_usd") or 0)})
        elif e.get("event") == "tool:post":
            res = d.get("result") or {}
            tools.append((d.get("tool_name"), len(json.dumps(res.get("output"), default=str))))
    return out, tools


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--kinds")
    args = ap.parse_args()
    kinds = set(args.kinds.split(",")) if args.kinds else None
    agg = defaultdict(lambda: defaultdict(float))
    for path in sorted(Path(args.root).glob("r*/*/*/result.json")):
        r = json.loads(path.read_text())
        if (kinds and r["kind"] not in kinds) or r.get("infrastructure_failure") or not r.get("session_dir"):
            continue
        cs, tools = calls(r["session_dir"])
        if not cs:
            continue
        a = agg[r["config"]]
        a["runs"] += 1
        a["calls"] += len(cs)
        a["first_cr"] += cs[0]["cr"]
        a["first_cw"] += cs[0]["cw"]
        a["first_cost"] += cs[0]["cost"]
        a["rest_cost"] += sum(c["cost"] for c in cs[1:])
        a["rest_cw"] += sum(c["cw"] for c in cs[1:])
        a["out"] += sum(c["out"] for c in cs)
        a["fast_ws_chars"] += sum(n for t, n in tools if t == "fast_workspace")
        a["other_tool_chars"] += sum(n for t, n in tools if t != "fast_workspace")
        read_rate = 0.30 if (cs[0]["model"] or "").startswith("claude-sonnet") else 0.20
        write_rate = 3.75 if (cs[0]["model"] or "").startswith("claude-sonnet") else 5.0
        a["first_cold_cost"] += cs[0]["cost"] + cs[0]["cr"] * (write_rate - read_rate) / 1e6
    print(f"{'config':10s} {'runs':>4s} {'calls':>5s} {'1st_cr':>7s} {'1st_cw':>7s} {'1st_$':>6s} {'1st_cold$':>9s} "
          f"{'rest_$':>6s} {'rest_cw':>7s} {'out':>5s} {'fastws_ch':>9s} {'tool_ch':>8s} {'total$':>6s}")
    for c, a in sorted(agg.items()):
        n = a["runs"]
        m = lambda k: a[k] / n
        print(f"{c:10s} {n:4.0f} {m('calls'):5.2f} {m('first_cr'):7.0f} {m('first_cw'):7.0f} {m('first_cost'):6.3f} "
              f"{m('first_cold_cost'):9.3f} {m('rest_cost'):6.3f} {m('rest_cw'):7.0f} {m('out'):5.0f} "
              f"{m('fast_ws_chars'):9.0f} {m('other_tool_chars'):8.0f} {m('first_cost') + m('rest_cost'):6.3f}")


if __name__ == "__main__":
    main()
