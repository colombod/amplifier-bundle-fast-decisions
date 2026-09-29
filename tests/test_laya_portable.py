import json
import os
from pathlib import Path
import tempfile
import shutil
import subprocess
import unittest
from unittest.mock import AsyncMock, patch

from amplifier_fast_decisions.contracts import Answer, Decision, DecisionResult, Question, canonical
from amplifier_fast_decisions.smart_tool import select, cua
from amplifier_fast_decisions.jevgrep import JevgrepTool


class Laya:
    name = 'laya'
    external = False

    def __init__(self):
        self.requests = []
        self.closed = False

    async def ask(self, req):
        self.requests.append(req)
        if req.questions:
            answers = {}
            for q in req.questions:
                if q.type == 'noul':
                    answers[q.name] = Answer(noul=.95)
                else:
                    choice = 'CLICK' if q.name == 'cua_operation' else 'reports'
                    answers[q.name] = Answer(probabilities={k: float(k == choice) for k in q.criteria})
            return DecisionResult(Decision('reason', {'reason': 1}), answers, model='laya-test')
        return DecisionResult(Decision('readme', {'readme': .98, 'reason': .02},
                                      model='laya-test', probability_kind='model_reported'), model='laya-test')

    ask_many = ask

    async def close(self):
        self.closed = True


def observed():
    return {'surface_id': 'fixture', 'revision': '1', 'text': 'Home',
            'elements': [{'id': 'reports', 'label': 'Reports', 'operations': ['CLICK']}]}


class PortableTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_laya_selection_with_real_identity_and_no_external_consent(self):
        model = Laya()
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {}, clear=True), \
             patch('amplifier_fast_decisions.smart_tool.LayaBackend', return_value=model):
            result = await select({'task': 'Read README.md', 'candidates': [
                {'id': 'readme', 'operation': 'read', 'path': 'README.md'}]}, events_dir=root, backend='laya')
        self.assertTrue(result.ok)
        self.assertEqual((result.backend, result.model, result.choice), ('laya', 'laya-test', 'readme'))
        self.assertTrue(model.closed)

    async def test_remote_laya_consent_checked_before_inference(self):
        model = Laya()
        model.external = True
        with patch('amplifier_fast_decisions.smart_tool.LayaBackend', return_value=model):
            result = await select({'task': 'Read README.md', 'candidates': [
                {'id': 'readme', 'operation': 'read', 'path': 'README.md'}]}, backend='laya')
        self.assertEqual(result.reason_code, 'external_state_not_enabled')
        self.assertEqual(model.requests, [])

    async def test_cua_one_typed_prediction_and_local_consent(self):
        model = Laya()
        with patch('amplifier_fast_decisions.local_backend.LayaBackend', return_value=model):
            result = await cua({'goal': 'Open Reports', 'snapshot': observed()}, backend='laya')
        self.assertEqual(result['action'], {'operation': 'CLICK', 'target': 'reports'})
        self.assertEqual(result['backend'], 'laya')
        self.assertFalse(result['executes_actions'])
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(model.requests[0].candidates, ())
        self.assertEqual({q.name for q in model.requests[0].questions}, {'cua_operation', 'click_target'})

    async def test_cua_oversize_is_not_silently_truncated(self):
        model = Laya()
        snapshot = observed()
        snapshot['text'] = 'x' * 3100
        with patch('amplifier_fast_decisions.local_backend.LayaBackend', return_value=model):
            result = await cua({'goal': 'Open Reports', 'snapshot': snapshot}, backend='laya')
        self.assertEqual(result['reason'], 'narrow_laya_snapshot')
        self.assertFalse(model.requests)

    async def test_local_retrieval_excludes_hidden_symlink_sensitive_and_marks_limits(self):
        model = Laya()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'retry.py').write_text('def retry(): return 3\n')
            (root / '.env').write_text('PRIVATE_VALUE')
            (root / 'secrets.py').write_text('PRIVATE_VALUE')
            (root / 'link.py').symlink_to(root / 'retry.py')
            listing = 'retry.py\0.env\0secrets.py\0link.py\0'
            with patch('amplifier_fast_decisions.laya_search.LayaBackend', return_value=model), \
                 patch('amplifier_fast_decisions.jevgrep._run_bounded', AsyncMock(return_value=(0, listing, False))):
                result = await JevgrepTool(backend="laya", root=root).search({'query': 'retry'})
            self.assertEqual(result['backend'], 'laya')
            self.assertEqual([r['path'] for r in result['matches']], ['retry.py'])
            self.assertNotIn('PRIVATE_VALUE', str(model.requests))
            self.assertEqual(result['files_omitted'], 3)
            (root / 'retry.py').write_text('x' * 2048)
            with patch('amplifier_fast_decisions.laya_search.LayaBackend', return_value=model), \
                 patch('amplifier_fast_decisions.jevgrep._run_bounded', AsyncMock(return_value=(0, 'retry.py\0', False))):
                result = await JevgrepTool(backend="laya", root=root, max_source_bytes=1024).search({'query': 'retry'})
            self.assertEqual(result['status'], 'incomplete')

    @unittest.skipUnless(shutil.which('rg'), 'rg not installed')
    async def test_real_inventory_honors_gitignore_and_nested_generated_dirs(self):
        model = Laya()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / '.gitignore').write_text('ignored.py\n')
            for relative in ['retry.py', 'ignored.py', 'pkg/node_modules/no.py', 'pkg/build/no.py']:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('retry = True\n')
            with patch('amplifier_fast_decisions.laya_search.LayaBackend', return_value=model):
                result = await JevgrepTool(backend="laya", root=root).search({'query': 'retry'})
            self.assertEqual([row['path'] for row in result['matches']], ['retry.py'])

    async def test_escaped_source_is_never_truncated_before_scoring(self):
        model = Laya()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = '\\' * 1800
            (root / 'escaped.py').write_text(content)
            with patch('amplifier_fast_decisions.laya_search.LayaBackend', return_value=model), \
                 patch('amplifier_fast_decisions.jevgrep._run_bounded', AsyncMock(return_value=(0, 'escaped.py\0', False))):
                result = await JevgrepTool(backend="laya", root=root).search({'query': 'escaping'})
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(''.join(req.state['source'] for req in model.requests), content)
            self.assertTrue(all(len(canonical(req.state)) <= 3000 for req in model.requests))

    async def test_laya_missing_identity_is_rejected_for_actions_and_questions(self):
        from amplifier_fast_decisions.local_backend import LayaBackend
        from amplifier_fast_decisions.backends import BackendUnavailable
        from amplifier_fast_decisions.contracts import Candidate, DecisionRequest
        action = DecisionRequest({}, (Candidate('read', 'Read', 'fast_workspace', {'operation': 'read', 'path': 'README.md'}),))
        questions = DecisionRequest({}, (), questions=(Question('relevant', 'noul', 'Relevant?'),))
        payloads = [
            {'answers': {'next_action': {'type': 'choice', 'choice': 'read', 'probabilities': {'read': .95, 'reason': .05}}}},
            {'answers': {'relevant': {'type': 'noul', 'noul': .9}}},
        ]
        for request, payload in zip([action, questions], payloads):
            with patch('amplifier_fast_decisions.local_backend._mlx_urllib_call', return_value=(payload, 200)):
                with self.assertRaisesRegex(BackendUnavailable, 'model identity'):
                    await LayaBackend().ask(request)
