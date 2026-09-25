"""Optional native-event bridge; observes but never grants permission.

Also runs the shadow scorer (P3 in docs/design/redesign-2026-09-17.md):
watches provider:request / tool:pre / tool:post / provider:error /
execution:end to measure "what would we have chosen" without ever touching
the turn. The snapshot (context read, candidate collection, hashing) runs on
the caller's coroutine and is hard-bounded; only backend scoring is deferred
to Runtime's shadow worker. Every handler always returns "continue" and
never raises into the hook chain -- a bug in shadow measurement must never
affect a real turn.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from .candidates import collect_candidates
from .contracts import digest, field_value
from .observatory import ensure_viewer, open_page, read_state, viewer_is_alive
from . import provenance
from .router import RoleRouter
from .runtime import get_runtime, session_identity
from .shadow import ShadowJob, ShadowOutcome
from .state import build_state

DEFAULT_OBSERVATORY_STATE_FILE = (
    Path.home() / ".amplifier" / "fast-decisions" / "serve.json"
)

__amplifier_module_type__ = "hook"
_HEARTBEAT_INTERVAL_SECONDS = 15


def _backend_label(backend) -> str | None:
    """A configured display name for the backend (e.g. a Jev-compatible
    server), or None. Operator-set config, never derived from state."""
    label = getattr(backend, "label", None)
    return label.strip()[:64] or None if isinstance(label, str) else None


_SURFACES = {
    "amplifier": "Amplifier CLI",
    "amplifier-tui": "Amplifier TUI",
    "amplifier-runtime": "Amplifier runtime",
    "amplifierd": "Amplifier daemon",
    "amplifier-app-studio": "Amplifier Studio",
}


def harness_name(config: dict | None = None) -> str:
    """Which Amplifier surface is running this session, from the launching
    executable's name (never its path). ``harness`` in config overrides."""
    configured = (config or {}).get("harness")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()[:40]
    try:
        exe = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
    except Exception:  # noqa: BLE001
        exe = ""
    if exe in ("-m", "-c", "python", "python3") or exe.startswith("python"):
        exe = ""
    return _SURFACES.get(exe, "Amplifier")


def _session_dir(coordinator) -> Path | None:
    try:
        value = coordinator.get_capability("session.working_dir")
    except Exception:  # noqa: BLE001
        return None
    return Path(value) if isinstance(value, (str, os.PathLike)) and str(value) else None


def repo_context(start: Path | None = None) -> dict:
    """``{"repo", "subdir", "branch"}`` for the session's git checkout, read
    from ``.git`` without running git. Only names -- the repository folder
    name, at most two path components below it, and the branch -- never an
    absolute path. Empty when the directory is not inside a git checkout."""
    try:
        here = (start or Path.cwd()).resolve()
    except OSError:
        return {}
    for depth, candidate in enumerate([here, *here.parents]):
        if depth > 30:
            break
        git = candidate / ".git"
        if not git.exists():
            continue
        info: dict = {"repo": candidate.name[:120]}
        rel = here.relative_to(candidate).parts
        if rel:
            info["subdir"] = "/".join(rel[:2])[:120]
        try:
            gitdir = git
            if git.is_file():  # worktree / submodule: "gitdir: <path>"
                text = git.read_text(encoding="utf-8", errors="replace").strip()
                if text.startswith("gitdir:"):
                    gitdir = (candidate / text.split(":", 1)[1].strip()).resolve()
            head = (gitdir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
            if head.startswith("ref: refs/heads/"):
                info["branch"] = head[len("ref: refs/heads/"):][:80]
        except OSError:
            pass
        return info
    return {}


def workspace_name(config: dict | None = None) -> str | None:
    """Basename of the session's working directory, for the viewer's session list.

    Never the full path: the parent directories are not recorded anywhere.
    ``workspace_name`` in the hook config overrides it; a missing or
    unreadable cwd yields ``None`` rather than raising into mount.
    """
    configured = (config or {}).get("workspace_name")
    if isinstance(configured, str) and configured.strip():
        # Normalize both Windows and POSIX separators even on another OS.
        name = configured.strip().replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        return name[:120] or None
    try:
        name = Path.cwd().name
    except OSError:
        return None
    return name[:120] or None


def _continue_result() -> Any:
    """Duck-typed 'continue' result: the real amplifier_core HookResult when
    importable (real kernel), a plain stand-in otherwise (offline unit
    tests). Both expose `.action`, which is all any consumer in this
    codebase (or amplifier_core's own hook dispatch) checks."""
    try:
        from amplifier_core.models import HookResult

        return HookResult(action="continue")
    except ImportError:
        return SimpleNamespace(action="continue")


def reset_planner_ctx_on_compaction(runtime: Any, event: str) -> None:
    """Invalidate the persisted ctx-estimate baseline on a real compaction.

    ``Runtime.planner_last_ctx`` (see runtime.py / orchestrator._estimate_ctx)
    is the last known total prompt size, persisted across turns so each new
    turn only has to measure its OWN new message rather than re-measuring the
    whole conversation. A "context:compaction" native event means the real
    conversation just shrank -- if planner_last_ctx is left untouched, every
    subsequent _estimate_ctx() call keeps adding new message sizes on top of
    the stale PRE-compaction baseline, silently and permanently overestimating
    context size (and therefore cost and cache-warmth) for the rest of the
    session. Resetting it to None makes the next _estimate_ctx() call fall
    back to its designed cold-start path (a fresh measurement of the current
    request) instead of compounding stale state. See
    docs/proposals/TURN-PLANNER.md.
    """
    if event == "context:compaction":
        runtime.planner_last_ctx = None


class ShadowScorer:
    """Off-critical-path 'what would we have chosen' measurement.

    Composes onto any orchestrator (including the untouched upstream loop):
    it reads state from the mounted context manager rather than from a
    request object, because provider:request carries no request
    (see docs/UPSTREAM_CONTRACT.md / design doc §0.3).
    """

    def __init__(self, runtime: Any, coordinator: Any, config: dict[str, Any]):
        self._runtime = runtime
        self._coordinator = coordinator
        policy = runtime.service.policy
        self._max_messages = config.get(
            "shadow_max_messages", policy.shadow_max_messages
        )
        self._budget_ms = config.get(
            "shadow_snapshot_budget_ms", policy.shadow_snapshot_budget_ms
        )
        self._state_source = config.get("shadow_state_source", "context_mount")
        self._turn_id: str | None = None

    async def on_provider_request(self, event: str, data: dict) -> Any:
        # No-op by design: upstream (loop-streaming) emits iteration 1's
        # provider:request BEFORE appending the turn's own user message to
        # the mounted context (amplifier_module_loop_streaming/__init__.py,
        # the "hoisted" provider:request block -- see
        # docs/UPSTREAM_CONTRACT.md and docs/design/redesign-2026-09-17.md
        # P3 postmortem). A snapshot taken here sees an empty/stale
        # ``context.get_messages()`` on that first iteration of every turn,
        # so no candidates are ever found and the real tool call that
        # follows is silently never scored. The snapshot instead happens in
        # ``on_tool_pre``, the earliest point at which the mounted context
        # is guaranteed (observed in a live DTU trace) to already include
        # this iteration's own user/assistant messages.
        return _continue_result()

    async def on_tool_pre(self, event: str, data: dict) -> Any:
        try:
            await self._propose_and_observe(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        return _continue_result()

    async def _propose_and_observe(self, data: dict) -> None:
        """Snapshot, propose, and immediately resolve against this same
        tool call -- all in one pass, now that ``on_tool_pre`` is the only
        point in the upstream loop where the mounted context is guaranteed
        to reflect this iteration's own messages (see ``on_provider_request``).
        The snapshot/candidate-collection step is bounded by the same
        ``shadow_snapshot_budget_ms`` as before; only backend scoring
        (``ShadowWorker._score``) remains deferred to the background task.
        """
        if self._runtime.service.policy.mode == "off":
            return
        if getattr(self._runtime, "orchestrator_owned", False):
            # loop-fast-decisions is on the real request path and already
            # decides/routes every provider call; a shadow proposal here would
            # only re-score the same state against the same backend.
            return
        job = await self._snapshot()
        if job is None:
            return
        tool = data.get("tool_name") or data.get("tool")
        if not isinstance(tool, str):
            tool = field_value(tool, "name", None)
        arguments = data.get("tool_input")
        if arguments is None:
            arguments = data.get("arguments")
        arguments_hash = digest(arguments) if isinstance(arguments, dict) else None
        self._runtime.submit_shadow(job)
        await self._runtime.service.emit(
            "shadow_observed",
            {
                "tool": tool,
                "tool_call_id": data.get("tool_call_id"),
                "arguments_hash": arguments_hash,
            },
            job.decision_id,
        )
        self._runtime.resolve_shadow(
            ShadowOutcome(
                decision_id=job.decision_id,
                actual_tool=tool,
                actual_arguments_hash=arguments_hash,
            )
        )

    async def on_tool_post(self, event: str, data: dict) -> Any:
        return _continue_result()

    async def on_provider_error(self, event: str, data: dict) -> Any:
        return _continue_result()

    async def on_execution_end(self, event: str, data: dict) -> Any:
        self._turn_id = None
        try:
            self._runtime.shadow_worker.sweep_unobserved()
        except Exception:
            pass
        return _continue_result()

    async def _snapshot(self) -> ShadowJob | None:
        if self._state_source == "off":
            return None
        try:
            async with asyncio.timeout(self._budget_ms / 1000):
                return await self._build_job()
        except TimeoutError:
            await self._runtime.service.emit(
                "fallback", {"reason_code": "shadow_snapshot_budget_exceeded"}
            )
            return None

    async def _build_job(self) -> ShadowJob | None:
        getter = getattr(self._coordinator, "get", None)
        context = getter("context") if callable(getter) else None
        if context is None or not hasattr(context, "get_messages"):
            await self._runtime.service.emit(
                "health", {"phase": "shadow", "reason_code": "shadow_context_unavailable"}
            )
            return None
        messages = list(await context.get_messages())
        messages = messages[-self._max_messages :] if self._max_messages > 0 else []
        tools = (getter("tools") if callable(getter) else None) or {}
        pseudo_request = SimpleNamespace(
            messages=messages, tools=None, tool_choice=None, model=None
        )
        service = self._runtime.service
        candidates, _reject_reasons = await collect_candidates(
            self._coordinator,
            pseudo_request,
            tools,
            service.configured_candidates,
            service.policy.max_candidates,
        )
        eligible = []
        for candidate in candidates:
            if await service._eligible(candidate, tools):
                eligible.append(candidate)
        eligible = eligible[: service.policy.max_candidates]
        if not eligible:
            await service.emit(
                "health", {"phase": "shadow", "reason_code": "no_eligible_candidates",
                           "candidate_count": 0}
            )
            return None
        state = build_state(pseudo_request, service.policy.max_state_chars)
        if self._turn_id is None:
            self._turn_id = uuid4().hex
        return ShadowJob(
            kind="turn",
            turn_id=self._turn_id,
            decision_id=uuid4().hex,
            state=state,
            candidates=tuple(eligible),
            questions=(),
            state_source="context_mount",
            state_hash=digest(state),
            state_revision=0,
        )



def install_auto_observatory(
    coordinator: Any,
    runtime: Any,
    config: dict[str, Any],
    events_dir: str | None,
    tasks: list[asyncio.Task],
) -> Any:
    """Register the auto-observatory ``session:start`` handler, at most once
    per session runtime.

    On the top-level session's first ``session:start``, reuse-or-spawn a
    detached ``afast serve`` and (optionally) open it in a browser. Fires
    exactly once per session, never delays session start (the actual
    spawn/health-check work happens in a background task appended to
    ``tasks``, which the caller drains at cleanup), and never raises into the
    hook chain.

    Both hooks-fast-decisions and loop-fast-decisions call this; the first
    caller (the orchestrator, by kernel mount order) wins and the second is a
    no-op, so composing both never launches two viewers. Returns the hook
    registration (an unregister callable) or ``None`` when already installed.
    """
    if getattr(runtime, "observatory_installed", False):
        return None
    runtime.observatory_installed = True
    fired = False

    async def on_session_start(event: str, data: dict) -> Any:
        nonlocal fired
        if fired:
            return _continue_result()
        fired = True

        obs_config = dict(config.get("observatory") or {})
        if not obs_config.get("enabled", True):
            return _continue_result()

        parent = data.get("parent_id") or data.get("parent_session_id")
        if not parent:
            try:
                _, parent = session_identity(coordinator)
            except Exception:
                parent = None
        if parent:
            await runtime.service.emit(
                "observatory", {"action": "skipped", "reason": "child_session"}
            )
            return _continue_result()

        if (
            os.environ.get("AFAST_OBSERVATORY") == "off"
            or os.environ.get("AMPLIFIER_NO_BROWSER") == "1"
        ):
            await runtime.service.emit(
                "observatory", {"action": "skipped", "reason": "env_disabled"}
            )
            return _continue_result()

        try:
            is_tty = sys.stdin.isatty() or sys.stdout.isatty()
        except Exception:
            is_tty = False
        if not is_tty:
            await runtime.service.emit(
                "observatory", {"action": "skipped", "reason": "non_tty"}
            )
            return _continue_result()

        port = obs_config.get("port", 8765)
        open_mode = obs_config.get("open_browser", "always")
        if open_mode not in ("always", "first", "never"):
            open_mode = "always"
        events_dir_value = (
            events_dir
            or os.getenv("AFAST_EVENTS_DIR")
            or str(Path.home() / ".amplifier" / "fast-decisions" / "events")
        )
        state_file = obs_config.get("state_file") or str(DEFAULT_OBSERVATORY_STATE_FILE)

        async def run_ensure() -> None:
            try:
                before = read_state(state_file)
                was_alive = before is not None and viewer_is_alive(before)
                url = await asyncio.to_thread(
                    ensure_viewer, events_dir_value, port, state_file
                )
                if url is None:
                    await runtime.service.emit(
                        "observatory",
                        {
                            "action": "failed",
                            "reason": "viewer_unreachable",
                            "port": port,
                        },
                    )
                    return
                action = "reused" if was_alive else "started"
                await runtime.service.emit(
                    "observatory", {"action": action, "reason": "ok", "port": port}
                )
                should_open = open_mode == "always" or (
                    open_mode == "first" and action == "started"
                )
                if should_open:
                    await asyncio.to_thread(open_page, url)
            except asyncio.CancelledError:
                raise
            except Exception:
                try:
                    await runtime.service.emit(
                        "observatory",
                        {
                            "action": "failed",
                            "reason": "unexpected_error",
                            "port": port,
                        },
                    )
                except Exception:
                    pass

        tasks.append(asyncio.create_task(run_ensure()))
        return _continue_result()

    return coordinator.hooks.register("session:start", on_session_start, priority=999)


async def mount(coordinator, config: dict):
    from amplifier_core.models import HookResult

    # The hook has no execution authority: it can never own an "active"
    # policy. If its own config asks for one (only meaningful when no
    # orchestrator is mounted -- the normal shadow rung -- and this call is
    # therefore the one building the runtime), downgrade to shadow and
    # record why, rather than silently building an active Policy an
    # observer-only module has no business owning.
    effective_config = dict(config)
    hook_requested_active = effective_config.get("mode") == "active"
    if hook_requested_active:
        effective_config["mode"] = "shadow"
    runtime, owner = get_runtime(coordinator, effective_config)
    if hook_requested_active:
        await runtime.service.emit(
            "fallback", {"reason_code": "hook_cannot_own_active"}
        )
    registrations = []

    # HC00 ("freeze source"): record which source actually ran, once per
    # session, as early as possible -- even in mode "off", since a baseline
    # profile still needs a receipt proving which source produced it. A
    # failure here must never prevent the rest of mount from proceeding.
    try:
        await runtime.service.emit(
            "source",
            provenance.source_event_data(
                mode=runtime.service.policy.mode, module="hooks-fast-decisions"
            ),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        pass

    # Report the shared runtime's effective policy, including when an active
    # orchestrator created it before this observer mounted.
    await runtime.service.emit("health", {
        "phase": "configuration",
        "mode": runtime.service.policy.mode,
        "backend": runtime.service.backend.name,
        "backend_label": _backend_label(runtime.service.backend),
        "allow_external_state": runtime.service.policy.allow_external_state,
        "policy_version": runtime.service.policy.version,
        "event_source": "native-hook-bridge",
        "session_label": config.get("session_label") if isinstance(config.get("session_label"), str) else None,
        "workspace_name": workspace_name(config),
        "harness": harness_name(config),
        **repo_context(_session_dir(coordinator)),
    })

    async def heartbeat():
        # Reports that this observer is still mounted, never that a tool or
        # provider is busy. The native lifecycle events carry turn state.
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
            try:
                await runtime.service.emit("health", {
                    "phase": "session_heartbeat", "event_source": "native-hook-bridge",
                })
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    heartbeat_task = asyncio.create_task(heartbeat())

    async def observe(event: str, data: dict):
        # Allowlisted structural fields only. Never copy native event bodies.
        reset_planner_ctx_on_compaction(runtime, event)
        tool = data.get("tool_name") or data.get("tool")
        if not isinstance(tool, str):
            tool = field_value(tool, "name", None)
        await runtime.service.emit(
            "health",
            {
                "native_event": event,
                "event_source": "native-hook-bridge",
                "tool": tool,
                "tool_call_id": data.get("tool_call_id"),
                "phase": data.get("phase"),
                "provider": data.get("provider") if isinstance(data.get("provider"), str) else None,
                "status": "observed",
                "retry_attempt": data.get("attempt") if event == "provider:retry" else None,
                "status_code": data.get("status_code") if event in {"provider:retry", "provider:error"} else None,
                "exception_type": data.get("error_type") if isinstance(data.get("error_type"), str) else None,
            },
        )
        return HookResult(action="continue")

    for event in ("execution:start", "execution:end", "provider:request",
                  "tool:pre", "tool:post", "provider:error", "provider:retry",
                  "session:end", "context:compaction"):
        registrations.append(coordinator.hooks.register(event, observe, priority=999))

    scorer = ShadowScorer(runtime, coordinator, config)
    for event, handler in (
        ("provider:request", scorer.on_provider_request),
        ("tool:pre", scorer.on_tool_pre),
        ("tool:post", scorer.on_tool_post),
        ("provider:error", scorer.on_provider_error),
        ("execution:end", scorer.on_execution_end),
    ):
        registrations.append(coordinator.hooks.register(event, handler, priority=999))

    # P4: the model-role router. Shadow-only -- never modifies the delegate
    # call. Cheap membership check per tool:pre/tool:post; the router itself
    # no-ops immediately when role_router is off (opt-out via config;
    # the shipped behaviors/fast-decisions.yaml default is on).
    router = RoleRouter(runtime, coordinator, config)

    async def on_role_pre(event: str, data: dict):
        if runtime.service.policy.mode == "off" or getattr(runtime, "orchestrator_owned", False):
            return _continue_result()
        tool = data.get("tool_name") or data.get("tool")
        if tool in router.tools:
            try:
                await router.on_delegate_pre(data)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        return _continue_result()

    async def on_role_post(event: str, data: dict):
        tool = data.get("tool_name") or data.get("tool")
        if tool in router.tools:
            try:
                router.on_delegate_post(data)
            except Exception:
                pass
        return _continue_result()

    registrations.append(
        coordinator.hooks.register("tool:pre", on_role_pre, priority=999)
    )
    registrations.append(
        coordinator.hooks.register("tool:post", on_role_post, priority=999)
    )

    # Auto-observatory (shared with loop-fast-decisions; whichever module
    # mounts first installs it -- see install_auto_observatory).
    observatory_tasks: list[asyncio.Task] = []
    observatory_registration = install_auto_observatory(
        coordinator, runtime, config, effective_config.get("events_dir"), observatory_tasks
    )
    if observatory_registration is not None:
        registrations.append(observatory_registration)

    async def cleanup():
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        try:
            await runtime.service.emit("health", {
                "phase": "session_closed", "event_source": "native-hook-bridge",
            })
        except Exception:
            pass
        for unregister in registrations:
            if callable(unregister):
                unregister()
        for task in observatory_tasks:
            if not task.done():
                task.cancel()
        for task in observatory_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if owner:
            await runtime.close()

    return cleanup
