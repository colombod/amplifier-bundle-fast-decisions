#!/usr/bin/env python3
"""Offline probe for the restricted-softmax scorer prototype (score_backend.py).

Same dataset, question and prompt text as probe.py, so the numbers compare
directly with the current Ollama top_logprobs path. Adds calibration (ECE,
reliability table on the labeled SWE items) and label diagnostics.

Judges:
  llamacpp:<ollama-tag|path.gguf>  full logits via llama-cpp-python (an Ollama tag
                                   resolves to the GGUF blob Ollama serves)
  mlx:<hf-repo-or-path>            full logits via mlx-lm
  ollama-noprefill:<tag>           Ollama, chat-template generation prompt (no
                                   "Answer:" prefill), think:false, top-20 folded,
                                   restricted over labels (the minimal Ollama fix)
  ollama:<tag>                     the current OllamaBackend path (for latency parity)

Usage:
  PYTHONPATH=src python3 evals/difficulty/score_probe.py DATASET.jsonl \\
      --judge llamacpp:qwen3:8b --judge mlx:mlx-community/Qwen3-8B-8bit --out report.json
Needs llama-cpp-python / mlx-lm in the running interpreter (use a scratch venv).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe import QUESTION, auc, state_for  # noqa: E402
from amplifier_fast_decisions.contracts import DecisionRequest  # noqa: E402

OLLAMA_ROOT = os.path.expanduser('~/.ollama/models')


def ollama_blob(tag: str) -> str:
    """GGUF model blob for an Ollama tag (or pass-through for a file path)."""
    if os.path.exists(tag):
        return tag
    name, _, ver = tag.partition(':')
    manifest = json.loads(Path(f'{OLLAMA_ROOT}/manifests/registry.ollama.ai/library/{name}/{ver or "latest"}').read_text())
    for layer in manifest['layers']:
        if layer['mediaType'] == 'application/vnd.ollama.image.model':
            return f"{OLLAMA_ROOT}/blobs/{layer['digest'].replace(':', '-')}"
    raise SystemExit(f'{tag}: no GGUF model layer (safetensors/MLX model?)')


def ece_binary(probs, ys, bins=10):
    n, total, table = len(probs), 0.0, []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, p in enumerate(probs) if lo <= p < hi or (b == bins - 1 and p == 1.0)]
        if not idx:
            continue
        conf = statistics.mean(probs[i] for i in idx)
        frac = statistics.mean(ys[i] for i in idx)
        total += len(idx) / n * abs(conf - frac)
        table.append({'bin': f'{lo:.1f}-{hi:.1f}', 'n': len(idx), 'mean_p': round(conf, 3), 'frac_complex': round(frac, 3)})
    return round(total, 3), table


class OllamaNoPrefill:
    """Minimal fix on the existing Ollama wire: no prefill, template gen prompt."""

    name, external = 'ollama-noprefill', False

    def __init__(self, model, combine='geo'):
        import httpx
        from amplifier_fast_decisions.local_backend import QUESTION_SYSTEM
        self.model, self.combine, self.system = model, combine, QUESTION_SYSTEM
        self.client = httpx.AsyncClient(timeout=60, trust_env=False)
        self.diag = []

    async def ask(self, request):
        from amplifier_fast_decisions.backends import BackendUnavailable
        from amplifier_fast_decisions.contracts import Answer, DecisionResult
        from amplifier_fast_decisions.local_backend import _placeholder_action, build_question_prompt
        from amplifier_fast_decisions.score_backend import combine_orders, restricted_softmax
        q = request.questions[0]
        dists = []
        for reverse in (False, True):
            prompt, labels = build_question_prompt(request.state, q, reverse=reverse)
            body = {'model': self.model, 'stream': False, 'think': False, 'logprobs': True, 'top_logprobs': 20,
                    'keep_alive': '30m', 'options': {'temperature': 0, 'num_predict': 1, 'num_ctx': 4096},
                    'messages': [{'role': 'system', 'content': self.system}, {'role': 'user', 'content': prompt}]}
            payload = (await self.client.post('http://127.0.0.1:11434/api/chat', json=body)).json()
            records = payload.get('logprobs') or []
            if len(records) != 1:
                raise BackendUnavailable('no logprobs')
            folded = {}
            for it in records[0]['top_logprobs']:
                k = it['token'].strip() or it['token']
                folded[k] = folded.get(k, 0.0) + math.exp(it['logprob'])
            present = {lab: folded[lab] for lab in labels if lab in folded}
            mass = sum(present.values())
            self.diag.append({'label_mass_top20': mass, 'labels_missing': len(labels) - len(present)})
            if len(present) < len(labels) or mass < 0.2:
                raise BackendUnavailable('label missing from top-20 or option mass < 0.2')
            dists.append(restricted_softmax({labels[k]: math.log(v) for k, v in present.items()}))
        dist = combine_orders(dists, self.combine)
        return DecisionResult(action=_placeholder_action(self.model), model=self.model, output_tokens=2,
                              answers={q.name: Answer(probabilities=dist, confidence=max(dist.values()))})

    async def close(self):
        await self.client.aclose()


def make_backend(judge, combine, other, answer_prefix='', min_label_mass=0.0):
    kind, _, target = judge.partition(':')
    if kind in ('llamacpp', 'mlx'):
        from amplifier_fast_decisions.score_backend import LlamaCppScorer, MlxScorer, ScoreBackend
        t = time.perf_counter()
        scorer = LlamaCppScorer(ollama_blob(target), model=target) if kind == 'llamacpp' else MlxScorer(target)
        print(f'# loaded {judge} in {time.perf_counter() - t:.1f}s', file=sys.stderr, flush=True)
        return ScoreBackend(scorer, combine=combine, other=other, answer_prefix=answer_prefix,
                            min_label_mass=min_label_mass)
    if kind == 'ollama-noprefill':
        return OllamaNoPrefill(target, combine)
    if kind == 'ollama':
        from amplifier_fast_decisions.local_backend import OllamaBackend
        return OllamaBackend(model=target, timeout_ms=60000)
    raise SystemExit(f'unknown judge {judge}')


async def run_judge(judge, rows, combine, other, warm, answer_prefix='', min_label_mass=0.0):
    backend = make_backend(judge, combine, other, answer_prefix, min_label_mass)
    req = lambda task: DecisionRequest(state=state_for(task), candidates=(), questions=(QUESTION,))  # noqa: E731
    for row in rows[:warm]:  # warm-up (model load, Metal kernels); not scored
        try:
            await backend.ask(req(row['task']))
        except Exception:  # noqa: BLE001
            pass
    out = []
    for row in rows:
        t0 = time.perf_counter()
        p, err, extra = None, None, {}
        try:
            res = await backend.ask(req(row['task']))
            p = res.answers['task_difficulty'].probabilities.get('complex')
        except Exception as exc:  # noqa: BLE001
            err = type(exc).__name__ + ': ' + str(exc)[:120]
        ms = (time.perf_counter() - t0) * 1000
        last = getattr(backend, 'last', {}).get('task_difficulty')
        if last is not None:
            extra = {'label_mass_full_vocab': [round(m, 4) for m in last.label_mass_full_vocab],
                     'p_other': last.p_other, 'label_ids': last.label_ids,
                     'per_order': [{k: round(v, 6) for k, v in d.items()} for d in last.per_order]}
        out.append({'id': row['id'], 'source': row['source'], 'label': row['label'], 'p_complex': p, 'ms': ms,
                    'error': err, **extra})
    if hasattr(backend, 'close'):
        await backend.close()
    return out, getattr(backend, 'diag', None)


def summarize(judge, scored, diag=None):
    ok = [r for r in scored if r['p_complex'] is not None]
    s = {'judge': judge, 'n': len(scored), 'errors': len(scored) - len(ok)}
    for name, subset in (('swe_verified', [r for r in ok if r['source'] == 'swe-verified']), ('all', ok)):
        pos = [r['p_complex'] for r in subset if r['label'] == 'complex']
        neg = [r['p_complex'] for r in subset if r['label'] == 'simple']
        a = auc(pos, neg)
        s[name] = {'auc': None if a is None else round(a, 3), 'n': len(subset),
                   'acc_at_0.5': round(sum((r['p_complex'] >= 0.5) == (r['label'] == 'complex') for r in subset)
                                       / len(subset), 3) if subset else None}
    swe = [r for r in ok if r['source'] == 'swe-verified']
    if swe:
        s['ece_swe'], s['reliability_swe'] = ece_binary([r['p_complex'] for r in swe],
                                                        [r['label'] == 'complex' for r in swe])
    s1 = [r['p_complex'] for r in ok if r['source'] == 's1']
    s['s1_mean_p_complex'] = round(statistics.mean(s1), 3) if s1 else None
    ms = sorted(r['ms'] for r in ok)
    if ms:
        s['latency_ms_p50'] = round(ms[len(ms) // 2], 1)
        s['latency_ms_p90'] = round(ms[int(len(ms) * 0.9)], 1)
    masses = [m for r in ok for m in r.get('label_mass_full_vocab', [])]
    if masses:
        s['label_mass_full_vocab_p50'] = round(statistics.median(masses), 4)
        s['label_mass_full_vocab_min'] = round(min(masses), 4)
    others = [r['p_other'] for r in ok if r.get('p_other') is not None]
    if others:
        s['p_other_p50'] = round(statistics.median(others), 4)
    if diag:
        s['label_missing_calls'] = sum(d['labels_missing'] > 0 for d in diag)
        s['calls'] = len(diag)
    return s


async def main_async(args):
    rows = [json.loads(line) for line in Path(args.dataset).read_text().splitlines() if line.strip()]
    report = {'dataset': args.dataset, 'combine': args.combine, 'other': args.other,
              'answer_prefix': args.answer_prefix, 'min_label_mass': args.min_label_mass, 'results': [], 'rows': {}}
    for judge in args.judge:
        scored, diag = await run_judge(judge, rows, args.combine, args.other, args.warm, args.answer_prefix,
                                       args.min_label_mass)
        report['rows'][judge] = scored
        summary = summarize(judge, scored, diag)
        report['results'].append(summary)
        print(json.dumps({k: v for k, v in summary.items() if k != 'reliability_swe'}), flush=True)
        if args.out:
            Path(args.out).write_text(json.dumps(report, indent=1))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('dataset')
    p.add_argument('--judge', action='append', required=True)
    p.add_argument('--combine', choices=('geo', 'mean'), default='geo')
    p.add_argument('--other', action='store_true', help='append an OTHER option; its mass abstains')
    p.add_argument('--answer-prefix', default='', help='text after the generation prompt, e.g. "Answer:"')
    p.add_argument('--min-label-mass', type=float, default=0.0)
    p.add_argument('--warm', type=int, default=2)
    p.add_argument('--out')
    asyncio.run(main_async(p.parse_args()))


if __name__ == '__main__':
    main()
