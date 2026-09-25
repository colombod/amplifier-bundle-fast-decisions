"""Screen configurations for large-repo-v0 (Opus 5.5 host).

``overrides`` is deep-merged by the kernel onto the shipped
behaviors/fast-decisions.yaml orchestrator config (composed runs, exactly as
a user installs the bundle). ``plain`` runs upstream loop-streaming with
fast-decisions off. ``model_routing`` below is the effective routing the
config ends up with, for the offline Jev pre-screen.
"""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

_BEHAVIOR = Path(__file__).resolve().parents[3] / "behaviors" / "fast-decisions.yaml"
_SHIPPED = yaml.safe_load(_BEHAVIOR.read_text())["session"]["orchestrator"]["config"]

TIERS = [
    {"max_p_complex": 0.2, "model": "claude-haiku-4-5", "effort": "low", "label": "haiku"},
    {"max_p_complex": 0.5, "model": "claude-sonnet-5", "effort": "medium", "label": "sonnet"},
]

OVERRIDES: dict[str, dict | None] = {
    "plain": None,
    # Today's shipped default: scope gate -> host model, no judge call.
    "default": {},
    # L2 conservative: judge asked in large repos; cheap only when confidently
    # easy AND read-only (repository edits stay on the host).
    "L2-both": {"model_routing": {"large_repo": {"max_p_complex": 0.3, "non_editing_max_p_edit": 0.5,
                                                 "require": "both"}}},
    # L2 permissive: read-only OR confidently easy.
    "L2-either": {"model_routing": {"large_repo": {"max_p_complex": 0.3, "non_editing_max_p_edit": 0.5,
                                                   "require": "either"}}},
    # L1+L2 via the one user-facing knob: tiers (Haiku for very easy) + L2-either.
    "frugal": {"profile": "frugal"},
    # L3 on top of L2-both: medium-confidence complex turns keep the host at medium effort.
    "L2-both-L3": {"model_routing": {"large_repo": {"max_p_complex": 0.3, "non_editing_max_p_edit": 0.5,
                                                    "require": "both"},
                                     "strong_effort": {"max_p_complex": 0.8, "effort": "medium"}}},
}


def _merge(base, overlay):
    out = dict(base)
    for k, v in overlay.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def effective(name: str) -> dict | None:
    overrides = OVERRIDES[name]
    if overrides is None:
        return None
    from amplifier_fast_decisions.routing_levers import apply_profile
    return apply_profile(_merge(copy.deepcopy(_SHIPPED), overrides))


CONFIGS = {name: (effective(name) or {}) for name in OVERRIDES}
