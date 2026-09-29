#!/usr/bin/env python3
"""Run public Laya fixtures via the installed portable CLI, from any harness.

This checks invocation and retrieval; abstention is a valid selection/CUA result.
It neither drives a desktop nor demonstrates avoided provider calls.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--capability', choices=['all', 'select', 'search', 'cua'], default='all')
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    source = root / 'source'
    source.mkdir(exist_ok=True)
    fixtures = {
        'retry.py': 'def retry_on_timeout(operation):\n    for attempt in range(3):\n        try:\n            return operation()\n        except TimeoutError:\n            if attempt == 2:\n                raise\n',
        'colors.py': 'COLORS = ["red", "green", "blue"]\n',
        'README.md': '# Retry fixture\nRetries operations after a timeout.\n',
    }
    for name, content in fixtures.items():
        path = source / name
        if path.exists() and path.read_text() != content:
            parser.error('Use a new output directory; fixture content differs.')
        path.write_text(content)
    payloads = {
        'select': {'task': 'Read README.md.', 'candidates': [
            {'id': 'readme', 'operation': 'read', 'path': 'README.md'}]},
        'search': {'query': 'Where are operations retried after a timeout?'},
        'cua': {'goal': 'Open the Weekly report', 'snapshot': {
            'surface_id': 'public-report-fixture', 'revision': '1', 'text': 'Reports',
            'elements': [{'id': name.lower(), 'label': name, 'operations': ['CLICK']}
                         for name in ['Weekly', 'Monthly']]}},
    }
    capabilities = payloads if args.capability == 'all' else [args.capability]
    failed = False
    for cap in capabilities:
        command = ['amplifier-fast-decisions', cap, '--backend', 'laya', '--input', '-']
        command += ['--root', str(source)] if cap == 'search' else []
        command += ['--events', str(root / 'events')] if cap == 'select' else []
        started = time.perf_counter()
        run = subprocess.run(command, input=json.dumps(payloads[cap]), text=True,
                             capture_output=True, timeout=70,
                             env={**os.environ, 'AFAST_TRAFFIC': 'test'})
        result = json.loads(run.stdout) if run.stdout.strip() else {}
        valid = run.returncode == 0 and result.get('backend') == 'laya' and bool(result.get('model'))
        if cap == 'search':
            valid &= result.get('status') == 'complete' and any(
                match['path'] == 'retry.py' for match in result.get('matches', []))
        record = {'capability': cap, 'exit_code': run.returncode, 'verified': valid,
                  'wall_ms': round((time.perf_counter() - started) * 1000, 2), 'result': result}
        (root / f'{cap}-result.json').write_text(json.dumps(record, indent=2) + '\n')
        print(json.dumps(record))
        failed |= not valid
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
