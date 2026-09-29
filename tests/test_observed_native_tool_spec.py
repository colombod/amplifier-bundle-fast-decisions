"""Model-native tool specs must survive ObservedTool wrapping.

loop-streaming builds each tool's model-facing spec with
``getattr(type(tool), "native_tool_spec", None)`` -- a TYPE-level read.
A model-native tool (Anthropic's ``computer_20251124``, native web search)
declares ``native_tool_spec`` on its class so the provider sends it in its
native shape. ObservedTool wraps every tool, and its ``__getattr__`` is
instance-level, which a type-level getattr never consults -- so wrapping
silently downgraded native tools to ordinary function tools.

No amplifier_core, no network. See orchestrator.py's ObservedTool.
"""

from __future__ import annotations

import unittest

from amplifier_fast_decisions.backends import BackendUnavailable
from amplifier_fast_decisions.contracts import Policy, TurnState
from amplifier_fast_decisions.orchestrator import ObservedTool
from amplifier_fast_decisions.runtime import Runtime
from amplifier_fast_decisions.service import DecisionService
from amplifier_fast_decisions.telemetry import Emitter

# The provider's native definition, fixed by the provider and declared at
# CLASS level -- exactly how a model-native tool opts in.
COMPUTER_USE_SPEC = {
    "type": "computer_20251124",
    "name": "computer",
    "display_width_px": 1280,
    "display_height_px": 360,
}


class FakeNativeTool:
    """A model-native tool: ``native_tool_spec`` lives on the class."""

    native_tool_spec = COMPUTER_USE_SPEC
    name = "computer"
    description = "Use a mouse and keyboard to interact with a computer."
    input_schema = {"type": "object", "properties": {"action": {"type": "string"}}}

    def __init__(self, result=None):
        self.result = result if result is not None else {"success": True, "output": "ok"}
        self.calls = []

    async def execute(self, input, **kwargs):
        self.calls.append(input)
        return self.result


class FakePlainTool:
    """An ordinary function tool: no native spec anywhere."""

    name = "bash"
    description = "Run a shell command."
    input_schema = {"type": "object", "properties": {"command": {"type": "string"}}}

    def __init__(self, result=None):
        self.result = result if result is not None else {"success": True, "output": "ok"}
        self.calls = []

    async def execute(self, input, **kwargs):
        self.calls.append(input)
        return self.result


class FakeBackend:
    """Never consulted in these tests (mode='off'); present to build a service."""

    name = "fake-native"

    def __init__(self):
        self.calls = 0

    async def ask(self, request):
        self.calls += 1
        raise BackendUnavailable("synthetic backend, never asked in these tests")

    async def close(self):
        pass


def setup_service(*, policy=None, backend=None):
    events = []
    policy = policy if policy is not None else Policy(mode="off")
    backend = backend if backend is not None else FakeBackend()
    emitter = Emitter("test-session", callback=events.append)
    service = DecisionService(policy, backend, emitter, coordinator=None, configured_candidates=[])
    service.turn = TurnState("test-turn")
    runtime = Runtime(service)
    return service, runtime, events


class ObservedNativeToolSpecTests(unittest.TestCase):
    """The type-level read loop-streaming actually performs."""

    def test_native_spec_survives_wrapping(self):
        service, runtime, events = setup_service()
        tool = FakeNativeTool()

        # The exact construction the orchestrator's execute() performs.
        observed = ObservedTool(tool, runtime, "computer", workspace=None, levers=None)

        self.assertIsNotNone(getattr(type(observed), "native_tool_spec", None))
        # loop-streaming reads the value off the instance after the type probe.
        self.assertIs(observed.native_tool_spec, COMPUTER_USE_SPEC)

    def test_native_property_runs_on_original_instance(self):
        class DerivedNativeTool(FakeNativeTool):
            @property
            def native_tool_spec(self):
                return {**super().native_tool_spec, "display_width_px": self.width}

        _, runtime, _ = setup_service()
        tool = DerivedNativeTool()
        tool.width = 640
        observed = ObservedTool(tool, runtime, "computer")
        self.assertEqual(observed.native_tool_spec, tool.native_tool_spec)
        tool.width = 1920
        self.assertEqual(observed.native_tool_spec["display_width_px"], 1920)

    def test_instance_override_and_replaced_class_spec_stay_live(self):
        class NativeTool(FakeNativeTool):
            pass

        _, runtime, _ = setup_service()
        first, second = NativeTool(), NativeTool()
        first.native_tool_spec = {**COMPUTER_USE_SPEC, "display_width_px": 800}
        wrapped_first = ObservedTool(first, runtime, "computer")
        wrapped_second = ObservedTool(second, runtime, "computer")
        self.assertIs(type(wrapped_first), type(wrapped_second))
        self.assertIs(wrapped_first.native_tool_spec, first.native_tool_spec)
        NativeTool.native_tool_spec = {**COMPUTER_USE_SPEC, "display_width_px": 1920}
        self.assertIs(wrapped_second.native_tool_spec, NativeTool.native_tool_spec)
        self.assertEqual(wrapped_first.native_tool_spec["display_width_px"], 800)

    def test_plain_tool_still_has_no_native_spec(self):
        service, runtime, events = setup_service()
        tool = FakePlainTool()

        observed = ObservedTool(tool, runtime, "bash", workspace=None, levers=None)

        self.assertIsNone(getattr(type(observed), "native_tool_spec", None))
        self.assertIsInstance(observed, ObservedTool)

    def test_delegated_attributes_are_unchanged(self):
        service, runtime, events = setup_service()
        native = ObservedTool(FakeNativeTool(), runtime, "computer", workspace=None, levers=None)
        plain = ObservedTool(FakePlainTool(), runtime, "bash", workspace=None, levers=None)

        self.assertEqual(native.name, "computer")
        self.assertEqual(native.input_schema, FakeNativeTool.input_schema)
        self.assertEqual(plain.description, "Run a shell command.")
        self.assertIsInstance(native, ObservedTool)


class ObservedNativeToolExecuteTests(unittest.IsolatedAsyncioTestCase):
    """Wrapping a native tool must not cost us the observation it exists for."""

    async def test_native_tool_still_executes_and_is_observed(self):
        service, runtime, events = setup_service()
        tool = FakeNativeTool()
        observed = ObservedTool(tool, runtime, "computer", workspace=None, levers=None)

        result = await observed.execute({"action": "screenshot"})

        self.assertEqual(result, tool.result)
        self.assertEqual(tool.calls, [{"action": "screenshot"}])
        starts = [e for e in events if e["event"].endswith("tool_start")]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["data"]["tool"], "computer")

    async def test_plain_tool_observation_is_unchanged(self):
        service, runtime, events = setup_service()
        tool = FakePlainTool()
        observed = ObservedTool(tool, runtime, "bash", workspace=None, levers=None)

        result = await observed.execute({"command": "echo hi"})

        self.assertEqual(result, tool.result)
        self.assertEqual(tool.calls, [{"command": "echo hi"}])
        starts = [e for e in events if e["event"].endswith("tool_start")]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["data"]["tool"], "bash")


class UpstreamBuildToolSpecTests(unittest.TestCase):
    """The real consumer, when loop-streaming is installed (upstream lane)."""

    def setUp(self):
        try:
            from amplifier_module_loop_streaming import _build_tool_spec
        except Exception:  # pragma: no cover - offline lane
            self.skipTest("requires installed upstream amplifier-module-loop-streaming")
        self._build_tool_spec = _build_tool_spec

    def test_wrapped_native_tool_keeps_its_native_shape(self):
        service, runtime, events = setup_service()
        observed = ObservedTool(FakeNativeTool(), runtime, "computer", workspace=None, levers=None)

        spec = self._build_tool_spec(observed)

        self.assertEqual(getattr(spec, "type", None), "computer_20251124")
        self.assertEqual(spec.name, "computer")

    def test_wrapped_plain_tool_stays_a_function_tool(self):
        service, runtime, events = setup_service()
        observed = ObservedTool(FakePlainTool(), runtime, "bash", workspace=None, levers=None)

        spec = self._build_tool_spec(observed)

        self.assertIsNone(getattr(spec, "type", None))
        self.assertEqual(spec.name, "bash")


if __name__ == "__main__":
    unittest.main()
