import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import forge_e2e

class WarmupTests(unittest.TestCase):
    def test_plain_and_remote_judges_never_require_ollama(self):
        sides = [{"mode": "off"}, {"mode": "active", "decision_overrides": {"backend": "jev"}},
                 {"mode": "active", "composition": "composed", "source_root": "."}]
        with patch.object(forge_e2e, "composed_effective_config", return_value={"backend": "jev"}), \
             patch.object(forge_e2e.urllib.request, "urlopen") as call:
            for side in sides: forge_e2e._warm_local_scorer(side)
        call.assert_not_called()

    def test_local_warms_the_configured_model(self):
        with patch.object(forge_e2e.urllib.request, "urlopen") as call, patch.object(forge_e2e.json, "load"):
            forge_e2e._warm_local_scorer({"mode": "active", "decision_overrides": {
                "backend": "ollama", "model": "custom-model", "ollama_url": "http://127.0.0.1:9999"}})
        request = call.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:9999/api/generate")
        self.assertEqual(json.loads(request.data)["model"], "custom-model")
