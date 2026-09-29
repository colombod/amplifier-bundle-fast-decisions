"""Authenticated, bounded serving for a fixed PyTorch Laya checkpoint.

Separate from the laptop server. Requires a client-token hash file, a pinned
checkpoint and explicit device. The reverse proxy owns public TLS. No prompts,
tokens, model outputs or exception messages are logged by this adapter.
"""
import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import time

MODEL = "convaiinnovations/laya"
REVISION = "55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851"
MAX_BODY = 65536


def read_clients(path):
    """Reload on each request so atomic replacement revokes keys immediately."""
    raw = Path(path).read_text() if path else os.environ.get("LAYA_CLIENT_HASHES_JSON", "")
    clients = json.loads(raw)
    if not isinstance(clients, dict) or not 1 <= len(clients) <= 1000:
        raise ValueError("Expected 1..1000 named client token hashes")
    for name, digest in clients.items():
        if (not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name)
                or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)):
            raise ValueError("Invalid client token hash file")
    if len(set(clients.values())) != len(clients):
        raise ValueError("Each client must have a distinct token")
    return clients


def validate(body):
    if not isinstance(body, dict) or set(body) - {"state", "questions", "model"}:
        raise ValueError("Expected state and questions")
    state, questions = body.get("state"), body.get("questions")
    if not isinstance(state, (str, dict)):
        raise ValueError("Expected text or object state")
    text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
    if not text or len(text) > 3000:
        raise ValueError("State must contain 1..3000 characters")
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 16:
        raise ValueError("Expected 1..16 questions")
    for name, spec in questions.items():
        if not isinstance(name, str) or not 1 <= len(name) <= 64 or not isinstance(spec, dict):
            raise ValueError("Invalid question")
        if set(spec) - {"type", "instructions", "criteria"}:
            raise ValueError("Unknown question fields")
        if spec.get("type") not in {"choice", "score", "noul"}:
            raise ValueError("Unsupported question type")
        instructions = spec.get("instructions")
        if not isinstance(instructions, str) or not 1 <= len(instructions) <= 512:
            raise ValueError("Question instructions must contain 1..512 characters")
        criteria = spec.get("criteria")
        if spec["type"] == "noul":
            if criteria is not None:
                raise ValueError("Noul questions have no criteria")
        else:
            expected = dict if spec["type"] == "choice" else list
            if not isinstance(criteria, expected) or not 2 <= len(criteria) <= 16:
                raise ValueError("Expected 2..16 alternatives")
            values = list(criteria) + list(criteria.values()) if isinstance(criteria, dict) else criteria
            if any(not isinstance(v, str) or not 1 <= len(v) <= 512 for v in values):
                raise ValueError("Invalid alternative label")
    return state, questions


def load_agent(device, revision):
    import laya
    agent = laya.load(MODEL, device=device, revision=revision)
    agent.predict("warmup", {"ready": {"type": "noul", "instructions": "Is the service ready?"}})
    if str(agent.device).split(":")[0] != device:
        raise RuntimeError("Requested device unavailable; refusing silent device fallback")
    return agent


def create_app(*, clients_file=None, device="cuda", revision=REVISION,
               max_inflight=4, requests_per_minute=600, deadline_s=10, _agent=None):
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse

    clients_file = clients_file or os.environ.get("LAYA_CLIENTS_FILE")
    if not clients_file and not os.environ.get("LAYA_CLIENT_HASHES_JSON"):
        raise ValueError("Client token hashes are required; anonymous serving is disabled")
    read_clients(clients_file)  # Fail before loading weights or listening.
    if device not in {"cuda", "cpu"} or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Require cpu/cuda and an exact checkpoint commit")
    if not 1 <= max_inflight <= 32 or not 1 <= requests_per_minute <= 6000 or not 0 < deadline_s <= 60:
        raise ValueError("Invalid serving limits")
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="laya")
    agent = None
    active = 0
    rates = {}
    counters = {"completed": 0, "failed": 0, "busy": 0, "rate_limited": 0}

    @asynccontextmanager
    async def lifespan(app):
        nonlocal agent
        try:
            agent = _agent if _agent is not None else await asyncio.to_thread(load_agent, device, revision)
            yield
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health():
        return JSONResponse({"status": "ok" if agent else "loading", "loaded": agent is not None,
                             "model": MODEL, "revision": revision,
                             "device": str(getattr(agent, "device", "unavailable")),
                             "inflight": active, **counters}, status_code=200 if agent else 503)

    @app.post("/v1/decide")
    @app.post("/v1/systemone")
    async def decide(request: Request):
        nonlocal active
        try:
            clients = read_clients(clients_file)
        except (OSError, ValueError):
            raise HTTPException(503, "Client authorization unavailable") from None
        supplied = request.headers.get("authorization", "")
        digest = hashlib.sha256(supplied.removeprefix("Bearer ").encode()).hexdigest()
        client = None
        for name, expected in clients.items():
            if hmac.compare_digest(digest, expected) and supplied.startswith("Bearer "):
                client = name
        if client is None:
            raise HTTPException(401, "Invalid or missing bearer token")
        # Drop revoked identities, keeping the rate ledger bounded by the key file.
        for name in set(rates) - set(clients):
            del rates[name]
        now = time.monotonic()
        history = rates.setdefault(client, deque())
        while history and history[0] <= now - 60:
            history.popleft()
        if len(history) >= requests_per_minute:
            counters["rate_limited"] += 1
            raise HTTPException(429, "Client rate limit", headers={"Retry-After": "60"})
        history.append(now)
        if agent is None or active >= max_inflight:
            counters["busy"] += 1
            raise HTTPException(503, "Service busy", headers={"Retry-After": "1"})
        active += 1
        owns_slot = True

        def release(_future):
            nonlocal active
            active -= 1

        try:
            raw = bytearray()
            async with asyncio.timeout(min(5, deadline_s)):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > MAX_BODY:
                        raise HTTPException(413, "Request too large")
                    raw.extend(chunk)
            try:
                body = json.loads(raw)
                state, questions = validate(body)
            except (ValueError, TypeError, RecursionError):
                raise HTTPException(400, "Invalid bounded decision request") from None
            # The client cannot select another checkpoint or override device/token budgets.
            loop = asyncio.get_running_loop()
            started = time.monotonic()
            def predict():
                before = getattr(agent, "cpu_fallback_count", 0)
                result = agent.predict(state, questions)
                if device == "cuda" and getattr(agent, "cpu_fallback_count", 0) != before:
                    raise RuntimeError("CUDA prediction fell back to CPU")
                return result

            future = pool.submit(predict)
            future.add_done_callback(lambda f: loop.call_soon_threadsafe(release, f))
            owns_slot = False  # Cancellation cannot free a running GPU slot.
            wrapped = asyncio.wrap_future(future)
            # A disconnected/timed-out caller may leave an inference exception
            # unobserved. Retrieve it without logging exception text or payloads.
            wrapped.add_done_callback(lambda f: None if f.cancelled() else f.exception())
            try:
                result = await asyncio.wait_for(asyncio.shield(wrapped), deadline_s)
            except BaseException:
                future.cancel()  # Cancels queued work; running inference retains its slot.
                raise
            if not isinstance(result, dict) or not result.get("model") or not isinstance(result.get("answers"), dict):
                raise ValueError("Invalid model result")
            result = {"model": result["model"], "answers": result["answers"],
                      "usage": result.get("usage", {}),
                      "checkpoint": MODEL, "checkpoint_revision": revision,
                      "device": str(getattr(agent, "device", "unavailable"))}
            response = JSONResponse(result, headers={"X-Inference-Time-Ms": str(round((time.monotonic()-started)*1000, 2))})
            counters["completed"] += 1
            return response
        except TimeoutError:
            counters["failed"] += 1
            raise HTTPException(504, "Decision deadline exceeded") from None
        except HTTPException:
            raise
        except Exception:
            counters["failed"] += 1
            raise HTTPException(500, "Decision failed") from None
        finally:
            if owns_slot:
                active -= 1

    return app


def main():
    import uvicorn
    app = create_app(device=os.getenv("LAYA_DEVICE", "cuda"),
                     revision=os.getenv("LAYA_MODEL_REVISION", REVISION),
                     max_inflight=int(os.getenv("LAYA_MAX_INFLIGHT", "4")),
                     requests_per_minute=int(os.getenv("LAYA_REQUESTS_PER_MINUTE", "600")),
                     deadline_s=float(os.getenv("LAYA_DEADLINE_SECONDS", "10")))
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8090")),
                access_log=False, limit_concurrency=32, timeout_keep_alive=5)


if __name__ == "__main__":
    main()
