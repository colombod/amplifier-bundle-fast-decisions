"""Orchestrator-primary composition: loop-fast-decisions replacing the host's
loop-streaming. No amplifier_core, no network -- contract doubles only."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from types import SimpleNamespace as NS

from amplifier_fast_decisions.backends import ScriptedBackend
from amplifier_fast_decisions.contracts import Policy, TurnState
from amplifier_fast_decisions.demo import DemoCoordinator, DemoProvider, demo_response
from amplifier_fast_decisions.observer import install_auto_observatory
from amplifier_fast_decisions.orchestrator import (
    HybridOrchestrator,
    RoutedProvider,
    _register_upstream_capabilities,
    upstream_config,
)
from amplifier_fast_decisions.runtime import Runtime, get_runtime
from amplifier_fast_decisions.service import DecisionService
from amplifier_fast_decisions.telemetry import Emitter


def routed_request():
    return NS(messages=[{"role": "user", "content": "Fix the bug"}], tools=[], tool_choice="auto")


def setup_service(policy):
    events = []
    coordinator = DemoCoordinator()
    emitter = Emitter(coordinator.session_id, callback=events.append)
    service = DecisionService(policy, ScriptedBackend(delay_ms=0), emitter, coordinator, [])
    service.turn = TurnState("t")
    return service, Runtime(service), events


class UpstreamConfigTests(unittest.TestCase):
    def test_root_loop_streaming_keys_are_forwarded(self):
        # What the kernel's deep merge leaves behind when this module replaces
        # foundation's loop-streaming: its settings sit at the top level.
        config = {"mode": "active", "backend": "none", "extended_thinking": True,
                  "max_iterations": 40, "goal_stall_threshold": 2,
                  "model_routing": {"start_model": "m"}}
        self.assertEqual(upstream_config(config), {
            "extended_thinking": True, "max_iterations": 40, "goal_stall_threshold": 2})

    def test_explicit_upstream_block_wins(self):
        config = {"extended_thinking": True, "upstream": {"extended_thinking": False, "stream_delay": 0}}
        self.assertEqual(upstream_config(config), {"extended_thinking": False, "stream_delay": 0})

    def test_decision_policy_keys_never_forwarded(self):
        forwarded = upstream_config({"mode": "active", "timeout_ms": 500, "allowed_tools": ["x"],
                                     "effort_routing": {"explore": "low"}, "observatory": {}})
        self.assertEqual(forwarded, {})

    def test_hybrid_builds_upstream_with_forwarded_config(self):
        seen = {}

        class FakeLoop:
            def __init__(self, config):
                seen.update(config)

        import amplifier_fast_decisions.orchestrator as orch
        original = orch._import_upstream_loop
        orch._import_upstream_loop = lambda: FakeLoop
        try:
            _, runtime, _ = setup_service(Policy(mode="active"))
            HybridOrchestrator({"extended_thinking": True, "mode": "active"}, DemoCoordinator(), runtime)
        finally:
            orch._import_upstream_loop = original
        self.assertEqual(seen, {"extended_thinking": True})


class UpstreamCapabilityTests(unittest.TestCase):
    def test_steer_pin_and_event_names_registered(self):
        import sys
        import types

        module = types.ModuleType("fake_loop_streaming_for_test")

        class ConversationProviderPin:
            def __init__(self, orchestrator, coordinator):
                self.orchestrator, self.coordinator = orchestrator, coordinator

        class StreamingOrchestrator:
            def steer(self, message):
                return message

        StreamingOrchestrator.__module__ = module.__name__
        setattr(module, "ConversationProviderPin", ConversationProviderPin)
        setattr(module, "StreamingOrchestrator", StreamingOrchestrator)
        sys.modules[module.__name__] = module
        try:
            coordinator = DemoCoordinator()
            upstream = StreamingOrchestrator()
            registered = _register_upstream_capabilities(coordinator, upstream)
        finally:
            del sys.modules[module.__name__]
        self.assertEqual(registered, ["session.steer", "conversation.provider_pin"])
        self.assertEqual(coordinator.get_capability("session.steer")("x"), "x")
        pin = coordinator.get_capability("conversation.provider_pin")
        self.assertIs(pin.orchestrator, upstream)
        names = [n for cb in coordinator.contributors["observability.events"] for n in cb()]
        self.assertIn("execution:end", names)

    def test_missing_upstream_features_are_tolerated(self):
        coordinator = DemoCoordinator()
        self.assertEqual(_register_upstream_capabilities(coordinator, object()), [])


class ExecutionEndBackfillTests(unittest.IsolatedAsyncioTestCase):
    """The orchestrator contract requires execution:end on every exit path;
    loop-streaming skips it on early returns and exceptions."""

    async def _run(self, upstream_execute):
        coordinator = DemoCoordinator()
        hooks = coordinator.hooks

        class Upstream:
            async def execute(self, prompt, context, providers, tools, hooks, **kwargs):
                return await upstream_execute(hooks)

        with tempfile.TemporaryDirectory() as tmp:
            config = {"backend": "none", "events_dir": tmp}
            runtime, _ = get_runtime(coordinator, config, owner=True)
            orch = HybridOrchestrator(config, coordinator, runtime, upstream=Upstream())
            orch.register_lifecycle_hooks(hooks)
            try:
                try:
                    await orch.execute("hi", NS(), {}, {}, hooks)
                except RuntimeError:
                    pass
            finally:
                await runtime.close()
        return [(e, d) for e, d in hooks.events if e == "execution:end"]

    async def test_backfills_when_upstream_raises_after_start(self):
        async def upstream(hooks):
            await hooks.emit("execution:start", {"prompt": "hi"})
            raise RuntimeError("provider exploded")

        ends = await self._run(upstream)
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0][1]["status"], "error")
        self.assertEqual(ends[0][1]["source"], "loop-fast-decisions")

    async def test_no_duplicate_when_upstream_emits_end(self):
        async def upstream(hooks):
            await hooks.emit("execution:start", {"prompt": "hi"})
            await hooks.emit("execution:end", {"response": "", "status": "completed"})
            return "done"

        ends = await self._run(upstream)
        self.assertEqual(len(ends), 1)
        self.assertNotIn("source", ends[0][1])

    async def test_no_end_without_start(self):
        async def upstream(hooks):
            return "Operation denied"  # prompt:submit denied before execution:start

        self.assertEqual(await self._run(upstream), [])


class ProviderMatchTests(unittest.IsolatedAsyncioTestCase):
    def test_validation(self):
        Policy(model_routing={"start_model": "m", "provider_match": "anthropic"})
        with self.assertRaises(ValueError):
            Policy(model_routing={"start_model": "m", "provider_match": ""})

    async def test_non_matching_provider_keeps_its_model(self):
        policy = Policy(mode="off", model_routing={"start_model": "claude-sonnet-5",
                                                   "provider_match": "anthropic"})
        _, runtime, events = setup_service(policy)
        facade = RoutedProvider(DemoProvider(delay_ms=0), runtime, {}, demo_response, "openai-primary")
        req = routed_request()
        await facade.complete(req)
        self.assertFalse(hasattr(req, "model"))
        routed = [e for e in events if e["event"].endswith("model_routed")]
        self.assertEqual(routed[-1]["data"]["reason_code"], "provider_not_matched")

    async def test_matching_provider_is_routed(self):
        policy = Policy(mode="off", model_routing={"start_model": "claude-sonnet-5",
                                                   "provider_match": "anthropic"})
        _, runtime, events = setup_service(policy)
        provider = DemoProvider(delay_ms=0)
        facade = RoutedProvider(provider, runtime, {}, demo_response, "anthropic-primary")
        req = routed_request()
        await facade.complete(req)
        self.assertEqual(req.model, "claude-sonnet-5")


class JudgeDisabledTests(unittest.IsolatedAsyncioTestCase):
    async def test_none_backend_routes_slow_without_backend_call(self):
        coordinator = DemoCoordinator()
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _ = get_runtime(coordinator, {"backend": "none", "mode": "active",
                                                   "events_dir": tmp}, owner=True)
            try:
                service = runtime.service
                self.assertEqual(service.backend.name, "unavailable")
                service.turn = TurnState("t")
                req = NS(messages=[{"role": "user", "content": "read README.md"}],
                         tools=[{"name": "fast_workspace"}], tool_choice="auto")
                self.assertIsNone(await service.choose(req, {}))
                self.assertLess(service.unhealthy_until, 1)  # circuit breaker never tripped
            finally:
                await runtime.close()


class OwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_orchestrator_owner_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = DemoCoordinator()
            hook_runtime, _ = get_runtime(coordinator, {"backend": "none", "events_dir": tmp})
            self.assertFalse(hook_runtime.orchestrator_owned)
            runtime, _ = get_runtime(coordinator, {"backend": "none", "events_dir": tmp}, owner=True)
            self.assertIs(runtime, hook_runtime)
            self.assertTrue(runtime.orchestrator_owned)
            await runtime.close()

    async def test_shadow_scorer_stands_down_under_orchestrator(self):
        from amplifier_fast_decisions.observer import ShadowScorer

        with tempfile.TemporaryDirectory() as tmp:
            coordinator = DemoCoordinator()
            runtime, _ = get_runtime(coordinator, {"backend": "deterministic", "mode": "active",
                                                   "events_dir": tmp}, owner=True)
            scorer = ShadowScorer(runtime, coordinator, {})
            called = []

            async def snapshot():
                called.append(True)

            scorer._snapshot = snapshot
            await scorer.on_tool_pre("tool:pre", {"tool_name": "read_file"})
            await runtime.close()
        self.assertEqual(called, [])


class ObservatoryOnceTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_install_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = DemoCoordinator()
            runtime, _ = get_runtime(coordinator, {"backend": "none", "events_dir": tmp}, owner=True)
            tasks: list[asyncio.Task] = []
            first = install_auto_observatory(coordinator, runtime, {}, tmp, tasks)
            second = install_auto_observatory(coordinator, runtime, {}, tmp, tasks)
            self.assertIsNotNone(first)
            self.assertIsNone(second)
            self.assertEqual(len(coordinator.hooks.handlers["session:start"]), 1)
            await runtime.close()




class MonotonicEffortTests(unittest.IsolatedAsyncioTestCase):
    """effort_routing.monotonic: effort never steps down within a turn, so the
    thinking budget (and with it the provider's message cache) is not churned
    by orient -> explore -> implement -> explore phase flips."""

    @staticmethod
    def _request(messages):
        return NS(messages=messages, tools=[], tool_choice="auto")

    async def _efforts(self, monotonic):
        policy = Policy(mode="off", effort_routing={"orient": "medium", "explore": "low",
                                                    "implement": "high", "monotonic": monotonic})
        _, runtime, events = setup_service(policy)
        facade = RoutedProvider(DemoProvider(delay_ms=0), runtime, {}, demo_response)
        user = {"role": "user", "content": "fix it"}
        read = {"role": "assistant", "content": "", "tool_calls": [{"id": "r", "name": "read_file"}]}
        write = {"role": "assistant", "content": "", "tool_calls": [{"id": "w", "name": "edit_file"}]}
        result = {"role": "tool", "content": "ok"}
        sequences = [[user], [user, read, result], [user, read, result, write, result]]
        applied = []
        for messages in sequences:
            req = self._request(messages)
            await facade.complete(req)
            applied.append(getattr(req, "reasoning_effort", None))
        reasons = [e["data"]["reason_code"] for e in events if e["event"].endswith("effort_routed")]
        return applied, reasons

    async def test_without_monotonic_effort_follows_phase(self):
        applied, _ = await self._efforts(False)
        self.assertEqual(applied, ["medium", "low", "high"])

    async def test_monotonic_holds_the_highest_effort(self):
        applied, reasons = await self._efforts(True)
        self.assertEqual(applied, ["medium", "medium", "high"])
        self.assertEqual(reasons[1], "monotonic_hold")

    def test_validation(self):
        Policy(effort_routing={"explore": "low", "monotonic": True})
        with self.assertRaises(ValueError):
            Policy(effort_routing={"explore": "low", "monotonic": "yes"})


class _DifficultyHarness(unittest.IsolatedAsyncioTestCase):
    """Shared fixtures for the start-tier router tests. Holds no tests itself,
    so subclasses do not re-run another class's tests under a new name."""

    class FakeJudge:
        name = "fake-judge"
        external = False

        def __init__(self, p_complex=None, fail=False):
            self.p_complex, self.fail, self.calls = p_complex, fail, 0

        async def ask(self, request):
            from amplifier_fast_decisions.contracts import Answer, Decision, DecisionResult, SLOW
            self.calls += 1
            if self.fail:
                raise RuntimeError("judge down")
            probs = {"simple": 1 - self.p_complex, "complex": self.p_complex}
            return DecisionResult(action=Decision(choice=SLOW, probabilities={SLOW: 1.0}),
                                  answers={"task_difficulty": Answer(probabilities=probs)})

        async def close(self):
            pass

    def _setup(self, routing, judge=None):
        events = []
        coordinator = DemoCoordinator()
        emitter = Emitter(coordinator.session_id, callback=events.append)
        policy = Policy(mode="off", model_routing=routing, read_shortcut=False)
        service = DecisionService(policy, judge or ScriptedBackend(delay_ms=0), emitter, coordinator, [])
        service.turn = TurnState("t")
        return service, Runtime(service), events

    async def _run(self, routing, prompt, judge=None, requests=3):
        service, runtime, events = self._setup(routing, judge)
        facade = RoutedProvider(DemoProvider(delay_ms=0), runtime, {}, demo_response, "anthropic-primary")
        models = []
        for _ in range(requests):
            req = NS(messages=[{"role": "user", "content": prompt}], tools=[], tool_choice="auto")
            await facade.complete(req)
            models.append(getattr(req, "model", None))
        judged = [e["data"] for e in events if e["event"].endswith("difficulty_judged")]
        return models, judged, service.turn

    ROUTING = {"start_model": "claude-sonnet-5", "provider_match": "anthropic",
               "max_requests_before_escalation": 1}


class DifficultyRouterTests(_DifficultyHarness):
    """model_routing.start_policy: decide the start tier once per turn."""

    async def test_default_policy_is_cheap_and_silent(self):
        models, judged, _ = await self._run(dict(self.ROUTING, max_requests_before_escalation=6), "fix typo")
        self.assertEqual(models, ["claude-sonnet-5"] * 3)
        self.assertEqual(judged, [])

    async def test_judge_complex_starts_strong_and_never_escalates_or_switches(self):
        judge = self.FakeJudge(p_complex=0.9)
        models, judged, turn = await self._run(dict(self.ROUTING, start_policy="judge"), "big bug", judge)
        self.assertEqual(models, [None, None, None])      # host model throughout
        self.assertEqual(judge.calls, 1)                  # decided once per turn
        self.assertEqual(judged[0]["reason_code"], "judge_strong")
        self.assertFalse(turn.escalated)

    async def test_judge_simple_starts_cheap(self):
        judge = self.FakeJudge(p_complex=0.1)
        models, judged, _ = await self._run(dict(self.ROUTING, start_policy="judge",
                                                 max_requests_before_escalation=6), "typo", judge)
        self.assertEqual(models, ["claude-sonnet-5"] * 3)
        self.assertEqual(judged[0]["reason_code"], "judge_cheap")

    async def test_judge_failure_falls_back_to_rules(self):
        judge = self.FakeJudge(fail=True)
        long_prompt = "x" * 2500
        models, judged, _ = await self._run(dict(self.ROUTING, start_policy="judge"), long_prompt, judge)
        self.assertEqual(judged[0]["reason_code"], "rules_strong")
        self.assertEqual(models[0], None)

    async def test_cheaper_host_is_never_routed_up(self):
        # A Haiku-hosted helper session: Sonnet would cost more and run slower.
        judge = self.FakeJudge(p_complex=0.05)
        service, runtime, events = self._setup(dict(self.ROUTING, start_policy="judge",
                                                    max_requests_before_escalation=None), judge)
        provider = DemoProvider(delay_ms=0)
        provider.default_model = "claude-haiku-4-5-20251001"
        facade = RoutedProvider(provider, runtime, {}, demo_response, "anthropic-primary")
        req = NS(messages=[{"role": "user", "content": "typo"}], tools=[], tool_choice="auto")
        await facade.complete(req)
        self.assertIsNone(getattr(req, "model", None))
        routed = [e["data"] for e in events if e["event"].endswith("model_routed")]
        self.assertEqual(routed[0]["reason_code"], "host_already_cheaper")

    async def test_more_expensive_host_still_routes(self):
        judge = self.FakeJudge(p_complex=0.05)
        service, runtime, events = self._setup(dict(self.ROUTING, start_policy="judge",
                                                    max_requests_before_escalation=None), judge)
        provider = DemoProvider(delay_ms=0)
        provider.default_model = "claude-opus-5-5"
        facade = RoutedProvider(provider, runtime, {}, demo_response, "anthropic-primary")
        req = NS(messages=[{"role": "user", "content": "typo"}], tools=[], tool_choice="auto")
        await facade.complete(req)
        self.assertEqual(req.model, "claude-sonnet-5")

    async def test_ui_model_pick_is_respected(self):
        # amplifier-runtime marks an in-session model pick in session_state.
        judge = self.FakeJudge(p_complex=0.1)
        service, runtime, events = self._setup(dict(self.ROUTING, start_policy="judge",
                                                    max_requests_before_escalation=6), judge)
        service.coordinator.session_state = {"ui.model_override": {"provider": "anthropic", "model": "claude-opus-5-5"}}
        facade = RoutedProvider(DemoProvider(delay_ms=0), runtime, {}, demo_response, "anthropic-primary")
        req = NS(messages=[{"role": "user", "content": "typo"}], tools=[], tool_choice="auto")
        await facade.complete(req)
        judged = [e["data"] for e in events if e["event"].endswith("difficulty_judged")]
        self.assertIsNone(getattr(req, "model", None))       # the user's model, untouched
        self.assertEqual(judged[0]["reason_code"], "user_model_strong")
        self.assertEqual(judged[0]["model"], "claude-opus-5-5")
        self.assertEqual(judge.calls, 0)                      # no judge call spent

    async def test_mid_session_default_model_change_is_respected(self):
        judge = self.FakeJudge(p_complex=0.1)
        service, runtime, events = self._setup(dict(self.ROUTING, start_policy="judge",
                                                    max_requests_before_escalation=6), judge)
        provider = DemoProvider(delay_ms=0)
        provider.default_model = "claude-opus-5-5"
        facade = RoutedProvider(provider, runtime, {}, demo_response, "anthropic-primary")
        first = NS(messages=[{"role": "user", "content": "typo"}], tools=[], tool_choice="auto")
        await facade.complete(first)
        self.assertEqual(first.model, "claude-sonnet-5")      # configured default: routable
        provider.default_model = "claude-fable-5-1"           # user switched models
        service.turn = TurnState("t2")
        second = NS(messages=[{"role": "user", "content": "typo"}], tools=[], tool_choice="auto")
        await facade.complete(second)
        self.assertIsNone(getattr(second, "model", None))
        judged = [e["data"] for e in events if e["event"].endswith("difficulty_judged")]
        self.assertEqual([j["reason_code"] for j in judged], ["judge_cheap", "user_model_strong"])

    def test_validation(self):
        with self.assertRaises(ValueError):
            Policy(model_routing={"start_model": "m", "start_policy": "vibes"})
        with self.assertRaises(ValueError):
            Policy(model_routing={"start_model": "m", "complex_min_probability": 1.5})
        with self.assertRaises(ValueError):
            Policy(read_shortcut="no")


class EffortByTierTests(_DifficultyHarness):
    """effort_routing.by_tier: one effort for the whole turn, chosen by the router's tier."""

    async def _efforts(self, p_complex):
        events = []
        coordinator = DemoCoordinator()
        emitter = Emitter(coordinator.session_id, callback=events.append)
        policy = Policy(mode="off", read_shortcut=False,
                        effort_routing={"orient": "medium", "explore": "low", "implement": "high",
                                        "by_tier": {"cheap": "medium", "strong": None}},
                        model_routing={"start_model": "claude-sonnet-5", "start_policy": "judge",
                                       "max_requests_before_escalation": 6})
        service = DecisionService(policy, self.FakeJudge(p_complex=p_complex), emitter, coordinator, [])
        service.turn = TurnState("t")
        facade = RoutedProvider(DemoProvider(delay_ms=0), Runtime(service), {}, demo_response, "anthropic")
        user = {"role": "user", "content": "task"}
        read = {"role": "assistant", "content": "", "tool_calls": [{"id": "r", "name": "read_file"}]}
        write = {"role": "assistant", "content": "", "tool_calls": [{"id": "w", "name": "edit_file"}]}
        result = {"role": "tool", "content": "ok"}
        applied = []
        for messages in ([user], [user, read, result], [user, read, result, write, result]):
            req = NS(messages=messages, tools=[], tool_choice="auto")
            await facade.complete(req)
            applied.append(getattr(req, "reasoning_effort", None))
        return applied

    async def test_cheap_turn_holds_one_effort_across_phases(self):
        self.assertEqual(await self._efforts(0.1), ["medium", "medium", "medium"])

    async def test_strong_turn_leaves_provider_default(self):
        self.assertEqual(await self._efforts(0.9), [None, None, None])

    def test_validation(self):
        with self.assertRaises(ValueError):
            Policy(effort_routing={"by_tier": {"huge": "low"}})
        with self.assertRaises(ValueError):
            Policy(effort_routing={"by_tier": {"cheap": "turbo"}})


class ScopeGateTests(_DifficultyHarness):
    """model_routing.cheap_max_workspace_files: repository-scale workspaces start strong."""

    async def _in_workspace(self, n_files, p_complex):
        import os
        from amplifier_fast_decisions import orchestrator as orch
        orch._workspace_file_counts.clear()
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(n_files):
                open(os.path.join(tmp, f"f{i}.py"), "w").close()
            os.makedirs(os.path.join(tmp, "node_modules"))
            for i in range(50):  # skipped: never counts toward scope
                open(os.path.join(tmp, "node_modules", f"d{i}.js"), "w").close()
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                judge = self.FakeJudge(p_complex=p_complex)
                models, judged, _ = await self._run(
                    dict(self.ROUTING, start_policy="judge", cheap_max_workspace_files=10,
                         max_requests_before_escalation=6), "typo", judge, requests=1)
                return models, judged, judge.calls
            finally:
                os.chdir(cwd)

    async def test_large_workspace_starts_strong_without_asking_the_judge(self):
        models, judged, calls = await self._in_workspace(25, p_complex=0.01)
        self.assertEqual(models, [None])
        self.assertEqual(judged[0]["reason_code"], "scope_strong")
        self.assertEqual(calls, 0)

    async def test_small_workspace_defers_to_the_judge(self):
        models, judged, calls = await self._in_workspace(5, p_complex=0.01)
        self.assertEqual(models, ["claude-sonnet-5"])
        self.assertEqual(judged[0]["reason_code"], "judge_cheap")
        self.assertEqual(calls, 1)

    def test_count_is_bounded(self):
        import os
        from amplifier_fast_decisions.orchestrator import workspace_file_count, _workspace_file_counts
        _workspace_file_counts.clear()
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(40):
                open(os.path.join(tmp, f"f{i}"), "w").close()
            self.assertEqual(workspace_file_count(tmp, 100), 40)
            self.assertGreater(workspace_file_count(tmp, 10), 10)


class ByTierPhaseTests(unittest.TestCase):
    def test_phase_value_is_valid(self):
        Policy(effort_routing={"explore": "low", "by_tier": {"cheap": "medium", "strong": "phase"}, "monotonic": True})


class _KwargsProvider(DemoProvider):
    """Records what the wrapped provider actually receives: the installed
    Anthropic provider reads the per-request model from kwargs, not from
    request.model, so kwargs is the receipt that matters."""

    def __init__(self, default_model=None, usage=None, served_model=None):
        super().__init__(delay_ms=0)
        self.default_model = default_model
        self.kwargs_seen = []
        self._usage = usage
        self._served_model = served_model

    async def complete(self, request, **kwargs):
        self.kwargs_seen.append(dict(kwargs))
        response = await super().complete(request, **kwargs)
        if self._usage is not None:
            response.usage = self._usage
        if self._served_model is not None:
            response.model = self._served_model
        return response


class RoutingReceiptTests(_DifficultyHarness):
    """What the provider is really called with, and what the slow_end receipt
    records for the savings estimate (added by the mutation audit)."""

    async def _complete(self, routing, prompt="typo", judge=None, provider=None, requests=1, **req_fields):
        service, runtime, events = self._setup(routing, judge)
        provider = provider or _KwargsProvider()
        facade = RoutedProvider(provider, runtime, {}, demo_response, "anthropic-primary")
        reqs = []
        for _ in range(requests):
            req = NS(messages=[{"role": "user", "content": prompt}], tools=[], tool_choice="auto", **req_fields)
            await facade.complete(req)
            reqs.append(req)
        return provider, events, reqs, service.turn

    async def test_cheap_turn_passes_start_model_to_provider_kwargs(self):
        provider, _, _, _ = await self._complete(dict(self.ROUTING, max_requests_before_escalation=6))
        self.assertEqual(provider.kwargs_seen[0].get("model"), "claude-sonnet-5")

    async def test_strong_turn_passes_no_model_override(self):
        provider, _, reqs, _ = await self._complete(dict(self.ROUTING, start_policy="judge"),
                                                    judge=self.FakeJudge(p_complex=0.9))
        self.assertNotIn("model", provider.kwargs_seen[0])
        self.assertIsNone(getattr(reqs[0], "model", None))

    async def test_complex_min_probability_is_the_gate(self):
        # p(complex)=0.9 is below a 0.95 gate: the turn stays cheap.
        _, events, _, turn = await self._complete(
            dict(self.ROUTING, start_policy="judge", complex_min_probability=0.95,
                 max_requests_before_escalation=6), judge=self.FakeJudge(p_complex=0.9))
        judged = [e["data"] for e in events if e["event"].endswith("difficulty_judged")]
        self.assertEqual(judged[0]["reason_code"], "judge_cheap")
        self.assertEqual(turn.start_tier, "cheap")

    async def test_rules_threshold_is_inclusive(self):
        routing = dict(self.ROUTING, start_policy="rules", complex_min_prompt_chars=50,
                       max_requests_before_escalation=6)
        _, at, _, _ = await self._complete(routing, prompt="x" * 50)
        _, below, _, _ = await self._complete(routing, prompt="x" * 49)
        reason = lambda evs: [e["data"]["reason_code"] for e in evs if e["event"].endswith("difficulty_judged")]
        self.assertEqual(reason(at), ["rules_strong"])
        self.assertEqual(reason(below), ["rules_cheap"])

    async def test_no_max_requests_means_no_escalation(self):
        routing = {"start_model": "claude-sonnet-5", "provider_match": "anthropic"}
        provider, events, _, turn = await self._complete(routing, requests=9)
        self.assertFalse(turn.escalated)
        self.assertEqual([k.get("model") for k in provider.kwargs_seen], ["claude-sonnet-5"] * 9)
        reasons = {e["data"]["reason_code"] for e in events if e["event"].endswith("model_routed")}
        self.assertEqual(reasons, {"start_model"})

    async def test_slow_end_records_host_model_and_provider_usage(self):
        usage = NS(input_tokens=1200, output_tokens=300, total_tokens=1500,
                   cache_read_tokens=1000, cache_write_tokens=50, cost_usd=0.0123)
        provider = _KwargsProvider(default_model="claude-opus-5-5", usage=usage, served_model="claude-sonnet-5-20260101")
        _, events, _, _ = await self._complete(dict(self.ROUTING, max_requests_before_escalation=6), provider=provider)
        end = [e["data"] for e in events if e["event"].endswith("slow_end")][-1]
        self.assertEqual(end["host_model"], "claude-opus-5-5")
        self.assertEqual(end["served_model"], "claude-sonnet-5-20260101")
        self.assertEqual(end["cache_read_tokens"], 1000)
        self.assertEqual(end["cache_write_tokens"], 50)
        self.assertAlmostEqual(end["cost_usd"], 0.0123)
        self.assertEqual(end["input_tokens"], 1200)

    async def test_host_pinned_effort_is_not_overridden_by_tier(self):
        events = []
        coordinator = DemoCoordinator()
        emitter = Emitter(coordinator.session_id, callback=events.append)
        policy = Policy(mode="off", read_shortcut=False,
                        effort_routing={"orient": "medium", "by_tier": {"cheap": "low", "strong": None}},
                        model_routing={"start_model": "claude-sonnet-5", "start_policy": "judge",
                                       "max_requests_before_escalation": 6})
        service = DecisionService(policy, self.FakeJudge(p_complex=0.1), emitter, coordinator, [])
        service.turn = TurnState("t")
        facade = RoutedProvider(DemoProvider(delay_ms=0), Runtime(service), {}, demo_response, "anthropic")
        req = NS(messages=[{"role": "user", "content": "task"}], tools=[], tool_choice="auto",
                 reasoning_effort="high")
        await facade.complete(req)
        self.assertEqual(service.turn.start_tier, "cheap")
        self.assertEqual(req.reasoning_effort, "high")

    def test_zero_workspace_file_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            Policy(model_routing={"start_model": "m", "cheap_max_workspace_files": 0})


class _RequestCapturingProvider(DemoProvider):
    """Records the exact request OBJECT each complete() call actually
    received -- identity matters here (has it been copied/shaped or not),
    not just the kwargs the installed Anthropic provider reads from."""

    def __init__(self):
        super().__init__(delay_ms=0)
        self.requests_seen: list = []

    async def complete(self, request, **kwargs):
        self.requests_seen.append(request)
        return await super().complete(request, **kwargs)


class EasyTurnShapingTests(_DifficultyHarness):
    def test_receipt_name_is_an_allowed_event(self):
        # The emitter drops unknown event names; a mocked emit would not notice.
        from amplifier_fast_decisions.contracts import EVENT_NAMES
        self.assertIn("fast_decisions:easy_turn_shaped", EVENT_NAMES)

    """HC12 ("easy-turn shaping", opt-in): model_routing.easy_turn_guidance /
    easy_turn_hide_tools apply only while turn.start_tier == "cheap", via a
    shaped COPY built per call -- the original request/messages/tools are
    never mutated."""

    SYSTEM = {"role": "system", "content": "Base system prompt."}

    def _cheap_request(self, prompt="typo", tools=None):
        return NS(messages=[self.SYSTEM, {"role": "user", "content": prompt}],
                   tools=tools if tools is not None else [], tool_choice="auto")

    async def _run_calls(self, routing, n=3, tools=None, judge=None):
        service, runtime, events = self._setup(routing, judge)
        provider = _RequestCapturingProvider()
        facade = RoutedProvider(provider, runtime, {}, demo_response, "anthropic-primary")
        reqs = []
        for _ in range(n):
            req = self._cheap_request(tools=tools)
            await facade.complete(req)
            reqs.append(req)
        return provider, events, reqs, service.turn

    async def test_default_config_is_no_change(self):
        # No easy_turn_* keys at all: byte-identical to pre-HC12 behavior.
        routing = dict(self.ROUTING, max_requests_before_escalation=6)
        provider, events, reqs, turn = await self._run_calls(routing)
        self.assertEqual(turn.start_tier, "cheap")
        for seen, original in zip(provider.requests_seen, reqs):
            self.assertIs(seen, original)  # no copy was made at all
        self.assertEqual([e for e in events if e["event"].endswith("easy_turn_shaped")], [])

    async def test_guidance_appended_once_and_identical_across_calls(self):
        guidance = "Work directly: batch independent tool calls, verify once."
        routing = dict(self.ROUTING, max_requests_before_escalation=6, easy_turn_guidance=guidance)
        provider, events, reqs, turn = await self._run_calls(routing, n=3)
        self.assertEqual(turn.start_tier, "cheap")
        expected = self.SYSTEM["content"] + "\n\n" + guidance
        system_contents = [seen.messages[0]["content"] for seen in provider.requests_seen]
        self.assertEqual(system_contents, [expected, expected, expected])  # identical every call
        for original in reqs:  # original objects passed in by the caller stay untouched
            self.assertEqual(original.messages[0]["content"], self.SYSTEM["content"])
        shaped = [e["data"] for e in events if e["event"].endswith("easy_turn_shaped")]
        self.assertEqual(len(shaped), 1)  # once per turn, not once per call
        self.assertEqual(shaped[0]["guidance_chars"], len(guidance))
        self.assertEqual(shaped[0]["hidden_tools"], [])

    async def test_strong_turn_untouched(self):
        judge = self.FakeJudge(p_complex=0.9)
        routing = dict(self.ROUTING, start_policy="judge",
                       easy_turn_guidance="ignored on a strong turn",
                       easy_turn_hide_tools=["todo"])
        provider, events, reqs, turn = await self._run_calls(routing, n=2, judge=judge)
        self.assertEqual(turn.start_tier, "strong")
        for seen, original in zip(provider.requests_seen, reqs):
            self.assertIs(seen, original)  # byte-for-byte: no shaping applied
        self.assertEqual([e for e in events if e["event"].endswith("easy_turn_shaped")], [])

    async def test_escalated_calls_are_not_shaped(self):
        # After the turn escalates to the host, calls go out unshaped.
        routing = dict(self.ROUTING, max_requests_before_escalation=1,
                       easy_turn_guidance="g", easy_turn_hide_tools=["todo"])
        tools = [{"name": "todo"}, {"name": "bash"}]
        provider, _events, reqs, turn = await self._run_calls(routing, n=3, tools=tools)
        self.assertTrue(turn.escalated)
        self.assertEqual([t["name"] for t in provider.requests_seen[0].tools], ["bash"])
        for seen, original in zip(provider.requests_seen[1:], reqs[1:]):
            self.assertIs(seen, original)

    async def test_hide_tools_filters_by_name(self):
        routing = dict(self.ROUTING, max_requests_before_escalation=6,
                       easy_turn_hide_tools=["todo"])
        tools = [{"name": "todo"}, {"name": "bash"}]
        provider, events, reqs, _turn = await self._run_calls(routing, n=1, tools=tools)
        seen_names = [t["name"] for t in provider.requests_seen[0].tools]
        self.assertEqual(seen_names, ["bash"])
        self.assertEqual(reqs[0].tools, tools)  # original list of tools untouched
        shaped = [e["data"] for e in events if e["event"].endswith("easy_turn_shaped")]
        self.assertEqual(shaped[0]["hidden_tools"], ["todo"])
        self.assertEqual(shaped[0]["guidance_chars"], 0)

    async def test_receipt_emitted_once_per_turn(self):
        routing = dict(self.ROUTING, max_requests_before_escalation=6,
                       easy_turn_guidance="g", easy_turn_hide_tools=["todo"])
        _provider, events, _reqs, turn = await self._run_calls(
            routing, n=4, tools=[{"name": "todo"}, {"name": "bash"}])
        shaped = [e for e in events if e["event"].endswith("easy_turn_shaped")]
        self.assertEqual(len(shaped), 1)
        self.assertTrue(turn.easy_turn_shaped)

    async def test_no_system_message_is_a_no_op_for_guidance(self):
        # Fail closed: never invent a system message just to attach guidance.
        routing = dict(self.ROUTING, max_requests_before_escalation=6, easy_turn_guidance="g")
        service, runtime, events = self._setup(routing)
        provider = _RequestCapturingProvider()
        facade = RoutedProvider(provider, runtime, {}, demo_response, "anthropic-primary")
        req = NS(messages=[{"role": "user", "content": "typo"}], tools=[], tool_choice="auto")
        await facade.complete(req)
        self.assertIs(provider.requests_seen[0], req)
        self.assertEqual([e for e in events if e["event"].endswith("easy_turn_shaped")], [])

    def test_validation(self):
        with self.assertRaises(ValueError):
            Policy(model_routing={"start_model": "m", "easy_turn_guidance": 5})
        with self.assertRaises(ValueError):
            Policy(model_routing={"start_model": "m", "easy_turn_hide_tools": "todo"})
        with self.assertRaises(ValueError):
            Policy(model_routing={"start_model": "m", "easy_turn_hide_tools": [1, 2]})
        Policy(model_routing={"start_model": "m", "easy_turn_guidance": "g",
                              "easy_turn_hide_tools": ["todo"]})


if __name__ == "__main__":
    unittest.main()
