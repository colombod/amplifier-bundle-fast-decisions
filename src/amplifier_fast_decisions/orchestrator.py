"""Hybrid orchestrator composed with the upstream streaming loop.

The upstream loop owns context, tools, approvals, steering and cancellation.
This module supplies contract-compatible provider/tool facades per execute().
It never patches a class, a global provider dictionary or amplifier-core.
"""
from __future__ import annotations
import asyncio
import os
from pathlib import Path
import re
import time
from typing import Any
from uuid import uuid4

from . import efficiency
from .contracts import (
    Candidate,
    DecisionRequest,
    DEFAULT_ESCALATION_WEIGHTS,
    Question,
    TurnState,
    canonical,
    effective_gate,
    field_value,
    digest,
    candidate_read_identity,
)
from . import effort
from . import routing_levers
from . import step_actions
from .savings import DEFAULT_RATES
from .backends import ask_many as backend_ask_many
from .runtime import Runtime, get_runtime
from . import provenance

__amplifier_module_type__ = "orchestrator"


def action_response(candidate: Candidate, tool_call_id: str):
    """Create real Amplifier envelopes; fail at mount/doctor on schema drift."""
    from amplifier_core.message_models import ChatResponse, ToolCall, Usage
    return ChatResponse(
        content=[],
        tool_calls=[ToolCall(id=tool_call_id, name=candidate.tool, arguments=candidate.arguments)],
        usage=Usage(input_tokens=0, output_tokens=0, total_tokens=0),
    )


def step_fields(response: Any) -> dict:
    """What this model call produced: how many tool calls, which tools (names
    only), and whether it ended the turn. The per-step router and the
    dashboard use this to tell routine continuations from real reasoning."""
    calls = field_value(response, "tool_calls", None) or []
    names = []
    for call in calls if isinstance(calls, (list, tuple)) else []:
        name = field_value(call, "name", None)
        if isinstance(name, str) and name:
            names.append(name[:64])
    fields = {"tool_calls": len(names)}
    if names:
        fields["tools"] = names[:16]
    reason = field_value(response, "finish_reason", None)
    if isinstance(reason, str) and reason:
        fields["finish_reason"] = reason[:32]
    fields["step_kind"] = "tool_call" if names else "final_answer"
    return fields


def usage_fields(response: Any) -> dict:
    """Token counts, cached-token counts and the provider's own cost figure
    (``cost_usd``, None when the provider has no rate data), plus the model
    that actually served the request. Feeds the savings estimate."""
    usage = field_value(response, "usage", {}) or {}
    fields = {k: field_value(usage, k) for k in ("input_tokens", "output_tokens", "total_tokens")}
    for key in ("cache_read_tokens", "cache_write_tokens"):
        value = field_value(usage, key)
        if isinstance(value, int) and not isinstance(value, bool):
            fields[key] = value
    cost = field_value(usage, "cost_usd")
    if cost is not None and not isinstance(cost, bool):
        try:
            fields["cost_usd"] = round(float(cost), 8)
        except (TypeError, ValueError):
            pass
    served = field_value(response, "model") or field_value(field_value(response, "metadata", {}) or {}, "model")
    if isinstance(served, str) and served:
        fields["served_model"] = served[:80]
    return fields


# HC04 ("opt-in model routing with escalation"): test-failure detection.
# Conservative and text-pattern based -- never raises, never inspects
# arguments, only the already-observed tool result text (capped at 20k
# chars before matching). See docs/ARCHITECTURE.md.
_TEST_TOOL_NAMES = frozenset({"bash", "python_check", "run_tests"})
_TEST_FAILURE_PATTERNS = (
    re.compile(r"FAILED \("),
    re.compile(r"FAIL:"),
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"(?m)^Error:"),
    re.compile(r"\b\d+\s+failed\b"),
)


def _is_test_tool(tool_key: str) -> bool:
    return tool_key in _TEST_TOOL_NAMES or "test" in tool_key


def _tool_result_text(result: Any) -> str:
    """Best-effort, defensive stringification of a tool result, capped at 20k chars.

    Never raises: an object with none of the common output fields falls
    back to ``str(result)``, and any exception there yields an empty string.
    """
    for attr in ("output", "result", "stdout", "content"):
        value = field_value(result, attr, None)
        if isinstance(value, str):
            return value[:20000]
    try:
        return str(result)[:20000]
    except Exception:
        return ""


def _result_chars(result: Any) -> int:
    """Serialized size of a tool result (what enters the next prompt). Never raises."""
    try:
        output = field_value(result, "output", None)
        if output is None:
            output = field_value(result, "error", None)
        if isinstance(output, str):
            return len(output)
        import json as _json
        return len(_json.dumps(output, default=str))
    except Exception:  # noqa: BLE001
        return 0


def _test_failure_observed(text: str) -> bool:
    return any(pattern.search(text) for pattern in _TEST_FAILURE_PATTERNS)


# HC05 ("judge-driven escalation and phase classification", opt-in): ask the
# configured DecisionBackend ONE Choice question, via the identical
# DecisionRequest/DecisionResult contract the read-shortcut
# (DecisionService.choose) uses -- so it inherits `Policy.timeout_ms` and the
# same `backend.external and not allow_external_state` gate the read-shortcut
# enforces (Jev refuses without consent), and produces a normal receipt.
# Bypasses DecisionService.choose entirely: there is no prepared action here,
# only a judgment question, so `candidates=()` and the backend's own
# next_action answer (degenerate to SLOW-only in that shape) is never built
# or consulted. See docs/ARCHITECTURE.md.
_JUDGE_STATE_TASK_PROMPT_CHARS = 300
_JUDGE_STATE_TOOL_RESULT_CHARS = 600


def _judge_context_needed(policy: Any) -> bool:
    """Whether ObservedTool.execute must track tool names/last result text
    this turn -- only while a judge mechanism is actually configured.
    Inert otherwise, matching every other HC0x seam."""
    model_routing = policy.model_routing
    effort_routing = policy.effort_routing
    return bool(
        (model_routing and model_routing.get("escalation_judge") in ("judge", "decomposed"))
        or (effort_routing and effort_routing.get("phase_judge"))
    )


def _first_user_text(request: Any) -> str:
    """Best-effort text of the FIRST user message in the request, or ``""``.
    Never raises: a malformed message shape is simply skipped."""
    try:
        messages = list(field_value(request, "messages") or [])
    except Exception:
        return ""
    for message in messages:
        if field_value(message, "role") != "user":
            continue
        content = field_value(message, "content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                text
                for block in content
                if isinstance(text := field_value(block, "text"), str)
            ]
            return "".join(parts)
        return ""
    return ""


def _turn_user_text(request: Any) -> str:
    """Text of the LATEST real user message (this turn's prompt), skipping
    tool-result carriers and messages that are only injected
    ``<system-reminder>`` envelopes. ``""`` when none. Never raises."""
    try:
        messages = list(field_value(request, "messages") or [])
    except Exception:
        return ""
    for message in reversed(messages):
        if field_value(message, "role") != "user":
            continue
        content = field_value(message, "content")
        if isinstance(content, list):
            content = "".join(
                text for block in content
                if field_value(block, "type") in (None, "text")
                and isinstance(text := field_value(block, "text"), str)
            )
        if not isinstance(content, str):
            continue
        stripped = re.sub(r"<system-reminders\b.*?</system-reminders>|<system-reminder\b.*?</system-reminder>",
                          "", content, flags=re.S).strip()
        if stripped:
            return stripped
    return ""


# Turn-start difficulty router question. Same wording as
# evals/difficulty/probe.py (measured there: jev AUC 0.83 on SWE-bench
# Verified difficulty labels) -- change both together.
DIFFICULTY_INSTRUCTIONS = (
    "Classify this software engineering task by how much work it takes an expert "
    "engineer who is new to the codebase."
)
DIFFICULTY_CRITERIA = {
    "simple": "A small, well-specified, localized change: the fix location is clear and it takes "
              "minutes (a typo, a one-function bug, a documented contract, an obvious edge case).",
    "complex": "Substantial work: the root cause must be investigated across an unfamiliar codebase, "
               "several files or subsystems change, or the behavior is subtle; it takes an hour or more.",
}
_DIFFICULTY_STATE_CHARS = 2500


_SCOPE_SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", ".tox",
                              ".mypy_cache", ".pytest_cache", "dist", "build", ".amplifier", ".swe"})
_workspace_file_counts: dict[str, int] = {}


def workspace_file_count(root: str, limit: int) -> int:
    """Files under ``root`` (skipping VCS/dependency/cache dirs), counted only
    up to ``limit + 1`` -- the walk stops as soon as the answer is "more than
    limit", so a huge repository costs a bounded amount. Memoized per
    process. Never raises (an unreadable root counts as 0)."""
    key = f"{root}|{limit}"
    if key in _workspace_file_counts:
        return _workspace_file_counts[key]
    count = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SCOPE_SKIP_DIRS]
            count += len(filenames)
            if count > limit:
                break
    except OSError:
        count = 0
    _workspace_file_counts[key] = count
    return count


def _note_start(service: Any, mechanism: str, judge_seconds: float) -> None:
    turn = getattr(service, "turn", None)
    if turn is not None:
        turn.start_mechanism = mechanism
        turn.judge_seconds = float(judge_seconds or 0.0)


def session_working_dir(service: Any) -> str:
    """The session's working directory: the kernel's ``session.working_dir``
    capability (set per session by the CLI, amplifier-runtime and Studio),
    falling back to the process cwd."""
    coordinator = getattr(service, "coordinator", None)
    getter = getattr(coordinator, "get_capability", None)
    if callable(getter):
        try:
            value = getter("session.working_dir")
        except Exception:  # noqa: BLE001
            value = None
        if isinstance(value, (str, os.PathLike)) and str(value):
            return str(value)
    return os.getcwd()


async def decide_start_tier(service: Any, request: Any, model_routing: dict[str, Any],
                            decision_id: str | None, user_model: str | None = None) -> str:
    """``"cheap"`` or ``"strong"`` for this turn (called once, at its first
    slow request). ``start_policy: cheap`` (default) keeps the pre-router
    behavior and emits nothing. ``rules`` uses prompt length. ``judge`` asks
    the configured backend one typed question and falls back to rules on
    any abstain / block / error. Never raises (CancelledError propagates)."""
    policy = model_routing.get("start_policy", "cheap")
    if user_model:
        # The user explicitly picked a model for this session (a UI model
        # picker): every turn runs on it -- no cheap start, no escalation.
        await service.emit("difficulty_judged", {
            "backend": service.backend.name, "choice": "strong", "probabilities": None,
            "duration_ms": 0.0, "reason_code": "user_model_strong", "model": user_model[:80],
            "mode": service.policy.mode,
        }, decision_id)
        _note_start(service, "user:model_pick", 0.0)
        return "strong"
    if policy == "cheap":
        return "cheap"
    task = _turn_user_text(request)
    turn = getattr(service, "turn", None)
    scope_limit = model_routing.get("cheap_max_workspace_files")
    scope_files = None
    if scope_limit is not None:
        files = await asyncio.to_thread(workspace_file_count, session_working_dir(service), scope_limit)
        if files > scope_limit:
            if not (model_routing.get("large_repo") and policy == "judge" and task):
                await service.emit("difficulty_judged", {
                    "backend": service.backend.name, "choice": "strong", "probabilities": None,
                    "duration_ms": 0.0, "reason_code": "scope_strong", "state_chars": len(task),
                    "candidate_count": files, "mode": service.policy.mode,
                    **_profile_field(service),
                }, decision_id)
                _note_start(service, "rule:scope_gate", 0.0)
                return "strong"
            scope_files = files  # large_repo lever: ask the judge anyway
    min_chars = model_routing.get("complex_min_prompt_chars", 2000)
    tier = "strong" if len(task) >= min_chars else "cheap"
    decided_by, p_complex, p_edit, duration_ms = "rules", None, None, 0.0
    if policy == "judge" and task:
        state = {"task": task[:_DIFFICULTY_STATE_CHARS]}
        if scope_files is not None and routing_levers.large_repo_wants_edit_question(model_routing):
            # One batched call: difficulty + "does this change code?"
            questions = [Question(name="task_difficulty", type="choice", instructions=DIFFICULTY_INSTRUCTIONS,
                                  criteria=DIFFICULTY_CRITERIA), routing_levers.edit_question()]
            batch_start = time.perf_counter()
            answers = await _ask_judges_many(service, questions=questions, state=state) or {}
            duration_ms = (time.perf_counter() - batch_start) * 1000
            choice, probability = answers.get("task_difficulty", (None, None))
            edit_choice, edit_probability = answers.get(routing_levers.EDIT_QUESTION_NAME, (None, None))
            if edit_choice is not None and edit_probability is not None:
                p_edit = edit_probability if edit_choice == "edits" else 1.0 - edit_probability
        else:
            choice, probability, duration_ms = await _ask_judge_choice(
                service, question_name="task_difficulty", instructions=DIFFICULTY_INSTRUCTIONS,
                criteria=DIFFICULTY_CRITERIA, state=state,
            )
        if choice is not None and probability is not None:
            p_complex = probability if choice == "complex" else 1.0 - probability
            decided_by = "judge"
    label, tier_model, tier_effort = None, None, None
    if scope_files is not None:
        # Scope-gated turn: the host model unless the large_repo lever allows
        # a cheap tier; any judge failure keeps the host model.
        allowed = p_complex is not None and routing_levers.large_repo_allows_cheap(
            model_routing, p_complex, p_edit)
        picked = None
        if allowed:
            # The tier its difficulty earns; a read-only turn the tiers call
            # complex (allowed via require: either) gets start_model.
            picked = routing_levers.choose_tier(model_routing, p_complex) or ("cheap", None, None)
        tier = "cheap" if picked else "strong"
        if picked and picked[1]:  # a configured tier (not the shipped start_model)
            label, tier_model, tier_effort = picked
        reason = f"scope_judge_{tier}" if p_complex is not None else "scope_fallback_strong"
    elif decided_by == "judge":
        picked = routing_levers.choose_tier(model_routing, p_complex)
        tier = "cheap" if picked else "strong"
        if picked and picked[1]:  # a configured tier (not the shipped start_model)
            label, tier_model, tier_effort = picked
        reason = f"judge_{tier}"
    else:
        reason = f"rules_{tier}"
    if tier == "strong":
        tier_effort = routing_levers.strong_effort(model_routing, p_complex)
        label = "strong_effort" if tier_effort else None
    if turn is not None and hasattr(turn, "tier_label"):
        turn.tier_label, turn.tier_model, turn.tier_effort = label, tier_model, tier_effort
    probabilities = None
    if p_complex is not None or p_edit is not None:
        probabilities = {k: v for k, v in (("complex", p_complex), ("edits_code", p_edit)) if v is not None}
    data = {
        "backend": service.backend.name, "choice": tier, "probabilities": probabilities,
        "duration_ms": duration_ms, "reason_code": reason,
        "state_chars": len(task), "mode": service.policy.mode, **_profile_field(service),
    }
    if scope_files is not None:
        data["candidate_count"] = scope_files
    if label is not None:
        data["tier"] = label
        data["requested_model"] = tier_model
        data["requested_effort"] = tier_effort
    await service.emit("difficulty_judged", data, decision_id)
    if scope_files is not None and p_complex is None:
        start_mech = "rule:scope_gate"
    elif decided_by == "judge":
        start_mech = f"{service.backend.name}:" + ("edits_code" if p_edit is not None else "task_difficulty")
    else:
        start_mech = "rule:prompt_length"
    _note_start(service, start_mech, duration_ms / 1000 if policy == "judge" and task else 0.0)
    return tier


def _profile_field(service: Any) -> dict:
    profile = getattr(service.policy, "profile", None)
    return {"profile": profile} if profile else {}


def _judge_state(
    request: Any, turn: TurnState, phase: str, max_state_chars: int
) -> dict[str, Any]:
    """Compact, bounded, JSON-able state for a judge Choice question: task
    prompt head, phase, this turn's slow-request count, tool names used so
    far, a capped excerpt of the last tool result, and the two HC04 failure
    signals. Trimmed further (last_tool_result_excerpt, then
    task_prompt_head) if the canonical serialization would still exceed
    ``max_state_chars``. Never raises."""
    state: dict[str, Any] = {
        "task_prompt_head": _first_user_text(request)[:_JUDGE_STATE_TASK_PROMPT_CHARS],
        "phase": phase,
        "slow_requests_seen": turn.slow_requests_seen,
        "tool_names_used": sorted(turn.tool_names_used),
        "last_tool_result_excerpt": turn.last_tool_result_text[:_JUDGE_STATE_TOOL_RESULT_CHARS],
        "test_failure_seen": turn.test_failure_seen,
        "provider_errors_seen": turn.provider_errors_seen,
    }
    if len(canonical(state)) <= max_state_chars:
        return state
    state = {**state, "last_tool_result_excerpt": ""}
    if len(canonical(state)) <= max_state_chars:
        return state
    return {**state, "task_prompt_head": ""}


async def _ask_judge_choice(
    service: Any,
    *,
    question_name: str,
    instructions: str,
    criteria: dict[str, str],
    state: dict[str, Any],
) -> tuple[str | None, float | None, float]:
    """Ask ONE Choice question via ``service.backend``, honoring the same
    ``backend.external and not policy.allow_external_state`` gate
    ``DecisionService.choose`` enforces (service.py ~line 110). Returns
    ``(choice, probability, duration_ms)``: ``choice`` is ``None`` on any
    policy-block/abstain/timeout/backend-error -- the caller always falls
    back to its deterministic rules in that case. Never raises
    (``CancelledError`` propagates).
    """
    if service.backend.external and not service.policy.allow_external_state:
        return None, None, 0.0
    question = Question(
        name=question_name, type="choice", instructions=instructions, criteria=criteria
    )
    decision_request = DecisionRequest(state=state, candidates=(), questions=(question,))
    start = time.perf_counter()
    deadline = asyncio.get_running_loop().time() + service.policy.timeout_ms / 1000
    try:
        async with asyncio.timeout_at(deadline):
            result = await service.backend.ask(decision_request)
    except asyncio.CancelledError:
        raise
    except Exception:
        return None, None, (time.perf_counter() - start) * 1000
    duration_ms = (time.perf_counter() - start) * 1000
    answer = result.answers.get(question_name)
    if answer is None or not answer.probabilities:
        return None, None, duration_ms
    choice = max(answer.probabilities, key=answer.probabilities.get)
    return choice, answer.probabilities[choice], duration_ms


def _escalation_judge_due(model_routing: dict[str, Any] | None, turn: TurnState) -> bool:
    """Whether HC05's escalation judge WOULD be asked for the upcoming slow
    request -- mirrors the elif chain in RoutedProvider.complete without
    mutating turn state, so it can be evaluated safely before
    ``turn.slow_requests_seen`` is incremented for real. Single source of
    truth for both the HC08 batching pre-check and the sequential path
    below (which reuses this same function, so the two can never drift).
    """
    if not model_routing or turn.escalated or turn.start_tier == "strong":
        return False
    if model_routing.get("escalate_on_test_failure") and turn.test_failure_seen:
        return False
    max_requests = model_routing.get("max_requests_before_escalation")
    prospective_slow_requests_seen = turn.slow_requests_seen + 1
    if max_requests is not None and prospective_slow_requests_seen > max_requests:
        return False
    return (
        model_routing.get("escalation_judge", "rules") == "judge"
        and prospective_slow_requests_seen > 1
    )


# HC08 ("one call per decision point", opt-in): the two Question shapes
# combined by a batched ask_many() call, identical in wording to the ones
# `_ask_judge_choice` builds inline for the sequential path -- kept in one
# place so the batched and sequential paths can never silently diverge in
# what they ask.
def _phase_question() -> Question:
    return Question(
        name="phase_classification", type="choice",
        instructions="Classify the current turn's phase for effort routing.",
        criteria=dict(effort.PHASE_CRITERIA),
    )


def _escalation_question() -> Question:
    return Question(
        name="escalation_judge", type="choice",
        instructions=(
            "Decide whether to keep using the cheaper model or "
            "escalate to the stronger model now."
        ),
        criteria={
            "continue_cheap": "The cheaper model is making progress; keep going.",
            "escalate": (
                "The task is beyond the cheaper model, or the plan has "
                "derailed; switch to the stronger model now."
            ),
        },
    )


async def _ask_judges_many(
    service: Any, *, questions: list[Question], state: dict[str, Any]
) -> dict[str, tuple[str | None, float | None]] | None:
    """HC08: ask every named ``Question`` in ONE ``ask_many()`` call,
    honoring the identical external-state gate ``_ask_judge_choice``
    enforces. Returns ``{question_name: (choice, probability)}`` on
    success -- including a policy-blocked or abstained call, which returns
    every question as ``(None, None)``, exactly what the sequential
    per-question path would also produce for the identical gate. Returns
    ``None`` only on an actual backend failure/timeout, signalling the
    caller to fall back to the sequential per-question path and count a
    batch fallback. Never raises (``CancelledError`` propagates).
    """
    if service.backend.external and not service.policy.allow_external_state:
        return {q.name: (None, None) for q in questions}
    decision_request = DecisionRequest(state=state, candidates=(), questions=tuple(questions))
    deadline = asyncio.get_running_loop().time() + service.policy.timeout_ms / 1000
    try:
        async with asyncio.timeout_at(deadline):
            result = await backend_ask_many(service.backend, decision_request)
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    answers: dict[str, tuple[str | None, float | None]] = {}
    for question in questions:
        answer = result.answers.get(question.name)
        if answer is None or not answer.probabilities:
            answers[question.name] = (None, None)
            continue
        choice = max(answer.probabilities, key=answer.probabilities.get)
        answers[question.name] = (choice, answer.probabilities[choice])
    return answers


# HC10 ("decomposed escalation signals", opt-in): five atomic yes/no
# questions asked in ONE batched ask_many() call, combined in code via a
# weighted sum instead of trusting a single judged verdict -- see
# DECOMPOSED_ESCALATION_SIGNALS/DEFAULT_ESCALATION_WEIGHTS (contracts.py)
# and docs/ARCHITECTURE.md. Every instruction is phrased with positive
# polarity (no negations) so a "yes" always means the signal is present.
_DECOMPOSED_UNCERTAIN_BAND = 0.1
_DECOMPOSED_SIGNAL_TEXT = {
    "plan_derailed": "the agent is repeating itself or has abandoned the stated plan",
    "repeated_tool_errors": "the last tool results contain repeated errors or failures",
    "tests_failing": "tests or checks are failing after edits",
    "unfamiliar_code": "the task requires understanding code the agent has not read",
    "beyond_tier": "the task needs deeper reasoning than a fast model reliably provides",
}


def _decomposed_escalation_questions() -> list[Question]:
    return [
        Question(
            name=signal,
            type="choice",
            instructions=text,
            criteria={
                "yes": f"True: {text}.",
                "no": f"False: {text} is not the case.",
            },
        )
        for signal, text in _DECOMPOSED_SIGNAL_TEXT.items()
    ]


async def _ask_decomposed_signals(
    service: Any, *, state: dict[str, Any]
) -> dict[str, float | None]:
    """HC10: ask all five decomposed escalation questions in ONE
    ``ask_many()`` call, honoring the identical external-state gate every
    other HC0x judge ask enforces. Returns ``{signal_name:
    probability_of_yes}`` -- every signal ``None`` on a policy-block,
    abstain, or backend failure/timeout (the caller then falls back to
    the deterministic rules only). Never raises (``CancelledError``
    propagates).
    """
    questions = _decomposed_escalation_questions()
    if service.backend.external and not service.policy.allow_external_state:
        return {q.name: None for q in questions}
    decision_request = DecisionRequest(state=state, candidates=(), questions=tuple(questions))
    deadline = asyncio.get_running_loop().time() + service.policy.timeout_ms / 1000
    try:
        async with asyncio.timeout_at(deadline):
            result = await backend_ask_many(service.backend, decision_request)
    except asyncio.CancelledError:
        raise
    except Exception:
        return {q.name: None for q in questions}
    signals: dict[str, float | None] = {}
    for question in questions:
        answer = result.answers.get(question.name)
        if answer is None or not answer.probabilities:
            signals[question.name] = None
            continue
        signals[question.name] = answer.probabilities.get("yes")
    return signals


# HC11 ("pre-tool risk classification in shadow mode", opt-in): three
# atomic questions asked BEFORE each tool execution, purely observational
# -- the answer never blocks, modifies, or approves the tool call; native
# approvals remain the sole authority. See docs/ARCHITECTURE.md.
def _tool_risk_state(tool_key: str, input: dict[str, Any]) -> dict[str, Any]:
    """Redacted judge input: tool name and argument KEYS only -- never
    argument values. Never raises."""
    try:
        keys = sorted(input.keys()) if isinstance(input, dict) else []
    except Exception:
        keys = []
    return {"tool": tool_key, "argument_keys": keys}


def _tool_risk_questions() -> list[Question]:
    return [
        Question(
            name="destructive",
            type="choice",
            instructions="the command deletes, overwrites or force-pushes data",
            criteria={
                "yes": "The command deletes, overwrites, or force-pushes data.",
                "no": "The command does not delete, overwrite, or force-push data.",
            },
        ),
        Question(
            name="touches_production",
            type="choice",
            instructions="the action affects a production system, credentials or billing",
            criteria={
                "yes": "The action affects a production system, credentials, or billing.",
                "no": "The action does not affect a production system, credentials, or billing.",
            },
        ),
        Question(
            name="category",
            type="choice",
            instructions="Classify the kind of action this tool call performs.",
            criteria={
                "read": "The tool call reads data without modifying anything.",
                "write": "The tool call writes, creates, or modifies data.",
                "execute": "The tool call executes a command or program.",
                "network": "The tool call makes a network request.",
                "other": "The tool call does not fit the other categories.",
            },
        ),
    ]


async def _ask_tool_risk(
    service: Any, *, state: dict[str, Any]
) -> dict[str, tuple[str | None, dict[str, float] | None]] | None:
    """HC11: ask the three tool-risk questions in ONE ``ask_many()``
    call, honoring the identical external-state gate every other HC0x
    judge ask enforces. Returns ``{question_name: (choice,
    probabilities)}`` -- including a policy-blocked/abstained call,
    which returns every question as ``(None, None)``. Returns ``None``
    only on an actual backend failure/timeout. Never raises
    (``CancelledError`` propagates); the caller never blocks or modifies
    tool execution on the result -- classification is observational only.
    """
    questions = _tool_risk_questions()
    if service.backend.external and not service.policy.allow_external_state:
        return {q.name: (None, None) for q in questions}
    decision_request = DecisionRequest(state=state, candidates=(), questions=tuple(questions))
    deadline = asyncio.get_running_loop().time() + service.policy.timeout_ms / 1000
    try:
        async with asyncio.timeout_at(deadline):
            result = await backend_ask_many(service.backend, decision_request)
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    answers: dict[str, tuple[str | None, dict[str, float] | None]] = {}
    for question in questions:
        answer = result.answers.get(question.name)
        if answer is None or not answer.probabilities:
            answers[question.name] = (None, None)
            continue
        choice = max(answer.probabilities, key=answer.probabilities.get)
        answers[question.name] = (choice, dict(answer.probabilities))
    return answers


_UNSEEN = object()


def _push(values: list, value: float, cap: int = 64) -> None:
    values.append(float(value))
    if len(values) > cap:
        del values[0]


def _mean(values: list, default: float) -> float:
    return sum(values) / len(values) if values else float(default)


class RoutedProvider:
    """Preserve the Provider protocol while intercepting complete() boundaries.

The facade is transport-transparent: `stream` is mirrored iff the wrapped
provider actually has it, and proxied verbatim -- the fast path is attempted
only in complete(). loop-streaming's streaming branch cannot dispatch tool
calls (_has_pending_tools is hard-coded False), so synthesizing a response
on that transport would fail open (drop the action silently) instead of
failing closed (defer to the LLM). See docs/COMPATIBILITY.md and
docs/UPSTREAM_CONTRACT.md.
"""
    def __init__(self, provider: Any, runtime: Runtime, tools: dict[str, Any],
                 response_factory=action_response, provider_key: str | None = None):
        self._provider = provider
        self._runtime = runtime
        self._tools = tools
        self._response_factory = response_factory
        self._provider_key = provider_key or getattr(provider, "name", "unknown")
        self._synthetic_responses: dict[int, Any] = {}
        # The provider's default model as first seen; a later change means the
        # user switched models mid-session (e.g. a UI model picker).
        self._initial_default_model: Any = _UNSEEN
        # Efficiency-receipt bookkeeping for this session (docs/GOAL.md).
        self._eff: dict[str, Any] = {"project": None, "traffic": None, "turn_id": None, "cur_tier": None,
                                     "prev_tier": None, "cur_mech": None, "prev_mech": None,
                                     "judge_charged": False, "host_calls_this_turn": 0, "host_last": None,
                                     "host_sum_usd": 0.0, "host_sum_s": 0.0, "host_n": 0}
        # Per-step decision point: prepared actions awaiting their receipt
        # (priced when the next model call shows what was skipped).
        self._pending_prepared: list[dict[str, Any]] = []

    def _user_selected_model(self, service: Any) -> str | None:
        """The model the user explicitly chose for this session, or None.

        Two signals: amplifier-runtime records an in-session pick as the
        ``ui.model_override`` session-state marker, and any mid-session
        change of the wrapped provider's ``default_model`` is a user switch.
        A model set before the session starts (settings, ``--model``) is
        indistinguishable from the configured default and stays routable."""
        current = getattr(self._provider, "default_model", None)
        if self._initial_default_model is _UNSEEN:
            self._initial_default_model = current
        state = getattr(getattr(service, "coordinator", None), "session_state", None)
        marker = state.get("ui.model_override") if isinstance(state, dict) else None
        if isinstance(marker, dict) and marker.get("model"):
            return str(marker["model"])
        if isinstance(current, str) and current and current != self._initial_default_model:
            return current
        return None

    def __getattr__(self, name: str):
        if name == "stream":
            inner = getattr(self._provider, "stream", None)
            if not callable(inner):
                raise AttributeError(name)  # mirror absence exactly
            return self._stream_proxy
        return getattr(self._provider, name)

    @property
    def name(self):
        return self._provider.name

    def get_info(self):
        return self._provider.get_info()

    async def list_models(self):
        return await self._provider.list_models()

    def _provider_matches(self, needle: str) -> bool:
        needle = needle.lower()
        names = (self._provider_key, getattr(self._provider, "name", None))
        return any(isinstance(n, str) and needle in n.lower() for n in names)

    def parse_tool_calls(self, response):
        # Provider-specific parsing must not attempt to decode a Jev envelope.
        if id(response) in self._synthetic_responses:
            return response.tool_calls or []
        return self._provider.parse_tool_calls(response)

    async def complete(self, request, **kwargs):
        service = self._runtime.service
        step_started = time.perf_counter()
        # Per-step decision point (step_actions.py, opt-in): classify this
        # step from deterministic features of the request; at a predictable
        # read/status step, offer the judge prepared read-only candidates.
        step = self._step_classify(service, request) if service.policy.step_actions else None
        if step is not None and step["offer"]:
            candidate = await service.choose(request, self._tools, extra_candidates=step["candidates"],
                                             force=True, workspace_paths=False)
            await self._step_after_choose(service, step, candidate)
        else:
            candidate = await service.choose(request, self._tools)
        turn = service.turn
        assert turn is not None
        if candidate:
            tool_call_id = "fd_" + uuid4().hex[:24]
            # Build the envelope BEFORE committing an execution mapping. If the
            # core schema has drifted, make the failure visible, then use slow.
            try:
                response = self._response_factory(candidate, tool_call_id)
            except Exception as exc:
                await service.emit("fallback", {"reason_code": "envelope_incompatible",
                                                 "exception_type": type(exc).__name__})
                await service.emit("routed", {"route": "slow", "destination": self._provider_key,
                                              "reason_code": "envelope_incompatible",
                                              "transport_measured": "provider-complete"})
            else:
                turn.used.add(candidate.fingerprint)
                turn.fast_streak += 1
                turn.fast_total += 1
                # HC02a: a fast submission is a completed read too -- record
                # it so a second proposal of the same unchanged file is
                # suppressed on the next decision in this turn.
                if service.policy.suppress_completed_reads:
                    identity = candidate_read_identity(candidate, self._tools)
                    if identity is not None:
                        turn.completed_reads[identity[0]] = identity[1]
                await service.emit("routed", {"mode": service.policy.mode,
                    "backend": service.backend.name, "policy_version": service.policy.version,
                    "route": "fast", "destination": candidate.tool,
                    "tool_call_id": tool_call_id,
                    "selected_candidate": candidate.id, "reason_code": "prepared_action_selected",
                    "status": "submitted_to_upstream", "transport_measured": "provider-complete"})
                turn.tool_decisions[tool_call_id] = {
                    "decision_id": service.last_decision_id, "tool": candidate.tool,
                    "arguments_hash": digest(candidate.arguments), "claimed": False,
                }
                self._synthetic_responses[id(response)] = response
                if step is not None:
                    # Priced once the next model call shows what was skipped.
                    mechanism = f"{service.backend.name}:next_action"
                    self._pending_prepared.append({
                        "tool": candidate.tool, "arguments": dict(candidate.arguments),
                        "tool_call_id": tool_call_id, "mechanism": mechanism,
                        "decision_s": time.perf_counter() - step_started})
                    await self._emit_step(service, step, "prepared", mechanism,
                                          {"candidate_origin": candidate.origin})
                else:
                    await self._receipt_prepared_action(service, turn, candidate.tool,
                                                        time.perf_counter() - step_started)
                return response
        turn.fast_streak = 0
        service.slow_total += 1
        model = field_value(request, "model") or "provider-default"
        decision_id = service.last_decision_id
        provider_call_id = "provider_" + uuid4().hex
        efficiency_routed_model = None
        # HC03 ("phase-specific effort routing", opt-in): entirely skipped
        # -- no attribute touched, no event emitted -- when the policy has
        # no effort_routing configured. See effort.py and docs/EVENTS.md.
        effort_routing = service.policy.effort_routing
        # HC04 ("opt-in model routing with escalation", opt-in): entirely
        # skipped -- no attribute touched, no event emitted -- when the
        # policy has no model_routing configured. See docs/ARCHITECTURE.md.
        model_routing = service.policy.model_routing
        effort_applied_this_request = False
        phase = None
        # Turn-start difficulty router: decided once, before effort and model
        # routing, so both can follow the same per-turn tier.
        if model_routing and turn.start_tier is None and model_routing.get("start_policy", "cheap") != "cheap":
            turn.start_tier = await decide_start_tier(service, request, model_routing, decision_id,
                                                         user_model=self._user_selected_model(service))
        if effort_routing:
            phase = effort.classify_phase(request)

        # HC08 ("one call per decision point", opt-in): when a phase judge
        # AND an escalation judge would BOTH fire for this same request,
        # ask them in ONE ask_many() call instead of two separate
        # backend.ask() calls below. `_escalation_judge_due` mirrors the
        # elif chain inside the model_routing block further down without
        # mutating turn state, so it is safe to consult here, before that
        # block's own `turn.slow_requests_seen += 1`. Falls back to the
        # identical sequential path (unchanged) whenever batching is off,
        # only one judge is due, or the batched call itself fails.
        phase_judge_due = bool(effort_routing and effort_routing.get("phase_judge"))
        escalation_judge_due = _escalation_judge_due(model_routing, turn)
        batched_answers: dict[str, tuple[str | None, float | None]] | None = None
        batch_duration_ms = 0.0
        if service.policy.decision_batching and phase_judge_due and escalation_judge_due:
            batch_questions = [_phase_question(), _escalation_question()]
            batch_state = _judge_state(request, turn, phase, service.policy.max_state_chars)
            batch_start = time.perf_counter()
            batched_answers = await _ask_judges_many(
                service, questions=batch_questions, state=batch_state
            )
            batch_duration_ms = (time.perf_counter() - batch_start) * 1000
            if batched_answers is None:
                turn.batch_fallbacks += 1
            else:
                await service.emit("decided_batch", {
                    "backend": service.backend.name,
                    "question_ids": [q.name for q in batch_questions],
                    "n_questions": len(batch_questions),
                    "duration_ms": batch_duration_ms,
                    "mode": service.policy.mode,
                }, decision_id)

        if effort_routing:
            # HC05 ("judge-driven phase classification", opt-in): ask the
            # judge to classify the phase instead of trusting the
            # deterministic classifier alone. An abstain/blocked/error
            # answer, or one below HC09's phase confidence gate, leaves
            # `phase` as the deterministic classification.
            if phase_judge_due:
                if batched_answers is not None:
                    judged_choice, judged_probability = batched_answers["phase_classification"]
                    judged_duration_ms = batch_duration_ms
                else:
                    judge_state = _judge_state(request, turn, phase, service.policy.max_state_chars)
                    judged_choice, judged_probability, judged_duration_ms = await _ask_judge_choice(
                        service,
                        question_name="phase_classification",
                        instructions="Classify the current turn's phase for effort routing.",
                        criteria=dict(effort.PHASE_CRITERIA),
                        state=judge_state,
                    )
                phase_gate = effective_gate(service.policy, "phase")
                phase_passed_gate = (
                    judged_choice is not None
                    and judged_probability is not None
                    and judged_probability >= phase_gate
                )
                agreed_with_rules = judged_choice == phase if judged_choice is not None else None
                await service.emit("phase_judged", {
                    "backend": service.backend.name, "choice": judged_choice,
                    "probability": judged_probability, "agreed_with_rules": agreed_with_rules,
                    "duration_ms": judged_duration_ms,
                    "gate": phase_gate, "passed_gate": phase_passed_gate,
                }, decision_id)
                if phase_passed_gate:
                    phase = judged_choice
            explore_requests = (
                turn.explore_requests + 1 if phase == effort.PHASE_EXPLORE else turn.explore_requests
            )
            host_pinned = field_value(request, "reasoning_effort", None) is not None
            applied_effort, reason_code = effort.decide_effort(
                phase,
                effort_routing,
                explore_requests=explore_requests,
                provider_errors_seen=turn.provider_errors_seen,
                host_pinned=host_pinned,
            )
            if phase == effort.PHASE_EXPLORE:
                turn.explore_requests = explore_requests
            by_tier = effort_routing.get("by_tier")
            if turn.tier_effort is not None and not host_pinned and not turn.escalated:
                # Routing levers: the chosen tier's own effort, for the whole turn.
                applied_effort = turn.tier_effort
                reason_code = f"tier_{turn.tier_label}"
            elif (by_tier is not None and turn.start_tier in by_tier and by_tier[turn.start_tier] != "phase"
                    and not host_pinned):
                applied_effort = by_tier[turn.start_tier]
                reason_code = f"tier_{turn.start_tier}"
            elif effort_routing.get("monotonic") and applied_effort is not None:
                applied_effort, held = effort.hold_monotonic(applied_effort, turn.max_effort_applied)
                if held:
                    reason_code = effort.REASON_MONOTONIC_HOLD
                turn.max_effort_applied = applied_effort
            if applied_effort is not None:
                if isinstance(request, dict):
                    request["reasoning_effort"] = applied_effort
                else:
                    setattr(request, "reasoning_effort", applied_effort)
                turn.effort_routed_requests += 1
                effort_applied_this_request = True
            await service.emit("effort_routed", {
                "phase": phase, "requested_effort": applied_effort,
                "default_effort": "provider_default", "reason_code": reason_code,
                "explore_requests": turn.explore_requests,
                "provider_call_id": provider_call_id, "mode": service.policy.mode,
            }, decision_id)
        if model_routing:
            start_model = model_routing["start_model"]
            start_effort = model_routing.get("start_effort")
            max_requests = model_routing.get("max_requests_before_escalation")
            override_explicit = model_routing.get("override_explicit_model", False)
            escalate_on_test_failure = model_routing.get("escalate_on_test_failure", False)

            escalation_judge_mode = model_routing.get("escalation_judge", "rules")

            turn.slow_requests_seen += 1
            routing_phase = phase if phase is not None else effort.classify_phase(request)
            if turn.start_tier is None:
                turn.start_tier = await decide_start_tier(service, request, model_routing, decision_id,
                                                         user_model=self._user_selected_model(service))
            # A turn judged complex starts on the host model and stays there:
            # no start_model override, no mid-turn escalation.
            strong_turn = turn.start_tier == "strong"
            if not turn.escalated and not strong_turn:
                if escalate_on_test_failure and turn.test_failure_seen:
                    turn.escalated, turn.escalation_reason = True, "test_failure"
                elif max_requests is not None and turn.slow_requests_seen > max_requests:
                    turn.escalated, turn.escalation_reason = True, "max_requests"
                elif escalation_judge_mode == "judge" and turn.slow_requests_seen > 1:
                    # HC05 ("judge-driven escalation", opt-in): the
                    # deterministic triggers above are a floor -- they still
                    # escalate regardless of the judge. Only asked once
                    # neither has already fired this request, and only past
                    # the turn's first slow request.
                    if batched_answers is not None:
                        judged_choice, judged_probability = batched_answers["escalation_judge"]
                        judged_duration_ms = batch_duration_ms
                    else:
                        judge_state = _judge_state(
                            request, turn, routing_phase, service.policy.max_state_chars
                        )
                        judged_choice, judged_probability, judged_duration_ms = await _ask_judge_choice(
                            service,
                            question_name="escalation_judge",
                            instructions=(
                                "Decide whether to keep using the cheaper model or "
                                "escalate to the stronger model now."
                            ),
                            criteria={
                                "continue_cheap": "The cheaper model is making progress; keep going.",
                                "escalate": (
                                    "The task is beyond the cheaper model, or the plan has "
                                    "derailed; switch to the stronger model now."
                                ),
                            },
                            state=judge_state,
                        )
                    turn.escalation_judgements += 1
                    escalation_gate = effective_gate(service.policy, "escalation")
                    escalation_passed_gate = (
                        judged_choice == "escalate"
                        and judged_probability is not None
                        and judged_probability >= escalation_gate
                    )
                    if judged_choice is None:
                        judge_decided = "fallback_rules"
                    elif escalation_passed_gate:
                        judge_decided = "escalate"
                        turn.escalated, turn.escalation_reason = True, "judge"
                        turn.escalations_by_judge += 1
                    else:
                        judge_decided = "continue"
                    await service.emit("escalation_judged", {
                        "backend": service.backend.name, "choice": judged_choice,
                        "probability": judged_probability, "decided": judge_decided,
                        "duration_ms": judged_duration_ms, "phase": routing_phase,
                        "slow_requests_seen": turn.slow_requests_seen,
                        "mode": service.policy.mode,
                        "gate": escalation_gate, "passed_gate": escalation_passed_gate,
                    }, decision_id)
                elif escalation_judge_mode == "decomposed" and turn.slow_requests_seen > 1:
                    # HC10 ("decomposed escalation signals", opt-in): five
                    # atomic yes/no probabilities, asked in ONE batched
                    # ask_many() call, combined in code via a weighted
                    # sum -- never a single trusted verdict. The
                    # deterministic triggers above remain a floor.
                    judge_state = _judge_state(
                        request, turn, routing_phase, service.policy.max_state_chars
                    )
                    signals_start = time.perf_counter()
                    signal_probabilities = await _ask_decomposed_signals(
                        service, state=judge_state
                    )
                    signals_duration_ms = (time.perf_counter() - signals_start) * 1000
                    turn.escalation_judgements += 1
                    weights = {
                        **DEFAULT_ESCALATION_WEIGHTS,
                        **(model_routing.get("escalation_weights") or {}),
                    }
                    escalation_score = sum(
                        weights.get(name, 0.0) * probability
                        for name, probability in signal_probabilities.items()
                        if probability is not None
                    )
                    escalation_gate = effective_gate(service.policy, "escalation")
                    if all(p is None for p in signal_probabilities.values()):
                        signals_decided = "fallback_rules"
                    elif escalation_score >= escalation_gate + _DECOMPOSED_UNCERTAIN_BAND:
                        signals_decided = "escalate"
                        turn.escalated, turn.escalation_reason = True, "decomposed"
                        turn.escalations_by_judge += 1
                    elif escalation_score <= escalation_gate - _DECOMPOSED_UNCERTAIN_BAND:
                        signals_decided = "continue"
                    else:
                        signals_decided = "uncertain_rules_only"
                    await service.emit("escalation_signals", {
                        "backend": service.backend.name,
                        "signal_probabilities": signal_probabilities,
                        "score": escalation_score, "gate": escalation_gate,
                        "band": _DECOMPOSED_UNCERTAIN_BAND, "decided": signals_decided,
                        "duration_ms": signals_duration_ms, "phase": routing_phase,
                        "slow_requests_seen": turn.slow_requests_seen,
                        "mode": service.policy.mode,
                    }, decision_id)

            requested_model = None
            requested_effort = None
            provider_match = model_routing.get("provider_match")
            if turn.escalated:
                reason_code = f"escalated_{turn.escalation_reason}"
            elif strong_turn:
                reason_code = "start_strong"
                if (turn.tier_effort is not None and not effort_applied_this_request
                        and field_value(request, "reasoning_effort", None) is None):
                    # strong_effort lever with no effort_routing configured.
                    requested_effort = turn.tier_effort
                    if isinstance(request, dict):
                        request["reasoning_effort"] = requested_effort
                    else:
                        setattr(request, "reasoning_effort", requested_effort)
            elif provider_match and not self._provider_matches(provider_match):
                reason_code = "provider_not_matched"
            elif not self._start_model_is_cheaper(turn.tier_model or start_model):
                # e.g. a Haiku-hosted helper session: "routing down" to Sonnet
                # would cost more and run slower, so keep the host.
                reason_code = "host_already_cheaper"
            else:
                explicit_model = field_value(request, "model", None)
                if explicit_model and not override_explicit:
                    reason_code = "host_pinned"
                else:
                    start_model = turn.tier_model or start_model
                    if turn.tier_effort is not None:
                        start_effort = turn.tier_effort
                    requested_model = start_model
                    efficiency_routed_model = start_model
                    if isinstance(request, dict):
                        request["model"] = start_model
                    else:
                        setattr(request, "model", start_model)
                    # The verified installed Anthropic provider reads the
                    # per-request model from kwargs (`kwargs.get("model", ...)`),
                    # never from request.model -- see docs/ARCHITECTURE.md.
                    # Setting request.model alone would be a receipt that
                    # lies about what was actually served.
                    kwargs["model"] = start_model
                    if (
                        start_effort is not None
                        and not effort_applied_this_request
                        and field_value(request, "reasoning_effort", None) is None
                    ):
                        requested_effort = start_effort
                        if isinstance(request, dict):
                            request["reasoning_effort"] = start_effort
                        else:
                            setattr(request, "reasoning_effort", start_effort)
                    turn.model_routed_requests += 1
                    reason_code = "start_model"
            await service.emit("model_routed", {
                "phase": routing_phase, "requested_model": requested_model,
                "requested_effort": requested_effort, "reason_code": reason_code,
                "escalated": turn.escalated, "escalation_reason": turn.escalation_reason,
                "model_routed_requests": turn.model_routed_requests,
                "provider_call_id": provider_call_id, "mode": service.policy.mode,
            }, decision_id)
            # Reflect any routing-applied model in the slow_start/slow_end
            # receipts below -- otherwise they'd keep showing the
            # pre-routing value even though a different model was requested.
            model = field_value(request, "model") or model
        # Per-step action (b): a routine read-only continuation on a cheaper
        # model for THIS step only, when the price/cache math says so.
        cheap = None
        if step is not None:
            cheap = await self._step_cheaper_model(service, turn, step, request, kwargs, efficiency_routed_model)
            if cheap is not None and cheap.get("model"):
                model = cheap["model"]
            await self._emit_step(service, step, "cheaper_model" if cheap and cheap.get("model") else "full",
                                  (cheap or {}).get("mechanism") or f"rule:{step['kind']}", cheap)
        step_model = cheap.get("model") if cheap else None
        if step_model:
            try:
                response, seconds = await self._call_provider(service, turn, request, kwargs, model,
                                                              provider_call_id, decision_id, model_routing)
            except asyncio.CancelledError:
                raise
            except Exception:
                response, discard = None, "error"
            else:
                discard = self._cheap_step_discard_reason(step, response)
            if discard is not None:
                if response is not None:
                    await self._receipt_discarded_cheap(service, response, seconds, step_model,
                                                        cheap["mechanism"], discard, decision_id)
                self._restore_host_model(request, kwargs, cheap)
                step_model, model = None, cheap.get("original_label") or "provider-default"
                provider_call_id = "provider_" + uuid4().hex
        if not step_model:
            response, seconds = await self._call_provider(service, turn, request, kwargs, model,
                                                          provider_call_id, decision_id, model_routing)
        await self._receipt_model_call(service, turn, response, efficiency_routed_model or step_model,
                                       seconds, decision_id,
                                       mechanism=cheap.get("mechanism") if step_model else None,
                                       judge_seconds=cheap.get("judge_seconds", 0.0) if step_model else None,
                                       step=step)
        return response

    async def _call_provider(self, service: Any, turn: Any, request: Any, kwargs: dict, model: Any,
                             provider_call_id: str, decision_id: Any, model_routing: Any) -> tuple[Any, float]:
        """One provider call with its slow_start/slow_end receipts."""
        await service.emit("slow_start", {"provider": self._provider_key, "model": model,
            "provider_call_id": provider_call_id,
            "route": "slow", "destination": self._provider_key, "status": "running",
            "transport_measured": "provider-complete"}, decision_id)
        start = time.perf_counter()
        try:
            # Preserve the actual request, model override, kwargs, and response identity.
            response = await self._provider.complete(request, **kwargs)
        except asyncio.CancelledError:
            await service.emit("slow_end", {"provider": self._provider_key, "model": model, **self._host_model_field(),
                "provider_call_id": provider_call_id,
                "status": "cancelled", "duration_ms": (time.perf_counter() - start) * 1000,
                "transport_measured": "provider-complete"}, decision_id)
            raise
        except Exception as exc:
            turn.provider_errors_seen += 1
            if model_routing and not turn.escalated and model_routing.get("escalate_on_provider_error"):
                turn.escalated, turn.escalation_reason = True, "provider_error"
            await service.emit("slow_end", {"provider": self._provider_key, "model": model, **self._host_model_field(),
                "provider_call_id": provider_call_id,
                "status": "error", "exception_type": type(exc).__name__,
                "duration_ms": (time.perf_counter() - start) * 1000,
                "transport_measured": "provider-complete"}, decision_id)
            raise
        seconds = time.perf_counter() - start
        await service.emit("slow_end", {"provider": self._provider_key, "model": model, **self._host_model_field(),
            "provider_call_id": provider_call_id,
            "status": "ok", "duration_ms": seconds * 1000,
            **self._served_fields(response, request, kwargs), **step_fields(response),
            "latency_kind": "provider_complete_wall_time",
            "transport_measured": "provider-complete"}, decision_id)
        return response, seconds

    def _session(self, service: Any) -> dict:
        """Session-scoped state. A facade is built per turn, but cache state,
        judge statistics and the previous turn's tier outlive the turn, so
        they live on the (per-session) DecisionService."""
        store = getattr(service, "_fd_session_state", None)
        if not isinstance(store, dict):
            store = {}
            try:
                service._fd_session_state = store
            except Exception:  # noqa: BLE001
                store = self.__dict__.setdefault("_local_session_state", {})
        return store

    def _step_state(self, service: Any) -> dict:
        return self._session(service).setdefault("step:" + str(self._provider_key), {
            "cache": {}, "asked": 0, "accepted": 0, "judge_s": [], "host_s": [], "tool_step_out": [],
            "last_prompt": None, "host_last": None, "cheap_since_host": 0, "host_last_before_cheap": None})

    def _eff_context(self, service: Any) -> dict:
        st = self._session(service).setdefault("eff:" + str(self._provider_key), self._eff)
        self._eff = st
        if st["project"] is None:
            wd = session_working_dir(service)
            try:
                from .observer import repo_context
                ctx = repo_context(Path(wd))
            except Exception:  # noqa: BLE001
                ctx = {}
            st["project"] = ctx.get("repo") or Path(wd).name or "(unknown)"
            st["traffic"] = efficiency.classify_traffic(wd)
        return st

    def _eff_turn(self, st: dict, turn: Any) -> None:
        if turn is not None and st["turn_id"] != turn.id:
            st["prev_tier"], st["cur_tier"] = st["cur_tier"], turn.start_tier
            st["prev_mech"], st["cur_mech"] = st["cur_mech"], turn.start_mechanism
            st["turn_id"], st["judge_charged"], st["host_calls_this_turn"] = turn.id, False, 0
        elif turn is not None:
            st["cur_tier"], st["cur_mech"] = turn.start_tier, turn.start_mechanism

    async def _receipt_model_call(self, service: Any, turn: Any, response: Any, routed_model: Any,
                                  seconds: float, decision_id: Any, *, mechanism: str | None = None,
                                  judge_seconds: float | None = None, step: dict | None = None) -> None:
        """Efficiency receipts for one completed model call. Never raises.

        ``mechanism``/``judge_seconds`` override the turn-start router's
        attribution for a per-step cheaper-model call."""
        try:
            st = self._eff_context(service)
            self._eff_turn(st, turn)
            usage = usage_fields(response)
            host = getattr(self._provider, "default_model", None)
            host = host if isinstance(host, str) else None
            per_step = mechanism is not None
            mech = mechanism or (turn.start_mechanism if turn is not None else None) or "router"
            judge_s = float((judge_seconds or 0.0) if per_step else (getattr(turn, "judge_seconds", 0.0) or 0.0))
            common = {"project": st["project"], "traffic": st["traffic"]}
            sst = self._step_state(service)
            now = time.monotonic()
            receipts = []
            if isinstance(routed_model, str):
                if per_step:
                    last = sst["host_last"]
                    warm = last is not None and now - last < 300
                    receipts.append(efficiency.cheaper_model_step(
                        usage=usage, seconds=seconds, served_model=routed_model, host_model=host,
                        host_cache_warm=warm, mechanism=mech, judge_seconds=judge_s, **common))
                    if sst["cheap_since_host"] == 0:
                        sst["host_last_before_cheap"] = last
                    sst["cheap_since_host"] += 1
                else:
                    warm = st["host_last"] is not None and now - st["host_last"] < 300
                    receipts.append(efficiency.cheaper_model_step(
                        usage=usage, seconds=seconds, served_model=routed_model, host_model=host,
                        host_cache_warm=warm, mechanism=mech,
                        judge_seconds=0.0 if st["judge_charged"] else judge_s, **common))
                    st["judge_charged"] = True
            else:
                if judge_s and not st["judge_charged"] and turn is not None and turn.start_tier == "strong":
                    receipts.append(efficiency.judge_overhead(mechanism=mech, judge_seconds=judge_s,
                                                              host_model=host, **common))
                    st["judge_charged"] = True
                if (st["host_calls_this_turn"] == 0 and st["prev_tier"] == "cheap"
                        and (usage.get("cache_write_tokens") or 0) > 0):
                    receipts.append(efficiency.host_rebuild_after_cheap(
                        usage=usage, seconds=seconds, host_model=host,
                        mechanism=st["prev_mech"] or mech, **common))
                elif (sst["cheap_since_host"] and sst["host_last_before_cheap"] is not None
                        and now - sst["host_last_before_cheap"] >= step_actions.settings(
                            service.policy.step_actions)["cache_ttl_s"]
                        and (usage.get("cache_write_tokens") or 0) > 0):
                    # Per-step cheap calls let the host's cache expire: the
                    # rewrite is a loss the switch caused.
                    receipts.append(efficiency.host_rebuild_after_cheap(
                        usage=usage, seconds=seconds, host_model=host, mechanism="rule:routine_readonly",
                        **common))
                sst["cheap_since_host"] = 0
                sst["host_last"] = now
                st["host_calls_this_turn"] += 1
                st["host_last"] = now
                cost = usage.get("cost_usd")
                if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                    st["host_sum_usd"] += float(cost)
                    st["host_sum_s"] += seconds
                    st["host_n"] += 1
                _push(sst["host_s"], seconds)
                if step_fields(response).get("tool_calls") and isinstance(usage.get("output_tokens"), int):
                    _push(sst["tool_step_out"], usage["output_tokens"])
            # Per-model prompt-cache state for the per-step price math.
            served = routed_model if isinstance(routed_model, str) else host
            prompt = (usage.get("input_tokens") or 0) + (usage.get("cache_write_tokens") or 0)
            if served and prompt:
                sst["cache"][served] = {"prefix": prompt, "t": now}
                sst["last_prompt"] = prompt + (usage.get("output_tokens") or 0)
            if self._pending_prepared:
                receipts.extend(self._prepared_receipts(service, turn, response, usage, seconds, host, common))
            for data in receipts:
                await service.emit("efficiency", data, decision_id)
        except Exception:  # noqa: BLE001 -- receipts must never break the loop
            return

    def _prepared_receipts(self, service: Any, turn: Any, response: Any, usage: dict, seconds: float,
                           host: str | None, common: dict) -> list[dict]:
        """Receipts for the prepared actions submitted since the last model
        call, now that this call shows what the skipped call would have cost."""
        pending, self._pending_prepared = self._pending_prepared, []
        cfg = step_actions.settings(service.policy.step_actions)
        sst = self._step_state(service)
        outs = sorted(sst["tool_step_out"])
        o_skip = outs[len(outs) // 2] if len(outs) >= 3 else cfg["skipped_output_tokens"]
        prompt = (usage.get("input_tokens") or 0) + (usage.get("cache_write_tokens") or 0)
        out_now = usage.get("output_tokens") or 0
        tps = efficiency.throughput(host) or 88.0
        skipped_s = max(0.5, seconds - max(0, out_now - o_skip) / tps)
        calls = step_actions.calls_of(response)
        workspace = self._tools.get("fast_workspace")
        decisions = getattr(turn, "tool_decisions", {}) or {}
        sizes = [int(decisions.get(p["tool_call_id"], {}).get("result_chars") or 0) // 4 for p in pending]
        receipts = []
        for i, p in enumerate(pending):
            after = sum(sizes[i:])
            repeated = step_actions.repeats_prepared(p["arguments"], calls, workspace)
            receipts.append(efficiency.prepared_step(
                tool=p["tool"], decision_seconds=p["decision_s"], host_model=host,
                skipped_prompt_tokens=max(0, prompt - after), skipped_output_tokens=o_skip,
                skipped_seconds=round(skipped_s, 4), mechanism=p["mechanism"], repeated=repeated,
                prepared_tokens=sizes[i], **common))
        return receipts

    # --- per-step decision point (step_actions.py) ---------------------------
    def _step_classify(self, service: Any, request: Any) -> dict | None:
        """Classify the upcoming step and, at a predictable read/status step,
        build the prepared candidates and decide whether asking the judge is
        worth its latency. Deterministic and cheap; never raises."""
        try:
            cfg = step_actions.settings(service.policy.step_actions)
            view = step_actions.analyze(request)
            kind, reason = step_actions.classify(view, cfg)
            step = {"cfg": cfg, "view": view, "kind": kind, "reason": reason, "candidates": [],
                    "offer": False, "expected_s": None, "judge_asked": False}
            if (cfg["prepared"] and service.policy.mode == "active"
                    and kind in (step_actions.TURN_START, step_actions.ROUTINE)):
                candidates = step_actions.candidates_for(view, kind, self._tools.get("fast_workspace"))
                if candidates:
                    sst = self._step_state(service)
                    expected = step_actions.expected_prepared_saving_s(
                        asked=sst["asked"], accepted=sst["accepted"], prior_accept=cfg["prior_accept"],
                        host_call_s=_mean(sst["host_s"], cfg["host_call_seconds_prior"]),
                        judge_s=_mean(sst["judge_s"], cfg["judge_seconds_prior"]))
                    step.update(candidates=candidates, expected_s=round(expected, 4), offer=expected > 0)
            return step
        except Exception:  # noqa: BLE001 -- the step layer is optional
            return None

    async def _step_after_choose(self, service: Any, step: dict, candidate: Any) -> None:
        """Judge statistics for the expected-saving gate, and a receipt for a
        judge call that did not produce a prepared action (pure overhead)."""
        ms = getattr(service, "last_backend_ms", None)
        if ms is None:
            return
        step["judge_asked"] = True
        sst = self._step_state(service)
        sst["asked"] += 1
        _push(sst["judge_s"], ms / 1000)
        if candidate:
            sst["accepted"] += 1
            return
        try:
            st = self._eff_context(service)
            host = getattr(self._provider, "default_model", None)
            await service.emit("efficiency", efficiency.judge_only(
                lever="prepared_action", mechanism=f"{service.backend.name}:next_action",
                decision="judge_declined_prepared", judge_seconds=ms / 1000,
                host_model=host if isinstance(host, str) else None,
                project=st["project"], traffic=st["traffic"]), service.last_decision_id)
        except Exception:  # noqa: BLE001
            return

    async def _emit_step(self, service: Any, step: dict, action: str, mechanism: str,
                         extra: dict | None = None) -> None:
        extra = extra or {}
        view = step["view"]
        data = {
            "step_class": step["kind"], "step_reason": step["reason"], "step_action": action,
            "mechanism": mechanism, "step_index": view.step_index,
            "tools": [n for n, _ in view.calls][:16], "candidate_count": len(step["candidates"]),
            "expected_saving_s": step["expected_s"], "judge_asked": step["judge_asked"],
            "mode": service.policy.mode,
        }
        for key in ("candidate_origin", "prompt_tokens_est", "cheap_model", "host_saving_usd", "cheap_cost_usd"):
            if extra.get(key) is not None:
                data[key] = extra[key]
        if extra.get("reason"):
            data["reason_code"] = extra["reason"]
        await service.emit("step_decided", data, service.last_decision_id)

    async def _step_cheaper_model(self, service: Any, turn: Any, step: dict, request: Any, kwargs: dict,
                                  routed_model: Any) -> dict | None:
        """Action (b): route THIS step to a cheaper model when the price- and
        cache-aware math says it is cheaper. Applies the model to the request
        and returns the decision (``model`` None keeps the host). Never
        routes a step the turn router already moved, a pinned or user-picked
        model, or anything but a read-only continuation."""
        cfg = step["cfg"]
        if not cfg["cheaper_model"] or step["kind"] not in (step_actions.ROUTINE, step_actions.AMBIGUOUS):
            return None
        decision: dict[str, Any] = {"model": None}
        try:
            host = getattr(self._provider, "default_model", None)
            if isinstance(routed_model, str) or turn.start_tier == "cheap":
                decision["reason"] = "turn_already_routed"
                return decision
            if field_value(request, "model", None) or kwargs.get("model"):
                decision["reason"] = "model_pinned"
                return decision
            if self._user_selected_model(service):
                decision["reason"] = "user_model"
                return decision
            if not isinstance(host, str):
                decision["reason"] = "host_unknown"
                return decision
            sst = self._step_state(service)
            now = time.monotonic()
            base = sst["last_prompt"]
            if not base:
                decision["reason"] = "prompt_size_unknown"
                return decision
            prompt = int(base + step["view"].result_chars / 3.5)
            outs = sorted(sst["tool_step_out"])
            out = outs[len(outs) // 2] if len(outs) >= 3 else cfg["skipped_output_tokens"]
            prefixes = {m: c["prefix"] for m, c in sst["cache"].items() if now - c["t"] < cfg["cache_ttl_s"]}
            choice = step_actions.cheaper_step(
                host=host, cheap_models=list(cfg["cheap_models"]), prompt_tokens=prompt, cache_prefix=prefixes,
                output_tokens=out, windows=dict(cfg["context_windows"]), rates=DEFAULT_RATES,
                amortize_steps=cfg["amortize_steps"])
            decision.update(reason=choice["reason"], prompt_tokens_est=prompt,
                            host_saving_usd=choice["host_saving_usd"], cheap_cost_usd=choice["cheap_cost_usd"],
                            mechanism="rule:routine_readonly", judge_seconds=0.0)
            if not choice["model"]:
                return decision
            if step["kind"] == step_actions.AMBIGUOUS:
                if not cfg["judge_ambiguous"] or (choice["saving_usd"] or 0) < cfg["min_judge_saving_usd"]:
                    decision["reason"] = "ambiguous_not_judged"
                    return decision
                view = step["view"]
                state = {"task": view.prompt[:600], "last_tools": [n for n, _ in view.calls][:8],
                         "last_result_excerpt": "\n".join(view.results)[-1500:]}
                answer, probability, ms = await _ask_judge_choice(
                    service, question_name=step_actions.NEXT_STEP_QUESTION,
                    instructions=step_actions.NEXT_STEP_INSTRUCTIONS,
                    criteria=dict(step_actions.NEXT_STEP_CRITERIA), state=state)
                decision["judge_seconds"] = ms / 1000
                step["judge_asked"] = True
                decision["mechanism"] = f"{service.backend.name}:changes_code"
                if answer != "read_only" or (probability or 0) < 0.8:
                    decision["reason"] = "judge_kept_host"
                    st = self._eff_context(service)
                    await service.emit("efficiency", efficiency.judge_only(
                        lever="cheaper_model", mechanism=decision["mechanism"], decision="judge_kept_host",
                        judge_seconds=ms / 1000, host_model=host, project=st["project"],
                        traffic=st["traffic"]), service.last_decision_id)
                    return decision
            decision["model"] = decision["cheap_model"] = choice["model"]
            decision["original_label"] = field_value(request, "model", None) or "provider-default"
            decision["original_request_model"] = field_value(request, "model", None)
            if isinstance(request, dict):
                request["model"] = choice["model"]
            else:
                setattr(request, "model", choice["model"])
            kwargs["model"] = choice["model"]
            return decision
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- on any doubt, the host runs the step
            if decision.get("model"):
                self._restore_host_model(request, kwargs, decision)
            decision["model"] = None
            decision["reason"] = "step_router_error"
            return decision

    def _cheap_step_discard_reason(self, step: dict, response: Any) -> str | None:
        """Cheaper-model responses are read-only continuations only: an edit,
        a mutating or test command, or (unless configured) a final answer is
        discarded and the host runs the step."""
        calls = step_actions.calls_of(response)
        if not calls:
            return None if step["cfg"]["cheap_may_answer"] else "final_answer"
        return None if step_actions.response_readonly(calls) else "not_read_only"

    @staticmethod
    def _restore_host_model(request: Any, kwargs: dict, decision: dict) -> None:
        kwargs.pop("model", None)
        original = decision.get("original_request_model")
        if isinstance(request, dict):
            request["model"] = original
        else:
            try:
                setattr(request, "model", original)
            except Exception:  # noqa: BLE001
                pass

    async def _receipt_discarded_cheap(self, service: Any, response: Any, seconds: float, served: str,
                                       mechanism: str, reason: str, decision_id: Any) -> None:
        try:
            st = self._eff_context(service)
            host = getattr(self._provider, "default_model", None)
            await service.emit("efficiency", efficiency.discarded_cheap_step(
                usage=usage_fields(response), seconds=seconds, served_model=served,
                host_model=host if isinstance(host, str) else None, mechanism=mechanism, reason=reason,
                project=st["project"], traffic=st["traffic"]), decision_id)
        except Exception:  # noqa: BLE001
            return

    async def _receipt_prepared_action(self, service: Any, turn: Any, tool: Any, seconds: float) -> None:
        try:
            st = self._eff_context(service)
            n = st["host_n"]
            host = getattr(self._provider, "default_model", None)
            data = efficiency.prepared_action(
                tool=str(tool), decision_seconds=seconds, host_model=host if isinstance(host, str) else None,
                avg_host_call_usd=(st["host_sum_usd"] / n) if n else None,
                avg_host_call_seconds=(st["host_sum_s"] / n) if n else None,
                mechanism=f"{service.backend.name}:next_action", project=st["project"], traffic=st["traffic"])
            await service.emit("efficiency", data, service.last_decision_id)
        except Exception:  # noqa: BLE001
            return

    def _start_model_is_cheaper(self, start_model: Any) -> bool:
        """True unless the wrapped provider's default model is known to be no
        more expensive than ``start_model`` (list input and output prices).
        Unknown models keep the previous behavior (route)."""
        from .savings import DEFAULT_RATES, _rates_for

        host = getattr(self._provider, "default_model", None)
        if not isinstance(host, str) or not isinstance(start_model, str) or host == start_model:
            return host != start_model
        host_rates, start_rates = _rates_for(host, DEFAULT_RATES), _rates_for(start_model, DEFAULT_RATES)
        if not host_rates or not start_rates:
            return True
        return start_rates[0] < host_rates[0] and start_rates[1] < host_rates[1]
    def _served_fields(self, response: Any, request: Any, kwargs: dict) -> dict:
        """usage_fields plus a served_model on EVERY ok receipt: the model the
        response names (served_model_source "response"), else the model this
        call was actually sent with -- the kwargs/request override or the
        provider's default (source "requested"), so benchmarks can store the
        serving model per request even when a provider omits it."""
        fields = usage_fields(response)
        if "served_model" in fields:
            fields["served_model_source"] = "response"
            return fields
        sent = kwargs.get("model") or field_value(request, "model") or getattr(self._provider, "default_model", None)
        if isinstance(sent, str) and sent:
            fields["served_model"] = sent[:80]
            fields["served_model_source"] = "requested"
        return fields

    def _host_model_field(self) -> dict:
        """The wrapped provider's configured default model -- the model a
        request without a routed override runs on -- for the savings estimate."""
        name = getattr(self._provider, "default_model", None)
        return {"host_model": name[:80]} if isinstance(name, str) and name else {}

    async def _stream_proxy(self, request, **kwargs):
        """Verbatim proxy of the provider's stream transport.

        The fast path is structurally unavailable here: loop-streaming's
        streaming branch cannot dispatch tool calls (`_has_pending_tools`
        always returns False, see docs/UPSTREAM_CONTRACT.md). We therefore
        never ask the service and never synthesize a response on this
        transport -- fail closed (defer to the real provider) rather than
        fail open (drop a prepared action silently).
        """
        service = self._runtime.service
        await service.emit("routed", {"route": "slow", "destination": self._provider_key,
            "reason_code": "fast_path_unavailable_on_transport",
            "transport_measured": "provider-stream"})
        start = time.perf_counter()
        provider_call_id = "provider_" + uuid4().hex
        await service.emit("slow_start", {"provider": self._provider_key,
            "provider_call_id": provider_call_id,
            "route": "slow", "destination": self._provider_key, "status": "running",
            "transport_measured": "provider-stream"})
        status = "error"
        try:
            async for chunk in self._provider.stream(request, **kwargs):
                yield chunk
            status = "ok"
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            await service.emit("slow_end", {"provider": self._provider_key,
                "provider_call_id": provider_call_id, "status": status,
                "duration_ms": (time.perf_counter() - start) * 1000,
                "transport_measured": "provider-stream"})


class ObservedTool:
    """Measure actual execute(), not merely a tool:pre hook which may be denied."""
    def __init__(
        self, tool: Any, runtime: Runtime, tool_key: str, *, workspace: Any = None
    ):
        self._tool, self._runtime, self._tool_key = tool, runtime, tool_key
        # HC02a: the raw fast_workspace tool (never wrapped, never executed
        # from here) used only to normalize a native read_file's file_path
        # into the same (path, revision) identity space as candidates.
        self._workspace = workspace

    def __getattr__(self, name):
        return getattr(self._tool, name)

    def _completed_read_identity(self, input: dict[str, Any]) -> tuple[str, str] | None:
        """HC02a: ``(normalized path, revision)`` for a successful read/list
        this tool call just performed, or ``None`` when this tool/shape is
        not part of the ledger (or the path cannot be normalized).

        Covers ``fast_workspace`` read/list (via its own ``read_identity``)
        and the native ``read_file`` tool's ``file_path`` (resolved against
        the fast_workspace tool's configured root, if one is mounted).
        """
        if not isinstance(input, dict):
            return None
        if self._tool_key == "fast_workspace":
            operation = input.get("operation")
            path = input.get("path")
            identity_fn = getattr(self._tool, "read_identity", None)
        elif self._tool_key == "read_file":
            operation, path = "read", input.get("file_path")
            identity_fn = (
                getattr(self._workspace, "read_identity", None)
                if self._workspace
                else None
            )
        else:
            return None
        if (
            not callable(identity_fn)
            or not isinstance(path, str)
            or operation not in ("read", "list")
        ):
            return None
        try:
            result = identity_fn(path, operation)
        except Exception:
            return None
        if not isinstance(result, tuple) or len(result) != 2:
            return None
        key, revision = result
        return str(key), str(revision)

    async def execute(self, input: dict[str, Any], **kwargs):
        service = self._runtime.service
        turn = service.turn
        decision_id = service.last_decision_id
        tool_call_id = "observed_" + uuid4().hex[:20]
        if turn:
            fingerprint = digest(input)
            for call_id, info in turn.tool_decisions.items():
                if not info["claimed"] and info["tool"] == self._tool_key and info["arguments_hash"] == fingerprint:
                    info["claimed"] = True
                    tool_call_id = call_id
                    decision_id = info["decision_id"]
                    break
            turn.revision += 1
        fields = {"tool": self._tool_key, "tool_call_id": tool_call_id, "status": "running"}
        await service.emit("tool_start", fields, decision_id)
        # HC11 ("pre-tool risk classification in shadow mode", opt-in):
        # ask the batched destructive/touches_production/category
        # questions BEFORE the tool runs. Purely observational -- the
        # answer is recorded in a receipt and never consulted to block,
        # modify, or approve this call; native approvals remain the sole
        # authority. Inert (no attribute touched, no call made) unless
        # `Policy.tool_risk_shadow` is configured, matching every other
        # HC0x seam.
        if turn and service.policy.tool_risk_shadow:
            risk_start = time.perf_counter()
            risk_state = _tool_risk_state(self._tool_key, input)
            risk_answers = await _ask_tool_risk(service, state=risk_state)
            risk_duration_ms = (time.perf_counter() - risk_start) * 1000
            if risk_answers is not None:
                destructive_choice, destructive_probabilities = risk_answers["destructive"]
                production_choice, production_probabilities = risk_answers["touches_production"]
                category_choice, category_probabilities = risk_answers["category"]
                await service.emit("tool_risk", {
                    "tool": self._tool_key,
                    "destructive": destructive_choice,
                    "touches_production": production_choice,
                    "category": category_choice,
                    "probabilities": {
                        "destructive": destructive_probabilities,
                        "touches_production": production_probabilities,
                        "category": category_probabilities,
                    },
                    "latency_ms": risk_duration_ms,
                    "backend": service.backend.name,
                    "mode": service.policy.mode,
                }, decision_id)
        start = time.perf_counter()
        try:
            result = await self._tool.execute(input, **kwargs)
        except asyncio.CancelledError:
            await service.emit("tool_end", {**fields, "status": "cancelled", "success": False,
                "duration_ms": (time.perf_counter() - start) * 1000}, decision_id)
            raise
        except Exception as exc:
            await service.emit("tool_end", {**fields, "status": "error", "success": False,
                "exception_type": type(exc).__name__, "duration_ms": (time.perf_counter() - start) * 1000}, decision_id)
            raise
        else:
            success = field_value(result, "success", None)
            if turn and tool_call_id in turn.tool_decisions:
                # A prepared action's result size: the per-step receipt
                # subtracts it from the next call's prompt.
                turn.tool_decisions[tool_call_id]["result_chars"] = _result_chars(result)
            if (
                turn
                and success is not False
                and service.policy.suppress_completed_reads
            ):
                identity = self._completed_read_identity(input)
                if identity is not None:
                    turn.completed_reads[identity[0]] = identity[1]
            # HC04 ("opt-in model routing with escalation"): observe a
            # successful test-tool execution's own result text for a
            # failure signature. Only tracked once per turn (subsequent
            # detections are redundant) and only while model_routing is
            # configured -- inert otherwise, matching every other HC04 seam.
            if (
                turn
                and not turn.test_failure_seen
                and service.policy.model_routing
                and _is_test_tool(self._tool_key)
            ):
                if _test_failure_observed(_tool_result_text(result)):
                    turn.test_failure_seen = True
            # HC05 ("judge-driven escalation and phase classification"):
            # feed the judge's compact state -- tool names used so far this
            # turn, and the last tool result's own text -- but ONLY while a
            # judge mechanism is actually configured. Inert otherwise,
            # matching every other HC0x seam.
            if turn and _judge_context_needed(service.policy):
                turn.tool_names_used.add(self._tool_key)
                turn.last_tool_result_text = _tool_result_text(result)
            await service.emit("tool_end", {**fields, "status": "ok" if success is not False else "error",
                "success": success, "duration_ms": (time.perf_counter() - start) * 1000}, decision_id)
            return result
        finally:
            if turn:
                turn.revision += 1


def _import_upstream_loop(cache_root: "Path | None" = None):
    """Import the upstream StreamingOrchestrator, falling back to Amplifier's module cache.

    Amplifier activates modules by inserting their checkout on sys.path at activation time; when this
    hybrid orchestrator replaces loop-streaming, the upstream module is never activated and may not be
    an installed distribution (observed after `amplifier update` moved the cache: module validation
    failed with ImportError). Locate the newest ``amplifier-module-loop-streaming-*`` checkout under
    ``~/.amplifier/cache`` and import from there. Fails loud when nothing is found.
    """
    try:
        from amplifier_module_loop_streaming import StreamingOrchestrator  # type: ignore
        return StreamingOrchestrator
    except ImportError as exc:
        import glob as _glob
        import os as _os
        import sys as _sys
        root = cache_root or (Path.home() / ".amplifier" / "cache")
        candidates = [
            d for d in _glob.glob(str(root / "amplifier-module-loop-streaming-*"))
            if _os.path.exists(_os.path.join(d, "amplifier_module_loop_streaming", "__init__.py"))
        ]
        candidates.sort(key=lambda d: _os.path.getmtime(d), reverse=True)
        for d in candidates:
            if d not in _sys.path:
                _sys.path.insert(0, d)
            try:
                from amplifier_module_loop_streaming import StreamingOrchestrator  # type: ignore
                return StreamingOrchestrator
            except ImportError:
                _sys.path.remove(d)
        raise RuntimeError(
            "Upstream loop-streaming module is not importable and no checkout was found under "
            f"{root}: install the amplifier extra in the SAME environment as Amplifier"
        ) from exc


# loop-streaming's own top-level config keys (amplifier_module_loop_streaming
# StreamingOrchestrator.__init__ and execute paths), plus every ``goal_*`` key.
# When this bundle replaces a root's loop-streaming through composition, the
# kernel's deep merge leaves the root's loop-streaming settings (foundation
# ships ``extended_thinking: true``) at the TOP level of this module's config.
# They belong to the wrapped loop, so they are forwarded there; an explicit
# ``upstream:`` block always wins. Decision-policy keys are never forwarded.
UPSTREAM_CONFIG_KEYS = frozenset({
    "max_iterations", "extended_thinking", "reasoning_effort", "budget_warn_ratio",
    "stream_delay", "min_delay_between_calls_ms", "ephemeral_injection_mode",
    "reminder_placement", "use_streaming",
})


def upstream_config(config: dict[str, Any]) -> dict[str, Any]:
    inherited = {k: v for k, v in config.items()
                 if k in UPSTREAM_CONFIG_KEYS or k.startswith("goal_")}
    return {**inherited, **dict(config.get("upstream") or {})}


def _register_upstream_capabilities(coordinator: Any, upstream: Any) -> list[str]:
    """Register what loop-streaming's own mount() would have registered.

    This module constructs the upstream loop directly (it never calls the
    upstream ``mount``, which would mount a second orchestrator), so without
    this the app-facing ``session.steer`` and ``conversation.provider_pin``
    capabilities and the upstream observability event names would silently
    disappear whenever this orchestrator replaces loop-streaming. Best-effort:
    an upstream revision lacking one of them simply does not get it.
    """
    import sys as _sys
    registered: list[str] = []
    module = _sys.modules.get(type(upstream).__module__)
    register_capability = getattr(coordinator, "register_capability", None)
    if callable(register_capability):
        steer = getattr(upstream, "steer", None)
        if callable(steer):
            register_capability("session.steer", steer)
            registered.append("session.steer")
        pin_cls = getattr(module, "ConversationProviderPin", None)
        if pin_cls is not None:
            try:
                register_capability("conversation.provider_pin", pin_cls(upstream, coordinator))
                registered.append("conversation.provider_pin")
            except Exception:
                pass
    register_contributor = getattr(coordinator, "register_contributor", None)
    if callable(register_contributor):
        register_contributor("observability.events", "loop-streaming", lambda: [
            "execution:start", "execution:end", "orchestrator:steering_injected",
            "orchestrator:goal_progress", "orchestrator:budget_warning",
            "orchestrator:provider_budget", "orchestrator:provider_overflow_recovery",
        ])
    return registered


def _hook_continue():
    try:
        from amplifier_core.models import HookResult
        return HookResult(action="continue")
    except ImportError:
        from types import SimpleNamespace
        return SimpleNamespace(action="continue")


class HybridOrchestrator:
    def __init__(self, config: dict[str, Any], coordinator: Any, runtime: Runtime,
                 *, upstream: Any = None, response_factory=action_response):
        self.config = config
        self.coordinator = coordinator
        self.runtime = runtime
        self.response_factory = response_factory
        if upstream is None:
            StreamingOrchestrator = _import_upstream_loop()
            upstream = StreamingOrchestrator(upstream_config(config))
        self.upstream = upstream
        # Lifecycle bookkeeping for the execution:end backfill (see execute()).
        self._execution_started = False
        self._execution_ended = False

    async def _on_execution_start(self, event: str, data: dict):
        self._execution_started = True
        return _hook_continue()

    async def _on_execution_end(self, event: str, data: dict):
        self._execution_ended = True
        return _hook_continue()

    def register_lifecycle_hooks(self, hooks: Any) -> list[Any]:
        """Observe the wrapped loop's execution:start/end so execute() can
        backfill a missing execution:end. The orchestrator contract requires
        execution:end on EVERY exit path; loop-streaming skips it on early
        returns (deny, cancel) and on exceptions."""
        register = getattr(hooks, "register", None)
        if not callable(register):
            return []
        return [
            register("execution:start", self._on_execution_start, priority=0,
                     name="loop-fast-decisions:execution-start"),
            register("execution:end", self._on_execution_end, priority=0,
                     name="loop-fast-decisions:execution-end"),
        ]

    async def _backfill_execution_end(self, hooks: Any, response: Any, status: str) -> None:
        if not self._execution_started or self._execution_ended:
            return
        emit = getattr(hooks, "emit", None)
        if not callable(emit):
            return
        try:
            await emit("execution:end", {
                "response": response if isinstance(response, str) else "",
                "status": {"ok": "completed"}.get(status, status),
                "source": "loop-fast-decisions",
            })
        except Exception:
            pass

    async def execute(self, prompt, context, providers, tools, hooks, **kwargs) -> str:
        async with self.runtime.lock:
            service = self.runtime.service
            service.turn = TurnState(uuid4().hex)
            service.last_decision_id = None
            service.slow_total = 0
            service.emitter.hooks = hooks
            # No self-authored "transport" claim here: a turn may enter the
            # facade through complete() and/or stream() multiple times, each
            # measured independently at the point of entry (transport_measured
            # on routed/slow_start/slow_end). See docs/UPSTREAM_CONTRACT.md.
            await service.emit("turn_start", {"mode": service.policy.mode,
                "backend": service.backend.name, "engine": "upstream-loop-streaming",
                "policy_version": service.policy.version,
                "allow_external_state": service.policy.allow_external_state,
                "effort_routing_enabled": bool(service.policy.effort_routing),
                "model_routing_enabled": bool(service.policy.model_routing)})
            # Provider keys and defaults are unchanged. Upstream pins and selections apply.
            workspace_tool = tools.get("fast_workspace")
            wrapped_tools = {
                key: ObservedTool(tool, self.runtime, key, workspace=workspace_tool)
                for key, tool in tools.items()
            }
            wrapped_providers = {key: RoutedProvider(provider, self.runtime, tools,
                self.response_factory, key) for key, provider in providers.items()}
            kwargs.setdefault("coordinator", self.coordinator)
            started = time.perf_counter()
            status = "error"
            response = None
            self._execution_started = False
            self._execution_ended = False
            try:
                response = await self.upstream.execute(prompt, context, wrapped_providers, wrapped_tools, hooks, **kwargs)
                status = "ok"
                return response
            except asyncio.CancelledError:
                status = "cancelled"
                await service.emit("cancelled", {"reason_code": "turn_cancelled"})
                raise
            finally:
                await self._backfill_execution_end(hooks, response, status)
                await service.emit("turn_end", {"fast_total": service.turn.fast_total,
                    "status": status, "duration_ms": (time.perf_counter() - started) * 1000,
                    "slow_total": service.slow_total,
                    **(self.runtime.recorder.health if self.runtime.recorder else {})})
                service.turn = None
                service.last_decision_id = None

    async def cleanup(self):
        await self.runtime.close()


async def mount(coordinator, config: dict):
    # Validate envelope construction before entering any user turn.
    action_response(Candidate("compat_check", "Schema check", "fast_workspace", {"operation": "list", "path": "."}), "compat_check")
    # The orchestrator owns decision policy; win regardless of module mount order.
    runtime, _ = get_runtime(coordinator, config, owner=True)
    # HC00 ("freeze source"): the same receipt hooks-fast-decisions emits,
    # from the orchestrator side too -- best-effort, never fatal to mount.
    try:
        await runtime.service.emit(
            "source",
            provenance.source_event_data(
                mode=runtime.service.policy.mode, module="loop-fast-decisions"
            ),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    registrations: list[Any] = []
    observatory_tasks: list[asyncio.Task] = []
    try:
        orchestrator = HybridOrchestrator(config, coordinator, runtime)
        await coordinator.mount("orchestrator", orchestrator)
        _register_upstream_capabilities(coordinator, orchestrator.upstream)
        hooks = getattr(coordinator, "hooks", None)
        if hooks is not None:
            registrations.extend(orchestrator.register_lifecycle_hooks(hooks))
            # Orchestrator-primary: the dashboard launches from here, so a
            # root that composes only this orchestrator (no hook) still gets
            # it. Installed at most once per session (the hook defers).
            from .observer import install_auto_observatory
            registration = install_auto_observatory(
                coordinator, runtime, config, config.get("events_dir"), observatory_tasks)
            if registration is not None:
                registrations.append(registration)
    except Exception:
        await runtime.close()
        raise

    async def cleanup():
        for unregister in registrations:
            if callable(unregister):
                try:
                    unregister()
                except Exception:
                    pass
        for task in observatory_tasks:
            if not task.done():
                task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await orchestrator.cleanup()

    return cleanup
