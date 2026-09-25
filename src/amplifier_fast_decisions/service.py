"""Fast decisions propose prepared actions; the upstream loop still executes them."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import uuid4

from .candidates import capability, collect_candidates, maybe_await
from .contracts import (
    SLOW,
    VALIDATOR_CAPABILITY,
    Candidate,
    DecisionRequest,
    Policy,
    TurnState,
    canonical,
    candidate_read_identity,
    classify_domain,
    compute_candidate_order_hash,
    effective_gate,
)
from .questions import collect_questions
from .state import automatic_tools, build_state, request_fingerprint, tool_names
from .telemetry import Emitter


class DecisionService:
    def __init__(
        self,
        policy: Policy,
        backend: Any,
        emitter: Emitter,
        coordinator: Any = None,
        configured_candidates: list[dict] | None = None,
    ):
        self.policy = policy
        self.backend = backend
        self.emitter = emitter
        self.coordinator = coordinator
        self.configured_candidates = configured_candidates or []
        self.turn: TurnState | None = None
        self.last_decision_id: str | None = None
        self.slow_total = 0
        self.unhealthy_until = 0.0
        # Wall time of the backend call in the most recent choose() (None
        # when that decision never reached the backend); read by the
        # orchestrator's per-step receipts.
        self.last_backend_ms: float | None = None

    async def emit(self, kind: str, data: dict, decision_id: str | None = None):
        return await self.emitter.emit(
            kind,
            data,
            turn_id=self.turn.id if self.turn else None,
            decision_id=decision_id or self.last_decision_id,
        )

    async def _eligible(self, candidate: Candidate, tools: dict[str, Any]) -> bool:
        if (
            candidate.tool not in self.policy.allowed_tools
            or candidate.tool not in tools
        ):
            return False
        tool = tools[candidate.tool]
        # The bundled workspace validates containment, exclusions and revision.
        local = getattr(tool, "validate_candidate", None)
        if candidate.tool == "fast_workspace":
            return bool(local and await maybe_await(local(candidate)))
        # Other tools need a separately installed trusted validator in addition
        # to allowlisting. Candidate origins or risk labels are NOT approval.
        validator = capability(self.coordinator, VALIDATOR_CAPABILITY)
        return bool(validator and await maybe_await(validator(candidate)))

    async def choose(self, request: Any, tools: dict[str, Any], *, extra_candidates: list[Any] | None = None,
                     force: bool = False, workspace_paths: bool = True,
                     state_instruction: str | None = None) -> Candidate | None:
        """The judged prepared-action decision. ``force`` (the orchestrator's
        per-step decision point, step_actions.py) asks even when the always-on
        ``read_shortcut`` is disabled, with ``extra_candidates`` built for this
        step; every other gate (mode, budgets, consent, eligibility,
        staleness) is unchanged."""
        self.last_backend_ms = None
        if self.turn is None:
            raise RuntimeError("No active turn")
        turn = self.turn
        turn.decision_count += 1
        self.last_decision_id = uuid4().hex
        decision_id = self.last_decision_id
        common = {
            "mode": self.policy.mode,
            "backend": self.backend.name,
            "policy_version": self.policy.version,
            "step": turn.decision_count,
            "state_revision": turn.revision,
        }

        async def slow(reason: str, **extra):
            await self.emit(
                "routed",
                {
                    **common,
                    "route": "slow",
                    "destination": "provider",
                    "reason_code": reason,
                    **extra,
                },
                decision_id,
            )

        if self.policy.mode == "off":
            return await slow("mode_off")
        if not self.policy.read_shortcut and not force:
            return await slow("read_shortcut_disabled")
        if self.backend.name == "unavailable":
            # Routing-only configuration (the orchestrator-primary default):
            # no judge is configured, so skip candidate collection entirely
            # instead of building state for a backend that can only raise
            # (and would then trip the circuit breaker on every turn).
            return await slow("judge_disabled")
        if not automatic_tools(request) or not tool_names(request):
            return await slow("no_automatic_tool_boundary")
        if (
            turn.fast_streak >= self.policy.max_fast_streak
            or turn.fast_total >= self.policy.max_fast_per_turn
        ):
            return await slow("fast_path_budget")
        if time.monotonic() < self.unhealthy_until:
            return await slow("backend_circuit_open")
        if self.backend.external and not self.policy.allow_external_state:
            return await slow("external_state_not_enabled")
        deadline = asyncio.get_running_loop().time() + self.policy.timeout_ms / 1000
        revision = turn.revision
        snapshot_hash = request_fingerprint(request)
        try:
            async with asyncio.timeout_at(deadline):
                candidates, candidate_reasons = await collect_candidates(
                    self.coordinator,
                    request,
                    tools,
                    self.configured_candidates,
                    self.policy.max_candidates,
                    extra=extra_candidates,
                    workspace_paths=workspace_paths,
                )
                questions, question_reasons = await collect_questions(
                    self.coordinator, self.policy.max_questions
                )
                mounted = tool_names(request)
                pre_suppress = [
                    c
                    for c in candidates
                    if c.tool in mounted and c.fingerprint not in turn.used
                ]
                # HC02a: drop fast_workspace read/list candidates already
                # recorded in the turn's completed-read ledger (unchanged
                # revision). Counted, never silently invisible.
                suppressed_count = 0
                eligible = []
                for c in pre_suppress:
                    if self.policy.suppress_completed_reads:
                        identity = candidate_read_identity(c, tools)
                        if (
                            identity is not None
                            and turn.completed_reads.get(identity[0]) == identity[1]
                        ):
                            suppressed_count += 1
                            continue
                    if await self._eligible(c, tools):
                        eligible.append(c)
                candidates = eligible[: self.policy.max_candidates]
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            await self.emit(
                "fallback",
                {
                    **common,
                    "reason_code": "decision_timeout",
                    "exception_type": type(exc).__name__,
                },
                decision_id,
            )
            return await slow("decision_timeout")
        except Exception as exc:
            await self.emit(
                "fallback",
                {
                    **common,
                    "reason_code": "candidate_source_error",
                    "exception_type": type(exc).__name__,
                },
                decision_id,
            )
            return await slow("candidate_source_error")
        for code in (*candidate_reasons, *question_reasons):
            await self.emit("fallback", {**common, "reason_code": code}, decision_id)
        if not candidates:
            # Cheap path preserved: a route that becomes slow purely because
            # every remaining candidate was already read this turn (unchanged
            # revision) gets its own reason code, still with no backend call.
            reason = (
                "already_read_unchanged"
                if suppressed_count and suppressed_count == len(pre_suppress)
                else "no_eligible_candidates"
            )
            return await slow(
                reason, candidates_suppressed_already_read=suppressed_count
            )
        order_hash = compute_candidate_order_hash(candidates)
        # Decided once, here, at the point the candidate set is built --
        # reused verbatim in scored/routed/fallback (via `common`) and by
        # the shadow scorer / role router (their own candidate sets).
        domain = classify_domain(candidates)
        common["domain"] = domain
        state_stats: dict[str, Any] = {}
        state = build_state(request, self.policy.max_state_chars, state_stats, state_instruction)
        state_chars = len(canonical(state))
        await self.emit(
            "requested",
            {
                **common,
                "state_hash": snapshot_hash,
                "state_chars": state_chars,
                "candidate_count": len(candidates),
                "question_count": len(questions),
                "candidate_order_hash": order_hash,
                "candidates_suppressed_already_read": suppressed_count,
                "candidates": [
                    {"id": c.id, "label": c.label, "tool": c.tool} for c in candidates
                ],
                # HC01: what build_state actually included, so state loss
                # can never hide behind a fast timing statistic alone.
                "observation_count": state_stats.get("observation_count"),
                "observations_available": state_stats.get("observations_available"),
                "observations_dropped": state_stats.get("observations_dropped"),
                "observations_clipped": state_stats.get("observations_clipped"),
                "task_anchored": state_stats.get("task_anchored"),
                "truncation_reason": state_stats.get("truncation_reason"),
            },
            decision_id,
        )
        start = time.perf_counter()
        try:
            async with asyncio.timeout_at(deadline):
                decision_request = DecisionRequest(
                    state=state,
                    candidates=tuple(candidates),
                    questions=tuple(questions),
                    candidate_order_hash=order_hash,
                )
                result = await self.backend.ask(decision_request)
            result.action.validate({c.id for c in candidates} | {SLOW})
        except asyncio.CancelledError:
            await self.emit(
                "cancelled",
                {**common, "reason_code": "decision_cancelled"},
                decision_id,
            )
            raise
        except Exception as exc:
            reason = (
                "decision_timeout" if isinstance(exc, TimeoutError) else "backend_error"
            )
            self.last_backend_ms = (time.perf_counter() - start) * 1000
            self.unhealthy_until = time.monotonic() + 5.0
            await self.emit(
                "fallback",
                {
                    **common,
                    "reason_code": reason,
                    "duration_ms": (time.perf_counter() - start) * 1000,
                    "exception_type": type(exc).__name__,
                },
                decision_id,
            )
            return await slow(reason)
        decision = result.action
        duration = (time.perf_counter() - start) * 1000
        self.last_backend_ms = duration
        p = decision.probabilities[decision.choice]
        others = [v for k, v in decision.probabilities.items() if k != decision.choice]
        margin = p - max(others, default=0)
        # HC09 ("stake-scaled confidence gates", opt-in): the read-shortcut
        # gate defaults to `min_probability` (unchanged) but a configured
        # `Policy.confidence_gates["read_shortcut"]` overrides it.
        gate = effective_gate(self.policy, "read_shortcut")
        passed_gate = p >= gate
        scored_data = {
            "gate": gate,
            "passed_gate": passed_gate,
            **common,
            "choice": decision.choice,
            "probabilities": decision.probabilities,
            "probability_kind": decision.probability_kind,
            "selected_probability": p,
            "reported_confidence": decision.reported_confidence,
            "confidence_kind": decision.confidence_kind,
            "option_set_hash": decision.option_set_hash,
            "margin": margin,
            "duration_ms": duration,
            "model": decision.model,
            "input_tokens": decision.input_tokens,
            "output_tokens": result.output_tokens,
            "synthetic": decision.synthetic,
            "candidate_order_hash": order_hash,
            "latency_kind": "decision_model_wall_time",
        }
        await self.emit("scored", scored_data, decision_id)
        proposed = "fast" if decision.choice != SLOW else "slow"
        if self.policy.mode == "shadow":
            return await slow(
                "shadow_only",
                proposed_route=proposed,
                shadow=True,
                selected_candidate=decision.choice,
            )
        if decision.synthetic and not self.policy.allow_synthetic_active:
            return await slow("synthetic_backend_not_authorized")
        if decision.choice == SLOW:
            return await slow("model_abstained")
        if p < gate or margin < self.policy.min_margin:
            return await slow("selection_threshold", proposed_route=proposed)
        candidate = next(c for c in candidates if c.id == decision.choice)
        # Revalidate both conversation and prepared action immediately before submission.
        if request_fingerprint(request) != snapshot_hash or turn.revision != revision:
            return await slow("stale_state")
        try:
            async with asyncio.timeout_at(deadline):
                still_valid = await self._eligible(candidate, tools)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return await slow("decision_timeout")
        except Exception:
            still_valid = False
        if not still_valid:
            return await slow("candidate_no_longer_eligible")
        if request_fingerprint(request) != snapshot_hash or turn.revision != revision:
            return await slow("stale_state")
        if asyncio.get_running_loop().time() >= deadline:
            return await slow("decision_deadline_exceeded")
        # Submission is committed by the provider facade only after a valid
        # Amplifier response envelope has been constructed.
        return candidate

    async def close(self):
        await self.backend.close()
