"""Routing levers (research/routing-levers): three tiers, confident routing in
large repositories, host effort for medium-confidence complex turns, the
profile knob, and served_model on every receipt. Contract doubles only."""

from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace as NS

from amplifier_fast_decisions import orchestrator as orch
from amplifier_fast_decisions.backends import ScriptedBackend
from amplifier_fast_decisions.contracts import (
    Answer, Decision, DecisionResult, Policy, SLOW, TurnState,
)
from amplifier_fast_decisions.demo import DemoCoordinator, DemoProvider, demo_response
from amplifier_fast_decisions.orchestrator import RoutedProvider
from amplifier_fast_decisions.privacy import safe_data
from amplifier_fast_decisions.routing_levers import (
    PROFILES, apply_profile, choose_tier, large_repo_allows_cheap, strong_effort,
)
from amplifier_fast_decisions.runtime import Runtime
from amplifier_fast_decisions.service import DecisionService
from amplifier_fast_decisions.telemetry import Emitter

TIERS = [
    {"max_p_complex": 0.2, "model": "claude-haiku-4-5", "effort": "low", "label": "haiku"},
    {"max_p_complex": 0.5, "model": "claude-sonnet-5", "effort": "medium", "label": "sonnet"},
]
BASE = {"start_model": "claude-sonnet-5", "provider_match": "anthropic", "start_policy": "judge"}


class FakeJudge:
    """Answers task_difficulty and (when asked) edits_code in one call."""
    name = "fake-judge"
    external = False

    def __init__(self, p_complex=None, p_edit=None, fail=False):
        self.p_complex, self.p_edit, self.fail = p_complex, p_edit, fail
        self.calls, self.questions = 0, []

    async def ask(self, request):
        self.calls += 1
        self.questions.append([q.name for q in request.questions])
        if self.fail:
            raise RuntimeError("judge down")
        answers = {}
        for q in request.questions:
            if q.name == "task_difficulty":
                answers[q.name] = Answer(probabilities={"simple": 1 - self.p_complex, "complex": self.p_complex})
            elif q.name == "edits_code" and self.p_edit is not None:
                answers[q.name] = Answer(probabilities={"edits": self.p_edit, "read_only": 1 - self.p_edit})
        return DecisionResult(action=Decision(choice=SLOW, probabilities={SLOW: 1.0}), answers=answers)

    async def ask_many(self, request):
        return await self.ask(request)

    async def close(self):
        pass


class RecordingProvider(DemoProvider):
    def __init__(self, default_model="claude-opus-5-5", served_model=None):
        super().__init__(delay_ms=0)
        self.default_model = default_model
        self.served_model = served_model
        self.kwargs_seen = []

    async def complete(self, request, **kwargs):
        self.kwargs_seen.append(dict(kwargs))
        response = await super().complete(request, **kwargs)
        if self.served_model is not None:
            response.model = self.served_model
        return response


async def run_turn(routing, judge=None, prompt="fix the typo", effort_routing=None, provider=None,
                   requests=1, profile=None):
    events = []
    coordinator = DemoCoordinator()
    emitter = Emitter(coordinator.session_id, callback=events.append)
    policy = Policy(mode="off", model_routing=routing, effort_routing=effort_routing,
                    read_shortcut=False, profile=profile)
    service = DecisionService(policy, judge or ScriptedBackend(delay_ms=0), emitter, coordinator, [])
    service.turn = TurnState("t")
    provider = provider or RecordingProvider()
    facade = RoutedProvider(provider, Runtime(service), {}, demo_response, "anthropic-primary")
    reqs = []
    for _ in range(requests):
        req = NS(messages=[{"role": "user", "content": prompt}], tools=[], tool_choice="auto")
        await facade.complete(req)
        reqs.append(req)
    judged = [e["data"] for e in events if e["event"].endswith("difficulty_judged")]
    ends = [e["data"] for e in events if e["event"].endswith("slow_end")]
    return provider, reqs, judged, ends, service.turn


class InWorkspace:
    """chdir into a temp workspace with ``n`` files (scope gate input)."""

    def __init__(self, n):
        self.n = n

    def __enter__(self):
        orch._workspace_file_counts.clear()
        self._tmp = tempfile.TemporaryDirectory()
        for i in range(self.n):
            open(os.path.join(self._tmp.name, f"f{i}.py"), "w").close()
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)
        return self

    def __exit__(self, *exc):
        os.chdir(self._cwd)
        self._tmp.cleanup()
        orch._workspace_file_counts.clear()


class TierTests(unittest.IsolatedAsyncioTestCase):
    async def _tier(self, p):
        return await run_turn(dict(BASE, tiers=TIERS), FakeJudge(p_complex=p), requests=2)

    async def test_very_easy_turn_takes_the_lowest_tier_for_the_whole_turn(self):
        provider, reqs, judged, _, turn = await self._tier(0.1)
        self.assertEqual([k.get("model") for k in provider.kwargs_seen], ["claude-haiku-4-5"] * 2)
        self.assertEqual([r.reasoning_effort for r in reqs], ["low", "low"])
        self.assertEqual(judged[0]["tier"], "haiku")
        self.assertEqual(judged[0]["reason_code"], "judge_cheap")
        self.assertEqual(turn.tier_model, "claude-haiku-4-5")

    async def test_easy_turn_takes_the_middle_tier(self):
        provider, reqs, judged, _, _ = await self._tier(0.3)
        self.assertEqual(provider.kwargs_seen[0].get("model"), "claude-sonnet-5")
        self.assertEqual(reqs[0].reasoning_effort, "medium")
        self.assertEqual(judged[0]["tier"], "sonnet")

    async def test_complex_turn_stays_on_the_host(self):
        provider, reqs, judged, _, _ = await self._tier(0.5)   # threshold is strict
        self.assertNotIn("model", provider.kwargs_seen[0])
        self.assertIsNone(getattr(reqs[0], "reasoning_effort", None))
        self.assertEqual(judged[0]["reason_code"], "judge_strong")
        self.assertNotIn("tier", judged[0])

    async def test_judge_failure_uses_rules_and_start_model_never_a_lower_tier(self):
        provider, _, judged, _, turn = await run_turn(dict(BASE, tiers=TIERS), FakeJudge(fail=True))
        self.assertEqual(judged[0]["reason_code"], "rules_cheap")
        self.assertEqual(provider.kwargs_seen[0].get("model"), "claude-sonnet-5")
        self.assertIsNone(turn.tier_model)

    async def test_tier_effort_overrides_by_tier_effort(self):
        effort_routing = {"orient": "medium", "by_tier": {"cheap": "medium", "strong": None}}
        _, reqs, _, _, _ = await run_turn(dict(BASE, tiers=TIERS), FakeJudge(p_complex=0.05),
                                          effort_routing=effort_routing)
        self.assertEqual(reqs[0].reasoning_effort, "low")

    async def test_without_tiers_behavior_is_unchanged(self):
        provider, reqs, judged, _, turn = await run_turn(dict(BASE, start_effort="medium"), FakeJudge(p_complex=0.1))
        self.assertEqual(provider.kwargs_seen[0].get("model"), "claude-sonnet-5")
        self.assertEqual(reqs[0].reasoning_effort, "medium")
        self.assertEqual(judged[0]["reason_code"], "judge_cheap")
        self.assertIsNone(turn.tier_label)

    def test_choose_tier(self):
        routing = dict(BASE, tiers=TIERS)
        self.assertEqual(choose_tier(routing, 0.0)[0], "haiku")
        self.assertEqual(choose_tier(routing, 0.2)[0], "sonnet")
        self.assertIsNone(choose_tier(routing, 0.9))
        self.assertEqual(choose_tier(BASE, 0.49), ("cheap", None, None))
        self.assertIsNone(choose_tier(dict(BASE, complex_min_probability=0.3), 0.3))


class LargeRepoTests(unittest.IsolatedAsyncioTestCase):
    GATED = dict(BASE, cheap_max_workspace_files=10)

    async def _large(self, routing, judge):
        with InWorkspace(25):
            return await run_turn(routing, judge)

    async def test_without_the_lever_the_gate_never_asks(self):
        judge = FakeJudge(p_complex=0.01, p_edit=0.01)
        provider, _, judged, _, _ = await self._large(self.GATED, judge)
        self.assertEqual(judged[0]["reason_code"], "scope_strong")
        self.assertEqual(judge.calls, 0)
        self.assertNotIn("model", provider.kwargs_seen[0])

    async def test_both_mode_keeps_repository_edits_on_the_host(self):
        routing = dict(self.GATED, large_repo={"max_p_complex": 0.3, "non_editing_max_p_edit": 0.5})
        judge = FakeJudge(p_complex=0.05, p_edit=0.9)
        provider, _, judged, _, _ = await self._large(routing, judge)
        self.assertEqual(judge.calls, 1)
        self.assertEqual(judge.questions[0], ["task_difficulty", "edits_code"])   # one batched call
        self.assertEqual(judged[0]["reason_code"], "scope_judge_strong")
        self.assertEqual(judged[0]["candidate_count"], 25)
        self.assertAlmostEqual(judged[0]["probabilities"]["edits_code"], 0.9)
        self.assertNotIn("model", provider.kwargs_seen[0])

    async def test_both_mode_routes_an_easy_question(self):
        routing = dict(self.GATED, large_repo={"max_p_complex": 0.3, "non_editing_max_p_edit": 0.5})
        provider, _, judged, _, _ = await self._large(routing, FakeJudge(p_complex=0.05, p_edit=0.1))
        self.assertEqual(judged[0]["reason_code"], "scope_judge_cheap")
        self.assertEqual(provider.kwargs_seen[0].get("model"), "claude-sonnet-5")

    async def test_either_mode_routes_a_read_only_turn_judged_complex_to_start_model(self):
        routing = dict(self.GATED, tiers=TIERS,
                       large_repo={"max_p_complex": 0.3, "non_editing_max_p_edit": 0.5, "require": "either"})
        provider, _, judged, _, _ = await self._large(routing, FakeJudge(p_complex=0.8, p_edit=0.1))
        self.assertEqual(judged[0]["reason_code"], "scope_judge_cheap")
        self.assertEqual(provider.kwargs_seen[0].get("model"), "claude-sonnet-5")

    async def test_either_mode_confident_easy_edit_takes_its_tier(self):
        routing = dict(self.GATED, tiers=TIERS,
                       large_repo={"max_p_complex": 0.3, "non_editing_max_p_edit": 0.5, "require": "either"})
        provider, _, judged, _, _ = await self._large(routing, FakeJudge(p_complex=0.1, p_edit=0.9))
        self.assertEqual(judged[0]["tier"], "haiku")
        self.assertEqual(provider.kwargs_seen[0].get("model"), "claude-haiku-4-5")

    async def test_difficulty_only_lever_asks_one_question(self):
        routing = dict(self.GATED, large_repo={"max_p_complex": 0.15})
        judge = FakeJudge(p_complex=0.1)
        _, _, judged, _, _ = await self._large(routing, judge)
        self.assertEqual(judge.questions[0], ["task_difficulty"])
        self.assertEqual(judged[0]["reason_code"], "scope_judge_cheap")

    async def test_judge_failure_keeps_the_host(self):
        routing = dict(self.GATED, large_repo={"max_p_complex": 0.3, "non_editing_max_p_edit": 0.5,
                                               "require": "either"})
        provider, _, judged, _, _ = await self._large(routing, FakeJudge(fail=True))
        self.assertEqual(judged[0]["reason_code"], "scope_fallback_strong")
        self.assertNotIn("model", provider.kwargs_seen[0])

    def test_allows_cheap_truth_table(self):
        both = {"large_repo": {"max_p_complex": 0.3, "non_editing_max_p_edit": 0.5}}
        either = {"large_repo": {**both["large_repo"], "require": "either"}}
        self.assertTrue(large_repo_allows_cheap(both, 0.1, 0.1))
        self.assertFalse(large_repo_allows_cheap(both, 0.1, 0.9))
        self.assertFalse(large_repo_allows_cheap(both, 0.1, None))     # missing answer = not satisfied
        self.assertTrue(large_repo_allows_cheap(either, 0.9, 0.1))
        self.assertTrue(large_repo_allows_cheap(either, 0.1, None))
        self.assertFalse(large_repo_allows_cheap(either, 0.9, 0.9))
        self.assertFalse(large_repo_allows_cheap({}, 0.0, 0.0))


class StrongEffortTests(unittest.IsolatedAsyncioTestCase):
    ROUTING = dict(BASE, strong_effort={"max_p_complex": 0.7, "effort": "medium"})

    async def test_medium_confidence_complex_turn_gets_host_effort(self):
        provider, reqs, judged, _, _ = await run_turn(self.ROUTING, FakeJudge(p_complex=0.6), requests=2)
        self.assertNotIn("model", provider.kwargs_seen[0])
        self.assertEqual([r.reasoning_effort for r in reqs], ["medium", "medium"])
        self.assertEqual(judged[0]["tier"], "strong_effort")

    async def test_confident_complex_turn_keeps_provider_default(self):
        _, reqs, judged, _, _ = await run_turn(self.ROUTING, FakeJudge(p_complex=0.9))
        self.assertIsNone(getattr(reqs[0], "reasoning_effort", None))
        self.assertNotIn("tier", judged[0])

    async def test_overrides_by_tier_strong_none(self):
        effort_routing = {"orient": "medium", "by_tier": {"cheap": "medium", "strong": None}}
        _, reqs, _, _, _ = await run_turn(self.ROUTING, FakeJudge(p_complex=0.6), effort_routing=effort_routing)
        self.assertEqual(reqs[0].reasoning_effort, "medium")

    def test_helper(self):
        self.assertEqual(strong_effort(self.ROUTING, 0.5), "medium")
        self.assertIsNone(strong_effort(self.ROUTING, None))
        self.assertIsNone(strong_effort(BASE, 0.5))


class ProfileTests(unittest.IsolatedAsyncioTestCase):
    ROUTING = dict(BASE, complex_min_probability=0.5, cheap_max_workspace_files=300)

    def test_balanced_is_todays_default(self):
        config = {"profile": "balanced", "model_routing": dict(self.ROUTING)}
        self.assertEqual(apply_profile(config), config)
        self.assertEqual(Policy.from_config(config).model_routing, self.ROUTING)

    def test_frugal_adds_tiers_and_large_repo(self):
        policy = Policy.from_config({"profile": "frugal", "model_routing": dict(self.ROUTING)})
        self.assertEqual(policy.profile, "frugal")
        self.assertEqual(policy.model_routing["tiers"][0]["model"], "claude-haiku-4-5")
        self.assertEqual(policy.model_routing["large_repo"]["require"], "either")
        self.assertEqual(policy.model_routing["cheap_max_workspace_files"], 300)   # gate stays

    def test_careful_tightens_the_gate_and_drops_levers(self):
        config = {"profile": "careful", "model_routing": dict(self.ROUTING, tiers=TIERS,
                                                             large_repo={"max_p_complex": 0.2})}
        routing = Policy.from_config(config).model_routing
        self.assertEqual(routing["complex_min_probability"], 0.3)
        self.assertNotIn("tiers", routing)
        self.assertNotIn("large_repo", routing)

    def test_profile_never_turns_routing_on(self):
        self.assertIsNone(Policy.from_config({"profile": "frugal"}).model_routing)

    def test_unknown_profile_is_rejected(self):
        with self.assertRaises(ValueError):
            Policy.from_config({"profile": "yolo", "model_routing": dict(self.ROUTING)})

    def test_every_profile_validates(self):
        for name in PROFILES:
            Policy.from_config({"profile": name, "model_routing": dict(self.ROUTING)})

    def test_config_input_is_not_mutated(self):
        routing = dict(self.ROUTING)
        apply_profile({"profile": "frugal", "model_routing": routing})
        self.assertNotIn("tiers", routing)

    async def test_profile_is_recorded_on_the_receipt(self):
        routing = apply_profile({"profile": "frugal", "model_routing": dict(BASE)})["model_routing"]
        _, _, judged, _, _ = await run_turn(routing, FakeJudge(p_complex=0.1), profile="frugal")
        self.assertEqual(judged[0]["profile"], "frugal")
        self.assertEqual(safe_data(judged[0])["tier"], "haiku")   # survives the telemetry allowlist


class ValidationTests(unittest.TestCase):
    def _bad(self, **routing):
        with self.assertRaises(ValueError):
            Policy(model_routing=dict(BASE, **routing))

    def test_rejects_malformed_levers(self):
        self._bad(tiers=[])
        self._bad(tiers=list(reversed(TIERS)))
        self._bad(tiers=[{"max_p_complex": 0.2, "model": "m", "effort": "turbo"}])
        self._bad(tiers=[{"max_p_complex": 0.2, "model": ""}])
        self._bad(tiers=[{"max_p_complex": 1.5, "model": "m"}])
        self._bad(tiers=[{"max_p_complex": 0.2, "model": "m", "colour": "red"}])
        self._bad(tiers=[{"max_p_complex": 0.2, "model": "m", "label": "Has Spaces"}])
        self._bad(large_repo={})
        self._bad(large_repo={"max_p_complex": 0.2, "require": "maybe"})
        self._bad(large_repo={"non_editing_max_p_edit": 0})
        self._bad(strong_effort={"max_p_complex": 0.7})
        self._bad(strong_effort={"max_p_complex": 0.7, "effort": "medium", "x": 1})

    def test_accepts_well_formed_levers(self):
        Policy(model_routing=dict(BASE, tiers=TIERS, large_repo={"non_editing_max_p_edit": 0.4},
                                  strong_effort={"max_p_complex": 0.7, "effort": "high"}))


class ServedModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_response_model_wins(self):
        provider = RecordingProvider(served_model="claude-sonnet-5-20260601")
        _, _, _, ends, _ = await run_turn(dict(BASE), FakeJudge(p_complex=0.1), provider=provider)
        self.assertEqual(ends[0]["served_model"], "claude-sonnet-5-20260601")
        self.assertEqual(ends[0]["served_model_source"], "response")

    async def test_routed_request_without_response_model_records_the_sent_model(self):
        _, _, _, ends, _ = await run_turn(dict(BASE, tiers=TIERS), FakeJudge(p_complex=0.1))
        self.assertEqual(ends[0]["served_model"], "claude-haiku-4-5")
        self.assertEqual(ends[0]["served_model_source"], "requested")

    async def test_host_turn_without_response_model_records_the_provider_default(self):
        _, _, _, ends, _ = await run_turn(dict(BASE), FakeJudge(p_complex=0.9))
        self.assertEqual(ends[0]["served_model"], "claude-opus-5-5")
        self.assertEqual(ends[0]["served_model_source"], "requested")

    async def test_fields_survive_the_telemetry_allowlist(self):
        _, _, _, ends, _ = await run_turn(dict(BASE), FakeJudge(p_complex=0.9))
        self.assertEqual(safe_data(ends[0])["served_model_source"], "requested")


class EscalationTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_error_escalation_drops_the_tier_effort(self):
        class Flaky(RecordingProvider):
            async def complete(self, request, **kwargs):
                if not self.kwargs_seen:
                    self.kwargs_seen.append(dict(kwargs))
                    raise RuntimeError("overloaded")
                return await super().complete(request, **kwargs)

        events = []
        coordinator = DemoCoordinator()
        emitter = Emitter(coordinator.session_id, callback=events.append)
        policy = Policy(mode="off", read_shortcut=False,
                        effort_routing={"orient": "medium", "by_tier": {"cheap": "medium", "strong": None}},
                        model_routing=dict(BASE, tiers=TIERS, escalate_on_provider_error=True))
        service = DecisionService(policy, FakeJudge(p_complex=0.1), emitter, coordinator, [])
        service.turn = TurnState("t")
        provider = Flaky()
        facade = RoutedProvider(provider, Runtime(service), {}, demo_response, "anthropic")
        first = NS(messages=[{"role": "user", "content": "x"}], tools=[], tool_choice="auto")
        with self.assertRaises(RuntimeError):
            await facade.complete(first)
        second = NS(messages=[{"role": "user", "content": "x"}], tools=[], tool_choice="auto")
        await facade.complete(second)
        self.assertEqual(provider.kwargs_seen[0].get("model"), "claude-haiku-4-5")
        self.assertNotIn("model", provider.kwargs_seen[1])
        self.assertNotEqual(getattr(second, "reasoning_effort", None), "low")


if __name__ == "__main__":
    unittest.main()
