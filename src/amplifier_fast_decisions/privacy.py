"""Best-effort text scrubbing plus strict metadata-only telemetry projection.

Scrubbing is NOT a DLP guarantee. External state transmission is separately opt-in.
"""

from __future__ import annotations

import math
import re
from typing import Any

_SECRET = re.compile(
    r"(?i)(api[_-]?key|authorization|password|passwd|secret|access[_-]?token|refresh[_-]?token)"
    r"([\s\"':=]+)([^\s,;\"'}]{4,})"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_KEYS = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|github_pat_[A-Za-z0-9_]{12,}|AKIA[A-Z0-9]{16})\b"
)
_PEM = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.DOTALL
)

# No prompt, arguments, outputs, arbitrary errors, or model thinking in event data.
SAFE_FIELDS = {
    "mode",
    "backend",
    "model",
    "provider",
    "provider_call_id",
    "destination",
    "route",
    "proposed_route",
    "reason_code",
    "policy_version",
    "state_hash",
    "candidate_count",
    "candidates",
    "choice",
    "probabilities",
    "probability_kind",
    "reported_confidence",
    "confidence_kind",
    "option_set_hash",
    "selected_probability",
    "margin",
    "duration_ms",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    # Provider-reported usage for the savings estimate: cached-token counts,
    # the provider's own cost figure, and the model that actually served.
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_usd",
    "served_model",
    "host_model",
    # Per-step outcome of a model call: tool-call count, tool names only,
    # finish reason, and a coarse step kind -- never arguments or results.
    "tool_calls",
    "tools",
    "finish_reason",
    "step_kind",
    # Efficiency receipts: lever, mechanism, decision, baseline/actual sides
    # (model, calls, cost, seconds), fixed deltas, estimation method, project
    # name and traffic class -- no prompts, arguments or results.
    "lever",
    "mechanism",
    "decision",
    "baseline",
    "actual",
    "method",
    "calls_saved",
    "usd_saved",
    "seconds_saved",
    "project",
    "traffic",
    # Per-step decision point (step_decided): the step's class and reason
    # (tool names only), the action taken, the judge's expected saving, and
    # the per-step price math -- never arguments, paths or results.
    "step_class",
    "step_reason",
    "step_action",
    "step_index",
    "expected_saving_s",
    "judge_asked",
    "candidate_origin",
    "prompt_tokens_est",
    "cheap_model",
    "host_saving_usd",
    "cheap_cost_usd",
    # Routing levers: where served_model came from ("response" | "requested"),
    # the chosen tier label and the user-facing profile name.
    "served_model_source",
    "tier",
    "profile",
    # Waste-guard receipts: the numeric inputs of the estimate (token and
    # poll counts, step cost) -- never commands, paths or outputs.
    "detail",
    "tool",
    "tool_call_id",
    "status",
    "success",
    "exception_type",
    "retry_attempt",
    "status_code",
    "fast_total",
    "slow_total",
    "shadow",
    "synthetic",
    "dropped_events",
    "recording_error",
    "queue_depth",
    "step",
    "engine",
    "transport",
    "transport_measured",
    "connect_ms",
    "reused_connection",
    "event_source",
    "native_event",
    "latency_kind",
    "selected_candidate",
    "message_count",
    "result_bytes",
    "hook_ms",
    "event_count",
    "phase",
    "session_label",
    # Operator-configured display name of the decision backend.
    "backend_label",
    "warmup",
    "state_revision",
    # Basename of the session's working directory (observer.workspace_name):
    # a single path component for the viewer's session list, never a path.
    "workspace_name",
    # Session context for the viewer: the launching surface's name, the git
    # repository folder name, <= 2 components below it, and the branch.
    "harness",
    "repo",
    "subdir",
    "branch",
    "candidate_revision",
    "upstream_complete_calls",
    "output_path_redacted",
    # Shadow (P3): the proposal/observation join and worker health, never
    # arguments/outputs/raw state.
    "agreement",
    "proposed_candidate",
    "actual_tool",
    "would_have_avoided_llm_turn",
    "state_source",
    "arguments_hash",
    "dropped_shadow_jobs",
    "pending",
    "proposed_model_role",
    "actual_model_role",
    "eligible_roles",
    # Channels/router (P4/P5): canonical ordering, question volume, reason
    # codes for rejected contributions -- never the contributions themselves.
    "candidate_order_hash",
    "question_count",
    # Bench measurement fields (PR D amendment): small, privacy-safe
    # scalars only -- an integer length, a fixed label, a boolean -- never
    # the state text itself. See docs/BENCH.md and contracts.classify_domain.
    "state_chars",
    "domain",
    "allow_external_state",
    # Auto-observatory (session-start viewer bootstrap): a fixed action label,
    # a fixed reason code, and the local loopback port -- never the
    # token-bearing URL, which is never placed in event data.
    "action",
    "reason",
    "port",
    # Source provenance (HC00 "freeze source"): hashes/counts/versions only,
    # never a filesystem path. See provenance.describe_source and
    # docs/PRIVACY.md.
    "source_kind",
    "source_git_sha",
    "source_tree_sha256",
    "source_py_files",
    "package_version",
    "python",
    "module",
    # Observation telemetry (HC01): small integer/bool scalars describing
    # what build_state actually included, never the observation text itself.
    "observation_count",
    "observations_available",
    "observations_dropped",
    "observations_clipped",
    "task_anchored",
    "truncation_reason",
    # HC02a: per-turn completed-read ledger suppression count -- an
    # integer, never a path or argument.
    "candidates_suppressed_already_read",
    # HC03 ("phase-specific effort routing", opt-in): the classified phase
    # label, the effort string actually requested (or null when unchanged),
    # the provider-default label, and a per-turn explore-request counter --
    # never raw messages, tool arguments or model output. reason_code and
    # provider_call_id (already listed above) are reused verbatim.
    "phase",
    "requested_effort",
    "default_effort",
    "explore_requests",
    "effort_routing_enabled",
    # HC04 ("opt-in model routing with escalation", opt-in): the model
    # actually requested (or null when left unchanged/host-pinned), the
    # per-turn escalation latch and its reason, the per-turn routed-request
    # counter, and whether the feature is configured at all -- never raw
    # messages, tool arguments or model output. phase, requested_effort,
    # reason_code, provider_call_id, mode (already listed above) are reused
    # verbatim.
    "requested_model",
    "escalated",
    "escalation_reason",
    "model_routed_requests",
    "model_routing_enabled",
    # HC05 ("judge-driven escalation and phase classification", opt-in):
    # the judge's raw choice label, its selected-alternative probability,
    # the decision actually made from it, and this turn's slow-request
    # counter at judgement time -- never the compact state text sent to
    # the judge. `backend`, `phase`, `duration_ms`, `mode`, `choice`
    # (already listed above) are reused verbatim.
    "probability",
    "decided",
    "slow_requests_seen",
    "agreed_with_rules",
    # HC08 ("one call per decision point", opt-in): the batched question
    # names and count for a `decided_batch` receipt -- never their
    # instructions/criteria text or the state sent to the backend.
    # `backend`, `duration_ms`, `mode` (already listed above) are reused
    # verbatim.
    "question_ids",
    "n_questions",
    # HC09 ("stake-scaled confidence gates", opt-in): the numeric
    # threshold applied to a judged decision and whether the judge's
    # answer cleared it -- never a raw probability distribution beyond
    # what HC05 already exposes via `probability`.
    "gate",
    "passed_gate",
    # HC10 ("decomposed escalation signals", opt-in): the five atomic
    # signal probabilities (keyed by signal name), the weighted score,
    # and the uncertain-band width -- never the compact state text sent
    # to the backend. `gate`, `decided`, `duration_ms`, `phase`,
    # `slow_requests_seen`, `mode`, `backend` (already listed above) are
    # reused verbatim.
    "signal_probabilities",
    "score",
    "band",
    # HC11 ("pre-tool risk classification in shadow mode", opt-in): the
    # classification labels and their probability distributions, never
    # tool arguments (redacted to keys only before reaching the judge,
    # and not carried in this receipt at all). `tool`, `probabilities`,
    # `backend`, `mode` (already listed above) are reused verbatim.
    "category",
    "destructive",
    "touches_production",
    "latency_ms",
    # HC12 ("easy-turn shaping", opt-in): the LENGTH of the guidance text
    # actually appended (never the guidance text itself) and the list of
    # tool NAMES actually hidden from one call's advertised tool list --
    # never tool arguments/outputs or message content. `provider_call_id`,
    # `mode` (already listed above) are reused verbatim.
    "guidance_chars",
    "hidden_tools",
    # Turn planner (opt-in): the objective used, the estimated prompt-token
    # context size, the per-option {model, warm, cold, cost, time} table,
    # and the chosen model id -- all small numeric/label scalars derived
    # from token counts already recorded elsewhere (never raw messages,
    # tool arguments or model output). `host_model`, `provider_call_id`,
    # `mode` (already listed above) are reused verbatim.
    "objective",
    "ctx",
    "options",
    # Turn planner lookahead (opt-in): the resolved continuation
    # probability used to price the risk of leaving the host cache
    # stale, and the session-kind label it came from -- both small,
    # already-derived scalars (see contracts.DEFAULT_PLANNER_CONTINUE_PROBABILITY),
    # never raw session content.
    "p_continue",
    "session_kind",
    # "value" objective (opt-in): the resolved USD/hour used to convert
    # TOTAL time into money -- a config-level policy scalar, never
    # session content. Each option's own "utility" rides along inside
    # "options" (already allowlisted above), not as a separate top-level
    # field.
    "value_of_time_usd_per_hour",
    # Per-delegation model routing (Policy.delegation_routing, opt-in): the
    # policy name, the delegated agent's NAME and model role, where the
    # anchor came from, the judge's four choice labels, what the policy
    # proposed and what was ACTUALLY pinned on the call, and the LENGTH of
    # the delegated instruction -- never the instruction itself, which is
    # sent to the judge and nowhere else (delegation.judge_state). `mode`,
    # `backend`, `probabilities`, `tool`, `lever`, `action`, `reason`,
    # `duration_ms`, `latency_ms` (already listed above) are reused verbatim;
    # this allowlist is the second, structural guarantee that no task text
    # can reach an event even if a future field carried it.
    "delegation_policy",
    "agent",
    "model_role",
    "role_source",
    "anchor_source",
    "anchor",
    "answers",
    "requirements",
    "instruction_chars",
    "proposed_preference",
    "actual_preference",
    "nudge",
    "move",
    "guard",
    "capability_conflict",
}


def scrub(text: str, limit: int = 4000) -> str:
    text = _PEM.sub("[REDACTED PRIVATE KEY]", str(text))
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _KEYS.sub("[REDACTED KEY]", text)
    text = _SECRET.sub(lambda m: m[1] + m[2] + "[REDACTED]", text)
    return text[:limit]


def safe_data(data: dict[str, Any]) -> dict[str, Any]:
    def clean(v: Any, depth: int = 0) -> Any:
        if depth > 4:
            return "[depth limit]"
        if isinstance(v, str):
            return scrub(v, 512)
        if isinstance(v, float) and not math.isfinite(v):
            return None
        if v is None or isinstance(v, (bool, int, float)):
            return v
        if isinstance(v, dict):
            return {
                scrub(str(k), 80): clean(x, depth + 1) for k, x in list(v.items())[:64]
            }
        if isinstance(v, (list, tuple)):
            return [clean(x, depth + 1) for x in v[:64]]
        return None

    return {k: clean(v) for k, v in data.items() if k in SAFE_FIELDS}
