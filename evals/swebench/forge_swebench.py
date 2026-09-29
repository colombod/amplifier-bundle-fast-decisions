#!/usr/bin/env python3
"""SWE-bench Verified through Forge: plain Amplifier vs the orchestrator-primary bundle.

A lighter path than the DTU scaffold in this directory (run.sh): every agent
run is an ordinary `amplifier run` in a real checkout of the instance's repo at
its base commit, driven in its own Forge PTY. The patch is `git diff` against
the base commit. Grading is the official `swebench.harness.run_evaluation`
(swebench 4.x, Docker), so "resolved" means exactly what it means on the SWE-bench
leaderboard.

Arms (profiles built by scripts/forge_e2e.py, same as the S1/S2 battery):
  plain          foundation + loop-streaming, --model claude-fable-5-1
  plain-sonnet   same, --model claude-sonnet-5   (model-confound control)
  orch-primary   the bundle root composed as shipped (orchestrator swap,
                 routing-only default), --model claude-fable-5-1

Usage:
  forge_swebench.py prepare --root R --instances ID[,ID...] --candidate-sha SHA \\
      --baseline-source DIR [--arms plain,plain-sonnet,orch-primary] [--reps 1] [--seed N]
  forge_swebench.py run    --root R [--parallel 3]
  forge_swebench.py grade  --root R [--swe-python PATH]
  forge_swebench.py report --root R

Explicit opt-in: `run` spends provider money, `grade` builds Docker images.
Run output (prompts, patches, traces) stays under --root; never commit it.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.metadata
import importlib.util
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import random
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import yaml
import budget_accounting

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT/'scripts'))
import forge_e2e  # noqa: E402

DATASET = 'princeton-nlp/SWE-bench_Verified'
DATASETS = {'verified': DATASET, 'full': 'princeton-nlp/SWE-bench'}
DEFAULT_SWE_PYTHON = Path.home()/'dev/afast-ev/swe-venv/bin/python'
MIRRORS = Path.home()/'dev/afast-ev/swe-mirrors'
ARMS = {
    'plain': {'model': 'claude-fable-5-1', 'composed': False},
    'plain-sonnet': {'model': 'claude-sonnet-5', 'composed': False},
    'orch-primary': {'model': 'claude-fable-5-1', 'composed': True},
    # Cache-aware effort (STUDY-DESIGN.md 18.4): the shipped config plus one change.
    'orch-primary-monotonic': {'model': 'claude-fable-5-1', 'composed': True,
                               'overrides': {'effort_routing': {'monotonic': True}}},
    # Turn-start difficulty router (STUDY-DESIGN.md 18.7): one typed simple/complex
    # question per turn picks the start tier; complex turns stay on the host model.
    'orch-router-jev': {'model': 'claude-fable-5-1', 'composed': True, 'overrides': {
        'backend': 'jev', 'model': 'jev-1.13.0', 'allow_external_state': True, 'read_shortcut': False,
        'timeout_ms': 3000, 'effort_routing': {'by_tier': {'cheap': 'medium', 'strong': None}},
        'model_routing': {'start_policy': 'judge'}}},
    # The shipped default: router on, no judge configured (prompt-length rule).
    'orch-router-rules': {'model': 'claude-fable-5-1', 'composed': True, 'overrides': {}},
    # Complex-task speed lever (STUDY-DESIGN.md 18.9): the shipped default, but the
    # strong tier keeps phase effort (orient medium / explore low / implement high)
    # with the monotonic hold -- same model as plain, at most one cache step-up.
    'orch-strong-phase-effort': {'model': 'claude-fable-5-1', 'composed': True, 'overrides': {
        'effort_routing': {'by_tier': {'strong': 'phase'}, 'monotonic': True}}},
    'orch-router-local': {'model': 'claude-fable-5-1', 'composed': True, 'overrides': {
        'backend': 'ollama', 'model': 'qwen:latest', 'read_shortcut': False,
        'timeout_ms': 8000, 'effort_routing': {'by_tier': {'cheap': 'medium', 'strong': None}},
        'model_routing': {'start_policy': 'judge'}}},
}
MATCHED_DECISION = {
    'model': 'jev-1.13.0', 'read_shortcut': False, 'model_routing': None,
    'effort_routing': {}, 'timeout_ms': 3000, 'step_actions': {'prepared': True},
    'cache_keepalive': {'enabled': False}, 'waste_guards': {'enabled': False},
}
ARMS.update({
    'plain-matched': {'model': 'claude-sonnet-5', 'composed': False, 'matched': True},
    'jev-prepared': {'model': 'claude-sonnet-5', 'composed': False, 'matched': True,
                     'active': True, 'overrides': {**MATCHED_DECISION, 'backend': 'jev', 'allow_external_state': True}},
    'laya-prepared': {'model': 'claude-sonnet-5', 'composed': False, 'matched': True,
                      'active': True, 'overrides': {**MATCHED_DECISION, 'backend': 'laya', 'allow_external_state': False}},
    'jevgrep': {'model': 'claude-sonnet-5', 'composed': False, 'matched': True, 'retrieval': True},
})
PROMPT = """You are working in a git checkout of the {repo} repository (your current directory).
Resolve the GitHub issue below by editing the repository's source code.

The project's Python environment (dependencies installed, repo mounted at /testbed) runs in Docker.
Run any Python/test command through the helper, from this directory:  .swe/run <command>
  e.g.  .swe/run python -m pytest path/to/test_file.py -x -q
Do not install dependencies or search outside this directory for an environment.

Rules:
- Work directly in this checkout; do not delegate to sub-agents and do not use the network.
- Make the minimal, correct source change that resolves the issue. You may add or run tests to check your work,
  but the fix must be in the library source.
- Do not commit; leave your changes in the working tree.
- When done, reply with a short summary of the change.

<issue>
{problem_statement}
</issue>
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _host_fingerprint():
    values = {}
    for distribution, module in [('amplifier-app-cli', 'amplifier_app_cli'),
                                 ('amplifier-core', 'amplifier_core'),
                                 ('amplifier-foundation', 'amplifier_foundation'),
                                 ('amplifier-module-provider-anthropic', 'amplifier_module_provider_anthropic'),
                                 ('amplifier-module-loop-streaming', 'amplifier_module_loop_streaming')]:
        try:
            spec = importlib.util.find_spec(module)
            values[distribution] = {'version': importlib.metadata.version(distribution),
                'source_hash': forge_e2e.tree_sha256(Path(spec.origin).parent) if spec and spec.origin else None}
        except (ImportError, importlib.metadata.PackageNotFoundError):
            values[distribution] = None
    return values


def _dump(path, value):
    path = Path(path)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def _docker_env(manifest):
    env = dict(os.environ)
    if manifest.get('docker_host'):
        env.pop('DOCKER_CONTEXT', None)
        env['DOCKER_HOST'] = manifest['docker_host']
    return env


def _git(*args, cwd=None, check=True):
    return subprocess.run(['git', *args], cwd=cwd, check=check, capture_output=True, text=True)


def _load_instances(ids, swe_python, dataset=DATASET, revision=None):
    script = (
        "import json,sys\n"
        "from datasets import load_dataset\n"
        f"ds=load_dataset({dataset!r}, revision={revision!r}, split='test')\n"
        "want=set(sys.argv[1].split(','))\n"
        "keep=['instance_id','repo','base_commit','problem_statement','version']\n"
        "print(json.dumps([{k:r[k] for k in keep} for r in ds if 'all' in want or r['instance_id'] in want]))\n"
    )
    out = subprocess.run([str(swe_python), '-c', script, ','.join(ids)], check=True, capture_output=True, text=True)
    rows = {r['instance_id']: r for r in json.loads(out.stdout)}
    if ids == ['all']:
        return [rows[i] for i in sorted(rows)]
    missing = [i for i in ids if i not in rows]
    if missing:
        raise SystemExit(f'instances not in {dataset}: {missing}')
    return [rows[i] for i in ids]


def _mirror(repo):
    MIRRORS.mkdir(parents=True, exist_ok=True)
    path = MIRRORS/(repo.replace('/', '__') + '.git')
    if not path.exists():
        _git('clone', '--mirror', '--quiet', f'https://github.com/{repo}.git', str(path))
    return path


def _build_workspace(run_dir, instance):
    workspace = run_dir/'workspace'
    _git('clone', '--quiet', '--shared', '--no-checkout', str(_mirror(instance['repo'])), str(workspace))
    _git('checkout', '--quiet', '--detach', instance['base_commit'], cwd=workspace)
    _git('config', 'user.email', 'bench@example.invalid', cwd=workspace)
    _git('config', 'user.name', 'bench', cwd=workspace)
    # Same isolation as forge_e2e._build_workspace: no user app bundles.
    (workspace/'.amplifier').mkdir(exist_ok=True)
    (workspace/'.amplifier/settings.local.yaml').write_text('bundle:\n  app: []\n')
    exclude = workspace/'.git/info/exclude'
    exclude.write_text(exclude.read_text() + '\n.amplifier/\n.swe/\n')
    helper = workspace/'.swe'/'run'
    helper.parent.mkdir()
    helper.write_text(
        '#!/bin/sh\n'
        '# Runs a command in the SWE-bench instance image (the grading environment), with this\n'
        '# checkout mounted at /testbed. Generated by evals/swebench/forge_swebench.py.\n'
        'cd "$(dirname "$0")/.." || exit 1\n'
        f'exec docker run --rm --platform linux/amd64 -v "$PWD":/testbed -w /testbed {instance_image(instance)} '
        'bash -lc \'source /opt/miniconda3/bin/activate testbed && '
        'if [ $# -eq 1 ]; then eval "$1"; else "$@"; fi\' _ "$@"\n')
    helper.chmod(0o755)
    return workspace


def instance_image(instance):
    """The official SWE-bench evaluation image for an instance (x86_64; runs emulated on arm64)."""
    iid = instance['instance_id'].replace('__', '_1776_').lower()
    return f'swebench/sweb.eval.x86_64.{iid}:latest'


def _pull_image(instance, env=None):
    subprocess.run(['docker', 'pull', '--platform', 'linux/amd64', '-q', instance_image(instance)],
                   check=True, capture_output=True, text=True, env=env)


def _arm_profile(name, spec, source, baseline, instance, workspace, config):
    """Keep controls free of the product root's transitive default tools."""
    if spec['composed']:
        side = {'source_root': str(source), 'mode': 'active', 'composition': 'composed',
                'decision_overrides': dict(spec.get('overrides') or {})}
    else:
        side = {'source_root': str(source if spec.get('matched') else baseline),
                'mode': 'active' if spec.get('active') else 'off',
                'decision_overrides': dict(spec.get('overrides') or {})}
    profile = forge_e2e._side_profile(name, side, instance['instance_id'], workspace, config)
    if not spec['composed']:
        # _side_profile's foundation mode includes the product root. Since
        # retrieval became a default that silently enabled it in plain runs.
        profile['includes'] = [{'bundle': config.get('foundation_source',
            'git+https://github.com/microsoft/amplifier-foundation@main')}]
    if spec.get('retrieval'):
        profile['tools'].append({'module': 'tool-jevgrep',
            'source': (source/'modules/tool-jevgrep').as_uri(),
            'config': {'root': str(workspace), 'allow_external_state': True}})
    return profile, side


def _metered_jevgrep(run_dir):
    executable = shutil.which('jg')
    if not executable:
        raise SystemExit('Install the pinned Jevgrep CLI before preparing retrieval runs')
    meter = run_dir.parent.parent/'tools/jevgrep_usage.mjs'
    if not meter.exists():
        meter.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(__file__).with_name('jevgrep_usage.mjs'), meter)
    wrapper = run_dir/'jg-metered'
    wrapper.write_text('#!/bin/sh\n'
        + 'export FD_JEVGREP_USAGE_FILE=' + shlex.quote(str(run_dir/'jevgrep-usage.jsonl')) + '\n'
        + 'export NODE_OPTIONS=' + shlex.quote('--import=' + json.dumps(str(meter))) + '\n'
        + 'exec ' + shlex.quote(executable) + ' "$@"\n')
    wrapper.chmod(0o700)
    return str(wrapper)


def _retrieval_cost(path):
    """Missing/malformed usage is unknown, never free."""
    if not path.exists():
        return None
    try:
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        if not rows or any(type(r.get('input_tokens')) is not int or r['input_tokens'] < 0 for r in rows):
            return None
        return sum(r['input_tokens'] for r in rows) * 0.042 / 1_000_000
    except (ValueError, TypeError, KeyError):
        return None


def _judge_cost(directory, session_id):
    total = 0.0
    for path in directory.glob('*.jsonl'):
        for line in path.read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                return None
            if event.get('session_id') != session_id:
                continue
            data = event.get('data') or {}
            if data.get('backend') != 'jev':
                continue
            if event.get('event') == 'fast_decisions:scored':
                tokens = data.get('input_tokens')
                if type(tokens) is not int or tokens < 0:
                    return None
                total += tokens * 0.042 / 1_000_000
            elif data.get('reason_code') in {'backend_error', 'decision_timeout'}:
                return None
    return total


def _mechanism_counts(directory, session_id):
    counts = defaultdict(int)
    for path in directory.glob('*.jsonl'):
        for line in path.read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('session_id') != session_id:
                continue
            kind, data = event.get('event'), event.get('data') or {}
            if kind == 'fast_decisions:scored':
                counts['judge_scored'] += 1
            if kind == 'fast_decisions:routed' and data.get('route') == 'fast':
                counts['fast_route_submissions'] += 1
            if kind == 'fast_decisions:tool_end':
                counts['fast_tool_completions'] += 1
                counts['fast_tool_failures'] += data.get('success') is False
    return dict(counts)


def cmd_prepare(args):
    prior_cost = getattr(args, 'prior_cost_usd', 0.0)
    if not math.isfinite(prior_cost) or prior_cost < 0:
        raise SystemExit('Prior cost must be finite and nonnegative')
    root = Path(args.root).expanduser().resolve()
    if (root/'manifest.json').exists():
        raise SystemExit(f'{root} already prepared')
    root.mkdir(parents=True, exist_ok=True)
    ids = [i.strip() for i in args.instances.split(',') if i.strip()]
    arms = [a.strip() for a in args.arms.split(',') if a.strip()]
    if not arms or len(set(arms)) != len(arms) or args.reps < 1:
        raise SystemExit('Choose distinct arms and at least one repetition')
    for arm in arms:
        if arm not in ARMS:
            raise SystemExit(f'unknown arm {arm!r}; choose from {sorted(ARMS)}')
    dataset = DATASETS[getattr(args, 'dataset', 'verified')]
    revision = getattr(args, 'dataset_revision', None)
    if ids == ['all'] and not revision:
        raise SystemExit('--instances all requires --dataset-revision to freeze the dataset')
    instances = _load_instances(ids, args.swe_python, dataset, revision)
    ids = [i['instance_id'] for i in instances]
    _dump(root/'instances.json', instances)
    lazy = getattr(args, 'lazy', False)
    if not lazy:
        for inst in instances:
            _pull_image(inst, _docker_env({'docker_host': getattr(args, 'docker_host', None)}))

    source = root/'source'
    if not source.exists():
        _git('-C', str(REPO_ROOT), 'worktree', 'add', '--detach', str(source), args.candidate_sha)
    baseline = Path(args.baseline_source).expanduser().resolve()

    rng = random.Random(args.seed)
    schedule = []
    for rep in range(1, args.reps + 1):
        block = list(instances)
        rng.shuffle(block)
        for inst in block:
            order = list(arms)
            rng.shuffle(order)
            schedule += [(inst, arm, rep) for arm in order]

    limits = {'max_iterations': args.max_iterations, 'extended_thinking': True}
    runs = {}
    for inst, arm, rep in schedule:
        name = forge_e2e._slug(f"{inst['instance_id']}__{arm}__r{rep}")
        run_dir = root/'runs'/name
        run_dir.mkdir(parents=True)
        workspace = run_dir/'workspace' if lazy else _build_workspace(run_dir, inst)
        spec = ARMS[arm]
        config = {'limits': limits, 'events_dir': str(run_dir/'events'),
                  'upstream_loop_source': forge_e2e.UPSTREAM_LOOP_SOURCE,
                  'foundation_source': getattr(args, 'foundation_source',
                      'git+https://github.com/microsoft/amplifier-foundation@main')}
        profile, side = _arm_profile(name, spec, source, baseline, inst, workspace, config)
        if spec.get('retrieval'):
            next(t for t in profile['tools'] if t['module'] == 'tool-jevgrep')['config']['executable'] = _metered_jevgrep(run_dir)
        forge_e2e._assert_bundle_uri_safe(run_dir/'profile.md')
        (run_dir/'profile.md').write_text('---\n' + json.dumps(profile, indent=2) + '\n---\n')
        if spec['composed']:
            effective = forge_e2e.composed_effective_config(source, profile['session']['orchestrator']['config'])
            if effective is not None:
                _dump(run_dir/'effective-loop-config.json', effective)
        prompt = PROMPT
        if spec.get('matched'):
            prompt = prompt.replace('and do not use the network.',
                'and do not look up external issues, solutions, or benchmark files. '
                'Configured model and source-retrieval calls are allowed. '
                'Use semantic code search if available and helpful; use ordinary grep for known symbols.')
        (run_dir/'prompt.txt').write_text(prompt.format(repo=inst['repo'], problem_statement=inst['problem_statement']))
        runs[name] = {'instance_id': inst['instance_id'], 'repo': inst['repo'], 'base_commit': inst['base_commit'],
                      'arm': arm, 'rep': rep, 'model': spec['model'], 'source_root': side['source_root'],
                      'profile_sha256': hashlib.sha256((run_dir/'profile.md').read_bytes()).hexdigest(),
                      'prompt_sha256': hashlib.sha256((run_dir/'prompt.txt').read_bytes()).hexdigest()}
    manifest = {'schema': 'forge-swebench-v1', 'created_at': _now(), 'dataset': dataset,
                'dataset_revision': revision, 'lazy': lazy,
                'all_instances': args.instances == 'all', 'instance_count': len(instances),
                'docker_host': getattr(args, 'docker_host', None), 'image_platform': 'linux/amd64',
                'foundation_source': config['foundation_source'],
                'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'host_fingerprint': _host_fingerprint(),
                'source_tree_sha256': forge_e2e.tree_sha256(source/'src/amplifier_fast_decisions'),
                'candidate_sha': _git('-C', str(source), 'rev-parse', 'HEAD').stdout.strip(),
                'baseline_source': str(baseline), 'baseline_sha': forge_e2e.git_sha(baseline),
                'upstream_loop_source': forge_e2e.UPSTREAM_LOOP_SOURCE, 'arms': {a: ARMS[a] for a in arms},
                'seed': args.seed, 'reps': args.reps, 'deadline_seconds': args.deadline_seconds,
                'limits': limits, 'settings_sha256': _settings_sha256(),
                'settings_hash_method': 'canonical-yaml-except-update-check-time-v1',
                'prior_cost_usd': getattr(args, 'prior_cost_usd', 0.0),
                'run_order': list(runs), 'runs': runs}
    _dump(root/'manifest.json', manifest)
    print(json.dumps({'prepared': str(root), 'runs': len(runs), 'instances': ids, 'arms': arms}))


def _project_sessions_dir(workspace):
    slug = str(Path(workspace).resolve()).replace('\\', '-').replace('/', '-').replace(':', '')
    return Path.home()/'.amplifier/projects'/slug/'sessions'


_launch_lock = threading.Lock()
SETTINGS = Path.home()/'.amplifier'/'settings.yaml'


def _settings_sha256():
    if not SETTINGS.exists():
        return None
    settings = yaml.safe_load(SETTINGS.read_text()) or {}
    # The CLI writes its automatic update-check timestamp when a session starts.
    # It cannot affect a resolved profile; retain every other setting in the hash.
    if isinstance(settings.get('updates'), dict):
        settings['updates'].pop('last_check', None)
    return hashlib.sha256(json.dumps(settings, sort_keys=True, default=str).encode()).hexdigest()


def _capture_patch(workspace, base_commit, index_path):
    """`git diff` of the working tree (tracked + untracked, minus excludes) against the
    base commit, through a private index file: never touches the workspace's own index,
    so a stale .git/index.lock left by the agent cannot silently yield an empty patch."""
    env = dict(os.environ, GIT_INDEX_FILE=str(index_path))
    for cmd in (['read-tree', base_commit], ['add', '-A']):
        proc = subprocess.run(['git', *cmd], cwd=workspace, env=env, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f'git {cmd[0]} failed: {proc.stderr.strip()[:300]}')
    return subprocess.run(['git', 'diff', '--cached', base_commit], cwd=workspace, env=env,
                          check=True, capture_output=True, text=True).stdout


def cmd_agent(args):
    """Worker: runs ONE agent session to completion inside a Forge PTY, then writes result.json.

    Forge's run_command observation returns after ~60 s while keeping the process alive
    (forge_e2e.launch_run relies on the same behavior), so completion is signalled by
    result.json, never by the observation."""
    root = Path(args.root).resolve()
    manifest = json.loads((root/'manifest.json').read_text())
    name = args.name
    item = manifest['runs'][name]
    run_dir = root/'runs'/name
    workspace = run_dir/'workspace'
    sessions = _project_sessions_dir(workspace)
    before = set(sessions.iterdir()) if sessions.exists() else set()
    deadline = manifest['deadline_seconds']
    argv = ['amplifier', 'run', '--bundle', (run_dir/'profile.md').as_uri(), '--mode', 'single',
            '--provider', 'anthropic', '--model', item['model'], '--output-format', 'json',
            (run_dir/'prompt.txt').read_text()]
    started_at, t0 = _now(), time.perf_counter()
    timed_out, exit_code, notes = False, None, []
    with open(run_dir/'amplifier-output.json', 'w') as out, open(run_dir/'amplifier-stderr.txt', 'w') as err:
        # Same isolation as forge_e2e.worker: the side's own frozen source wins over whatever
        # editable install of amplifier_fast_decisions the shared Amplifier tool env holds
        # (activating a file:// module pip-installs it editable there; last install wins).
        env = _docker_env(manifest)
        if item.get('source_root'):
            env['PYTHONPATH'] = str(Path(item['source_root'])/'src')
        # AMPLIFIER_MEMORY_CAPTURE=off: this launches the real `amplifier
        # run` CLI, which inherits the caller's memory bundle install (if
        # any) via os.environ above -- without this it writes a capture
        # after every tool call in every one of these worker runs, into
        # the LAUNCHING USER's personal memory store. Matches forge_e2e.
        # worker's same fix. amplifier-bundle-memory's automation_gate
        # module honors this var; harmless no-op on older/no memory bundle.
        env['AMPLIFIER_MEMORY_CAPTURE'] = 'off'
        env['AFAST_TRAFFIC'] = 'test'
        if manifest.get('arms', {}).get(item.get('arm'), {}).get('retrieval'):
            # A benchmark-local empty config makes the existing environment
            # TypeSafe key the measured provider; saved user providers are untouched.
            config_dir = run_dir/'xdg'
            config_dir.mkdir(mode=0o700, exist_ok=True)
            env['XDG_CONFIG_HOME'] = str(config_dir)
        proc = subprocess.Popen(argv, cwd=workspace, stdout=out, stderr=err, start_new_session=True, env=env)
        try:
            exit_code = proc.wait(timeout=deadline)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                exit_code = proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                exit_code = proc.wait()
    wall_ms = (time.perf_counter() - t0) * 1000
    try:
        patch = _capture_patch(workspace, item['base_commit'], run_dir/'patch.index')
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        patch = ''
        notes.append(f'patch_capture_failed:{exc}'[:300])
    (run_dir/'patch.diff').write_text(patch)
    found = [p for p in sessions.iterdir() if p not in before and (p/'events.jsonl').exists()] if sessions.exists() else []
    native = forge_e2e.native_summary(found[0]) if len(found) == 1 else None
    effort = forge_e2e.effort_summary(found[0]) if len(found) == 1 else []
    exec_ms = _exec_time_ms(found[0]/'events.jsonl') if len(found) == 1 else None
    if len(found) != 1:
        notes.append(f'session_dirs_found:{len(found)}')
    result = {'name': name, **item, 'started_at': started_at, 'ended_at': _now(), 'wall_time_ms': wall_ms,
              'exec_time_ms': exec_ms, 'exit_code': exit_code, 'timed_out': timed_out,
              'session_id': found[0].name if len(found) == 1 else None, 'native': native,
              'models_served': effort, 'cost_usd': ((native or {}).get('usage') or {}).get('cost_usd'),
              'patch_bytes': len(patch.encode()), 'empty_patch': not patch.strip(),
              'infrastructure_failure': any(n.startswith(('patch_capture_failed', 'session_dirs_found')) for n in notes),
              'notes': notes}
    calls = (native or {}).get('tool_names', {}).get('jevgrep', 0)
    result['retrieval_cost_usd'] = _retrieval_cost(run_dir/'jevgrep-usage.jsonl') if calls else 0.0
    result['provider_cost_usd'] = result['cost_usd']
    result['judge_cost_usd'] = _judge_cost(run_dir/'events', result['session_id'])
    result['mechanisms'] = _mechanism_counts(run_dir/'events', result['session_id'])
    costs = [result['provider_cost_usd'], result['retrieval_cost_usd'], result['judge_cost_usd']]
    result['cost_usd'] = sum(costs) if all(c is not None for c in costs) else None
    _dump(run_dir/'result.json', result)


def _launch_worker(forge, command, run_dir):
    """Heal one proven pre-spawn failure; never replay an ambiguous/live worker."""
    for attempt in range(2):
        try:
            obs = forge.call('run_command', {'command': '/bin/zsh', 'args': ['-lc', command],
                'cwd': str(run_dir/'workspace'), 'timeoutMs': 60000})
        except SystemExit as exc:
            text = str(exc).removeprefix('forge: ')
            try: obs = json.loads(text)
            except ValueError: obs = {'launch_error': text[:500]}
        if (attempt == 0 and isinstance(obs, dict) and set(obs) == {'launch_error'}
                and obs['launch_error'] == 'Error: posix_spawnp failed.'
                and not any((run_dir/p).exists() for p in
                            ('amplifier-output.json','amplifier-stderr.txt','result.json','events'))):
            _dump(run_dir/'pre-spawn-repair.json', {'at':_now(),'observation':obs,
                'reason':'No worker spawned; one bounded Forge doctor repair', 'retry_limit':1})
            if forge_e2e.forge_self_heal({'forge_py':str(forge_e2e.FORGE)}):
                continue
        return obs


def _run_one(root, manifest, name, forge):
    run_dir = root/'runs'/name
    for filename, key in [('profile.md', 'profile_sha256'), ('prompt.txt', 'prompt_sha256')]:
        expected = manifest['runs'][name].get(key)
        if expected and hashlib.sha256((run_dir/filename).read_bytes()).hexdigest() != expected:
            raise SystemExit(f'{filename} changed after campaign preparation')
    if manifest.get('runner_sha256') and hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != manifest['runner_sha256']:
        raise SystemExit('Runner changed after campaign preparation')
    if manifest.get('host_fingerprint') and _host_fingerprint() != manifest['host_fingerprint']:
        raise SystemExit('Amplifier host packages changed after campaign preparation')
    if manifest['arms'][manifest['runs'][name]['arm']].get('matched'):
        source = Path(manifest['runs'][name]['source_root'])
        if forge_e2e.tree_sha256(source/'src/amplifier_fast_decisions') != manifest.get('source_tree_sha256'):
            raise SystemExit('Frozen Fast Decisions source changed')
    # STUDY-DESIGN.md 18.5: a mid-campaign change to the user's Amplifier settings
    # (providers, overrides, modules) invalidates every later run; stop, don't continue.
    if 'settings_sha256' in manifest and _settings_sha256() != manifest['settings_sha256']:
        raise SystemExit(f'{SETTINGS} changed since prepare; refusing to launch {name} (STUDY-DESIGN.md 18.5)')
    if manifest.get('lazy'):
        instance = next(i for i in json.loads((root/'instances.json').read_text())
                        if i['instance_id'] == manifest['runs'][name]['instance_id'])
        _pull_image(instance, _docker_env(manifest))
        if not (run_dir/'workspace').exists():
            _build_workspace(run_dir, instance)
    cmd = shlex.join([sys.executable, str(Path(__file__).resolve()), 'agent', '--root', str(root), '--name', name])
    cmd = 'set -a; . ~/.amplifier/keys.env 2>/dev/null; set +a; ' + cmd
    with _launch_lock:  # same spacing discipline as forge_e2e launches
        forge_e2e._wait_for_launch_spacing(time.sleep, time.monotonic)
    obs = _launch_worker(forge, cmd, run_dir)
    _dump(run_dir/'forge-observation.json', {k: v for k, v in (obs or {}).items() if k != 'output'})
    deadline = time.monotonic() + manifest['deadline_seconds'] + 180
    while not (run_dir/'result.json').exists() and time.monotonic() < deadline:
        if (obs or {}).get('launch_error'):
            break
        time.sleep(5)
    if not (run_dir/'result.json').exists():
        _dump(run_dir/'result.json', {'name': name, **manifest['runs'][name], 'infrastructure_failure': True,
                                      'timed_out': True, 'exec_time_ms': None, 'wall_time_ms': None,
                                      'cost_usd': None, 'patch_bytes': 0, 'empty_patch': True,
                                      'notes': ['worker_never_reported', str((obs or {}).get('launch_error', ''))[:300]]})
    if (obs or {}).get('sessionId'):
        try:
            forge.call('close_terminal', {'id': obs['sessionId']})
        except SystemExit:
            pass
    return json.loads((run_dir/'result.json').read_text())


def _exec_time_ms(events_path):
    first = last = None
    for line in events_path.open():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        kind, ts = ev.get('event'), ev.get('ts') or ev.get('timestamp')
        if not isinstance(ts, str):
            continue
        try:
            t = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        except ValueError:
            continue
        if kind == 'llm:request' and first is None:
            first = t
        elif kind == 'llm:response':
            last = t
    if first is None or last is None:
        return None
    return max(0.0, (last - first).total_seconds() * 1000)


def cmd_run(args):
    root = Path(args.root).expanduser().resolve()
    with (root/'controller.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another controller already owns this campaign')
        return _run_campaign(args, root)


def _run_campaign(args, root):
    manifest = json.loads((root/'manifest.json').read_text())
    for filename, expected in manifest.get('harness_file_sha256', {}).items():
        if Path(filename).name != filename or hashlib.sha256(Path(__file__).with_name(filename).read_bytes()).hexdigest() != expected:
            raise SystemExit('Frozen accounting harness changed')
    sys.path.insert(0, str(forge_e2e.FORGE.parent))
    import forge  # type: ignore  # the Forge skill's stdlib helper (same one battery.py loads)
    forge_e2e.forge_self_heal({'forge_py': str(forge_e2e.FORGE)})
    pending = [n for n in manifest['run_order'] if not (root/'runs'/n/'result.json').exists()]
    pending_total = len(pending)
    max_runs = getattr(args, 'max_runs', None)
    if max_runs is not None:
        if max_runs < 1:
            raise SystemExit('--max-runs must be positive')
        pending = pending[:max_runs]
    budget = getattr(args, 'max_cost_usd', None)
    if any(a.get('matched') for a in manifest['arms'].values()) and budget is None:
        raise SystemExit('Matched campaigns require --max-cost-usd before paid runs')
    if budget is not None:
        if not math.isfinite(budget) or budget <= 0 or args.parallel != 1:
            raise SystemExit('Budgeted runs require a positive finite cap and --parallel 1')
        for index, name in enumerate(pending):
            settled = [json.loads(p.read_text()) for p in (root/'runs').glob('*/result.json')]
            try:
                accounting = budget_accounting.ledger(root, manifest, settled)
            except ValueError as exc:
                raise SystemExit(str(exc)) from None
            _dump(root/'budget-ledger.json', accounting)
            if any(r.get('infrastructure_failure') for r in settled):
                raise SystemExit('Infrastructure failure; repair before continuing paid runs')
            spent = accounting['budget_accounted_usd']
            balances = {k:v for k,v in accounting.items() if k != 'runs'}
            reserve = getattr(args, 'reserve_per_run_usd', 10.0)
            if not math.isfinite(reserve) or reserve <= 0:
                raise SystemExit('Per-run reservation must be positive and finite')
            if spent + reserve > budget:
                _dump(root/'campaign-status.json', {'status': 'budget_exhausted', **balances,
                    'cap_usd': budget, 'next_run': name, 'pending': len(pending) - index})
                return
            _dump(root/'campaign-status.json', {'status': 'running', **balances,
                'cap_usd': budget, 'current_run': name, 'pending': len(pending) - index})
            result = _run_one(root, manifest, name, forge)
            print(json.dumps({'name': name, 'cost_usd': result.get('cost_usd'),
                              'infrastructure_failure': result.get('infrastructure_failure')}), flush=True)
            entry = budget_accounting.account(root, manifest, result)
            if result.get('infrastructure_failure') or entry is None:
                _dump(root/'campaign-status.json', {'status': 'needs_reconciliation', 'run': name})
                raise SystemExit('Run needs infrastructure/cost reconciliation before continuing')
            accounting = budget_accounting.ledger(root, manifest, settled+[result])
            _dump(root/'budget-ledger.json', accounting)
            balances = {k:v for k,v in accounting.items() if k != 'runs'}
            if getattr(args, 'grade_blocks', False):
                iid = manifest['runs'][name]['instance_id']
                block = [n for n, item in manifest['runs'].items() if item['instance_id'] == iid]
                if all((root/'runs'/n/'result.json').exists() for n in block):
                    _dump(root/'campaign-status.json', {'status': 'grading', 'instance_id': iid,
                        'cap_usd': budget, **balances})
                    cmd_grade(SimpleNamespace(root=str(root), swe_python=str(DEFAULT_SWE_PYTHON),
                        max_workers=1, timeout=1800, per_instance=True, instances=[iid]))
                    graded = [r for r in _report_rows(root, manifest) if r['instance_id'] == iid]
                    if len(graded) != len(block) or any(r['resolved'] is None for r in graded):
                        _dump(root/'campaign-status.json', {'status': 'grading_error', 'instance_id': iid,
                            'cap_usd': budget})
                        raise SystemExit('Official grading incomplete; repair before more paid launches')
                    if getattr(args, 'prune_graded_images', False):
                        if not manifest.get('docker_host'):
                            raise SystemExit('Pruning requires a manifest-pinned Docker endpoint')
                        subprocess.run(['docker','image','rm',instance_image({'instance_id':iid})],
                            env=_docker_env(manifest), check=True, capture_output=True, text=True)
                    cmd_report(SimpleNamespace(root=str(root), quiet=True))
        _dump(root/'campaign-status.json', {'status': 'run_limit_reached'
            if len(pending) < pending_total else 'agents_complete_grading_pending',
            'pending': pending_total - len(pending), 'cap_usd': budget,
            **{k:v for k,v in budget_accounting.ledger(root, manifest,
                [json.loads(p.read_text()) for p in (root/'runs').glob('*/result.json')]).items() if k != 'runs'}})
        return
    print(json.dumps({'pending': len(pending), 'parallel': args.parallel}), flush=True)
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        for result in pool.map(lambda n: _run_one(root, manifest, n, forge), pending):
            print(json.dumps({k: result.get(k) for k in ('name', 'exit_code', 'timed_out', 'wall_time_ms',
                                                           'exec_time_ms', 'cost_usd', 'patch_bytes',
                                                           'infrastructure_failure')}), flush=True)


def cmd_grade(args):
    root = Path(args.root).expanduser().resolve()
    manifest = json.loads((root/'manifest.json').read_text())
    grading = root/'grading'
    grading.mkdir(exist_ok=True)
    dataset = manifest.get('dataset', DATASET)
    if manifest.get('dataset_revision'):
        # Keep gold patches/test metadata in the grader, never in agent prompts
        # or the issue-only preparation snapshot. Grade the same pinned data.
        snapshot = grading/'dataset.jsonl'
        if not snapshot.exists():
            script = ('import json,sys\nfrom datasets import load_dataset\n'
                      'ds=load_dataset(sys.argv[1],revision=sys.argv[2],split="test")\n'
                      'with open(sys.argv[3],"w") as f:\n'
                      ' for row in ds: f.write(json.dumps(row)+"\\n")\n')
            subprocess.run([str(args.swe_python), '-c', script, dataset,
                            manifest['dataset_revision'], str(snapshot)], check=True)
        dataset = str(snapshot)
    by_run_id = defaultdict(list)
    for name, item in manifest['runs'].items():
        if getattr(args, 'instances', None) and item['instance_id'] not in args.instances:
            continue
        result_path = root/'runs'/name/'result.json'
        if not result_path.exists():
            continue
        patch_path = root/'runs'/name/'patch.diff'
        if not patch_path.exists():
            continue  # infrastructure failure stays ungraded
        patch = patch_path.read_text()
        suffix = '-'+item['instance_id'] if getattr(args, 'per_instance', False) else ''
        by_run_id[f"{item['arm']}{suffix}-r{item['rep']}"].append(
            {'instance_id': item['instance_id'], 'model_name_or_path': item['arm'], 'model_patch': patch})
    active_reports = (json.loads((grading/'active-reports.json').read_text())
                      if (grading/'active-reports.json').exists() and getattr(args, 'per_instance', False) else {})
    for group_id, preds in sorted(by_run_id.items()):
        digest = hashlib.sha256(json.dumps(preds, sort_keys=True).encode()).hexdigest()[:16]
        arm, rep = group_id.rsplit('-r', 1)
        run_id = f'{arm}-{digest}-r{rep}'
        report = grading/f"{preds[0]['model_name_or_path']}.{run_id}.json"
        active_reports[group_id] = report.name
        _dump(grading/'active-reports.json', active_reports)
        if report.exists():
            continue
        preds_path = grading/f'{run_id}.jsonl'
        preds_path.write_text(''.join(json.dumps(p) + '\n' for p in preds))
        cmd = [str(args.swe_python), '-m', 'swebench.harness.run_evaluation', '--dataset_name', dataset,
               '--split', 'test', '--predictions_path', str(preds_path), '--run_id', run_id,
               '--max_workers', str(args.max_workers), '--timeout', str(args.timeout),
               '--cache_level', 'instance',  # keep the instance images: they are also the agents' environment
               '--instance_ids', *[p['instance_id'] for p in preds]]
        print('+', ' '.join(cmd), flush=True)
        subprocess.run(cmd, cwd=grading, check=False, env=_docker_env(manifest))


def _report_rows(root, manifest):
    grading = root/'grading'
    resolved = {}
    active = grading/'active-reports.json'
    reports = ([grading/name for name in json.loads(active.read_text()).values()]
               if active.exists() else sorted(grading.glob('*.json')))
    for report in reports:
        if not report.exists():
            continue
        try:
            data = json.loads(report.read_text())
        except ValueError:
            continue
        if 'resolved_ids' not in data:
            continue
        arm, run_id = report.name.split('.', 1)[0], report.name.split('.', 1)[1].rsplit('.json', 1)[0]
        rep = int(run_id.rsplit('-r', 1)[1])
        valid = set(data.get('completed_ids', [])) - set(data.get('error_ids', []))
        for iid in valid:
            resolved[(iid, arm, rep)] = iid in set(data['resolved_ids'])
        for iid in data.get('empty_patch_ids', []):
            resolved[(iid, arm, rep)] = False
    rows = []
    for name, item in manifest['runs'].items():
        path = root/'runs'/name/'result.json'
        if not path.exists():
            continue
        r = json.loads(path.read_text())
        r['resolved'] = resolved.get((item['instance_id'], item['arm'], item['rep']))
        rows.append(r)
    return rows


def _geomean(xs):
    xs = [x for x in xs if x and x > 0]
    return math.exp(sum(math.log(x) for x in xs) / len(xs)) if xs else None


def cmd_report(args):
    root = Path(args.root).expanduser().resolve()
    manifest = json.loads((root/'manifest.json').read_text())
    rows = _report_rows(root, manifest)
    arms = list(manifest['arms'])
    by = defaultdict(dict)
    for r in rows:
        by[(r['instance_id'], r['rep'])][r['arm']] = r
    summary = {}
    for arm in arms:
        mine = [r for r in rows if r['arm'] == arm]
        summary[arm] = {
            'planned': sum(item['arm'] == arm for item in manifest['runs'].values()),
            'runs': len(mine),
            'resolved': sum(1 for r in mine if r['resolved']),
            'graded': sum(1 for r in mine if r['resolved'] is not None),
            'empty_patches': sum(1 for r in mine if r['empty_patch']),
            'infra_failures': sum(1 for r in mine if r['infrastructure_failure']),
            'timeouts': sum(1 for r in mine if r['timed_out']),
            'median_exec_s': _median([r['exec_time_ms'] / 1000 for r in mine if r.get('exec_time_ms')]),
            'median_wall_s': _median([r['wall_time_ms'] / 1000 for r in mine if r.get('wall_time_ms')]),
            'total_cost_usd': (round(sum(r['cost_usd'] for r in mine), 2)
                               if mine and all(r.get('cost_usd') is not None for r in mine) else None),
            'known_cost_usd': round(sum(r.get('cost_usd') or 0 for r in mine), 2),
            'unknown_cost_runs': sum(r.get('cost_usd') is None for r in mine),
            'provider_responses': sum((r.get('native') or {}).get('provider_responses', 0) for r in mine),
            'jevgrep_calls': sum((r.get('native') or {}).get('tool_names', {}).get('jevgrep', 0) for r in mine),
            'judge_scored': sum(r.get('mechanisms', {}).get('judge_scored', 0) for r in mine),
            'fast_route_submissions': sum(r.get('mechanisms', {}).get('fast_route_submissions', 0) for r in mine),
        }
        summary[arm]['cost_per_resolved_usd'] = (
            _round(summary[arm]['total_cost_usd'] / summary[arm]['resolved'])
            if summary[arm]['total_cost_usd'] is not None and summary[arm]['resolved'] else None)
    paired = {}
    baseline_arm = 'plain-matched' if 'plain-matched' in arms else 'plain'
    for arm in arms:
        if arm == baseline_arm:
            continue
        time_ratios, cost_ratios = [], []
        for pair in by.values():
            if baseline_arm in pair and arm in pair:
                a, b = pair[baseline_arm], pair[arm]
                if a.get('exec_time_ms') and b.get('exec_time_ms'):
                    time_ratios.append(b['exec_time_ms'] / a['exec_time_ms'])
                if a.get('cost_usd') and b.get('cost_usd'):
                    cost_ratios.append(b['cost_usd'] / a['cost_usd'])
        paired[f'{arm}_vs_{baseline_arm}'] = {'pairs': len(time_ratios), 'geomean_exec_time_ratio': _round(_geomean(time_ratios)),
                                     'geomean_cost_ratio': _round(_geomean(cost_ratios))}
    per_instance = {f'{iid} r{rep}': {arm: {'resolved': r['resolved'], 'exec_s': _round(r['exec_time_ms'] / 1000) if r.get('exec_time_ms') is not None else None,
                                             'cost': _round(r.get('cost_usd')), 'models': [m.get('model') for m in r.get('models_served') or []]}
                                       for arm, r in pair.items()} for (iid, rep), pair in sorted(by.items())}
    complete = all(v['graded'] == v['planned'] for v in summary.values())
    out = {'summary': summary, 'paired': paired, 'per_instance': per_instance,
           'complete': complete,
           'label': ('full-suite-screen-one-repetition' if complete else 'full-suite-incomplete')
                    if manifest.get('all_instances') and manifest['reps'] == 1
                    else ('screen' if manifest['reps'] < 3 else 'dev-reps>=3'),
           'evidence_limits': ['Provider-reported cost is an estimate, not billing.',
                               'One repetition does not establish run-to-run stability; repeat before a production decision.',
                               'Empty patches count as unresolved; grader infrastructure errors remain unknown.',
                               'Fast route submissions alone do not prove net time or money saved.',
                               'Local inference hardware cost is unpriced. x86_64 tests are emulated on this Apple Silicon host.',
                               'exec time = first llm:request to last llm:response in the parent session.']}
    _dump(root/'report.json', out)
    if not getattr(args, 'quiet', False):
        print(json.dumps(out, indent=2))


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    mid = len(xs) // 2
    return round(xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2, 1)


def _round(x, n=3):
    return None if x is None else round(x, n)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--root', required=True)
    p.add_argument('--instances', required=True)
    p.add_argument('--dataset', choices=sorted(DATASETS), default='verified')
    p.add_argument('--dataset-revision')
    p.add_argument('--prior-cost-usd', type=float, default=0.0, help='Known setup/abandoned-run costs charged against the campaign limit')
    p.add_argument('--docker-host', help='Freeze a dedicated Docker endpoint without changing the user context')
    p.add_argument('--foundation-source', default='git+https://github.com/microsoft/amplifier-foundation@main')
    p.add_argument('--lazy', action='store_true', help='Prepare manifests without pulling every image or checkout')
    p.add_argument('--candidate-sha', required=True)
    p.add_argument('--baseline-source', required=True)
    p.add_argument('--arms', default='plain,plain-sonnet,orch-primary')
    p.add_argument('--reps', type=int, default=1)
    p.add_argument('--seed', type=int, default=20260924)
    p.add_argument('--deadline-seconds', type=int, default=1800)
    p.add_argument('--max-iterations', type=int, default=100)
    p.add_argument('--swe-python', default=str(DEFAULT_SWE_PYTHON))
    p.set_defaults(func=cmd_prepare)
    p = sub.add_parser('run')
    p.add_argument('--root', required=True)
    p.add_argument('--parallel', type=int, default=3)
    p.add_argument('--max-runs', type=int, help='Limit new agent runs for a paid preflight; resume retains every receipt')
    p.add_argument('--grade-blocks', action='store_true', help='Officially grade each completed issue block before advancing')
    p.add_argument('--prune-graded-images', action='store_true', help='Remove only each completed block image from the pinned Docker endpoint')
    p.add_argument('--max-cost-usd', type=float, help='Launch guard on recorded estimated API costs, not a billing cap')
    p.add_argument('--reserve-per-run-usd', type=float, default=10.0,
                   help='Reserve before each run; an in-flight run can exceed this estimate')
    p.set_defaults(func=cmd_run)
    p = sub.add_parser('agent', help='internal: one agent run (launched inside Forge by `run`)')
    p.add_argument('--root', required=True)
    p.add_argument('--name', required=True)
    p.set_defaults(func=cmd_agent)
    p = sub.add_parser('grade')
    p.add_argument('--root', required=True)
    p.add_argument('--swe-python', default=str(DEFAULT_SWE_PYTHON))
    p.add_argument('--max-workers', type=int, default=1)
    p.add_argument('--timeout', type=int, default=1800)
    p.add_argument('--per-instance', action='store_true', help='Stable per-issue grading cache for incremental campaigns')
    p.add_argument('--instances', nargs='+', help='Grade only these completed issue IDs')
    p.set_defaults(func=cmd_grade)
    p = sub.add_parser('report')
    p.add_argument('--root', required=True)
    p.set_defaults(func=cmd_report)
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == '__main__':
    main()
