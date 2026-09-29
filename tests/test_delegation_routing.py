"""Per-delegation model routing (Policy.delegation_routing, opt-in).

One decision per `delegate` call, taken before the child session exists. No
amplifier_core, no network, no judge: every backend here is a fake.
See docs/DELEGATION-ROUTING.md and src/amplifier_fast_decisions/delegation.py.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from types import SimpleNamespace as NS

from amplifier_fast_decisions import delegation
from amplifier_fast_decisions.backends import BackendUnavailable
from amplifier_fast_decisions.contracts import (
    Answer,
    Decision,
    DecisionResult,
    Policy,
    SLOW,
    TurnState,
)
from amplifier_fast_decisions.delegation import (
    DelegateFacade,
    DelegationDecider,
    facade_for,
)
from amplifier_fast_decisions.delegation_policy import DelegationPolicy
from amplifier_fast_decisions.demo import DemoCoordinator
from amplifier_fast_decisions.orchestrator import HybridOrchestrator, ObservedTool
from amplifier_fast_decisions.runtime import Runtime, get_runtime
from amplifier_fast_decisions.service import DecisionService
from amplifier_fast_decisions.telemetry import Emitter

EVENT = "fast_decisions:delegation_routed"

# A mid-tier anchor, as the routing matrix would resolve a `research` role.
SONNET = [{"provider": "anthropic", "model": "claude-sonnet-5", "config": {}}]
# The four answers a judge gives for "a cheaper tier would do, standard depth".
DOWN_STANDARD = {
    "min_tier": {"small": 0.93, "mid": 0.05, "frontier": 0.02},
    "answer_depth": {"brief": 0.05, "standard": 0.90, "thorough": 0.05},
    "needs_computer_use": {"yes": 0.02, "no": 0.98},
    "needs_vision": {"yes": 0.02, "no": 0.98},
}


class FakeJudgeBackend:
    """Scripts one Answer per named Question per ask() call.

    Mirrors FakeRiskBackend in test_tool_risk_shadow.py: a ``None`` script
    raises BackendUnavailable, a missing question name is left unanswered, and
    ``delay_ms`` lets a test drive the deadline without real latency.
    """

    name = "fake-judge"

    def __init__(self, scripts, *, external=False, delay_ms=0):
        self.scripts = list(scripts)
        self.external = external
        self.delay_ms = delay_ms
        self.calls = 0
        self.requests = []

    async def ask(self, request):
        self.requests.append(request)
        step = self.scripts[min(self.calls, len(self.scripts) - 1)]
        self.calls += 1
        if self.delay_ms:
            await asyncio.sleep(self.delay_ms / 1000)
        if step is None:
            raise BackendUnavailable("synthetic judge failure")
        answers = {}
        for question in request.questions:
            probabilities = step.get(question.name)
            if probabilities is not None:
                answers[question.name] = Answer(probabilities=dict(probabilities), confidence=0.9)
        action = Decision(SLOW, {SLOW: 1.0}, 1.0, self.name, 0, True)
        return DecisionResult(action=action, answers=answers, model=self.name,
                              input_tokens=0, output_tokens=0, synthetic=True)

    async def close(self):
        pass


class FakeResolver:
    """The routing-matrix `model_role_resolver` capability, duck-typed."""

    known_roles = ("research", "coding", "general")

    def __init__(self, preferences=None):
        self.preferences = SONNET if preferences is None else preferences
        self.resolved = []

    async def resolve(self, role):
        self.resolved.append(role)
        return [dict(p) for p in self.preferences]


class FailingResolver:
    known_roles = ("research",)

    async def resolve(self, role):
        raise RuntimeError("resolver exploded")


class FakeDelegateTool:
    """The real delegate tool's stand-in: records arguments and its task."""

    name = "delegate"
    description = "Delegate to an agent"
    input_schema = {"type": "object"}

    def __init__(self):
        self.calls = []
        self.tasks = []

    async def execute(self, input, **kwargs):
        self.calls.append(input)
        self.tasks.append(asyncio.current_task())
        return {"success": True, "output": "delegated"}


class FakeOtherTool:
    name = "bash"

    async def execute(self, input, **kwargs):
        return {"success": True, "output": "ok"}


def setup_service(*, policy, backend, coordinator=None):
    events = []
    emitter = Emitter("test-session", callback=events.append)
    service = DecisionService(policy, backend, emitter, coordinator=coordinator,
                              configured_candidates=[])
    service.turn = TurnState("test-turn")
    return service, Runtime(service), events


def make_coordinator(*, resolver=None, agents=None):
    """A coordinator with the role-resolver capability. ``resolver=False``
    registers none, i.e. a session with no routing matrix composed."""
    coordinator = DemoCoordinator()
    if resolver is not False:
        coordinator.register_capability(
            delegation.ROLE_RESOLVER_CAPABILITY, resolver or FakeResolver()
        )
    coordinator.config = {"agents": agents or {}}
    return coordinator


def build_decider(*, backend, agents=None, resolver=None, mode="enforce",
                  deadline_ms=750, policy_kwargs=None):
    """A decider wired to fakes, plus the event list it will emit into."""
    policy = Policy(mode="off", **(policy_kwargs or {}))
    coordinator = make_coordinator(resolver=resolver, agents=agents)
    _, runtime, events = setup_service(policy=policy, backend=backend,
                                       coordinator=coordinator)
    decider = DelegationDecider(runtime, coordinator, mode=mode,
                                policy=DelegationPolicy.load("v3"),
                                deadline_ms=deadline_ms)
    return decider, events


def receipts(events):
    return [e["data"] for e in events if e["event"] == EVENT]


class DelegationRoutingValidationTests(unittest.TestCase):
    def test_default_is_off(self):
        self.assertIsNone(Policy().delegation_routing)

    def test_empty_dict_is_legal_and_off(self):
        self.assertEqual(Policy(delegation_routing={}).delegation_routing, {})

    def test_unknown_key_raises(self):
        with self.assertRaises(ValueError):
            Policy(delegation_routing={"mode": "shadow", "polcy": "v3"})

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            Policy(delegation_routing={"mode": "on"})

    def test_bad_deadline_raises(self):
        with self.assertRaises(ValueError):
            Policy(delegation_routing={"mode": "shadow", "deadline_ms": 0})

    def test_non_dict_raises(self):
        with self.assertRaises(ValueError):
            Policy(delegation_routing="shadow")

    def test_event_name_is_declared(self):
        from amplifier_fast_decisions.contracts import EVENT_NAMES

        self.assertIn(EVENT, EVENT_NAMES)


class FacadeConstructionTests(unittest.TestCase):
    """`facade_for` is the on/off switch: off means NO facade, not a no-op one."""

    def _runtime(self, delegation_routing):
        policy = Policy(mode="off", delegation_routing=delegation_routing)
        _, runtime, _ = setup_service(policy=policy, backend=FakeJudgeBackend([]))
        return runtime

    def test_none_returns_no_facade(self):
        runtime = self._runtime(None)
        self.assertIsNone(facade_for(FakeDelegateTool(), runtime, make_coordinator()))

    def test_mode_off_returns_no_facade(self):
        runtime = self._runtime({"mode": "off"})
        self.assertIsNone(facade_for(FakeDelegateTool(), runtime, make_coordinator()))

    def test_shadow_returns_a_facade(self):
        runtime = self._runtime({"mode": "shadow"})
        self.assertIsInstance(facade_for(FakeDelegateTool(), runtime, make_coordinator()),
                              DelegateFacade)

    def test_unloadable_policy_disables_the_feature(self):
        runtime = self._runtime({"mode": "enforce", "policy": "no-such-policy"})
        self.assertIsNone(facade_for(FakeDelegateTool(), runtime, make_coordinator()))

    def test_facade_never_advertises_a_native_tool_spec(self):
        """loop-streaming reads native_tool_spec off type(tool); advertising one
        here would let the provider bypass the arguments just decided."""
        self.assertIsNone(getattr(DelegateFacade, "native_tool_spec", None))

    def test_facade_passes_tool_attributes_through(self):
        tool = FakeDelegateTool()
        facade = DelegateFacade(tool, None)
        self.assertEqual(facade.name, "delegate")
        self.assertEqual(facade.input_schema, {"type": "object"})


class OrchestratorSeamTests(unittest.IsolatedAsyncioTestCase):
    """Where the facade actually hooks in: HybridOrchestrator.execute's tool map."""

    async def _run(self, delegation_routing, *, backend=None, agents=None,
                   resolver=None, tools=None):
        coordinator = make_coordinator(resolver=resolver or FakeResolver(), agents=agents)
        seen = {}

        class Upstream:
            async def execute(self, prompt, context, providers, tools, hooks, **kwargs):
                seen.update(tools)
                seen["_task"] = asyncio.current_task()
                if "delegate" in tools:
                    await tools["delegate"].execute(
                        {"agent": "researcher", "instruction": "count the files",
                         "model_role": "research"}
                    )
                return "done"

        with tempfile.TemporaryDirectory() as tmp:
            config = {"backend": "none", "events_dir": tmp, "mode": "off",
                      "read_shortcut": False}
            if delegation_routing is not None:
                config["delegation_routing"] = delegation_routing
            runtime, _ = get_runtime(coordinator, config, owner=True)
            if backend is not None:
                runtime.service.backend = backend
            events = []
            runtime.service.emitter.callback = events.append
            orchestrator = HybridOrchestrator(config, coordinator, runtime,
                                              upstream=Upstream())
            try:
                await orchestrator.execute(
                    "hi", None, {}, tools if tools is not None else
                    {"delegate": FakeDelegateTool(), "bash": FakeOtherTool()},
                    coordinator.hooks,
                )
            finally:
                await runtime.close()
        return seen, events

    async def test_off_leaves_the_tool_mapping_untouched(self):
        """Off is byte-for-byte unchanged: an ObservedTool, no judge call, no event."""
        delegate = FakeDelegateTool()
        backend = FakeJudgeBackend([DOWN_STANDARD])
        seen, events = await self._run(None, backend=backend,
                                       tools={"delegate": delegate, "bash": FakeOtherTool()})
        self.assertIsInstance(seen["delegate"], ObservedTool)
        self.assertNotIsInstance(seen["delegate"], DelegateFacade)
        self.assertEqual(backend.calls, 0)
        self.assertEqual(receipts(events), [])
        # The delegate ran with exactly the arguments the model produced.
        self.assertEqual(delegate.calls, [{"agent": "researcher",
                                           "instruction": "count the files",
                                           "model_role": "research"}])

    async def test_mode_off_in_config_also_leaves_it_untouched(self):
        seen, events = await self._run({"mode": "off"},
                                       backend=FakeJudgeBackend([DOWN_STANDARD]))
        self.assertNotIsInstance(seen["delegate"], DelegateFacade)
        self.assertEqual(receipts(events), [])

    async def test_only_delegate_is_wrapped(self):
        seen, _ = await self._run({"mode": "shadow"},
                                  backend=FakeJudgeBackend([DOWN_STANDARD]))
        self.assertIsInstance(seen["delegate"], DelegateFacade)
        self.assertIsInstance(seen["bash"], ObservedTool)
        self.assertNotIsInstance(seen["bash"], DelegateFacade)

    async def test_the_facade_wraps_the_observed_tool_so_receipts_survive(self):
        delegate = FakeDelegateTool()
        _, events = await self._run({"mode": "shadow"},
                                    backend=FakeJudgeBackend([DOWN_STANDARD]),
                                    tools={"delegate": delegate})
        kinds = [e["event"] for e in events]
        self.assertIn("fast_decisions:tool_start", kinds)
        self.assertIn("fast_decisions:tool_end", kinds)
        self.assertIn(EVENT, kinds)
        tool_events = [e["data"] for e in events
                       if e["event"] == "fast_decisions:tool_end"]
        self.assertTrue(any(d["tool"] == "delegate" for d in tool_events))

    async def test_real_tool_runs_in_the_same_asyncio_task(self):
        """tool-delegate reads its dispatch context from
        coordinator._tool_dispatch_contexts[asyncio.current_task()]."""
        delegate = FakeDelegateTool()
        seen, _ = await self._run({"mode": "enforce"},
                                  backend=FakeJudgeBackend([DOWN_STANDARD]),
                                  tools={"delegate": delegate})
        self.assertEqual(len(delegate.tasks), 1)
        self.assertIs(delegate.tasks[0], seen["_task"])

    async def test_enforce_pins_the_policy_lever_on_the_real_call(self):
        delegate = FakeDelegateTool()
        _, events = await self._run({"mode": "enforce"},
                                    backend=FakeJudgeBackend([DOWN_STANDARD]),
                                    tools={"delegate": delegate})
        pinned = delegate.calls[0]["provider_preferences"]
        # v3, mid anchor + "small suffices" + standard depth -> the model lever.
        self.assertEqual(pinned[0]["model"], "claude-haiku-4-5-20251001")
        self.assertEqual(pinned[0]["provider"], "anthropic")
        # Everything else about the call is untouched.
        self.assertEqual(delegate.calls[0]["instruction"], "count the files")
        record = receipts(events)[0]
        self.assertEqual(record["action"], "adjust")
        self.assertEqual(record["lever"], "model")
        self.assertEqual(record["nudge"], "model_down")
        self.assertEqual(record["actual_preference"]["model"], "claude-haiku-4-5-20251001")

    async def test_shadow_records_the_proposal_but_changes_nothing(self):
        delegate = FakeDelegateTool()
        _, events = await self._run({"mode": "shadow"},
                                    backend=FakeJudgeBackend([DOWN_STANDARD]),
                                    tools={"delegate": delegate})
        self.assertNotIn("provider_preferences", delegate.calls[0])
        record = receipts(events)[0]
        self.assertEqual(record["action"], "shadow")
        self.assertEqual(record["proposed_preference"]["model"], "claude-haiku-4-5-20251001")
        # Proposed WITHOUT actual: nothing was pinned.
        self.assertIsNone(record["actual_preference"])


class DecisionRecordTests(unittest.IsolatedAsyncioTestCase):
    """The decision itself, driven directly -- no orchestrator in the way."""

    _setup = staticmethod(build_decider)

    async def test_record_carries_answers_probabilities_anchor_and_backend(self):
        decider, events = self._setup(backend=FakeJudgeBackend([DOWN_STANDARD]))
        await decider.adjust({"agent": "researcher", "instruction": "x" * 400,
                              "model_role": "research"})
        record = receipts(events)[0]
        self.assertEqual(record["mode"], "enforce")
        self.assertEqual(record["delegation_policy"], "v3")
        self.assertEqual(record["backend"], "fake-judge")
        self.assertEqual(record["agent"], "researcher")
        self.assertEqual(record["model_role"], "research")
        self.assertEqual(record["role_source"], "call")
        self.assertEqual(record["anchor_source"], "resolver")
        self.assertEqual(record["anchor"]["model"], "claude-sonnet-5")
        self.assertEqual(record["answers"]["min_tier"], "small")
        self.assertEqual(record["answers"]["answer_depth"], "standard")
        self.assertAlmostEqual(record["answers"]["min_tier_p"], 0.93)
        self.assertEqual(record["requirements"],
                         {"needs_computer_use": False, "needs_vision": False})
        self.assertAlmostEqual(record["probabilities"]["min_tier"]["small"], 0.93)
        self.assertEqual(record["instruction_chars"], 400)
        self.assertIsNotNone(record["duration_ms"])
        self.assertIsNotNone(record["latency_ms"])

    async def test_instruction_text_reaches_the_judge_but_never_an_event(self):
        secret = "SENTINELTEXT-do-not-log-this-instruction"
        backend = FakeJudgeBackend([DOWN_STANDARD])
        decider, events = self._setup(backend=backend)
        await decider.adjust({"agent": "researcher", "instruction": secret,
                              "model_role": "research"})
        # The judge saw it (that is the whole point of asking).
        self.assertIn(secret, backend.requests[0].state["instruction"])
        # No event, anywhere, carries a byte of it.
        self.assertNotIn(secret, repr(events))
        record = receipts(events)[0]
        self.assertNotIn("instruction", record)
        self.assertEqual(record["instruction_chars"], len(secret))

    async def test_instruction_is_truncated_before_it_reaches_the_judge(self):
        backend = FakeJudgeBackend([DOWN_STANDARD])
        decider, _ = self._setup(backend=backend)
        await decider.adjust({"agent": "researcher", "instruction": "y" * 9000,
                              "model_role": "research"})
        state = backend.requests[0].state
        self.assertEqual(len(state["instruction"]), delegation.MAX_INSTRUCTION_CHARS)
        self.assertEqual(state["instruction_chars"], 9000)

    async def test_frontmatter_role_chain_is_recorded_and_uses_preresolved_anchor(self):
        agents = {
            "ui-builder": {
                "model_role": ["ui-coding", "coding", "general"],
                "provider_preferences": [
                    {"provider": "anthropic", "model": "claude-opus-5", "config": {}}
                ],
            }
        }
        decider, events = self._setup(backend=FakeJudgeBackend([DOWN_STANDARD]), agents=agents)
        result = await decider.adjust({"agent": "ui-builder", "instruction": "build it"})
        record = receipts(events)[0]
        self.assertEqual(record["model_role"], ["ui-coding", "coding", "general"])
        self.assertEqual(record["role_source"], "agent_frontmatter")
        self.assertEqual(record["anchor_source"], "agent_preresolved")
        self.assertEqual(record["anchor"]["model"], "claude-opus-5")
        # frontier anchor + "small suffices" + standard depth -> effort first.
        self.assertEqual(record["lever"], "effort")
        self.assertEqual(result["provider_preferences"][0]["model"], "claude-opus-5")
        self.assertEqual(result["provider_preferences"][0]["config"]["reasoning_effort"], "low")

    async def test_computer_use_requirement_blocks_an_incapable_candidate(self):
        """A delegation the judge says needs computer use must never be routed
        onto a model verified to reject the computer-use tool type."""
        answers = {
            **DOWN_STANDARD,
            "answer_depth": {"brief": 0.02, "standard": 0.08, "thorough": 0.90},
            "needs_computer_use": {"yes": 0.95, "no": 0.05},
        }
        agents = {
            "operator": {
                "model_role": "general",
                "provider_preferences": [
                    {"provider": "anthropic", "model": "claude-sonnet-5-5", "config": {}}
                ],
            }
        }
        decider, events = self._setup(backend=FakeJudgeBackend([answers]), agents=agents)
        original = {"agent": "operator", "instruction": "click the button"}
        result = await decider.adjust(dict(original))
        record = receipts(events)[0]
        # thorough + down -> the effort lever, which keeps the anchor's own
        # model; that model is the one with the verified conflict, so the
        # policy declines to touch this delegation at all.
        self.assertEqual(record["move"], "down")
        self.assertEqual(record["action"], "abstain")
        self.assertEqual(record["requirements"]["needs_computer_use"], True)
        self.assertEqual(record["capability_conflict"]["capability"], "computer_use")
        self.assertEqual(record["capability_conflict"]["model"], "claude-sonnet-5-5")
        self.assertEqual(result, original)

    async def test_up_needs_the_thorough_threshold(self):
        """Up at thorough depth only on a strong answer (v3: p >= 0.85)."""
        weak = {
            **DOWN_STANDARD,
            "min_tier": {"small": 0.05, "mid": 0.20, "frontier": 0.75},
            "answer_depth": {"brief": 0.02, "standard": 0.08, "thorough": 0.90},
        }
        decider, events = self._setup(backend=FakeJudgeBackend([weak]))
        result = await decider.adjust({"agent": "a", "instruction": "i",
                                       "model_role": "research"})
        record = receipts(events)[0]
        self.assertEqual(record["action"], "abstain")
        self.assertIn("needs p >= 0.85", record["reason"])
        self.assertNotIn("provider_preferences", result)

        strong = {**weak, "min_tier": {"small": 0.03, "mid": 0.06, "frontier": 0.91}}
        decider, events = self._setup(backend=FakeJudgeBackend([strong]))
        result = await decider.adjust({"agent": "a", "instruction": "i",
                                       "model_role": "research"})
        record = receipts(events)[0]
        self.assertEqual(record["action"], "adjust")
        self.assertEqual(record["move"], "up")
        self.assertEqual(result["provider_preferences"][0]["model"], "claude-opus-5")


class AbstainTests(unittest.IsolatedAsyncioTestCase):
    """Every failure is an abstain that returns the ORIGINAL arguments."""

    ORIGINAL = {"agent": "researcher", "instruction": "do the thing",
                "model_role": "research"}

    _setup = staticmethod(build_decider)

    async def _abstains(self, decider, events, *, reason_contains, original=None):
        original = original if original is not None else dict(self.ORIGINAL)
        result = await decider.adjust(dict(original))
        record = receipts(events)[0]
        self.assertEqual(record["action"], "abstain")
        self.assertIn(reason_contains, record["reason"])
        self.assertEqual(result, original)
        self.assertIsNone(record["actual_preference"])
        return record

    async def test_judge_deadline(self):
        backend = FakeJudgeBackend([DOWN_STANDARD], delay_ms=200)
        decider, events = self._setup(backend=backend, deadline_ms=20)
        await self._abstains(decider, events, reason_contains="judge_deadline")

    async def test_unavailable_judge(self):
        decider, events = self._setup(backend=FakeJudgeBackend([None]))
        await self._abstains(decider, events, reason_contains="judge_unavailable")

    async def test_external_backend_without_consent(self):
        """The same gate every other judge ask in this package enforces."""
        backend = FakeJudgeBackend([DOWN_STANDARD], external=True)
        decider, events = self._setup(backend=backend,
                                      policy_kwargs={"allow_external_state": False})
        await self._abstains(decider, events, reason_contains="external_state_not_allowed")
        self.assertEqual(backend.calls, 0, "no external call may be attempted")

    async def test_external_backend_with_consent_is_allowed(self):
        backend = FakeJudgeBackend([DOWN_STANDARD], external=True)
        decider, events = self._setup(backend=backend,
                                      policy_kwargs={"allow_external_state": True})
        await decider.adjust(dict(self.ORIGINAL))
        self.assertEqual(receipts(events)[0]["action"], "adjust")

    async def test_malformed_answers(self):
        decider, events = self._setup(backend=FakeJudgeBackend([{"min_tier": {}}]))
        await self._abstains(decider, events, reason_contains="judge_abstained")

    async def test_missing_depth_answer(self):
        decider, events = self._setup(
            backend=FakeJudgeBackend([{"min_tier": DOWN_STANDARD["min_tier"]}])
        )
        await self._abstains(decider, events, reason_contains="judge_abstained")

    async def test_invalid_probabilities_never_change_delegation(self):
        invalid = [
            {"unexpected": 1.0},
            {"small": float("nan"), "mid": 0.05, "frontier": 0.02},
            {"small": float("inf")},
            {"small": 1.2, "mid": -0.2},
            {"small": True},
            {"small": "0.93", "mid": 0.07},
            {"small": 0.2, "mid": 0.1},
        ]
        for probabilities in invalid:
            with self.subTest(probabilities=probabilities):
                decider, events = self._setup(backend=FakeJudgeBackend([
                    {**DOWN_STANDARD, "min_tier": probabilities}
                ]))
                await self._abstains(decider, events,
                                     reason_contains="judge_malformed_probabilities")

    async def test_invalid_requirement_answer_does_not_downgrade(self):
        decider, events = self._setup(backend=FakeJudgeBackend([
            {**DOWN_STANDARD, "needs_computer_use": {"unknown": 1.0}}
        ]))
        await self._abstains(decider, events,
                             reason_contains="judge_malformed_probabilities")

    async def test_caller_pinned_preferences_win(self):
        backend = FakeJudgeBackend([DOWN_STANDARD])
        decider, events = self._setup(backend=backend)
        original = {**self.ORIGINAL,
                    "provider_preferences": [{"provider": "openai", "model": "gpt-6-luna"}]}
        await self._abstains(decider, events, reason_contains="caller_pinned",
                             original=original)
        self.assertEqual(backend.calls, 0, "an explicit pin is not worth a judge call")

    async def test_no_resolver_capability(self):
        decider, events = self._setup(backend=FakeJudgeBackend([DOWN_STANDARD]),
                                      resolver=False)
        await self._abstains(decider, events, reason_contains="role_resolver_unavailable")

    async def test_failing_resolver(self):
        decider, events = self._setup(backend=FakeJudgeBackend([DOWN_STANDARD]),
                                      resolver=FailingResolver())
        await self._abstains(decider, events, reason_contains="resolver_failed")

    async def test_no_role_and_no_agent_preferences(self):
        decider, events = self._setup(backend=FakeJudgeBackend([DOWN_STANDARD]))
        await self._abstains(decider, events, reason_contains="no_anchor",
                             original={"agent": "nobody", "instruction": "x"})

    async def test_brief_depth_abstains(self):
        """Brief depth was never observed in the sample; the policy has no rule."""
        brief = {**DOWN_STANDARD,
                 "answer_depth": {"brief": 0.90, "standard": 0.08, "thorough": 0.02}}
        decider, events = self._setup(backend=FakeJudgeBackend([brief]))
        await self._abstains(decider, events, reason_contains="brief depth")

    async def test_a_broken_decider_still_runs_the_delegation(self):
        """Any unexpected error is an abstain, never a failed tool call."""

        class Exploding:
            def choose(self, *args, **kwargs):
                raise RuntimeError("boom")

            name = "v3"

        decider, events = self._setup(backend=FakeJudgeBackend([DOWN_STANDARD]))
        decider.policy = Exploding()
        result = await decider.adjust(dict(self.ORIGINAL))
        self.assertEqual(result, self.ORIGINAL)
        self.assertEqual(receipts(events)[0]["reason"], "decider_error_RuntimeError")

    async def test_facade_runs_the_delegation_even_if_adjust_itself_raises(self):
        """adjust() never raises (every test above); the facade assumes nothing."""

        class ExplodingDecider:
            async def adjust(self, input):
                raise RuntimeError("boom")

        tool = FakeDelegateTool()
        facade = DelegateFacade(tool, ExplodingDecider())
        result = await facade.execute(dict(self.ORIGINAL))
        self.assertEqual(result, {"success": True, "output": "delegated"})
        self.assertEqual(tool.calls, [self.ORIGINAL])


class DoubleRoutingTests(unittest.IsolatedAsyncioTestCase):
    """What the CHILD session does with a pin this layer placed.

    A delegation pin reaches the child as its session's provider/model. If the
    child also runs this orchestrator with `model_routing.start_policy`, that
    router decides the child's start tier for itself. These tests record what
    the shipped code actually does today -- they are evidence for the open
    question in docs/DELEGATION-ROUTING.md, not an endorsement of it.
    """

    def _routed_provider(self, default_model):
        from amplifier_fast_decisions.orchestrator import RoutedProvider

        policy = Policy(mode="off")
        service, runtime, _ = setup_service(policy=policy, backend=FakeJudgeBackend([]))
        provider = NS(name="anthropic", default_model=default_model)
        return RoutedProvider(provider, runtime, {}, provider_key="anthropic"), service

    def test_a_delegation_pin_does_not_read_as_a_user_model_pick(self):
        """The pin is the child's model from its first request, so the
        "user switched models mid-session" signal never fires: no existing
        early return protects a delegation pin from the child's own router."""
        routed, service = self._routed_provider("claude-haiku-4-5-20251001")
        self.assertIsNone(routed._user_selected_model(service))
        # Still None on a second look: an unchanged default is not a pick.
        self.assertIsNone(routed._user_selected_model(service))

    def test_a_mid_session_model_change_is_still_detected(self):
        """The same signal DOES fire for a real mid-session switch -- the
        mechanism works, it simply does not see a delegation pin."""
        routed, service = self._routed_provider("claude-sonnet-5")
        self.assertIsNone(routed._user_selected_model(service))
        routed._provider.default_model = "claude-opus-5"
        self.assertEqual(routed._user_selected_model(service), "claude-opus-5")


if __name__ == "__main__":
    unittest.main()
