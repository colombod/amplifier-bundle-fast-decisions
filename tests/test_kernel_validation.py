"""Real-kernel validation lane.

Runs amplifier_core's own OrchestratorValidator/HookValidator/ToolValidator
against our modules' mount() functions, using the exact configs shipped in
bundles/shadow.yaml and behaviors/fast-decisions.yaml. These validators call
mount() against a real (Rust-backed) MockCoordinator and assert against its
actual mount-point contract -- unlike our unit tests, which mount into a
permissive fake coordinator that accepts any mount-point name.

This is the test that would have caught the "session" vs "orchestrator"
mount-point defect: our fake coordinator in test_decisions.py has no concept
of valid mount points, so it silently accepted the wrong name.

Skipped (not failed) when amplifier_core is not importable -- run this file
with the real Amplifier CLI's interpreter (which has amplifier_core 1.6.1)
to exercise it:

    python3 tests/_hostpy.py  # prints the resolved interpreter path, or why none was found
    $(python3 tests/_hostpy.py | head -1) \
        -m unittest tests.test_kernel_validation -v
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
HAS_CORE = importlib.util.find_spec("amplifier_core") is not None


def _active_orchestrator_config() -> dict:
    """bundles/shadow.yaml no longer swaps the orchestrator (P3): shadow
    measurement lives on the hook and composes onto any orchestrator. The
    only remaining bundle that configures amplifier_fast_decisions.orchestrator
    is bundles/active.yaml -- use it to validate the orchestrator's own mount
    contract (unrelated to shadow/active mode, which the hook config below
    exercises separately via behaviors/fast-decisions.yaml)."""
    data = yaml.safe_load(
        (ROOT / "bundles" / "active.yaml").read_text(encoding="utf-8")
    )
    return data["session"]["orchestrator"]["config"]


def _behavior_configs() -> tuple[dict, dict]:
    """Return (hook_config, tool_config) from behaviors/fast-decisions-shadow.yaml."""
    data = yaml.safe_load(
        (ROOT / "behaviors" / "fast-decisions-shadow.yaml").read_text(encoding="utf-8")
    )
    hook_config = data["hooks"][0]["config"]
    tool_config = data["tools"][0]["config"]
    return hook_config, tool_config


@unittest.skipUnless(HAS_CORE, "amplifier_core not installed (real-kernel lane only)")
class KernelValidationTests(unittest.IsolatedAsyncioTestCase):
    """Validate our modules against amplifier_core's own protocol validators."""

    def setUp(self):
        # The shipped configs carry no events_dir: without this the mounted
        # modules would record into ~/.amplifier/fast-decisions/events (the
        # test-session-*.jsonl files seen there).
        import os
        import tempfile
        from unittest import mock
        self._events = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"AFAST_EVENTS_DIR": self._events.name,
                                               "AFAST_OBSERVATORY": "off"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._events.cleanup)

    @staticmethod
    def _format_errors(result) -> str:
        lines = [result.summary()]
        for check in result.errors:
            lines.append(f"  [{check.severity}] {check.name}: {check.message}")
        return "\n".join(lines)

    async def test_orchestrator_mount_point_contract(self):
        from amplifier_core.validation.orchestrator import OrchestratorValidator

        config = _active_orchestrator_config()
        result = await OrchestratorValidator().validate(
            "amplifier_fast_decisions.orchestrator", config=config
        )
        self.assertTrue(result.passed, self._format_errors(result))

    async def test_hook_mount_contract(self):
        from amplifier_core.validation.hook import HookValidator

        hook_config, _ = _behavior_configs()
        result = await HookValidator().validate(
            "amplifier_fast_decisions.observer", config=hook_config
        )
        self.assertTrue(result.passed, self._format_errors(result))

    async def test_tool_mount_contract(self):
        from amplifier_core.validation.tool import ToolValidator

        _, tool_config = _behavior_configs()
        result = await ToolValidator().validate(
            "amplifier_fast_decisions.workspace", config=tool_config
        )
        self.assertTrue(result.passed, self._format_errors(result))


if __name__ == "__main__":
    unittest.main()
