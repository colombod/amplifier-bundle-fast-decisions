"""Registry attachment: fast-decisions without replacing the orchestrator.

loop-fast-decisions replaces the session orchestrator so that, on every
execute(), it can hand the upstream loop a ``RoutedProvider`` around each
provider and an ``ObservedTool`` around each tool. Hosts that own their loop
cannot accept that: Amplifier Unified runs loop-live and refuses custom
orchestrators rather than silently replacing them.

Every decision fast-decisions makes happens inside those two facades, not in
the orchestrator itself. This module mounts the same facades once, in the
coordinator's live ``providers`` and ``tools`` registries, which every host
passes to its loop, and takes turn boundaries from the loop's own events:

- ``execution:start``: begin a turn (closing any turn left open) and wrap
  anything mounted since the last turn.
- ``orchestrator:complete``: end the turn. Only the final emission of a
  ``/goal`` pursuit counts, matching one execute() in orchestrator mode.
- ``session:end`` and cleanup: end any open turn.

Differences from orchestrator mode (docs/REGISTRY-MODE.md):

- ``fast_decisions:turn_start`` follows ``execution:start`` instead of
  preceding it.
- Only loop steps are routed: provider calls outside a turn, calls without
  tools (session naming, compaction summaries) and calls carrying an explicit
  ``model`` keyword (a user's model selection) reach the provider unchanged.
- A provider a host captured before this module wrapped it stays unrouted
  (fails safe to plain Amplifier).
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
import time
from typing import Any, Iterator
from uuid import uuid4

from . import provenance
from .contracts import Candidate, TurnState
from .levers import Levers
from .orchestrator import (
    ObservedTool,
    RoutedProvider,
    _hook_continue,
    action_response,
    project_and_traffic,
    usage_fields,
    waste_guard_for,
)
from .runtime import Runtime, get_runtime

__amplifier_module_type__ = "hook"

_OURS = (RoutedProvider, ObservedTool)


def _contains_ours(value: Any, depth: int = 3) -> bool:
    """True when ``value`` is one of our facades or wraps one.

    Hosts wrap registry entries after we do (loop-live's ``AsyncTool`` around
    ``delegate``, Unified's ``PersistentDelegate``). Wrapping those again would
    observe one call twice. Reads instance ``__dict__`` only, so a proxy's
    ``__getattr__`` is never triggered."""
    seen: set[int] = set()
    frontier = [value]
    for _ in range(depth + 1):
        following = []
        for item in frontier:
            if id(item) in seen:
                continue
            seen.add(id(item))
            if isinstance(item, _OURS):
                return True
            try:
                attributes = object.__getattribute__(item, "__dict__")
            except (AttributeError, TypeError):
                continue
            following.extend(v for v in attributes.values()
                             if not isinstance(v, (str, bytes, int, float, bool, type(None))))
        frontier = following
    return False


class _RawTools(Mapping):
    """The live tools registry with our ``ObservedTool`` facades unwrapped.

    Orchestrator mode gives the judge the loop's raw tool dict (candidate
    validation reads ``validate_candidate`` and ``read_identity`` off the raw
    tool). Keep that exact view while the registry itself holds facades."""

    def __init__(self, coordinator: Any):
        self._coordinator = coordinator

    def _live(self) -> dict[str, Any]:
        return self._coordinator.get("tools") or {}

    def __getitem__(self, key: str) -> Any:
        tool = self._live()[key]
        return tool._tool if isinstance(tool, ObservedTool) else tool

    def __iter__(self) -> Iterator[str]:
        return iter(list(self._live()))

    def __len__(self) -> int:
        return len(self._live())


class RegistryRouter:
    def __init__(self, coordinator: Any, runtime: Runtime, config: dict[str, Any], *,
                 response_factory=action_response):
        self.coordinator = coordinator
        self.runtime = runtime
        self.config = config
        self.response_factory = response_factory
        self.raw_tools = _RawTools(coordinator)
        self.providers: dict[str, RoutedProvider] = {}
        self.tools: dict[str, ObservedTool] = {}
        self._levers: Levers | None = None
        self._shared_warm: int | None = None
        self._started = 0.0
        self._execution_ended = False

    async def attach(self) -> None:
        """Wrap every registered provider and tool that is not already ours."""
        providers = self.coordinator.get("providers") or {}
        for key, provider in list(providers.items()):
            if _contains_ours(provider):
                continue
            wrapped = RoutedProvider(provider, self.runtime, self.raw_tools,
                                     self.response_factory, key, levers=self._levers)
            wrapped._registry_gate = True
            await self.coordinator.mount("providers", wrapped, name=key)
            self.providers[key] = wrapped
        tools = self.coordinator.get("tools") or {}
        workspace = self.raw_tools.get("fast_workspace")
        for key, tool in list(tools.items()):
            if _contains_ours(tool):
                continue
            wrapped = ObservedTool(tool, self.runtime, key, workspace=workspace, levers=self._levers)
            await self.coordinator.mount("tools", wrapped, name=key)
            self.tools[key] = wrapped

    async def begin_turn(self) -> None:
        service = self.runtime.service
        if service.turn is not None:
            # orchestrator:complete is not emitted on every exit path.
            await self.end_turn("abandoned")
        await self.attach()
        async with self.runtime.lock:
            service.turn = TurnState(uuid4().hex)
            service.last_decision_id = None
            service.slow_total = 0
            loop = self.coordinator.get("orchestrator")
            await service.emit("turn_start", {"mode": service.policy.mode,
                "backend": service.backend.name,
                "engine": "registry:" + type(loop).__module__.split(".")[0],
                "policy_version": service.policy.version,
                "allow_external_state": service.policy.allow_external_state,
                "effort_routing_enabled": bool(service.policy.effort_routing),
                "model_routing_enabled": bool(service.policy.model_routing)})
            guard = waste_guard_for(service)
            if guard is not None:
                guard.new_turn()
            levers = Levers(service.policy, service, lambda: project_and_traffic(service),
                            usage_fn=usage_fields, shared_warm=self._shared_warm)
            self._levers = levers if levers.active else None
            workspace = self.raw_tools.get("fast_workspace")
            for provider in self.providers.values():
                provider.begin_turn(self._levers)
            for tool in self.tools.values():
                tool.begin_turn(self._levers, workspace)
            self._started = time.perf_counter()
            self._execution_ended = False

    async def end_turn(self, status: str, response: Any = None) -> None:
        service = self.runtime.service
        if service.turn is None:
            return
        async with self.runtime.lock:
            if self._levers is not None:
                await self._levers.finish(status)
                self._shared_warm = self._levers.shared_warm
                self._levers = None
            for provider in self.providers.values():
                provider.begin_turn(None)
            for tool in self.tools.values():
                tool.begin_turn(None, tool._workspace)
            await self._backfill_execution_end(response, status)
            guard = getattr(service, "waste_guard", None)
            if guard is not None:
                try:
                    for data in guard.end_turn():
                        await service.emit("efficiency", data)
                except Exception:  # noqa: BLE001
                    pass
            await service.emit("turn_end", {"fast_total": service.turn.fast_total,
                "status": status, "duration_ms": (time.perf_counter() - self._started) * 1000,
                "slow_total": service.slow_total,
                **(self.runtime.recorder.health if self.runtime.recorder else {})})
            service.turn = None
            service.last_decision_id = None

    async def _backfill_execution_end(self, response: Any, status: str) -> None:
        """Same contract as HybridOrchestrator: loop-streaming skips
        execution:end on early returns and exceptions."""
        if self._execution_ended or status == "session_end":
            return
        self._execution_ended = True
        emit = getattr(getattr(self.coordinator, "hooks", None), "emit", None)
        if not callable(emit):
            return
        try:
            await emit("execution:end", {
                "response": response if isinstance(response, str) else "",
                "status": {"ok": "completed"}.get(status, status),
                "source": "hooks-fast-decisions-router",
            })
        except Exception:  # noqa: BLE001
            pass

    # Hook handlers --------------------------------------------------------

    async def on_execution_start(self, event: str, data: dict):
        try:
            await self.begin_turn()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- never block the user's turn
            pass
        return _hook_continue()

    async def on_execution_end(self, event: str, data: dict):
        self._execution_ended = True
        return _hook_continue()

    async def on_orchestrator_complete(self, event: str, data: dict):
        if data.get("goal_final") is False:
            return _hook_continue()
        status = data.get("status")
        status = status if status in ("cancelled", "error") else "ok"
        try:
            await self.end_turn(status, data.get("response"))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass
        return _hook_continue()

    async def on_session_end(self, event: str, data: dict):
        try:
            await self.end_turn("session_end")
        except Exception:  # noqa: BLE001
            pass
        return _hook_continue()

    async def on_tool_post(self, event: str, data: dict):
        """Deliver a pending loop-stop note with the next model request (same
        as HybridOrchestrator._on_tool_post)."""
        levers = self._levers
        note = levers.take_note() if levers is not None else None
        if not note:
            return _hook_continue()
        try:
            from amplifier_core.models import HookResult
            return HookResult(action="inject_context", context_injection=note,
                              context_injection_role="user", ephemeral=True)
        except ImportError:
            from types import SimpleNamespace
            return SimpleNamespace(action="inject_context", context_injection=note,
                                   context_injection_role="user", ephemeral=True,
                                   append_to_last_tool_result=False)

    def register(self, hooks: Any) -> list[Any]:
        register = getattr(hooks, "register", None)
        if not callable(register):
            return []
        name = "hooks-fast-decisions-router:"
        return [
            register("execution:start", self.on_execution_start, priority=0, name=name + "execution-start"),
            register("execution:end", self.on_execution_end, priority=0, name=name + "execution-end"),
            register("orchestrator:complete", self.on_orchestrator_complete, priority=0,
                     name=name + "orchestrator-complete"),
            register("session:end", self.on_session_end, priority=0, name=name + "session-end"),
            register("tool:post", self.on_tool_post, priority=50, name=name + "loop-stop"),
        ]


async def mount(coordinator, config: dict):
    # Validate envelope construction before entering any user turn.
    action_response(Candidate("compat_check", "Schema check", "fast_workspace",
                              {"operation": "list", "path": "."}), "compat_check")
    # Decision policy owner, exactly as loop-fast-decisions: the shadow
    # scorer and role router in hooks-fast-decisions stand down.
    runtime, _ = get_runtime(coordinator, config, owner=True)
    try:
        await runtime.service.emit("source", provenance.source_event_data(
            mode=runtime.service.policy.mode, module="hooks-fast-decisions-router"))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        pass
    registrations: list[Any] = []
    observatory_tasks: list[asyncio.Task] = []
    router = RegistryRouter(coordinator, runtime, config)
    try:
        await router.attach()
        hooks = getattr(coordinator, "hooks", None)
        if hooks is not None:
            registrations.extend(router.register(hooks))
            from .observer import install_auto_observatory
            registration = install_auto_observatory(
                coordinator, runtime, config, config.get("events_dir"), observatory_tasks)
            if registration is not None:
                registrations.append(registration)
    except Exception:
        await runtime.close()
        raise

    async def cleanup():
        try:
            await router.end_turn("session_end")
        except Exception:  # noqa: BLE001
            pass
        for unregister in registrations:
            if callable(unregister):
                try:
                    unregister()
                except Exception:  # noqa: BLE001
                    pass
        for task in observatory_tasks:
            if not task.done():
                task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await runtime.close()

    return cleanup
