"""Routing levers for the turn-start difficulty router (all opt-in).

Absent config keys leave the shipped two-tier router byte-for-byte unchanged.
Everything here is pure (no I/O, no Amplifier imports) so it is unit-testable
and shared by the orchestrator (decide_start_tier) and Policy validation.

Levers (``model_routing`` keys):

``tiers``
    Ordered cheap tiers by judged p(complex)::

        tiers:
          - {max_p_complex: 0.2, model: claude-haiku-4-5, effort: low, label: haiku}
          - {max_p_complex: 0.5, model: claude-sonnet-5, effort: medium, label: sonnet}

    A judged turn takes the first tier whose ``max_p_complex`` its p(complex)
    is strictly below; otherwise it stays on the host model. When the judge
    is unavailable the prompt-length rule decides cheap/strong as before and
    a cheap turn uses ``start_model`` (never a lower tier on a guess).

``large_repo``
    The scope gate (``cheap_max_workspace_files``) normally puts every turn in
    a large workspace on the host model without asking the judge. With
    ``large_repo`` set, the judge is asked anyway (one batched call) and the
    turn may still take a cheap tier when::

        max_p_complex:           p(complex) < this        (confident-easy)
        non_editing_max_p_edit:  p(edits code) < this     (question/status turn)
        require: both | either   (default both: only configured conditions count)

    Any judge failure keeps the host model (the gate's safe default).

``strong_effort``
    ``{max_p_complex, effort}``: a turn judged complex whose p(complex) is
    below ``max_p_complex`` (medium confidence) stays on the host model but at
    this reasoning effort. Measured risk: lower host effort lost 2 SWE-bench
    fixes -- keep it off unless a screen says otherwise.

``profile`` (top-level loop config key; ``fast_decisions.profile`` in docs)
    One user-facing knob that rewrites the keys above; see ``PROFILES``.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from .contracts import ALLOWED_EFFORTS, Question

_LABEL_RE = re.compile(r"^[a-z0-9_-]{1,24}$")
_TIER_KEYS = frozenset({"max_p_complex", "model", "effort", "label"})
_LARGE_REPO_KEYS = frozenset({"max_p_complex", "non_editing_max_p_edit", "require"})
_STRONG_EFFORT_KEYS = frozenset({"max_p_complex", "effort"})

# The cheap/fast/right trade each profile makes (see PROFILES.md in
# evals/suites/large-repo-v0 for the screen behind these numbers).
#   careful:  right > cheap. Only confidently easy turns (p(complex) < 0.3)
#             leave the host model; large repos always stay on it.
#   balanced: today's shipped default -- two tiers at p(complex) 0.5, scope
#             gate keeps large repos on the host model.
#   frugal:   cheap > right. Three tiers (Haiku for very easy turns), and in
#             large repos question/status turns or confidently easy turns may
#             take a cheap tier. Repository edits the judge is unsure about
#             still run on the host model.
PROFILES: dict[str, dict[str, Any]] = {
    "careful": {
        "model_routing": {"complex_min_probability": 0.3},
        "_drop": ("tiers", "large_repo", "strong_effort"),
    },
    "balanced": {},
    "frugal": {
        "model_routing": {
            "tiers": [
                {"max_p_complex": 0.2, "model": "claude-haiku-4-5", "effort": "low", "label": "haiku"},
                {"max_p_complex": 0.5, "model": "claude-sonnet-5", "effort": "medium", "label": "sonnet"},
            ],
            "large_repo": {"max_p_complex": 0.3, "non_editing_max_p_edit": 0.5, "require": "either"},
        },
    },
}


def apply_profile(config: dict[str, Any]) -> dict[str, Any]:
    """Return ``config`` with ``profile`` expanded into ``model_routing``.

    The profile wins over the keys it names (it is the user's knob, layered on
    top of the behavior's defaults). A profile never turns routing on by
    itself: with no ``model_routing`` configured, it only records the name.
    Unknown profile names are left for Policy validation to reject."""
    name = config.get("profile")
    spec = PROFILES.get(name) if isinstance(name, str) else None
    if not spec:
        return config
    routing = config.get("model_routing")
    if not isinstance(routing, dict):
        return config
    merged = copy.deepcopy(routing)
    for key in spec.get("_drop", ()):
        merged.pop(key, None)
    merged.update(copy.deepcopy(spec.get("model_routing", {})))
    return {**config, "model_routing": merged}


def _probability(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1:
        raise ValueError(f"{name} must be a number in (0, 1]")
    return float(value)


def validate_levers(model_routing: dict[str, Any]) -> None:
    """Fail loud on malformed lever config (called from validate_model_routing)."""
    tiers = model_routing.get("tiers")
    if tiers is not None:
        if not isinstance(tiers, list) or not tiers:
            raise ValueError("model_routing.tiers must be a non-empty list")
        previous = 0.0
        for i, tier in enumerate(tiers):
            if not isinstance(tier, dict):
                raise ValueError(f"model_routing.tiers[{i}] must be a dict")
            unknown = set(tier) - _TIER_KEYS
            if unknown:
                raise ValueError(f"model_routing.tiers[{i}] has unknown keys: {sorted(unknown)}")
            threshold = _probability(tier.get("max_p_complex"), f"model_routing.tiers[{i}].max_p_complex")
            if threshold <= previous:
                raise ValueError("model_routing.tiers must be in strictly ascending max_p_complex order")
            previous = threshold
            model = tier.get("model")
            if not isinstance(model, str) or not model:
                raise ValueError(f"model_routing.tiers[{i}].model must be a non-empty string")
            effort = tier.get("effort")
            if effort is not None and effort not in ALLOWED_EFFORTS:
                raise ValueError(f"model_routing.tiers[{i}].effort must be one of {sorted(ALLOWED_EFFORTS)}")
            label = tier.get("label")
            if label is not None and (not isinstance(label, str) or not _LABEL_RE.match(label)):
                raise ValueError(f"model_routing.tiers[{i}].label must match {_LABEL_RE.pattern}")
    large = model_routing.get("large_repo")
    if large is not None:
        if not isinstance(large, dict):
            raise ValueError("model_routing.large_repo must be a dict")
        unknown = set(large) - _LARGE_REPO_KEYS
        if unknown:
            raise ValueError(f"model_routing.large_repo has unknown keys: {sorted(unknown)}")
        if large.get("max_p_complex") is None and large.get("non_editing_max_p_edit") is None:
            raise ValueError("model_routing.large_repo needs max_p_complex and/or non_editing_max_p_edit")
        for key in ("max_p_complex", "non_editing_max_p_edit"):
            if large.get(key) is not None:
                _probability(large[key], f"model_routing.large_repo.{key}")
        if large.get("require", "both") not in ("both", "either"):
            raise ValueError("model_routing.large_repo.require must be both or either")
    strong = model_routing.get("strong_effort")
    if strong is not None:
        if not isinstance(strong, dict):
            raise ValueError("model_routing.strong_effort must be a dict")
        unknown = set(strong) - _STRONG_EFFORT_KEYS
        if unknown:
            raise ValueError(f"model_routing.strong_effort has unknown keys: {sorted(unknown)}")
        _probability(strong.get("max_p_complex"), "model_routing.strong_effort.max_p_complex")
        if strong.get("effort") not in ALLOWED_EFFORTS:
            raise ValueError(f"model_routing.strong_effort.effort must be one of {sorted(ALLOWED_EFFORTS)}")


def choose_tier(model_routing: dict[str, Any], p_complex: float) -> tuple[str, str | None, str | None] | None:
    """``(label, model, effort)`` of the cheap tier a judged turn takes, or
    ``None`` for the host model. ``model``/``effort`` None = use
    ``start_model``/``start_effort``/``by_tier`` as shipped."""
    tiers = model_routing.get("tiers")
    if tiers:
        for tier in tiers:
            if p_complex < tier["max_p_complex"]:
                return tier.get("label") or tier["model"][:24], tier["model"], tier.get("effort")
        return None
    gate = model_routing.get("complex_min_probability", 0.5)
    return ("cheap", None, None) if p_complex < gate else None


def strong_effort(model_routing: dict[str, Any], p_complex: float | None) -> str | None:
    """The host-model effort for a medium-confidence complex turn, or None."""
    spec = model_routing.get("strong_effort")
    if not spec or p_complex is None:
        return None
    return spec["effort"] if p_complex < spec["max_p_complex"] else None


def large_repo_wants_edit_question(model_routing: dict[str, Any]) -> bool:
    large = model_routing.get("large_repo") or {}
    return large.get("non_editing_max_p_edit") is not None


def large_repo_allows_cheap(model_routing: dict[str, Any], p_complex: float | None,
                            p_edit: float | None) -> bool:
    """Whether a scope-gated turn may still take a cheap tier. A missing
    judge answer for a configured condition counts as "not satisfied"."""
    large = model_routing.get("large_repo")
    if not large:
        return False
    conditions: list[bool] = []
    if large.get("max_p_complex") is not None:
        conditions.append(p_complex is not None and p_complex < large["max_p_complex"])
    if large.get("non_editing_max_p_edit") is not None:
        conditions.append(p_edit is not None and p_edit < large["non_editing_max_p_edit"])
    if not conditions:
        return False
    return all(conditions) if large.get("require", "both") == "both" else any(conditions)


# Second typed question for large repositories, asked in the same batched Jev
# call as task_difficulty. Positive polarity: "edits" means the signal is
# present.
EDIT_QUESTION_NAME = "edits_code"
EDIT_INSTRUCTIONS = (
    "Decide whether fulfilling this request requires creating, modifying or deleting files "
    "in the repository (code, tests, configuration or documentation)."
)
EDIT_CRITERIA = {
    "edits": "The request asks for a change to files: fix, implement, add, refactor, rename, "
             "update, write or delete something in the repository.",
    "read_only": "The request only needs reading or running things: a question, explanation, "
                 "summary, search, review, status check or listing, with no file changes.",
}


def edit_question() -> Question:
    return Question(name=EDIT_QUESTION_NAME, type="choice", instructions=EDIT_INSTRUCTIONS,
                    criteria=dict(EDIT_CRITERIA))
