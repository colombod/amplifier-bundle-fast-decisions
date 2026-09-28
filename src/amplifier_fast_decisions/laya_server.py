"""Local decide endpoint for Laya, an open-source typed-decision classifier.

Accepts Jev's exact request shape (``{state, model?, questions: {id:
{type, instructions, criteria}}}``) at ``POST /v1/systemone`` -- the same
path ``JevBackend``'s stdlib fallback transport posts to -- with
``POST /v1/decide`` kept as an alias (the path ``LayaBackend`` already
uses). Both routes answer with Jev's shape (``{model, answers: {id:
{type, noul} | {type, choice, probabilities, confidence} | {type, score,
legend, probabilities, confidence}}, usage}``): whatever ``laya_mlx``/
``laya``'s ``agent.predict()`` returns is normalized into that per-question
shape, filling in ``confidence`` via ``(n*p_max-1)/(n-1)`` (reference only)
when the agent did not report one. ``LayaBackend`` (local_backend.py) is
the client for this server and keeps reading ``probabilities`` exactly as
before -- this normalization only guarantees the wire shape, it does not
change what ``LayaBackend`` consumes.

Run:
    python -m amplifier_fast_decisions.laya_server --host 127.0.0.1 --port 8090

Imports ``laya_mlx`` (Apple Silicon) lazily, falling back to the upstream
``laya`` (PyTorch, for CUDA/CPU) if present; exits with a clear message if
neither is importable. Never silently substitutes a different model.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from typing import Any

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090
DEFAULT_MODEL = "aac6fef/laya-mlx"
DEFAULT_DTYPE = "float16"

# Jev's canonical decide path; /v1/decide is kept as an alias since
# LayaBackend (local_backend.py) already posts there.
DECIDE_PATHS = ("/v1/systemone", "/v1/decide")


def _confidence_from_probabilities(probabilities: Any) -> float | None:
    """``(n * p_max - 1) / (n - 1)`` -- reference-only confidence derived
    from a probability distribution when the agent did not report one of
    its own. ``None`` when there are fewer than two alternatives or the
    distribution is malformed."""
    if not isinstance(probabilities, dict) or len(probabilities) < 2:
        return None
    values = [
        v
        for v in probabilities.values()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    ]
    if len(values) != len(probabilities):
        return None
    n = len(values)
    p_max = max(values)
    return (n * p_max - 1) / (n - 1)


def _normalize_answer(spec: Any, raw: Any) -> Any:
    """Reshape one raw per-question answer from ``agent.predict()`` into
    Jev's ``{type, noul}`` / ``{type, choice, probabilities, confidence}``
    / ``{type, score, legend, probabilities, confidence}`` union. A
    non-dict ``raw`` answer is returned unchanged (never guessed at).
    """
    if not isinstance(raw, dict):
        return raw
    declared_type = spec.get("type") if isinstance(spec, dict) else None
    answer_type = raw.get("type", declared_type)
    if answer_type == "noul":
        return {"type": "noul", "noul": raw.get("noul")}
    probabilities = raw.get("probabilities") or {}
    confidence = raw.get("confidence")
    if confidence is None:
        confidence = _confidence_from_probabilities(probabilities)
    if answer_type == "score":
        return {
            "type": "score",
            "score": raw.get("score"),
            "legend": raw.get("legend"),
            "probabilities": probabilities,
            "confidence": confidence,
        }
    return {
        "type": "choice",
        "choice": raw.get("choice"),
        "probabilities": probabilities,
        "confidence": confidence,
    }


def _to_jev_shape(result: Any, questions: dict, default_model: str | None) -> Any:
    """Normalize ``agent.predict()``'s result into Jev's
    ``{model, answers, usage}`` response shape. A non-dict result (or one
    missing ``answers``) is returned unchanged -- this is a reshaping
    layer, not a validator; malformed output still surfaces to the caller
    instead of being silently coerced.
    """
    if not isinstance(result, dict):
        return result
    answers_raw = result.get("answers")
    if not isinstance(answers_raw, dict):
        return result
    answers = {
        name: _normalize_answer(spec, answers_raw[name])
        for name, spec in questions.items()
        if name in answers_raw
    }
    return {
        "model": result.get("model") or default_model,
        "answers": answers,
        "usage": result.get("usage") or {},
    }


def _load_agent(model: str, dtype: str) -> Any:
    """Load the Laya agent, preferring the MLX (Apple Silicon) port and
    falling back to the upstream PyTorch package. Raises ``ImportError``
    (never a silent no-op) if neither is installed."""
    try:
        import laya_mlx  # type: ignore[import-not-found]

        return laya_mlx.load(model, dtype=dtype, compile=True, cache_prompts=True)
    except ImportError:
        pass
    try:
        import laya  # type: ignore[import-not-found]

        return laya.load(model)
    except ImportError as exc:
        raise ImportError(
            "Neither laya_mlx (Apple Silicon) nor laya (PyTorch, CUDA/CPU) is "
            "importable in this Python environment. Install one, e.g.: "
            "uv pip install 'laya-mlx @ git+https://github.com/mizorewww/laya-mlx@main'"
        ) from exc


def _warm(agent: Any) -> None:
    """One tiny predict at startup so the (~2.9s on Apple Silicon) compile
    cost is paid here, not on the first real caller's request. Best-effort:
    a genuine failure still surfaces on the first real /v1/decide call."""
    try:
        agent.predict(
            "ok", {"warmup": {"type": "noul", "instructions": "warmup probe"}}
        )
    except Exception as exc:  # noqa: BLE001 -- warmup must never crash startup
        print(f"laya_server: warmup probe failed ({exc}); continuing", file=sys.stderr)


class _LayaHTTPServer(ThreadingHTTPServer):
    """Adds the loaded agent/config as typed attributes so the handler
    below never touches an untyped ``ThreadingHTTPServer`` attribute."""

    model_name: str
    expected_token: str | None
    agent: Any
    inference_lock: Any


class _Handler(BaseHTTPRequestHandler):
    server_version = "LayaDecideServer/1"

    def log_message(self, *_args, **_kwargs) -> None:
        return  # silence default request logging

    def _write_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        """No auth is required unless the server was started with
        ``--token-env`` naming an env var that is actually set (empty/unset
        means auth is not enforced, matching the rest of the local hosts'
        opt-in auth posture)."""
        token = getattr(self.server, "expected_token", None)
        if not token:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {token}"

    def do_GET(self) -> None:
        if self.path != "/health":
            self._write_json(404, {"error": "not found"})
            return
        self._write_json(
            200,
            {
                "status": "ok",
                "model": getattr(self.server, "model_name", None),
                "loaded": getattr(self.server, "agent", None) is not None,
            },
        )

    def do_POST(self) -> None:
        if self.path not in DECIDE_PATHS:
            self._write_json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._write_json(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._write_json(400, {"error": "invalid JSON body"})
            return
        state = body.get("state")
        questions = body.get("questions")
        if (
            not isinstance(state, (str, dict))
            or not isinstance(questions, dict)
            or not questions
        ):
            self._write_json(
                400,
                {"error": "body requires 'state' and a non-empty 'questions' object"},
            )
            return
        # Jev's request shape allows an optional per-request "model"; this
        # server always serves whatever model was loaded at startup
        # (--model), so the field is accepted (never a 400) but not
        # forwarded to a fixed, already-loaded agent.
        agent = getattr(self.server, "agent", None)
        if agent is None:
            self._write_json(503, {"error": "model not loaded"})
            return
        try:
            # One loaded GPU model is shared by all HTTP clients. The SDK's
            # mutable inference state and MPS command buffers are not reentrant.
            with self.server.inference_lock:
                result = agent.predict(state, questions)
        except Exception as exc:  # noqa: BLE001 -- a bad prediction must never crash the server
            self._write_json(500, {"error": f"predict failed: {exc}"})
            return
        shaped = _to_jev_shape(
            result, questions, getattr(self.server, "model_name", None)
        )
        try:
            body_bytes = json.dumps(shaped).encode("utf-8")
        except (TypeError, ValueError):
            self._write_json(500, {"error": "model returned a non-JSON-safe result"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)


def build_server(
    host: str, port: int, model: str, dtype: str, token_env: str | None
) -> _LayaHTTPServer:
    """Construct and warm the server. Raises ``SystemExit`` (after printing a
    clear message) if the Laya package cannot be loaded -- never falls back
    to serving without a model."""
    server = _LayaHTTPServer((host, port), _Handler)
    server.inference_lock = Lock()
    server.model_name = model
    server.expected_token = os.getenv(token_env) if token_env else None
    try:
        server.agent = _load_agent(model, dtype)
    except ImportError as exc:
        print(f"laya_server: {exc}", file=sys.stderr)
        server.server_close()
        raise SystemExit(1) from exc
    _warm(server.agent)
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="amplifier_fast_decisions.laya_server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--dtype", default=DEFAULT_DTYPE)
    parser.add_argument(
        "--token-env",
        default=None,
        help="Env var name carrying a bearer token to require on requests; unset means no auth.",
    )
    args = parser.parse_args(argv)
    server = build_server(args.host, args.port, args.model, args.dtype, args.token_env)
    print(
        f"laya_server: listening on http://{args.host}:{server.server_port} (model={args.model})",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
