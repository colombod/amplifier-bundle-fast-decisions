"""Ask hosted Jev the router's two questions (task_difficulty, edits_code) for
every large-repo-v0 prompt, in the same batched call the large_repo lever
makes, and print what each lever config would route. No model calls; needs
TYPESAFE_API_KEY. Output JSON goes to --out.

    PYTHONPATH=src python3 evals/suites/large-repo-v0/jev_prescreen.py --reps 3 --out /tmp/x.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tasks as T  # noqa: E402
from configs import CONFIGS  # noqa: E402
from keys import load_keys  # noqa: E402

from amplifier_fast_decisions.backends import JevBackend  # noqa: E402
from amplifier_fast_decisions.contracts import DecisionRequest, Question  # noqa: E402
from amplifier_fast_decisions.orchestrator import DIFFICULTY_CRITERIA, DIFFICULTY_INSTRUCTIONS  # noqa: E402
from amplifier_fast_decisions import routing_levers as RL  # noqa: E402


async def ask(backend, prompt):
    questions = (Question(name="task_difficulty", type="choice", instructions=DIFFICULTY_INSTRUCTIONS,
                          criteria=DIFFICULTY_CRITERIA), RL.edit_question())
    start = time.perf_counter()
    result = await backend.ask_many(DecisionRequest(state={"task": prompt[:2500]}, candidates=(),
                                                    questions=questions))
    ms = (time.perf_counter() - start) * 1000
    d = result.answers["task_difficulty"].probabilities
    e = result.answers["edits_code"].probabilities
    return d.get("complex"), e.get("edits"), ms


def route(routing, p_complex, p_edit):
    """What the large-repo router does for a scope-gated turn under ``routing``."""
    if not routing or not routing.get("large_repo"):
        return "host(scope)"
    if not RL.large_repo_allows_cheap(routing, p_complex, p_edit):
        return "host"
    picked = RL.choose_tier(routing, p_complex) or ("cheap", None, None)
    return picked[1] or routing.get("start_model", "claude-sonnet-5")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--out")
    args = ap.parse_args()
    load_keys()
    backend = JevBackend(timeout_ms=5000)
    rows = []
    for name, task in T.TASKS.items():
        samples = [await ask(backend, task["prompt"]) for _ in range(args.reps)]
        pc = statistics.median(s[0] for s in samples)
        pe = statistics.median(s[1] for s in samples)
        routes = {c: route(CONFIGS[c].get("model_routing"), pc, pe) for c in CONFIGS if c != "plain"}
        rows.append({"task": name, "kind": task["kind"], "p_complex": [round(s[0], 3) for s in samples],
                     "p_edit": [round(s[1], 3) for s in samples], "ms": [round(s[2]) for s in samples],
                     "median_p_complex": pc, "median_p_edit": pe, "routes": routes})
        print(f"{name:24s} {task['kind']:8s} pc={pc:.2f} pe={pe:.2f} "
              + " ".join(f"{c}={r}" for c, r in routes.items()))
    await backend.close()
    if args.out:
        Path(args.out).write_text(json.dumps(rows, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
