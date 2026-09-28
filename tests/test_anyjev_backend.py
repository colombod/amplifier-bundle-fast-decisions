"""Hermetic protocol/fallback tests; optional upstream tests use synthetic logits."""
import asyncio
import copy
import importlib.util
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from types import SimpleNamespace

from amplifier_fast_decisions.anyjev_backend import AnyJevBackend, bounded_state, question_spec
from amplifier_fast_decisions.anyjev_server import CONTRACT, SYSTEM, Judge, convert_question, make_server
from amplifier_fast_decisions.backends import BackendUnavailable, ask_many
from amplifier_fast_decisions.contracts import Candidate, DecisionRequest, Question, digest
from amplifier_fast_decisions.demo import DemoCoordinator
from amplifier_fast_decisions.runtime import get_runtime


Q = Question("difficulty", "choice", "How difficult?", {"easy": "Minutes", "hard": "Hours"})
N = Question("safe", "noul", "Is this safe?")


def request(*questions):
    return DecisionRequest({"task": "Fix typo"}, (), questions or (Q,))


def payload(req=None, level="L2"):
    req = req or request()
    return {"model": "test-model", "level": level, "answers": {
        q.name: {"level": level, "question_hash": digest(question_spec(q)),
                 "probabilities": dict(zip(q.criteria or ["true", "false"], [0.8, 0.2]))}
        for q in req.questions}}


@contextmanager
def serving(judge):
    server = make_server(judge, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_question_batch_maps_choice_and_noul(self):
        backend = AnyJevBackend(model="test-model")
        req = request(Q, N)
        with patch.object(backend, "_post", return_value=payload(req)) as post:
            result = await ask_many(backend, req)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(result.answers[Q.name].probabilities, {"easy": 0.8, "hard": 0.2})
        self.assertEqual(result.answers[N.name].noul, 0.8)
        self.assertIsNone(result.input_tokens)
        self.assertFalse(result.synthetic)
        self.assertEqual(result.action.choice, "reason")
        self.assertEqual(result.action.probability_kind, "anyjev_L2")

    async def test_rejects_wrong_identity_levels_and_probabilities(self):
        bad = []
        for key, value in [("model", "other"), ("level", "L0"), ("answers", {})]:
            p = payload(); p[key] = value; bad.append(p)
        for key, value in [("question_hash", "stale"), ("level", "L0"),
                           ("probabilities", {"easy": 0.9, "hard": 0.9}),
                           ("probabilities", {"easy": True, "hard": 0}),
                           ("probabilities", {"easy": float("nan"), "hard": 0}),
                           ("probabilities", {"easy": -0.1, "hard": 1.1}),
                           ("probabilities", {"easy": 1.0})]:
            p = payload(); p["answers"][Q.name][key] = value; bad.append(p)
        for p in bad + [None, [], "wrong"]:
            backend = AnyJevBackend(model="test-model")
            with self.subTest(payload=p), patch.object(backend, "_post", return_value=p):
                with self.assertRaises(BackendUnavailable):
                    await backend.ask(request())

    async def test_no_candidates_or_unsupported_questions_reach_network(self):
        backend = AnyJevBackend(model="test-model")
        candidate = Candidate("read", "Read file", "fast_workspace", {"operation": "read", "path": "a.py"})
        bad = [DecisionRequest({}, (candidate,), (Q,)), DecisionRequest({}, (), ()),
               request(Question("score", "score", "Score it")), request(Q, Q)]
        with patch.object(backend, "_post") as post:
            for req in bad:
                with self.assertRaises(BackendUnavailable):
                    await backend.ask(req)
            post.assert_not_called()

    async def test_cancellation_does_not_queue_another_worker(self):
        backend = AnyJevBackend(model="test-model", timeout_ms=1000)
        started, release = threading.Event(), threading.Event()
        def slow(_):
            started.set(); release.wait(1); return payload()
        with patch.object(backend, "_post", side_effect=slow) as post:
            task = asyncio.create_task(backend.ask(request()))
            await asyncio.to_thread(started.wait, 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            with self.assertRaises(BackendUnavailable):
                await backend.ask(request())
            release.set()
            await backend._pending
            self.assertEqual(post.call_count, 1)

    async def test_timeout_and_late_error_are_bounded(self):
        backend = AnyJevBackend(model="test-model", timeout_ms=10)
        def slow(_):
            time.sleep(0.05)
            raise BackendUnavailable("late")
        with patch.object(backend, "_post", side_effect=slow):
            with self.assertRaises(TimeoutError):
                await backend.ask(request())
            with self.assertRaises(BackendUnavailable):
                await backend._pending

    async def test_real_http_transport_and_server_failure(self):
        class Stub:
            model, level = "test-model", "L2"
            def predict(self, body):
                return payload()
        with serving(Stub()) as url:
            result = await AnyJevBackend(model="test-model", url=url).ask(request())
            self.assertEqual(result.answers[Q.name].probabilities["hard"], 0.2)
        class Failing(Stub):
            def predict(self, body):
                raise ValueError("private state must not escape")
        with serving(Failing()) as url:
            with self.assertRaises(BackendUnavailable) as caught:
                await AnyJevBackend(model="test-model", url=url).ask(request())
            self.assertNotIn("private", str(caught.exception))

    async def test_runtime_constructs_local_backend_without_model_imports(self):
        with tempfile.TemporaryDirectory() as root:
            runtime, _ = get_runtime(DemoCoordinator(), {"backend": "anyjev", "model": "test-model",
                                                        "events_dir": root, "mode": "active"})
            self.assertIsInstance(runtime.service.backend, AnyJevBackend)
            self.assertFalse(runtime.service.backend.external)
            await runtime.close()
            for config in [{"anyjev_level": "L0", "model": "test-model"}, {}]:
                with self.assertRaises(ValueError):
                    get_runtime(DemoCoordinator(), {"backend": "anyjev", "mode": "active",
                                                     "events_dir": root, **config})

    async def test_orchestrator_consumes_judgment_and_preserves_fallback_and_user_pin(self):
        from amplifier_fast_decisions.orchestrator import (
            decide_start_tier, DIFFICULTY_INSTRUCTIONS, DIFFICULTY_CRITERIA,
        )
        q = Question("task_difficulty", "choice", DIFFICULTY_INSTRUCTIONS, DIFFICULTY_CRITERIA)
        routing = {"start_model": "cheap-model", "start_policy": "judge"}
        host_request = SimpleNamespace(messages=[{"role": "user", "content": "x" * 2200}])
        with tempfile.TemporaryDirectory() as root:
            runtime, _ = get_runtime(DemoCoordinator(), {"backend": "anyjev", "model": "test-model",
                                    "events_dir": root, "mode": "active", "model_routing": routing})
            backend = runtime.service.backend
            with patch.object(backend, "_post", return_value=payload(request(q))):
                self.assertEqual(await decide_start_tier(runtime.service, host_request, routing, None), "cheap")
            with patch.object(backend, "_post", side_effect=BackendUnavailable("unavailable")):
                self.assertEqual(await decide_start_tier(runtime.service, host_request, routing, None), "strong")
            with patch.object(backend, "_post") as post:
                self.assertEqual(await decide_start_tier(runtime.service, host_request, routing, None,
                                                        user_model="chosen-model"), "strong")
                post.assert_not_called()
            await runtime.close()

    async def test_existing_shadow_L0_cannot_be_promoted_to_active_by_owner(self):
        with tempfile.TemporaryDirectory() as root:
            coordinator = DemoCoordinator()
            runtime, _ = get_runtime(coordinator, {"backend": "anyjev", "model": "test-model",
                                                    "events_dir": root, "anyjev_level": "L0"})
            with self.assertRaises(ValueError):
                get_runtime(coordinator, {"mode": "active"}, owner=True)
            self.assertEqual(runtime.service.policy.mode, "shadow")
            await runtime.close()


class ValidationTests(unittest.TestCase):
    def test_rejects_external_urls_redirect_surfaces_and_missing_model(self):
        for url in ["https://127.0.0.1:8091", "http://localhost:8091", "http://example.com",
                    "http://127.0.0.1:8091/path", "http://user:pass@127.0.0.1:8091"]:
            with self.assertRaises(ValueError):
                AnyJevBackend(model="test-model", url=url)
        with self.assertRaises(ValueError):
            AnyJevBackend(model="")

    def test_state_is_scrubbed_and_bounded(self):
        self.assertLessEqual(len(bounded_state({"task": "x" * 20000})), 12000)
        self.assertNotIn("abc123secret", bounded_state({"task": "api_key=abc123secret"}))


@unittest.skipUnless(importlib.util.find_spec("anyjev"), "optional pinned AnyJev dependency")
class UpstreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_decider_L0_L1_L2_and_exact_head_binding(self):
        from anyjev import Decider
        from anyjev.backends.fake import FakeBackend
        from amplifier_fast_decisions.anyjev_server import SYSTEM

        def content(state, option):
            return 4.0 if ("hard" in state) == ("hard" in option) else 0.0
        upstream = FakeBackend(content, position_bias=[2, 0])
        decider = Decider(upstream, prior="none", system=SYSTEM, adapt=False, shared_prefix=False)
        q = convert_question(question_spec(Q))
        states = [bounded_state({"task": f'{"hard" if i % 2 else "easy"} task {i}'}) for i in range(100)]
        labels = [i % 2 for i in range(100)]
        for level in ["L0", "L1", "L2"]:
            artifacts = None
            if level != "L0":
                decider.calibrate(q, states, labels, level=level)
                artifacts = decider.export_artifacts()
                artifacts["fast_decisions"] = {"contract": CONTRACT, "system_hash": digest(SYSTEM)}
            judge = Judge(decider, model="test-model", level=level, artifacts=artifacts)
            with serving(judge) as url:
                result = await AnyJevBackend(model="test-model", url=url, level=level).ask(request())
                self.assertGreater(result.answers[Q.name].probabilities["easy"], 0.5)
                if level != "L0":
                    changed = Question(Q.name, Q.type, "A DIFFERENT meaning", Q.criteria)
                    with self.assertRaises(BackendUnavailable):
                        await AnyJevBackend(model="test-model", url=url, level=level).ask(request(changed))
                    wrong = copy.deepcopy(artifacts); wrong["model"] = "another-model"
                    with self.assertRaises(ValueError):
                        Judge(decider, model="test-model", level=level, artifacts=wrong)


if __name__ == "__main__":
    unittest.main()
