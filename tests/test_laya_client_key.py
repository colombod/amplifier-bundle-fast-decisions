import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ClientKeyTests(unittest.TestCase):
    def test_private_key_hash_and_refuse_overwrite(self):
        script = Path(__file__).resolve().parents[1] / 'scripts/laya-client-key.py'
        with tempfile.TemporaryDirectory() as tmp:
            store, token = Path(tmp)/'auth/clients.json', Path(tmp)/'private/alice.token'
            command = [sys.executable, str(script), '--client', 'alice',
                       '--store', str(store), '--token-file', str(token)]
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            secret = token.read_text().strip()
            self.assertGreaterEqual(len(secret), 40)
            self.assertNotIn(secret, result.stdout + result.stderr)
            self.assertEqual(json.loads(store.read_text()), {'alice':hashlib.sha256(secret.encode()).hexdigest()})
            if os.name == 'posix':
                self.assertEqual(token.stat().st_mode & 0o777, 0o600)
                self.assertEqual(store.stat().st_mode & 0o777, 0o600)
            again = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(again.returncode, 0)
            self.assertEqual(token.read_text().strip(), secret)
