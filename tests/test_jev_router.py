"""Router gates protect no-network paths and avoid retaining supplied state."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('project_jev_router', Path(__file__).resolve().parents[1] / 'scripts/jev_router.py')
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.data = {'task': 'Find the cause of a failed check', 'context': 'No diagnostic read yet',
                     'options': {'inspect': 'Read the failing receipt', 'retry': 'Repeat the check'}}

    def fail_backend(self, data):
        self.fail('This path must not invoke a model')

    def test_simple_and_bypass_never_invoke_backend(self):
        for reason in router.SKIPS:
            result = router.decide(skip=reason, backend=self.fail_backend)
            self.assertFalse(result['api_called'])

    def test_dry_run_never_invokes_backend(self):
        result = router.decide(self.data, dry_run=True, backend=self.fail_backend)
        self.assertEqual(result['status'], 'dry_run')
        self.assertFalse(result['api_called'])

    def test_bad_or_unbounded_input_never_invokes_backend(self):
        for data in [None, {}, {**self.data, 'context': 'x' * 6001},
                     {**self.data, 'options': {'abstain': 'reserved', 'ok': 'fine'}},
                     {**self.data, 'context': 'Bearer PRIVATE_TEST_SENTINEL'}]:
            with self.assertRaises(ValueError):
                router.decide(data, backend=self.fail_backend)

    def test_advice_does_not_execute_or_log_supplied_content(self):
        result = router.decide(self.data, backend=lambda _: {
            'choice': 'inspect', 'model': 'fixture', 'confidence': .8})
        self.assertEqual(result['status'], 'advice')
        self.assertFalse(result['executes_actions'])
        self.assertEqual(result['mode'], 'shadow')
        self.assertNotIn(self.data['task'], json.dumps(result))
        self.assertNotIn(self.data['context'], json.dumps(result))

    def test_backend_failure_does_not_leak_exception_content(self):
        def broken(data):
            raise RuntimeError('PRIVATE_TEST_SENTINEL')
        result = router.decide(self.data, backend=broken)
        self.assertEqual(result['status'], 'unavailable')
        self.assertIsNone(result['api_called'])
        self.assertNotIn('PRIVATE_TEST_SENTINEL', json.dumps(result))

    def test_receipts_are_private_and_append_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'receipts.jsonl'
            router.append({'id': 'first'}, path)
            router.append({'id': 'second'}, path)
            self.assertEqual([json.loads(s)['id'] for s in path.read_text().splitlines()], ['first', 'second'])
            if __import__('os').name != 'nt':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
