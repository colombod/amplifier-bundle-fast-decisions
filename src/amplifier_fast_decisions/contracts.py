"""Small vendor-neutral contracts; no Amplifier or network imports."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

SERVICE_CAPABILITY = "fast_decisions.service"
RUNTIME_CAPABILITY = "fast_decisions.runtime"
CANDIDATES_CAPABILITY = "fast_decisions.candidates"
CANDIDATES_CHANNEL = "fast_decisions.candidates"
QUESTIONS_CHANNEL = "fast_decisions.questions"
VALIDATOR_CAPABILITY = "fast_decisions.validate_candidate"
SLOW = "reason"
NEXT_ACTION = "next_action"
QUESTION_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
EVENT_PREFIX = "fast_decisions:"
EVENT_NAMES = tuple(
    EVENT_PREFIX + n
    for n in (
        "turn_start",
        "turn_end",
        "requested",
        "scored",
        "routed",
        "fallback",
        "slow_start",
        "slow_end",
        "tool_start",
        "tool_end",
        "cancelled",
        "health",
        "shadow_proposed",
        "shadow_observed",
        "shadow_agreement",
        "role_proposed",
        "role_agreement",
        "observatory",
        "source",
        "effort_routed",
        "model_routed",
        "escalation_judged",
        "phase_judged",
        # HC08 ("one call per decision point", opt-in): summary receipt for
        # a batched ask_many() call. See orchestrator.py and docs/EVENTS.md.
        "decided_batch",
        # HC10 ("decomposed escalation signals", opt-in): the five atomic
        # signal probabilities, the weighted score, the gate, and the
        # action taken. See orchestrator.py and docs/EVENTS.md.
        "escalation_signals",
        # HC11 ("pre-tool risk classification in shadow mode", opt-in):
        # never authorizes or blocks anything -- observation only. See
        # orchestrator.py and docs/EVENTS.md.
        "tool_risk",
        # Turn-start difficulty router (model_routing.start_policy): which
        # tier the turn starts on, who decided, with what probability.
        "difficulty_judged",
        # Efficiency receipts (docs/GOAL.md): one per optimization decision,
        # with the baseline and the savings fixed at decision time.
        "efficiency",
        # Cache keep-alive refresh calls (usage and cost of each) and
        # loop-stop notes (pattern kind and tool name only). See levers.py.
        "cache_refresh",
        "loop_note",
        # Waste guards (guards.py): which guard acted on a tool call (block,
        # pointer, poll_wait) -- tool name and reason code only.
        "waste_guard",
    )
)

# HC03 ("phase-specific effort routing"): the effort strings a host provider
# accepts on a per-request override (`request.reasoning_effort`). Kept here,
# not in effort.py, so Policy validation (below) has no dependency on that
# module -- effort.py imports FROM contracts, never the reverse.
ALLOWED_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


def validate_effort_routing(effort_routing: Any) -> None:
    """Fail loud on a malformed ``effort_routing`` policy at mount time.

    ``None`` or an empty dict is the default-off shape and always valid --
    routing stays fully opt-in. Never silently ignores a bad value.
    """
    if not effort_routing:
        return
    if not isinstance(effort_routing, dict):
        raise ValueError("effort_routing must be a dict")
    for phase in ("orient", "explore", "implement"):
        effort = effort_routing.get(phase)
        if effort is not None and effort not in ALLOWED_EFFORTS:
            raise ValueError(
                f"effort_routing.{phase} must be one of {sorted(ALLOWED_EFFORTS)}"
            )
    unknown = set(effort_routing) - {
        "orient",
        "explore",
        "implement",
        "max_explore_requests",
        "escalate_after_provider_errors",
        # HC05 ("judge-driven phase classification", opt-in): ask the
        # configured DecisionBackend to classify the phase instead of the
        # deterministic classify_phase(). See orchestrator.py and
        # docs/ARCHITECTURE.md.
        "phase_judge",
        # Cache-aware effort (opt-in): within a turn, never apply an effort
        # below the highest already applied. On Anthropic, changing the
        # thinking budget invalidates the cached message prefix, so phase
        # flips (implement -> explore -> implement) each re-wrote the whole
        # conversation cache -- measured as ~3x cache-write tokens.
        "monotonic",
        # Effort by the turn-start difficulty tier (requires model_routing
        # with a start_policy): {"cheap": <effort|None>, "strong": <effort|None>}.
        # When the tier is known it REPLACES the phase effort for the whole
        # turn -- one effort per turn, so the provider's message cache is
        # never invalidated by an effort change. None = provider default;
        # "phase" = this tier uses the phase map (with ``monotonic`` if set).
        "by_tier",
    }
    if unknown:
        raise ValueError(f"effort_routing has unknown keys: {sorted(unknown)}")
    by_tier = effort_routing.get("by_tier")
    if by_tier is not None:
        if not isinstance(by_tier, dict) or set(by_tier) - {"cheap", "strong"}:
            raise ValueError("effort_routing.by_tier must map cheap/strong to an effort or null")
        for tier_effort in by_tier.values():
            if tier_effort is not None and tier_effort != "phase" and tier_effort not in ALLOWED_EFFORTS:
                raise ValueError(f"effort_routing.by_tier values must be one of {sorted(ALLOWED_EFFORTS)}, 'phase' or null")
    for key in ("max_explore_requests", "escalate_after_provider_errors"):
        value = effort_routing.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 1
        ):
            raise ValueError(f"effort_routing.{key} must be a positive integer")
    phase_judge = effort_routing.get("phase_judge")
    if phase_judge is not None and not isinstance(phase_judge, bool):
        raise ValueError("effort_routing.phase_judge must be a bool")
    monotonic = effort_routing.get("monotonic")
    if monotonic is not None and not isinstance(monotonic, bool):
        raise ValueError("effort_routing.monotonic must be a bool")


# HC04 ("opt-in model routing with escalation"): the effort strings a host
# provider accepts on Policy.model_routing["start_effort"]. Reuses
# ALLOWED_EFFORTS above -- same vocabulary as effort_routing.
MODEL_ROUTING_KEYS = frozenset(
    {
        "start_model",
        "start_effort",
        "max_requests_before_escalation",
        "escalate_on_test_failure",
        "escalate_on_provider_error",
        "override_explicit_model",
        # HC05 ("judge-driven escalation", opt-in): "rules" (default,
        # current behavior) asks nothing extra; "judge" asks the configured
        # DecisionBackend a single Choice question before every slow request
        # past the first, while not yet escalated. Deterministic triggers
        # above remain a floor and still escalate regardless of the judge's
        # answer. See orchestrator.py and docs/ARCHITECTURE.md.
        "escalation_judge",
        "escalate_min_probability",
        # HC10 ("decomposed escalation signals", opt-in): per-signal
        # weight override for escalation_judge: "decomposed". Missing
        # signals fall back to DEFAULT_ESCALATION_WEIGHTS. See
        # orchestrator.py and docs/ARCHITECTURE.md.
        "escalation_weights",
        # Orchestrator-primary safety: when set, start_model is applied only
        # to providers whose mount key or name contains this substring
        # (case-insensitive). Composed onto a root with several providers,
        # this keeps an Anthropic model id from ever being sent to, say, an
        # OpenAI or vLLM provider. None (default) applies to every provider.
        "provider_match",
        # Turn-start difficulty router: "cheap" (default -- every turn starts
        # on start_model, the pre-router behavior), "rules" (prompt length),
        # or "judge" (one typed simple/complex question to the configured
        # backend at the turn's first slow request, falling back to rules).
        # A turn judged complex starts -- and stays -- on the host model:
        # no start_model override and no mid-turn escalation (a mid-turn
        # model switch re-writes the whole prompt cache).
        "start_policy",
        "complex_min_probability",
        "complex_min_prompt_chars",
        # Scope gate: a workspace with more files than this is a repository-
        # scale task, where the cheap start model lost quality on SWE-bench
        # (escalation did not recover a misdirected start); such turns start
        # strong whatever the judge says. None = no gate.
        "cheap_max_workspace_files",
    }
)

ESCALATION_JUDGE_MODES = frozenset({"rules", "judge", "decomposed"})

# HC10 ("decomposed escalation signals", opt-in): five atomic yes/no
# signals asked in ONE batched call, combined in code via a weighted sum
# instead of trusting a single judged verdict. See orchestrator.py and
# docs/ARCHITECTURE.md.
DECOMPOSED_ESCALATION_SIGNALS = (
    "plan_derailed",
    "repeated_tool_errors",
    "tests_failing",
    "unfamiliar_code",
    "beyond_tier",
)
DEFAULT_ESCALATION_WEIGHTS: dict[str, float] = {
    "tests_failing": 0.30,
    "repeated_tool_errors": 0.25,
    "plan_derailed": 0.20,
    "beyond_tier": 0.15,
    "unfamiliar_code": 0.10,
}


def validate_model_routing(model_routing: Any) -> None:
    """Fail loud on a malformed ``model_routing`` policy at mount time.

    ``None`` is the only default-off shape -- routing stays fully opt-in.
    Unlike ``effort_routing``, an empty dict is NOT treated as off: if a
    dict is supplied at all, ``start_model`` is required, because a model
    pin with no starting model is meaningless. Never silently ignores a
    bad value.
    """
    if model_routing is None:
        return
    if not isinstance(model_routing, dict):
        raise ValueError("model_routing must be a dict")
    unknown = set(model_routing) - MODEL_ROUTING_KEYS
    if unknown:
        raise ValueError(f"model_routing has unknown keys: {sorted(unknown)}")
    start_model = model_routing.get("start_model")
    if not isinstance(start_model, str) or not start_model:
        raise ValueError("model_routing.start_model must be a non-empty string")
    start_policy = model_routing.get("start_policy")
    if start_policy is not None and start_policy not in ("cheap", "rules", "judge"):
        raise ValueError("model_routing.start_policy must be cheap, rules or judge")
    cmp_ = model_routing.get("complex_min_probability")
    if cmp_ is not None and (isinstance(cmp_, bool) or not isinstance(cmp_, (int, float)) or not 0 < cmp_ < 1):
        raise ValueError("model_routing.complex_min_probability must be in (0, 1)")
    cmc = model_routing.get("complex_min_prompt_chars")
    if cmc is not None and (isinstance(cmc, bool) or not isinstance(cmc, int) or cmc < 1):
        raise ValueError("model_routing.complex_min_prompt_chars must be a positive integer")
    cmwf = model_routing.get("cheap_max_workspace_files")
    if cmwf is not None and (isinstance(cmwf, bool) or not isinstance(cmwf, int) or cmwf < 1):
        raise ValueError("model_routing.cheap_max_workspace_files must be a positive integer")
    provider_match = model_routing.get("provider_match")
    if provider_match is not None and (not isinstance(provider_match, str) or not provider_match):
        raise ValueError("model_routing.provider_match must be a non-empty string")
    start_effort = model_routing.get("start_effort")
    if start_effort is not None and start_effort not in ALLOWED_EFFORTS:
        raise ValueError(
            f"model_routing.start_effort must be one of {sorted(ALLOWED_EFFORTS)}"
        )
    max_requests = model_routing.get("max_requests_before_escalation")
    if max_requests is not None and (
        isinstance(max_requests, bool)
        or not isinstance(max_requests, int)
        or max_requests < 1
    ):
        raise ValueError(
            "model_routing.max_requests_before_escalation must be a positive integer"
        )
    for key in (
        "escalate_on_test_failure",
        "escalate_on_provider_error",
        "override_explicit_model",
    ):
        value = model_routing.get(key)
        if value is not None and not isinstance(value, bool):
            raise ValueError(f"model_routing.{key} must be a bool")
    escalation_judge = model_routing.get("escalation_judge")
    if escalation_judge is not None and escalation_judge not in ESCALATION_JUDGE_MODES:
        raise ValueError(
            f"model_routing.escalation_judge must be one of {sorted(ESCALATION_JUDGE_MODES)}"
        )
    escalate_min_probability = model_routing.get("escalate_min_probability")
    if escalate_min_probability is not None and (
        isinstance(escalate_min_probability, bool)
        or not isinstance(escalate_min_probability, (int, float))
        or not 0 <= escalate_min_probability <= 1
    ):
        raise ValueError(
            "model_routing.escalate_min_probability must be a number between 0 and 1"
        )
    escalation_weights = model_routing.get("escalation_weights")
    if escalation_weights is not None:
        if not isinstance(escalation_weights, dict):
            raise ValueError("model_routing.escalation_weights must be a dict")
        unknown_signals = set(escalation_weights) - set(DECOMPOSED_ESCALATION_SIGNALS)
        if unknown_signals:
            raise ValueError(
                f"model_routing.escalation_weights has unknown keys: {sorted(unknown_signals)}"
            )
        for key, value in escalation_weights.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 <= value <= 1
            ):
                raise ValueError(
                    f"model_routing.escalation_weights.{key} must be a number in [0, 1]"
                )


# HC09 ("stake-scaled confidence gates", opt-in): the three judged decision
# points a confidence gate can be configured for. "read_shortcut" is the
# fast-path action choice (DecisionService.choose); "phase" and
# "escalation" are HC05's two judge mechanisms. See effective_gate() below
# and docs/ARCHITECTURE.md.
CONFIDENCE_GATE_KINDS = frozenset({"read_shortcut", "phase", "escalation"})


def validate_confidence_gates(confidence_gates: Any) -> None:
    """Fail loud on a malformed ``confidence_gates`` policy at mount time.

    ``None`` is the default-off shape: every kind falls back to its
    pre-HC09 legacy source (see ``effective_gate``). Never silently
    ignores a bad value.
    """
    if confidence_gates is None:
        return
    if not isinstance(confidence_gates, dict):
        raise ValueError("confidence_gates must be a dict")
    unknown = set(confidence_gates) - CONFIDENCE_GATE_KINDS
    if unknown:
        raise ValueError(f"confidence_gates has unknown keys: {sorted(unknown)}")
    for key, value in confidence_gates.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1:
            raise ValueError(f"confidence_gates.{key} must be a number in (0, 1]")


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def field_value(value: Any, name: str, fallback: Any = None) -> Any:
    return (
        value.get(name, fallback)
        if isinstance(value, dict)
        else getattr(value, name, fallback)
    )


def indexed(container: Any, name: str, fallback: Any = None) -> Any:
    """Subscript access (``container[name]``), falling back to attribute access.

    The verified Jev SDK shape is ``response.answers["next_action"]`` --
    subscriptable, not necessarily a plain dict. Falls back to attribute
    access so a dict-like *or* an attribute-bearing object both work.
    """
    if container is None:
        return fallback
    try:
        return container[name]
    except (TypeError, KeyError, IndexError):
        return getattr(container, name, fallback)


def jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # Do not stringify arbitrary runtime objects (could expose secrets).
    return None


@dataclass(frozen=True)
class Candidate:
    """Prepared action, never a permission grant. Args are copied at the boundary."""

    id: str
    label: str
    tool: str
    arguments: dict[str, Any]
    rationale: str = "Prepared read-only action"
    origin: str = "trusted_config"
    revision: str = ""

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", self.id) or self.id == SLOW:
            raise ValueError("Invalid or reserved candidate id")
        if not self.tool or not isinstance(self.arguments, dict):
            raise ValueError("Candidate requires a tool and an argument object")
        canonical(self.arguments)
        if len(canonical(self.arguments)) > 16384:
            raise ValueError("Candidate arguments exceed the 16 KiB limit")

    @property
    def fingerprint(self) -> str:
        return digest(
            {
                "id": self.id,
                "tool": self.tool,
                "args": self.arguments,
                "revision": self.revision,
            }
        )


def candidate_order_key(candidate: Candidate) -> tuple[str, str]:
    """Canonical stable sort key for serialisation: ``(origin, id)``."""
    return (candidate.origin, candidate.id)


DOMAIN_TOOL_CHOICE = "tool-choice"
DOMAIN_READ_TARGET = "read-target"
DOMAIN_MODEL_ROLE = "model-role"
DOMAINS = (DOMAIN_TOOL_CHOICE, DOMAIN_READ_TARGET, DOMAIN_MODEL_ROLE)


def classify_domain(candidates: Sequence[Candidate], *, kind: str = "action") -> str:
    """One pure classifier, reused everywhere a domain label is recorded
    (service.py's main decision path, shadow.py's off-critical-path
    scoring, router.py's model-role proposals, and bench's offline
    replay/suite reporting) so there is exactly one place this decision is
    made, at the point the candidate set is built:

    - ``kind="role"`` (a model-role router decision, which has no
      candidate set of its own) always classifies as ``"model-role"``.
    - Every candidate targeting ``fast_workspace`` (and at least one
      candidate present) classifies as ``"read-target"``.
    - Anything else -- mixed tools, non-workspace tools, or an empty
      candidate set -- classifies as ``"tool-choice"``.
    """
    if kind == "role":
        return DOMAIN_MODEL_ROLE
    if candidates and all(c.tool == "fast_workspace" for c in candidates):
        return DOMAIN_READ_TARGET
    return DOMAIN_TOOL_CHOICE


def compute_candidate_order_hash(
    candidates: list[Candidate] | tuple[Candidate, ...],
) -> str:
    """Deterministic hash of the canonical ``(origin, id)`` ordering.

    Computed once per request. Two runs over the same candidate set
    (regardless of collection/iteration order) produce the same hash.
    """
    ordered = sorted(candidates, key=candidate_order_key)
    return digest([[c.origin, c.id] for c in ordered])


QuestionType = Literal["choice", "score", "noul"]


class InvalidCriteria(ValueError):
    """A choice question's criteria count is outside the vendor's 2..255 bound."""


@dataclass(frozen=True)
class Question:
    """A bounded judgment question, evaluated in the same backend request as
    the action choice. ``type`` uses the vendor's own primitive names
    (``choice`` / ``score`` / ``noul``) so no translation table is needed.
    """

    name: str
    type: QuestionType
    instructions: str
    criteria: dict[str, str] = field(default_factory=dict)
    origin: str = "contributed"

    def __post_init__(self) -> None:
        if not QUESTION_NAME_RE.fullmatch(self.name) or self.name == NEXT_ACTION:
            raise ValueError("Invalid or reserved question name")
        if self.type not in ("choice", "score", "noul"):
            raise ValueError("Invalid question type")
        if not self.instructions or len(self.instructions) > 512:
            raise ValueError("instructions must be 1..512 chars")
        if self.type == "choice":
            if not 2 <= len(self.criteria) <= 255:
                raise InvalidCriteria("choice question needs 2..255 criteria")
        elif self.criteria:
            raise ValueError("criteria only valid for choice questions")


@dataclass(frozen=True)
class DecisionRequest:
    """One batched request per state: the action choice plus every
    contributed question, scored independently by the backend in a single
    call. ``questions`` never includes ``next_action`` -- the backend
    assembles that from ``candidates`` itself."""

    state: dict[str, Any]
    candidates: tuple[Candidate, ...]
    questions: tuple[Question, ...] = ()
    candidate_order_hash: str = ""


@dataclass(frozen=True)
class Answer:
    """One question's result. ``probabilities`` for choice/score,
    ``noul`` for noul questions."""

    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float | None = None
    noul: float | None = None


@dataclass(frozen=True)
class Decision:
    choice: str
    probabilities: dict[str, float]
    reported_confidence: float | None = None
    model: str = "unknown"
    input_tokens: int | None = None
    synthetic: bool = False
    probability_kind: str = "backend_reported"
    confidence_kind: str = "unspecified"
    option_set_hash: str | None = None

    def validate(self, choices: set[str]) -> None:
        if self.choice not in choices or set(self.probabilities) != choices:
            raise ValueError("Decision alternatives do not match the request")
        vals = list(self.probabilities.values())
        if any(
            isinstance(v, bool)
            or not isinstance(v, (int, float))
            or not math.isfinite(v)
            or not 0 <= v <= 1
            for v in vals
        ):
            raise ValueError("Invalid probability")
        if not math.isclose(sum(vals), 1.0, abs_tol=0.025):
            raise ValueError("Probabilities do not sum to one")
        if self.probabilities[self.choice] + 1e-9 < max(vals):
            raise ValueError(
                "Selected choice is not the maximum-probability alternative"
            )
        if self.reported_confidence is not None and (
            isinstance(self.reported_confidence, bool)
            or not isinstance(self.reported_confidence, (int, float))
            or not math.isfinite(self.reported_confidence)
            or not 0 <= self.reported_confidence <= 1
        ):
            raise ValueError("Invalid reported confidence")


@dataclass(frozen=True)
class DecisionResult:
    """The full batched response: the ``next_action`` choice plus every
    contributed question's answer, keyed by name."""

    action: Decision
    answers: dict[str, Answer] = field(default_factory=dict)
    model: str = "unknown"
    input_tokens: int | None = None
    output_tokens: int | None = None
    synthetic: bool = False



# Cache keep-alive and loop-stop levers (levers.py): config keys, defaults and
# validation live here so Policy validation needs no import from levers.
KEEPALIVE_KEYS = frozenset({"enabled", "interval_s", "max_refreshes", "min_prefix_tokens", "max_output_tokens"})
LOOP_STOP_KEYS = frozenset({"enabled", "repeat_threshold", "failure_threshold", "sleep_threshold", "min_sleep_s",
                            "expected_further_calls"})
KEEPALIVE_DEFAULTS = {"enabled": True, "interval_s": 270.0, "max_refreshes": 6, "min_prefix_tokens": 20_000,
                      "max_output_tokens": 1}
# Expected further model calls continuing the pattern after a rule fires,
# WITHOUT a note -- the baseline of a loop_stop receipt: the mean continuation
# in the owner's real sessions of 2026-09-25 (50 session files, turn-scoped;
# exact repeat at the 3rd identical call: 7 episodes, mean 0.14; 3rd
# consecutive failure: 13 episodes, mean 0.77; 2nd sleep-as-timer call: 15
# episodes, mean 6.3), rounded to whole calls. Not the calls remaining in the
# turn: those (median 26.5) are mostly useful work, not loop.
EXPECTED_FURTHER_CALLS = {"repeat": 0, "failures": 1, "sleep_timer": 6}
EXPECTED_SOURCE = "mean pattern continuation, real sessions 2026-09-25"
LOOP_STOP_DEFAULTS = {"enabled": True, "repeat_threshold": 3, "failure_threshold": 3, "sleep_threshold": 2,
                      "min_sleep_s": 5.0, "expected_further_calls": EXPECTED_FURTHER_CALLS}


def _lever_num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def validate_cache_keepalive(cfg: Any) -> None:
    if cfg is None:
        return
    if not isinstance(cfg, dict):
        raise ValueError("cache_keepalive must be a dict")
    unknown = set(cfg) - KEEPALIVE_KEYS
    if unknown:
        raise ValueError(f"cache_keepalive has unknown keys: {sorted(unknown)}")
    if not isinstance(cfg.get("enabled", True), bool):
        raise ValueError("cache_keepalive.enabled must be a bool")
    interval = cfg.get("interval_s", 270.0)
    if _lever_num(interval) is None or not 0 < interval < 300.0:
        raise ValueError("cache_keepalive.interval_s must be in (0, 300)")
    for key in ("max_refreshes", "min_prefix_tokens", "max_output_tokens"):
        value = cfg.get(key, KEEPALIVE_DEFAULTS[key])
        if isinstance(value, bool) or not isinstance(value, int) or value < (1 if key != "min_prefix_tokens" else 0):
            raise ValueError(f"cache_keepalive.{key} must be a positive integer")


def validate_loop_stop(cfg: Any) -> None:
    if cfg is None:
        return
    if not isinstance(cfg, dict):
        raise ValueError("loop_stop must be a dict")
    unknown = set(cfg) - LOOP_STOP_KEYS
    if unknown:
        raise ValueError(f"loop_stop has unknown keys: {sorted(unknown)}")
    if not isinstance(cfg.get("enabled", True), bool):
        raise ValueError("loop_stop.enabled must be a bool")
    for key in ("repeat_threshold", "failure_threshold", "sleep_threshold"):
        value = cfg.get(key, LOOP_STOP_DEFAULTS[key])
        if isinstance(value, bool) or not isinstance(value, int) or value < 2:
            raise ValueError(f"loop_stop.{key} must be an integer >= 2")
    if _lever_num(cfg.get("min_sleep_s", 5.0)) is None:
        raise ValueError("loop_stop.min_sleep_s must be a number")
    expected = cfg.get("expected_further_calls")
    if expected is not None:
        if not isinstance(expected, dict) or set(expected) - set(EXPECTED_FURTHER_CALLS) or any(
                isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in expected.values()):
            raise ValueError("loop_stop.expected_further_calls maps repeat/failures/sleep_timer to integers >= 0")


@dataclass(frozen=True)
class Policy:
    mode: Literal["off", "shadow", "active"] = "shadow"
    timeout_ms: int = 750
    min_probability: float = 0.90
    min_margin: float = 0.20
    max_fast_streak: int = 3
    max_fast_per_turn: int = 12
    max_candidates: int = 12
    max_questions: int = 8
    max_state_chars: int = 12000
    allow_external_state: bool = False
    allow_synthetic_active: bool = False
    allowed_tools: tuple[str, ...] = ("fast_workspace",)
    # Hard bounds for the shadow snapshot, which runs on the hook's critical
    # path (only backend scoring is deferred to the shadow worker). Exceeding
    # either is a normal, counted outcome (reason_code shadow_snapshot_budget_exceeded),
    # never an exception into the hook chain. See docs/design/redesign-2026-09-17.md P3.
    shadow_max_messages: int = 12
    shadow_snapshot_budget_ms: int = 25
    # HC02a ("revision-aware completed-read suppression"): drop fast_workspace
    # read/list candidates whose (path, revision) the turn's completed-read
    # ledger already shows as read this turn. Default True so the candidate
    # profile exercises it; the baseline never runs the decision loop at all.
    suppress_completed_reads: bool = True
    # HC03 ("phase-specific effort routing", opt-in): None/empty means fully
    # off -- RoutedProvider never reads request.reasoning_effort and never
    # emits fast_decisions:effort_routed. See effort.py and docs/ARCHITECTURE.md.
    effort_routing: dict[str, Any] | None = None
    # HC04 ("opt-in model routing with escalation", opt-in): None means fully
    # off -- RoutedProvider never reads/writes request.model or
    # request.reasoning_effort for this feature and never emits
    # fast_decisions:model_routed. See orchestrator.py and docs/ARCHITECTURE.md.
    model_routing: dict[str, Any] | None = None
    # HC08 ("one call per decision point", opt-in): when True, the
    # orchestrator combines the HC05 phase-judge and escalation-judge asks
    # into ONE ask_many() backend call whenever both are due for the same
    # request, instead of two separate ask() calls. Default False -- the
    # sequential path (unchanged) is used identically to before HC08.
    # See orchestrator.py and docs/ARCHITECTURE.md.
    decision_batching: bool = False
    # HC09 ("stake-scaled confidence gates", opt-in): per-judged-decision
    # probability floor below which the judge's answer is NOT acted on
    # ("do not act": read_shortcut lets the model run, phase keeps the
    # deterministic classification, escalation stays on rules). None
    # (default) means every kind falls back to its pre-HC09 legacy source
    # -- see effective_gate(). A configured kind here always overrides its
    # legacy alias (min_probability / escalate_min_probability).
    confidence_gates: dict[str, float] | None = None
    # HC11 ("pre-tool risk classification in shadow mode", opt-in): when
    # True, ObservedTool.execute asks a batched destructive/
    # touches_production/category classification BEFORE each tool call
    # and records a `fast_decisions:tool_risk` receipt. Never blocks,
    # modifies or approves anything -- native approvals remain
    # authoritative. Default False -- inert, matching every other HC0x
    # seam. See orchestrator.py and docs/ARCHITECTURE.md.
    tool_risk_shadow: bool = False
    # The judged read shortcut (DecisionService.choose). False keeps a judge
    # backend available to the routers (e.g. start_policy: judge) without
    # putting a candidate-scoring call in front of slow requests.
    read_shortcut: bool = True
    # Cache keep-alive during long tool/helper waits (levers.py). None (the
    # default) is fully off; a dict turns it on with contracts.KEEPALIVE_DEFAULTS
    # for omitted keys ({"enabled": false} keeps it off).
    cache_keepalive: dict[str, Any] | None = None
    # Loop-stop nudges: repeated identical calls, consecutive failures and
    # sleep-as-timer polling get a short note in the next request (levers.py).
    # None (the default) is fully off.
    loop_stop: dict[str, Any] | None = None
    # Waste guards (guards.py): identical-result pointers, repeated-failure
    # stops and sleep-polls run in place. None/False: off (the code default,
    # so tests and old profiles are unchanged); True or a dict of
    # guards.GuardConfig fields: on. behaviors/fast-decisions.yaml turns
    # them on. Independent of mode, routing and the scope gate.
    waste_guards: Any = None
    version: str = "policy-v1"

    def __post_init__(self) -> None:
        if self.mode not in {"off", "shadow", "active"}:
            raise ValueError("mode must be off, shadow, or active")
        if not 10 <= self.timeout_ms <= 60000:
            raise ValueError("timeout_ms must be between 10 and 60000")
        if not (0 <= self.min_probability <= 1 and 0 <= self.min_margin <= 1):
            raise ValueError("Invalid selection thresholds")
        if not 1 <= self.max_candidates <= 63:
            raise ValueError("max_candidates must be between 1 and 63")
        if min(self.max_fast_streak, self.max_fast_per_turn, self.max_candidates) < 1:
            raise ValueError("Decision limits must be positive")
        if not 0 <= self.max_questions <= 64:
            raise ValueError("max_questions must be between 0 and 64")
        if not 512 <= self.max_state_chars <= 100000:
            raise ValueError("max_state_chars must be between 512 and 100000")
        if not 1 <= self.shadow_max_messages <= 200:
            raise ValueError("shadow_max_messages must be between 1 and 200")
        if not 1 <= self.shadow_snapshot_budget_ms <= 5000:
            raise ValueError("shadow_snapshot_budget_ms must be between 1 and 5000")
        validate_effort_routing(self.effort_routing)
        validate_model_routing(self.model_routing)
        if not isinstance(self.decision_batching, bool):
            raise ValueError("decision_batching must be a bool")
        validate_confidence_gates(self.confidence_gates)
        if not isinstance(self.tool_risk_shadow, bool):
            raise ValueError("tool_risk_shadow must be a bool")
        if not isinstance(self.read_shortcut, bool):
            raise ValueError("read_shortcut must be a bool")
        validate_cache_keepalive(self.cache_keepalive)
        validate_loop_stop(self.loop_stop)
        if self.waste_guards is not None and not isinstance(self.waste_guards, (bool, dict)):
            raise ValueError("waste_guards must be a bool or a mapping")

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> Policy:
        names = cls.__dataclass_fields__
        values = {k: v for k, v in config.items() if k in names}
        if "allowed_tools" in values:
            values["allowed_tools"] = tuple(values["allowed_tools"])
        if "allow_external_state" not in values:
            # Environment-level default, consulted only when a profile omits
            # the field (profile config always wins). See .env.example.
            env_value = os.getenv("FAST_DECISIONS_ALLOW_EXTERNAL_STATE")
            if env_value is not None:
                values["allow_external_state"] = env_value.strip().lower() in ("1", "true", "yes", "on")
        return cls(**values)


def effective_gate(policy: Policy, kind: str) -> float:
    """Stake-scaled confidence gate for one judged decision point (HC09).

    ``kind`` is one of ``"read_shortcut"``, ``"phase"``, ``"escalation"``.
    ``Policy.confidence_gates`` (default ``None``, fully opt-in) overrides
    the legacy per-mechanism default for exactly the kinds it names; any
    kind it omits -- or the policy omitting ``confidence_gates`` entirely
    -- falls back to the pre-HC09 behavior byte-for-byte: ``min_probability``
    for ``read_shortcut`` (DecisionService.choose's existing threshold),
    ``model_routing.escalate_min_probability`` (default 0.7) for
    ``escalation``, and ``0.0`` for ``phase`` -- phase classification never
    had a probability floor before HC09, so any non-abstain judge answer
    still applies by default.
    """
    if kind not in CONFIDENCE_GATE_KINDS:
        raise ValueError(f"Unknown confidence gate kind: {kind!r}")
    gates = policy.confidence_gates or {}
    if kind in gates:
        return gates[kind]
    if kind == "read_shortcut":
        return policy.min_probability
    if kind == "escalation":
        return (policy.model_routing or {}).get("escalate_min_probability", 0.7)
    return 0.0  # kind == "phase"


class DecisionBackend(Protocol):
    name: str
    external: bool

    async def ask(self, request: DecisionRequest) -> DecisionResult: ...
    # HC08 ("one call per decision point", opt-in): ask every question in
    # one DecisionRequest in a single logical call. A backend need not
    # implement this itself -- backends.ask_many() provides a default
    # (concurrent single-question asks, merged) for any backend lacking
    # one of its own. See backends.py.
    async def ask_many(self, request: DecisionRequest) -> DecisionResult: ...
    async def close(self) -> None: ...


@dataclass
class TurnState:
    id: str
    used: set[str] = field(default_factory=set)
    fast_streak: int = 0
    fast_total: int = 0
    decision_count: int = 0
    revision: int = 0
    tool_decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    # HC02a: per-turn completed-read ledger, normalized path -> revision.
    # Fed by both fast submissions (orchestrator.RoutedProvider.complete) and
    # successful native/provider-selected reads (orchestrator.ObservedTool.execute).
    # Reset with the rest of TurnState at turn start; never records a denied
    # or failed call.
    completed_reads: dict[str, str] = field(default_factory=dict)
    # HC03 ("phase-specific effort routing"): per-turn counters. Reset with
    # the rest of TurnState at turn start. explore_requests counts every
    # explore-phase slow request seen this turn (1-indexed as consulted by
    # decide_effort); effort_routed_requests counts only those where effort
    # was actually lowered; provider_errors_seen counts upstream provider
    # exceptions (never CancelledError) observed this turn.
    explore_requests: int = 0
    effort_routed_requests: int = 0
    provider_errors_seen: int = 0
    # HC04 ("opt-in model routing with escalation"): per-turn counters/flags.
    # Reset with the rest of TurnState at turn start. slow_requests_seen
    # counts every slow request seen while model_routing is enabled (not
    # gated on whether routing actually applied); model_routed_requests
    # counts only requests where start_model was actually set;
    # test_failure_seen is set by ObservedTool.execute observing a failing
    # test-tool result; escalated/escalation_reason latch permanently once
    # tripped (never reset mid-turn).
    slow_requests_seen: int = 0
    model_routed_requests: int = 0
    test_failure_seen: bool = False
    escalated: bool = False
    escalation_reason: str | None = None
    # HC05 ("judge-driven escalation and phase classification", opt-in):
    # per-turn judge context and counters. tool_names_used/last_tool_result_text
    # are fed by ObservedTool.execute, but ONLY while a judge mechanism is
    # actually configured (escalation_judge: "judge" or phase_judge: true) --
    # inert otherwise, matching every other HC0x seam. escalation_judgements
    # counts every time the escalation judge was actually asked (not gated on
    # its answer); escalations_by_judge counts only the judge-caused
    # escalations (a deterministic trigger firing first does not count here).
    tool_names_used: set[str] = field(default_factory=set)
    last_tool_result_text: str = ""
    escalation_judgements: int = 0
    escalations_by_judge: int = 0
    # HC08 ("one call per decision point", opt-in): counts every time a
    # batched ask_many() call failed (exception/timeout, never a policy
    # block or abstain) and this request fell back to the sequential
    # per-question path instead.
    batch_fallbacks: int = 0
    # effort_routing.monotonic: the highest effort applied so far this turn.
    max_effort_applied: str | None = None
    # Turn-start difficulty router: "cheap" | "strong", decided once at the
    # turn's first slow request (None until then / when routing is off).
    start_tier: str | None = None
    # Who decided the start tier (e.g. "jev:task_difficulty", "rule:scope_gate")
    # and how long the judge took, for efficiency receipts.
    start_mechanism: str | None = None
    judge_seconds: float = 0.0


def candidate_read_identity(
    candidate: Candidate, tools: dict[str, Any]
) -> tuple[str, str] | None:
    """``(normalized path, current revision)`` for a ``fast_workspace``
    read/list candidate, else ``None``.

    Recomputes the *live* revision through the tool's own ``read_identity``
    (mirrors ``_eligible``'s live recheck via ``validate_candidate``) so the
    completed-read ledger and candidate eligibility share one identity space
    with workspace.py's own revision function. Never raises: a tool without
    ``read_identity``, a malformed argument shape, or a resolution error all
    return ``None`` (candidate is simply not suppression-eligible).
    """
    if candidate.tool != "fast_workspace":
        return None
    args = candidate.arguments
    if set(args) != {"operation", "path"} or args.get("operation") not in (
        "read",
        "list",
    ):
        return None
    tool = tools.get("fast_workspace")
    identity_fn = getattr(tool, "read_identity", None)
    if not callable(identity_fn):
        return None
    try:
        result = identity_fn(args["path"], args["operation"])
    except Exception:
        return None
    if not isinstance(result, tuple) or len(result) != 2:
        return None
    path, rev = result
    return str(path), str(rev)
