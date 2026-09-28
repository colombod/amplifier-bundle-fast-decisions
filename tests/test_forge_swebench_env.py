"""forge_swebench.cmd_agent launches the real `amplifier run` CLI, which
inherits os.environ (and with it whatever memory bundle is installed for
the launching user). AMPLIFIER_MEMORY_CAPTURE=off must be set in that
subprocess's env so a SWE-bench worker run never writes a memory capture
per tool call into the launching user's personal memory store.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SWEBENCH_DIR = REPO_ROOT / "evals" / "swebench"
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(SWEBENCH_DIR))

import forge_swebench  # noqa: E402

_real_popen = forge_swebench.subprocess.Popen


class CmdAgentEnvTests(unittest.TestCase):
    def test_cmd_agent_sets_amplifier_memory_capture_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "runs" / "r1"
            workspace = run_dir / "workspace"
            workspace.mkdir(parents=True)
            (run_dir / "profile.md").write_text("---\n{}\n---\n")
            (run_dir / "prompt.txt").write_text("do the thing")
            manifest = {
                "runs": {
                    "r1": {
                        "model": "claude-fable-5-1",
                        "base_commit": "HEAD",
                    }
                },
                "deadline_seconds": 5,
            }
            (root / "manifest.json").write_text(json.dumps(manifest))

            captured = {}

            class FakeProcess:
                def wait(self, timeout=None):
                    return 0

            def fake_popen(argv, *a, **kw):
                if argv[:2] == ["amplifier", "run"]:
                    captured["env"] = kw.get("env")
                    captured["argv"] = argv
                    return FakeProcess()
                return _real_popen(argv, *a, **kw)

            args = SimpleNamespace(root=str(root), name="r1")
            with patch.object(forge_swebench.subprocess, "Popen", fake_popen):
                forge_swebench.cmd_agent(args)

            self.assertEqual(captured["env"]["AMPLIFIER_MEMORY_CAPTURE"], "off")
            self.assertEqual(captured["env"]["AFAST_TRAFFIC"], "test")
            self.assertEqual(captured["argv"][0:2], ["amplifier", "run"])


if __name__ == "__main__":
    unittest.main()
