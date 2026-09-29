"""One decision service per session; no global provider mutations."""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
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

_PLANNER_STATE_ID_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _planner_state_path(events_dir: str, session_id: str) -> Path:
    safe_id = _PLANNER_STATE_ID_RE.sub("_", session_id)[:200] or "unknown"
    return Path(events_dir).expanduser() / "planner-state" / f"{safe_id}.json"


def load_planner_state(
    events_dir: str | None, session_id: str | None
) -> tuple[dict[str, dict[str, Any]], int | None]:
    """Best-effort load of the turn planner's persisted per-session cache
    state (Runtime.planner_state / planner_last_ctx), written by
    ``save_planner_state`` after every real provider response while the
    planner is enabled. Each turn is a fresh process under
    ``amplifier run --resume``/``amplifier continue``, so without this the
    in-memory ``Runtime`` starts empty every turn and every model looks
    cold forever -- see docs/proposals/TURN-PLANNER.md.

    Never raises: a missing ``events_dir``/``session_id``, a missing file,
    a corrupt/malformed file, or any I/O error all fall back to an empty
    state (today's fresh-process behavior) -- a persistence failure must
    never fail the turn that triggered it.
    """
    if not events_dir or not session_id:
        return {}, None
    try:
        raw = _planner_state_path(events_dir, session_id).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return {}, None
    models = data.get("models") if isinstance(data, dict) else None
    state: dict[str, dict[str, Any]] = {}
    if isinstance(models, dict):
        for model, entry in models.items():
            if not isinstance(model, str) or not isinstance(entry, dict):
                continue
            last_used_at = entry.get("last_used_at")
            cached_tokens = entry.get("cached_tokens")
            if (
                isinstance(last_used_at, (int, float))
                and not isinstance(last_used_at, bool)
                and isinstance(cached_tokens, int)
                and not isinstance(cached_tokens, bool)
            ):
                state[model] = {
                    "last_used_at": float(last_used_at),
                    "cached_tokens": cached_tokens,
                }
    last_ctx = data.get("last_ctx") if isinstance(data, dict) else None
    if isinstance(last_ctx, bool) or not isinstance(last_ctx, int):
        last_ctx = None
    return state, last_ctx


def save_planner_state(
    events_dir: str | None,
    session_id: str | None,
    state: dict[str, dict[str, Any]],
    last_ctx: int | None,
) -> None:
    """Best-effort ATOMIC write (tmp file + ``os.replace``) of the turn
    planner's cache state. Never raises -- an I/O error here must never
    fail the turn that triggered it; the next process simply falls back
    to loading whatever was last written successfully (or nothing).
    """
    if not events_dir or not session_id:
        return
    try:
        path = _planner_state_path(events_dir, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"models": state, "last_ctx": last_ctx})
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


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


def _schedule_backend_close(backend: Any) -> None:
    """Best-effort, non-blocking close of a backend that was replaced before
    any turn used it. Never raises."""
    close = getattr(backend, "close", None)
    if close is None or not asyncio.iscoroutinefunction(close):
        return

    async def _run() -> None:
        try:
            await close()
        except Exception:
            _logger.debug(
                "backend close failed for %s", getattr(backend, "name", backend), exc_info=True
            )

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
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
                 shadow_capacity: int = 64, shadow_drain_ms: int = 2000,
                 events_dir: str | None = None, session_id: str | None = None,
                 parent_session_id: str | None = None):
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
        # Turn planner (model_routing.planner, opt-in): per-session,
        # per-model cache state -- {model: {"last_used_at": float,
        # "cached_tokens": int}} -- fed from every real provider
        # response's usage while the planner is configured (see
        # orchestrator.RoutedProvider.complete); read (never written) by
        # planner.plan_turn(). planner_last_ctx is the most recently
        # reported total prompt size (any model), the turn planner's
        # ctx estimate for the NEXT request when available. Both stay
        # empty/None -- and unread -- unless model_routing.planner is
        # enabled. See planner.py and docs/proposals/TURN-PLANNER.md.
        self.planner_state: dict[str, dict[str, Any]] = {}
        self.planner_last_ctx: int | None = None
        # events_dir/session_id identify where/under what key persisted
        # planner state lives on disk (<events_dir>/planner-state/<session_id>.json)
        # -- None (the default, e.g. demo.py/tests) means persistence is
        # inert: ensure_planner_state_loaded/persist_planner_state become
        # no-ops and the planner behaves exactly as an in-memory-only
        # session. _planner_state_loaded guards a single lazy load attempt
        # per process, made only the first time the planner actually runs
        # this turn (orchestrator.RoutedProvider.complete), not at mount.
        # parent_session_id (None for a root session) is the same signal
        # session_identity() reads -- exposed here for the lookahead's
        # session-kind detection (orchestrator._session_kind).
        self.events_dir = events_dir
        self.session_id = session_id
        self.parent_session_id = parent_session_id
        self._planner_state_loaded = False

    def ensure_planner_state_loaded(self) -> None:
        """Lazily load persisted turn-planner cache state ONCE per process,
        the first time the planner actually runs. Each turn is a fresh
        process under ``amplifier run --resume``/``amplifier continue``;
        without this, ``planner_state``/``planner_last_ctx`` would start
        empty every turn and every model would look cold forever. A no-op
        if events_dir/session_id are unset, if already attempted this
        process, or if in-memory state is already populated (e.g. a caller
        -- a test -- seeded it directly before the planner ran)."""
        if self._planner_state_loaded:
            return
        self._planner_state_loaded = True
        if self.planner_state or self.planner_last_ctx is not None:
            return
        state, last_ctx = load_planner_state(self.events_dir, self.session_id)
        if state:
            self.planner_state = state
        if last_ctx is not None:
            self.planner_last_ctx = last_ctx

    def persist_planner_state(self) -> None:
        """Best-effort save of the current in-memory turn-planner cache
        state, called after every response that updated it. A no-op if
        events_dir/session_id are unset (matching every other HC0x seam:
        inert unless the planner is actually configured with session
        context available)."""
        save_planner_state(
            self.events_dir, self.session_id, self.planner_state, self.planner_last_ctx
        )

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


def _build_backend(config: dict[str, Any], policy: Policy) -> Any:
    """The judge backend a module's config asks for. Raises ValueError on a bad config."""
    backend_name = config.get("backend") or _env_backend_default() or "jev"
    if backend_name == "none":  # readable alias: routing-only, no judge
        backend_name = "unavailable"
    if backend_name not in {"jev", "unavailable", "deterministic", "ollama", "mlx", "hosted", "gateway", "laya", "anyjev"}:
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

        level = config.get("anyjev_level", "L2")
        if policy.mode == "active" and level == "L0":
            raise ValueError("AnyJev L0 is uncalibrated; use shadow evaluation or fit L1/L2 first")
        backend = AnyJevBackend(model=config.get("model"),
                                url=config.get("anyjev_url", DEFAULT_URL),
                                level=level, timeout_ms=policy.timeout_ms)
    elif backend_name == "mlx":
        from .local_backend import MlxBackend, mlx_base_url

        backend = MlxBackend(
            model=config.get("model") or os.getenv("FAST_DECISIONS_LOCAL_MODEL") or "mlx-community/Qwen3-0.6B-4bit",
            url=config.get("mlx_url") or mlx_base_url(),
            timeout_ms=policy.timeout_ms,
        )
    elif backend_name == "laya":
        from .local_backend import LayaBackend

        backend = LayaBackend(
            url=config.get("laya_url"),
            timeout_ms=policy.timeout_ms,
            token_env=config.get("laya_token_env"),
        )
    elif backend_name in ("hosted", "gateway"):  # "gateway" is a legacy alias
        from .local_backend import HOSTED_DEFAULT_TOKEN_ENV, HostedBackend

        model = config.get("model") or os.getenv("FAST_DECISIONS_HOSTED_MODEL")
        if not model:
            raise ValueError("Hosted backend requires a model in config")
        key_env = (
            config.get("hosted_token_env")
            or config.get("gateway_key_env")  # legacy alias
            or HOSTED_DEFAULT_TOKEN_ENV
        )
        backend = HostedBackend(
            model=model,
            url=config.get("hosted_url") or config.get("gateway_url"),  # "gateway_url" is a legacy alias
            timeout_ms=policy.timeout_ms,
            api_key=os.getenv(key_env),
        )
    elif backend_name == "ollama":
        from .local_backend import OllamaBackend

        backend = OllamaBackend(
            model=config.get("model") or os.getenv("FAST_DECISIONS_LOCAL_MODEL") or "qwen3:0.6b",
            url=config.get("ollama_url", "http://127.0.0.1:11434"),
            timeout_ms=policy.timeout_ms,
        )
    elif backend_name == "deterministic":
        # In-process, offline scorer (external=False, never gated by
        # allow_external_state): the "shadow, external=false" rung on the
        # design's rung ladder (docs/design/redesign-2026-09-17.md P3
        # decision 5). Explicitly opted into via config -- not a silent
        # substitution for jev.
        backend = ScriptedBackend()
    else:
        backend = UnavailableBackend()
    return backend


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
            # mounts with config: {}) might build the runtime first. Telemetry
            # remains the one-per-session singleton regardless of who built it.
            #
            # The registry owner (hooks-fast-decisions-router) is itself a
            # hook, so hook config order CAN put the shadow observer first.
            # Its backend (e.g. deterministic) must not decide active turns:
            # an owner that names a backend replaces it. An owner that names
            # none keeps the existing one (and its safety checks below).
            owner_policy = Policy.from_config(config)
            if not existing.orchestrator_owned and config.get("backend"):
                previous = existing.service.backend
                existing.service.backend = _build_backend(config, owner_policy)
                _schedule_backend_warmup(existing.service.backend)
                _schedule_backend_close(previous)
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
    try:
        backend = _build_backend(config, policy)
    except Exception:
        recorder.close()
        raise
    _schedule_backend_warmup(backend)
    service = DecisionService(policy, backend, emitter, coordinator, config.get("candidates"))
    runtime = Runtime(service, recorder, shadow_capacity=config.get("shadow_capacity", 64),
                       shadow_drain_ms=config.get("shadow_drain_ms", 2000),
                       events_dir=events_dir, session_id=session_id,
                       parent_session_id=parent)
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
