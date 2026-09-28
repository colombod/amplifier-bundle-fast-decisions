"""Tests for scripts/battery.py -- the multi-harness benchmark runner.

Stdlib unittest only. Never invokes claude/codex/opencode/amplifier for real:
external harnesses are driven through a fake `forge` module (patched into
sys.modules) whose `call('run_command', ...)` returns canned per-harness
stdout, and the two amplifier harnesses are driven through fake
launcher/waiter/closer callables that write worker-shaped result.json files
directly. Task data comes from a small fake `battery_tasks` module (2 code
tasks + 1 answer task) injected via sys.modules, never the real one (which
another builder is writing concurrently).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import battery  # noqa: E402
import forge_e2e  # noqa: E402


# --------------------------------------------------------------------------
# Fake battery_tasks module (2 code tasks + 1 answer task)
# --------------------------------------------------------------------------

class _Task:
    def __init__(self, name, family, split, kind, prompt, files, protected,
                 expected_answer=None, evaluate_fn=None):
        self.name = name
        self.family = family
        self.split = split
        self.kind = kind
        self.prompt = prompt
        self.files = files
        self.protected = protected
        self.expected_answer = expected_answer
        self._evaluate_fn = evaluate_fn

    def evaluate(self, workspace):
        return self._evaluate_fn(workspace)


def _eval_module(workspace, attr):
    import importlib.util
    spec = importlib.util.spec_from_file_location('candidate_solution', Path(workspace)/'solution.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, attr)


def _eval_add_one(workspace):
    fn = _eval_module(workspace, 'add_one')
    checks, failures = 0, []
    for x, expected in [(1, 2), (5, 6), (-1, 0)]:
        checks += 1
        try:
            ok = fn(x) == expected
        except Exception:
            ok = False
        if not ok:
            failures.append(f'add_one({x})!={expected}')
    return {'checks': checks, 'passed': checks-len(failures), 'failed': len(failures), 'failure_labels': failures}


def _eval_double(workspace):
    fn = _eval_module(workspace, 'double')
    checks, failures = 0, []
    for x, expected in [(1, 2), (3, 6), (0, 0)]:
        checks += 1
        try:
            ok = fn(x) == expected
        except Exception:
            ok = False
        if not ok:
            failures.append(f'double({x})!={expected}')
    return {'checks': checks, 'passed': checks-len(failures), 'failed': len(failures), 'failure_labels': failures}


def make_fake_battery_tasks():
    mod = ModuleType('battery_tasks')

    tasks = {
        'add_one': _Task(
            'add_one', 'arith', 'dev', 'code',
            'Fix solution.py so add_one(x) returns x+1. Run python3 -m unittest -v test_public.py.',
            {'README.md': 'Fix add_one.\n', 'solution.py': 'def add_one(x):\n    return x\n',
             'test_public.py': ('import unittest\nfrom solution import add_one\n'
                                 'class T(unittest.TestCase):\n def test(self):\n  self.assertEqual(add_one(1), 2)\n')},
            ('README.md', 'test_public.py'), evaluate_fn=_eval_add_one),
        'double': _Task(
            'double', 'arith', 'holdout', 'code',
            'Fix solution.py so double(x) returns x*2. Run python3 -m unittest -v test_public.py.',
            {'README.md': 'Fix double.\n', 'solution.py': 'def double(x):\n    return x\n',
             'test_public.py': ('import unittest\nfrom solution import double\n'
                                 'class T(unittest.TestCase):\n def test(self):\n  self.assertEqual(double(3), 6)\n')},
            ('README.md', 'test_public.py'), evaluate_fn=_eval_double),
        'capital': _Task(
            'capital', 'geography', 'dev', 'answer',
            'What is the capital of France? Answer in one word.',
            {'README.md': 'Answer the question in the prompt with one word.\n'},
            ('README.md',), expected_answer=r'(?i)paris'),
    }

    def check_answer(task, final_message):
        checks = 1
        if task.expected_answer and final_message and re.search(task.expected_answer, final_message):
            return {'checks': checks, 'passed': 1, 'failed': 0, 'failure_labels': []}
        return {'checks': checks, 'passed': 0, 'failed': 1, 'failure_labels': ['answer_mismatch']}

    def split(name):
        if name == 'all':
            return list(tasks)
        return [n for n, t in tasks.items() if t.split == name]

    mod.TASKS = tasks
    mod.check_answer = check_answer
    mod.split = split
    mod.REFERENCE_SOLUTIONS = {}
    return mod


def _patched_battery_tasks():
    return patch.dict(sys.modules, {'battery_tasks': make_fake_battery_tasks()})


# --------------------------------------------------------------------------
# Canned harness stdout fixtures
# --------------------------------------------------------------------------

CLAUDE_STDOUT = '\n'.join([
    json.dumps({'type': 'system', 'subtype': 'init'}),
    json.dumps({'type': 'assistant', 'text': 'working...'}),
    json.dumps({'type': 'result', 'total_cost_usd': 0.42, 'duration_ms': 12345, 'num_turns': 3,
                'modelUsage': {'claude-opus-5[1m]': {'input_tokens': 100, 'output_tokens': 50}},
                'result': 'All good, tests updated and passing.'}),
]) + '\n'

CODEX_STDOUT = '\n'.join([
    json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1000, 'cached_input_tokens': 200,
                                                       'cache_write_input_tokens': 50, 'output_tokens': 300,
                                                       'reasoning_output_tokens': 20}}),
    json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 500, 'cached_input_tokens': 0,
                                                       'cache_write_input_tokens': 0, 'output_tokens': 100,
                                                       'reasoning_output_tokens': 0}}),
]) + '\n'

OPENCODE_STDOUT = '\n'.join([
    json.dumps({'type': 'text', 'part': {'text': 'Working on it...'}}),
    json.dumps({'type': 'step_finish', 'part': {'tokens': {'total': 300, 'input': 200, 'output': 100,
                                                              'reasoning': 0, 'cache': 0}, 'cost': 0.05}}),
    json.dumps({'type': 'text', 'part': {'text': 'Done, solution updated.'}}),
]) + '\n'


def make_fake_forge(behaviors):
    """behaviors: {command: value|callable(args)->dict|Exception}."""
    calls = []

    def call(tool, args):
        calls.append((tool, dict(args)))
        if tool == 'close_terminal':
            return {}
        behavior = behaviors.get(args['command'])
        if isinstance(behavior, BaseException):
            raise behavior
        if callable(behavior):
            return behavior(args)
        return behavior
    return SimpleNamespace(call=call), calls


def codex_ok(args):
    argv = args['args']
    out_path = argv[argv.index('-o')+1]
    Path(out_path).write_text('Fixed the bug and validated with tests.\n')
    return {'output': CODEX_STDOUT, 'exitCode': 0}


# --------------------------------------------------------------------------
# Pure-function tests: harness output parsing
# --------------------------------------------------------------------------

class ParseClaudeTests(unittest.TestCase):
    def test_parses_last_result_line(self):
        parsed = battery._parse_claude(CLAUDE_STDOUT)
        self.assertEqual(parsed['cost_usd'], 0.42)
        self.assertEqual(parsed['harness_duration_ms'], 12345)
        self.assertEqual(parsed['num_turns'], 3)
        self.assertEqual(parsed['model'], 'claude-opus-5[1m]')
        self.assertEqual(parsed['tokens'], {'input_tokens': 100, 'output_tokens': 50})
        self.assertEqual(parsed['final_message'], 'All good, tests updated and passing.')
        self.assertEqual(parsed['cost_source'], 'harness_reported')
        self.assertTrue(parsed['cost_billable'])

    def test_no_result_line_returns_none(self):
        self.assertIsNone(battery._parse_claude('{"type":"system"}\n'))


class ParseCodexTests(unittest.TestCase):
    def test_sums_usage_across_turns_and_reads_last_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            msg_path = Path(tmp)/'last-message.md'
            msg_path.write_text('Fixed it.\n')
            parsed = battery._parse_codex(CODEX_STDOUT, msg_path, model='gpt-6-astra')
        self.assertEqual(parsed['tokens'], {'input_tokens': 1500, 'cached_input_tokens': 200,
                                             'cache_write_input_tokens': 50, 'output_tokens': 400,
                                             'reasoning_output_tokens': 20})
        self.assertEqual(parsed['final_message'], 'Fixed it.\n')
        self.assertEqual(parsed['cost_source'], 'computed_from_tokens_estimate')
        self.assertIsNotNone(parsed['cost_usd'])
        self.assertTrue(parsed['cost_billable'])

    def test_unknown_model_leaves_cost_unknown(self):
        parsed = battery._parse_codex(CODEX_STDOUT, None, model='some-unpriced-model')
        self.assertIsNone(parsed['cost_usd'])
        self.assertEqual(parsed['cost_source'], 'unknown')
        self.assertFalse(parsed['cost_billable'])

    def test_no_usage_events_returns_none_tokens(self):
        parsed = battery._parse_codex('{"type":"other"}\n', None, model='gpt-6-astra')
        self.assertIsNone(parsed['tokens'])


class ParseOpencodeTests(unittest.TestCase):
    def test_concatenates_last_text_and_sums_cost(self):
        parsed = battery._parse_opencode(OPENCODE_STDOUT, model='runpod/zai-org/GLM-5.3-Flash')
        self.assertEqual(parsed['final_message'], 'Done, solution updated.')
        self.assertEqual(parsed['tokens']['total'], 300)
        self.assertAlmostEqual(parsed['cost_usd'], 0.05)
        self.assertEqual(parsed['cost_source'], 'gateway_internal_not_metered')
        self.assertFalse(parsed['cost_billable'])


# --------------------------------------------------------------------------
# Pure-function tests: evaluate math
# --------------------------------------------------------------------------

class EvaluateMathTests(unittest.TestCase):
    def test_penalized_ms_uses_wall_time_only_when_passed(self):
        self.assertEqual(battery._penalized_ms({'outcome_passed': True, 'wall_time_ms': 111}, 60000), 111)
        self.assertEqual(battery._penalized_ms({'outcome_passed': False, 'wall_time_ms': 111}, 60000), 60000)
        self.assertEqual(battery._penalized_ms(None, 60000), 60000)

    def test_geomean(self):
        self.assertAlmostEqual(battery._geomean([1.0, 4.0]), 2.0)
        self.assertIsNone(battery._geomean([]))

    def test_sign_test_hand_computed(self):
        # 4 wins (candidate faster), 1 loss, 0 ties -> n=5, k=min(4,1)=1
        # exact two-sided binomial: 2 * sum_{i=0}^{1} C(5,i)/2^5 = 2*(1+5)/32 = 12/32 = 0.375
        diffs = [-10, -10, -10, -10, 5]
        result = battery._sign_test(diffs)
        self.assertEqual(result['wins'], 4)
        self.assertEqual(result['losses'], 1)
        self.assertEqual(result['ties'], 0)
        self.assertAlmostEqual(result['p_value'], 0.375)

    def test_sign_test_all_ties(self):
        result = battery._sign_test([0, 0, 0])
        self.assertIsNone(result['p_value'])


# --------------------------------------------------------------------------
# prepare
# --------------------------------------------------------------------------

def _prepare_args(root, experiment, tasks='all', harnesses='claude,codex,opencode,amplifier-plain,amplifier-fd',
                   seed=7, baseline_source=None, candidate_source=None, deadline_seconds=60,
                   fd_backend=None, allow_external_state=False, candidate_sha=None,
                   amplifier_model=None, amplifier_effort=None, amplifier_bundle=None):
    return SimpleNamespace(
        root=str(root), experiment=experiment, harnesses=harnesses, tasks=tasks, seed=seed,
        fd_override=None, deadline_seconds=deadline_seconds, claude_model='claude-x', codex_model='gpt-6-astra',
        opencode_model='runpod/zai-org/GLM-5.3-Flash', claude_max_budget_usd=3.0,
        baseline_source=baseline_source, candidate_source=candidate_source,
        fd_backend=fd_backend, allow_external_state=allow_external_state, candidate_sha=candidate_sha,
        amplifier_model=amplifier_model, amplifier_effort=amplifier_effort, amplifier_bundle=amplifier_bundle)


def _init_git_repo(path):
    '''Minimal committed git repo, used as a --candidate-source worktree
    stand-in. Returns the HEAD sha.'''
    path.mkdir(parents=True, exist_ok=True)
    (path/'modules').mkdir(exist_ok=True)
    (path/'src'/'amplifier_fast_decisions').mkdir(parents=True, exist_ok=True)
    (path/'src'/'amplifier_fast_decisions'/'__init__.py').write_text('__version__ = "0.0.0"\n')
    run = lambda *args: subprocess.run(['git', '-C', str(path), *args], check=True,
                                        capture_output=True, text=True)
    run('init', '-q')
    run('config', 'user.email', 'test@example.com')
    run('config', 'user.name', 'Test')
    run('add', '-A')
    run('commit', '-q', '-m', 'initial')
    return run('rev-parse', 'HEAD').stdout.strip()


class PrepareTests(unittest.TestCase):
    def test_frozen_order_deterministic_and_workspaces_identical_per_task(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            base = Path(tmp)
            baseline = base/'baseline'; candidate = base/'candidate'
            baseline.mkdir(); candidate.mkdir()
            root_a = base/'campaign_a'; root_b = base/'campaign_b'
            args_a = _prepare_args(root_a, 'e1', baseline_source=str(baseline), candidate_source=str(candidate))
            args_b = _prepare_args(root_b, 'e1', baseline_source=str(baseline), candidate_source=str(candidate))
            manifest_a = battery.cmd_prepare(args_a)
            manifest_b = battery.cmd_prepare(args_b)
            self.assertEqual(manifest_a['run_order'], manifest_b['run_order'])

            proposal = json.loads((root_a/'experiments'/'e1'/'proposal.json').read_text())
            self.assertEqual(proposal['schema_version'], battery.BATTERY_SCHEMA)
            self.assertEqual(sorted(proposal['harnesses']), sorted(['claude', 'codex', 'opencode',
                                                                      'amplifier-plain', 'amplifier-fd']))
            self.assertIn('add_one', proposal['dev_tasks'])
            self.assertIn('capital', proposal['dev_tasks'])
            self.assertIn('double', proposal['holdout_tasks'])
            self.assertIn('prompt_sha256', proposal)
            self.assertIn('preregistered_at_utc', proposal)
            self.assertEqual(proposal['models']['codex'], 'gpt-6-astra')

            experiment_dir = root_a/'experiments'/'e1'
            by_task = {}
            for name, item in manifest_a['runs'].items():
                run_dir = battery._run_dir_for(experiment_dir, name, item['harness'])
                by_task.setdefault(item['task'], set()).add(forge_e2e.hash_files(run_dir/'workspace'))
            for task, hashes in by_task.items():
                self.assertEqual(len(hashes), 1, f'workspace mismatch for {task}: {hashes}')

    def test_only_dev_split_selected(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            base = Path(tmp)
            baseline = base/'baseline'; candidate = base/'candidate'
            baseline.mkdir(); candidate.mkdir()
            root = base/'campaign'
            args = _prepare_args(root, 'dev1', tasks='dev', harnesses='claude',
                                  baseline_source=str(baseline), candidate_source=str(candidate))
            manifest = battery.cmd_prepare(args)
            tasks_seen = {item['task'] for item in manifest['runs'].values()}
            self.assertEqual(tasks_seen, {'add_one', 'capital'})

    def test_amplifier_sub_manifest_carries_battery_task_prompt_and_matching_sha256(self):
        """Defect 1: amplifier runs must receive the battery task's own prompt, not
        the generic legacy README-repair prompt -- and the sub-manifest must record
        it so forge_e2e.worker can verify it before ever launching amplifier."""
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            import forge_workloads
            base = Path(tmp)
            baseline = base/'baseline'; candidate = base/'candidate'
            baseline.mkdir(); candidate.mkdir()
            root = base/'campaign'
            args = _prepare_args(root, 'e8', harnesses='amplifier-fd', tasks='dev',
                                  baseline_source=str(baseline), candidate_source=str(candidate))
            battery.cmd_prepare(args)
            experiment_dir = root/'experiments'/'e8'
            amp_manifest = json.loads((experiment_dir/'runs'/'amplifier'/'manifest.json').read_text())
            self.assertTrue(amp_manifest['runs'])
            for name, item in amp_manifest['runs'].items():
                task_prompt = forge_workloads.task_prompt(item['task'])
                self.assertIsNotNone(task_prompt, name)
                self.assertNotEqual(task_prompt, forge_e2e.PROMPT, name)
                self.assertEqual(item['prompt'], task_prompt, name)
                self.assertEqual(item['prompt_sha256'], hashlib.sha256(task_prompt.encode()).hexdigest(), name)
                self.assertEqual(item['deadline_seconds'], 60)

    def test_amplifier_harnesses_require_sources(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = Path(tmp)/'campaign'
            args = _prepare_args(root, 'e2', harnesses='amplifier-plain', tasks='dev')
            with self.assertRaises(SystemExit) as ctx:
                battery.cmd_prepare(args)
            self.assertEqual(ctx.exception.code, 4)

    def test_candidate_git_worktree_is_frozen_into_a_detached_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            base = Path(tmp)
            baseline = base/'baseline'; baseline.mkdir()
            candidate = base/'candidate'
            head_sha = _init_git_repo(candidate)
            root = base/'campaign'
            args = _prepare_args(root, 'e3', harnesses='amplifier-fd', tasks='dev',
                                  baseline_source=str(baseline), candidate_source=str(candidate))
            manifest = battery.cmd_prepare(args)
            self.assertTrue(manifest['run_order'])

            experiment_dir = root.resolve()/'experiments'/'e3'
            snapshot = experiment_dir/'source'
            self.assertTrue(snapshot.is_dir())
            snapshot_head = subprocess.run(
                ['git', '-C', str(snapshot), 'rev-parse', 'HEAD'],
                check=True, capture_output=True, text=True).stdout.strip()
            self.assertEqual(snapshot_head, head_sha)

            amp_manifest = json.loads((experiment_dir/'runs'/'amplifier'/'manifest.json').read_text())
            self.assertEqual(amp_manifest['sides']['amplifier-fd']['source_root'], str(snapshot))
            self.assertEqual(amp_manifest['sides']['amplifier-fd']['source_git_sha'], head_sha)
            # Baseline (a plain, non-git dir here) is never snapshotted.
            self.assertEqual(amp_manifest['sides']['amplifier-plain']['source_root'], str(baseline.resolve()))

            proposal = json.loads((experiment_dir/'proposal.json').read_text())
            self.assertEqual(proposal['candidate_source'], str(snapshot))
            self.assertEqual(proposal['candidate_source_snapshot']['git_sha'], head_sha)
            self.assertEqual(proposal['candidate_source_snapshot']['original_source_root'],
                              str(candidate.resolve()))

            # Editing the live worktree after prepare must not change the
            # frozen snapshot's committed content.
            (candidate/'src'/'amplifier_fast_decisions'/'__init__.py').write_text('__version__ = "9.9.9"\n')
            still_head = subprocess.run(
                ['git', '-C', str(snapshot), 'rev-parse', 'HEAD'],
                check=True, capture_output=True, text=True).stdout.strip()
            self.assertEqual(still_head, head_sha)
            self.assertNotEqual(
                (snapshot/'src'/'amplifier_fast_decisions'/'__init__.py').read_text(),
                (candidate/'src'/'amplifier_fast_decisions'/'__init__.py').read_text(),
            )

    def test_non_git_candidate_source_is_used_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            base = Path(tmp)
            baseline = base/'baseline'; baseline.mkdir()
            candidate = base/'candidate'; candidate.mkdir()
            root = base/'campaign'
            args = _prepare_args(root, 'e4', harnesses='amplifier-fd', tasks='dev',
                                  baseline_source=str(baseline), candidate_source=str(candidate))
            battery.cmd_prepare(args)
            experiment_dir = root/'experiments'/'e4'
            self.assertFalse((experiment_dir/'source').exists())
            proposal = json.loads((experiment_dir/'proposal.json').read_text())
            self.assertEqual(proposal['candidate_source'], str(candidate.resolve()))
            self.assertIsNone(proposal['candidate_source_snapshot'])

    def test_fd_backend_jev_requires_allow_external_state(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            base = Path(tmp)
            baseline = base/'baseline'; baseline.mkdir()
            candidate = base/'candidate'; candidate.mkdir()
            root = base/'campaign'
            args = _prepare_args(root, 'e5', harnesses='amplifier-fd', tasks='dev',
                                  baseline_source=str(baseline), candidate_source=str(candidate),
                                  fd_backend='jev', allow_external_state=False)
            with self.assertRaises(SystemExit) as ctx:
                battery.cmd_prepare(args)
            self.assertEqual(ctx.exception.code, 4)
            # Refused before any experiment state is created.
            self.assertFalse((root/'experiments'/'e5').exists())

    def test_fd_backend_jev_with_allow_external_state_sets_decision_overrides(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            base = Path(tmp)
            baseline = base/'baseline'; baseline.mkdir()
            candidate = base/'candidate'; candidate.mkdir()
            root = base/'campaign'
            args = _prepare_args(root, 'e6', harnesses='amplifier-fd', tasks='dev',
                                  baseline_source=str(baseline), candidate_source=str(candidate),
                                  fd_backend='jev', allow_external_state=True)
            battery.cmd_prepare(args)
            experiment_dir = root/'experiments'/'e6'
            amp_manifest = json.loads((experiment_dir/'runs'/'amplifier'/'manifest.json').read_text())
            fd_side = amp_manifest['sides']['amplifier-fd']
            self.assertEqual(fd_side['decision_overrides']['backend'], 'jev')
            self.assertTrue(fd_side['decision_overrides']['allow_external_state'])

    def test_fd_backend_ollama_does_not_require_allow_external_state(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            base = Path(tmp)
            baseline = base/'baseline'; baseline.mkdir()
            candidate = base/'candidate'; candidate.mkdir()
            root = base/'campaign'
            args = _prepare_args(root, 'e7', harnesses='amplifier-fd', tasks='dev',
                                  baseline_source=str(baseline), candidate_source=str(candidate),
                                  fd_backend='ollama')
            manifest = battery.cmd_prepare(args)
            self.assertTrue(manifest['run_order'])


class SlugAndExperimentDirTests(unittest.TestCase):
    """Defect: cell/experiment ids may legitimately contain '+' (e.g.
    'judge-jev+effort-incumbent'). '+' is outside RFC 3986's unreserved set,
    so Path.as_uri() percent-encodes it to '%2B' the moment a run directory
    becomes a bundle file:// URI, and Amplifier's bundle loader never
    decodes it back -- "Bundle Error -- File not found" for a path that
    plainly exists on disk. The fix: slug every path SEGMENT derived from an
    id; never touch the id itself (it stays the manifest/analysis key)."""

    def test_slug_replaces_unsafe_chars_only(self):
        self.assertEqual(battery._slug('judge-jev+effort-incumbent'), 'judge-jev-effort-incumbent')
        self.assertEqual(battery._slug('safe-name_1.2'), 'safe-name_1.2')
        self.assertEqual(battery._slug('a b/c:d'), 'a-b-c-d')

    def test_experiment_dir_for_is_slugged_but_run_dir_for_composes_under_it(self):
        root = Path('/campaign-root')
        exp_dir = battery._experiment_dir_for(root, 'judge-jev+effort-incumbent-s1-dev-r1')
        self.assertNotIn('+', str(exp_dir))
        self.assertEqual(exp_dir.name, 'judge-jev-effort-incumbent-s1-dev-r1')
        run_dir = battery._run_dir_for(exp_dir, 'judge-jev+effort-incumbent-s1-dev-r1-amplifier-fd-a2', 'amplifier-fd')
        self.assertNotIn('+', str(run_dir))
        self.assertEqual(run_dir.name, 'judge-jev-effort-incumbent-s1-dev-r1-amplifier-fd-a2')
        self.assertIn('amplifier', run_dir.parts)

    def test_prepare_with_plus_in_experiment_id_produces_safe_on_disk_paths(self):
        """End-to-end: cmd_prepare with a '+'-bearing experiment id must
        create directories with no '+' anywhere, while proposal.json/
        manifest.json keep the id and run names verbatim (the analysis
        identifiers a downstream evaluate/report call keys off of)."""
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            base = Path(tmp)
            baseline = base/'baseline'; baseline.mkdir()
            candidate = base/'candidate'; candidate.mkdir()
            root = base/'campaign'
            exp_id = 'judge-jev+effort-incumbent-s1-dev-r1'
            args = _prepare_args(root, exp_id, harnesses='amplifier-fd,claude', tasks='dev',
                                  baseline_source=str(baseline), candidate_source=str(candidate))
            manifest = battery.cmd_prepare(args)

            experiment_dir = battery._experiment_dir_for(root, exp_id)
            self.assertTrue(experiment_dir.exists())
            self.assertNotIn('+', str(experiment_dir))

            proposal = json.loads((experiment_dir/'proposal.json').read_text())
            self.assertEqual(proposal['experiment_id'], exp_id)  # analysis identifier, unslugged

            # manifest run keys/run_order are the raw (unslugged) names -- the
            # analysis identifiers -- even though they contain '+'.
            self.assertTrue(any('+' in n for n in manifest['run_order']))
            self.assertTrue(any('+' in n for n in manifest['runs']))

            # But every actual run directory on disk is percent-encoding-safe.
            for name, item in manifest['runs'].items():
                run_dir = battery._run_dir_for(experiment_dir, name, item['harness'])
                self.assertTrue(run_dir.exists(), run_dir)
                self.assertNotIn('+', str(run_dir))
                # str(run_dir) must round-trip through urllib.parse.quote unchanged --
                # i.e. it would never be percent-encoded into a bundle file:// URI.
                import urllib.parse
                self.assertEqual(urllib.parse.quote(str(run_dir), safe='/'), str(run_dir))


class AmplifierModelEffortTests(unittest.TestCase):
    """--amplifier-model/--amplifier-effort must reach BOTH amplifier side
    profiles/manifests (plain and fd) identically, so amplifier-plain can be
    run standalone at a pinned model/effort with no fast-decisions involved."""

    def test_default_model_unchanged_when_flags_omitted(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            base = Path(tmp)
            baseline = base/'baseline'; baseline.mkdir()
            candidate = base/'candidate'; candidate.mkdir()
            root = base/'campaign'
            args = _prepare_args(root, 'e9', harnesses='amplifier-plain,amplifier-fd', tasks='dev',
                                  baseline_source=str(baseline), candidate_source=str(candidate))
            battery.cmd_prepare(args)
            experiment_dir = root/'experiments'/'e9'
            proposal = json.loads((experiment_dir/'proposal.json').read_text())
            self.assertEqual(proposal['amplifier_model'], 'claude-fable-5-1')
            self.assertIsNone(proposal['amplifier_effort'])
            self.assertEqual(proposal['models']['amplifier-plain'], 'claude-fable-5-1')
            self.assertEqual(proposal['models']['amplifier-fd'], 'claude-fable-5-1')
            amp_manifest = json.loads((experiment_dir/'runs'/'amplifier'/'manifest.json').read_text())
            self.assertEqual(amp_manifest['model'], 'claude-fable-5-1')
            for name, item in amp_manifest['runs'].items():
                run_dir = battery._run_dir_for(experiment_dir, name, item['side'])
                profile = json.loads((run_dir/'profile.md').read_text().split('---')[1])
                self.assertNotIn('providers', profile, name)

    def test_amplifier_model_and_effort_reach_both_side_profiles_and_manifests(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            base = Path(tmp)
            baseline = base/'baseline'; baseline.mkdir()
            candidate = base/'candidate'; candidate.mkdir()
            root = base/'campaign'
            args = _prepare_args(root, 'e10', harnesses='amplifier-plain,amplifier-fd', tasks='dev',
                                  baseline_source=str(baseline), candidate_source=str(candidate),
                                  amplifier_model='claude-sonnet-5', amplifier_effort='medium')
            battery.cmd_prepare(args)
            experiment_dir = root/'experiments'/'e10'
            proposal = json.loads((experiment_dir/'proposal.json').read_text())
            self.assertEqual(proposal['amplifier_model'], 'claude-sonnet-5')
            self.assertEqual(proposal['amplifier_effort'], 'medium')
            self.assertEqual(proposal['models']['amplifier-plain'], 'claude-sonnet-5')
            self.assertEqual(proposal['models']['amplifier-fd'], 'claude-sonnet-5')
            amp_manifest = json.loads((experiment_dir/'runs'/'amplifier'/'manifest.json').read_text())
            self.assertEqual(amp_manifest['model'], 'claude-sonnet-5')
            self.assertEqual(amp_manifest['amplifier_effort'], 'medium')
            seen_sides = set()
            for name, item in amp_manifest['runs'].items():
                run_dir = battery._run_dir_for(experiment_dir, name, item['side'])
                profile = json.loads((run_dir/'profile.md').read_text().split('---')[1])
                providers = profile.get('providers')
                self.assertTrue(providers, name)
                entry = next(pr for pr in providers if pr['module'] == 'provider-anthropic')
                self.assertEqual(entry['config']['reasoning_effort'], 'medium', name)
                seen_sides.add(item['side'])
            self.assertEqual(seen_sides, {'amplifier-plain', 'amplifier-fd'})


class AmplifierBundleTests(unittest.TestCase):
    """--amplifier-bundle {foundation,lean}: isolates the installed-context
    weight hypothesis. Must reach proposal.json, both manifests, and both
    side profiles (plain and fd) identically."""

    def test_default_is_foundation_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            base = Path(tmp)
            baseline = base/'baseline'; baseline.mkdir()
            candidate = base/'candidate'; candidate.mkdir()
            root = base/'campaign'
            args = _prepare_args(root, 'eb1', harnesses='amplifier-plain,amplifier-fd', tasks='dev',
                                  baseline_source=str(baseline), candidate_source=str(candidate))
            battery.cmd_prepare(args)
            experiment_dir = root/'experiments'/'eb1'
            proposal = json.loads((experiment_dir/'proposal.json').read_text())
            self.assertEqual(proposal['amplifier_bundle'], 'foundation')
            amp_manifest = json.loads((experiment_dir/'runs'/'amplifier'/'manifest.json').read_text())
            self.assertEqual(amp_manifest['amplifier_bundle'], 'foundation')
            for name, item in amp_manifest['runs'].items():
                run_dir = battery._run_dir_for(experiment_dir, name, item['side'])
                profile = json.loads((run_dir/'profile.md').read_text().split('---')[1])
                self.assertTrue(profile['includes'], name)

    def test_lean_reaches_proposal_manifests_and_both_side_profiles(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            base = Path(tmp)
            baseline = base/'baseline'; baseline.mkdir()
            candidate = base/'candidate'; candidate.mkdir()
            root = base/'campaign'
            args = _prepare_args(root, 'eb2', harnesses='amplifier-plain,amplifier-fd', tasks='dev',
                                  baseline_source=str(baseline), candidate_source=str(candidate),
                                  amplifier_bundle='lean')
            battery.cmd_prepare(args)
            experiment_dir = root/'experiments'/'eb2'
            proposal = json.loads((experiment_dir/'proposal.json').read_text())
            self.assertEqual(proposal['amplifier_bundle'], 'lean')
            amp_manifest = json.loads((experiment_dir/'runs'/'amplifier'/'manifest.json').read_text())
            self.assertEqual(amp_manifest['amplifier_bundle'], 'lean')
            seen_sides = set()
            for name, item in amp_manifest['runs'].items():
                run_dir = battery._run_dir_for(experiment_dir, name, item['side'])
                profile = json.loads((run_dir/'profile.md').read_text().split('---')[1])
                self.assertEqual(profile['includes'], [], name)
                tool_modules = {t['module'] for t in profile['tools']}
                self.assertEqual(
                    tool_modules, {'tool-filesystem', 'tool-bash', 'tool-todo', 'tool-fast-workspace'}, name)
                providers = profile.get('providers')
                self.assertTrue(providers, name)
                self.assertIn('source', next(p for p in providers if p['module'] == 'provider-anthropic'))
                seen_sides.add(item['side'])
            self.assertEqual(seen_sides, {'amplifier-plain', 'amplifier-fd'})


# --------------------------------------------------------------------------
# Defect C: claude --permission-mode wiring
# --------------------------------------------------------------------------

_POLY_PY_INSTRUCTIONS = "Implement `add(a, b)` returning the sum of `a` and `b`.\n"
_POLY_PY_EXAMPLE = "def add(a, b):\n    return a + b\n"
_POLY_PY_STUB = "def add(a, b):\n    pass\n"
_POLY_PY_TEST = (
    "import unittest\nfrom add_numbers import add\n\n\n"
    "class AddNumbersTest(unittest.TestCase):\n"
    "    def test_positive(self):\n        self.assertEqual(add(2, 3), 5)\n"
)
_POLY_PY_CONFIG = {
    "files": {
        "solution": ["add_numbers.py"],
        "test": ["add_numbers_test.py"],
        "example": [".meta/example.py"],
    }
}


def _build_fake_polyglot_corpus(root):
    """Minimal one-exercise polyglot-benchmark-shaped corpus (python only),
    just enough for battery.py's --task-source polyglot plumbing to load and
    schedule one real task -- no network, no other languages."""
    ex = root / 'python' / 'exercises' / 'practice' / 'add-numbers'
    (ex / '.docs').mkdir(parents=True, exist_ok=True)
    (ex / '.docs' / 'instructions.md').write_text(_POLY_PY_INSTRUCTIONS)
    (ex / '.meta').mkdir(parents=True, exist_ok=True)
    (ex / '.meta' / 'config.json').write_text(json.dumps(_POLY_PY_CONFIG))
    (ex / '.meta' / 'example.py').write_text(_POLY_PY_EXAMPLE)
    (ex / 'add_numbers.py').write_text(_POLY_PY_STUB)
    (ex / 'add_numbers_test.py').write_text(_POLY_PY_TEST)


def _prepare_polyglot_args(root, experiment, polyglot_root, harnesses='claude',
                            claude_permission_mode=None):
    return SimpleNamespace(
        root=str(root), experiment=experiment, harnesses=harnesses, tasks=None, seed=1,
        fd_override=None, deadline_seconds=None, claude_model='claude-x', codex_model=None,
        opencode_model=None, claude_max_budget_usd=3.0,
        baseline_source=None, candidate_source=None,
        fd_backend=None, allow_external_state=False, candidate_sha=None,
        amplifier_model=None, amplifier_effort=None,
        task_source='polyglot', polyglot_root=str(polyglot_root), languages='python',
        slice=1, split='all', claude_permission_mode=claude_permission_mode,
    )


class ClaudePermissionModeTests(unittest.TestCase):
    """Defect C: Claude Code refuses build/test commands under the default
    --permission-mode acceptEdits. --claude-permission-mode threads the choice
    into the claude argv builder and proposal.json; battery task-source keeps
    the unchanged acceptEdits default, polyglot defaults to bypassPermissions."""

    def test_claude_argv_uses_requested_permission_mode(self):
        argv = battery._claude_argv('hi', 'claude-x', 3.0, 'bypassPermissions')
        self.assertIn('--permission-mode', argv)
        self.assertEqual(argv[argv.index('--permission-mode') + 1], 'bypassPermissions')

    def test_claude_argv_default_is_acceptEdits(self):
        argv = battery._claude_argv('hi', 'claude-x', 3.0)
        self.assertEqual(argv[argv.index('--permission-mode') + 1], 'acceptEdits')

    def test_battery_task_source_defaults_to_acceptEdits_in_proposal_and_commands(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            base = Path(tmp)
            baseline = base/'baseline'; candidate = base/'candidate'
            baseline.mkdir(); candidate.mkdir()
            root = base/'campaign'
            args = _prepare_args(root, 'cpm1', harnesses='claude', tasks='dev',
                                  baseline_source=str(baseline), candidate_source=str(candidate))
            battery.cmd_prepare(args)
            proposal = json.loads((root/'experiments'/'cpm1'/'proposal.json').read_text())
            self.assertEqual(proposal['claude_permission_mode'], 'acceptEdits')
            claude_cmd = proposal['commands']['claude']
            self.assertEqual(claude_cmd[claude_cmd.index('--permission-mode') + 1], 'acceptEdits')

    def test_battery_task_source_explicit_override_wins(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            base = Path(tmp)
            baseline = base/'baseline'; candidate = base/'candidate'
            baseline.mkdir(); candidate.mkdir()
            root = base/'campaign'
            args = _prepare_args(root, 'cpm2', harnesses='claude', tasks='dev',
                                  baseline_source=str(baseline), candidate_source=str(candidate))
            args.claude_permission_mode = 'bypassPermissions'
            battery.cmd_prepare(args)
            proposal = json.loads((root/'experiments'/'cpm2'/'proposal.json').read_text())
            self.assertEqual(proposal['claude_permission_mode'], 'bypassPermissions')
            claude_cmd = proposal['commands']['claude']
            self.assertEqual(claude_cmd[claude_cmd.index('--permission-mode') + 1], 'bypassPermissions')

    def test_polyglot_task_source_defaults_to_bypassPermissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            corpus = base/'polyglot-benchmark'
            _build_fake_polyglot_corpus(corpus)
            root = base/'campaign'
            args = _prepare_polyglot_args(root, 'poly1', corpus)
            battery.cmd_prepare(args)
            proposal = json.loads((root/'experiments'/'poly1'/'proposal.json').read_text())
            self.assertEqual(proposal['claude_permission_mode'], 'bypassPermissions')
            claude_cmd = proposal['commands']['claude']
            self.assertEqual(claude_cmd[claude_cmd.index('--permission-mode') + 1], 'bypassPermissions')

    def test_polyglot_task_source_explicit_override_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            corpus = base/'polyglot-benchmark'
            _build_fake_polyglot_corpus(corpus)
            root = base/'campaign'
            args = _prepare_polyglot_args(root, 'poly2', corpus, claude_permission_mode='acceptEdits')
            battery.cmd_prepare(args)
            proposal = json.loads((root/'experiments'/'poly2'/'proposal.json').read_text())
            self.assertEqual(proposal['claude_permission_mode'], 'acceptEdits')

    def test_run_external_passes_proposal_permission_mode_to_claude_argv(self):
        with patch('battery._claude_argv', wraps=battery._claude_argv) as spy:
            fake_forge = SimpleNamespace(call=lambda *a, **k: {
                'output': '{"type": "result", "result": "ok"}', 'exitCode': 0,
            })
            with tempfile.TemporaryDirectory() as tmp:
                run_dir = Path(tmp)/'run'; run_dir.mkdir()
                (run_dir/'workspace').mkdir()
                battery._run_external('claude', run_dir, run_dir/'workspace', 'do it', 60, 'claude-x',
                                       fake_forge, 3.0, 'bypassPermissions')
            spy.assert_called_once_with('do it', 'claude-x', 3.0, 'bypassPermissions')


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

def _write_protocol(root, per_launch=1.0, cap=1000.0, max_launches=100, baseline=None, candidate=None,
                     max_parallel_timed_runs=None):
    root.mkdir(parents=True, exist_ok=True)
    for sub in ('experiments', 'reports', 'handoff'):
        (root/sub).mkdir(parents=True, exist_ok=True)
    limits = {'max_benchmark_worker_launches': max_launches, 'estimated_total_usd': cap, 'max_candidates': 100}
    if max_parallel_timed_runs is not None:
        limits['max_parallel_timed_runs'] = max_parallel_timed_runs
    (root/'protocol.json').write_text(json.dumps({
        'reservation_policy': {'per_launch_usd': per_launch},
        'limits': limits,
    }))
    # campaign._write_checkpoint() (called by battery.cmd_run) needs campaign.json's
    # sources block; a minimal stub is enough since we bypass campaign.py's full init.
    (root/'campaign.json').write_text(json.dumps({
        'campaign_id': 'battery-test',
        'sources': {
            'baseline_source': {'path': str(baseline or root), 'git_sha': None, 'tree_sha256': None},
            'candidate_worktree': {'path': str(candidate or root), 'git_sha': None, 'tree_sha256': None},
            'installed_cache': {'path': str(root), 'git_sha': None, 'tree_sha256': None},
        },
    }))


def fake_amplifier_launcher(amp_root, name):
    active = 'amplifier-fd' in name
    (amp_root/name).mkdir(parents=True, exist_ok=True)
    result = {
        'outcome_passed': True, 'wall_time_ms': 5000.0 if active else 9000.0, 'exit_code': 0,
        'timed_out': False, 'native': {'usage': {'cost_usd': None}},
        'effort_receipts': [{'model': 'claude-fable-5-1', 'count': 3}], 'model': 'claude-fable-5-1',
        'final_message': None, 'quality': {'checks': 3, 'passed': 3, 'failed': 0, 'failure_labels': []},
        'protected_files_unchanged': {'README.md': True, 'test_public.py': True},
        'started_at': 't0', 'ended_at': 't1', 'infrastructure_failure': False,
    }
    (amp_root/name/'result.json').write_text(json.dumps(result))


def fake_amplifier_waiter(amp_root, name, timeout):
    return (amp_root/name/'result.json').exists()


def fake_amplifier_closer(amp_root, name):
    pass


class RunTests(unittest.TestCase):
    def _prepared(self, tmp, harnesses, tasks='dev', per_launch=1.0, cap=1000.0, max_launches=100,
                  max_parallel_timed_runs=None, experiment='e1'):
        base = Path(tmp)
        baseline = base/'baseline'; candidate = base/'candidate'
        baseline.mkdir(exist_ok=True); candidate.mkdir(exist_ok=True)
        root = base/'campaign'
        args = _prepare_args(root, experiment, harnesses=harnesses, tasks=tasks,
                              baseline_source=str(baseline), candidate_source=str(candidate))
        battery.cmd_prepare(args)
        _write_protocol(root, per_launch=per_launch, cap=cap, max_launches=max_launches,
                         baseline=baseline, candidate=candidate,
                         max_parallel_timed_runs=max_parallel_timed_runs)
        return root

    def test_external_harnesses_dispatch_parse_and_settle(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = self._prepared(tmp, 'claude,codex,opencode', tasks='dev')
            forge_module, calls = make_fake_forge({'claude': {'output': CLAUDE_STDOUT, 'exitCode': 0},
                                                     'codex': codex_ok,
                                                     'opencode': {'output': OPENCODE_STDOUT, 'exitCode': 0}})
            battery.cmd_run(SimpleNamespace(root=str(root), experiment='e1'), forge_module=forge_module)

            manifest = json.loads((root/'experiments'/'e1'/'runs'/'manifest.json').read_text())
            results = {}
            for name, item in manifest['runs'].items():
                run_dir = battery._run_dir_for(root/'experiments'/'e1', name, item['harness'])
                results[name] = json.loads((run_dir/'result.json').read_text())

            claude_result = next(r for r in results.values() if r['harness'] == 'claude' and r['task'] == 'add_one')
            self.assertEqual(claude_result['cost_usd'], 0.42)
            self.assertTrue(claude_result['cost_billable'])
            self.assertEqual(claude_result['model'], 'claude-opus-5[1m]')

            codex_result = next(r for r in results.values() if r['harness'] == 'codex' and r['task'] == 'add_one')
            self.assertIsNotNone(codex_result['cost_usd'])
            self.assertEqual(codex_result['cost_source'], 'computed_from_tokens_estimate')

            opencode_result = next(r for r in results.values() if r['harness'] == 'opencode' and r['task'] == 'add_one')
            self.assertFalse(opencode_result['cost_billable'])
            self.assertEqual(opencode_result['cost_source'], 'gateway_internal_not_metered')

            ledger = [json.loads(line) for line in (root/'ledger.jsonl').read_text().splitlines()]
            settlements = {e['reservation']: e for e in ledger if e.get('type') == 'settlement'}
            reservations = {e['id']: e for e in ledger if e.get('type') == 'reservation'}
            # every reservation was settled
            self.assertEqual(set(reservations), set(settlements))
            # opencode settles at 0.0 (not billable here), never "unknown"
            opencode_reservation = next(rid for rid, e in reservations.items()
                                         if e['purpose'].endswith(f"{opencode_result['name']}"))
            self.assertNotIn('unknown', settlements[opencode_reservation])
            self.assertEqual(settlements[opencode_reservation]['actual_usd'], 0.0)

    def test_amplifier_harnesses_use_launcher_waiter_closer(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = self._prepared(tmp, 'amplifier-plain,amplifier-fd', tasks='dev')
            battery.cmd_run(SimpleNamespace(root=str(root), experiment='e1'),
                             launcher=fake_amplifier_launcher, waiter=fake_amplifier_waiter,
                             closer=fake_amplifier_closer)
            manifest = json.loads((root/'experiments'/'e1'/'runs'/'manifest.json').read_text())
            for name, item in manifest['runs'].items():
                run_dir = battery._run_dir_for(root/'experiments'/'e1', name, item['harness'])
                result = json.loads((run_dir/'result.json').read_text())
                self.assertTrue(result['outcome_passed'])
                self.assertEqual(result['cost_source'], 'unknown')  # native cost_usd was None
                self.assertFalse(result['infrastructure_failure'])

    def test_timeout_observation_marks_timed_out_not_completion(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = self._prepared(tmp, 'claude', tasks='dev')
            timeout_exc = SystemExit('forge: '+json.dumps({'timeout': True, 'exitCode': None, 'output': ''}))
            forge_module, _calls = make_fake_forge({'claude': timeout_exc})
            battery.cmd_run(SimpleNamespace(root=str(root), experiment='e1'), forge_module=forge_module)
            manifest = json.loads((root/'experiments'/'e1'/'runs'/'manifest.json').read_text())
            for name, item in manifest['runs'].items():
                run_dir = battery._run_dir_for(root/'experiments'/'e1', name, item['harness'])
                result = json.loads((run_dir/'result.json').read_text())
                self.assertTrue(result['timed_out'])
                self.assertFalse(result['outcome_passed'])
                self.assertFalse(result['infrastructure_failure'])

    def test_skips_completed_runs(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = self._prepared(tmp, 'claude', tasks='dev')
            experiment_dir = root/'experiments'/'e1'
            manifest = json.loads((experiment_dir/'runs'/'manifest.json').read_text())
            first_name = manifest['run_order'][0]
            run_dir = battery._run_dir_for(experiment_dir, first_name, manifest['runs'][first_name]['harness'])
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir/'result.json').write_text(json.dumps({'outcome_passed': True, 'name': first_name,
                                                             'harness': 'claude', 'task': manifest['runs'][first_name]['task']}))
            forge_module, calls = make_fake_forge({'claude': {'output': CLAUDE_STDOUT, 'exitCode': 0}})
            battery.cmd_run(SimpleNamespace(root=str(root), experiment='e1'), forge_module=forge_module)
            invoked_cwds = [c[1]['cwd'] for c in calls if c[0] == 'run_command']
            self.assertFalse(any(first_name in cwd for cwd in invoked_cwds))
            ledger = [json.loads(line) for line in (root/'ledger.jsonl').read_text().splitlines()]
            launched = [e['run'] for e in ledger if e.get('type') == 'run_launched']
            self.assertNotIn(first_name, launched)

    def test_infrastructure_failure_retries_once_then_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = self._prepared(tmp, 'claude', tasks='dev')
            attempts = {'n': 0}

            def flaky(args):
                attempts['n'] += 1
                if attempts['n'] == 1:
                    raise RuntimeError('simulated launch crash')
                return {'output': CLAUDE_STDOUT, 'exitCode': 0}
            forge_module, _calls = make_fake_forge({'claude': flaky})
            battery.cmd_run(SimpleNamespace(root=str(root), experiment='e1'), forge_module=forge_module)
            manifest = json.loads((root/'experiments'/'e1'/'runs'/'manifest.json').read_text())
            retried = [n for n in manifest['run_order'] if n.endswith('-a2')]
            self.assertEqual(len(retried), 1)
            retry_run_dir = battery._run_dir_for(root/'experiments'/'e1', retried[0],
                                                  manifest['runs'][retried[0]]['harness'])
            retry_result = json.loads((retry_run_dir/'result.json').read_text())
            # The retry attempt reached the (fake) harness successfully -- outcome_passed depends
            # on the fake claude call actually editing solution.py, which it deliberately does not.
            self.assertFalse(retry_result['infrastructure_failure'])
            self.assertEqual(retry_result['exit_code'], 0)
            self.assertEqual(retry_result['attempt'], 2)
            ledger = [json.loads(line) for line in (root/'ledger.jsonl').read_text().splitlines()]
            self.assertTrue(any(e.get('type') == 'infrastructure_retry_scheduled' for e in ledger))

    def test_amplifier_retry_carries_prompt_and_deadline(self):
        """Defect 1: a retried amplifier run must also carry the battery task's
        prompt into the sub-manifest (add_run), not just the initial prepare."""
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            import forge_workloads
            root = self._prepared(tmp, 'amplifier-fd', tasks='dev')
            attempts = {'n': 0}

            def flaky_launcher(amp_root, name):
                attempts['n'] += 1
                if attempts['n'] == 1:
                    raise RuntimeError('simulated launch crash')
                fake_amplifier_launcher(amp_root, name)

            battery.cmd_run(SimpleNamespace(root=str(root), experiment='e1'),
                             launcher=flaky_launcher, waiter=fake_amplifier_waiter,
                             closer=fake_amplifier_closer)
            experiment_dir = root/'experiments'/'e1'
            manifest = json.loads((experiment_dir/'runs'/'manifest.json').read_text())
            retried = [n for n in manifest['run_order'] if n.endswith('-a2')]
            self.assertEqual(len(retried), 1)
            amp_manifest = json.loads((experiment_dir/'runs'/'amplifier'/'manifest.json').read_text())
            retry_item = amp_manifest['runs'][retried[0]]
            expected_prompt = forge_workloads.task_prompt(retry_item['task'])
            self.assertEqual(retry_item['prompt'], expected_prompt)
            self.assertEqual(retry_item['deadline_seconds'], 60)

    def test_budget_refusal_pauses(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = self._prepared(tmp, 'claude', tasks='dev', per_launch=50.0, cap=10.0)
            forge_module, _calls = make_fake_forge({'claude': {'output': CLAUDE_STDOUT, 'exitCode': 0}})
            with self.assertRaises(SystemExit) as ctx:
                battery.cmd_run(SimpleNamespace(root=str(root), experiment='e1'), forge_module=forge_module)
            self.assertEqual(ctx.exception.code, 3)
            ledger = [json.loads(line) for line in (root/'ledger.jsonl').read_text().splitlines()]
            self.assertTrue(any(e.get('type') == 'paused' and e.get('reason') == 'budget' for e in ledger))

    def test_launch_cap_pauses(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = self._prepared(tmp, 'claude', tasks='dev', max_launches=0)
            forge_module, _calls = make_fake_forge({'claude': {'output': CLAUDE_STDOUT, 'exitCode': 0}})
            with self.assertRaises(SystemExit) as ctx:
                battery.cmd_run(SimpleNamespace(root=str(root), experiment='e1'), forge_module=forge_module)
            self.assertEqual(ctx.exception.code, 3)
            ledger = [json.loads(line) for line in (root/'ledger.jsonl').read_text().splitlines()]
            self.assertTrue(any(e.get('type') == 'paused' and e.get('reason') == 'launch_cap' for e in ledger))

    def test_circuit_breaker_trips_after_consecutive_infra_failures(self):
        """Defect: a bursty scheduler ran 35 experiments of garbage after Forge
        became unreachable, because nothing stopped it. Every dispatch here
        raises (simulated launch crash) -- with --max-consecutive-infra-failures
        set to 2, the THIRD launch must never happen: cmd_run stops launching,
        pauses resumably (exit 3), and records the streak + last failure text."""
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = self._prepared(tmp, 'claude', tasks='dev')  # 2 tasks -> up to 4 names with retries
            attempts = {'n': 0}

            def always_fails(args):
                attempts['n'] += 1
                raise RuntimeError(f'simulated launch crash #{attempts["n"]}')
            forge_module, _calls = make_fake_forge({'claude': always_fails})
            with self.assertRaises(SystemExit) as ctx:
                battery.cmd_run(SimpleNamespace(root=str(root), experiment='e1',
                                                 max_consecutive_infra_failures=2),
                                 forge_module=forge_module)
            self.assertEqual(ctx.exception.code, 3)
            # The breaker must stop launching BEFORE exhausting every scheduled name:
            # at most 2 real launches happened before it tripped.
            self.assertLessEqual(attempts['n'], 2)
            ledger = [json.loads(line) for line in (root/'ledger.jsonl').read_text().splitlines()]
            paused = [e for e in ledger if e.get('type') == 'paused' and e.get('reason') == 'consecutive_infra_failures']
            self.assertEqual(len(paused), 1)
            self.assertEqual(paused[0]['consecutive_infra_failures'], 2)
            self.assertIn('simulated launch crash', paused[0]['last_failure'])
            # Resumable: the manifest still has unlaunched retry names queued up,
            # never marked launched, ready for a later `battery run` to pick up.
            manifest = json.loads((root/'experiments'/'e1'/'runs'/'manifest.json').read_text())
            launched_names = {e['run'] for e in ledger if e.get('type') == 'run_launched'}
            self.assertTrue(set(manifest['run_order']) - launched_names)

    def test_circuit_breaker_default_is_five_and_resets_on_success(self):
        """The default threshold (5) must not fire on the existing one-infra-
        failure-then-succeeds retry path (test_infrastructure_failure_retries_once_
        then_succeeds already covers success outright; this asserts the counter
        itself resets rather than accumulating across unrelated successes)."""
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = self._prepared(tmp, 'claude', tasks='dev')
            attempts = {'n': 0}

            def flaky_once_per_task(args):
                attempts['n'] += 1
                if attempts['n'] in (1, 3):
                    raise RuntimeError('simulated launch crash')
                return {'output': CLAUDE_STDOUT, 'exitCode': 0}
            forge_module, _calls = make_fake_forge({'claude': flaky_once_per_task})
            # Must complete normally (no SystemExit) -- two ISOLATED infra failures,
            # each immediately followed by a success, never reach the default streak of 5.
            battery.cmd_run(SimpleNamespace(root=str(root), experiment='e1'), forge_module=forge_module)
            ledger = [json.loads(line) for line in (root/'ledger.jsonl').read_text().splitlines()]
            self.assertFalse(any(e.get('type') == 'paused' and e.get('reason') == 'consecutive_infra_failures'
                                  for e in ledger))


# --------------------------------------------------------------------------
# --parallel N: bounded-concurrency scheduler (still single-writer for the
# ledger/manifest; only the blocking dispatch itself overlaps)
# --------------------------------------------------------------------------

def _tracking_amplifier_launcher(tracker, delay=0.05):
    """Fake launcher that records how many launches are concurrently "in the
    harness" (between incrementing and decrementing `tracker['current']`),
    sleeping just long enough that --parallel > 1 actually overlaps two of
    these in real wall-clock time instead of finishing before the next one
    is even submitted."""
    def launcher(amp_root, name):
        with tracker['lock']:
            tracker['current'] += 1
            tracker['max_seen'] = max(tracker['max_seen'], tracker['current'])
            tracker['order'].append(name)
        time.sleep(delay)
        (amp_root/name).mkdir(parents=True, exist_ok=True)
        result = {
            'outcome_passed': True, 'wall_time_ms': 1000.0, 'exit_code': 0,
            'timed_out': False, 'native': {'usage': {'cost_usd': None}},
            'effort_receipts': [], 'model': 'claude-fable-5-1',
            'final_message': None, 'quality': {'checks': 1, 'passed': 1, 'failed': 0, 'failure_labels': []},
            'protected_files_unchanged': {'README.md': True},
            'started_at': 't0', 'ended_at': 't1', 'infrastructure_failure': False,
        }
        (amp_root/name/'result.json').write_text(json.dumps(result))
        with tracker['lock']:
            tracker['current'] -= 1
    return launcher


class ParallelSchedulerTests(unittest.TestCase):
    def _prepared(self, tmp, harnesses, tasks='all', max_parallel_timed_runs=None):
        return RunTests._prepared(self, tmp, harnesses, tasks=tasks,
                                   max_parallel_timed_runs=max_parallel_timed_runs)

    def test_parallel_never_exceeds_requested_bound(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = self._prepared(tmp, 'amplifier-plain,amplifier-fd', tasks='all',
                                   max_parallel_timed_runs=3)
            tracker = {'lock': threading.Lock(), 'current': 0, 'max_seen': 0, 'order': []}
            battery.cmd_run(SimpleNamespace(root=str(root), experiment='e1', parallel=3),
                             launcher=_tracking_amplifier_launcher(tracker),
                             waiter=fake_amplifier_waiter, closer=fake_amplifier_closer)
            self.assertLessEqual(tracker['max_seen'], 3)
            self.assertGreater(tracker['max_seen'], 1, 'test is meaningless if nothing ever overlapped')
            manifest = json.loads((root/'experiments'/'e1'/'runs'/'manifest.json').read_text())
            self.assertEqual(len(tracker['order']), len(manifest['run_order']))

    def test_pairs_of_same_task_launch_in_the_same_wave(self):
        """cmd_prepare's frozen schedule already groups every harness for a task
        consecutively (see cmd_prepare); the scheduler must preserve that
        adjacency instead of reordering it, so a task's amplifier-plain and
        amplifier-fd runs launch back to back, not scattered across waves."""
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = self._prepared(tmp, 'amplifier-plain,amplifier-fd', tasks='all',
                                   max_parallel_timed_runs=2)
            tracker = {'lock': threading.Lock(), 'current': 0, 'max_seen': 0, 'order': []}
            battery.cmd_run(SimpleNamespace(root=str(root), experiment='e1', parallel=2),
                             launcher=_tracking_amplifier_launcher(tracker),
                             waiter=fake_amplifier_waiter, closer=fake_amplifier_closer)
            manifest = json.loads((root/'experiments'/'e1'/'runs'/'manifest.json').read_text())
            position = {name: i for i, name in enumerate(tracker['order'])}
            by_task = {}
            for name, item in manifest['runs'].items():
                by_task.setdefault(item['task'], []).append(name)
            for task, names in by_task.items():
                self.assertEqual(len(names), 2, task)
                positions = sorted(position[n] for n in names)
                self.assertEqual(positions[1]-positions[0], 1,
                                  f'{task}: launch positions {positions} not adjacent')

    def test_parallel_one_output_matches_sequential_fields(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = self._prepared(tmp, 'claude', tasks='dev', max_parallel_timed_runs=1)
            forge_module, _calls = make_fake_forge({'claude': {'output': CLAUDE_STDOUT, 'exitCode': 0}})
            battery.cmd_run(SimpleNamespace(root=str(root), experiment='e1', parallel=1),
                             forge_module=forge_module)
            manifest = json.loads((root/'experiments'/'e1'/'runs'/'manifest.json').read_text())
            for name, item in manifest['runs'].items():
                run_dir = battery._run_dir_for(root/'experiments'/'e1', name, item['harness'])
                result = json.loads((run_dir/'result.json').read_text())
                self.assertEqual(result['concurrency_at_launch'], 1)
                self.assertEqual(result['concurrency_max'], 1)
                self.assertIn(result['outcome_passed'], (True, False))  # field present
            add_one_result = json.loads((battery._run_dir_for(
                root/'experiments'/'e1', 'e1-add_one-claude-a1', 'claude')/'result.json').read_text())
            self.assertEqual(add_one_result['cost_usd'], 0.42)

    def test_concurrency_fields_present_on_every_result(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = self._prepared(tmp, 'amplifier-plain,amplifier-fd', tasks='dev',
                                   max_parallel_timed_runs=2)
            tracker = {'lock': threading.Lock(), 'current': 0, 'max_seen': 0, 'order': []}
            battery.cmd_run(SimpleNamespace(root=str(root), experiment='e1', parallel=2),
                             launcher=_tracking_amplifier_launcher(tracker),
                             waiter=fake_amplifier_waiter, closer=fake_amplifier_closer)
            manifest = json.loads((root/'experiments'/'e1'/'runs'/'manifest.json').read_text())
            for name, item in manifest['runs'].items():
                run_dir = battery._run_dir_for(root/'experiments'/'e1', name, item['harness'])
                result = json.loads((run_dir/'result.json').read_text())
                self.assertIn('concurrency_at_launch', result)
                self.assertIn('concurrency_max', result)
                self.assertIsInstance(result['concurrency_at_launch'], int)
                self.assertIsInstance(result['concurrency_max'], int)
                self.assertGreaterEqual(result['concurrency_max'], result['concurrency_at_launch'])

    def test_protocol_guard_refuses_parallel_above_limit(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = self._prepared(tmp, 'claude', tasks='dev', max_parallel_timed_runs=2)
            forge_module, _calls = make_fake_forge({'claude': {'output': CLAUDE_STDOUT, 'exitCode': 0}})
            with self.assertRaises(SystemExit) as ctx:
                battery.cmd_run(SimpleNamespace(root=str(root), experiment='e1', parallel=3),
                                 forge_module=forge_module)
            self.assertEqual(ctx.exception.code, 4)

    def test_protocol_guard_defaults_limit_to_one(self):
        """No max_parallel_timed_runs recorded in protocol.json (older/unwired
        campaigns) -> the same conservative default (1) campaign.py itself uses."""
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = self._prepared(tmp, 'claude', tasks='dev', max_parallel_timed_runs=None)
            forge_module, _calls = make_fake_forge({'claude': {'output': CLAUDE_STDOUT, 'exitCode': 0}})
            with self.assertRaises(SystemExit) as ctx:
                battery.cmd_run(SimpleNamespace(root=str(root), experiment='e1', parallel=2),
                                 forge_module=forge_module)
            self.assertEqual(ctx.exception.code, 4)


# --------------------------------------------------------------------------
# evaluate (hand-built experiment tree; bypasses cmd_prepare/cmd_run entirely)
# --------------------------------------------------------------------------

class EvaluateTests(unittest.TestCase):
    def test_per_harness_and_paired_math(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = Path(tmp)/'campaign'
            experiment_dir = root/'experiments'/'e1'
            runs_root = experiment_dir/'runs'
            runs_root.mkdir(parents=True)
            (runs_root/'amplifier').mkdir(parents=True)

            # Two tasks (add_one=dev, double=holdout), two harnesses (amplifier-fd, amplifier-plain).
            # fd wins on add_one (2000 < 4000), fd loses on double (5000 > 3000).
            fixtures = [
                ('e1-add_one-amplifier-fd-a1', 'add_one', 'amplifier-fd', True, 2000.0, 0.10),
                ('e1-add_one-amplifier-plain-a1', 'add_one', 'amplifier-plain', True, 4000.0, 0.20),
                ('e1-double-amplifier-fd-a1', 'double', 'amplifier-fd', True, 5000.0, 0.10),
                ('e1-double-amplifier-plain-a1', 'double', 'amplifier-plain', True, 3000.0, 0.20),
            ]
            run_order, runs = [], {}
            for name, task, harness, passed, ms, cost in fixtures:
                run_dir = runs_root/'amplifier'/name if harness.startswith('amplifier') else runs_root/name
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir/'result.json').write_text(json.dumps({
                    'name': name, 'task': task, 'harness': harness, 'family': 'arith', 'split': 'dev',
                    'outcome_passed': passed, 'wall_time_ms': ms, 'cost_usd': cost, 'timed_out': False,
                }))
                run_order.append(name)
                runs[name] = {'name': name, 'task': task, 'harness': harness, 'attempt': 1}
            (runs_root/'manifest.json').write_text(json.dumps({'run_order': run_order, 'runs': runs,
                                                                 'deadline_seconds': 600}))
            (experiment_dir/'proposal.json').write_text(json.dumps({
                'experiment_id': 'e1', 'dev_tasks': ['add_one'], 'holdout_tasks': ['double']}))

            comparison = battery.cmd_evaluate(SimpleNamespace(root=str(root), experiment='e1'))
            self.assertEqual(comparison['per_harness']['amplifier-fd']['n'], 2)
            self.assertEqual(comparison['per_harness']['amplifier-fd']['success_rate'], 1.0)
            paired = comparison['amplifier_fd_vs_plain']
            self.assertEqual(paired['wins'], 1)
            self.assertEqual(paired['losses'], 1)
            self.assertAlmostEqual(paired['geomean_ratio'], (2000/4000*5000/3000)**0.5)
            self.assertIn('add_one', comparison['dev'])
            self.assertIn('double', comparison['holdout'])
            self.assertEqual(comparison['per_task']['add_one']['fastest_passing_harness'], 'amplifier-fd')
            self.assertEqual(comparison['per_task']['double']['fastest_passing_harness'], 'amplifier-plain')
            self.assertAlmostEqual(paired['cost_ratio'], (0.10+0.10)/(0.20+0.20))


def _write_profile(run_dir, loop_config):
    """Minimal profile.md matching forge_e2e._build_run's own '---\n<json>\n---\n' shape,
    with just enough structure for battery._profile_loop_config to read back the
    orchestrator config (backend/model/model_routing/effort_routing)."""
    profile = {'session': {'orchestrator': {'module': 'loop-fast-decisions', 'config': loop_config}}}
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir/'profile.md').write_text('---\n'+json.dumps(profile)+'\n---\n')


def _write_receipts(run_dir, events):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir/'receipts.jsonl').write_text('\n'.join(json.dumps(e) for e in events)+'\n' if events else '')


def _one_amplifier_fd_experiment(root, experiment, loop_config, receipt_events):
    """A single-run amplifier-fd experiment (proposal/manifest/result/receipts/profile),
    ready for battery.cmd_evaluate. Returns experiment_dir."""
    experiment_dir = root/'experiments'/experiment
    runs_root = experiment_dir/'runs'
    name = f'{experiment}-add_one-amplifier-fd-a1'
    run_dir = runs_root/'amplifier'/name
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir/'result.json').write_text(json.dumps({
        'name': name, 'task': 'add_one', 'harness': 'amplifier-fd', 'family': 'arith', 'split': 'dev',
        'outcome_passed': True, 'wall_time_ms': 1000.0, 'cost_usd': 0.10, 'timed_out': False,
    }))
    _write_profile(run_dir, loop_config)
    _write_receipts(run_dir, receipt_events)
    (runs_root/'manifest.json').write_text(json.dumps({
        'run_order': [name], 'runs': {name: {'name': name, 'task': 'add_one', 'harness': 'amplifier-fd', 'attempt': 1}},
        'deadline_seconds': 600}))
    (experiment_dir/'proposal.json').write_text(json.dumps({
        'experiment_id': experiment, 'dev_tasks': ['add_one'], 'holdout_tasks': []}))
    return experiment_dir


class MechanismGateTests(unittest.TestCase):
    """Defect (verified in campaign data): an amplifier-fd run's fast-decisions backend can
    be requested (e.g. jev) yet never actually score anything (every request fell back) --
    the paired win/loss comparison is meaningless if the mechanism never engaged. See
    docs/EVENTS.md for the `fast_decisions:*` receipt vocabulary this reads.
    """

    def test_external_backend_never_scored_sets_mechanism_engaged_false(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = Path(tmp)/'campaign'
            loop_config = {'backend': 'jev', 'model': 'jev-remote'}
            events = [{'event': 'fast_decisions:fallback',
                       'data': {'backend': 'jev', 'reason_code': 'external_state_not_enabled'}}
                      for _ in range(9)]
            _one_amplifier_fd_experiment(root, 'j1', loop_config, events)
            comparison = battery.cmd_evaluate(SimpleNamespace(root=str(root), experiment='j1'))
            mechanism = comparison['mechanism']
            self.assertFalse(mechanism['mechanism_engaged'])
            self.assertIn("external backend 'jev' refused/never scored", mechanism['mechanism_reason'])
            self.assertEqual(mechanism['fallback_count'], 9)
            self.assertEqual(mechanism['scored_by_backend'], {})
            report = (root/'experiments'/'j1'/'REPORT.md').read_text()
            self.assertIn('WARNING: mechanism_engaged=false', report)

    def test_default_backend_scored_sets_mechanism_engaged_true(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = Path(tmp)/'campaign'
            loop_config = {'backend': 'ollama', 'model': 'qwen3:0.6b'}
            events = [
                {'event': 'fast_decisions:scored', 'data': {'backend': 'ollama'}},
                {'event': 'fast_decisions:routed', 'data': {'backend': 'ollama', 'route': 'fast'}},
                {'event': 'fast_decisions:routed', 'data': {'backend': 'ollama', 'route': 'slow'}},
            ]
            _one_amplifier_fd_experiment(root, 'ok1', loop_config, events)
            comparison = battery.cmd_evaluate(SimpleNamespace(root=str(root), experiment='ok1'))
            mechanism = comparison['mechanism']
            self.assertTrue(mechanism['mechanism_engaged'])
            self.assertIsNone(mechanism['mechanism_reason'])
            self.assertEqual(mechanism['scored_by_backend'], {'ollama': 1})
            self.assertEqual(mechanism['routed_by_route'], {'fast': 1, 'slow': 1})
            report = (root/'experiments'/'ok1'/'REPORT.md').read_text()
            self.assertNotIn('WARNING', report)

    def test_model_routing_configured_but_never_applied_sets_mechanism_engaged_false(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = Path(tmp)/'campaign'
            loop_config = {'backend': 'ollama', 'model_routing': {'start_model': 'claude-cheap-1'}}
            events = [{'event': 'fast_decisions:scored', 'data': {'backend': 'ollama'}}]
            _one_amplifier_fd_experiment(root, 'mr1', loop_config, events)
            comparison = battery.cmd_evaluate(SimpleNamespace(root=str(root), experiment='mr1'))
            mechanism = comparison['mechanism']
            self.assertFalse(mechanism['mechanism_engaged'])
            self.assertIn('model routing configured', mechanism['mechanism_reason'])
            self.assertEqual(mechanism['model_routed_requested_models'], {})

    def test_model_routing_applied_sets_mechanism_engaged_true(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = Path(tmp)/'campaign'
            loop_config = {'backend': 'ollama', 'model_routing': {'start_model': 'claude-cheap-1'}}
            events = [
                {'event': 'fast_decisions:scored', 'data': {'backend': 'ollama'}},
                {'event': 'fast_decisions:model_routed',
                 'data': {'requested_model': 'claude-cheap-1', 'escalated': False, 'phase': 'orient'}},
                {'event': 'fast_decisions:model_routed',
                 'data': {'requested_model': None, 'escalated': True, 'escalation_reason': 'max_requests', 'phase': 'implement'}},
            ]
            _one_amplifier_fd_experiment(root, 'mr2', loop_config, events)
            comparison = battery.cmd_evaluate(SimpleNamespace(root=str(root), experiment='mr2'))
            mechanism = comparison['mechanism']
            self.assertTrue(mechanism['mechanism_engaged'])
            self.assertEqual(mechanism['model_routed_requested_models'], {'claude-cheap-1': 1, None: 1})
            self.assertEqual(mechanism['model_routed_escalations_by_reason'], {'max_requests': 1})

    def test_unavailable_backend_with_scores_sets_mechanism_engaged_false(self):
        """Spec section 6a: a judge-off cell (backend='unavailable') that somehow
        scored anyway is a defect in the other direction -- the judge was supposed
        to be off and was not."""
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = Path(tmp)/'campaign'
            loop_config = {'backend': 'unavailable'}
            events = [{'event': 'fast_decisions:scored', 'data': {'backend': 'unavailable'}}]
            _one_amplifier_fd_experiment(root, 'u1', loop_config, events)
            comparison = battery.cmd_evaluate(SimpleNamespace(root=str(root), experiment='u1'))
            mechanism = comparison['mechanism']
            self.assertFalse(mechanism['mechanism_engaged'])
            self.assertIn("judge configured 'unavailable' (off) but scored=1", mechanism['mechanism_reason'])

    def test_unavailable_backend_with_no_scores_sets_mechanism_engaged_true(self):
        """Spec section 6a: a deliberate judge-off cell (backend='unavailable') that
        never scored anything is engaged (the off-switch verified, not just tolerated).
        effort-only cells rely on this to run at all."""
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = Path(tmp)/'campaign'
            loop_config = {'backend': 'unavailable', 'effort_routing': {'implement': 'high'}}
            events = [{'event': 'fast_decisions:effort_routed',
                       'data': {'phase': 'implement', 'requested_effort': 'high'}}]
            _one_amplifier_fd_experiment(root, 'u2', loop_config, events)
            comparison = battery.cmd_evaluate(SimpleNamespace(root=str(root), experiment='u2'))
            mechanism = comparison['mechanism']
            self.assertTrue(mechanism['mechanism_engaged'])
            self.assertIsNone(mechanism['mechanism_reason'])
            self.assertEqual(mechanism['scored_by_backend'], {})
            self.assertEqual(mechanism['effort_routed_by_phase_effort'], {'implement:high': 1})

    def test_judged_engaged_none_when_neither_judge_mechanism_configured(self):
        """HC05: a cell that configures neither escalation_judge nor
        phase_judge reports judged_engaged=None (not applicable) -- this
        MUST NOT be confused with judged_engaged=False (configured but
        never fired)."""
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = Path(tmp)/'campaign'
            loop_config = {'backend': 'ollama', 'model_routing': {'start_model': 'claude-cheap-1'}}
            events = [{'event': 'fast_decisions:scored', 'data': {'backend': 'ollama'}},
                      {'event': 'fast_decisions:model_routed',
                       'data': {'requested_model': 'claude-cheap-1', 'escalated': False}}]
            _one_amplifier_fd_experiment(root, 'nj1', loop_config, events)
            comparison = battery.cmd_evaluate(SimpleNamespace(root=str(root), experiment='nj1'))
            mechanism = comparison['mechanism']
            self.assertIsNone(mechanism['judged_engaged'])
            self.assertIsNone(mechanism['judged_reason'])

    def test_judged_engaged_false_when_escalation_judge_configured_but_never_fired(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = Path(tmp)/'campaign'
            loop_config = {'backend': 'ollama',
                            'model_routing': {'start_model': 'claude-sonnet-5', 'escalation_judge': 'judge'}}
            events = [{'event': 'fast_decisions:scored', 'data': {'backend': 'ollama'}},
                      {'event': 'fast_decisions:model_routed',
                       'data': {'requested_model': 'claude-sonnet-5', 'escalated': False}}]
            _one_amplifier_fd_experiment(root, 'ej1', loop_config, events)
            comparison = battery.cmd_evaluate(SimpleNamespace(root=str(root), experiment='ej1'))
            mechanism = comparison['mechanism']
            self.assertFalse(mechanism['judged_engaged'])
            self.assertIn('escalation_judge configured but 0 escalation_judged receipts', mechanism['judged_reason'])

    def test_judged_engaged_false_when_escalation_judged_from_wrong_backend(self):
        """The exact defect class R4 already guards for scored/model_routed:
        a judged receipt that came from a DIFFERENT backend than the cell's
        own configured_backend must not be credited."""
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = Path(tmp)/'campaign'
            loop_config = {'backend': 'ollama',
                            'model_routing': {'start_model': 'claude-sonnet-5', 'escalation_judge': 'judge'}}
            events = [{'event': 'fast_decisions:scored', 'data': {'backend': 'ollama'}},
                      {'event': 'fast_decisions:escalation_judged',
                       'data': {'backend': 'jev', 'choice': 'escalate', 'decided': 'escalate'}}]
            _one_amplifier_fd_experiment(root, 'ej2', loop_config, events)
            comparison = battery.cmd_evaluate(SimpleNamespace(root=str(root), experiment='ej2'))
            mechanism = comparison['mechanism']
            self.assertFalse(mechanism['judged_engaged'])
            self.assertIn('different backend than configured', mechanism['judged_reason'])
            self.assertEqual(mechanism['escalation_judged_by_backend'], {'jev': 1})

    def test_judged_engaged_true_when_escalation_judge_fired_on_configured_backend(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = Path(tmp)/'campaign'
            loop_config = {'backend': 'ollama',
                            'model_routing': {'start_model': 'claude-sonnet-5', 'escalation_judge': 'judge'}}
            events = [{'event': 'fast_decisions:scored', 'data': {'backend': 'ollama'}},
                      {'event': 'fast_decisions:escalation_judged',
                       'data': {'backend': 'ollama', 'choice': 'continue_cheap', 'decided': 'continue'}}]
            _one_amplifier_fd_experiment(root, 'ej3', loop_config, events)
            comparison = battery.cmd_evaluate(SimpleNamespace(root=str(root), experiment='ej3'))
            mechanism = comparison['mechanism']
            self.assertTrue(mechanism['judged_engaged'])
            self.assertIsNone(mechanism['judged_reason'])
            self.assertEqual(mechanism['escalation_judged_by_backend'], {'ollama': 1})
            self.assertEqual(mechanism['escalation_judged_by_decided'], {'continue': 1})

    def test_judged_engaged_false_when_phase_judge_configured_but_never_fired(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = Path(tmp)/'campaign'
            loop_config = {'backend': 'ollama', 'effort_routing': {'implement': 'high', 'phase_judge': True}}
            events = [{'event': 'fast_decisions:effort_routed', 'data': {'phase': 'implement', 'requested_effort': 'high'}}]
            _one_amplifier_fd_experiment(root, 'pj1', loop_config, events)
            comparison = battery.cmd_evaluate(SimpleNamespace(root=str(root), experiment='pj1'))
            mechanism = comparison['mechanism']
            self.assertFalse(mechanism['judged_engaged'])
            self.assertIn('phase_judge configured but 0 phase_judged receipts', mechanism['judged_reason'])

    def test_judged_engaged_true_when_phase_judge_fired_with_agreement_counts(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = Path(tmp)/'campaign'
            loop_config = {'backend': 'ollama', 'effort_routing': {'implement': 'high', 'phase_judge': True}}
            events = [
                {'event': 'fast_decisions:phase_judged',
                 'data': {'backend': 'ollama', 'choice': 'implement', 'agreed_with_rules': True}},
                {'event': 'fast_decisions:phase_judged',
                 'data': {'backend': 'ollama', 'choice': 'explore', 'agreed_with_rules': False}},
            ]
            _one_amplifier_fd_experiment(root, 'pj2', loop_config, events)
            comparison = battery.cmd_evaluate(SimpleNamespace(root=str(root), experiment='pj2'))
            mechanism = comparison['mechanism']
            self.assertTrue(mechanism['judged_engaged'])
            self.assertEqual(mechanism['phase_judged_by_backend'], {'ollama': 2})
            self.assertEqual(mechanism['phase_judged_agreement'], {'agreed': 1, 'disagreed': 1})

    def test_judge_decision_latency_included_in_p95(self):
        """escalation_judged/phase_judged carry duration_ms/backend from the
        same _ask_judge_choice call site, so they feed the generic latency
        aggregation exactly like scored/fallback (_LATENCY_EVENT_KINDS)."""
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = Path(tmp)/'campaign'
            loop_config = {'backend': 'jev',
                            'model_routing': {'start_model': 'claude-sonnet-5', 'escalation_judge': 'judge'}}
            events = [{'event': 'fast_decisions:scored', 'data': {'backend': 'jev', 'duration_ms': 10.0}},
                      {'event': 'fast_decisions:escalation_judged',
                       'data': {'backend': 'jev', 'choice': 'escalate', 'decided': 'escalate', 'duration_ms': 20.0}}]
            _one_amplifier_fd_experiment(root, 'lat1', loop_config, events)
            comparison = battery.cmd_evaluate(SimpleNamespace(root=str(root), experiment='lat1'))
            mechanism = comparison['mechanism']
            self.assertEqual(mechanism['decision_latency_ms_by_backend']['jev']['n'], 2)

    def test_no_amplifier_fd_runs_means_mechanism_is_none(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = Path(tmp)/'campaign'
            experiment_dir = root/'experiments'/'noamp'
            runs_root = experiment_dir/'runs'
            run_dir = runs_root/'claude-add_one'
            run_dir.mkdir(parents=True)
            (run_dir/'result.json').write_text(json.dumps({
                'name': 'claude-add_one', 'task': 'add_one', 'harness': 'claude', 'family': 'arith', 'split': 'dev',
                'outcome_passed': True, 'wall_time_ms': 1000.0, 'cost_usd': 0.10, 'timed_out': False}))
            (runs_root/'manifest.json').write_text(json.dumps({
                'run_order': ['claude-add_one'],
                'runs': {'claude-add_one': {'name': 'claude-add_one', 'task': 'add_one', 'harness': 'claude', 'attempt': 1}},
                'deadline_seconds': 600}))
            (experiment_dir/'proposal.json').write_text(json.dumps({
                'experiment_id': 'noamp', 'dev_tasks': ['add_one'], 'holdout_tasks': []}))
            comparison = battery.cmd_evaluate(SimpleNamespace(root=str(root), experiment='noamp'))
            self.assertIsNone(comparison['mechanism'])
            self.assertIsNone(comparison['amplifier_fd_series_label'])
            report = (root/'experiments'/'noamp'/'REPORT.md').read_text()
            self.assertIn('not evaluated (no amplifier-fd runs)', report)


class DecisionLatencyTests(unittest.TestCase):
    """Evidence-gap fix: DESIGN-BRIDGE.md rule (b) needs a receipts-derived judge
    decision-latency p50/p95 to gate promoting an external judge backend. Real
    `fast_decisions:scored` receipts carry `duration_ms` (see docs/EVENTS.md /
    campaign receipts) -- these tests use synthetic receipts shaped the same way.
    """

    def test_p50_p95_computed_from_scored_receipt_duration_ms(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = Path(tmp)/'campaign'
            loop_config = {'backend': 'ollama', 'model': 'qwen3:0.6b'}
            # 10 scored receipts, duration_ms 10..100 in steps of 10 -> p50=60 (nearest-rank
            # at ceil(0.50*10)=5th smallest -> index 4 -> 50... use exact values below instead
            # of hand-deriving, and assert against the module's own _percentile for parity.
            durations = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
            events = [{'event': 'fast_decisions:scored',
                       'data': {'backend': 'ollama', 'duration_ms': d, 'latency_kind': 'decision_model_wall_time'}}
                      for d in durations]
            _one_amplifier_fd_experiment(root, 'lat1', loop_config, events)
            comparison = battery.cmd_evaluate(SimpleNamespace(root=str(root), experiment='lat1'))
            mechanism = comparison['mechanism']
            expected_p50 = battery._percentile(sorted(durations), 50)
            expected_p95 = battery._percentile(sorted(durations), 95)
            self.assertEqual(mechanism['decision_latency_ms_p50'], expected_p50)
            self.assertEqual(mechanism['decision_latency_ms_p95'], expected_p95)
            self.assertEqual(mechanism['decision_latency_budget_ms'], 500)
            self.assertTrue(mechanism['latency_within_budget'])
            self.assertIsNone(mechanism['decision_latency_reason'])
            self.assertEqual(mechanism['decision_latency_ms_by_backend']['ollama']['n'], 10)
            report = (root/'experiments'/'lat1'/'REPORT.md').read_text()
            self.assertIn('decision_latency_ms_p50=', report)
            self.assertIn('latency_within_budget=True', report)

    def test_latency_over_budget_sets_within_budget_false(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = Path(tmp)/'campaign'
            loop_config = {'backend': 'jev', 'model': 'jev-remote'}
            events = [{'event': 'fast_decisions:scored', 'data': {'backend': 'jev', 'duration_ms': 900.0}}
                      for _ in range(5)]
            _one_amplifier_fd_experiment(root, 'lat2', loop_config, events)
            comparison = battery.cmd_evaluate(SimpleNamespace(root=str(root), experiment='lat2'))
            mechanism = comparison['mechanism']
            self.assertEqual(mechanism['decision_latency_ms_p95'], 900.0)
            self.assertFalse(mechanism['latency_within_budget'])

    def test_per_backend_breakdown_keeps_backends_separate(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = Path(tmp)/'campaign'
            loop_config = {'backend': 'ollama'}
            events = [
                {'event': 'fast_decisions:scored', 'data': {'backend': 'ollama', 'duration_ms': 40.0}},
                {'event': 'fast_decisions:scored', 'data': {'backend': 'ollama', 'duration_ms': 60.0}},
                {'event': 'fast_decisions:scored', 'data': {'backend': 'jev', 'duration_ms': 800.0}},
            ]
            _one_amplifier_fd_experiment(root, 'lat3', loop_config, events)
            comparison = battery.cmd_evaluate(SimpleNamespace(root=str(root), experiment='lat3'))
            by_backend = comparison['mechanism']['decision_latency_ms_by_backend']
            self.assertEqual(by_backend['ollama']['n'], 2)
            self.assertEqual(by_backend['jev']['n'], 1)
            self.assertEqual(by_backend['jev']['p95'], 800.0)

    def test_no_latency_field_in_any_receipt_yields_null_not_invented(self):
        """No scored/fallback receipt carries duration_ms -- must report null with a
        reason, never fabricate a number."""
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = Path(tmp)/'campaign'
            loop_config = {'backend': 'ollama'}
            events = [
                {'event': 'fast_decisions:scored', 'data': {'backend': 'ollama'}},
                {'event': 'fast_decisions:routed', 'data': {'backend': 'ollama', 'route': 'fast'}},
            ]
            _one_amplifier_fd_experiment(root, 'lat4', loop_config, events)
            comparison = battery.cmd_evaluate(SimpleNamespace(root=str(root), experiment='lat4'))
            mechanism = comparison['mechanism']
            self.assertIsNone(mechanism['decision_latency_ms_p95'])
            self.assertIsNone(mechanism['decision_latency_ms_p50'])
            self.assertIsNone(mechanism['latency_within_budget'])
            self.assertIsNotNone(mechanism['decision_latency_reason'])
            report = (root/'experiments'/'lat4'/'REPORT.md').read_text()
            self.assertIn('decision_latency_ms_p95=None', report)
            self.assertIn('decision_latency:', report)

    def test_fallback_receipt_latency_is_picked_up_if_present(self):
        """Generic-by-kind check: a fallback receipt carrying duration_ms (not seen
        in real receipts as of this writing, but not hardcoded away either) must
        still be counted."""
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = Path(tmp)/'campaign'
            loop_config = {'backend': 'jev'}
            events = [{'event': 'fast_decisions:fallback', 'data': {'backend': 'jev', 'duration_ms': 250.0}}]
            _one_amplifier_fd_experiment(root, 'lat5', loop_config, events)
            comparison = battery.cmd_evaluate(SimpleNamespace(root=str(root), experiment='lat5'))
            mechanism = comparison['mechanism']
            self.assertEqual(mechanism['decision_latency_ms_p95'], 250.0)
            self.assertEqual(mechanism['decision_latency_ms_by_backend']['jev']['n'], 1)


class SeriesLabelTests(unittest.TestCase):
    def test_label_carries_backend_and_routing_flags(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = Path(tmp)/'campaign'
            loop_config = {'backend': 'ollama', 'model': 'qwen3:0.6b',
                           'effort_routing': {'explore': 'low'}, 'model_routing': None}
            events = [{'event': 'fast_decisions:scored', 'data': {'backend': 'ollama'}}]
            _one_amplifier_fd_experiment(root, 'lbl1', loop_config, events)
            comparison = battery.cmd_evaluate(SimpleNamespace(root=str(root), experiment='lbl1'))
            label = comparison['amplifier_fd_series_label']
            self.assertNotEqual(label, 'amplifier-fd')
            self.assertIn('judge=ollama qwen3:0.6b', label)
            self.assertIn('effort explore->low', label)
            self.assertIn('model routing: off', label)
            report = (root/'experiments'/'lbl1'/'REPORT.md').read_text()
            self.assertIn(label, report)


# --------------------------------------------------------------------------
# reevaluate: prompt_matches re-scoring (Re-scoring requirement, no re-runs)
# --------------------------------------------------------------------------

class PolyglotReevaluateTests(unittest.TestCase):
    """Confirms `reevaluate` re-runs the (fixed) polyglot language evaluator
    against a preserved workspace by resolving the task through the
    registered polyglot task source recorded in proposal.json -- so the
    ~20 finished POLY-BASE runs can be re-scored without re-running the
    harness."""

    def test_reevaluate_reruns_polyglot_evaluator_on_preserved_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            corpus = base/'polyglot-benchmark'
            _build_fake_polyglot_corpus(corpus)

            root = base/'campaign'
            experiment_dir = root/'experiments'/'e1'
            runs_root = experiment_dir/'runs'
            run_name = 'e1-poly_python_add-numbers-claude-a1'
            run_dir = runs_root/run_name
            workspace = run_dir/'workspace'
            workspace.mkdir(parents=True)
            # Agent's final workspace state: the reference solution (a correct
            # fix) plus the shipped (protected) test file, unmodified.
            (workspace/'add_numbers.py').write_text(_POLY_PY_EXAMPLE)
            (workspace/'add_numbers_test.py').write_text(_POLY_PY_TEST)

            # A prior (wrong) scoring -- e.g. from Defect A's harness_error --
            # incorrectly marked this correct solution as failed.
            (run_dir/'result.json').write_text(json.dumps({
                'name': run_name, 'task': 'poly_python_add-numbers', 'harness': 'claude', 'attempt': 1,
                'exit_code': 0, 'timed_out': False, 'infrastructure_failure': False,
                'outcome_passed': False,
                'quality': {'checks': 1, 'passed': 0, 'failed': 1, 'failure_labels': ['harness_error:boom']},
            }))
            runs_root.mkdir(parents=True, exist_ok=True)
            (runs_root/'manifest.json').write_text(json.dumps({
                'run_order': [run_name],
                'runs': {run_name: {'name': run_name, 'task': 'poly_python_add-numbers',
                                     'harness': 'claude', 'attempt': 1}},
                'deadline_seconds': 120,
            }))
            (experiment_dir/'proposal.json').write_text(json.dumps({
                'experiment_id': 'e1',
                'task_source': {'kind': 'polyglot', 'root': str(corpus), 'languages': ['python']},
            }))

            result = battery.cmd_reevaluate(SimpleNamespace(root=str(root), experiment='e1', reason='defect-a-fix'))

            self.assertEqual(len(result['changed']), 1, result)
            changed = result['changed'][0]
            self.assertEqual(changed['run'], run_name)
            self.assertTrue(changed['outcome_passed'])
            self.assertEqual(changed['failed_checks'], 0)

            new_result = json.loads((run_dir/'result.json').read_text())
            self.assertTrue(new_result['outcome_passed'])
            self.assertEqual(new_result['quality']['failed'], 0)
            self.assertTrue((run_dir/'result-before-reevaluate.json').exists())


class ReevaluatePromptMatchesTests(unittest.TestCase):
    def _base_experiment(self, tmp, name='amplifier-fd', prompt=None, prompt_sha256=None):
        with _patched_battery_tasks():
            pass  # noqa: keep the same import surface as other tests below
        root = Path(tmp)/'campaign'
        experiment_dir = root/'experiments'/'e1'
        runs_root = experiment_dir/'runs'
        run_name = 'e1-add_one-'+name+'-a1'
        run_dir = (runs_root/'amplifier'/run_name) if name in battery.AMPLIFIER_HARNESSES else (runs_root/run_name)
        workspace = run_dir/'workspace'
        workspace.mkdir(parents=True)
        (workspace/'README.md').write_text('Fix add_one.\n')
        (workspace/'solution.py').write_text('def add_one(x):\n    return x+1\n')
        (run_dir/'result.json').write_text(json.dumps({
            'name': run_name, 'task': 'add_one', 'harness': name, 'attempt': 1,
            'exit_code': 0, 'timed_out': False, 'infrastructure_failure': False,
            'outcome_passed': False, 'quality': {'checks': 0, 'passed': 0, 'failed': 0, 'failure_labels': []},
        }))
        (runs_root/'manifest.json').write_text(json.dumps({
            'run_order': [run_name],
            'runs': {run_name: {'name': run_name, 'task': 'add_one', 'harness': name, 'attempt': 1}},
            'deadline_seconds': 600,
        }))
        (experiment_dir/'proposal.json').write_text(json.dumps({'experiment_id': 'e1'}))
        campaign_stub = {'campaign_id': 'stub'}
        (root/'campaign.json').write_text(json.dumps(campaign_stub))
        if name in battery.AMPLIFIER_HARNESSES:
            amp_item = {'name': run_name, 'task': 'add_one', 'side': name, 'attempt': 1}
            if prompt is not None:
                amp_item['prompt'] = prompt
            if prompt_sha256 is not None:
                amp_item['prompt_sha256'] = prompt_sha256
            (runs_root/'amplifier').mkdir(parents=True, exist_ok=True)
            (runs_root/'amplifier'/'manifest.json').write_text(json.dumps({'runs': {run_name: amp_item}}))
        return root, run_name

    def test_prompt_matches_true_when_recorded_prompt_matches_sha256(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            prompt = 'Fix solution.py so add_one(x) returns x+1. Run python3 -m unittest -v test_public.py.'
            sha = hashlib.sha256(prompt.encode()).hexdigest()
            root, run_name = self._base_experiment(tmp, prompt=prompt, prompt_sha256=sha)
            result = battery.cmd_reevaluate(SimpleNamespace(root=str(root), experiment='e1'))
            check = next(c for c in result['prompt_checks'] if c['run'] == run_name)
            self.assertIs(check['prompt_matches'], True)
            self.assertIsNone(check['note'])

    def test_prompt_matches_false_when_sha256_disagrees(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root, run_name = self._base_experiment(tmp, prompt='tampered prompt', prompt_sha256='deadbeef')
            result = battery.cmd_reevaluate(SimpleNamespace(root=str(root), experiment='e1'))
            check = next(c for c in result['prompt_checks'] if c['run'] == run_name)
            self.assertIs(check['prompt_matches'], False)

    def test_prompt_matches_none_when_legacy_sub_manifest_has_no_prompt_field(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root, run_name = self._base_experiment(tmp)  # no prompt/prompt_sha256 recorded
            result = battery.cmd_reevaluate(SimpleNamespace(root=str(root), experiment='e1'))
            check = next(c for c in result['prompt_checks'] if c['run'] == run_name)
            self.assertIsNone(check['prompt_matches'])
            self.assertEqual(check['note'], 'prompt not recorded; generic prompt suspected')

    def test_prompt_matches_none_for_external_harness(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root, run_name = self._base_experiment(tmp, name='claude')
            result = battery.cmd_reevaluate(SimpleNamespace(root=str(root), experiment='e1'))
            check = next(c for c in result['prompt_checks'] if c['run'] == run_name)
            self.assertIsNone(check['prompt_matches'])
            self.assertEqual(check['note'], 'prompt_sha256 not tracked for external harnesses')


# --------------------------------------------------------------------------
# forge_e2e legacy behavior must survive the generalization
# --------------------------------------------------------------------------

class ForgeE2ELegacyStillWorksTests(unittest.TestCase):
    def test_build_workspace_still_writes_legacy_trio(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)/'run'
            workspace = forge_e2e._build_workspace(run_dir, 'scheduler')
            self.assertEqual((workspace/'README.md').read_text(), forge_e2e.SPECS['scheduler'])
            self.assertEqual((workspace/'solution.py').read_text(), forge_e2e.STARTERS['scheduler'])
            self.assertEqual((workspace/'test_public.py').read_text(), forge_e2e.PUBLIC['scheduler'])

    def test_task_kind_defaults_to_code_without_battery_tasks(self):
        self.assertEqual(forge_e2e._task_kind('scheduler'), 'code')

    def test_task_protected_and_files_fallback(self):
        self.assertEqual(forge_e2e._task_protected('scheduler'), ('README.md', 'test_public.py'))
        files = forge_e2e._task_files('scheduler')
        self.assertEqual(set(files), {'README.md', 'solution.py', 'test_public.py'})




class CodexConfigModelTests(unittest.TestCase):
    def test_codex_configured_model_from_toml(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp)/'config.toml'
            cfg.write_text('model = "gpt-6-astra"\nmodel_reasoning_effort = "high"\n')
            self.assertEqual(battery._codex_configured_model(cfg), 'gpt-6-astra')
            self.assertIsNone(battery._codex_configured_model(Path(tmp)/'missing.toml'))


# --------------------------------------------------------------------------
# compute_exec_time (pure helper) -- one case per harness-specific source,
# plus the universal wall-time fallback
# --------------------------------------------------------------------------

class ComputeExecTimeTests(unittest.TestCase):
    def test_amplifier_uses_native_events_first_request_to_last_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)/'home'
            run_dir = Path(tmp)/'run'
            workspace = run_dir/'workspace'
            workspace.mkdir(parents=True)
            (run_dir/'worker-result.json').write_text(json.dumps({'session_id': 'sess-1'}))
            slug = str(workspace.resolve()).replace('\\', '-').replace('/', '-').replace(':', '')
            sessions_dir = fake_home/'.amplifier/projects'/slug/'sessions'/'sess-1'
            sessions_dir.mkdir(parents=True)
            events = [
                {'type': 'llm:request', 'ts': '2026-01-01T00:00:00+00:00'},
                {'type': 'other'},
                {'type': 'llm:response', 'ts': '2026-01-01T00:00:01+00:00'},
                {'type': 'llm:request', 'ts': '2026-01-01T00:00:02+00:00'},
                {'type': 'llm:response', 'ts': '2026-01-01T00:00:05+00:00'},
            ]
            (sessions_dir/'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events)+'\n')
            result = {'harness': 'amplifier-fd', 'wall_time_ms': 99999.0}
            with patch('battery.Path.home', return_value=fake_home):
                ms, source = battery.compute_exec_time(result, run_dir)
                annotated = battery._with_exec_time(result, run_dir)
            self.assertEqual(source, 'native_events_first_request_to_last_response')
            self.assertAlmostEqual(ms, 5000.0)
            self.assertEqual(annotated['provider_requests'], 2)

    def test_amplifier_falls_back_to_wall_without_native_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)/'run'
            (run_dir/'workspace').mkdir(parents=True)
            result = {'harness': 'amplifier-plain', 'wall_time_ms': 4200.0}
            with patch('battery.Path.home', return_value=Path(tmp)/'nohome'):
                ms, source = battery.compute_exec_time(result, run_dir)
            self.assertEqual(ms, 4200.0)
            self.assertEqual(source, 'wall_includes_startup')

    def test_claude_uses_harness_duration_ms(self):
        result = {'harness': 'claude', 'harness_duration_ms': 12345, 'wall_time_ms': 99999.0}
        ms, source = battery.compute_exec_time(result, Path('/nonexistent-run-dir'))
        self.assertEqual(ms, 12345)
        self.assertEqual(source, 'harness_duration_ms')

    def test_opencode_uses_step_event_timestamps(self):
        stdout = '\n'.join([
            json.dumps({'type': 'step_start', 'timestamp': 1000}),
            json.dumps({'type': 'text', 'part': {'text': 'x'}}),
            json.dumps({'type': 'step_finish', 'timestamp': 3500}),
        ])
        result = {'harness': 'opencode', 'wall_time_ms': 99999.0}
        ms, source = battery.compute_exec_time(result, Path('/nonexistent-run-dir'), stdout_text=stdout)
        self.assertEqual(ms, 2500)
        self.assertEqual(source, 'harness_event_timestamps')

    def test_opencode_reads_saved_harness_stdout_when_not_passed_inline(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)/'run'
            run_dir.mkdir(parents=True)
            stdout = '\n'.join([
                json.dumps({'type': 'step_start', 'timestamp': 5000}),
                json.dumps({'type': 'step_finish', 'timestamp': 5750}),
            ])
            (run_dir/'harness-stdout.txt').write_text(stdout)
            result = {'harness': 'opencode', 'wall_time_ms': 99999.0}
            ms, source = battery.compute_exec_time(result, run_dir)
            self.assertEqual(ms, 750)
            self.assertEqual(source, 'harness_event_timestamps')

    def test_opencode_falls_back_to_wall_without_any_stdout(self):
        result = {'harness': 'opencode', 'wall_time_ms': 3210.0}
        ms, source = battery.compute_exec_time(result, Path('/nonexistent-run-dir'))
        self.assertEqual(ms, 3210.0)
        self.assertEqual(source, 'wall_includes_startup')

    def test_codex_has_no_native_timestamps_and_falls_back_to_wall(self):
        result = {'harness': 'codex', 'wall_time_ms': 7777.0}
        ms, source = battery.compute_exec_time(result, Path('/nonexistent-run-dir'))
        self.assertEqual(ms, 7777.0)
        self.assertEqual(source, 'wall_includes_startup')


# --------------------------------------------------------------------------
# Scenario (multi-turn) exec-time windowing -- sums each turn's own
# first-request -> last-response window instead of one whole-session window,
# so the inter-turn gap (including any --turn-gap-seconds pause) is excluded.
# --------------------------------------------------------------------------

class ScenarioExecMetricsTests(unittest.TestCase):
    def _make_session(self, tmp, events):
        fake_home = Path(tmp)/'home'
        run_dir = Path(tmp)/'run'
        workspace = run_dir/'workspace'
        workspace.mkdir(parents=True)
        (run_dir/'worker-result.json').write_text(json.dumps({'session_id': 'sess-1'}))
        slug = str(workspace.resolve()).replace('\\', '-').replace('/', '-').replace(':', '')
        sessions_dir = fake_home/'.amplifier/projects'/slug/'sessions'/'sess-1'
        sessions_dir.mkdir(parents=True)
        (sessions_dir/'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events)+'\n')
        return fake_home, run_dir

    def test_sums_per_turn_windows_excluding_the_gap_between_them(self):
        # Turn 1: request@0s -> response@2s (2000ms). Idle gap 2s-100s (a
        # --turn-gap-seconds pause). Turn 2: request@100s -> response@103s (3000ms).
        # A whole-session window would report ~103000ms; the correct answer is 5000ms.
        events = [
            {'event': 'llm:request', 'ts': '2026-01-01T00:00:00+00:00', 'data': {'model': 'model-a'}},
            {'event': 'llm:response', 'ts': '2026-01-01T00:00:02+00:00', 'data': {'usage': {'cost_usd': 0.02}}},
            {'event': 'llm:request', 'ts': '2026-01-01T00:01:40+00:00', 'data': {'model': 'model-b'}},
            {'event': 'llm:response', 'ts': '2026-01-01T00:01:43+00:00', 'data': {'usage': {'cost_usd': 0.03}}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            fake_home, run_dir = self._make_session(tmp, events)
            result = {
                'harness': 'amplifier-fd', 'wall_time_ms': 999999.0,
                'turns': [
                    {'index': 1, 'subtask': 'a', 'started_at': '2026-01-01T00:00:00+00:00',
                     'ended_at': '2026-01-01T00:00:02+00:00', 'skipped': False},
                    {'index': 2, 'subtask': 'b', 'started_at': '2026-01-01T00:01:40+00:00',
                     'ended_at': '2026-01-01T00:01:43+00:00', 'skipped': False},
                ],
            }
            with patch('battery.Path.home', return_value=fake_home):
                ms, requests = battery._amplifier_exec_metrics(result, run_dir)
                annotated = battery._with_exec_time(result, run_dir)
        self.assertAlmostEqual(ms, 5000.0)
        self.assertEqual(requests, 2)
        self.assertEqual(annotated['exec_time_ms'], 5000.0)
        turn1, turn2 = annotated['turns']
        self.assertAlmostEqual(turn1['exec_time_ms'], 2000.0)
        self.assertAlmostEqual(turn2['exec_time_ms'], 3000.0)
        self.assertAlmostEqual(turn1['cost_usd'], 0.02)
        self.assertAlmostEqual(turn2['cost_usd'], 0.03)
        self.assertEqual(turn1['model_counts'], {'model-a': 1})
        self.assertEqual(turn2['model_counts'], {'model-b': 1})

    def test_skipped_turns_are_excluded_from_the_sum_and_left_unannotated(self):
        events = [
            {'event': 'llm:request', 'ts': '2026-01-01T00:00:00+00:00', 'data': {'model': 'model-a'}},
            {'event': 'llm:response', 'ts': '2026-01-01T00:00:01+00:00', 'data': {'usage': {'cost_usd': 0.01}}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            fake_home, run_dir = self._make_session(tmp, events)
            result = {
                'harness': 'amplifier-fd', 'wall_time_ms': 999999.0,
                'turns': [
                    {'index': 1, 'subtask': 'a', 'started_at': '2026-01-01T00:00:00+00:00',
                     'ended_at': '2026-01-01T00:00:01+00:00', 'skipped': False},
                    {'index': 2, 'subtask': 'b', 'started_at': None, 'ended_at': None, 'skipped': True},
                ],
            }
            with patch('battery.Path.home', return_value=fake_home):
                ms, requests = battery._amplifier_exec_metrics(result, run_dir)
                annotated = battery._with_exec_time(result, run_dir)
        self.assertAlmostEqual(ms, 1000.0)
        self.assertEqual(requests, 1)
        turn1, turn2 = annotated['turns']
        self.assertAlmostEqual(turn1['exec_time_ms'], 1000.0)
        self.assertIsNone(turn2['exec_time_ms'])
        self.assertEqual(turn2['model_counts'], {})

    def test_background_naming_call_excluded_from_exec_time_but_cost_kept(self):
        """Reproduces the real 2026-09-25 mt-smoke shape: turn 2's real
        response finishes, then a background session-naming call (haiku,
        purpose='session-naming') fires and finishes just before ended_at.
        Its cost must still be added to cost_usd; its time must not inflate
        exec_time_ms/provider_requests, and its model must not appear in
        model_counts -- it gets its own background_calls entry instead."""
        events = [
            {'event': 'llm:request', 'ts': '2026-01-01T00:00:00+00:00', 'data': {'model': 'model-a'}},
            {'event': 'llm:response', 'ts': '2026-01-01T00:00:05+00:00', 'data': {'usage': {'cost_usd': 0.05}}},
            {'event': 'llm:request', 'ts': '2026-01-01T00:00:06+00:00',
             'data': {'model': 'claude-haiku-4-5-20251001', 'purpose': 'session-naming',
                      'origin_module': 'hooks-session-naming'}},
            {'event': 'llm:response', 'ts': '2026-01-01T00:00:07+00:00',
             'data': {'purpose': 'session-naming', 'origin_module': 'hooks-session-naming',
                      'usage': {'cost_usd': 0.003}}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            fake_home, run_dir = self._make_session(tmp, events)
            result = {
                'harness': 'amplifier-fd', 'wall_time_ms': 99999.0,
                'turns': [{'index': 1, 'subtask': 'a', 'started_at': '2026-01-01T00:00:00+00:00',
                           'ended_at': '2026-01-01T00:00:08+00:00', 'skipped': False}],
            }
            with patch('battery.Path.home', return_value=fake_home):
                annotated = battery._with_exec_time(result, run_dir)
        turn = annotated['turns'][0]
        # exec_time_ms is the REAL response window (0s -> 5s), not stretched to 7s by the background call.
        self.assertAlmostEqual(turn['exec_time_ms'], 5000.0)
        self.assertEqual(turn['provider_requests'], 1)
        self.assertEqual(turn['model_counts'], {'model-a': 1})
        # Cost of the background call is still counted -- the user paid for it.
        self.assertAlmostEqual(turn['cost_usd'], 0.053)
        self.assertEqual(len(turn['background_calls']), 1)
        bg = turn['background_calls'][0]
        self.assertEqual(bg['purpose'], 'session-naming')
        self.assertEqual(bg['model'], 'claude-haiku-4-5-20251001')
        self.assertEqual(bg['origin_module'], 'hooks-session-naming')

    def test_single_turn_result_is_unaffected_by_the_scenario_branch(self):
        """A result with no 'turns' key must take the exact pre-existing
        whole-session-window code path (byte-identical single-turn behavior)."""
        events = [
            {'type': 'llm:request', 'ts': '2026-01-01T00:00:00+00:00'},
            {'type': 'llm:response', 'ts': '2026-01-01T00:00:01+00:00'},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            fake_home, run_dir = self._make_session(tmp, events)
            result = {'harness': 'amplifier-fd', 'wall_time_ms': 99999.0}
            with patch('battery.Path.home', return_value=fake_home):
                ms, requests = battery._amplifier_exec_metrics(result, run_dir)
        self.assertAlmostEqual(ms, 1000.0)
        self.assertEqual(requests, 1)


# --------------------------------------------------------------------------
# backfill-exec: fills exec_time_ms/exec_time_source on existing results,
# in place, idempotently
# --------------------------------------------------------------------------

class BackfillExecTests(unittest.TestCase):
    def test_backfill_is_idempotent_and_uses_harness_duration_ms_for_claude(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'campaign'
            experiment_dir = root/'experiments'/'e1'
            runs_root = experiment_dir/'runs'
            runs_root.mkdir(parents=True)
            root.mkdir(parents=True, exist_ok=True)
            name = 'e1-add_one-claude-a1'
            run_dir = runs_root/name
            run_dir.mkdir(parents=True)
            (run_dir/'result.json').write_text(json.dumps({
                'name': name, 'task': 'add_one', 'harness': 'claude', 'family': 'arith',
                'outcome_passed': True, 'wall_time_ms': 9000.0, 'harness_duration_ms': 5000,
                'cost_usd': 0.1, 'timed_out': False,
            }))
            manifest = {'run_order': [name],
                        'runs': {name: {'name': name, 'task': 'add_one', 'harness': 'claude', 'attempt': 1}},
                        'deadline_seconds': 600}
            (runs_root/'manifest.json').write_text(json.dumps(manifest))

            first = battery.cmd_backfill_exec(SimpleNamespace(root=str(root), experiment='e1'))
            self.assertEqual(first['updated'], [name])
            result = json.loads((run_dir/'result.json').read_text())
            self.assertEqual(result['exec_time_ms'], 5000)
            self.assertEqual(result['exec_time_source'], 'harness_duration_ms')
            # untouched fields survive backfill
            self.assertEqual(result['cost_usd'], 0.1)

            second = battery.cmd_backfill_exec(SimpleNamespace(root=str(root), experiment='e1'))
            self.assertEqual(second['updated'], [])
            self.assertEqual(json.loads((run_dir/'result.json').read_text()), result)

    def test_backfill_opencode_from_saved_harness_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'campaign'
            experiment_dir = root/'experiments'/'e1'
            runs_root = experiment_dir/'runs'
            runs_root.mkdir(parents=True)
            root.mkdir(parents=True, exist_ok=True)
            name = 'e1-add_one-opencode-a1'
            run_dir = runs_root/name
            run_dir.mkdir(parents=True)
            (run_dir/'result.json').write_text(json.dumps({
                'name': name, 'task': 'add_one', 'harness': 'opencode', 'family': 'arith',
                'outcome_passed': True, 'wall_time_ms': 9000.0, 'timed_out': False,
            }))
            stdout = '\n'.join([json.dumps({'type': 'step_start', 'timestamp': 100}),
                                 json.dumps({'type': 'step_finish', 'timestamp': 1600})])
            (run_dir/'harness-stdout.txt').write_text(stdout)
            manifest = {'run_order': [name],
                        'runs': {name: {'name': name, 'task': 'add_one', 'harness': 'opencode', 'attempt': 1}},
                        'deadline_seconds': 600}
            (runs_root/'manifest.json').write_text(json.dumps(manifest))

            battery.cmd_backfill_exec(SimpleNamespace(root=str(root), experiment='e1'))
            result = json.loads((run_dir/'result.json').read_text())
            self.assertEqual(result['exec_time_ms'], 1500)
            self.assertEqual(result['exec_time_source'], 'harness_event_timestamps')


# --------------------------------------------------------------------------
# evaluate --baseline-root / --baseline-experiment: cross-campaign comparison
# --------------------------------------------------------------------------

def _write_fixture_experiment(experiment_dir, harness_results):
    """harness_results: {(task, harness): {result-dict fields}} -> a minimal
    hand-built experiment tree cmd_evaluate/_load_experiment_assigned can read."""
    runs_root = experiment_dir/'runs'
    runs_root.mkdir(parents=True, exist_ok=True)
    run_order, runs = [], {}
    for (task, harness), fields in harness_results.items():
        name = f'{task}-{harness}-a1'
        run_dir = runs_root/'amplifier'/name if harness.startswith('amplifier') else runs_root/name
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir/'result.json').write_text(json.dumps({
            'name': name, 'task': task, 'harness': harness, 'family': 'arith', 'attempt': 1, **fields}))
        run_order.append(name)
        runs[name] = {'name': name, 'task': task, 'harness': harness, 'attempt': 1}
    (runs_root/'manifest.json').write_text(json.dumps({'run_order': run_order, 'runs': runs,
                                                         'deadline_seconds': 600}))
    (experiment_dir/'proposal.json').write_text(json.dumps({'experiment_id': experiment_dir.name,
                                                             'dev_tasks': [], 'holdout_tasks': []}))


class CrossCampaignEvaluateTests(unittest.TestCase):
    def test_evaluate_with_baseline_root_hand_computed(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            base = Path(tmp)
            candidate_root = base/'campaign_candidate'
            baseline_root = base/'campaign_baseline'
            cand_dir = candidate_root/'experiments'/'cand1'
            base_dir = baseline_root/'experiments'/'base1'

            _write_fixture_experiment(cand_dir, {
                ('add_one', 'amplifier-fd'): {'outcome_passed': True, 'exec_time_ms': 2000.0,
                                               'wall_time_ms': 2000.0},
                ('double', 'amplifier-fd'): {'outcome_passed': True, 'exec_time_ms': 5000.0,
                                              'wall_time_ms': 5000.0},
            })
            _write_fixture_experiment(base_dir, {
                ('add_one', 'claude'): {'outcome_passed': True, 'exec_time_ms': 4000.0, 'wall_time_ms': 4000.0},
                ('double', 'claude'): {'outcome_passed': True, 'exec_time_ms': 3000.0, 'wall_time_ms': 3000.0},
            })

            comparison = battery.cmd_evaluate(SimpleNamespace(
                root=str(candidate_root), experiment='cand1',
                baseline_root=str(baseline_root), baseline_experiment='base1'))

            cross = comparison['cross']
            self.assertIsNotNone(cross)
            self.assertEqual(cross['common_tasks'], ['add_one', 'double'])
            claude_row = cross['baselines']['claude']
            self.assertEqual(claude_row['wins'], 1)
            self.assertEqual(claude_row['losses'], 1)
            # hand-computed: geomean((2000/4000), (5000/3000))
            self.assertAlmostEqual(claude_row['geomean_ratio'], (2000/4000*5000/3000)**0.5)
            # hand-computed exact two-sided sign test, n=2, k=min(1,1)=1: 2*(C(2,0)+C(2,1))/4 = 1.0 (clamped)
            self.assertAlmostEqual(claude_row['sign_test_p_value'], 1.0)
            self.assertEqual(claude_row['candidate_successes'], 2)
            self.assertEqual(claude_row['baseline_successes'], 2)
            self.assertEqual(cross['rank']['add_one']['rank'], 1)  # candidate (2000ms) faster than claude (4000ms)
            self.assertEqual(cross['rank']['double']['rank'], 2)  # claude (3000ms) faster than candidate (5000ms)

    def test_evaluate_without_baseline_root_still_works(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            candidate_root = Path(tmp)/'campaign_candidate'
            cand_dir = candidate_root/'experiments'/'cand1'
            _write_fixture_experiment(cand_dir, {
                ('add_one', 'amplifier-fd'): {'outcome_passed': True, 'exec_time_ms': 2000.0,
                                               'wall_time_ms': 2000.0},
            })
            comparison = battery.cmd_evaluate(SimpleNamespace(root=str(candidate_root), experiment='cand1'))
            self.assertIsNone(comparison['cross'])


# --------------------------------------------------------------------------
# routing/served-model recording on result.json (task #2): per-provider-call
# served vs requested model, effort/reason/escalation and token usage, plus
# per-turn judge decisions -- derived from receipts.jsonl for a routed
# (amplifier-fd) run, and from native events.jsonl alone for a plain run.
# --------------------------------------------------------------------------

class RoutingFromReceiptsTests(unittest.TestCase):
    """`_routing_from_receipts`: a routed run's receipts.jsonl -> turn_decisions
    (difficulty_judged) + provider_calls (slow_start/effort_routed/model_routed/
    slow_end correlated by provider_call_id) + served_model_counts."""

    def test_none_when_receipts_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)/'run'
            run_dir.mkdir()
            self.assertIsNone(battery._routing_from_receipts(run_dir))

    def test_none_when_receipts_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)/'run'
            _write_receipts(run_dir, [])
            self.assertIsNone(battery._routing_from_receipts(run_dir))

    def test_provider_call_correlates_routing_and_usage_by_provider_call_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)/'run'
            events = [
                {'event': 'fast_decisions:difficulty_judged',
                 'data': {'backend': 'ollama', 'choice': 'strong', 'probabilities': {'complex': 0.83},
                          'reason_code': 'judge_strong'}},
                {'event': 'fast_decisions:effort_routed',
                 'data': {'phase': 'implement', 'requested_effort': 'high', 'provider_call_id': 'pc-1'}},
                {'event': 'fast_decisions:model_routed',
                 'data': {'requested_model': 'claude-cheap-1', 'reason_code': 'start_model',
                          'escalated': False, 'escalation_reason': None, 'provider_call_id': 'pc-1'}},
                {'event': 'fast_decisions:slow_start',
                 'data': {'provider': 'anthropic', 'model': 'claude-cheap-1', 'provider_call_id': 'pc-1'}},
                {'event': 'fast_decisions:slow_end',
                 'data': {'provider': 'anthropic', 'model': 'claude-cheap-1', 'provider_call_id': 'pc-1',
                          'status': 'ok', 'served_model': 'claude-cheap-1-20260101', 'input_tokens': 180,
                          'output_tokens': 65, 'cache_read_tokens': 40}},
                # a second provider call, escalated -- no effort_routed receipt this time
                {'event': 'fast_decisions:model_routed',
                 'data': {'requested_model': None, 'reason_code': 'escalated_max_requests',
                          'escalated': True, 'escalation_reason': 'max_requests', 'provider_call_id': 'pc-2'}},
                {'event': 'fast_decisions:slow_start',
                 'data': {'provider': 'anthropic', 'model': 'claude-strong-1', 'provider_call_id': 'pc-2'}},
                {'event': 'fast_decisions:slow_end',
                 'data': {'provider': 'anthropic', 'model': 'claude-strong-1', 'provider_call_id': 'pc-2',
                          'status': 'ok', 'served_model': 'claude-strong-1-20260101', 'input_tokens': 400,
                          'output_tokens': 120}},
            ]
            _write_receipts(run_dir, events)

            routing = battery._routing_from_receipts(run_dir)
            self.assertEqual(len(routing['turn_decisions']), 1)
            decision = routing['turn_decisions'][0]
            self.assertEqual(decision['decision'], 'strong')
            self.assertEqual(decision['tier'], 'strong')
            self.assertEqual(decision['backend'], 'ollama')
            self.assertAlmostEqual(decision['p_complex'], 0.83)
            self.assertEqual(decision['reason'], 'judge_strong')

            calls = {c['provider_call_id']: c for c in routing['provider_calls']}
            self.assertEqual(len(calls), 2)
            pc1 = calls['pc-1']
            self.assertEqual(pc1['requested_model'], 'claude-cheap-1')
            self.assertEqual(pc1['served_model'], 'claude-cheap-1-20260101')
            self.assertEqual(pc1['reason_code'], 'start_model')
            self.assertFalse(pc1['escalated'])
            self.assertEqual(pc1['effort'], 'high')
            self.assertEqual(pc1['input_tokens'], 180)
            self.assertEqual(pc1['output_tokens'], 65)
            self.assertEqual(pc1['cache_read_tokens'], 40)
            self.assertNotIn('cache_write_tokens', pc1)

            pc2 = calls['pc-2']
            # model_routed's requested_model was None (no override this call) --
            # falls back to slow_start's actual model, never left blank.
            self.assertEqual(pc2['requested_model'], 'claude-strong-1')
            self.assertEqual(pc2['served_model'], 'claude-strong-1-20260101')
            self.assertTrue(pc2['escalated'])
            self.assertEqual(pc2['reason_code'], 'escalated_max_requests')
            self.assertIsNone(pc2['effort'])  # no effort_routed receipt for this call

            self.assertEqual(routing['served_model_counts'],
                              {'claude-cheap-1-20260101': 1, 'claude-strong-1-20260101': 1})

    def test_served_model_falls_back_to_requested_when_provider_never_reports_one(self):
        """Some providers don't echo a model back on the response -- served_model
        must still be populated, from slow_end's own 'model' or the requested
        model, never left blank when a call clearly happened."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)/'run'
            events = [
                {'event': 'fast_decisions:model_routed',
                 'data': {'requested_model': 'claude-cheap-1', 'reason_code': 'start_model',
                          'escalated': False, 'provider_call_id': 'pc-1'}},
                {'event': 'fast_decisions:slow_start',
                 'data': {'provider': 'anthropic', 'model': 'claude-cheap-1', 'provider_call_id': 'pc-1'}},
                {'event': 'fast_decisions:slow_end',
                 'data': {'provider': 'anthropic', 'model': 'claude-cheap-1', 'provider_call_id': 'pc-1',
                          'status': 'ok'}},  # no served_model, no 'model' override at slow_end either
            ]
            _write_receipts(run_dir, events)
            routing = battery._routing_from_receipts(run_dir)
            call = routing['provider_calls'][0]
            self.assertEqual(call['served_model'], 'claude-cheap-1')  # falls back to slow_end's 'model'
            self.assertEqual(routing['served_model_counts'], {'claude-cheap-1': 1})

    def test_duplicate_slow_end_for_same_provider_call_id_is_not_double_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)/'run'
            events = [
                {'event': 'fast_decisions:slow_start', 'data': {'model': 'm1', 'provider_call_id': 'pc-1'}},
                {'event': 'fast_decisions:slow_end',
                 'data': {'model': 'm1', 'served_model': 'm1', 'provider_call_id': 'pc-1'}},
                {'event': 'fast_decisions:slow_end',  # e.g. a retried emit -- must not double-count
                 'data': {'model': 'm1', 'served_model': 'm1', 'provider_call_id': 'pc-1'}},
            ]
            _write_receipts(run_dir, events)
            routing = battery._routing_from_receipts(run_dir)
            self.assertEqual(len(routing['provider_calls']), 1)
            self.assertEqual(routing['served_model_counts'], {'m1': 1})


class ProviderCallsFromNativeEventsTests(unittest.TestCase):
    """`_provider_calls_from_native_events`: the plain-harness fallback -- no
    fast-decisions receipts exist, so provider_calls (and served_model_counts)
    are derived from the native session's own llm:response events instead."""

    def _fake_session(self, tmp, events):
        fake_home = Path(tmp)/'home'
        run_dir = Path(tmp)/'run'
        workspace = run_dir/'workspace'
        workspace.mkdir(parents=True)
        (run_dir/'worker-result.json').write_text(json.dumps({'session_id': 'sess-1'}))
        slug = str(workspace.resolve()).replace('\\', '-').replace('/', '-').replace(':', '')
        sessions_dir = fake_home/'.amplifier/projects'/slug/'sessions'/'sess-1'
        sessions_dir.mkdir(parents=True)
        (sessions_dir/'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events)+'\n')
        return fake_home, run_dir

    def test_plain_run_derives_served_model_counts_from_llm_response_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = [
                {'event': 'llm:request', 'data': {'model': 'claude-fable-5-1'}},
                {'event': 'llm:response', 'data': {'model': 'claude-fable-5-1',
                                                    'usage': {'input_tokens': 500, 'output_tokens': 80}}},
                {'event': 'llm:request', 'data': {'model': 'claude-fable-5-1'}},
                {'event': 'llm:response', 'data': {'model': 'claude-fable-5-1',
                                                    'usage': {'input_tokens': 700, 'output_tokens': 120}}},
            ]
            fake_home, run_dir = self._fake_session(tmp, events)
            result = {'harness': 'amplifier-plain'}
            with patch('battery.Path.home', return_value=fake_home):
                calls = battery._provider_calls_from_native_events(result, run_dir)
            self.assertEqual(len(calls), 2)
            for call in calls:
                self.assertEqual(call['served_model'], 'claude-fable-5-1')
                self.assertEqual(call['requested_model'], 'claude-fable-5-1')
                self.assertIsNone(call['reason_code'])
                self.assertIsNone(call['escalated'])
            self.assertEqual(calls[0]['input_tokens'], 500)
            self.assertEqual(calls[1]['output_tokens'], 120)

    def test_empty_when_no_session_resolvable(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)/'run'
            (run_dir/'workspace').mkdir(parents=True)
            with patch('battery.Path.home', return_value=Path(tmp)/'nohome'):
                calls = battery._provider_calls_from_native_events({'harness': 'amplifier-plain'}, run_dir)
            self.assertEqual(calls, [])


class RoutingSummaryTests(unittest.TestCase):
    """`_routing_summary`: prefers receipts.jsonl, falls back to native events,
    and degrades to {'available': False, 'reason': ...} rather than raising --
    this is what `_settle_and_record` attaches to every result.json."""

    def test_routed_run_uses_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)/'run'
            events = [
                {'event': 'fast_decisions:model_routed',
                 'data': {'requested_model': 'claude-cheap-1', 'reason_code': 'start_model',
                          'escalated': False, 'provider_call_id': 'pc-1'}},
                {'event': 'fast_decisions:slow_start', 'data': {'model': 'claude-cheap-1', 'provider_call_id': 'pc-1'}},
                {'event': 'fast_decisions:slow_end',
                 'data': {'model': 'claude-cheap-1', 'served_model': 'claude-cheap-1-20260101',
                          'provider_call_id': 'pc-1', 'status': 'ok'}},
            ]
            _write_receipts(run_dir, events)
            routing = battery._routing_summary({'harness': 'amplifier-fd'}, run_dir)
            self.assertTrue(routing['available'])
            self.assertEqual(routing['served_model_counts'], {'claude-cheap-1-20260101': 1})
            self.assertEqual(len(routing['provider_calls']), 1)

    def test_plain_run_falls_back_to_native_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)/'home'
            run_dir = Path(tmp)/'run'
            workspace = run_dir/'workspace'
            workspace.mkdir(parents=True)
            (run_dir/'worker-result.json').write_text(json.dumps({'session_id': 'sess-1'}))
            slug = str(workspace.resolve()).replace('\\', '-').replace('/', '-').replace(':', '')
            sessions_dir = fake_home/'.amplifier/projects'/slug/'sessions'/'sess-1'
            sessions_dir.mkdir(parents=True)
            events = [{'event': 'llm:response', 'data': {'model': 'claude-fable-5-1', 'usage': {}}}]
            (sessions_dir/'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events)+'\n')
            with patch('battery.Path.home', return_value=fake_home):
                routing = battery._routing_summary({'harness': 'amplifier-plain'}, run_dir)
            self.assertTrue(routing['available'])
            self.assertEqual(routing['turn_decisions'], [])
            self.assertEqual(routing['served_model_counts'], {'claude-fable-5-1': 1})

    def test_unavailable_when_neither_source_has_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)/'run'
            (run_dir/'workspace').mkdir(parents=True)
            with patch('battery.Path.home', return_value=Path(tmp)/'nohome'):
                routing = battery._routing_summary({'harness': 'amplifier-plain'}, run_dir)
            self.assertFalse(routing['available'])
            self.assertIn('reason', routing)

    def test_never_raises_even_when_receipts_parsing_blows_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)/'run'
            run_dir.mkdir()
            orig = battery._routing_from_receipts
            battery._routing_from_receipts = lambda _run_dir: (_ for _ in ()).throw(RuntimeError('boom'))
            try:
                routing = battery._routing_summary({'harness': 'amplifier-fd'}, run_dir)
            finally:
                battery._routing_from_receipts = orig
            self.assertFalse(routing['available'])
            self.assertIn('routing_summary_error', routing['reason'])


class MechanismReportServedModelCountsTests(unittest.TestCase):
    """`_run_mechanism_counts`/`_mechanism_report` aggregate served_model_counts
    across an experiment's amplifier-fd runs -- cheap to expose since the
    receipts are already parsed for the mechanism gate."""

    def test_served_model_counts_aggregated_across_runs_in_comparison_mechanism(self):
        with tempfile.TemporaryDirectory() as tmp, _patched_battery_tasks():
            root = Path(tmp)/'campaign'
            loop_config = {'backend': 'ollama', 'model': 'qwen3:0.6b'}
            events = [
                {'event': 'fast_decisions:scored', 'data': {'backend': 'ollama'}},
                {'event': 'fast_decisions:slow_end', 'data': {'served_model': 'claude-cheap-1-20260101'}},
                {'event': 'fast_decisions:slow_end', 'data': {'served_model': 'claude-cheap-1-20260101'}},
            ]
            _one_amplifier_fd_experiment(root, 'sm1', loop_config, events)
            comparison = battery.cmd_evaluate(SimpleNamespace(root=str(root), experiment='sm1'))
            mechanism = comparison['mechanism']
            self.assertEqual(mechanism['served_model_counts'], {'claude-cheap-1-20260101': 2})


if __name__ == '__main__':
    unittest.main()
