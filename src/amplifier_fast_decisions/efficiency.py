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

from .savings import DEFAULT_RATES, _rates_for, price

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
            project: str | None, traffic: str, harness: str | None = None, detail: dict | None = None) -> dict:
    """Build receipt data with the deltas fixed (baseline minus actual)."""
    if lever not in LEVERS:
        raise ValueError(f"unknown efficiency lever: {lever}")
    b_cost, a_cost = _num(baseline.get("cost_usd")), _num(actual.get("cost_usd"))
    b_s, a_s = _num(baseline.get("seconds")), _num(actual.get("seconds"))
    data = {
        "lever": lever, "mechanism": mechanism[:80], "decision": decision[:80],
        "baseline": baseline, "actual": actual, "method": method[:200],
        "calls_saved": int(baseline.get("calls", 0)) - int(actual.get("calls", 0)),
        "usd_saved": None if b_cost is None or a_cost is None else round(b_cost - a_cost, 8),
        "seconds_saved": None if b_s is None or a_s is None else round(b_s - a_s, 4),
        "project": (project or "(unknown)")[:120], "traffic": traffic,
    }
    if harness:
        data["harness"] = str(harness)[:40]
    if detail:
        data["detail"] = {k: v for k, v in detail.items() if isinstance(v, (int, float, str, bool)) or v is None}
    return data


def side(model: str | None, calls: int, cost_usd: float | None, seconds: float | None, **extra: Any) -> dict:
    """One side (baseline or actual) of a receipt."""
    return _side(model, calls, cost_usd, seconds, **extra)


def guard_receipt(*, lever: str, mechanism: str, decision: str, baseline: dict, actual: dict, method: str,
                  project: str | None, traffic: str, harness: str | None = None, detail: dict | None = None) -> dict:
    """Receipt for a waste guard firing (guards.py). Same shape as every
    other receipt, plus ``harness`` and a small numeric ``detail`` block with
    the inputs of the estimate so it can be recomputed."""
    return receipt(lever=lever, mechanism=mechanism, decision=decision, baseline=baseline, actual=actual,
                   method=method, project=project, traffic=traffic, harness=harness, detail=detail)


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


CACHE_TTL_S = 300.0  # Anthropic prompt cache: 5-minute TTL, refreshed on use


def cache_keepalive(*, model: str | None, prefix_tokens: int, refresh_costs: list, tools: list[str],
                    gap_s: float | None, next_cache_read: int | None, next_model: str | None, project: str | None,
                    traffic: str, shared_warm_tokens: int = 0, mechanism: str = "rule:tool_wait",
                    ttl_s: float = CACHE_TTL_S, rates: dict | None = None) -> dict:
    """Receipt for one keep-alive episode (one or more refreshes during one wait).

    Baseline (the same next step without keep-alive, same model): the next
    real call re-writes the cached prefix (``prefix_tokens`` x cache-write
    price) when the time since the cache was last used (``gap_s``) reached the
    TTL, or reads it (x cache-read price) when it did not. The leading
    ``shared_warm_tokens`` (what the session's first call already read from
    cache, i.e. a prefix other sessions keep warm: tools and the static system
    prompt) are assumed to stay warm without keep-alive and are priced as reads
    on both sides. Actual: every
    refresh's cost plus the next call's price for those prefix tokens as
    observed -- the part it read from cache at the read price, the rest at the
    write price. With no later call on the same model nothing reused the
    cache, so nothing was avoided. Calls: refreshes are extra calls
    (calls_saved = -refreshes). Seconds are not estimated (refreshes run
    concurrently with the tool; the cold-write latency is not modelled)."""
    rates = rates or DEFAULT_RATES
    r = _rates_for(model, rates)
    n = len(refresh_costs)
    known = [c for c in refresh_costs if isinstance(c, (int, float)) and not isinstance(c, bool)]
    refresh_usd = float(sum(known)) if len(known) == n else None
    prefix = max(0, int(prefix_tokens or 0))
    shared = min(max(0, int(shared_warm_tokens or 0)), prefix)
    extra = {"refresh_calls": n, "prefix_tokens": prefix, "tools": sorted(set(tools))[:8]}
    if next_model is None or next_cache_read is None:
        base, actual = 0.0, refresh_usd
        decision, method = "keepalive_unused", "no later call in the turn reused the cache"
    elif next_model != model:
        base, actual = 0.0, refresh_usd
        decision, method = "keepalive_unused", "next call ran on another model (per-model caches)"
    else:
        expired = gap_s is None or gap_s >= ttl_s
        read = min(max(0, int(next_cache_read)), prefix)
        if r is None:
            base = actual = None
        else:
            base = ((prefix - shared) * (r[3] if expired else r[2]) + shared * r[2]) / 1e6
            actual = None if refresh_usd is None else refresh_usd + (read * r[2] + (prefix - read) * r[3]) / 1e6
        confirmed = prefix > 0 and read >= 0.95 * prefix
        decision = ("keepalive_not_needed" if not expired else
                    "kept_cache_warm" if confirmed else "keepalive_missed")
        method = (f"list prices; prefix = last call's cached tokens ({shared} shared-warm); next call read {read}/{prefix} "
                  f"({'confirmed' if confirmed else 'not confirmed'}); gap {round(gap_s or 0)}s vs ttl {int(ttl_s)}s")
    return receipt(lever="cache_keepalive", mechanism=mechanism, decision=decision,
                   baseline=_side(model, 0, base, None, prefix_tokens=prefix, shared_warm_tokens=shared),
                   actual=_side(model, n, actual, None, **extra), method=method, project=project, traffic=traffic)


def loop_stop(*, kinds: list[str], tools: list[str], expected_further_calls: int,
              observed_calls: int | None, observed_usd: float | None, observed_seconds: float | None,
              marginal_call_usd: float | None, marginal_call_seconds: float | None, note_usd: float | None,
              host_model: str | None, source: str, project: str | None, traffic: str) -> dict:
    """Receipt for a turn's loop-stop nudge (a short note to the model, never a hard stop).

    One receipt per turn, from the first note on (later notes in the same turn
    join it, so a call matching two patterns is never counted twice).
    Baseline: ``expected_further_calls`` model calls continuing the first
    pattern -- its mean continuation in real sessions without a note
    (``source``) -- priced at this turn's marginal call (mean of its calls
    after the first, which carries the cold cache write). Actual: the distinct
    model calls observed after the note that continued any noted pattern,
    until the turn ended, at their provider-reported cost, plus the note's
    own tokens. Calls that replace the pattern (a different way to wait or
    retry) are not counted, so this can overstate; the method says what was
    measured. When the rest of the turn could not be
    observed (cancelled or failed turn) ``observed_calls`` is None: calls_saved
    then comes from the observed stop and the method says so."""
    measured = observed_calls is not None
    per_usd, per_s = _num(marginal_call_usd), _num(marginal_call_seconds)
    base_usd = None if per_usd is None else expected_further_calls * per_usd
    base_s = None if per_s is None else expected_further_calls * per_s
    if measured:
        calls = int(observed_calls)
        act_usd = None if _num(observed_usd) is None else _num(observed_usd) + (_num(note_usd) or 0.0)
        act_s = _num(observed_seconds)
    else:
        calls, act_usd, act_s = 0, None, None
    first = kinds[0] if kinds else "loop"
    method = (f"expected {expected_further_calls} further '{first}' calls ({source}), "
              "priced at this turn's marginal call; "
              + ("observed pattern calls after the note until turn end, at provider cost" if measured
                 else "rest of turn unobservable: calls_saved from the observed stop"))
    return receipt(lever="loop_stop", mechanism="rule:" + first, decision="nudged_" + "+".join(kinds or ["loop"]),
                   baseline=_side(host_model, expected_further_calls, base_usd, base_s),
                   actual=_side(host_model, calls, act_usd, act_s, tools=sorted(set(tools))[:8], measured=measured),
                   method=method, project=project, traffic=traffic)


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
    totals, by_lever, by_project, by_harness = _empty(), {lever: _empty() for lever in LEVERS}, {}, {}
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
        _add(by_harness.setdefault(str(data.get("harness") or "Amplifier"), _empty()), data)
    return {
        "totals": _round(totals),
        "by_lever": {lv: dict(_round(v), label=LEVER_LABELS[lv]) for lv, v in by_lever.items()},
        "by_project": {p: {lv: _round(v) for lv, v in levers.items()} for p, levers in sorted(by_project.items())},
        "by_harness": {h: _round(v) for h, v in sorted(by_harness.items())},
        "excluded_test_receipts": excluded,
        "include_test": include_test,
        "method": "Sums of per-decision receipts (fast_decisions:efficiency). Each receipt fixes its baseline "
                  "(the same step on the default model) and its savings at decision time.",
    }


def summarize(events_dir: str | Path, *, include_test: bool = False, since: str | None = None) -> dict:
    report = aggregate(iter_receipts(events_dir), include_test=include_test, since=since)
    report["events_dir"] = str(Path(events_dir).expanduser())
    return report
