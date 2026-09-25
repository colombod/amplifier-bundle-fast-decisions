"""Trusted candidate collection. Never extract executable actions from model text.

A contribution is extensibility, never authority (unchanged). One
misbehaving contributor (raises, returns garbage, or exceeds bounds) is
dropped and counted; it can never disable the fast path for the others
(P5, docs/design/redesign-2026-09-17.md).
"""

from __future__ import annotations

import inspect
import json
import re
from typing import Any

from .contracts import (
    CANDIDATES_CAPABILITY,
    CANDIDATES_CHANNEL,
    Candidate,
    candidate_order_key,
    canonical,
    field_value,
)
from .state import message_text


def capability(coordinator: Any, name: str) -> Any:
    if coordinator is None:
        return None
    return coordinator.get_capability(name)


async def maybe_await(value):
    return await value if inspect.isawaitable(value) else value


def parse_candidate(value: Any) -> Candidate:
    if isinstance(value, Candidate):
        # Deep-copy the mutable args at the trusted boundary.
        return Candidate(
            **{**value.__dict__, "arguments": json.loads(canonical(value.arguments))}
        )
    if not isinstance(value, dict):
        raise ValueError("Candidate must be a Candidate or dictionary")
    return Candidate(
        **{**value, "arguments": json.loads(canonical(value.get("arguments", {})))}
    )


async def _collect_groups(
    coordinator: Any, request: Any, tools: dict[str, Any], configured: list[dict] | None,
    extra: list[Any] | None = None, workspace_paths: bool = True,
) -> list[list[Any] | None]:
    groups: list[list[Any] | None] = []
    if configured:
        groups.append(list(configured))
    if extra:
        # Per-step prepared actions (step_actions.py): built by the
        # orchestrator from the step's own deterministic features.
        groups.append(list(extra))

    supplier = capability(coordinator, CANDIDATES_CAPABILITY)
    if supplier is not None:
        result = await maybe_await(supplier(request))
        if result is not None:
            groups.append(list(result) if isinstance(result, list) else None)

    collect = getattr(coordinator, "collect_contributions", None)
    if collect:
        contributions = await maybe_await(collect(CANDIDATES_CHANNEL)) or []
        if isinstance(contributions, dict):
            contributions = list(contributions.values())
        for contribution in contributions:
            if isinstance(contribution, list):
                groups.append(contribution)
            elif isinstance(contribution, (Candidate, dict)):
                groups.append([contribution])
            else:
                groups.append(None)

    workspace = tools.get("fast_workspace")
    if workspace_paths and workspace and hasattr(workspace, "candidate_for_path"):
        messages = field_value(request, "messages", []) or []
        user = next(
            (m for m in reversed(messages) if field_value(m, "role") == "user"), None
        )
        # Only explicitly named files; no guessed paths, shell commands, or recursive scan.
        pattern = r"(?<![\w/])(?:\./)?[\w./-]+\.(?:md|txt|py|js|ts|tsx|jsx|json|yaml|yml|toml|rs|html|css)\b"
        paths = (
            list(dict.fromkeys(re.findall(pattern, message_text(user)))) if user else []
        )
        workspace_candidates = []
        for index, path in enumerate(paths[:12]):
            candidate = workspace.candidate_for_path(path, index)
            if candidate:
                workspace_candidates.append(candidate)
        if workspace_candidates:
            groups.append(workspace_candidates)
    return groups


async def collect_candidates(
    coordinator: Any,
    request: Any,
    tools: dict[str, Any],
    configured: list[dict] | None = None,
    max_candidates: int = 12,
    *,
    extra: list[Any] | None = None,
    workspace_paths: bool = True,
) -> tuple[list[Candidate], list[str]]:
    """Returns ``(candidates, reject_reasons)``.

    ``reject_reasons`` is a flat list of reason codes (one per malformed
    group, per internal conflict, or per cross-group conflict), plus at most
    one ``contribution_truncated`` if the bound was exceeded. Counted --
    never raised -- by the caller.
    """
    reasons: list[str] = []
    groups = await _collect_groups(coordinator, request, tools, configured, extra, workspace_paths)

    by_id: dict[str, Candidate] = {}
    for group in groups:
        if group is None:
            reasons.append("contribution_shape_invalid")
            continue
        try:
            parsed = [parse_candidate(x) for x in group]
        except Exception:
            reasons.append("contribution_shape_invalid")
            continue
        local: dict[str, Candidate] = {}
        conflict = False
        for c in parsed:
            if c.id in local and local[c.id].fingerprint != c.fingerprint:
                conflict = True
                break
            local[c.id] = c
        if conflict:
            reasons.append("contribution_conflict")
            continue
        if any(
            cid in by_id and by_id[cid].fingerprint != c.fingerprint
            for cid, c in local.items()
        ):
            reasons.append("contribution_conflict")
            continue
        by_id.update(local)

    ordered = sorted(by_id.values(), key=candidate_order_key)
    if len(ordered) > max_candidates:
        reasons.append("contribution_truncated")
        ordered = ordered[:max_candidates]
    return ordered, reasons
