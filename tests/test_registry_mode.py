"""Registry attachment (registry.py): the facades mounted in the coordinator's
provider/tool registries instead of by replacing the orchestrator.

The first classes use contract doubles only. RealLoopTests runs the INSTALLED
loop-streaming (and loop-live, when present) and is skipped elsewhere:

    $(python3 tests/_hostpy.py | head -1) -m unittest tests.test_registry_mode -v
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace as NS

import yaml

from amplifier_fast_decisions.demo import DemoCoordinator, DemoProvider
from amplifier_fast_decisions.orchestrator import ObservedTool, RoutedProvider
from amplifier_fast_decisions.registry import RegistryRouter, _contains_ours, mount
from amplifier_fast_decisions.runtime import get_runtime

ROOT = Path(__file__).resolve().parents[1]
HAS_CORE = importlib.util.find_spec("amplifier_core") is not None
HAS_LOOP = importlib.util.find_spec("amplifier_module_loop_streaming") is not None
HAS_LIVE = importlib.util.find_spec("amplifier_module_loop_live") is not None


class EchoTool:
    name = "echo"
    description = "Echo"
    input_schema = {"type": "object"}

    def __init__(self):
        self.calls = []

    async def execute(self, input, **kwargs):
        self.calls.append(input)
        return NS(success=True, output="ok", error=None)


class HostWrapper:
    """A host-side wrapper around a registry entry (loop-live's AsyncTool)."""

    def __init__(self, tool):
        self.tool = tool


def events(coordinator, name):
    return [data for event, data in coordinator.hooks.events if event == "fast_decisions:" + name]


def request(tools=True):
    return NS(messages=[{"role": "user", "content": "hi"}],
              tools=[{"name": "echo"}] if tools else None, tool_choice="auto", model=None)


class RegistryFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.coordinator = DemoCoordinator()
        self.provider = DemoProvider(delay_ms=0)
        self.tool = EchoTool()
        await self.coordinator.mount("providers", self.provider, name="demo")
        await self.coordinator.mount("tools", self.tool, name="echo")
        config = {"mode": "active", "backend": "unavailable", "events_dir": self._tmp.name}
        self.runtime, _ = get_runtime(self.coordinator, config, owner=True)
        self.router = RegistryRouter(self.coordinator, self.runtime, config)
        self.router.register(self.coordinator.hooks)
        await self.router.attach()

    async def asyncTearDown(self):
        await self.runtime.close()
        self._tmp.cleanup()

    def registry(self, point):
        return self.coordinator.get(point)


class AttachTests(RegistryFixture):
    async def test_wraps_every_provider_and_tool(self):
        self.assertIsInstance(self.registry("providers")["demo"], RoutedProvider)
        self.assertIsInstance(self.registry("tools")["echo"], ObservedTool)
        self.assertTrue(self.registry("providers")["demo"]._registry_gate)

    async def test_attach_is_idempotent(self):
        provider = self.registry("providers")["demo"]
        await self.router.attach()
        self.assertIs(self.registry("providers")["demo"], provider)

    async def test_host_wrapper_around_facade_is_not_rewrapped(self):
        await self.coordinator.mount("tools", HostWrapper(self.registry("tools")["echo"]), name="echo")
        await self.router.attach()
        self.assertIsInstance(self.registry("tools")["echo"], HostWrapper)

    async def test_late_mount_is_wrapped_at_next_turn(self):
        late = EchoTool()
        await self.coordinator.mount("tools", late, name="late")
        await self.coordinator.hooks.emit("execution:start", {"prompt": "p"})
        self.assertIsInstance(self.registry("tools")["late"], ObservedTool)

    async def test_judge_sees_raw_tools(self):
        self.assertIs(self.router.raw_tools["echo"], self.tool)
        self.assertEqual(list(self.router.raw_tools), ["echo"])

    def test_contains_ours_reads_instance_state_only(self):
        class Proxy:
            def __getattr__(self, name):
                raise AssertionError("__getattr__ must not be used")
        self.assertFalse(_contains_ours(Proxy()))

    async def test_policy_owner_stands_down_shadow_scoring(self):
        self.assertTrue(self.runtime.orchestrator_owned)


class PassthroughTests(RegistryFixture):
    async def test_call_outside_turn_reaches_provider_unchanged(self):
        req = request()
        await self.registry("providers")["demo"].complete(req)
        self.assertIs(self.provider.requests[-1], req)
        self.assertEqual(events(self.coordinator, "routed"), [])

    async def test_call_without_tools_in_turn_is_not_routed(self):
        await self.coordinator.hooks.emit("execution:start", {"prompt": "p"})
        await self.registry("providers")["demo"].complete(request(tools=False))
        self.assertEqual(events(self.coordinator, "routed"), [])

    async def test_explicit_model_in_turn_is_not_routed(self):
        await self.coordinator.hooks.emit("execution:start", {"prompt": "p"})
        await self.registry("providers")["demo"].complete(request(), model="user-pick")
        self.assertEqual(events(self.coordinator, "routed"), [])

    async def test_loop_step_in_turn_is_routed(self):
        await self.coordinator.hooks.emit("execution:start", {"prompt": "p"})
        await self.registry("providers")["demo"].complete(request())
        self.assertTrue(events(self.coordinator, "routed"))

    async def test_orchestrator_mode_facade_is_unchanged(self):
        # Outside registry attachment the gate is off: a tool-less call is
        # still handled by the facade, exactly as before.
        facade = RoutedProvider(DemoProvider(delay_ms=0), self.runtime, {})
        self.runtime.service.turn = None
        self.assertFalse(facade._registry_passthrough(request(tools=False), {}))


class TurnLifecycleTests(RegistryFixture):
    async def test_turn_opens_and_closes_on_final_completion(self):
        await self.coordinator.hooks.emit("execution:start", {"prompt": "p"})
        self.assertIsNotNone(self.runtime.service.turn)
        self.assertTrue(events(self.coordinator, "turn_start")[0]["data"]["engine"].startswith("registry:"))
        await self.coordinator.hooks.emit("orchestrator:complete", {"status": "success", "goal_final": True})
        self.assertIsNone(self.runtime.service.turn)
        self.assertEqual(events(self.coordinator, "turn_end")[0]["data"]["status"], "ok")

    async def test_goal_continuation_stays_in_one_turn(self):
        await self.coordinator.hooks.emit("execution:start", {"prompt": "p"})
        await self.coordinator.hooks.emit("orchestrator:complete", {"status": "success", "goal_final": False})
        self.assertIsNotNone(self.runtime.service.turn)

    async def test_missing_execution_end_is_backfilled_once(self):
        await self.coordinator.hooks.emit("execution:start", {"prompt": "p"})
        await self.coordinator.hooks.emit("orchestrator:complete", {"status": "cancelled"})
        ends = [d for e, d in self.coordinator.hooks.events if e == "execution:end"]
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]["status"], "cancelled")

    async def test_native_execution_end_is_not_duplicated(self):
        await self.coordinator.hooks.emit("execution:start", {"prompt": "p"})
        await self.coordinator.hooks.emit("execution:end", {"status": "completed"})
        await self.coordinator.hooks.emit("orchestrator:complete", {"status": "success"})
        self.assertEqual(sum(e == "execution:end" for e, _ in self.coordinator.hooks.events), 1)

    async def test_turn_left_open_is_closed_by_next_start(self):
        await self.coordinator.hooks.emit("execution:start", {"prompt": "p"})
        first = self.runtime.service.turn.id
        await self.coordinator.hooks.emit("execution:start", {"prompt": "q"})
        self.assertNotEqual(self.runtime.service.turn.id, first)
        self.assertEqual(events(self.coordinator, "turn_end")[0]["data"]["status"], "abandoned")

    async def test_session_end_closes_open_turn(self):
        await self.coordinator.hooks.emit("execution:start", {"prompt": "p"})
        await self.coordinator.hooks.emit("session:end", {})
        self.assertIsNone(self.runtime.service.turn)

    async def test_tool_runs_through_facade_in_turn(self):
        await self.coordinator.hooks.emit("execution:start", {"prompt": "p"})
        await self.registry("tools")["echo"].execute({"x": 1})
        self.assertEqual(self.tool.calls, [{"x": 1}])
        self.assertTrue(events(self.coordinator, "tool_start"))


@unittest.skipUnless(HAS_CORE, "amplifier_core not installed (real-kernel lane only)")
class MountTests(unittest.IsolatedAsyncioTestCase):
    async def test_mount_attaches_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = DemoCoordinator()
            await coordinator.mount("providers", DemoProvider(delay_ms=0), name="demo")
            cleanup = await mount(coordinator, {"mode": "active", "backend": "unavailable",
                                                "events_dir": tmp, "observatory": {"enabled": False}})
            self.assertIsInstance(coordinator.get("providers")["demo"], RoutedProvider)
            await cleanup()


class OwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_backend_replaces_shadow_hook_backend(self):
        """Hook order can mount the shadow observer (backend: deterministic)
        before the router. Active routing must use the router's backend."""
        from amplifier_fast_decisions.backends import ScriptedBackend, UnavailableBackend
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = DemoCoordinator()
            runtime, created = get_runtime(coordinator, {"mode": "shadow", "backend": "deterministic",
                                                         "events_dir": tmp})
            self.assertTrue(created)
            self.assertIsInstance(runtime.service.backend, ScriptedBackend)
            same, created = get_runtime(coordinator, {"mode": "active", "backend": "unavailable",
                                                      "events_dir": tmp}, owner=True)
            self.assertIs(same, runtime)
            self.assertFalse(created)
            self.assertIsInstance(runtime.service.backend, UnavailableBackend)
            self.assertEqual(runtime.service.policy.mode, "active")
            await runtime.close()

    async def test_later_non_owner_does_not_replace_owner_backend(self):
        from amplifier_fast_decisions.backends import UnavailableBackend
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = DemoCoordinator()
            runtime, _ = get_runtime(coordinator, {"mode": "active", "backend": "unavailable",
                                                   "events_dir": tmp}, owner=True)
            get_runtime(coordinator, {"mode": "shadow", "backend": "deterministic", "events_dir": tmp})
            self.assertIsInstance(runtime.service.backend, UnavailableBackend)
            await runtime.close()


class BehaviorTests(unittest.TestCase):
    def setUp(self):
        self.primary = yaml.safe_load((ROOT / "behaviors/fast-decisions.yaml").read_text())
        self.registry = yaml.safe_load((ROOT / "behaviors/fast-decisions-registry.yaml").read_text())

    def test_router_config_matches_orchestrator_config(self):
        router = next(h for h in self.registry["hooks"] if h["module"] == "hooks-fast-decisions-router")
        self.assertEqual(router["config"], self.primary["session"]["orchestrator"]["config"])

    def test_does_not_replace_the_orchestrator(self):
        self.assertNotIn("session", self.registry)

    def test_keeps_the_shadow_bridge_and_workspace_tool(self):
        self.assertEqual([h["module"] for h in self.registry["hooks"]],
                         ["hooks-fast-decisions-router", "hooks-fast-decisions"])
        self.assertEqual(self.registry["tools"], self.primary["tools"])


@unittest.skipUnless(HAS_LOOP, "amplifier_module_loop_streaming not installed (real-kernel lane only)")
class RealLoopTests(unittest.IsolatedAsyncioTestCase):
    """The installed loop, unmodified, with the facades only in the registry."""

    class Provider:
        name = "fake"

        def __init__(self, repeats=1, tool="echo", arguments=None):
            self.calls = 0
            self.repeats = repeats
            self.tool, self.arguments = tool, arguments or {"x": 1}
            self.requests = []

        def get_info(self):
            return NS(id=self.name, display_name=self.name, context_window=32000)

        async def list_models(self):
            return []

        def parse_tool_calls(self, response):
            return response.tool_calls or []

        async def complete(self, request, **kwargs):
            from amplifier_core.message_models import ChatResponse, ToolCall, Usage
            self.calls += 1
            self.requests.append(request)
            usage = Usage(input_tokens=1, output_tokens=1, total_tokens=2)
            if self.calls <= self.repeats:
                return ChatResponse(content=[], usage=usage,
                                    tool_calls=[ToolCall(id=f"c{self.calls}", name=self.tool, arguments=self.arguments)])
            return ChatResponse(content=[{"type": "text", "text": "done"}], tool_calls=[], usage=usage)

    async def run_loop(self, loop, repeats=1, config=None, tool_name="echo", arguments=None):
        from amplifier_core.testing import MockContextManager, MockCoordinator
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = MockCoordinator()
            provider, tool = self.Provider(repeats, tool_name, arguments), EchoTool()
            tool.name = tool_name
            await coordinator.mount("providers", provider, name="fake")
            await coordinator.mount("tools", tool, name=tool_name)
            seen = []

            async def record(event, data):
                from amplifier_core.models import HookResult
                seen.append(event)
                return HookResult(action="continue")

            for name in ("fast_decisions:turn_start", "fast_decisions:turn_end",
                         "fast_decisions:tool_start", "fast_decisions:routed", "execution:end",
                         "fast_decisions:waste_guard"):
                coordinator.hooks.register(name, record)
            cleanup = await mount(coordinator, {"mode": "active", "backend": "unavailable",
                                                "events_dir": tmp, "observatory": {"enabled": False},
                                                **(config or {})})
            try:
                # What amplifier_core's session.execute passes: the live registries.
                result = await loop.execute("hi", MockContextManager(), coordinator.get("providers"),
                                            coordinator.get("tools"), coordinator.hooks,
                                            coordinator=coordinator)
            finally:
                await cleanup()
            return result, provider, tool, seen

    def assert_parity(self, result, provider, tool, seen):
        self.assertEqual(result, "done")
        self.assertEqual(provider.calls, 2)
        self.assertEqual(tool.calls, [{"x": 1}])
        self.assertEqual(seen.count("fast_decisions:turn_start"), 1)
        self.assertEqual(seen.count("fast_decisions:turn_end"), 1)
        self.assertIn("fast_decisions:routed", seen)
        self.assertIn("fast_decisions:tool_start", seen)
        self.assertEqual(seen.count("execution:end"), 1)

    async def test_loop_streaming(self):
        from amplifier_module_loop_streaming import StreamingOrchestrator
        self.assert_parity(*await self.run_loop(StreamingOrchestrator({})))

    @unittest.skipUnless(HAS_LIVE, "amplifier_module_loop_live not installed")
    async def test_loop_live(self):
        from amplifier_module_loop_live.orchestrator import BundleLiveOrchestrator
        self.assert_parity(*await self.run_loop(BundleLiveOrchestrator({})))

    async def assert_repeat_stopped(self, loop):
        """The model issues the same unchanged call four times: the fourth is
        not run and the model is told to use the earlier result."""
        _, provider, tool, seen = await self.run_loop(
            loop, repeats=4, config={"waste_guards": {"enabled": True}},
            tool_name="bash", arguments={"command": "git status"})
        self.assertEqual(len(tool.calls), 3)
        self.assertEqual(seen.count("fast_decisions:waste_guard"), 1)
        last = str(provider.requests[-1].messages[-1])
        self.assertIn("earlier result", last)

    async def test_waste_guard_steers_loop_streaming(self):
        from amplifier_module_loop_streaming import StreamingOrchestrator
        await self.assert_repeat_stopped(StreamingOrchestrator({}))

    @unittest.skipUnless(HAS_LIVE, "amplifier_module_loop_live not installed")
    async def test_waste_guard_steers_loop_live(self):
        from amplifier_module_loop_live.orchestrator import BundleLiveOrchestrator
        await self.assert_repeat_stopped(BundleLiveOrchestrator({}))


if __name__ == "__main__":
    unittest.main()
