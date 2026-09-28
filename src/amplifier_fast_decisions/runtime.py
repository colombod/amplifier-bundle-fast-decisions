"""One decision service per session; no global provider mutations."""
from __future__ import annotations
import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from .backends import JevBackend, ScriptedBackend, UnavailableBackend
from .contracts import SERVICE_CAPABILITY, RUNTIME_CAPABILITY, EVENT_NAMES, Policy
from .service import DecisionService
from .shadow import ShadowJob, ShadowOutcome, ShadowWorker
from .telemetry import Emitter, JsonlRecorder

_logger = logging.getLogger(__name__)


def _schedule_backend_warmup(backend: Any) -> None:
    """Best-effort, non-blocking warmup at mount time.

    Ollama's warmup absorbs the model reload, Jev's opens the TLS
    connection, Mlx/Laya/Hosted hit their health endpoints -- all costly
    on the first real decision if paid there instead of here. Deterministic/
    unavailable/scripted backends have no ``warmup`` coroutine and are
    skipped naturally by the ``iscoroutinefunction`` check below.

    Never raises and never blocks the mount: any failure is swallowed and
    logged at debug. The first real decision simply pays the handshake
    cost itself, same as if warmup had never run.
    """
    warmup = getattr(backend, "warmup", None)
    if warmup is None or not asyncio.iscoroutinefunction(warmup):
        return

    async def _run() -> None:
        try:
            await warmup()
        except Exception:
            _logger.debug(
                "backend warmup failed for %s", getattr(backend, "name", backend), exc_info=True
            )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        loop.create_task(_run())
    else:
        threading.Thread(target=lambda: asyncio.run(_run()), daemon=True).start()


def _env_backend_default() -> str | None:
    """Environment-level backend default, consulted only when a profile
    omits ``backend`` (profile config always wins). ``FAST_DECISIONS_JUDGE``
    selects the judge family (``local``/``hosted``/``jev``/``deterministic``);
    ``local`` is disambiguated by ``FAST_DECISIONS_LOCAL_HOST``
    (``ollama`` (default), ``mlx``, or ``laya``). See .env.example.
    """
    judge = os.getenv("FAST_DECISIONS_JUDGE")
    if not judge:
        return None
    if judge == "local":
        return os.getenv("FAST_DECISIONS_LOCAL_HOST", "ollama")
    return judge


def session_identity(coordinator: Any) -> tuple[str, str | None]:
    session = getattr(coordinator, "session", None)
    session_id = getattr(coordinator, "session_id", None) or getattr(session, "session_id", None)
    parent = getattr(coordinator, "parent_id", None) or getattr(session, "parent_id", None)
    return str(session_id or uuid4().hex), str(parent) if parent else None


class Runtime:
    def __init__(self, service: DecisionService, recorder: JsonlRecorder | None = None, *,
                 shadow_capacity: int = 64, shadow_drain_ms: int = 2000):
        self.service = service
        self.recorder = recorder
        self.lock = asyncio.Lock()
        self.closed = False
        self.shadow_worker = ShadowWorker(service, shadow_capacity)
        self.shadow_drain_ms = shadow_drain_ms
        self._shadow_task: asyncio.Task | None = None
        # True once loop-fast-decisions mounted against this runtime: the
        # orchestrator is then on the real request path, so the hook's
        # shadow scorer/role router would only duplicate (and pay for)
        # decisions the orchestrator already makes or routes.
        self.orchestrator_owned = False
        # Set by the first module that installs the auto-observatory
        # session:start handler, so orchestrator + hook never both launch it.
        self.observatory_installed = False

    def start_shadow_worker(self) -> None:
        """Idempotent. The runtime's creator owns this task (mirrors runtime
        lifecycle ownership: whichever module builds the Runtime also closes
        it -- see get_runtime's owner semantics below).

        Runtime/get_runtime can be constructed outside a running event loop
        (e.g. a synchronous test wiring a fake runtime via get_runtime), so
        this silently no-ops rather than raising when there is no loop to
        schedule onto; submit_shadow/resolve_shadow call this again lazily,
        and by the time either fires on the real hook path a loop is always
        running."""
        if self._shadow_task is not None and not self._shadow_task.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._shadow_task = None
            return
        self._shadow_task = asyncio.create_task(self.shadow_worker.run())

    def submit_shadow(self, job: ShadowJob) -> bool:
        """Non-blocking enqueue. False when the queue is full (counted as dropped_shadow_jobs)."""
        self.start_shadow_worker()
        return self.shadow_worker.submit(job)

    def resolve_shadow(self, outcome: ShadowOutcome) -> None:
        """Non-blocking. Records what the LLM actually did against a pending proposal."""
        self.start_shadow_worker()
        self.shadow_worker.resolve(outcome)

    async def close(self):
        """Idempotent. Drains the shadow worker with a bounded budget, then
        cancels the task and awaits it, so no shadow work outlives the
        session and no task is left pending at interpreter exit."""
        if self.closed:
            return
        self.closed = True
        try:
            await self.service.emit("health", {
                **(self.recorder.health if self.recorder else {}),
                **self.shadow_worker.health,
            })
            if self._shadow_task is not None:
                try:
                    await asyncio.wait_for(self.shadow_worker.join(), timeout=self.shadow_drain_ms / 1000)
                except TimeoutError:
                    pass
                self._shadow_task.cancel()
                try:
                    await self._shadow_task
                except asyncio.CancelledError:
                    pass
            await self.service.close()
        finally:
            if self.recorder:
                await asyncio.to_thread(self.recorder.close)


def get_runtime(coordinator: Any, config: dict[str, Any], *, owner: bool = False) -> tuple[Runtime, bool]:
    existing = coordinator.get_capability(RUNTIME_CAPABILITY)
    if existing is not None:
        if not isinstance(existing, Runtime):
            raise TypeError("fast_decisions.runtime has an incompatible implementation")
        if owner:
            # The orchestrator is the sole owner of decision policy (mode,
            # thresholds, allowed_tools). Mount order across module types IS
            # a documented, guaranteed kernel contract (@core:CONTRACTS.md
            # Module Lifecycle; amplifier_core's _session_init.py loads
            # orchestrator (:74) before context (:104), providers (:154),
            # tools (:222) and hooks (:248)) -- within a session the
            # orchestrator always builds the runtime before the hook mounts,
            # so no race exists to solve here. This re-apply is defensive,
            # not a race resolution: it protects out-of-session construction
            # paths (unit tests, `afast demo`, a future non-kernel host)
            # where a non-owning module (e.g. the observer hook, which
            # mounts with config: {}) might build the runtime first. Backend/
            # telemetry remain the one-per-session singleton regardless of
            # who built them first.
            owner_policy = Policy.from_config(config)
            if owner_policy.mode == "active" and existing.service.backend.name == "anyjev-L0":
                raise ValueError("AnyJev L0 is uncalibrated; use shadow evaluation or fit L1/L2 first")
            existing.service.policy = owner_policy
            existing.orchestrator_owned = True
        return existing, False
    policy = Policy.from_config(config)
    session_id, parent = session_identity(coordinator)
    events_dir = config.get("events_dir") or os.getenv("AFAST_EVENTS_DIR") or str(
        Path.home() / ".amplifier" / "fast-decisions" / "events")
    recorder = JsonlRecorder(events_dir, session_id)
    emitter = Emitter(session_id, parent_session_id=parent, hooks=coordinator.hooks, recorder=recorder)
    backend_name = config.get("backend") or _env_backend_default() or "jev"
    if backend_name == "none":  # readable alias: routing-only, no judge
        backend_name = "unavailable"
    if backend_name not in {"jev", "unavailable", "deterministic", "ollama", "mlx", "hosted", "gateway", "laya", "anyjev"}:
        recorder.close()
        raise ValueError(
            "Backend must be jev, deterministic, ollama, mlx, hosted (alias gateway), laya, anyjev, or unavailable"
        )
    if backend_name == "jev":
        # jev_url / jev_url_env / jev_key_env point the Jev client at any
        # other Jev System One-compatible server; backend_label names it.
        backend = JevBackend(
            model=config.get("model"),
            timeout_ms=policy.timeout_ms,
            base_url=config.get("jev_url"),
            base_url_env=config.get("jev_url_env"),
            api_key_env=config.get("jev_key_env"),
            label=config.get("backend_label"),
        )
    elif backend_name == "anyjev":
        from .anyjev_backend import AnyJevBackend, DEFAULT_URL

        try:
            level = config.get("anyjev_level", "L2")
            if policy.mode == "active" and level == "L0":
                raise ValueError("AnyJev L0 is uncalibrated; use shadow evaluation or fit L1/L2 first")
            backend = AnyJevBackend(model=config.get("model"),
                                    url=config.get("anyjev_url", DEFAULT_URL),
                                    level=level, timeout_ms=policy.timeout_ms)
        except Exception:
            recorder.close()
            raise
    elif backend_name == "mlx":
        from .local_backend import MlxBackend, mlx_base_url

        backend = MlxBackend(
            model=config.get("model") or os.getenv("FAST_DECISIONS_LOCAL_MODEL") or "mlx-community/Qwen3-0.6B-4bit",
            url=config.get("mlx_url") or mlx_base_url(),
            timeout_ms=policy.timeout_ms,
        )
    elif backend_name == "laya":
        from .local_backend import LayaBackend

        try:
            backend = LayaBackend(
                url=config.get("laya_url"),
                timeout_ms=policy.timeout_ms,
                token_env=config.get("laya_token_env"),
            )
        except Exception:
            recorder.close()
            raise
    elif backend_name in ("hosted", "gateway"):  # "gateway" is a legacy alias
        from .local_backend import HOSTED_DEFAULT_TOKEN_ENV, HostedBackend

        model = config.get("model") or os.getenv("FAST_DECISIONS_HOSTED_MODEL")
        if not model:
            recorder.close()
            raise ValueError("Hosted backend requires a model in config")
        key_env = (
            config.get("hosted_token_env")
            or config.get("gateway_key_env")  # legacy alias
            or HOSTED_DEFAULT_TOKEN_ENV
        )
        try:
            backend = HostedBackend(
                model=model,
                url=config.get("hosted_url") or config.get("gateway_url"),  # "gateway_url" is a legacy alias
                timeout_ms=policy.timeout_ms,
                api_key=os.getenv(key_env),
            )
        except Exception:
            recorder.close()
            raise
    elif backend_name == "ollama":
        from .local_backend import OllamaBackend

        try:
            backend = OllamaBackend(
                model=config.get("model") or os.getenv("FAST_DECISIONS_LOCAL_MODEL") or "qwen3:0.6b",
                url=config.get("ollama_url", "http://127.0.0.1:11434"),
                timeout_ms=policy.timeout_ms,
            )
        except Exception:
            recorder.close()
            raise
    elif backend_name == "deterministic":
        # In-process, offline scorer (external=False, never gated by
        # allow_external_state): the "shadow, external=false" rung on the
        # design's rung ladder (docs/design/redesign-2026-09-17.md P3
        # decision 5). Explicitly opted into via config -- not a silent
        # substitution for jev.
        backend = ScriptedBackend()
    else:
        backend = UnavailableBackend()
    _schedule_backend_warmup(backend)
    service = DecisionService(policy, backend, emitter, coordinator, config.get("candidates"))
    runtime = Runtime(service, recorder, shadow_capacity=config.get("shadow_capacity", 64),
                       shadow_drain_ms=config.get("shadow_drain_ms", 2000))
    # The runtime's creator (whichever module calls get_runtime first this
    # session) owns the shadow worker's lifecycle: it starts the task here
    # and drains/cancels it in Runtime.close, which the same module's
    # cleanup calls (observer.py/orchestrator.py both do this today).
    runtime.orchestrator_owned = owner
    runtime.start_shadow_worker()
    coordinator.register_capability(RUNTIME_CAPABILITY, runtime)
    coordinator.register_capability(SERVICE_CAPABILITY, service)
    if hasattr(coordinator, "register_contributor"):
        coordinator.register_contributor("observability.events", "fast-decisions", lambda: list(EVENT_NAMES))
    return runtime, True
