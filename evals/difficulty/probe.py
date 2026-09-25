#!/usr/bin/env python3
"""Offline probe: can a fast judge tell simple coding tasks from complex ones?

The orchestrator's turn-start router asks one typed question -- "simple or
complex?" -- to pick the start model for the whole turn (cheap vs strong).
This probe measures each judge on labeled prompts before any live run:

  * SWE-bench Verified human difficulty annotations: "<15 min fix" (simple)
    vs "1-4 hours" / ">4 hours" (complex), balanced and seeded.
  * The S1 battery prompts (simple) as an out-of-suite sanity set.

Judges: jev (TypeSafe, external -- requires TYPESAFE_API_KEY), any local
Ollama model (one-token typed question, permutation-debiased), and a
deterministic length rule. Metric: ROC AUC of P(complex) (threshold-free),
plus accuracy at 0.5 and latency.

Usage:
  PYTHONPATH=src python3 evals/difficulty/probe.py DATASET.jsonl \\
      --judge jev --judge ollama:qwen3:0.6b --judge rules --out report.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from amplifier_fast_decisions.contracts import DecisionRequest, Question  # noqa: E402

QUESTION = Question(
    name='task_difficulty', type='choice',
    instructions=('Classify this software engineering task by how much work it takes an expert '
                  'engineer who is new to the codebase.'),
    criteria={
        'simple': 'A small, well-specified, localized change: the fix location is clear and it takes '
                  'minutes (a typo, a one-function bug, a documented contract, an obvious edge case).',
        'complex': 'Substantial work: the root cause must be investigated across an unfamiliar codebase, '
                   'several files or subsystems change, or the behavior is subtle; it takes an hour or more.',
    },
)
STATE_CHARS = 2500


def state_for(task: str) -> dict:
    return {'task': task[:STATE_CHARS]}


def auc(scores_pos, scores_neg):
    if not scores_pos or not scores_neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in scores_pos for n in scores_neg)
    return wins / (len(scores_pos) * len(scores_neg))


async def judge_rows(judge: str, rows: list[dict]) -> list[dict]:
    out = []
    backend = None
    if judge == 'jev':
        from amplifier_fast_decisions.backends import JevBackend
        backend = JevBackend(model='jev-1.13.0', timeout_ms=10000)
    elif judge.startswith('hosted:'):
        # OpenAI-compatible host (e.g. vLLM behind LiteLLM on RunPod): URL from
        # FAST_DECISIONS_HOSTED_URL, bearer token from FAST_DECISIONS_HOSTED_TOKEN (or the env var named by
        # FAST_DECISIONS_HOSTED_TOKEN_ENV), e.g. to point the probe straight at a vLLM/RunPod origin.
        import os
        from amplifier_fast_decisions.local_backend import HostedBackend
        backend = HostedBackend(model=judge.split(':', 1)[1], url=os.environ.get('FAST_DECISIONS_HOSTED_URL'),
                                timeout_ms=20000, token_env=os.environ.get('FAST_DECISIONS_HOSTED_TOKEN_ENV'))
    elif judge.startswith('ollama:'):
        from amplifier_fast_decisions.local_backend import OllamaBackend
        backend = OllamaBackend(model=judge.split(':', 1)[1], timeout_ms=20000)
    for row in rows:
        t0 = time.perf_counter()
        p_complex, error = None, None
        if judge == 'rules':
            # Length rule: long, narrative issues read as complex.
            p_complex = min(1.0, len(row['task']) / 3000)
        else:
            try:
                result = await backend.ask(DecisionRequest(state=state_for(row['task']), candidates=(),
                                                           questions=(QUESTION,)))
                p_complex = result.answers['task_difficulty'].probabilities.get('complex')
            except Exception as exc:  # measured, never fatal
                error = type(exc).__name__ + ': ' + str(exc)[:120]
        out.append({'id': row['id'], 'source': row['source'], 'label': row['label'],
                    'p_complex': p_complex, 'ms': (time.perf_counter() - t0) * 1000, 'error': error})
    if backend is not None and hasattr(backend, 'close'):
        await backend.close()
    return out


def summarize(judge, scored):
    ok = [r for r in scored if r['p_complex'] is not None]
    summary = {'judge': judge, 'n': len(scored), 'errors': len(scored) - len(ok)}
    for name, subset in (('swe_verified', [r for r in ok if r['source'] == 'swe-verified']), ('all', ok)):
        pos = [r['p_complex'] for r in subset if r['label'] == 'complex']
        neg = [r['p_complex'] for r in subset if r['label'] == 'simple']
        acc = (sum((r['p_complex'] >= 0.5) == (r['label'] == 'complex') for r in subset) / len(subset)) if subset else None
        summary[name] = {'auc': None if auc(pos, neg) is None else round(auc(pos, neg), 3),
                         'acc_at_0.5': None if acc is None else round(acc, 3), 'n': len(subset)}
    s1 = [r['p_complex'] for r in ok if r['source'] == 's1']
    summary['s1_mean_p_complex'] = round(statistics.mean(s1), 3) if s1 else None
    ms = sorted(r['ms'] for r in ok)
    summary['latency_ms_p50'] = round(ms[len(ms) // 2], 1) if ms else None
    summary['latency_ms_p90'] = round(ms[min(len(ms) - 1, int(len(ms) * 0.9))], 1) if ms else None
    summary['latency_ms_max'] = round(ms[-1], 1) if ms else None
    summary['decisions_over_5s'] = sum(m > 5000 for m in ms)
    return summary


async def main_async(args):
    rows = [json.loads(line) for line in Path(args.dataset).read_text().splitlines() if line.strip()]
    report = {'dataset': args.dataset, 'question': QUESTION.instructions, 'results': [], 'rows': {}}
    for judge in args.judge:
        scored = await judge_rows(judge, rows)
        report['rows'][judge] = scored
        summary = summarize(judge, scored)
        report['results'].append(summary)
        print(json.dumps(summary), flush=True)
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('dataset')
    p.add_argument('--judge', action='append', required=True)
    p.add_argument('--out')
    asyncio.run(main_async(p.parse_args()))


if __name__ == '__main__':
    main()
