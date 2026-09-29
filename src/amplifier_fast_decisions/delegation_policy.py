"""The delegation routing policy: judge answers + anchor -> a lever, or abstain.

The policy itself is DATA (``policies/delegation_v3.json``); this module is the
small interpreter for it. Shipped as JSON rather than YAML because this package
declares no runtime dependencies (see ``pyproject.toml``) and this file is read
on a delegation's critical path -- an optional import there would mean the
feature silently disappears in a default install.

Order of operations for one delegation:

1. **Tier** of the anchor model, by name pattern.
2. **Base move** from ``min_tier`` vs the anchor's tier, ignored below the
   policy's base probability threshold.
3. **Depth overlay**: depth x base move -> the effort lever, the move ladder,
   or an abstain.
4. **Candidate walk**: the first candidate that passes the guards, the context
   window fit check and the capability requirements wins. Nothing fits ->
   abstain.

Nothing here raises for ordinary "no decision" cases; they return a
:class:`Choice` with ``final=None`` and a reason. See docs/DELEGATION-ROUTING.md
for the per-rule provenance.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Shipped policies, by name: ``policies/delegation_<name>.json``.
POLICY_DIR = Path(__file__).resolve().parent / "policies"
#: The only shipped policy today -- the one the paid A/B evaluation ran.
DEFAULT_POLICY = "v3"

#: (lever, move) -> the nudge name recorded on the receipt.
_NUDGE = {
    ("effort", "down"): "effort_low",
    ("effort", "keep"): "effort_low",
    ("model", "down"): "model_down",
    ("model", "up"): "model_up",
    ("provider", "down"): "provider_down",
    ("provider", "up"): "provider_up",
}


@dataclass(frozen=True)
class Choice:
    """A policy outcome. ``final is None`` means abstain, and ``reason`` says why."""

    final: dict[str, Any] | None = None
    lever: str | None = None
    nudge: str | None = None
    move: str | None = None
    reason: str | None = None
    guard: str | None = None
    capability_conflict: dict[str, Any] | None = None
    skipped: list[dict[str, Any]] = field(default_factory=list)


class DelegationPolicy:
    """A JSON-defined delegation routing policy."""

    def __init__(self, name: str, data: dict[str, Any]):
        self.name = name
        self.data = data

    # --- loading ---------------------------------------------------------

    @classmethod
    def load(cls, spec: str = DEFAULT_POLICY) -> DelegationPolicy:
        """Load by shipped name (``v3``) or by path to a JSON policy file."""
        shipped = POLICY_DIR / f"delegation_{spec}.json"
        path = shipped if shipped.exists() else Path(spec).expanduser()
        if not path.exists():
            raise FileNotFoundError(
                f"delegation_routing: policy {spec!r} is neither a shipped policy "
                f"({POLICY_DIR}) nor an existing file"
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TypeError(f"delegation_routing: policy {path} is not a mapping")
        return cls(str(data.get("name") or spec), data)

    # --- helpers ---------------------------------------------------------

    def tier_of(self, model: str | None) -> str:
        """Tier of a resolved model name, first pattern match wins."""
        low = (model or "").lower()
        patterns = self.data.get("tier_patterns") or {}
        for tier in ("frontier", "small", "mid"):
            if any(str(p).lower() in low for p in patterns.get(tier) or []):
                return tier
        return str(self.data.get("default_tier") or "mid")

    def est_input_tokens(self, instruction_chars: int) -> int:
        cfg = self.data.get("context") or {}
        per_token = int(cfg.get("chars_per_token") or 4)
        floor = int(cfg.get("agent_context_floor_tokens") or 0)
        return floor + int(max(instruction_chars, 0)) // per_token

    def _fits(self, candidate: dict[str, Any], need: int) -> bool:
        window = candidate.get("context_window")
        if not window:
            return True  # unchanged model, or no window recorded: no claim to check
        cfg = self.data.get("context") or {}
        budget = int(int(window) * (1 - float(cfg.get("safety_margin") or 0.0))) - int(
            cfg.get("output_reserve_tokens") or 0
        )
        return need <= budget

    def _capability_conflict(
        self, model: str | None, requirements: dict[str, bool]
    ) -> dict[str, Any] | None:
        """A verified "this model cannot do X" for a required X. Unknown = allowed."""
        low = (model or "").lower()
        table = self.data.get("capabilities") or {}
        req_map = self.data.get("requirements") or {}
        facts: dict[str, Any] = {}
        for name, caps in table.items():
            if str(name).lower() in low and isinstance(caps, dict):
                facts.update(caps)
        for question, needed in requirements.items():
            if not needed:
                continue
            capability = req_map.get(question)
            if capability and facts.get(capability) is False:
                return {"model": model, "requirement": question, "capability": capability}
        return None

    def _base_move(self, min_tier: str, min_tier_p: float, anchor_tier: str) -> str:
        """``down`` / ``keep`` / ``up`` from the tier answer alone."""
        rank = self.data.get("tier_rank") or {}
        threshold = float(self.data.get("base_threshold") or 0.0)
        if min_tier not in rank or anchor_tier not in rank:
            return "keep"
        if min_tier_p < threshold or rank[min_tier] == rank[anchor_tier]:
            return "keep"
        return "down" if rank[min_tier] < rank[anchor_tier] else "up"

    # --- the decision ----------------------------------------------------

    def choose(
        self,
        anchor: list[dict[str, Any]],
        answers: Any,
        instruction_chars: int = 0,
    ) -> Choice:
        """Pick a final preference for this delegation, or abstain with a reason.

        ``answers`` is a :class:`~.delegation.JudgeAnswers` (anything carrying
        ``min_tier`` / ``min_tier_p`` / ``answer_depth`` /
        ``needs_computer_use`` / ``needs_vision``).
        """
        if not anchor:
            return Choice(reason="no anchor")
        head = anchor[0]
        anchor_tier = self.tier_of(head.get("model"))

        depth_rules = (self.data.get("depth") or {}).get(answers.answer_depth)
        if not isinstance(depth_rules, dict):
            return Choice(reason=f"depth {answers.answer_depth}: no rule in policy")
        if depth_rules.get("action") == "abstain":
            return Choice(
                reason=depth_rules.get("reason") or f"depth {answers.answer_depth}: no move"
            )

        move = self._base_move(answers.min_tier, answers.min_tier_p, anchor_tier)
        rule = depth_rules.get(move)
        if not isinstance(rule, dict) or rule.get("action") == "abstain":
            reason = (
                rule.get("reason")
                if isinstance(rule, dict)
                else f"depth {answers.answer_depth} + {move}: no move"
            )
            return Choice(move=move, reason=reason)

        min_p = rule.get("min_p")
        if min_p is not None and answers.min_tier_p < float(min_p):
            return Choice(
                move=move,
                reason=(
                    f"{move} at depth {answers.answer_depth} needs p >= {min_p}, "
                    f"got {answers.min_tier_p:.2f}"
                ),
            )

        if rule.get("target") == "effort":
            candidates = [dict(self.data.get("effort_target") or {})]
        else:
            candidates = [
                dict(c)
                for c in ((self.data.get("moves") or {}).get(move) or {}).get(anchor_tier)
                or []
            ]
        if not candidates:
            return Choice(move=move, reason=f"no {move} candidates for tier {anchor_tier}")

        requirements = {
            "needs_computer_use": bool(answers.needs_computer_use),
            "needs_vision": bool(answers.needs_vision),
        }
        guards = self.data.get("guards") or {}
        no_effort_tiers = {str(t) for t in guards.get("never_lower_effort_on_tiers") or []}
        need = self.est_input_tokens(instruction_chars)

        skipped: list[dict[str, Any]] = []
        conflict: dict[str, Any] | None = None
        guard_hit: str | None = None

        for candidate in candidates:
            lever = str(candidate.get("lever") or "model")
            # The effort lever never changes the model: it inherits the anchor's.
            provider = candidate.get("provider") or head.get("provider")
            model = candidate.get("model") or head.get("model")
            if lever == "effort":
                provider, model = head.get("provider"), head.get("model")

            if lever == "effort" and anchor_tier in no_effort_tiers:
                guard_hit = "never_lower_effort_on_tiers"
                skipped.append({"model": model, "why": guard_hit})
                continue
            if not self._fits(candidate, need):
                skipped.append({"model": model, "why": f"window < {need} tokens"})
                continue
            found = self._capability_conflict(model, requirements)
            if found:
                conflict = found
                skipped.append({"model": model, "why": f"lacks {found['capability']}"})
                continue

            config = dict(head.get("config") or {}) if lever == "effort" else {}
            config.update(dict(candidate.get("config") or {}))
            return Choice(
                final={"provider": provider, "model": model, "config": config},
                lever=lever,
                nudge=_NUDGE.get((lever, move), f"{lever}_{move}"),
                move=move,
                guard=guard_hit,
                capability_conflict=conflict,
                skipped=skipped,
            )

        reason = "no candidate survived the guards"
        if conflict:
            reason = f"capability conflict: {conflict['model']} lacks {conflict['capability']}"
        elif guard_hit:
            reason = f"guard {guard_hit} on tier {anchor_tier}"
        return Choice(
            move=move,
            reason=reason,
            guard=guard_hit,
            capability_conflict=conflict,
            skipped=skipped,
        )
