"""Turn planner: cache- and price-aware model choice per turn.

Spec: docs/proposals/TURN-PLANNER.md. No amplifier_core, no network.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

from amplifier_fast_decisions import planner
from amplifier_fast_decisions.backends import ScriptedBackend
from amplifier_fast_decisions.contracts import (
    DEFAULT_PLANNER_CONTINUE_PROBABILITY,
    DEFAULT_PLANNER_PRIORS,
    Policy,
    TurnState,
    effective_planner_config,
)
from amplifier_fast_decisions.demo import DemoCoordinator, DemoProvider, demo_response
from amplifier_fast_decisions.orchestrator import (
    RoutedProvider,
    _estimate_ctx,
    _request_chars,
    _session_kind,
)
from amplifier_fast_decisions.runtime import Runtime, load_planner_state, save_planner_state
from amplifier_fast_decisions.savings import DEFAULT_RATES
from amplifier_fast_decisions.service import DecisionService
from amplifier_fast_decisions.telemetry import Emitter

BALANCED_CONFIG: dict[str, Any] = {
    "objective": "balanced",
    "cost_tolerance": 0.05,
    "cache_ttl_seconds": 300,
    "expected_calls": 3.5,
    "output_tokens_per_call": 170,
    "priors": DEFAULT_PLANNER_PRIORS,
}
OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5"
FABLE = "claude-fable-5-1"


def config(**overrides) -> dict[str, Any]:
    return {**BALANCED_CONFIG, **overrides}


# ---------------------------------------------------------------------------
# Pure function: planner.plan_turn
# ---------------------------------------------------------------------------


class PlanTurnTests(unittest.TestCase):
    def test_fresh_session_opus_host_balanced_prefers_sonnet(self):
        """Both cold; Sonnet writes cheaper and is faster -- balanced picks it."""
        plan = planner.plan_turn(OPUS, [SONNET], {}, 60000, 0.0, config(), DEFAULT_RATES)
        self.assertFalse(plan["abstained"])
        by_model = {o["model"]: o for o in plan["options"]}
        self.assertLess(by_model[SONNET]["cost"], by_model[OPUS]["cost"])
        self.assertLess(by_model[SONNET]["time"], by_model[OPUS]["time"])
        self.assertEqual(plan["choice"], SONNET)

    def test_speed_and_cost_objectives_agree_when_one_model_wins_both(self):
        for objective in ("speed", "cost"):
            plan = planner.plan_turn(
                OPUS, [SONNET], {}, 60000, 0.0, config(objective=objective), DEFAULT_RATES
            )
            self.assertEqual(plan["choice"], SONNET, objective)

    def test_opus_warm_80k_cached_wins_over_cold_sonnet(self):
        """Same session later: Opus warm with 80k cached beats a cold Sonnet."""
        state = {OPUS: {"last_used_at": 0.0, "cached_tokens": 80000}}
        plan = planner.plan_turn(OPUS, [SONNET], state, 90000, 10.0, config(), DEFAULT_RATES)
        self.assertFalse(plan["abstained"])
        by_model = {o["model"]: o for o in plan["options"]}
        self.assertTrue(by_model[OPUS]["warm"])
        self.assertFalse(by_model[SONNET]["warm"])
        self.assertEqual(by_model[OPUS]["cold"], 10000)
        self.assertLess(by_model[OPUS]["cost"], by_model[SONNET]["cost"])
        self.assertEqual(plan["choice"], OPUS)

    def test_fable_warm_sonnet_cold_speed_vs_cost_diverge(self):
        """Fable host warm (cold=0) with Sonnet cold at 30k: speed picks the
        faster Sonnet, cost picks the cheaper (warm) Fable."""
        state = {FABLE: {"last_used_at": 0.0, "cached_tokens": 40000}}
        speed_plan = planner.plan_turn(
            FABLE, [SONNET], state, 30000, 10.0, config(objective="speed"), DEFAULT_RATES
        )
        cost_plan = planner.plan_turn(
            FABLE, [SONNET], state, 30000, 10.0, config(objective="cost"), DEFAULT_RATES
        )
        self.assertEqual(speed_plan["choice"], SONNET)
        self.assertEqual(cost_plan["choice"], FABLE)

    def test_unknown_host_price_abstains(self):
        plan = planner.plan_turn(
            "totally-unknown-model", [SONNET], {}, 1000, 0.0, config(), DEFAULT_RATES
        )
        self.assertTrue(plan["abstained"])
        self.assertIsNone(plan["choice"])
        self.assertEqual(plan["options"], [])

    def test_unknown_host_prior_also_abstains(self):
        """A price with no prior is equally not a candidate -- including the host."""
        rates = {**DEFAULT_RATES, "priced-no-prior": (1.0, 1.0, 1.0, 1.0)}
        plan = planner.plan_turn("priced-no-prior", [SONNET], {}, 1000, 0.0, config(), rates)
        self.assertTrue(plan["abstained"])

    def test_ttl_expiry_makes_a_model_cold(self):
        state = {OPUS: {"last_used_at": 0.0, "cached_tokens": 80000}}
        cfg = config(cache_ttl_seconds=300)
        warm_plan = planner.plan_turn(OPUS, [SONNET], state, 90000, 299.0, cfg, DEFAULT_RATES)
        cold_plan = planner.plan_turn(OPUS, [SONNET], state, 90000, 301.0, cfg, DEFAULT_RATES)
        by_model_warm = {o["model"]: o for o in warm_plan["options"]}
        by_model_cold = {o["model"]: o for o in cold_plan["options"]}
        self.assertTrue(by_model_warm[OPUS]["warm"])
        self.assertFalse(by_model_cold[OPUS]["warm"])
        self.assertEqual(by_model_cold[OPUS]["cold"], 90000)
        # Now cold, Opus is no longer cheap enough to stay eligible for balanced.
        self.assertEqual(warm_plan["choice"], OPUS)
        self.assertEqual(cold_plan["choice"], SONNET)

    def test_candidates_defaulting_and_dedup(self):
        """The host itself listed again as a candidate is not double-counted."""
        plan = planner.plan_turn(OPUS, [OPUS, SONNET, SONNET], {}, 60000, 0.0, config(), DEFAULT_RATES)
        models = [o["model"] for o in plan["options"]]
        self.assertEqual(len(models), len(set(models)))

    def test_ties_go_to_host(self):
        """Identical price/prior for host and candidate: a tie always picks the host."""
        rates = {**DEFAULT_RATES, "twin-model": DEFAULT_RATES[OPUS]}
        priors = {**DEFAULT_PLANNER_PRIORS, "twin-model": DEFAULT_PLANNER_PRIORS[OPUS]}
        plan = planner.plan_turn(
            OPUS, ["twin-model"], {}, 60000, 0.0, config(priors=priors), rates
        )
        self.assertEqual(plan["choice"], OPUS)

    def test_candidate_vs_candidate_tie_goes_to_first_listed_candidate(self):
        """TURN-PLANNER.md ## Behaviour only specifies "ties go to the host" --
        it is silent on a tie between two non-host candidates. The
        implementation resolves `winners[0]` by the ORDER of the `candidates`
        sequence the caller passed in (not by dict/hash ordering). This test
        locks in that deterministic, caller-order-dependent behaviour and
        proves it by reversing the candidate order and observing the choice
        flip -- so any future change to a hash- or dict-ordering-based
        tie-break (which would NOT be reliably order-preserving) fails loudly."""
        rates = {**DEFAULT_RATES, "twin-a": DEFAULT_RATES[SONNET], "twin-b": DEFAULT_RATES[SONNET]}
        priors = {
            **DEFAULT_PLANNER_PRIORS,
            "twin-a": DEFAULT_PLANNER_PRIORS[SONNET],
            "twin-b": DEFAULT_PLANNER_PRIORS[SONNET],
        }
        cfg = config(objective="cost", priors=priors)

        forward = planner.plan_turn(OPUS, ["twin-a", "twin-b"], {}, 60000, 0.0, cfg, rates)
        reversed_ = planner.plan_turn(OPUS, ["twin-b", "twin-a"], {}, 60000, 0.0, cfg, rates)

        self.assertFalse(forward["abstained"])
        by_model = {o["model"]: o for o in forward["options"]}
        self.assertEqual(by_model["twin-a"]["cost"], by_model["twin-b"]["cost"])
        self.assertLess(by_model["twin-a"]["cost"], by_model[OPUS]["cost"])  # host not a winner

        self.assertEqual(forward["choice"], "twin-a")
        self.assertEqual(reversed_["choice"], "twin-b")


# ---------------------------------------------------------------------------
# Config validation (contracts.py)
# ---------------------------------------------------------------------------


class PlannerValidationTests(unittest.TestCase):
    def test_absent_planner_key_is_valid(self):
        Policy(model_routing={"start_model": SONNET})

    def test_planner_none_is_valid(self):
        Policy(model_routing={"start_model": SONNET, "planner": None})

    def test_planner_must_be_dict(self):
        with self.assertRaises(ValueError):
            Policy(model_routing={"start_model": SONNET, "planner": "yes"})

    def test_unknown_planner_key_raises(self):
        with self.assertRaises(ValueError):
            Policy(model_routing={"start_model": SONNET, "planner": {"bogus": True}})

    def test_enabled_must_be_bool(self):
        with self.assertRaises(ValueError):
            Policy(model_routing={"start_model": SONNET, "planner": {"enabled": "yes"}})

    def test_invalid_objective_raises(self):
        with self.assertRaises(ValueError):
            Policy(
                model_routing={
                    "start_model": SONNET,
                    "planner": {"enabled": True, "objective": "fastest"},
                }
            )

    def test_negative_cost_tolerance_raises(self):
        with self.assertRaises(ValueError):
            Policy(
                model_routing={
                    "start_model": SONNET,
                    "planner": {"enabled": True, "cost_tolerance": -0.1},
                }
            )

    def test_candidates_must_be_strings(self):
        with self.assertRaises(ValueError):
            Policy(
                model_routing={
                    "start_model": SONNET,
                    "planner": {"enabled": True, "candidates": [1, 2]},
                }
            )

    def test_non_positive_cache_ttl_raises(self):
        with self.assertRaises(ValueError):
            Policy(
                model_routing={
                    "start_model": SONNET,
                    "planner": {"enabled": True, "cache_ttl_seconds": 0},
                }
            )

    def test_priors_unknown_model_key_shape_ok_but_bad_field_raises(self):
        with self.assertRaises(ValueError):
            Policy(
                model_routing={
                    "start_model": SONNET,
                    "planner": {
                        "enabled": True,
                        "priors": {"my-model": {"latency_s": -1.0}},
                    },
                }
            )

    def test_priors_unknown_prior_key_raises(self):
        with self.assertRaises(ValueError):
            Policy(
                model_routing={
                    "start_model": SONNET,
                    "planner": {
                        "enabled": True,
                        "priors": {"my-model": {"bogus": 1.0}},
                    },
                }
            )

    def test_valid_full_planner_config_accepted(self):
        Policy(
            model_routing={
                "start_model": SONNET,
                "planner": {
                    "enabled": True,
                    "objective": "speed",
                    "cost_tolerance": 0.1,
                    "candidates": [SONNET, "claude-haiku-4-5"],
                    "cache_ttl_seconds": 120,
                    "expected_calls": 2.0,
                    "output_tokens_per_call": 100,
                    "priors": {SONNET: {"latency_s": 1.0}},
                },
            }
        )


class EffectivePlannerConfigTests(unittest.TestCase):
    def test_none_model_routing_returns_none(self):
        self.assertIsNone(effective_planner_config(None))

    def test_missing_planner_key_returns_none(self):
        self.assertIsNone(effective_planner_config({"start_model": SONNET}))

    def test_disabled_returns_none(self):
        self.assertIsNone(
            effective_planner_config({"start_model": SONNET, "planner": {"enabled": False}})
        )

    def test_enabled_merges_defaults(self):
        merged = effective_planner_config(
            {"start_model": SONNET, "planner": {"enabled": True}}
        )
        self.assertIsNotNone(merged)
        self.assertEqual(merged["objective"], "balanced")
        self.assertEqual(merged["priors"], DEFAULT_PLANNER_PRIORS)

    def test_custom_prior_overrides_only_that_model(self):
        merged = effective_planner_config(
            {
                "start_model": SONNET,
                "planner": {"enabled": True, "priors": {SONNET: {"latency_s": 9.9}}},
            }
        )
        self.assertEqual(merged["priors"][SONNET], {"latency_s": 9.9})
        self.assertEqual(merged["priors"][OPUS], DEFAULT_PLANNER_PRIORS[OPUS])


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------


def user(content="Fix the bug"):
    return {"role": "user", "content": content}


def request(messages, model=None):
    kwargs = {"messages": messages, "tools": [], "tool_choice": "auto"}
    if model is not None:
        kwargs["model"] = model
    return NS(**kwargs)


def setup_service(*, policy=None):
    events = []
    coordinator = DemoCoordinator()
    policy = policy or Policy(mode="off")
    emitter = Emitter(coordinator.session_id, callback=events.append)
    service = DecisionService(policy, ScriptedBackend(delay_ms=0), emitter, coordinator, [])
    service.turn = TurnState("test-turn")
    runtime = Runtime(service)
    return service, runtime, events


@dataclass
class PricedResponse:
    content: list = field(default_factory=list)
    tool_calls: list = field(default_factory=list)
    model: str = ""
    usage: Any = None


class PricedProvider:
    """Contract double: reports a controllable served model and usage
    (input/cache_read/cache_write tokens) per call, in call order -- lets a
    test drive the turn planner's per-model cache state precisely."""

    name = "priced-provider"

    def __init__(self, default_model: str, usage_by_call: list[dict] | None = None):
        self.default_model = default_model
        self.calls = 0
        self.requests: list[Any] = []
        self.usage_by_call = usage_by_call or []

    def get_info(self):
        return NS(id=self.name, display_name=self.name, context_window=200000)

    async def list_models(self):
        return []

    def parse_tool_calls(self, response):
        return response.tool_calls

    async def complete(self, request, **kwargs):
        self.requests.append(request)
        served = kwargs.get("model") or self.default_model
        idx = self.calls
        self.calls += 1
        spec = self.usage_by_call[idx] if idx < len(self.usage_by_call) else {}
        usage = NS(
            input_tokens=spec.get("input_tokens", 100),
            output_tokens=spec.get("output_tokens", 50),
            total_tokens=spec.get("input_tokens", 100) + spec.get("output_tokens", 50),
            cache_read_tokens=spec.get("cache_read_tokens", 0),
            cache_write_tokens=spec.get("cache_write_tokens", 0),
        )
        return PricedResponse(model=served, usage=usage)


def planner_policy(**planner_overrides) -> Policy:
    return Policy(
        mode="off",
        model_routing={
            "start_model": SONNET,
            "planner": {"enabled": True, "objective": "balanced", **planner_overrides},
        },
    )


class PlannerDisabledByteIdenticalTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_planner_key_is_byte_identical_to_pre_planner_hc04(self):
        policy = Policy(mode="off", model_routing={"start_model": SONNET})
        _service, runtime, events = setup_service(policy=policy)
        provider = DemoProvider(delay_ms=0)
        facade = RoutedProvider(provider, runtime, {}, demo_response)
        req = request([user()])

        await facade.complete(req)

        self.assertEqual(req.model, SONNET)
        routed = [e for e in events if e["event"].endswith("model_routed")]
        self.assertEqual(routed[0]["data"]["reason_code"], "start_model")
        self.assertFalse(any(e["event"].endswith("turn_planned") for e in events))

    async def test_planner_enabled_false_is_byte_identical(self):
        policy = Policy(
            mode="off",
            model_routing={"start_model": SONNET, "planner": {"enabled": False}},
        )
        _service, runtime, events = setup_service(policy=policy)
        provider = DemoProvider(delay_ms=0)
        facade = RoutedProvider(provider, runtime, {}, demo_response)
        req = request([user()])

        await facade.complete(req)

        self.assertEqual(req.model, SONNET)
        routed = [e for e in events if e["event"].endswith("model_routed")]
        self.assertEqual(routed[0]["data"]["reason_code"], "start_model")
        self.assertFalse(any(e["event"].endswith("turn_planned") for e in events))


def _strip_nondeterministic_event_fields(events):
    """Drop fields that are inherently variable per-run (random ids, wall-clock
    timestamps, monotonic clock, the randomized DemoCoordinator session_id, and
    the randomized per-decision correlation id -- DecisionService.last_decision_id
    is `uuid4().hex` generated fresh per decision in service.py, and shows up both
    as a top-level event field and, for some events, inside `data` too) so that
    two independent runs can be compared for true payload equality without false
    negatives from these expected, behavior-irrelevant sources of variance."""
    variable_keys = {
        "event_id",
        "timestamp",
        "monotonic_ns",
        "session_id",
        "parent_session_id",
        "decision_id",
        "provider_call_id",
        "duration_ms",
    }

    def strip(obj):
        if isinstance(obj, dict):
            return {k: strip(v) for k, v in obj.items() if k not in variable_keys}
        if isinstance(obj, list):
            return [strip(v) for v in obj]
        return obj

    return [strip(e) for e in events]


class PlannerDisabledFullByteIdenticalTests(unittest.IsolatedAsyncioTestCase):
    """Strengthens PlannerDisabledByteIdenticalTests: the existing tests above
    only assert req.model equality plus two event fields (reason_code and
    turn_planned-absence). That is NOT the "byte-identical requests and
    receipts" comparison the spec's disabled-planner requirement calls for --
    a planner-disabled run could still diverge in some other request field or
    some other emitted event field/value and those tests would not catch it.

    These tests instead perform a genuine full-payload comparison: the ENTIRE
    constructed request object (every field) and the ENTIRE emitted event
    stream (every event, every field, in order) between a planner-ABSENT
    baseline run and a planner-enabled-but-DISABLED run, via
    json.dumps(sort_keys=True) string equality. Only fields that are
    inherently non-deterministic per run (random ids, timestamps) are
    stripped first -- everything else must match exactly.
    """

    async def _run(self, policy):
        _service, runtime, events = setup_service(policy=policy)
        provider = DemoProvider(delay_ms=0)
        facade = RoutedProvider(provider, runtime, {}, demo_response)
        req = request([user()])
        await facade.complete(req)
        return vars(req), _strip_nondeterministic_event_fields(events)

    async def test_no_planner_key_vs_planner_disabled_full_request_payload_byte_identical(self):
        baseline_req, _baseline_events = await self._run(
            Policy(mode="off", model_routing={"start_model": SONNET})
        )
        disabled_req, _disabled_events = await self._run(
            Policy(
                mode="off",
                model_routing={"start_model": SONNET, "planner": {"enabled": False}},
            )
        )

        self.assertEqual(
            json.dumps(baseline_req, sort_keys=True, default=str),
            json.dumps(disabled_req, sort_keys=True, default=str),
            "planner-absent and planner-disabled runs must produce a byte-identical request",
        )

    async def test_no_planner_key_vs_planner_disabled_full_event_stream_byte_identical(self):
        _baseline_req, baseline_events = await self._run(
            Policy(mode="off", model_routing={"start_model": SONNET})
        )
        _disabled_req, disabled_events = await self._run(
            Policy(
                mode="off",
                model_routing={"start_model": SONNET, "planner": {"enabled": False}},
            )
        )

        self.assertEqual(
            json.dumps(baseline_events, sort_keys=True, default=str),
            json.dumps(disabled_events, sort_keys=True, default=str),
            "planner-absent and planner-disabled runs must emit a byte-identical event stream "
            "(modulo inherently non-deterministic ids/timestamps)",
        )


class PlannerOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_abstain_falls_back_to_start_model_unchanged(self):
        policy = planner_policy()
        _service, runtime, events = setup_service(policy=policy)
        # A host with no price/prior in DEFAULT_RATES/DEFAULT_PLANNER_PRIORS.
        provider = PricedProvider(default_model="totally-unpriced-model")
        facade = RoutedProvider(provider, runtime, {}, demo_response)
        req = request([user()])

        await facade.complete(req)

        self.assertEqual(req.model, SONNET)
        routed = [e for e in events if e["event"].endswith("model_routed")]
        self.assertEqual(routed[0]["data"]["reason_code"], "start_model")
        self.assertFalse(any(e["event"].endswith("turn_planned") for e in events))

    async def test_planner_chooses_candidate_and_emits_turn_planned(self):
        policy = planner_policy()
        _service, runtime, events = setup_service(policy=policy)
        provider = PricedProvider(default_model=OPUS, usage_by_call=[{"input_tokens": 60000}])
        facade = RoutedProvider(provider, runtime, {}, demo_response)
        req = request([user()])

        await facade.complete(req)

        self.assertEqual(req.model, SONNET)
        routed = [e for e in events if e["event"].endswith("model_routed")]
        self.assertEqual(routed[0]["data"]["reason_code"], "planner_balanced")
        self.assertEqual(routed[0]["data"]["requested_model"], SONNET)
        planned = [e for e in events if e["event"].endswith("turn_planned")]
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0]["data"]["choice"], SONNET)
        self.assertEqual(planned[0]["data"]["host_model"], OPUS)

    async def test_planner_chooses_host_applies_no_model_override(self):
        policy = planner_policy()
        _service, runtime, events = setup_service(policy=policy)
        # Opus warm with 80k cached beats a cold Sonnet -- see PlanTurnTests.
        # last_used_at is wall-clock time.time(), matching what the
        # orchestrator itself records (see RoutedProvider.complete).
        runtime.planner_state[OPUS] = {"last_used_at": time.time(), "cached_tokens": 80000}
        runtime.planner_last_ctx = 90000
        provider = PricedProvider(default_model=OPUS, usage_by_call=[{"input_tokens": 90000}])
        facade = RoutedProvider(provider, runtime, {}, demo_response)
        req = request([user()])

        await facade.complete(req)

        self.assertFalse(hasattr(req, "model"))
        routed = [e for e in events if e["event"].endswith("model_routed")]
        self.assertEqual(routed[0]["data"]["reason_code"], "planner_host")
        self.assertIsNone(routed[0]["data"]["requested_model"])
        planned = [e for e in events if e["event"].endswith("turn_planned")]
        self.assertEqual(planned[0]["data"]["choice"], OPUS)

    async def test_hard_turn_never_consults_planner(self):
        policy = Policy(
            mode="off",
            model_routing={
                "start_model": SONNET,
                "start_policy": "rules",
                "complex_min_prompt_chars": 20,
                "planner": {"enabled": True},
            },
        )
        service, runtime, events = setup_service(policy=policy)
        provider = PricedProvider(default_model=OPUS, usage_by_call=[{"input_tokens": 75000}])
        facade = RoutedProvider(provider, runtime, {}, demo_response)
        req = request([user("A" * 60)])  # exceeds complex_min_prompt_chars

        await facade.complete(req)

        self.assertFalse(hasattr(req, "model"))
        routed = [e for e in events if e["event"].endswith("model_routed")]
        self.assertEqual(routed[0]["data"]["reason_code"], "start_strong")
        self.assertFalse(service.turn.planner_decided)
        self.assertFalse(any(e["event"].endswith("turn_planned") for e in events))

    async def test_host_pinned_turn_never_consults_planner(self):
        policy = planner_policy()
        service, runtime, events = setup_service(policy=policy)
        provider = PricedProvider(default_model=OPUS, usage_by_call=[{"input_tokens": 60000}])
        facade = RoutedProvider(provider, runtime, {}, demo_response)
        req = request([user()], model="host-pinned-model")

        await facade.complete(req)

        self.assertEqual(req.model, "host-pinned-model")
        routed = [e for e in events if e["event"].endswith("model_routed")]
        self.assertEqual(routed[0]["data"]["reason_code"], "host_pinned")
        self.assertFalse(service.turn.planner_decided)
        self.assertFalse(any(e["event"].endswith("turn_planned") for e in events))

    async def test_decided_once_per_turn(self):
        policy = planner_policy()
        _service, runtime, events = setup_service(policy=policy)
        provider = PricedProvider(
            default_model=OPUS,
            usage_by_call=[{"input_tokens": 60000}, {"input_tokens": 5000}],
        )
        facade = RoutedProvider(provider, runtime, {}, demo_response)

        req_a = request([user()])
        req_b = request([user()])
        await facade.complete(req_a)
        await facade.complete(req_b)

        planned = [e for e in events if e["event"].endswith("turn_planned")]
        self.assertEqual(len(planned), 1)  # decided once, not recomputed on 2nd call
        routed = [e for e in events if e["event"].endswith("model_routed")]
        self.assertEqual(len(routed), 2)
        self.assertEqual(routed[0]["data"]["requested_model"], routed[1]["data"]["requested_model"])
        self.assertEqual(req_a.model, req_b.model)

    async def test_cache_state_updated_from_provider_response(self):
        """Total prompt size is input_tokens + cache_write_tokens -- NOT
        + cache_read_tokens, which the installed Anthropic provider already
        folds into input_tokens (see the comment on the cache-state update
        in orchestrator.RoutedProvider.complete). Uses the exact numbers
        from a real live-smoke receipt: a call reporting
        {input: 75256, cache_read: 75254, cache_write: 1062} must be
        recorded as total prompt 76318 -- which is exactly what the VERY
        NEXT real call in that same smoke reported as its own input_tokens
        (75256 + 1062 == 76318), confirming the formula against ground
        truth, not just self-consistency."""
        policy = planner_policy()
        _service, runtime, _events = setup_service(policy=policy)
        provider = PricedProvider(
            default_model=OPUS,
            usage_by_call=[
                {"input_tokens": 75256, "cache_read_tokens": 75254, "cache_write_tokens": 1062},
            ],
        )
        facade = RoutedProvider(provider, runtime, {}, demo_response)

        await facade.complete(request([user()]))

        # Sonnet was chosen (fresh session, balanced) -- its cache state now reflects usage.
        entry = runtime.planner_state[SONNET]
        self.assertEqual(entry["cached_tokens"], 76318)
        self.assertIsInstance(entry["last_used_at"], float)
        self.assertEqual(runtime.planner_last_ctx, 76318)

    async def test_cache_state_second_receipt_pair_no_cache_read(self):
        """Second real-smoke pair: a fresh-cache-write-only first call
        (input: 2, cache_read: None, cache_write: 68569) is followed by a
        real second call reporting input_tokens 68571 -- exactly
        2 + 68569, confirming the same formula when cache_read is absent
        (None) rather than zero."""
        policy = planner_policy()
        _service, runtime, _events = setup_service(policy=policy)
        provider = PricedProvider(
            default_model=OPUS,
            usage_by_call=[{"input_tokens": 2, "cache_write_tokens": 68569}],
        )
        facade = RoutedProvider(provider, runtime, {}, demo_response)

        await facade.complete(request([user()]))

        entry = runtime.planner_state[SONNET]
        self.assertEqual(entry["cached_tokens"], 68571)
        self.assertEqual(runtime.planner_last_ctx, 68571)

    async def test_planner_host_suppresses_easy_turn_shaping(self):
        """HC12 shaping must not fire on a request the planner routed to the
        host, even though turn.start_tier still reads "cheap"."""
        policy = Policy(
            mode="off",
            model_routing={
                "start_model": SONNET,
                "planner": {"enabled": True},
                "easy_turn_guidance": "Batch your tool calls.",
            },
        )
        _service, runtime, events = setup_service(policy=policy)
        runtime.planner_state[OPUS] = {"last_used_at": time.time(), "cached_tokens": 80000}
        runtime.planner_last_ctx = 90000
        provider = PricedProvider(default_model=OPUS, usage_by_call=[{"input_tokens": 90000}])
        facade = RoutedProvider(provider, runtime, {}, demo_response)

        await facade.complete(request([{"role": "system", "content": "sys"}, user()]))

        self.assertFalse(any(e["event"].endswith("easy_turn_shaped") for e in events))
        # The provider received the ORIGINAL request (no guidance appended).
        self.assertEqual(provider.requests[0].messages[0]["content"], "sys")


class NoServedModelPricedProvider(PricedProvider):
    """PricedProvider variant whose response NEVER reports a served model
    (blank `model`, no `metadata` attribute at all) -- exercises the
    `served in (None, "", "provider-default")` fallback branch in the
    post-response cache-update block (orchestrator.py, ~lines 1314-1322).
    No existing test double behaved this way: PricedProvider/DemoProvider
    always set response.model to the model that actually served the
    request, so that fallback branch was previously untested. See
    REVIEW.md Area 2 ("coverage gap identified but left open")."""

    async def complete(self, request, **kwargs):
        response = await super().complete(request, **kwargs)
        response.model = ""
        return response


class CacheKeyRealModelIdRegressionTests(unittest.IsolatedAsyncioTestCase):
    """Closes the coverage gap REVIEW.md Area 2 explicitly left open, and
    turns Area 1's "correct by construction" claim into an actual
    regression test rather than a code-reading conclusion. Together these
    three tests prove: (1) the untested "provider-default" placeholder
    fallback branch keys the cache by a REAL model id, never the literal
    placeholder; (2) a `ui.model_override` ("--model" style) pick, with the
    planner ENABLED, still bypasses the planner branch entirely; and (3) a
    mid-session `provider.default_model` change is never served from a
    stale cache key on the very next turn."""

    async def test_no_served_model_in_response_falls_back_to_real_provider_default(self):
        """Planner enabled, planner chooses the HOST (not a candidate) --
        same setup as test_planner_chooses_host_applies_no_model_override --
        so `model` is never reassigned away from the "provider-default"
        placeholder set at request-construction time. The response reports
        NO served model at all (empty `model`, no `metadata`). The
        cache-update block must key planner_state by the actual
        provider.default_model at call time, never by "provider-default"
        (or None/empty)."""
        policy = planner_policy()
        _service, runtime, events = setup_service(policy=policy)
        runtime.planner_state[OPUS] = {"last_used_at": time.time(), "cached_tokens": 80000}
        runtime.planner_last_ctx = 90000
        provider = NoServedModelPricedProvider(default_model=OPUS, usage_by_call=[{"input_tokens": 90000}])
        facade = RoutedProvider(provider, runtime, {}, demo_response)
        req = request([user()])

        await facade.complete(req)

        routed = [e["data"] for e in events if e["event"].endswith("model_routed")]
        self.assertEqual(routed[0]["reason_code"], "planner_host")  # confirms the host path fired
        self.assertNotIn("provider-default", runtime.planner_state)
        self.assertNotIn(None, runtime.planner_state)
        self.assertNotIn("", runtime.planner_state)
        entry = runtime.planner_state[OPUS]
        self.assertEqual(entry["cached_tokens"], 90000)
        self.assertIsInstance(entry["last_used_at"], float)

    async def test_ui_model_override_bypasses_planner_with_planner_enabled(self):
        """A `ui.model_override` session-state marker (how amplifier-runtime
        represents an in-session model pick / a `--model`-style CLI
        override) must force strong_turn=True and bypass the planner
        branch entirely, even with the turn planner ENABLED. The existing
        test_ui_model_pick_is_respected (test_orchestrator_primary.py)
        proves this for the difficulty router alone but never turns the
        planner on, so it does not prove the planner is actually
        bypassed -- this test does."""
        policy = planner_policy()
        service, runtime, events = setup_service(policy=policy)
        service.coordinator.session_state = {
            "ui.model_override": {"provider": "anthropic", "model": OPUS}
        }
        provider = PricedProvider(default_model=SONNET, usage_by_call=[{"input_tokens": 60000}])
        facade = RoutedProvider(provider, runtime, {}, demo_response)
        req = request([user()])

        await facade.complete(req)

        self.assertEqual(service.turn.start_tier, "strong")
        self.assertFalse(service.turn.planner_decided)          # planner branch never ran
        self.assertFalse(any(e["event"].endswith("turn_planned") for e in events))
        judged = [e["data"] for e in events if e["event"].endswith("difficulty_judged")]
        self.assertEqual(judged[0]["reason_code"], "user_model_strong")
        self.assertEqual(judged[0]["model"], OPUS)
        routed = [e["data"] for e in events if e["event"].endswith("model_routed")]
        self.assertEqual(routed[0]["reason_code"], "start_strong")
        self.assertIsNone(getattr(req, "model", None))          # untouched: the override IS the served model
        # The cache-update block (planner enabled) still runs on every slow
        # call regardless of routing branch -- keyed by the real served
        # model (the provider's own default), never the placeholder.
        self.assertIn(SONNET, runtime.planner_state)
        self.assertNotIn("provider-default", runtime.planner_state)

    async def test_mid_session_default_model_change_keys_cache_by_new_model_not_stale_one(self):
        """Two turns of the SAME session, planner ENABLED throughout. Turn 1
        runs the planner normally (fresh session, Opus host, balanced picks
        Sonnet). Mid-session -- without any explicit per-request override --
        `provider.default_model` changes to a brand-new model. Turn 2 must
        (a) force strong_turn via user_model_strong (Area 1), bypassing the
        planner branch, and (b) have its post-response cache-update entry
        keyed by the NEW default_model, never by turn 1's host/choice --
        proving planner cache state is never stale across a mid-session
        model change."""
        policy = planner_policy()
        service, runtime, events = setup_service(policy=policy)
        provider = PricedProvider(
            default_model=OPUS,
            usage_by_call=[{"input_tokens": 60000}, {"input_tokens": 40000}],
        )
        facade = RoutedProvider(provider, runtime, {}, demo_response)

        first = request([user()])
        await facade.complete(first)
        self.assertEqual(first.model, SONNET)          # fresh/cold: balanced picks Sonnet over Opus
        self.assertIn(SONNET, runtime.planner_state)

        provider.default_model = FABLE                 # user switched models mid-session
        service.turn = TurnState("t2")
        second = request([user()])
        await facade.complete(second)

        self.assertIsNone(getattr(second, "model", None))   # host-pinned to the NEW model, untouched
        judged = [e["data"] for e in events if e["event"].endswith("difficulty_judged")]
        self.assertEqual([j["reason_code"] for j in judged], ["user_model_strong"])
        self.assertFalse(service.turn.planner_decided)      # planner never ran for turn 2
        routed = [e["data"] for e in events if e["event"].endswith("model_routed")]
        self.assertEqual(routed[-1]["reason_code"], "start_strong")
        self.assertIn(FABLE, runtime.planner_state)
        self.assertEqual(runtime.planner_state[FABLE]["cached_tokens"], 40000)
        # Turn 1's own cache entry (Sonnet) must be untouched by turn 2.
        self.assertEqual(runtime.planner_state[SONNET]["cached_tokens"], 60000)


class PlannerMultiTurnHandComputedTests(unittest.IsolatedAsyncioTestCase):
    async def test_opus_host_four_turn_sequence(self):
        """Hand-computed sequence on an Opus host, balanced objective,
        Sonnet as the only candidate (default DEFAULT_PLANNER_PRIORS and
        DEFAULT_PLANNER_CONTINUE_PROBABILITY), a root (non-sub) session:

        - turn1 (easy, fresh, ctx=60k, session_kind=first_turn, p=0.25):
          both cold. Sonnet's BASE numbers are still cheaper and faster
          (0.28941188 USD / 10.05s vs Opus's 0.3426 USD / 12.72s -- each
          model's own measured output_tokens_per_call now applies: Opus
          180, Sonnet 165) -- but with the one-step lookahead, choosing
          Sonnet risks a full cold rewrite of Opus's cache on some later
          turn:
              lookahead_cost = 0.25 * 60000 * (5.0 - 0.2) / 1e6 = 0.072
          Sonnet's TOTAL cost (0.28941188 + 0.072 = 0.36141188) now
          EXCEEDS balanced's budget (Opus's own cost * 1.05 =
          0.3426 * 1.05 = 0.35973) -- Sonnet is no longer eligible ->
          chosen: OPUS (host, reason_code planner_host). This is the fix
          for the live-smoke regression: the OLD myopic (no-lookahead)
          choice was SONNET here, which then paid a full cold write on
          Opus anyway at turn2 (a hard turn) -- TWO cold writes instead
          of one.
        - turn2 (hard, ctx=75k): the difficulty router sends it straight to
          the host (Opus); the planner is never consulted. This warms
          Opus's cache to 75k tokens. planner_state is now non-empty, so
          every later turn's session_kind is later_turn (p=0.8).
        - turn3 (easy, ctx=90k, later_turn, p=0.8): Opus is warm
          (cold=15k, cost 0.1476) while Sonnet is cold at 90k (base cost
          0.42913688 PLUS lookahead 0.8*15000*4.8/1e6=0.0576 = 0.48673688
          total) -- outside balanced's 5% cost-tolerance budget
          (0.1476 * 1.05 = 0.1550) either way -- so only Opus is eligible
          -> chosen: OPUS (host, via the planner, reason_code planner_host).
        - turn4 (easy, ctx=100k, later_turn, p=0.8): Opus stays warm
          (cold=10k, cost 0.1306); Sonnet is still cold at 100k (base
          0.47571188 + lookahead 0.8*10000*4.8/1e6=0.0384 = 0.51411188
          total), again outside budget (0.1306 * 1.05 = 0.13713) ->
          chosen: OPUS again.

        Net effect of the fix: Opus is chosen on EVERY turn of this
        sequence -- the host never pays more than the one cold write it
        would have paid anyway, and the planner never creates a second one.
        """
        policy = Policy(
            mode="off",
            model_routing={
                "start_model": SONNET,
                "start_policy": "rules",
                "complex_min_prompt_chars": 20,
                "planner": {"enabled": True, "objective": "balanced"},
            },
        )
        service, runtime, events = setup_service(policy=policy)
        provider = PricedProvider(
            default_model=OPUS,
            usage_by_call=[
                {"input_tokens": 60000},  # turn1
                {"input_tokens": 75000},  # turn2 (hard, host)
                {"input_tokens": 90000},  # turn3
                {"input_tokens": 100000},  # turn4
            ],
        )
        facade = RoutedProvider(provider, runtime, {}, demo_response)

        # turn1: easy, fresh ctx 60k. An empty new-user-message keeps
        # _estimate_ctx's "+ this turn's new user message" term at exactly
        # 0, so ctx stays precisely at the hand-computed value below --
        # the incremental-ctx addition itself has its own dedicated test
        # (EstimateCtxTests). Root session (no parent_session_id), nothing
        # recorded yet -> session_kind == first_turn.
        service.turn = TurnState("t1")
        runtime.planner_last_ctx = 60000
        req1 = request([user("")])
        await facade.complete(req1)
        self.assertFalse(hasattr(req1, "model"))  # host chosen -- no override

        # turn2: hard (long prompt) ctx 75k -- runs on the host, untouched.
        service.turn = TurnState("t2")
        req2 = request([user("A" * 60)])
        await facade.complete(req2)
        self.assertFalse(hasattr(req2, "model"))

        # turn3: easy ctx 90k -- Opus (warm) beats a cold Sonnet.
        service.turn = TurnState("t3")
        runtime.planner_last_ctx = 90000
        req3 = request([user("")])
        await facade.complete(req3)
        self.assertFalse(hasattr(req3, "model"))  # host chosen -- no override

        # turn4: easy ctx 100k -- Opus stays warm enough to win again.
        service.turn = TurnState("t4")
        runtime.planner_last_ctx = 100000
        req4 = request([user("")])
        await facade.complete(req4)
        self.assertFalse(hasattr(req4, "model"))

        routed = [e for e in events if e["event"].endswith("model_routed")]
        self.assertEqual(
            [r["data"]["reason_code"] for r in routed],
            ["planner_host", "start_strong", "planner_host", "planner_host"],
        )
        planned = [e for e in events if e["event"].endswith("turn_planned")]
        self.assertEqual(len(planned), 3)  # turn2 never invokes the planner
        self.assertEqual([p["data"]["choice"] for p in planned], [OPUS, OPUS, OPUS])
        self.assertEqual(
            [p["data"]["session_kind"] for p in planned],
            ["first_turn", "later_turn", "later_turn"],
        )
        self.assertEqual([p["data"]["p_continue"] for p in planned], [0.25, 0.8, 0.8])
        # Base costs (the "cost" field) are unaffected by lookahead --
        # only the separate lookahead_cost/lookahead_time fields are new.
        self.assertAlmostEqual(planned[0]["data"]["options"][1]["cost"], 0.28941188, places=5)
        self.assertAlmostEqual(planned[0]["data"]["options"][1]["lookahead_cost"], 0.072, places=4)
        self.assertAlmostEqual(planned[1]["data"]["options"][0]["cost"], 0.1476, places=4)
        self.assertAlmostEqual(planned[2]["data"]["options"][0]["cost"], 0.1306, places=4)
        # Host options never carry a lookahead penalty.
        for p in planned:
            self.assertEqual(p["data"]["options"][0]["lookahead_cost"], 0.0)
            self.assertEqual(p["data"]["options"][0]["lookahead_time"], 0.0)


# ---------------------------------------------------------------------------
# ctx estimation (_estimate_ctx / _request_chars)
# ---------------------------------------------------------------------------


class EstimateCtxTests(unittest.TestCase):
    def test_persisted_last_ctx_plus_new_user_message(self):
        """Prefer the persisted/known last_ctx, adding only the NEW user
        message's own size -- not re-measuring the whole conversation."""
        runtime = Runtime(DecisionService(Policy(mode="off"), ScriptedBackend(delay_ms=0),
                                           Emitter("s"), DemoCoordinator(), []))
        runtime.planner_last_ctx = 76318
        req = request([user("x" * 400)])  # 400 chars -> 100 tokens

        self.assertEqual(_estimate_ctx(req, runtime), 76318 + 100)

    def test_persisted_last_ctx_with_empty_new_message_is_unchanged(self):
        runtime = Runtime(DecisionService(Policy(mode="off"), ScriptedBackend(delay_ms=0),
                                           Emitter("s"), DemoCoordinator(), []))
        runtime.planner_last_ctx = 60000
        self.assertEqual(_estimate_ctx(request([user("")]), runtime), 60000)

    def test_cold_start_falls_back_to_full_request_chars(self):
        runtime = Runtime(DecisionService(Policy(mode="off"), ScriptedBackend(delay_ms=0),
                                           Emitter("s"), DemoCoordinator(), []))
        self.assertIsNone(runtime.planner_last_ctx)
        req = request([user("x" * 4000)])

        self.assertEqual(_estimate_ctx(req, runtime), _request_chars(req) // 4)
        self.assertGreater(_estimate_ctx(req, runtime), 0)

    def test_request_chars_includes_tool_results_and_tool_use_arguments(self):
        """The earlier version only counted a content block's ``text``
        field, silently dropping tool_result content and tool_use
        arguments -- often the largest part of a real prompt (file reads,
        command output) -- which measured ~2.6x against real receipts.
        Serializing the full messages/tools structure closes that gap."""
        bare = [user("short")]
        with_tool_traffic = [
            user("short"),
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "bash",
                     "input": {"command": "x" * 5000}},
                ],
            },
            {
                "role": "tool",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "y" * 5000},
                ],
            },
        ]

        chars_bare = _request_chars(request(bare))
        chars_with_tools = _request_chars(request(with_tool_traffic))

        self.assertGreater(chars_with_tools, chars_bare + 9000)

    def test_request_chars_never_raises_on_malformed_request(self):
        # No messages/tools attributes at all -- just the fixed overhead of
        # the serialized (empty) payload shape, never an exception.
        empty_payload_chars = len('{"messages":[],"tools":[]}')
        self.assertEqual(_request_chars(NS()), empty_payload_chars)
        self.assertEqual(_request_chars(object()), empty_payload_chars)

    def test_request_chars_never_raises_on_unserializable_content(self):
        """A content object jsonable() cannot represent (no model_dump,
        not a JSON primitive/container) is simply dropped -- never raises."""
        class Unserializable:
            pass

        req = request([{"role": "user", "content": Unserializable()}])
        self.assertIsInstance(_request_chars(req), int)


class ContextCompactionResetTests(unittest.TestCase):
    """A real "context:compaction" native event means the conversation just
    shrank. If Runtime.planner_last_ctx (the persisted pre-compaction total
    prompt size) is left untouched, every subsequent _estimate_ctx() call
    keeps adding new message sizes on top of the stale, too-large baseline --
    silently and permanently overestimating cost/cache-warmth for the rest of
    the session. observer.reset_planner_ctx_on_compaction is the hook that
    must invalidate that baseline."""

    def _runtime(self):
        return Runtime(DecisionService(Policy(mode="off"), ScriptedBackend(delay_ms=0),
                                        Emitter("s"), DemoCoordinator(), []))

    def test_compaction_event_resets_stale_planner_last_ctx(self):
        from amplifier_fast_decisions import observer

        runtime = self._runtime()
        runtime.planner_last_ctx = 150000  # stale pre-compaction baseline

        observer.reset_planner_ctx_on_compaction(runtime, "context:compaction")

        self.assertIsNone(runtime.planner_last_ctx)

    def test_compaction_reset_makes_next_estimate_fall_back_to_cold_start(self):
        from amplifier_fast_decisions import observer

        runtime = self._runtime()
        runtime.planner_last_ctx = 150000
        req = request([user("x" * 4000)])

        observer.reset_planner_ctx_on_compaction(runtime, "context:compaction")

        # Post-compaction: must NOT be 150000 + new_message_chars//4 (stale
        # baseline compounding); must fall back to the fresh cold-start
        # estimate, exactly like a brand new session.
        self.assertEqual(_estimate_ctx(req, runtime), _request_chars(req) // 4)

    def test_unrelated_events_do_not_reset_planner_last_ctx(self):
        from amplifier_fast_decisions import observer

        runtime = self._runtime()
        runtime.planner_last_ctx = 150000

        for event in ("provider:request", "tool:pre", "tool:post",
                      "execution:start", "execution:end", "session:end"):
            observer.reset_planner_ctx_on_compaction(runtime, event)

        self.assertEqual(runtime.planner_last_ctx, 150000)


# ---------------------------------------------------------------------------
# Cross-process persistence (Runtime.ensure_planner_state_loaded /
# persist_planner_state, runtime.load_planner_state / save_planner_state)
# ---------------------------------------------------------------------------


class PlannerPersistenceTests(unittest.TestCase):
    def test_missing_events_dir_or_session_id_is_a_no_op(self):
        self.assertEqual(load_planner_state(None, "s"), ({}, None))
        self.assertEqual(load_planner_state("/tmp/whatever", None), ({}, None))
        save_planner_state(None, "s", {"m": {"last_used_at": 1.0, "cached_tokens": 2}}, 3)  # no raise

    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_planner_state(tmp, "no-such-session"), ({}, None))

    def test_corrupted_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "planner-state" / "sess.json"
            path.parent.mkdir(parents=True)
            path.write_text("{not valid json", encoding="utf-8")
            self.assertEqual(load_planner_state(tmp, "sess"), ({}, None))

    def test_round_trip_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = {OPUS: {"last_used_at": 12345.5, "cached_tokens": 90000}}
            save_planner_state(tmp, "sess-1", state, 90000)

            loaded_state, loaded_ctx = load_planner_state(tmp, "sess-1")

            self.assertEqual(loaded_state, state)
            self.assertEqual(loaded_ctx, 90000)

    def test_session_id_is_sanitized_for_the_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            save_planner_state(tmp, "weird/session:id", {}, None)
            files = list((Path(tmp) / "planner-state").glob("*.json"))
            self.assertEqual(len(files), 1)

    def test_malformed_entries_are_dropped_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "planner-state" / "sess.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "models": {
                    OPUS: {"last_used_at": 1.0, "cached_tokens": 100},
                    "bad-1": {"last_used_at": "not-a-number", "cached_tokens": 1},
                    "bad-2": {"last_used_at": 1.0, "cached_tokens": "not-an-int"},
                    "bad-3": "not-a-dict",
                },
                "last_ctx": 100,
            }), encoding="utf-8")

            state, ctx = load_planner_state(tmp, "sess")

            self.assertEqual(state, {OPUS: {"last_used_at": 1.0, "cached_tokens": 100}})
            self.assertEqual(ctx, 100)


class PlannerPersistenceOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def _service_and_runtime(self, tmp, session_id, *, policy=None):
        events = []
        coordinator = DemoCoordinator()
        policy = policy or planner_policy()
        emitter = Emitter(coordinator.session_id, callback=events.append)
        service = DecisionService(policy, ScriptedBackend(delay_ms=0), emitter, coordinator, [])
        service.turn = TurnState("t")
        runtime = Runtime(service, events_dir=tmp, session_id=session_id)
        return service, runtime, events

    async def test_state_survives_a_simulated_process_restart(self):
        """Each turn is a fresh process under `amplifier run --resume` --
        simulated here by constructing a brand-new Runtime (same events_dir
        + session_id) for the second turn instead of reusing the first
        Runtime object. Whichever model process 1 actually served must be
        WARM in process 2's plan; a model neither process ever served must
        stay cold -- this is the whole point of persistence, not merely
        "no exception"."""
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "session-abc"

            # "Process 1", turn 1.
            _s1, runtime1, events1 = self._service_and_runtime(tmp, session_id)
            provider1 = PricedProvider(
                default_model=OPUS,
                usage_by_call=[{"input_tokens": 75256, "cache_write_tokens": 1062}],
            )
            facade1 = RoutedProvider(provider1, runtime1, {}, demo_response)
            await facade1.complete(request([user()]))
            planned1 = [e for e in events1 if e["event"].endswith("turn_planned")]
            served_model = planned1[0]["data"]["choice"]  # whatever the planner actually chose

            # "Process 2", turn 2: a BRAND NEW Runtime -- nothing shared in
            # memory with process 1 except the same events_dir/session_id.
            _s2, runtime2, events2 = self._service_and_runtime(tmp, session_id)
            self.assertEqual(runtime2.planner_state, {})  # fresh process, empty in-memory
            provider2 = PricedProvider(default_model=OPUS, usage_by_call=[{"input_tokens": 76318}])
            facade2 = RoutedProvider(provider2, runtime2, {}, demo_response)

            await facade2.complete(request([user("")]))  # empty: ctx == persisted last_ctx exactly

            planned2 = [e for e in events2 if e["event"].endswith("turn_planned")]
            self.assertEqual(len(planned2), 1)
            # ctx = persisted last_ctx (76318) + this turn's empty new user message.
            self.assertEqual(planned2[0]["data"]["ctx"], 76318)
            options_by_model = {o["model"]: o for o in planned2[0]["data"]["options"]}
            self.assertTrue(options_by_model[served_model]["warm"])
            other_model = OPUS if served_model != OPUS else SONNET
            if other_model in options_by_model:  # never used by either process
                self.assertFalse(options_by_model[other_model]["warm"])

    async def test_ttl_still_applies_across_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "session-ttl"
            # Seed persisted state directly with an old last_used_at (wall
            # clock, far enough in the past to exceed the default 300s TTL).
            old_state = {OPUS: {"last_used_at": time.time() - 400, "cached_tokens": 80000}}
            save_planner_state(tmp, session_id, old_state, 80000)

            _s2, runtime2, events2 = self._service_and_runtime(tmp, session_id)
            provider2 = PricedProvider(default_model=OPUS, usage_by_call=[{"input_tokens": 90000}])
            facade2 = RoutedProvider(provider2, runtime2, {}, demo_response)
            await facade2.complete(request([user()]))

            planned = [e for e in events2 if e["event"].endswith("turn_planned")]
            opus_option = next(o for o in planned[0]["data"]["options"] if o["model"] == OPUS)
            self.assertFalse(opus_option["warm"])  # TTL expired even though it was loaded from disk

    async def test_corrupted_persisted_file_falls_back_to_in_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "session-corrupt"
            path = Path(tmp) / "planner-state" / f"{session_id}.json"
            path.parent.mkdir(parents=True)
            path.write_text("{ this is not json", encoding="utf-8")

            _service, runtime, events = self._service_and_runtime(tmp, session_id)
            provider = PricedProvider(default_model=OPUS, usage_by_call=[{"input_tokens": 60000}])
            facade = RoutedProvider(provider, runtime, {}, demo_response)

            await facade.complete(request([user()]))  # must not raise

            planned = [e for e in events if e["event"].endswith("turn_planned")]
            self.assertEqual(len(planned), 1)

    async def test_state_persisted_to_disk_after_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "session-persist"
            _service, runtime, _events = self._service_and_runtime(tmp, session_id)
            provider = PricedProvider(
                default_model=OPUS,
                usage_by_call=[{"input_tokens": 75256, "cache_read_tokens": 75254, "cache_write_tokens": 1062}],
            )
            facade = RoutedProvider(provider, runtime, {}, demo_response)

            await facade.complete(request([user()]))

            on_disk_state, on_disk_ctx = load_planner_state(tmp, session_id)
            self.assertEqual(on_disk_ctx, 76318)
            self.assertEqual(on_disk_state[SONNET]["cached_tokens"], 76318)


# ---------------------------------------------------------------------------
# Lookahead (planner.plan_turn's one-step lookahead term)
# ---------------------------------------------------------------------------


class LookaheadTests(unittest.TestCase):
    def test_root_first_turn_p025_prefers_host_over_myopically_cheaper_candidate(self):
        """Opus host, fresh ROOT session (session kind: first_turn,
        p_continue=0.25), ctx=70000, both cold. Sonnet's BASE numbers are
        cheaper and faster (cost 0.33598688 vs Opus's 0.3976 -- Opus's own
        prior now measures 180 output tokens/call, Sonnet's 165; time
        10.115 vs 12.915), but its lookahead penalty --
        ``0.25 * 70000 * (5.0 - 0.2) / 1e6 == 0.084`` -- pushes its TOTAL
        cost to 0.41998688, which exceeds balanced's budget
        (``0.3976 * 1.05 == 0.41748``). Sonnet is therefore not eligible
        -> chosen: OPUS (the host)."""
        plan = planner.plan_turn(
            OPUS, [SONNET], {}, 70000, 0.0, config(), DEFAULT_RATES, p_continue=0.25
        )
        by_model = {o["model"]: o for o in plan["options"]}
        self.assertAlmostEqual(by_model[SONNET]["cost"], 0.33598688, places=5)
        self.assertAlmostEqual(by_model[SONNET]["lookahead_cost"], 0.084, places=5)
        self.assertAlmostEqual(by_model[SONNET]["cost"] + by_model[SONNET]["lookahead_cost"], 0.41998688, places=5)
        self.assertAlmostEqual(by_model[OPUS]["cost"] * 1.05, 0.41748, places=5)
        self.assertEqual(by_model[OPUS]["lookahead_cost"], 0.0)
        self.assertEqual(plan["choice"], OPUS)

    def test_sub_session_p006_still_prefers_the_cheaper_candidate(self):
        """Same inputs as above but sub_session's much lower p_continue
        (0.06) keeps Sonnet's lookahead penalty small --
        ``0.06 * 70000 * 4.8 / 1e6 == 0.02016`` -- so its total cost
        (0.33598688 + 0.02016 == 0.35614688) stays under budget
        (0.41748), and it wins on time (10.1969s total vs Opus's
        12.915s)."""
        plan = planner.plan_turn(
            OPUS, [SONNET], {}, 70000, 0.0, config(), DEFAULT_RATES, p_continue=0.06
        )
        by_model = {o["model"]: o for o in plan["options"]}
        self.assertAlmostEqual(by_model[SONNET]["lookahead_cost"], 0.02016, places=5)
        self.assertLess(
            by_model[SONNET]["cost"] + by_model[SONNET]["lookahead_cost"],
            by_model[OPUS]["cost"] * 1.05,
        )
        self.assertEqual(plan["choice"], SONNET)

    def test_later_turn_with_host_already_warm_lookahead_is_zero(self):
        """Opus host, LATER turn, Opus already warm (cold=10000 this
        turn), Sonnet cold at 100000, p_continue=0.8 (later_turn).
        Lookahead uses the HOST's cold THIS turn (10000), not Sonnet's:
        ``0.8 * 10000 * 4.8 / 1e6 == 0.0384``. Still nowhere near enough
        to make Sonnet competitive against a warm Opus -> chosen: OPUS."""
        state = {OPUS: {"last_used_at": 0.0, "cached_tokens": 90000}}
        plan = planner.plan_turn(
            OPUS, [SONNET], state, 100000, 10.0, config(), DEFAULT_RATES, p_continue=0.8
        )
        by_model = {o["model"]: o for o in plan["options"]}
        self.assertTrue(by_model[OPUS]["warm"])
        self.assertAlmostEqual(by_model[SONNET]["lookahead_cost"], 0.0384, places=4)
        self.assertEqual(plan["choice"], OPUS)

    def test_fable_host_later_turn_both_warm_lookahead_vanishes(self):
        """Fable host, LATER turn, BOTH Fable and Sonnet already warm
        (cold=0 for both) at ctx=30000, p_continue=0.8. Because the HOST's
        own cold this turn is 0, every lookahead term is exactly 0
        regardless of p_continue -- there is no stale cache to eventually
        pay for. Fable's own prior now measures 260 output tokens/call
        (vs Sonnet's 165), so Fable's cost is 0.07175, not the
        global-default-output-tokens 0.056 an earlier version of this
        test measured. Sonnet's (unpenalized) cost 0.04618688 is under
        budget (0.07175 * 1.05 == 0.0753375) and its time (9.66s) beats
        Fable's (18.9s) -> chosen: SONNET."""
        state = {
            FABLE: {"last_used_at": 0.0, "cached_tokens": 40000},
            SONNET: {"last_used_at": 0.0, "cached_tokens": 35000},
        }
        plan = planner.plan_turn(
            FABLE, [SONNET], state, 30000, 10.0, config(), DEFAULT_RATES, p_continue=0.8
        )
        by_model = {o["model"]: o for o in plan["options"]}
        self.assertEqual(by_model[FABLE]["cold"], 0)
        self.assertEqual(by_model[SONNET]["lookahead_cost"], 0.0)
        self.assertEqual(by_model[SONNET]["lookahead_time"], 0.0)
        self.assertAlmostEqual(by_model[FABLE]["cost"], 0.07175, places=5)
        self.assertAlmostEqual(by_model[SONNET]["cost"], 0.04618688, places=5)
        self.assertEqual(plan["choice"], SONNET)

    def test_default_p_continue_is_zero_preserves_pre_lookahead_behavior(self):
        """Omitting p_continue entirely (backward-compatible default 0.0)
        reproduces the pre-lookahead choice for the very first documented
        scenario (fresh session, both cold -> Sonnet)."""
        plan = planner.plan_turn(OPUS, [SONNET], {}, 60000, 0.0, config(), DEFAULT_RATES)
        self.assertEqual(plan["choice"], SONNET)
        by_model = {o["model"]: o for o in plan["options"]}
        self.assertEqual(by_model[SONNET]["lookahead_cost"], 0.0)

    def test_host_option_always_carries_zero_lookahead(self):
        for p in (0.0, 0.06, 0.25, 0.8, 1.0):
            plan = planner.plan_turn(OPUS, [SONNET], {}, 60000, 0.0, config(), DEFAULT_RATES, p_continue=p)
            host_option = next(o for o in plan["options"] if o["model"] == OPUS)
            self.assertEqual(host_option["lookahead_cost"], 0.0)
            self.assertEqual(host_option["lookahead_time"], 0.0)


# ---------------------------------------------------------------------------
# continue_probability config validation + effective_planner_config merge
# ---------------------------------------------------------------------------


class ContinueProbabilityValidationTests(unittest.TestCase):
    def test_absent_is_valid_and_defaults_apply(self):
        merged = effective_planner_config({"start_model": SONNET, "planner": {"enabled": True}})
        self.assertEqual(merged["continue_probability"], DEFAULT_PLANNER_CONTINUE_PROBABILITY)

    def test_must_be_dict(self):
        with self.assertRaises(ValueError):
            Policy(
                model_routing={
                    "start_model": SONNET,
                    "planner": {"enabled": True, "continue_probability": 0.5},
                }
            )

    def test_unknown_key_raises(self):
        with self.assertRaises(ValueError):
            Policy(
                model_routing={
                    "start_model": SONNET,
                    "planner": {"enabled": True, "continue_probability": {"bogus": 0.5}},
                }
            )

    def test_out_of_range_value_raises(self):
        with self.assertRaises(ValueError):
            Policy(
                model_routing={
                    "start_model": SONNET,
                    "planner": {"enabled": True, "continue_probability": {"first_turn": 1.5}},
                }
            )
        with self.assertRaises(ValueError):
            Policy(
                model_routing={
                    "start_model": SONNET,
                    "planner": {"enabled": True, "continue_probability": {"later_turn": -0.1}},
                }
            )

    def test_individual_key_override_merges_with_defaults(self):
        merged = effective_planner_config({
            "start_model": SONNET,
            "planner": {"enabled": True, "continue_probability": {"first_turn": 0.9}},
        })
        self.assertEqual(merged["continue_probability"]["first_turn"], 0.9)
        self.assertEqual(
            merged["continue_probability"]["later_turn"],
            DEFAULT_PLANNER_CONTINUE_PROBABILITY["later_turn"],
        )

    def test_valid_full_override_accepted(self):
        Policy(
            model_routing={
                "start_model": SONNET,
                "planner": {
                    "enabled": True,
                    "continue_probability": {"sub_session": 0.0, "first_turn": 1.0, "later_turn": 1.0},
                },
            }
        )


# ---------------------------------------------------------------------------
# Session-kind detection (orchestrator._session_kind)
# ---------------------------------------------------------------------------


class SessionKindTests(unittest.TestCase):
    def _runtime(self, *, parent_session_id=None, session_id="root-session-abc"):
        return Runtime(
            DecisionService(Policy(mode="off"), ScriptedBackend(delay_ms=0), Emitter("s"), DemoCoordinator(), []),
            session_id=session_id,
            parent_session_id=parent_session_id,
        )

    def test_explicit_parent_id_wins(self):
        runtime = self._runtime(parent_session_id="root-abc", session_id="child-xyz")
        self.assertEqual(_session_kind(runtime), "sub_session")

    def test_naming_convention_fallback_when_no_explicit_parent(self):
        runtime = self._runtime(parent_session_id=None, session_id="a1b2c3_code-reviewer")
        self.assertEqual(_session_kind(runtime), "sub_session")

    def test_plain_uuid_like_session_id_is_not_a_sub_session(self):
        runtime = self._runtime(parent_session_id=None, session_id="a1b2c3d4e5f6")
        self.assertNotEqual(_session_kind(runtime), "sub_session")

    def test_root_session_first_turn_when_planner_state_empty(self):
        runtime = self._runtime()
        self.assertEqual(runtime.planner_state, {})
        self.assertEqual(_session_kind(runtime), "first_turn")

    def test_root_session_later_turn_once_planner_state_populated(self):
        runtime = self._runtime()
        runtime.planner_state[OPUS] = {"last_used_at": time.time(), "cached_tokens": 1000}
        self.assertEqual(_session_kind(runtime), "later_turn")


# ---------------------------------------------------------------------------
# Per-model output_tokens_per_call priors
# ---------------------------------------------------------------------------


class PerModelOutputTokensTests(unittest.TestCase):
    def test_prior_output_tokens_overrides_global_default(self):
        """Fable's own prior (260 output tokens/call) applies instead of
        the global config default (170) -- confirmed by comparing against
        a hand-built rates/priors pair that isolates the difference."""
        priors_without_override = {FABLE: {**DEFAULT_PLANNER_PRIORS[FABLE]}}
        del priors_without_override[FABLE]["output_tokens_per_call"]
        cfg_without = config(priors=priors_without_override, output_tokens_per_call=170)
        cfg_with = config()  # DEFAULT_PLANNER_PRIORS, Fable's own 260

        plan_without = planner.plan_turn(FABLE, [], {}, 30000, 0.0, cfg_without, DEFAULT_RATES)
        plan_with = planner.plan_turn(FABLE, [], {}, 30000, 0.0, cfg_with, DEFAULT_RATES)

        fable_without = plan_without["options"][0]
        fable_with = plan_with["options"][0]
        self.assertGreater(fable_with["cost"], fable_without["cost"])

    def test_model_without_its_own_output_tokens_prior_falls_back_to_global(self):
        priors = {"custom-model": {"latency_s": 1.0, "calls_factor": 1.0, "cold_s_per_100k": 1.0}}
        rates = {**DEFAULT_RATES, "custom-model": DEFAULT_RATES[SONNET]}
        cfg = config(priors=priors, output_tokens_per_call=999)
        plan = planner.plan_turn("custom-model", [], {}, 10000, 0.0, cfg, rates)
        # n * 999 * output_rate is the only output-token term at ctx=10000 cold.
        option = plan["options"][0]
        n = 3.5 * 1.0
        expected_output_component = n * 999 * DEFAULT_RATES[SONNET][1] / 1_000_000
        self.assertAlmostEqual(
            option["cost"],
            (10000 * DEFAULT_RATES[SONNET][3] + (n - 1) * 10000 * DEFAULT_RATES[SONNET][2]) / 1_000_000
            + expected_output_component,
            places=8,
        )

    def test_fable_costs_more_than_the_old_uniform_170_default_once_warm(self):
        """The live-smoke regression: with a single global 170-token
        default, a warm Fable looked marginally cheaper than a warm
        Sonnet. With Fable's own measured 260, it no longer does."""
        state = {FABLE: {"last_used_at": 0.0, "cached_tokens": 30000}}
        plan = planner.plan_turn(FABLE, [], state, 30000, 10.0, config(), DEFAULT_RATES)
        fable = plan["options"][0]
        self.assertTrue(fable["warm"])
        self.assertEqual(fable["cold"], 0)
        # cold=0 -> cost is purely n * output_tokens * output_rate / 1e6.
        n = 3.5 * DEFAULT_PLANNER_PRIORS[FABLE]["calls_factor"]
        old_cost = n * 170 * DEFAULT_RATES[FABLE][1] / 1_000_000
        self.assertGreater(fable["cost"], old_cost)


# ---------------------------------------------------------------------------
# "value" objective (planner.plan_turn)
# ---------------------------------------------------------------------------


class ValueObjectiveTests(unittest.TestCase):
    def test_fable_host_later_turn_fable_warm_75k_sonnet_warm_70k(self):
        """Fable host, LATER turn (p_continue=0.8), ctx=75000: Fable
        warm at 75000 (cold=0, cost 0.111125, time 18.9s -- Fable's own
        260 output tokens/call and $50/M output rate dominate); Sonnet
        warm at 70000 (cold=5000, cost 0.117774375, time 9.6925s).

        balanced: Sonnet's cost (0.117774375) is just OUTSIDE budget
        (0.111125 * 1.05 == 0.11668125) -- not eligible -> chosen: FABLE.
        This reproduces the live-smoke regression itself: balanced alone
        still keeps a warm-but-slow-and-token-heavy Fable.

        value (value_of_time_usd_per_hour=36, i.e. $0.01/s): Fable's
        utility = 0.111125 + 0.01 * 18.9 = 0.300125; Sonnet's utility =
        0.117774375 + 0.01 * 9.6925 = 0.214699375. Sonnet's utility is far
        lower -> chosen: SONNET. This is the actual fix: value correctly
        prices Fable's ~2.3x slower per-call latency as money, which
        balanced's pure cost-tolerance budget cannot see at all.
        """
        state = {
            FABLE: {"last_used_at": 0.0, "cached_tokens": 75000},
            SONNET: {"last_used_at": 0.0, "cached_tokens": 70000},
        }
        balanced_plan = planner.plan_turn(
            FABLE, [SONNET], state, 75000, 10.0, config(objective="balanced"),
            DEFAULT_RATES, p_continue=0.8,
        )
        value_plan = planner.plan_turn(
            FABLE, [SONNET], state, 75000, 10.0, config(objective="value"),
            DEFAULT_RATES, p_continue=0.8,
        )
        by_balanced = {o["model"]: o for o in balanced_plan["options"]}
        by_value = {o["model"]: o for o in value_plan["options"]}

        self.assertAlmostEqual(by_balanced[FABLE]["cost"], 0.111125, places=6)
        self.assertAlmostEqual(by_balanced[SONNET]["cost"], 0.117774375, places=6)
        self.assertEqual(balanced_plan["choice"], FABLE)

        self.assertAlmostEqual(by_value[FABLE]["utility"], 0.300125, places=6)
        self.assertAlmostEqual(by_value[SONNET]["utility"], 0.214699375, places=6)
        self.assertEqual(value_plan["value_of_time_usd_per_hour"], 36)
        self.assertEqual(value_plan["choice"], SONNET)

    def test_opus_host_later_turn_opus_warm_90k_sonnet_cold_value_keeps_opus(self):
        """Opus host, LATER turn (p_continue=0.8), Opus warm at 90000
        (cold=0, cost 0.0756, time 11.55s, utility 0.1911); Sonnet cold at
        90000 (cost 0.429136875, time 10.245s, utility 0.531586875).
        Despite Sonnet's lower time, its much higher cold-write cost
        dominates its utility -> chosen: OPUS."""
        state = {OPUS: {"last_used_at": 0.0, "cached_tokens": 90000}}
        plan = planner.plan_turn(
            OPUS, [SONNET], state, 90000, 10.0, config(objective="value"),
            DEFAULT_RATES, p_continue=0.8,
        )
        by_model = {o["model"]: o for o in plan["options"]}
        self.assertAlmostEqual(by_model[OPUS]["utility"], 0.1911, places=6)
        self.assertAlmostEqual(by_model[SONNET]["utility"], 0.531586875, places=6)
        self.assertEqual(plan["choice"], OPUS)

    def test_opus_fresh_sub_session_70k_value_picks_sonnet(self):
        """Opus host, fresh SUB-SESSION (p_continue=0.06), ctx=70000, both
        cold: Opus utility 0.3976 + 0.01*12.915 == 0.52675; Sonnet utility
        (0.335986875 base cost + 0.02016 lookahead) + 0.01*(10.115 +
        0.0819) == 0.458115875. Sonnet's utility is lower -> chosen:
        SONNET -- the low sub-session continuation probability keeps its
        lookahead penalty small enough that its speed still wins out."""
        plan = planner.plan_turn(
            OPUS, [SONNET], {}, 70000, 0.0, config(objective="value"),
            DEFAULT_RATES, p_continue=0.06,
        )
        by_model = {o["model"]: o for o in plan["options"]}
        self.assertAlmostEqual(by_model[OPUS]["utility"], 0.52675, places=6)
        self.assertAlmostEqual(by_model[SONNET]["utility"], 0.458115875, places=6)
        self.assertEqual(plan["choice"], SONNET)

    def test_value_of_time_zero_equals_cost_objective(self):
        """value_of_time_usd_per_hour=0 degenerates value to exactly the
        cost objective: same choice, and each option's utility equals its
        total_cost exactly (the time term is multiplied by 0)."""
        state = {
            FABLE: {"last_used_at": 0.0, "cached_tokens": 75000},
            SONNET: {"last_used_at": 0.0, "cached_tokens": 70000},
        }
        value_plan = planner.plan_turn(
            FABLE, [SONNET], state, 75000, 10.0,
            config(objective="value", value_of_time_usd_per_hour=0),
            DEFAULT_RATES, p_continue=0.8,
        )
        cost_plan = planner.plan_turn(
            FABLE, [SONNET], state, 75000, 10.0, config(objective="cost"),
            DEFAULT_RATES, p_continue=0.8,
        )
        self.assertEqual(value_plan["choice"], cost_plan["choice"])
        by_value = {o["model"]: o for o in value_plan["options"]}
        by_cost = {o["model"]: o for o in cost_plan["options"]}
        for model in (FABLE, SONNET):
            self.assertAlmostEqual(by_value[model]["utility"], by_cost[model]["cost"], places=8)

    def test_huge_value_of_time_equals_speed_objective(self):
        """An arbitrarily large value_of_time_usd_per_hour (here 1e9,
        i.e. $277,777/s) degenerates value to exactly the speed
        objective: whichever option has the least TOTAL time dominates
        every other term in the utility sum."""
        state = {
            FABLE: {"last_used_at": 0.0, "cached_tokens": 75000},
            SONNET: {"last_used_at": 0.0, "cached_tokens": 70000},
        }
        value_plan = planner.plan_turn(
            FABLE, [SONNET], state, 75000, 10.0,
            config(objective="value", value_of_time_usd_per_hour=1e9),
            DEFAULT_RATES, p_continue=0.8,
        )
        speed_plan = planner.plan_turn(
            FABLE, [SONNET], state, 75000, 10.0, config(objective="speed"),
            DEFAULT_RATES, p_continue=0.8,
        )
        self.assertEqual(value_plan["choice"], speed_plan["choice"])
        self.assertEqual(value_plan["choice"], SONNET)

    def test_utility_not_recorded_for_non_value_objectives(self):
        for objective in ("speed", "cost", "balanced"):
            plan = planner.plan_turn(
                OPUS, [SONNET], {}, 60000, 0.0, config(objective=objective), DEFAULT_RATES
            )
            for option in plan["options"]:
                self.assertNotIn("utility", option)
            self.assertNotIn("value_of_time_usd_per_hour", plan)

    def test_value_objective_ties_go_to_host(self):
        rates = {**DEFAULT_RATES, "twin-model": DEFAULT_RATES[OPUS]}
        priors = {**DEFAULT_PLANNER_PRIORS, "twin-model": DEFAULT_PLANNER_PRIORS[OPUS]}
        plan = planner.plan_turn(
            OPUS, ["twin-model"], {}, 60000, 0.0, config(objective="value", priors=priors), rates
        )
        self.assertEqual(plan["choice"], OPUS)


class ValueObjectiveValidationTests(unittest.TestCase):
    def test_value_is_a_valid_objective(self):
        Policy(model_routing={"start_model": SONNET, "planner": {"enabled": True, "objective": "value"}})

    def test_value_of_time_default_is_36(self):
        merged = effective_planner_config({"start_model": SONNET, "planner": {"enabled": True}})
        self.assertEqual(merged["value_of_time_usd_per_hour"], 36)

    def test_value_of_time_must_be_non_negative(self):
        with self.assertRaises(ValueError):
            Policy(
                model_routing={
                    "start_model": SONNET,
                    "planner": {"enabled": True, "value_of_time_usd_per_hour": -1},
                }
            )

    def test_value_of_time_must_be_a_number(self):
        with self.assertRaises(ValueError):
            Policy(
                model_routing={
                    "start_model": SONNET,
                    "planner": {"enabled": True, "value_of_time_usd_per_hour": "expensive"},
                }
            )

    def test_value_of_time_zero_is_valid(self):
        Policy(
            model_routing={
                "start_model": SONNET,
                "planner": {"enabled": True, "value_of_time_usd_per_hour": 0},
            }
        )

    def test_prior_output_tokens_per_call_validated_like_other_prior_fields(self):
        with self.assertRaises(ValueError):
            Policy(
                model_routing={
                    "start_model": SONNET,
                    "planner": {
                        "enabled": True,
                        "priors": {SONNET: {"output_tokens_per_call": -1}},
                    },
                }
            )


class ValueObjectiveOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_value_objective_receipt_carries_utility_and_value_of_time(self):
        policy = Policy(
            mode="off",
            model_routing={
                "start_model": SONNET,
                "planner": {"enabled": True, "objective": "value"},
            },
        )
        _service, runtime, events = setup_service(policy=policy)
        provider = PricedProvider(default_model=OPUS, usage_by_call=[{"input_tokens": 60000}])
        facade = RoutedProvider(provider, runtime, {}, demo_response)

        await facade.complete(request([user()]))

        planned = [e for e in events if e["event"].endswith("turn_planned")]
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0]["data"]["value_of_time_usd_per_hour"], 36)
        for option in planned[0]["data"]["options"]:
            self.assertIn("utility", option)

    async def test_balanced_objective_receipt_has_no_value_fields(self):
        policy = planner_policy()  # objective defaults to balanced
        _service, runtime, events = setup_service(policy=policy)
        provider = PricedProvider(default_model=OPUS, usage_by_call=[{"input_tokens": 60000}])
        facade = RoutedProvider(provider, runtime, {}, demo_response)

        await facade.complete(request([user()]))

        planned = [e for e in events if e["event"].endswith("turn_planned")]
        self.assertNotIn("value_of_time_usd_per_hour", planned[0]["data"])
        for option in planned[0]["data"]["options"]:
            self.assertNotIn("utility", option)


if __name__ == "__main__":
    unittest.main()
