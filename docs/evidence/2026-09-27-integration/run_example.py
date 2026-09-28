"""Repeat the eight-session paid Amplifier/Forge example on fresh workspaces.

Run with Amplifier's Python. Requires configured Anthropic and TypeSafe credentials,
Forge, and an installed Amplifier CLI. Output must be a new directory. For exact
implementation reproduction pass a detached checkout of the recorded source SHA.
"""
from pathlib import Path
import argparse
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[3])
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
source = args.source.expanduser().resolve()
root = args.output.expanduser().resolve()
sys.path.insert(0, str(source / 'scripts'))
import forge_e2e as f
import battery_tasks

runs = []
for rep in range(1, 5):
    for side in (['baseline', 'fast'] if rep % 2 else ['fast', 'baseline']):
        runs.append({'name': f'{side}-{rep}', 'task': 'repair_parse_duration',
                     'side': side, 'rep': rep, 'seed': 20260927 + rep, 'block': rep,
                     'prompt': battery_tasks.TASKS['repair_parse_duration'].prompt})
f.prepare(root, {
    'runs': runs,
    'sides': {'baseline': {'source_root': str(source), 'mode': 'off'},
              'fast': {'source_root': str(source), 'mode': 'active',
                       'composition': 'composed', 'decision_overrides': {}}},
    'provider': 'anthropic', 'model': 'claude-fable-5-1',
    'amplifier_bundle': 'foundation', 'host_python': sys.executable,
    'forge_py': str(Path.home() / '.agents/skills/amplifier-skill-forge/tools/forge.py'),
    'limits': {'timeout_seconds': 300, 'max_iterations': 20, 'extended_thinking': True},
})
f.batch(root)
