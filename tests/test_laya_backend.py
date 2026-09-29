"""Stdlib-only tests for the Laya backend/server pair.

Covers three layers, all hermetic (a fake http.server or a fake in-process
agent stands in for the real laya-mlx model -- the real model is never
loaded in tests):

1. ``laya_server.py``'s HTTP handler, with ``_load_agent`` monkeypatched to
   a fake agent so no real model is loaded.
2. ``LayaBackend`` (local_backend.py), the client, against a bare
   ``http.server`` fake decide endpoint (mirrors test_gateway_backend.py's
   pattern).
3. ``runtime.py``'s factory wiring for ``backend: "laya"`` and
   ``cli.py``'s ``_laya_server_check`` doctor probe.
"""

from __future__ import annotations

import http.server
import json
import os
import threading
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from amplifier_fast_decisions import laya_server
from amplifier_fast_decisions.backends import BackendUnavailable
from amplifier_fast_decisions.contracts import Candidate, DecisionRequest, Question, NEXT_ACTION, SLOW
from amplifier_fast_decisions.local_backend import (
    LAYA_DEFAULT_TOKEN_ENV,
    LAYA_DEFAULT_URL,
    LayaBackend,
    _build_laya_input,
    laya_base_url,
)


def _request() -> DecisionRequest:
    return DecisionRequest(
        state={"observations": [{"role": "user", "text": "Read README.md"}]},
        candidates=(
            Candidate(
                "read", "Read README.md", "fast_workspace",
                {"operation": "read", "path": "README.md"},
            ),
        ),
    )


def _decide_payload(*, model="laya-rl-agent", choice="read", probabilities=None):
    probabilities = probabilities if probabilities is not None else {"read": 0.9, SLOW: 0.1}
    return {
        "model": model,
        "answers": {
            NEXT_ACTION: {
                "type": "choice",
                "confidence": 0.9,
                "choice": choice,
                "probabilities": probabilities,
            }
        },
    }


# --- LayaBackend (client) tests ---------------------------------------------


class LayaUrlValidationTests(unittest.TestCase):
    def test_loopback_http_allowed(self):
        self.assertEqual(laya_base_url("http://127.0.0.1:8090"), "http://127.0.0.1:8090")

    def test_nonloopback_requires_https(self):
        with self.assertRaises(ValueError):
            laya_base_url("http://laya.example.internal")

    def test_nonloopback_https_accepted(self):
        self.assertEqual(
            laya_base_url("https://laya.example.internal"), "https://laya.example.internal"
        )

    def test_rejects_credentials_query_fragment(self):
        for url in (
            "https://user:secret@laya.example.internal",
            "https://laya.example.internal?x=1",
            "https://laya.example.internal#frag",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                laya_base_url(url)


class LayaConstructionTests(unittest.TestCase):
    def test_default_url_and_token_env(self):
        backend = LayaBackend()
        self.assertEqual(backend.base_url, LAYA_DEFAULT_URL)
        self.assertEqual(backend.token_env, LAYA_DEFAULT_TOKEN_ENV)
        self.assertFalse(backend.external)
        self.assertEqual(backend.name, "laya")

    def test_env_url_override(self):
        with mock.patch.dict(os.environ, {"FAST_DECISIONS_LAYA_URL": "http://127.0.0.1:9999"}):
            backend = LayaBackend()
        self.assertEqual(backend.base_url, "http://127.0.0.1:9999")

    def test_nonloopback_url_is_external(self):
        backend = LayaBackend(url="https://laya.example.internal")
        self.assertTrue(backend.external)

    def test_explicit_token_env(self):
        backend = LayaBackend(token_env="MY_LAYA_TOKEN")
        self.assertEqual(backend.token_env, "MY_LAYA_TOKEN")


class _DecideHandler(http.server.BaseHTTPRequestHandler):
    """Configured per-test via class attributes, rebound by ``_make_handler``."""

    health_status = 200
    decide_status = 200
    decide_payload = None
    delay_s = 0.0
    request_paths: list = []
    request_bodies: list = []
    request_headers: list = []

    def log_message(self, *_args, **_kwargs):
        return

    def do_GET(self):  # noqa: N802
        type(self).request_paths.append(self.path)
        type(self).request_headers.append(dict(self.headers))
        if self.path == "/health":
            body = b'{"status": "ok"}'
            self.send_response(self.health_status)
        else:
            body = b"not found"
            self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        type(self).request_paths.append(self.path)
        type(self).request_headers.append(dict(self.headers))
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = {}
        type(self).request_bodies.append(parsed)
        if self.delay_s:
            time.sleep(self.delay_s)
        payload = self.decide_payload if self.decide_payload is not None else _decide_payload()
        body = json.dumps(payload).encode("utf-8")
        self.send_response(self.decide_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _make_handler(**attrs):
    attrs.setdefault("request_paths", [])
    attrs.setdefault("request_bodies", [])
    attrs.setdefault("request_headers", [])
    return type("_DecideHandler", (_DecideHandler,), dict(attrs))


@contextmanager
def _running_server(handler_cls):
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class LayaAskTests(unittest.TestCase):
    def test_prepared_git_snapshot_is_scoreable_but_arbitrary_git_is_rejected(self):
        request = DecisionRequest(state={'observations':[]}, candidates=(
            Candidate('git_status','Read repository status','fast_workspace',{'operation':'git','path':'.'}),))
        _, criteria, _ = _build_laya_input(request)
        self.assertIn('git_status',criteria)
        self.assertIn(SLOW,criteria)
        for args in [{'operation':'git','path':'..'},
                     {'operation':'git','path':'.','command':'reset --hard'}]:
            with self.subTest(args=args), self.assertRaises(BackendUnavailable):
                _build_laya_input(DecisionRequest(state={},candidates=(
                    Candidate('git_status','Git','fast_workspace',args),)))

    def test_typed_questions_are_batched_and_missing_answers_fail_closed(self):
        import asyncio
        questions = (Question("difficulty", "choice", "How difficult?", {"easy": "easy", "hard": "hard"}),
                     Question("relevant", "noul", "Is this relevant?"))
        request = DecisionRequest(state={"task": "Read README.md"}, candidates=(), questions=questions)
        payload = {"model": "laya", "answers": {
            "difficulty": {"type": "choice", "choice": "easy", "probabilities": {"easy": .9, "hard": .1}},
            "relevant": {"type": "noul", "noul": .8}}}
        handler = _make_handler(decide_payload=payload)
        with _running_server(handler) as url:
            backend = LayaBackend(url=url, timeout_ms=2000)
            result = asyncio.run(backend.ask_many(request))
            self.assertEqual(len(handler.request_bodies), 1)
            self.assertEqual(set(handler.request_bodies[0]["questions"]), {"difficulty", "relevant"})
            self.assertEqual(result.answers["relevant"].noul, .8)
            self.assertIsNone(result.output_tokens)
            del payload["answers"]["relevant"]
            with self.assertRaises(BackendUnavailable):
                asyncio.run(backend.ask(request))

    def test_scores_via_decide_endpoint(self):
        handler = _make_handler(decide_payload=_decide_payload())
        with _running_server(handler) as base_url:
            backend = LayaBackend(url=base_url, timeout_ms=2000)
            import asyncio

            result = asyncio.run(backend.ask(_request()))
        self.assertEqual(result.action.choice, "read")
        self.assertAlmostEqual(result.action.probabilities["read"], 0.9)
        self.assertEqual(handler.request_paths, ["/v1/decide"])
        self.assertEqual(handler.request_headers[0].get("User-Agent"), "amplifier-fast-decisions/0.1")
        body = handler.request_bodies[0]
        self.assertIn("state", body)
        self.assertIn(NEXT_ACTION, body["questions"])
        criteria = body["questions"][NEXT_ACTION]["criteria"]
        self.assertEqual(set(criteria), {"read", SLOW})

    def test_renormalizes_probabilities_off_by_small_amount(self):
        payload = _decide_payload(probabilities={"read": 0.94, SLOW: 0.1})  # sums to 1.04
        handler = _make_handler(decide_payload=payload)
        with _running_server(handler) as base_url:
            backend = LayaBackend(url=base_url, timeout_ms=2000)
            import asyncio

            result = asyncio.run(backend.ask(_request()))
        total = sum(result.action.probabilities.values())
        self.assertAlmostEqual(total, 1.0, places=6)

    def test_missing_answers_raises_backend_unavailable(self):
        handler = _make_handler(decide_payload={"model": "laya-rl-agent"})
        with _running_server(handler) as base_url:
            backend = LayaBackend(url=base_url, timeout_ms=2000)
            import asyncio

            with self.assertRaises(BackendUnavailable):
                asyncio.run(backend.ask(_request()))

    def test_alternatives_mismatch_raises_backend_unavailable(self):
        payload = _decide_payload(choice="read", probabilities={"read": 0.5, "other": 0.5})
        handler = _make_handler(decide_payload=payload)
        with _running_server(handler) as base_url:
            backend = LayaBackend(url=base_url, timeout_ms=2000)
            import asyncio

            with self.assertRaises(BackendUnavailable):
                asyncio.run(backend.ask(_request()))

    def test_http_error_raises_backend_unavailable(self):
        handler = _make_handler(decide_status=500)
        with _running_server(handler) as base_url:
            backend = LayaBackend(url=base_url, timeout_ms=2000)
            import asyncio

            with self.assertRaises(BackendUnavailable):
                asyncio.run(backend.ask(_request()))

    def test_authorization_header_sent_when_token_env_set(self):
        handler = _make_handler(decide_payload=_decide_payload())
        with _running_server(handler) as base_url, mock.patch.dict(
            os.environ, {"MY_LAYA_TOKEN": "secret-value"}
        ):
            backend = LayaBackend(url=base_url, timeout_ms=2000, token_env="MY_LAYA_TOKEN")
            import asyncio

            asyncio.run(backend.ask(_request()))
        self.assertEqual(handler.request_headers[0]["Authorization"], "Bearer secret-value")

    def test_no_authorization_header_when_token_env_unset(self):
        handler = _make_handler(decide_payload=_decide_payload())
        with _running_server(handler) as base_url:
            backend = LayaBackend(url=base_url, timeout_ms=2000, token_env="UNSET_LAYA_TOKEN_VAR")
            import asyncio

            asyncio.run(backend.ask(_request()))
        self.assertNotIn("Authorization", handler.request_headers[0])


class LayaWarmupTests(unittest.TestCase):
    def test_warmup_calls_health_then_decide(self):
        handler = _make_handler(
            decide_payload=_decide_payload(choice="a", probabilities={"a": 0.9, SLOW: 0.1})
        )
        with _running_server(handler) as base_url:
            backend = LayaBackend(url=base_url, timeout_ms=2000)
            import asyncio

            asyncio.run(backend.warmup())
        self.assertEqual(handler.request_paths, ["/health", "/v1/decide"])

    def test_warmup_raises_on_unhealthy_server(self):
        handler = _make_handler(health_status=503)
        with _running_server(handler) as base_url:
            backend = LayaBackend(url=base_url, timeout_ms=2000)
            import asyncio

            with self.assertRaises(BackendUnavailable):
                asyncio.run(backend.warmup())


# --- laya_server.py (server) tests ------------------------------------------


class _FakeAgent:
    def __init__(self, response=None, raise_on=None):
        self._response = response if response is not None else _decide_payload()
        self._raise_on = raise_on
        self.calls = []

    def predict(self, state, questions):
        self.calls.append((state, questions))
        if self._raise_on:
            raise self._raise_on
        return self._response


class LayaServerHandlerTests(unittest.TestCase):
    def test_concurrent_clients_serialize_model_inference(self):
        from concurrent.futures import ThreadPoolExecutor
        import urllib.request

        class NonReentrantAgent(_FakeAgent):
            active = 0
            maximum = 0

            def predict(self, state, questions):
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                time.sleep(0.03)
                self.active -= 1
                return super().predict(state, questions)

        server, thread, agent = self._build(agent=NonReentrantAgent())
        barrier = threading.Barrier(4)
        def call(_):
            barrier.wait(timeout=5)
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/decide",
                data=json.dumps({"state": "test", "questions": {
                    NEXT_ACTION: {"type": "choice", "criteria": {"read": "read", SLOW: "reason"}}
                }}).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                self.assertEqual(list(pool.map(call, range(4))), [200] * 4)
            self.assertEqual(len(agent.calls), 4)
            self.assertEqual(agent.maximum, 1)
        finally:
            self._stop(server, thread)

    def _build(self, agent=None, token_env=None):
        fake_agent = agent if agent is not None else _FakeAgent()
        with mock.patch.object(laya_server, "_load_agent", return_value=fake_agent), \
             mock.patch.object(laya_server, "_warm"):
            server = laya_server.build_server("127.0.0.1", 0, "aac6fef/laya-mlx", "float16", token_env)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, fake_agent

    def _stop(self, server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    def test_health_reports_loaded_model(self):
        server, thread, _ = self._build()
        try:
            import urllib.request

            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/health", timeout=5
            ) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        finally:
            self._stop(server, thread)
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["loaded"])
        self.assertEqual(payload["model"], "aac6fef/laya-mlx")

    def test_decide_passes_through_agent_result(self):
        agent_result = _decide_payload(choice="read", probabilities={"read": 0.8, SLOW: 0.2})
        server, thread, agent = self._build(agent=_FakeAgent(response=agent_result))
        try:
            import urllib.request

            body = json.dumps(
                {
                    "state": "obs",
                    "questions": {
                        NEXT_ACTION: {
                            "type": "choice",
                            "instructions": "x",
                            "criteria": {"read": "read", SLOW: "reason"},
                        }
                    },
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/decide",
                data=body, method="POST", headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        finally:
            self._stop(server, thread)
        # The server reshapes the agent's raw result into Jev's
        # {model, answers, usage} response shape -- the underlying
        # choice/probabilities/confidence values are unchanged.
        self.assertEqual(payload["model"], "laya-rl-agent")
        self.assertEqual(payload["usage"], {})
        answer = payload["answers"][NEXT_ACTION]
        self.assertEqual(answer["type"], "choice")
        self.assertEqual(answer["choice"], "read")
        self.assertEqual(answer["probabilities"], {"read": 0.8, SLOW: 0.2})
        self.assertAlmostEqual(answer["confidence"], 0.9)
        self.assertEqual(len(agent.calls), 1)

    def test_decide_reachable_via_systemone_alias(self):
        agent_result = _decide_payload(choice="read", probabilities={"read": 0.8, SLOW: 0.2})
        server, thread, agent = self._build(agent=_FakeAgent(response=agent_result))
        try:
            import urllib.request

            body = json.dumps(
                {
                    "state": "obs",
                    "model": "some-model",
                    "questions": {
                        NEXT_ACTION: {
                            "type": "choice",
                            "instructions": "x",
                            "criteria": {"read": "read", SLOW: "reason"},
                        }
                    },
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/systemone",
                data=body, method="POST", headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        finally:
            self._stop(server, thread)
        answer = payload["answers"][NEXT_ACTION]
        self.assertEqual(answer["choice"], "read")
        self.assertEqual(len(agent.calls), 1)

    def test_decide_computes_confidence_when_agent_omits_it(self):
        agent_result = {
            "model": "laya-rl-agent",
            "answers": {
                NEXT_ACTION: {
                    "type": "choice",
                    "choice": "read",
                    "probabilities": {"read": 0.8, SLOW: 0.2},
                }
            },
        }
        server, thread, agent = self._build(agent=_FakeAgent(response=agent_result))
        try:
            import urllib.request

            body = json.dumps(
                {
                    "state": "obs",
                    "questions": {
                        NEXT_ACTION: {
                            "type": "choice",
                            "instructions": "x",
                            "criteria": {"read": "read", SLOW: "reason"},
                        }
                    },
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/systemone",
                data=body, method="POST", headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        finally:
            self._stop(server, thread)
        answer = payload["answers"][NEXT_ACTION]
        # (n * p_max - 1) / (n - 1) with n=2, p_max=0.8 -> 0.6
        self.assertAlmostEqual(answer["confidence"], 0.6)

    def test_decide_rejects_missing_questions(self):
        server, thread, _ = self._build()
        try:
            import urllib.error
            import urllib.request

            body = json.dumps({"state": "obs"}).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/decide",
                data=body, method="POST", headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 400)
        finally:
            self._stop(server, thread)

    def test_decide_500_on_predict_failure(self):
        server, thread, _ = self._build(agent=_FakeAgent(raise_on=RuntimeError("boom")))
        try:
            import urllib.error
            import urllib.request

            body = json.dumps(
                {
                    "state": "obs",
                    "questions": {
                        NEXT_ACTION: {
                            "type": "choice",
                            "instructions": "x",
                            "criteria": {"a": "a", SLOW: "reason"},
                        }
                    },
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/decide",
                data=body, method="POST", headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 500)
        finally:
            self._stop(server, thread)

    def test_decide_requires_matching_bearer_token(self):
        with mock.patch.dict(os.environ, {"TEST_LAYA_SERVER_TOKEN": "expected-token"}):
            server, thread, _ = self._build(token_env="TEST_LAYA_SERVER_TOKEN")
            try:
                import urllib.error
                import urllib.request

                body = json.dumps(
                    {
                        "state": "obs",
                        "questions": {
                            NEXT_ACTION: {
                                "type": "choice",
                                "instructions": "x",
                                "criteria": {"a": "a", SLOW: "reason"},
                            }
                        },
                    }
                ).encode("utf-8")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/v1/decide",
                    data=body, method="POST", headers={"Content-Type": "application/json"},
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(ctx.exception.code, 401)
            finally:
                self._stop(server, thread)

    def test_unknown_path_is_404(self):
        server, thread, _ = self._build()
        try:
            import urllib.error
            import urllib.request

            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/nope", timeout=5)
            self.assertEqual(ctx.exception.code, 404)
        finally:
            self._stop(server, thread)

    def test_build_server_exits_when_agent_unavailable(self):
        with mock.patch.object(
            laya_server, "_load_agent", side_effect=ImportError("no laya installed")
        ):
            with self.assertRaises(SystemExit):
                laya_server.build_server("127.0.0.1", 0, "aac6fef/laya-mlx", "float16", None)


# --- runtime.py factory wiring ------------------------------------------------


class RuntimeLayaBackendTests(unittest.TestCase):
    def test_get_runtime_selects_laya_backend(self):
        from amplifier_fast_decisions.runtime import get_runtime

        fake_backend = SimpleNamespace(name="laya", external=False)
        coordinator = SimpleNamespace(
            get_capability=lambda name: None,
            register_capability=lambda name, value: None,
            session=None,
            session_id="test-session",
            parent_id=None,
            hooks=None,
        )
        with mock.patch(
            "amplifier_fast_decisions.local_backend.LayaBackend", return_value=fake_backend
        ) as cls:
            runtime, created = get_runtime(
                coordinator,
                {
                    "backend": "laya",
                    "laya_url": "http://127.0.0.1:8090",
                    "events_dir": "/tmp/afast-laya-test",
                },
            )
        self.assertTrue(created)
        cls.assert_called_once()
        _, kwargs = cls.call_args
        self.assertEqual(kwargs["url"], "http://127.0.0.1:8090")
        self.assertIs(runtime.service.backend, fake_backend)

    def test_laya_is_a_recognized_backend_name(self):
        from amplifier_fast_decisions.runtime import get_runtime

        coordinator = SimpleNamespace(
            get_capability=lambda name: None,
            register_capability=lambda name, value: None,
            session=None,
            session_id="test-session-2",
            parent_id=None,
            hooks=None,
        )
        with mock.patch(
            "amplifier_fast_decisions.local_backend.LayaBackend",
            return_value=SimpleNamespace(name="laya", external=False),
        ):
            get_runtime(coordinator, {"backend": "laya", "events_dir": "/tmp/afast-laya-test-2"})
        # No ValueError means "laya" was accepted by the backend-name check.


# --- cli.py doctor probe -----------------------------------------------------


class LayaDoctorCheckTests(unittest.TestCase):
    def test_reachable_when_health_ok(self):
        from amplifier_fast_decisions.cli import _laya_server_check

        handler = _make_handler()
        with _running_server(handler) as base_url, mock.patch.dict(
            os.environ, {"FAST_DECISIONS_LAYA_URL": base_url}
        ):
            result = _laya_server_check()
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "reachable")
        self.assertEqual(result["check"], "laya_judge")
        self.assertEqual(
            handler.request_headers[-1]["User-Agent"],
            "amplifier-fast-decisions/0.1",
        )

    def test_unreachable_when_nothing_listening(self):
        from amplifier_fast_decisions.cli import _laya_server_check

        with mock.patch.dict(os.environ, {"FAST_DECISIONS_LAYA_URL": "http://127.0.0.1:1"}):
            result = _laya_server_check()
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "unreachable")


if __name__ == "__main__":
    unittest.main()
