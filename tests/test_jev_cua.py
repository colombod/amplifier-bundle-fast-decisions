import asyncio
import copy
import unittest
import importlib.util
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from amplifier_fast_decisions.contracts import Answer, Decision, DecisionResult
from amplifier_fast_decisions.jev_cua import (
    CuaSelector,
    digest,
    metadata,
    run,
    run_step,
    snapshot,
)
from amplifier_fast_decisions.cua_host import TryCuaHost


def observation():
    return {
        "surface_id": "fixture",
        "revision": "1",
        "text": "Choose a report",
        "elements": [
            {"id": "weekly", "label": "Weekly report", "operations": ["CLICK"]},
            {"id": "search", "label": "Search", "operations": ["TYPE_TEXT"]},
            {
                "id": "format",
                "label": "Format",
                "operations": ["SELECT"],
                "options": {"csv": "CSV", "pdf": "PDF"},
            },
        ],
    }


class Judge:
    def __init__(self, op="CLICK", target="weekly", *, probability=1, malformed=None):
        self.op = op
        self.target = target
        self.probability = probability
        self.malformed = malformed
        self.requests = []

    async def ask_many(self, req):
        self.requests.append(req)
        choices = {c.id: 0 for c in req.candidates}
        choices["reason"] = 0
        choices[self.op] = self.probability
        if self.op != "reason":
            choices["reason"] = 1 - self.probability
        answers = {}
        for q in req.questions:
            probs = {k: 0 for k in q.criteria}
            probs[self.target if self.target in probs else "reason"] = 1
            if q.name == self.malformed:
                probs = {"invented": 1}
            answers[q.name] = Answer(probs)
        return DecisionResult(
            Decision(self.op, choices),
            answers,
            model="jev-1.13.0",
            input_tokens=100,
            output_tokens=0,
        )

    async def close(self):
        pass


class CuaTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(
        importlib.util.find_spec("amplifier_module_loop_streaming"),
        "requires actual Amplifier loop",
    )
    async def test_native_loop_preserves_allow_and_deny(self):
        from amplifier_core.testing import MockCoordinator, MockContextManager
        from amplifier_core.message_models import ChatResponse, ToolCall, Usage
        from amplifier_core.models import HookResult
        from amplifier_module_loop_streaming import StreamingOrchestrator
        from amplifier_fast_decisions.jev_cua import JevCuaTool

        class Provider:
            name = "controlled-cua-test"

            def __init__(self):
                self.calls = 0

            def get_info(self):
                return SimpleNamespace(
                    id=self.name, display_name=self.name, context_window=32000
                )

            def parse_tool_calls(self, response):
                return response.tool_calls or []

            async def complete(self, request, **kwargs):
                self.calls += 1
                return ChatResponse(
                    content=[]
                    if self.calls == 1
                    else [{"type": "text", "text": "done"}],
                    tool_calls=[
                        ToolCall(
                            id="cua-1",
                            name="jev_cua",
                            arguments={
                                "goal": "Open weekly",
                                "snapshot": observation(),
                            },
                        )
                    ]
                    if self.calls == 1
                    else [],
                    usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
                )

        for approval, expected_calls in [("continue", 1), ("deny", 0)]:
            coordinator = MockCoordinator()
            tool = JevCuaTool(allow_external_state=False)
            original = tool.execute
            tool.execute = AsyncMock(side_effect=original)

            async def decide(event, data):
                return HookResult(action=approval)

            coordinator.hooks.register("tool:pre", decide, priority=0)
            try:
                await StreamingOrchestrator({}).execute(
                    "test",
                    MockContextManager(),
                    {"controlled": Provider()},
                    {"jev_cua": tool},
                    coordinator.hooks,
                    coordinator=coordinator,
                )
                self.assertEqual(tool.execute.await_count, expected_calls)
            finally:
                await tool.selector.close()

    @unittest.skipUnless(
        importlib.util.find_spec("amplifier_core"), "requires actual Amplifier host"
    )
    async def test_native_mount_envelope_and_metadata_record(self):
        from amplifier_core.testing import MockCoordinator
        from amplifier_core.models import ToolResult
        from amplifier_fast_decisions.jev_cua import mount

        coordinator = MockCoordinator()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ, {"AFAST_EVENTS_DIR": directory, "AFAST_TRAFFIC": "test"}
            ),
        ):
            cleanup = await mount(coordinator, {"backend": "jev", "allow_external_state": False})
            try:
                tool = coordinator.get("tools", "jev_cua")
                result = await tool.execute(
                    {"goal": "Open weekly", "snapshot": observation()}
                )
                self.assertIsInstance(result, ToolResult)
                self.assertEqual(result.output["reason"], "external_state_not_enabled")
            finally:
                await cleanup()
            lines = [
                json.loads(line)
                for p in Path(directory).glob("*.jsonl")
                for line in p.read_text().splitlines()
            ]
            self.assertEqual(lines[0]["event"], "fast_decisions:cua_decided")
            self.assertEqual(lines[0]["data"]["traffic"], "test")
            self.assertNotIn("weekly", str(lines))

    def selector(self, **kwargs):
        judge = Judge(**kwargs)
        return CuaSelector(backend=judge, allow_external_state=True), judge

    def host(self):
        return SimpleNamespace(
            observe=AsyncMock(side_effect=lambda: observation()),
            approve=AsyncMock(return_value=True),
            execute=AsyncMock(return_value=True),
            verify=AsyncMock(return_value=True),
        )

    async def test_one_batched_request_selects_compatible_target(self):
        selector, judge = self.selector()
        out = await selector.choose("Open weekly report", observation())
        self.assertEqual(out["action"], {"operation": "CLICK", "target": "weekly"})
        self.assertEqual(len(judge.requests), 1)
        self.assertEqual(
            {q.name for q in judge.requests[0].questions},
            {"click_target", "type_text_target", "select_target"},
        )
        self.assertEqual(out["judge_calls"], 1)
        self.assertFalse(out["executes_actions"])

    async def test_selected_head_validated_unused_head_ignored(self):
        for head, status in [("click_target", "reason"), ("select_target", "proposal")]:
            selector, _ = self.selector(malformed=head)
            self.assertEqual(
                (await selector.choose("Open weekly", observation()))["status"], status
            )

    async def test_low_probability_abstains(self):
        selector, _ = self.selector(probability=0.6)
        self.assertEqual(
            (await selector.choose("Open weekly", observation()))["status"], "reason"
        )

    async def test_opt_out_does_not_call(self):
        judge = Judge()
        selector = CuaSelector(backend=judge)
        out = await selector.choose("Open weekly", observation())
        self.assertEqual(out["reason"], "external_state_not_enabled")
        self.assertEqual(judge.requests, [])

    async def test_sensitive_controls_removed_and_metadata_is_scoped(self):
        selector, judge = self.selector()
        obs = observation()
        obs["elements"][1].update(sensitive=True, value="private-value")
        out = await selector.choose("Open weekly", obs)
        self.assertNotIn("private-value", str(judge.requests))
        self.assertNotIn("TYPE_TEXT", {c.id for c in judge.requests[0].candidates})
        self.assertNotIn("weekly", str(metadata(out)))
        self.assertNotIn("surface_id", metadata(out))

    async def test_select_only_observed_option(self):
        selector, _ = self.selector(op="SELECT", target="format:pdf")
        out = await selector.choose("Select PDF", observation())
        self.assertEqual(
            out["action"], {"operation": "SELECT", "target": "format", "option": "pdf"}
        )

    async def test_denied_never_executes(self):
        selector, _ = self.selector()
        host = self.host()
        host.approve.return_value = False
        self.assertEqual(
            (await run_step(selector, "Open weekly", host))["status"], "denied"
        )
        host.execute.assert_not_called()

    async def test_stale_before_or_after_approval_never_executes(self):
        for at in (1, 2):
            selector, _ = self.selector()
            host = self.host()
            observations = [observation() for _ in range(3)]
            observations[at]["revision"] = "2"
            host.observe.side_effect = observations
            self.assertEqual(
                (await run_step(selector, "Open weekly", host))["status"], "stale"
            )
            host.execute.assert_not_called()

    async def test_done_requires_host_verification(self):
        for verified, status in [(True, "done"), (False, "verification_failed")]:
            selector, _ = self.selector(op="DONE")
            host = self.host()
            host.verify.return_value = verified
            self.assertEqual(
                (await run_step(selector, "Open weekly", host))["status"], status
            )
            host.execute.assert_not_called()

    async def test_type_text_requires_host_generation(self):
        selector, _ = self.selector(op="TYPE_TEXT", target="search")
        host = self.host()
        self.assertEqual(
            (await run_step(selector, "Search reports", host))["status"], "needs_text"
        )
        host.execute.assert_not_called()

    async def test_no_reasoning_calls_in_bounded_loop_and_repeat_stops_before_execution(
        self,
    ):
        selector, judge = self.selector()
        host = self.host()
        record = AsyncMock()
        result = await run(selector, "Open weekly", host, record=record)
        self.assertEqual(result["status"], "loop_stopped")
        self.assertEqual(host.execute.await_count, 1)
        self.assertEqual(len(judge.requests), 2)
        self.assertEqual(record.await_count, 2)

    async def test_step_limit(self):
        selector, _ = self.selector()
        host = self.host()
        self.assertEqual(
            (await run(selector, "Open weekly", host, max_steps=1))["status"],
            "step_limit",
        )

    async def test_timeout_unknown_usage_and_cancellation(self):
        async def hang(req):
            await asyncio.sleep(10)

        backend = SimpleNamespace(ask_many=hang)
        selector = CuaSelector(
            backend=backend, allow_external_state=True, timeout_ms=100
        )
        out = await selector.choose("Open weekly", observation())
        self.assertEqual(out["status"], "reason")
        self.assertTrue(out["usage_unknown"])
        task = asyncio.create_task(selector.choose("Open weekly", observation()))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_unexpected_provenance_rejected(self):
        selector, judge = self.selector()
        original = judge.ask_many

        async def synthetic(req):
            result = await original(req)
            return DecisionResult(
                result.action, result.answers, model=result.model, synthetic=True
            )

        judge.ask_many = synthetic
        self.assertEqual(
            (await selector.choose("Open weekly", observation()))["status"], "reason"
        )

    def test_invalid_snapshot(self):
        for mutation in [
            lambda o: o["elements"].append(o["elements"][0]),
            lambda o: o["elements"][0].update(operations=["EXEC_JS"]),
            lambda o: o["elements"][2].update(options={1: "bad"}),
            lambda o: o.update(text="x" * 8001),
        ]:
            obs = observation()
            mutation(obs)
            with self.assertRaises(ValueError):
                snapshot(obs)

    async def test_trycua_click_and_moving_native_target(self):
        obs = observation()
        obs["elements"] = obs["elements"][:1]
        point = [20, 30]

        async def observe():
            return copy.deepcopy(obs), {"weekly": list(point)}

        interface = SimpleNamespace(left_click=AsyncMock())
        host = TryCuaHost(
            interface,
            observe_controls=observe,
            approve=AsyncMock(return_value=True),
            verify=AsyncMock(return_value=False),
        )
        selector, _ = self.selector()
        out = await run_step(selector, "Open weekly", host)
        self.assertEqual(out["status"], "executed")
        interface.left_click.assert_awaited_once_with(x=20, y=30)
        old = await host.observe()
        point[0] = 21
        self.assertFalse(
            await host.execute({"operation": "CLICK", "target": "weekly"}, old)
        )
        self.assertEqual(interface.left_click.await_count, 1)
        self.assertNotEqual(digest(old), digest(await host.observe()))
