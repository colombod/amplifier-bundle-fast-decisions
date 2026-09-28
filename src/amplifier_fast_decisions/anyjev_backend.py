"""Bounded, loopback-only client for the optional AnyJev question server.

The host stays dependency-free. A separate process owns the model and fitted
artifacts; every response must match the requested model, question and level.
This backend judges fixed questions, never selects prepared tool actions.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import math
from urllib.parse import urlsplit

from .backends import BackendUnavailable
from .contracts import Answer, Decision, DecisionRequest, DecisionResult, SLOW, canonical, digest
from .local_backend import _validate_loopback_origin
from .privacy import scrub

LEVELS = {"L0": 0, "L1": 1, "L2": 2}
DEFAULT_URL = "http://127.0.0.1:8091"
MAX_BODY = 65536
STATE_CHARS = 12000


def question_spec(question):
    if question.type not in {"choice", "noul"}:
        raise BackendUnavailable("AnyJev routing supports choice and noul questions")
    if question.type == "choice" and not 2 <= len(question.criteria) <= 26:
        raise BackendUnavailable("AnyJev requires 2..26 choice options")
    # Ordered pairs preserve option order through JSON serialization.
    return {"name": question.name, "type": question.type,
            "instructions": question.instructions, "criteria": [[k, v] for k, v in question.criteria.items()]}


def bounded_state(state):
    return scrub(canonical(state), STATE_CHARS)


class AnyJevBackend:
    external = False

    def __init__(self, *, model: str, url: str = DEFAULT_URL, level: str = "L2", timeout_ms: int = 750):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("AnyJev requires an explicit served model identity")
        if level not in LEVELS:
            raise ValueError("AnyJev level must be L0, L1 or L2")
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
            raise ValueError("AnyJev timeout_ms must be positive")
        self.url = _validate_loopback_origin(url)
        self.model, self.level, self.timeout_ms = model, level, timeout_ms
        self.name = f"anyjev-{level}"
        # One outstanding network worker, including after caller cancellation.
        self._pending = None

    def _post(self, body):
        origin = urlsplit(self.url)
        conn = http.client.HTTPConnection(origin.hostname, origin.port or 80,
                                          timeout=self.timeout_ms / 1000)
        try:
            conn.request("POST", "/v1/decide", body=body, headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            raw = response.read(MAX_BODY + 1)
            if response.status != 200 or len(raw) > MAX_BODY:
                raise BackendUnavailable("AnyJev endpoint refused the decision")
            return json.loads(raw)
        except Exception:
            # Never surface response bodies, state or server exception messages.
            raise BackendUnavailable("AnyJev endpoint unavailable or invalid") from None
        finally:
            conn.close()

    async def ask(self, request: DecisionRequest) -> DecisionResult:
        if request.candidates or not 1 <= len(request.questions) <= 8:
            raise BackendUnavailable("AnyJev accepts 1..8 fixed questions and no action candidates")
        specs = [question_spec(q) for q in request.questions]
        if len({s["name"] for s in specs}) != len(specs):
            raise BackendUnavailable("AnyJev question names must be unique")
        body = json.dumps({"model": self.model, "level": self.level,
                           "state": bounded_state(request.state), "questions": specs}).encode()
        if len(body) > MAX_BODY:
            raise BackendUnavailable("AnyJev request exceeds size limit")
        if self._pending is not None and not self._pending.done():
            raise BackendUnavailable("AnyJev previous request is still running")
        self._pending = asyncio.create_task(asyncio.to_thread(self._post, body))
        # Retrieve a late exception after timeout/cancellation without logging it.
        self._pending.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        done, _ = await asyncio.wait({self._pending}, timeout=self.timeout_ms / 1000)
        if not done:
            raise TimeoutError("AnyJev decision deadline exceeded")
        payload = self._pending.result()
        try:
            if payload["model"] != self.model or payload["level"] != self.level:
                raise ValueError("identity")
            raw_answers = payload["answers"]
            if set(raw_answers) != {s["name"] for s in specs}:
                raise ValueError("answers")
            answers = {}
            for q, spec in zip(request.questions, specs):
                raw = raw_answers[q.name]
                if raw["question_hash"] != digest(spec) or raw["level"] != self.level:
                    raise ValueError("question identity or calibration level")
                probs = raw["probabilities"]
                keys = set(q.criteria) if q.type == "choice" else {"true", "false"}
                if set(probs) != keys or any(isinstance(p, bool) or not isinstance(p, (int, float))
                                            or not math.isfinite(p) or not 0 <= p <= 1
                                            for p in probs.values()):
                    raise ValueError("probability")
                if not math.isclose(sum(probs.values()), 1.0, abs_tol=1e-6):
                    raise ValueError("mass")
                answers[q.name] = (Answer(noul=probs["true"]) if q.type == "noul" else
                                   Answer(probabilities=probs, confidence=max(probs.values())))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise BackendUnavailable("AnyJev response failed identity or probability validation") from None
        action = Decision(choice=SLOW, probabilities={SLOW: 1.0}, model=self.model,
                          probability_kind=f"anyjev_{self.level}", confidence_kind="not_reported")
        # Token usage is unknown: upstream does not expose reliable accounting.
        return DecisionResult(action=action, answers=answers, model=self.model)

    ask_many = ask

    async def close(self):
        # Socket workers are bounded by timeout_ms; no model lives in the host.
        pass
