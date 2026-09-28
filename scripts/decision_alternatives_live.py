"""Matched paid Amplifier/Forge experiments for avoided generation and retrieval.

Two repetitions, alternating order; identical task/model/effort per pair.
Uses disposable public fixtures, native receipts, independent battery graders.
Jevgrep charges are unknown and never counted as zero. No global settings change.
"""
from pathlib import Path
import argparse
import sys
import json
import hashlib
import yaml
import forge_e2e as f
import battery_tasks as b


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument('--prepare-only', action='store_true')
    ap.add_argument('--retrieval-only', action='store_true',
                    help='Four semantic-retrieval runs, explicitly exercising the added tool')
    ap.add_argument('--laya-only', action='store_true',
                    help='Eight matched plain/Laya runs after local server repair')
    ap.add_argument('--composed-smoke', action='store_true',
                    help='One acceptance run through the default composed bundle')
    args = ap.parse_args()
    source = args.source.resolve(); root = args.output.resolve()
    common = {'source_root': str(source), 'mode': 'active'}
    decision = {'model': 'jev-latest', 'read_shortcut': False, 'model_routing': None, 'effort_routing': {},
                'timeout_ms': 3000, 'step_actions': {'prepared': True},
                'cache_keepalive': {'enabled': False}, 'waste_guards': {'enabled': False}}
    sides = {'plain': {'source_root': str(source), 'mode': 'off'},
             'jev': {**common, 'decision_overrides': {**decision, 'backend': 'jev', 'allow_external_state': True}},
             'laya': {**common, 'decision_overrides': {**decision, 'backend': 'laya', 'allow_external_state': False}},
             'retrieval': {'source_root': str(source), 'mode': 'off'}}
    runs = []
    for task in ['answer_audit_log_key', 'repair_parse_duration']:
        for rep in [1, 2]:
            for side in (['plain', 'jev', 'laya'] if rep == 1 else ['laya', 'jev', 'plain']):
                runs.append({'name': f'{task}-{side}-{rep}', 'task': task, 'side': side,
                             'rep': rep, 'seed': 928+rep, 'prompt': b.TASKS[task].prompt})
    for rep in [1, 2]:
        task = 'answer_sqlite_import_module'
        prompt = ('Read README.md, locate the implementation needed to answer its question, and answer precisely. '
                  'Use jevgrep for unfamiliar behavior if available; use direct reads or grep for exact symbols. '
                  'Work only inside this public fixture directory. Source sharing through jevgrep is authorized. '
                  'Do not edit files or delegate. Finish with ANSWER: followed by the exact answer.')
        for side in (['plain', 'retrieval'] if rep == 1 else ['retrieval', 'plain']):
            runs.append({'name': f'{task}-{side}-{rep}', 'task': task, 'side': side,
                         'rep': rep, 'seed': 928+rep, 'prompt': prompt})
    if args.retrieval_only:
        runs = []
        task = 'answer_audit_log_key'
        prompt = ('Read README.md and answer its audit-log-key question accurately. '
                  'You may choose only the relevant files instead of following its read-every-file direction. '
                  'If jevgrep is available, use it once to locate the code responsible for constructing and storing '
                  'the checkout audit key; otherwise locate that behavior with ordinary tools. '
                  'Work only in this public fixture; source sharing through jevgrep is authorized. '
                  'Do not edit or delegate. Finish with ANSWER: followed by the exact key.')
        for rep in [1, 2]:
            for side in (['plain', 'retrieval'] if rep == 1 else ['retrieval', 'plain']):
                runs.append({'name': f'{task}-{side}-{rep}', 'task': task, 'side': side,
                             'rep': rep, 'seed': 928+rep, 'prompt': prompt})
    elif args.laya_only:
        runs = [r for r in runs if r['side'] in {'plain', 'laya'}
                and r['task'] != 'answer_sqlite_import_module']
    elif args.composed_smoke:
        sides['composed'] = {**common, 'composition': 'composed'}
        runs = [{'name': 'default-retrieval-acceptance', 'task': 'answer_audit_log_key',
                 'side': 'composed', 'rep': 1,
                 'prompt': 'Read README.md. Use jevgrep once with the absolute workspace path to locate '
                 'the code constructing and storing the checkout audit key. Read the relevant files '
                 'and answer accurately. This is a public fixture; source sharing is authorized. '
                 'Do not edit or delegate. Finish with ANSWER: followed by the exact key.'}]
    if not (root / 'manifest.json').exists():
        manifest = f.prepare(root, {'runs': runs, 'sides': sides,
            'provider': 'anthropic', 'model': 'claude-sonnet-5', 'amplifier_bundle': 'lean',
            'host_python': sys.executable, 'events_dir': str(root/'events'),
            'forge_py': str(Path.home()/'.agents/skills/amplifier-skill-forge/tools/forge.py'),
            'limits': {'timeout_seconds': 180, 'max_iterations': 16, 'extended_thinking': False}})
        for name, item in manifest['runs'].items():
            if item['side'] != 'retrieval': continue
            path = root / f._slug(name) / 'profile.md'
            profile = yaml.safe_load(path.read_text().split('---')[1])
            profile['tools'].append({'module': 'tool-jevgrep',
                'source': (source/'modules/tool-jevgrep').as_uri(),
                'config': {'root': str(root/f._slug(name)/'workspace'), 'allow_external_state': True}})
            path.write_text('---\n'+yaml.safe_dump(profile,sort_keys=False)+'---\n'+(source/'context/jevgrep.md').read_text())
            item['profile_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        f.dump(root/'manifest.json', manifest)
    if not args.prepare_only: f.batch(root)

if __name__ == '__main__': main()
