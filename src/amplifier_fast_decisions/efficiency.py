"""Efficiency receipts: one auditable record per optimization decision.

Every decision that changes what a step costs (a prepared action instead of a
model call, a cheaper model for a step, a kept-warm cache, a blocked helper
launch, a loop stopped early, a right-sized context) emits a
``fast_decisions:efficiency`` event carrying:

* ``lever`` and ``mechanism`` -- what kind of efficiency, and which judge, rule
  or mechanism decided it (``jev:task_difficulty``, ``rule:prompt_length``, ...);
* ``decision`` -- what was decided;
* ``baseline`` / ``actual`` -- calls, cost and seconds for the same step on the
  default model versus what actually ran, both fixed at decision time;
* ``calls_saved``, ``usd_saved``, ``seconds_saved`` -- baseline minus actual
  (negative when the decision cost more), and ``method`` -- how the baseline
  was estimated;
* ``project`` and ``traffic`` (``production`` or ``test``).

Because the savings are fixed in the receipt, dashboard totals are plain sums
over stored records: :func:`aggregate` (used by the dashboard and ``afast
efficiency``) and any independent re-sum give the same numbers. Test,
development and benchmark traffic is marked at the source and excluded from
production totals by default.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .savings import DEFAULT_RATES, price

EVENT = "fast_decisions:efficiency"
LEVERS = (
    "prepared_action",
    "cache_keepalive",
    "cheaper_model",
    "launch_blocked",
    "loop_stop",
    "context_rightsize",
)
LEVER_LABELS = {
    "prepared_action": "Calls skipped by prepared actions",
    "cache_keepalive": "Cache kept warm",
    "cheaper_model": "Steps on a cheaper model",
    "launch_blocked": "Helper launches blocked",
    "loop_stop": "Loops stopped early",
    "context_rightsize": "Context right-sized",
}
# Output tokens per second, measured 2026-09-24 (docs/evidence/2026-09-24/
# followups/followups.json, 4 x 600-token answers per model). Used only to
# estimate how long the same output would have taken on another model; every
# receipt names this method.
DEFAULT_THROUGHPUT_TPS = {"claude-opus-5-5": 88.0, "claude-sonnet-5": 113.0, "claude-haiku-4-5": 96.0}
_TEST_ENV_VALUES = {"test", "tests", "dev", "development", "benchmark", "bench", "eval", "evals", "ci"}


def throughput(model: str | None, table: dict[str, float] | None = None) -> float | None:
    table = table or DEFAULT_THROUGHPUT_TPS
    if not model:
        return None
    if model in table:
        return table[model]
    for name in sorted(table, key=len, reverse=True):
        if model.startswith(name):
            return table[name]
    return None


def classify_traffic(working_dir: str | os.PathLike | None, *, session_label: str | None = None,
                     env: Any = None) -> str:
    """``production`` or ``test``. Explicit ``AFAST_TRAFFIC`` wins; otherwise
    sessions in temp dirs, eval/benchmark roots, agent worktrees or labeled
    as Forge benchmark runs are test traffic."""
    env = os.environ if env is None else env
    declared = str(env.get("AFAST_TRAFFIC", "")).strip().lower()
    if declared in _TEST_ENV_VALUES:
        return "test"
    if declared == "production":
        return "production"
    if isinstance(session_label, str) and session_label.strip().lower().startswith("forge"):
        return "test"
    if working_dir:
        path = str(Path(working_dir).expanduser())
        roots = {tempfile.gettempdir(), "/tmp", "/private/tmp", "/private/var/folders", "/var/folders"}
        if any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots if r):
            return "test"
        markers = ("/afast-", "/.claude/worktrees/", "/.amplifier/evaluation/", "/experiments/")
        if any(m in path for m in markers):
            return "test"
    return "production"


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _side(model: str | None, calls: int, cost_usd: float | None, seconds: float | None, **extra: Any) -> dict:
    side = {"model": model, "calls": int(calls),
            "cost_usd": None if cost_usd is None else round(cost_usd, 8),
            "seconds": None if seconds is None else round(seconds, 4)}
    side.update({k: v for k, v in extra.items() if v is not None})
    return side


def receipt(*, lever: str, mechanism: str, decision: str, baseline: dict, actual: dict, method: str,
            project: str | None, traffic: str) -> dict:
    """Build receipt data with the deltas fixed (baseline minus actual)."""
    if lever not in LEVERS:
        raise ValueError(f"unknown efficiency lever: {lever}")
    b_cost, a_cost = _num(baseline.get("cost_usd")), _num(actual.get("cost_usd"))
    b_s, a_s = _num(baseline.get("seconds")), _num(actual.get("seconds"))
    return {
        "lever": lever, "mechanism": mechanism[:80], "decision": decision[:80],
        "baseline": baseline, "actual": actual, "method": method[:120],
        "calls_saved": int(baseline.get("calls", 0)) - int(actual.get("calls", 0)),
        "usd_saved": None if b_cost is None or a_cost is None else round(b_cost - a_cost, 8),
        "seconds_saved": None if b_s is None or a_s is None else round(b_s - a_s, 4),
        "project": (project or "(unknown)")[:120], "traffic": traffic,
    }


def _tokens(fields: dict) -> dict:
    as_int = lambda v: v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0
    return {"input": as_int(fields.get("input_tokens")), "output": as_int(fields.get("output_tokens")),
            "cache_read": as_int(fields.get("cache_read_tokens")), "cache_write": as_int(fields.get("cache_write_tokens"))}


def cheaper_model_step(*, usage: dict, seconds: float | None, served_model: str | None, host_model: str | None,
                       host_cache_warm: bool, mechanism: str, judge_seconds: float = 0.0, project: str | None,
                       traffic: str, rates: dict | None = None) -> dict:
    """Receipt for one model call served by a cheaper model instead of the host.

    Actual cost is the provider's ``cost_usd`` when present, else the served
    model's list price. The baseline prices the same tokens on the host; when
    the host's cache was warm (an earlier host call in this session within the
    cache lifetime) the call's cache writes are priced as host cache reads.
    Seconds: output time rescaled by measured throughput; judge time is added
    to the actual side."""
    rates = rates or DEFAULT_RATES
    tokens = _tokens(usage)
    actual_cost = _num(usage.get("cost_usd"))
    if actual_cost is None:
        actual_cost = price(served_model, tokens, rates)
    base_tokens = tokens
    if host_cache_warm and tokens["cache_write"]:
        base_tokens = dict(tokens, cache_read=tokens["cache_read"] + tokens["cache_write"],
                           input=tokens["input"] + tokens["cache_write"], cache_write=0)
    baseline_cost = price(host_model, base_tokens, rates)
    tps_served, tps_host = throughput(served_model), throughput(host_model)
    baseline_s = seconds * tps_served / tps_host if (seconds is not None and tps_served and tps_host) else None
    actual_s = None if seconds is None else seconds + judge_seconds
    return receipt(lever="cheaper_model", mechanism=mechanism, decision="step_on_" + str(served_model),
                   baseline=_side(host_model, 1, baseline_cost, baseline_s),
                   actual=_side(served_model, 1, actual_cost, actual_s, judge_seconds=round(judge_seconds, 4) or None),
                   method=("list prices on recorded tokens" + ("; host cache warm" if host_cache_warm else "")
                           + "; seconds by measured throughput"),
                   project=project, traffic=traffic)


def host_rebuild_after_cheap(*, usage: dict, seconds: float | None, host_model: str | None, mechanism: str,
                             project: str | None, traffic: str, rates: dict | None = None) -> dict:
    """Receipt charging routing for the host's cache rebuild on its first call
    after a cheaper-model turn: without the switch those cache writes would
    have been cache reads."""
    rates = rates or DEFAULT_RATES
    tokens = _tokens(usage)
    actual_cost = _num(usage.get("cost_usd"))
    if actual_cost is None:
        actual_cost = price(host_model, tokens, rates)
    warm = dict(tokens, cache_read=tokens["cache_read"] + tokens["cache_write"],
                input=tokens["input"] + tokens["cache_write"], cache_write=0)
    baseline_cost = price(host_model, warm, rates)
    return receipt(lever="cheaper_model", mechanism=mechanism, decision="host_cache_rebuild_after_cheap_turn",
                   baseline=_side(host_model, 1, baseline_cost, seconds), actual=_side(host_model, 1, actual_cost, seconds),
                   method="host cache writes repriced as reads (no switch)", project=project, traffic=traffic)


def judge_overhead(*, mechanism: str, judge_seconds: float, host_model: str | None, project: str | None,
                   traffic: str) -> dict:
    """Receipt for a judged turn that stayed on the host: the judge's time is
    pure overhead (nothing saved)."""
    return receipt(lever="cheaper_model", mechanism=mechanism, decision="kept_on_host",
                   baseline=_side(host_model, 0, 0.0, 0.0), actual=_side(host_model, 0, 0.0, judge_seconds, judge_calls=1),
                   method="judge latency only", project=project, traffic=traffic)


def prepared_action(*, tool: str, decision_seconds: float, host_model: str | None, avg_host_call_usd: float | None,
                    avg_host_call_seconds: float | None, mechanism: str, project: str | None, traffic: str) -> dict:
    """Receipt for a model call replaced by a prepared tool action. Baseline:
    this session's average host call so far (unknown until one is seen)."""
    return receipt(lever="prepared_action", mechanism=mechanism, decision="prepared_" + tool,
                   baseline=_side(host_model, 1, avg_host_call_usd, avg_host_call_seconds),
                   actual=_side(None, 0, 0.0, decision_seconds),
                   method="session average host call", project=project, traffic=traffic)


def judge_only(*, lever: str, mechanism: str, decision: str, judge_seconds: float, host_model: str | None,
               project: str | None, traffic: str) -> dict:
    """Receipt for a judge call whose answer changed nothing (it abstained,
    or kept the step on the host): its latency is pure overhead."""
    return receipt(lever=lever, mechanism=mechanism, decision=decision,
                   baseline=_side(host_model, 0, 0.0, 0.0),
                   actual=_side(host_model, 0, 0.0, judge_seconds, judge_calls=1),
                   method="judge latency only", project=project, traffic=traffic)


def prepared_step(*, tool: str, decision_seconds: float, host_model: str | None, skipped_prompt_tokens: int,
                  skipped_output_tokens: int, skipped_seconds: float | None, mechanism: str, project: str | None,
                  traffic: str, repeated: bool = False, prepared_tokens: int = 0, rates: dict | None = None) -> dict:
    """Receipt for one model call replaced by a prepared action, priced once
    the next model call is known (per-step decision point).

    The skipped call is the same step on the default (host) model: its prompt
    is the next call's prompt minus the prepared result, all of it a cache
    read (the conversation prefix was already cached or is written by the next
    call either way), plus a typical tool-call step's output. When the model
    re-fetched what the prepared action fetched (``repeated``), nothing was
    saved: the prepared result was extra context, charged as one host cache
    write, and the decision time is lost."""
    rates = rates or DEFAULT_RATES
    if repeated:
        extra = price(host_model, {"input": prepared_tokens, "cache_write": prepared_tokens}, rates) \
            if prepared_tokens else 0.0
        return receipt(lever="prepared_action", mechanism=mechanism, decision="prepared_repeated_by_model",
                       baseline=_side(host_model, 0, 0.0, 0.0),
                       actual=_side(None, 0, extra, decision_seconds),
                       method="model re-fetched the prepared result; its tokens charged as a host cache write",
                       project=project, traffic=traffic)
    tokens = {"input": max(0, skipped_prompt_tokens), "cache_read": max(0, skipped_prompt_tokens),
              "output": max(0, skipped_output_tokens)}
    return receipt(lever="prepared_action", mechanism=mechanism, decision="prepared_" + tool,
                   baseline=_side(host_model, 1, price(host_model, tokens, rates), skipped_seconds,
                                  prompt_tokens=tokens["input"], output_tokens=tokens["output"]),
                   actual=_side(None, 0, 0.0, decision_seconds),
                   method=("skipped host call: next call's prompt minus the prepared result as cache reads, "
                           "typical tool-step output; seconds from the next call's latency"),
                   project=project, traffic=traffic)


def discarded_cheap_step(*, usage: dict, seconds: float | None, served_model: str | None, host_model: str | None,
                         mechanism: str, reason: str, project: str | None, traffic: str,
                         rates: dict | None = None) -> dict:
    """Receipt for a cheaper-model step whose response was discarded (it
    edited or ended the turn) and re-run on the host: the cheap call is a
    pure loss."""
    rates = rates or DEFAULT_RATES
    cost = _num(usage.get("cost_usd"))
    if cost is None:
        cost = price(served_model, _tokens(usage), rates)
    return receipt(lever="cheaper_model", mechanism=mechanism, decision="cheap_step_discarded_" + reason,
                   baseline=_side(host_model, 0, 0.0, 0.0), actual=_side(served_model, 1, cost, seconds),
                   method="discarded cheaper-model call (host re-ran the step)", project=project, traffic=traffic)


def _empty() -> dict:
    return {"receipts": 0, "calls_saved": 0, "usd_saved": 0.0, "seconds_saved": 0.0,
            "usd_unknown": 0, "seconds_unknown": 0}


def _add(into: dict, data: dict) -> None:
    into["receipts"] += 1
    into["calls_saved"] += int(data.get("calls_saved") or 0)
    usd, secs = _num(data.get("usd_saved")), _num(data.get("seconds_saved"))
    if usd is None:
        into["usd_unknown"] += 1
    else:
        into["usd_saved"] += usd
    if secs is None:
        into["seconds_unknown"] += 1
    else:
        into["seconds_saved"] += secs


def _round(d: dict) -> dict:
    d["usd_saved"] = round(d["usd_saved"], 6)
    d["seconds_saved"] = round(d["seconds_saved"], 3)
    return d


def iter_receipts(events_dir: str | Path) -> Iterable[dict]:
    """Every stored efficiency event (deduplicated by event_id)."""
    root = Path(events_dir).expanduser()
    seen: set[str] = set()
    for path in sorted(root.glob("*.jsonl")) if root.is_dir() else []:
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if EVENT not in line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("event") != EVENT or event.get("event_id") in seen:
                    continue
                seen.add(event.get("event_id"))
                yield event


def aggregate(events: Iterable[dict], *, include_test: bool = False, since: str | None = None) -> dict:
    """Sum receipts by lever and by project x lever. Exact: plain sums of the
    stored ``calls_saved`` / ``usd_saved`` / ``seconds_saved`` fields."""
    totals, by_lever, by_project = _empty(), {lever: _empty() for lever in LEVERS}, {}
    excluded = 0
    for event in events:
        data = event.get("data") or {}
        if since and str(event.get("timestamp", ""))[:10] < since:
            continue
        if data.get("traffic") != "production" and not include_test:
            excluded += 1
            continue
        lever = data.get("lever")
        if lever not in by_lever:
            continue
        project = data.get("project") or "(unknown)"
        _add(totals, data)
        _add(by_lever[lever], data)
        _add(by_project.setdefault(project, {lv: _empty() for lv in LEVERS}).setdefault(lever, _empty()), data)
    return {
        "totals": _round(totals),
        "by_lever": {lv: dict(_round(v), label=LEVER_LABELS[lv]) for lv, v in by_lever.items()},
        "by_project": {p: {lv: _round(v) for lv, v in levers.items()} for p, levers in sorted(by_project.items())},
        "excluded_test_receipts": excluded,
        "include_test": include_test,
        "method": "Sums of per-decision receipts (fast_decisions:efficiency). Each receipt fixes its baseline "
                  "(the same step on the default model) and its savings at decision time.",
    }


def summarize(events_dir: str | Path, *, include_test: bool = False, since: str | None = None) -> dict:
    report = aggregate(iter_receipts(events_dir), include_test=include_test, since=since)
    report["events_dir"] = str(Path(events_dir).expanduser())
    return report
