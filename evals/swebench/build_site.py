#!/usr/bin/env python3
"""Export an allowlisted experiment website, optionally serving live local data."""
from __future__ import annotations
import argparse
from collections import defaultdict
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import shutil
import statistics

import forge_swebench as campaign

HERE = Path(__file__).resolve().parent
EVIDENCE = HERE.parents[1]/'docs/evidence/2026-09-28-decisions'
LABELS = {'plain-matched': 'Plain Amplifier', 'jev-prepared': 'Jev prepared actions',
          'laya-prepared': 'Laya prepared actions', 'jevgrep': 'Jevgrep retrieval'}


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def finite(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def example_data():
    examples = []
    specs = [('results', 'answer_audit_log_key', 'jev', 'Follow code to an audit key',
              'One prepared read bypassed a model call in each run. This is a small fixture, not a SWE-bench result.'),
             ('results', 'repair_parse_duration', 'jev', 'Repair a duration parser',
              'A bypass occurred in each run, but the later agent trajectory used more calls and took longer.'),
             ('retrieval-results', 'answer_audit_log_key', 'retrieval', 'Find audit behavior with Jevgrep',
              'Both retrieval runs used Jevgrep. Its usage was not metered in this older experiment, so total dollar savings are unknown.'),
             ('laya-results', 'repair_parse_duration', 'laya', 'Repair with the local Laya judge',
              'No Laya choice cleared the unchanged confidence gates. Zero calls were bypassed; the timing difference is not attributed to Laya.')]
    for source, task, arm, title, note in specs:
        data = read_json(EVIDENCE/(source+'.json'), {})
        selected = [r for r in data.get('rows', []) if r['task'] == task and r['arm'] in {'plain', arm}]
        groups = []
        for name in ['plain', arm]:
            rows = [r for r in selected if r['arm'] == name]
            if not rows:
                continue
            costs = [finite(r.get('total_estimated_cost_usd')) for r in rows]
            groups.append({'arm': name, 'runs': len(rows), 'passed': sum(r['passed'] for r in rows),
                'seconds': statistics.mean(r['wall_seconds'] for r in rows),
                'calls': statistics.mean(r['provider_calls'] for r in rows),
                'cost': statistics.mean(costs) if all(c is not None for c in costs) else None,
                'bypassed': sum(r['calls_bypassed'] for r in rows)})
        examples.append({'title': title, 'note': note, 'groups': groups, 'source': source+'.json'})
    return examples


def payload(root):
    root = Path(root)
    manifest = read_json(root/'manifest.json', {})
    status = read_json(root/'campaign-status.json', {})
    if status.get('status') in {'running', 'grading'}:
        process = read_json(root/'controller-process.json', {})
        if process.get('pid'):
            try:
                os.kill(process['pid'], 0)
            except ProcessLookupError:
                status['status'] = 'needs_reconciliation'
    results = {r['name']: r for r in campaign._report_rows(root, manifest)}
    issues = defaultdict(dict)
    arm_rows = defaultdict(list)
    for name, spec in manifest.get('runs', {}).items():
        result = results.get(name)
        state = 'pending'
        if result:
            state = ('error' if result.get('infrastructure_failure') else
                     'resolved' if result.get('resolved') is True else
                     'unresolved' if result.get('resolved') is False else 'ungraded')
        elif status.get('current_run') == name and status.get('status') == 'running':
            state = 'running'
        row = {'arm': spec['arm'], 'rep': spec['rep'], 'state': state,
               'seconds': finite((result or {}).get('wall_time_ms')),
               'cost': finite((result or {}).get('cost_usd')),
               'provider_calls': finite(((result or {}).get('native') or {}).get('provider_responses')),
               'judge_scored': ((result or {}).get('mechanisms') or {}).get('judge_scored', 0),
               'fast_submissions': ((result or {}).get('mechanisms') or {}).get('fast_route_submissions', 0)}
        entry = campaign.budget_accounting.account(root, manifest, result) if result else None
        row['known_cost'] = entry['known_usd'] if entry else row['cost']
        row['budget_hold'] = entry['held_usd'] if entry else 0
        if row['seconds'] is not None:
            row['seconds'] /= 1000
        issues[spec['instance_id']].setdefault(spec['arm'], []).append(row)
        arm_rows[spec['arm']].append(row)
    arms = []
    for arm in manifest.get('arms', {}):
        rows = arm_rows[arm]
        done = [r for r in rows if r['state'] not in {'pending', 'running'}]
        costs = [r['cost'] for r in done]
        arms.append({'id': arm, 'label': LABELS.get(arm, arm), 'planned': len(rows),
            'completed': len(done), 'graded': sum(r['state'] in {'resolved','unresolved'} for r in rows),
            'resolved': sum(r['state'] == 'resolved' for r in rows),
            'errors': sum(r['state'] == 'error' for r in rows),
            'known_cost': sum(r['known_cost'] for r in done if r['known_cost'] is not None),
            'unknown_cost': sum(c is None for c in costs),
            'total_cost': sum(costs) if costs and all(c is not None for c in costs) else None,
            'median_seconds': statistics.median([r['seconds'] for r in done if r['seconds'] is not None])
                if any(r['seconds'] is not None for r in done) else None})
    order = list(LABELS)
    arms.sort(key=lambda r: order.index(r['id']) if r['id'] in order else len(order))
    judges = read_json(EVIDENCE/'judges.json', {}).get('summary', {})
    complete = bool(arms) and all(a['graded'] == a['planned'] for a in arms)
    comparisons = []
    for arm in LABELS:
        if arm == 'plain-matched':
            continue
        time_ratios, cost_ratios, paired_grades = [], [], []
        for issue in issues.values():
            base = {r['rep']: r for r in issue.get('plain-matched', [])}
            for row in issue.get(arm, []):
                other = base.get(row['rep'])
                if not other or any(r['state'] in {'pending','running'} for r in [row,other]):
                    continue
                if row['seconds'] and other['seconds']:
                    time_ratios.append(row['seconds']/other['seconds'])
                if row['cost'] and other['cost']:
                    cost_ratios.append(row['cost']/other['cost'])
                if all(r['state'] in {'resolved','unresolved'} for r in [row,other]):
                    paired_grades.append((row['state']=='resolved',other['state']=='resolved'))
        comparisons.append({'arm':arm,'time_pairs':len(time_ratios),'cost_pairs':len(cost_ratios),
            'time_ratio':math.exp(statistics.mean(map(math.log,time_ratios))) if time_ratios else None,
            'cost_ratio':math.exp(statistics.mean(map(math.log,cost_ratios))) if cost_ratios else None,
            'graded_pairs':len(paired_grades), 'resolution_delta':sum(a-b for a,b in paired_grades)})
    return {'schema': 'fast-decisions-public-study-v1',
        'updated': datetime.now(timezone.utc).isoformat(),
        'status': status.get('status', 'prepared'), 'complete': complete,
        'cap': finite(status.get('cap_usd')), 'setup_cost': finite(manifest.get('prior_cost_usd',0)),
        'budget_hold': sum(r['budget_hold'] for rows in arm_rows.values() for r in rows),
        'comparisons': comparisons, 'dataset': manifest.get('dataset'),
        'dataset_revision': manifest.get('dataset_revision'), 'candidate': manifest.get('candidate_sha'),
        'model': next(iter(manifest.get('arms', {}).values()), {}).get('model'),
        'arms': arms, 'issues': [{'id': iid, 'arms': rows} for iid, rows in sorted(issues.items())],
        'examples': example_data(),
        'judges': {name: {key: finite(values.get(key)) for key in ['correct','total','median_ms']}
                   for name, values in judges.items()}}


def build(root, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    for name in ['index.html', 'styles.css', 'app.js']:
        shutil.copyfile(HERE/'site'/name, output/name)
    (output/'data.json').write_text(json.dumps(payload(root), allow_nan=False)+'\n')
    evidence = output/'evidence'
    evidence.mkdir(exist_ok=True)
    for name in ['results.json','retrieval-results.json','laya-results.json','judges.json','REPORT.md','method.md']:
        # These files are already reviewed, committed public experiment evidence.
        shutil.copyfile(EVIDENCE/name, evidence/name)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--port', type=int, default=52114)
    args = parser.parse_args()
    output = build(args.root, args.output)
    if not args.serve:
        print(output/'index.html')
        return
    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            route = self.path.split('?', 1)[0]
            if route == '/data.json':
                try:
                    body = json.dumps(payload(args.root), allow_nan=False).encode()
                except (OSError, ValueError, KeyError):
                    self.send_error(503, 'Study data is temporarily unavailable')
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif route in {'/','/index.html','/styles.css','/app.js'} or route in {
                '/evidence/'+n for n in ['results.json','retrieval-results.json','laya-results.json',
                                         'judges.json','REPORT.md','method.md']}:
                super().do_GET()
            else:
                self.send_error(404)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', args.port), partial(Handler, directory=str(output)))
    print(f'http://127.0.0.1:{args.port}/', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
