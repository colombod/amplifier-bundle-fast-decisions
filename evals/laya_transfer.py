#!/usr/bin/env python3
"""Compare a frozen manifest across base HTTP, typed in-process MPS, and Jev.

Labels never enter requests. Typed in-process timing is NOT comparable to HTTP.
Run with a Python environment containing the pinned Laya SDK and httpx.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import time

from amplifier_fast_decisions.laya_server import _to_jev_shape
from evals.laya_quality import score, summarize


def main():
    import httpx
    import laya

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--checkpoint', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    frozen = args.manifest.read_bytes()
    manifest = json.loads(frozen)
    args.output.mkdir(parents=True, exist_ok=True)
    model = laya.load(args.checkpoint, device='mps')
    rows = []
    with httpx.Client(timeout=15, follow_redirects=False) as client, (args.output / 'requests.jsonl').open('w') as log:
        for order in (0, 1):
            for index, case in enumerate(manifest['cases']):
                arms = ['laya_local', 'laya_typed', 'jev']
                arms = arms[index % 3:] + arms[:index % 3]
                for arm in arms:
                    payload = copy.deepcopy(case['payload'])
                    if order and case['kind'] != 'search':
                        spec = payload['questions']['decision']
                        spec['criteria'] = dict(reversed(list(spec['criteria'].items())))
                    row = dict(arm=arm, id=case['id'], kind=case['kind'], expected=case['expected'], order=order, valid=False)
                    start = time.perf_counter()
                    try:
                        if arm == 'laya_typed':
                            raw = model.predict(payload['state'], payload['questions'])
                            data = _to_jev_shape(raw, payload['questions'], args.checkpoint)
                            row['transport'] = 'in-process MPS; not comparable to HTTP'
                        else:
                            headers = {'User-Agent': 'amplifier-fast-decisions/0.1'}
                            url = 'http://127.0.0.1:8090/v1/decide'
                            if arm == 'jev':
                                url = 'https://api.typesafe.ai/v1/systemone'
                                headers['Authorization'] = 'Bearer ' + os.environ['TYPESAFE_API_KEY']
                                payload['model'] = 'jev-1.13.0'
                            response = client.post(url, json=payload, headers=headers)
                            row['status'] = response.status_code
                            response.raise_for_status()
                            data = response.json()
                        row.update(elapsed_ms=(time.perf_counter() - start) * 1000,
                                   model=data.get('model'), answer=data.get('answers', {}).get('decision'), usage=data.get('usage'))
                        row.update(score(case, data, manifest.get('threshold', .75)), valid=True)
                    except Exception as exc:
                        row.update(error=type(exc).__name__, elapsed_ms=(time.perf_counter() - start) * 1000)
                    rows.append(row)
                    log.write(json.dumps(row) + '\n')
                    log.flush()
            print(json.dumps({'order': order, 'requests': len(rows)}), flush=True)
    report = summarize(rows)
    report['provenance'] = dict(manifest_sha256=hashlib.sha256(frozen).hexdigest(), checkpoint=args.checkpoint,
                                scope='Exploratory transfer test; no fine-tuning; hand-authored labels')
    (args.output / 'summary.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({arm: report[arm]['all'] for arm in ('laya_local', 'laya_typed', 'jev')}))


if __name__ == '__main__':
    main()
