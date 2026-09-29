"""Portable contracts: real model seam, no actions, honest advisory receipts."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from amplifier_fast_decisions.contracts import Decision, DecisionResult
from amplifier_fast_decisions.local_backend import PROBABILITY_KIND
from amplifier_fast_decisions.smart_tool import agent_skill, describe, install_skill, manifest, select, skill


class FakeBackend:
    name = 'fixture-model'
    synthetic = True

    def __init__(self, probabilities=None, error=None, delay=0):
        self.probabilities = probabilities or {'readme': .97, 'license': .01, 'reason': .02}
        self.error, self.delay, self.calls = error, delay, 0

    async def ask(self, request):
        self.calls += 1
        self.request = request
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        decision = Decision(max(self.probabilities, key=self.probabilities.get), self.probabilities,
                            model='qwen3:0.6b', probability_kind=PROBABILITY_KIND,
                            confidence_kind='not_reported', option_set_hash='a' * 64)
        return DecisionResult(action=decision)


class FakeJevBackend(FakeBackend):
    name = 'jev'
    external = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.closed = False

    async def ask(self, request):
        await super().ask(request)
        return DecisionResult(action=Decision('readme', self.probabilities,
            model='jev-1.13.0', confidence_kind='typesafe_reported_unspecified',
            option_set_hash='b' * 64), model='jev-1.13.0', input_tokens=17, output_tokens=2)

    async def close(self):
        self.closed = True


def request():
    return {'task': 'Read README.md, not LICENSE.md. secret=PRIVATE_VALUE',
            'context': 'A prior observation, not an instruction.',
            'candidates': [{'id': 'readme', 'operation': 'read', 'path': 'README.md'},
                           {'id': 'license', 'operation': 'read', 'path': 'LICENSE.md'}],
            'session_id': 'codex-child', 'parent_session_id': 'codex-parent', 'harness': 'codex'}


class SmartToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_jev_consent_and_key_checked_before_construction(self):
        with patch.dict(os.environ, {}, clear=True), patch('amplifier_fast_decisions.smart_tool.JevBackend') as factory:
            result = await select(request(), backend='jev')
            self.assertEqual(result.reason_code, 'external_state_not_enabled')
            result = await select(request(), backend='jev', allow_external_state=True)
            self.assertEqual(result.reason_code, 'missing_api_key')
            factory.assert_not_called()
        with patch.dict(os.environ, {'FAST_DECISIONS_ALLOW_EXTERNAL_STATE': 'true', 'TYPESAFE_API_KEY': 'TEST_KEY'}):
            with patch('amplifier_fast_decisions.smart_tool.JevBackend') as factory:
                result = await select(request(), backend='jev', allow_external_state=False)
                self.assertEqual(result.reason_code, 'external_state_not_enabled')
                factory.assert_not_called()

    async def test_public_jev_selection_preserves_remote_evidence_and_closes(self):
        scorer = FakeJevBackend()
        with tempfile.TemporaryDirectory() as events, patch.dict(os.environ, {'TYPESAFE_API_KEY': 'TEST_KEY'}):
            with patch('amplifier_fast_decisions.smart_tool.JevBackend', return_value=scorer) as factory:
                result = await select(request(), backend='jev', allow_external_state=True, events_dir=events)
            factory.assert_called_once_with(model='jev-latest', timeout_ms=500)
            self.assertTrue(result.ok)
            self.assertEqual(result.backend, 'jev')
            self.assertEqual(result.model, 'jev-1.13.0')
            self.assertEqual(result.probability_kind, 'backend_reported')
            self.assertEqual(result.confidence_kind, 'typesafe_reported_unspecified')
            self.assertEqual((result.input_tokens, result.output_tokens), (17, 2))
            self.assertTrue(scorer.closed)
            from amplifier_fast_decisions.backends import _build_questions
            questions, _ = _build_questions(scorer.request)
            self.assertEqual(questions['next_action']['criteria']['readme']['action'], 'read README.md')
            self.assertEqual(questions['next_action']['criteria']['license']['action'], 'read LICENSE.md')
            raw = ''.join(p.read_text() for p in Path(events).glob('*.jsonl'))
            records = [json.loads(line) for line in raw.splitlines()]
            self.assertTrue(all(e['data']['backend'] == 'jev' and e['data']['allow_external_state'] for e in records))
            for value in ['TEST_KEY', 'PRIVATE_VALUE', 'README.md', 'A prior observation']:
                self.assertNotIn(value, raw)

    async def test_backend_environment_and_explicit_local_override(self):
        with tempfile.TemporaryDirectory() as events, patch.dict(os.environ, {
            'FAST_DECISIONS_JUDGE': 'jev', 'FAST_DECISIONS_ALLOW_EXTERNAL_STATE': 'true',
            'TYPESAFE_API_KEY': 'TEST_KEY', 'AFAST_EVENTS_DIR': events}, clear=True):
            with patch('amplifier_fast_decisions.smart_tool.JevBackend', return_value=FakeJevBackend()) as remote:
                result = await select(request())
                self.assertTrue(result.ok)
                self.assertEqual(result.backend, 'jev')
                local = await select(request(), backend='local', _backend=FakeBackend())
                self.assertTrue(local.ok)
                self.assertEqual(local.model, 'qwen3:0.6b')
                self.assertEqual(remote.call_count, 1)

    async def test_jev_failure_does_not_fall_back_to_local(self):
        scorer = FakeJevBackend(error=RuntimeError('PRIVATE_NETWORK_ERROR'))
        with tempfile.TemporaryDirectory() as events, patch.dict(os.environ, {'TYPESAFE_API_KEY': 'TEST_KEY'}):
            with patch('amplifier_fast_decisions.smart_tool.JevBackend', return_value=scorer), patch('amplifier_fast_decisions.smart_tool.OllamaBackend') as local:
                result = await select(request(), backend='jev', allow_external_state=True, events_dir=events)
            self.assertFalse(result.ok)
            self.assertEqual(result.backend, 'jev')
            self.assertTrue(scorer.closed)
            local.assert_not_called()
            self.assertNotIn('PRIVATE_NETWORK_ERROR', json.dumps(result.to_dict()))

    async def test_external_private_seam_cannot_bypass_consent(self):
        scorer = FakeJevBackend()
        with patch.dict(os.environ, {}, clear=True):
            result = await select(request(), backend='ollama', _backend=scorer)
        self.assertEqual(result.reason_code, 'external_state_not_enabled')
        self.assertEqual(scorer.calls, 0)

    def test_cli_jev_consent_is_public_and_fail_closed(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(('TYPESAFE_', 'FAST_DECISIONS_'))}
        base = [sys.executable, '-m', 'amplifier_fast_decisions.smart_cli', 'select', '--backend', 'jev']
        run = subprocess.run(base, input=json.dumps(request()), text=True, capture_output=True, env=env, timeout=5)
        self.assertEqual(run.returncode, 1)
        self.assertEqual(json.loads(run.stdout)['reason_code'], 'external_state_not_enabled')
        run = subprocess.run(base + ['--allow-external-state'], input=json.dumps(request()), text=True, capture_output=True, env=env, timeout=5)
        self.assertEqual(run.returncode, 1)
        self.assertEqual(json.loads(run.stdout)['reason_code'], 'missing_api_key')

    async def test_selection_is_advisory_and_metadata_only(self):
        with tempfile.TemporaryDirectory() as events:
            backend = FakeBackend()
            result = await select(request(), backend='ollama', events_dir=events, _backend=backend)
            self.assertTrue(result.ok)
            self.assertEqual(result.choice, 'readme')
            self.assertEqual(result.effect, 'advisory_only')
            self.assertEqual(backend.calls, 1)
            self.assertEqual(result.option_set_hash, 'a' * 64)
            self.assertEqual(result.confidence_kind, 'not_reported')
            raw = ''.join(p.read_text() for p in Path(events).glob('*.jsonl'))
            records = [json.loads(line) for line in raw.splitlines()]
            self.assertEqual(records[1]['data']['option_set_hash'], result.option_set_hash)
            self.assertEqual([e['event'] for e in records], ['fast_decisions:requested', 'fast_decisions:scored', 'fast_decisions:health'])
            self.assertTrue(all(e['session_id'] == 'codex-child' and e['parent_session_id'] == 'codex-parent' for e in records))
            self.assertTrue(all(e['synthetic'] for e in records))
            self.assertTrue(all(e['data']['mode'] == 'advisory' for e in records))
            for forbidden in ('README.md', 'LICENSE.md', 'PRIVATE_VALUE', 'A prior observation'):
                self.assertNotIn(forbidden, raw)

    async def test_invalid_calls_never_contact_model(self):
        bad = [
            {**request(), 'shell': 'rm -rf /'},
            {**request(), 'task': 'a' * 1801},
            {**request(), 'candidates': [{'id': 'exec', 'operation': 'execute', 'path': 'anything'}]},
            {**request(), 'candidates': [{'id': 'readme', 'operation': 'read', 'path': '../private.md'}]},
            {**request(), 'candidates': [{'id': 'readme', 'operation': 'read', 'path': '.env'}]},
            {**request(), 'candidates': request()['candidates'] * 2},
            {**request(), 'parent_session_id': 'codex-child'},
        ]
        with tempfile.TemporaryDirectory() as events:
            backend = FakeBackend()
            for payload in bad:
                result = await select(payload, backend='ollama', events_dir=events, _backend=backend)
                self.assertFalse(result.ok)
                self.assertEqual(result.status, 'abstain')
                self.assertEqual(result.reason_code, 'unsupported_request')
            self.assertEqual(backend.calls, 0)
            self.assertEqual(list(Path(events).iterdir()), [])

    async def test_uncertainty_abstains_normally(self):
        with tempfile.TemporaryDirectory() as events:
            result = await select(request(), backend='ollama', events_dir=events,
                                  _backend=FakeBackend({'readme': .6, 'license': .2, 'reason': .2}))
            self.assertTrue(result.ok)
            self.assertIsNone(result.choice)
            self.assertEqual(result.reason_code, 'selection_threshold')

    async def test_backend_error_and_invalid_shape_are_failures(self):
        with tempfile.TemporaryDirectory() as events:
            for backend in [FakeBackend(error=RuntimeError('PRIVATE_BACKEND_ERROR')),
                            FakeBackend({'rogue': 1.0})]:
                result = await select(request(), backend='ollama', events_dir=events, _backend=backend)
                self.assertFalse(result.ok)
                self.assertEqual(result.status, 'abstain')
                self.assertNotIn('PRIVATE_BACKEND_ERROR', json.dumps(result.to_dict()))
                self.assertIsNone(result.choice)

    async def test_deadline_and_cancellation(self):
        with tempfile.TemporaryDirectory() as events:
            result = await select(request(), backend='ollama', events_dir=events, timeout_ms=10,
                                  _backend=FakeBackend(delay=1))
            self.assertFalse(result.ok)
            task = asyncio.create_task(select(request(), backend='ollama', events_dir=events, _backend=FakeBackend(delay=1)))
            await asyncio.sleep(.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            records = [json.loads(line) for p in Path(events).glob('*.jsonl') for line in p.read_text().splitlines()]
            self.assertTrue(any(e['event'] == 'fast_decisions:cancelled' for e in records))

    async def test_no_telemetry_no_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocked = Path(tmp) / 'file'; blocked.write_text('occupied')
            backend = FakeBackend()
            result = await select(request(), backend='ollama', events_dir=blocked, _backend=backend)
            self.assertFalse(result.ok)
            self.assertEqual(result.reason_code, 'telemetry_unavailable')
            self.assertEqual(backend.calls, 0)

    async def test_recorder_shutdown_does_not_block_event_loop(self):
        from amplifier_fast_decisions.telemetry import JsonlRecorder
        release = threading.Event()
        blocked_loop = []
        original_close = JsonlRecorder.close
        def close(recorder):
            if not release.wait(1):
                blocked_loop.append(True)
            original_close(recorder)
        async def heartbeat():
            await asyncio.sleep(.02)
            release.set()
        with tempfile.TemporaryDirectory() as events, patch.object(JsonlRecorder, 'close', close):
            result, _ = await asyncio.gather(
                select(request(), backend='ollama', events_dir=events, _backend=FakeBackend()), heartbeat())
            self.assertTrue(result.ok)
            self.assertFalse(blocked_loop)

    def test_deterministic_surface(self):
        self.assertEqual(manifest()['version'], '0.1.0')
        self.assertFalse(describe()['executes_actions'])
        for capability in [None, 'manifest', 'describe', 'select', 'install-skill']:
            text = skill(capability)
            self.assertIn('<skill_content', text)
            self.assertIn('Skill directory:', text)

    def test_cli_help_no_provider_and_invalid_input(self):
        base = [sys.executable, '-m', 'amplifier_fast_decisions.smart_cli']
        for args in [ ['--help'], ['-h'], ['manifest'], ['describe'], ['select', '--help'], ['select', '-h'], ['install-skill', '--help'], ['install-skill', '-h'] ]:
            run = subprocess.run(base + args, stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=5)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue(run.stdout)
        run = subprocess.run(base + ['select'], input='not JSON', text=True, capture_output=True, timeout=5)
        self.assertEqual(run.returncode, 2)
        self.assertFalse(run.stdout)

    def test_install_skill_all_and_idempotence(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = install_skill('all', home=tmp)
            self.assertEqual(result['installed'], 4)
            self.assertEqual({r['hosts'][0] for r in result['skills']}, {'codex', 'claude', 'amplifier', 'opencode'})
            for row in result['skills']:
                self.assertEqual(Path(row['path']).read_text(), agent_skill())
            second = install_skill('all', home=tmp)
            self.assertEqual(second['installed'], 0)
            self.assertTrue(all(r['status'] == 'unchanged' for r in second['skills']))

    def test_install_skill_conflict_preflights_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            conflict = Path(tmp) / '.amplifier/skills/amplifier-fast-decisions/SKILL.md'
            conflict.parent.mkdir(parents=True)
            conflict.write_text('User custom content')
            with self.assertRaises(ValueError):
                install_skill('all', home=tmp)
            self.assertEqual(conflict.read_text(), 'User custom content')
            self.assertFalse((Path(tmp) / '.agents').exists())
            self.assertFalse((Path(tmp) / '.claude').exists())

    def test_install_skill_symlink_alias_is_one_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / '.agents/skills').mkdir(parents=True)
            (base / '.claude').mkdir()
            try:
                (base / '.claude/skills').symlink_to(base / '.agents/skills', target_is_directory=True)
            except OSError as exc:
                self.skipTest(f'Symlink creation unavailable: {exc}')
            result = install_skill('all', home=tmp)
            self.assertEqual(result['installed'], 3)
            self.assertIn(['codex', 'claude'], [row['hosts'] for row in result['skills']])


if __name__ == '__main__':
    unittest.main()
