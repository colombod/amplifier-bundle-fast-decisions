#!/usr/bin/env python3
"""Live, local AnyJev integration smoke. No agent tasks or quality benchmark.

Loads a pre-downloaded model, serves it on an ephemeral loopback port, sends
the actual difficulty question through the bundle client, then stops the
server. Run in the optional AnyJev environment with PYTHONPATH=src.
"""
import argparse
import asyncio
import importlib.metadata
import json
import platform
import threading
import time
from pathlib import Path

from amplifier_fast_decisions.anyjev_backend import AnyJevBackend
from amplifier_fast_decisions.anyjev_server import Judge, make_decider, make_server
from amplifier_fast_decisions.contracts import DecisionRequest, Question, digest
from amplifier_fast_decisions.orchestrator import DIFFICULTY_CRITERIA, DIFFICULTY_INSTRUCTIONS

CASES = {
    "typo": "Fix the spelling of teh to the in README.md.",
    "edge": "Fix clamp(value, low, high) so values below low return low; keep the existing API.",
    "distributed": ("Diagnose intermittent cross-region duplicate payments across the scheduler, queue, "
                    "database, and payment service. Identify the race, preserve backwards compatibility, "
                    "migrate existing records, and add integration tests."),
}


async def measure(url):
    backend = AnyJevBackend(model="anyjev-smoke", url=url, level="L0", timeout_ms=60000)
    rows = []
    for repeat in range(2):
        for name, task in CASES.items():
            for reverse in (False, True):
                criteria = dict(reversed(list(DIFFICULTY_CRITERIA.items()))) if reverse else DIFFICULTY_CRITERIA
                question = Question("task_difficulty", "choice", DIFFICULTY_INSTRUCTIONS, criteria)
                start = time.perf_counter()
                row = {"case": name, "repeat": repeat + 1, "reverse": reverse}
                try:
                    result = await backend.ask(DecisionRequest({"task": task}, (), (question,)))
                    row["probabilities"] = result.answers[question.name].probabilities
                except Exception as exc:
                    row["error"] = type(exc).__name__
                row["ms"] = round((time.perf_counter() - start) * 1000, 2)
                rows.append(row)
                print(json.dumps(row), flush=True)
    await backend.close()
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", required=True)
    p.add_argument("--device", default="cpu", choices=["cpu", "mps", "cuda"])
    p.add_argument("--dtype", default="float32")
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    if args.output.exists():
        p.error("output already exists; choose a new evidence path")
    decider = make_decider(args.model, device=args.device, dtype=args.dtype, level="L0", prior="none")
    judge = Judge(decider, model="anyjev-smoke", level="L0")
    server = make_server(judge, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        rows = asyncio.run(measure(f"http://127.0.0.1:{server.server_port}"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    report = {
        "kind": "live local classifier smoke; no completed agent tasks, calibration or quality claim",
        "upstream_commit": "45add301a7aa60ed3420c83d15c061e84e5bce61",
        "model_snapshot": Path(args.model).name,
        "model_config_hash": digest(json.loads((Path(args.model) / "config.json").read_text())),
        "device": args.device, "dtype": args.dtype, "level": "L0", "prior": "none",
        "python": platform.python_version(),
        "versions": {name: importlib.metadata.version(name) for name in ["anyjev", "torch", "transformers", "numpy"]},
        "timing": "client wall time; model load excluded; first request includes cold inference",
        "cases": CASES, "rows": rows,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    if any("error" in row for row in rows):
        raise SystemExit("Smoke failed; inspect error counts before using results")


if __name__ == "__main__":
    main()
