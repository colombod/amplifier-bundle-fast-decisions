"""Turn planner: cache- and price-aware model choice for one easy turn.

See docs/proposals/TURN-PLANNER.md for the full specification. This module
is a PURE function (no Amplifier, network, or clock side effects beyond the
``now`` parameter the caller supplies): the orchestrator gathers the inputs
(host, candidates, per-session cache state, ``ctx``), calls ``plan_turn``,
records the returned per-model cache state after the real provider call,
and applies the returned ``choice``. No amplifier_core import.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .savings import _rates_for


def _prior_for(model: str, priors: dict[str, dict[str, float]]) -> dict[str, float] | None:
    """Prefix-matched prior lookup for ``model``, mirroring
    ``savings._rates_for``'s dated-id matching: an exact key wins, else the
    longest prior name that is a prefix of ``model`` (a dated id like
    ``claude-sonnet-5-20260101`` matches the family entry
    ``claude-sonnet-5``). ``None`` when no prior matches.
    """
    if model in priors:
        return priors[model]
    for name in sorted(priors, key=len, reverse=True):
        if model.startswith(name):
            return priors[name]
    return None


def _option(
    model: str,
    *,
    ctx: int,
    state: dict[str, dict[str, Any]],
    now: float,
    cache_ttl_seconds: float,
    expected_calls: float,
    output_tokens_per_call: float,
    priors: dict[str, dict[str, float]],
    rates: dict[str, tuple[float, float, float, float]],
) -> dict[str, Any] | None:
    """One option's ``{model, warm, cold, cost, time}``, or ``None`` when
    ``model`` has no price (``rates``, ``savings.DEFAULT_RATES``-shaped) or
    no prior -- per spec, such a model "is not a candidate".
    """
    rate = _rates_for(model, rates)
    prior = _prior_for(model, priors)
    if rate is None or prior is None:
        return None
    entry = state.get(model)
    warm = bool(
        entry is not None
        and (now - entry.get("last_used_at", float("-inf"))) < cache_ttl_seconds
    )
    cached_tokens = entry.get("cached_tokens", 0) if (warm and entry) else 0
    cold = max(0, ctx - cached_tokens) if warm else ctx
    calls_factor = prior.get("calls_factor", 1.0)
    n = expected_calls * calls_factor
    read_rate, write_rate = rate[2], rate[3]
    output_rate = rate[1]
    # Per-model measured output tokens/call (e.g. Fable writes ~2x
    # Sonnet's/Opus's) when the prior has one; else the global
    # planner.output_tokens_per_call fallback. A single global default
    # silently hid Fable's much larger per-call output, understating its
    # cost once warm -- see docs/proposals/TURN-PLANNER.md.
    output_tokens = prior.get("output_tokens_per_call", output_tokens_per_call)
    cost = (
        cold * write_rate
        + (ctx - cold) * read_rate
        + (n - 1) * ctx * read_rate
        + n * output_tokens * output_rate
    ) / 1_000_000
    latency_s = prior.get("latency_s", 0.0)
    cold_s_per_100k = prior.get("cold_s_per_100k", 0.0)
    time_s = n * latency_s + cold / 100_000 * cold_s_per_100k
    return {"model": model, "warm": warm, "cold": cold, "cost": cost, "time": time_s}


def plan_turn(
    host: str,
    candidates: Sequence[str],
    state: dict[str, dict[str, Any]],
    ctx: int,
    now: float,
    config: dict[str, Any],
    rates: dict[str, tuple[float, float, float, float]],
    p_continue: float = 0.0,
) -> dict[str, Any]:
    """Decide the model for one easy turn (spec:
    docs/proposals/TURN-PLANNER.md, "Lookahead" section).

    ``state`` maps a model id to ``{"last_used_at": float, "cached_tokens": int}``,
    updated by the caller after every real provider response -- this
    function only reads it. ``config`` is the merged
    ``model_routing.planner`` dict (``objective`` -- ``speed``/``cost``/
    ``balanced``/``value`` -- ``cost_tolerance``, ``cache_ttl_seconds``,
    ``expected_calls``, ``output_tokens_per_call`` (a model's own
    ``priors[model].output_tokens_per_call`` wins when present),
    ``priors``, ``value_of_time_usd_per_hour`` (``objective: value`` only));
    ``candidates`` are the cheap models to consider alongside the host.
    ``p_continue`` is the caller-resolved probability that a
    LATER turn in this session runs on the host (see
    ``contracts.DEFAULT_PLANNER_CONTINUE_PROBABILITY`` and
    ``orchestrator._session_kind`` for how it is chosen) -- this function
    only consumes the number, it never decides session kind itself.

    One-step lookahead (the greedy per-turn choice is myopic: picking a
    cheap candidate whose cache write is cheaper THIS turn can force a
    full cold cache write on the host on some LATER turn, instead of the
    single cold write the host would have paid THIS turn if chosen now).
    For every candidate option ``m`` (never the host, whose lookahead is
    always 0), using the HOST option's OWN ``cold`` this turn
    (``cold_host``) and the host's own rates/prior:

        lookahead_cost = p_continue * cold_host * (write_host - read_host)
        lookahead_time = p_continue * cold_host / 100000 * cold_s_per_100k_host

    (``cost`` divides by 1e6 as elsewhere.) These are recorded as their own
    ``lookahead_cost``/``lookahead_time`` fields on each option (``0.0`` for
    the host) so receipts show the term explicitly; the ``speed``/``cost``/
    ``balanced`` rules below compare ``cost + lookahead_cost`` and
    ``time + lookahead_time`` -- the TOTALS -- never the bare fields alone.
    When the host is already warm this turn (``cold_host == 0``), every
    lookahead term is exactly 0 -- there is no stale cache to eventually
    pay for.

    Returns::

        {
            "ctx": ctx,
            "objective": objective,
            "host": host,
            "options": [{"model", "warm", "cold", "cost", "time",
                         "lookahead_cost", "lookahead_time"}, ...],
            "choice": <model id> | None,
            "abstained": bool,
        }

    ``abstained`` is ``True`` only when the host itself has no price or no
    prior -- per spec, "the planner abstains and today's behaviour
    applies" (the caller must fall back to its pre-planner logic; ``choice``
    is ``None`` and ``options`` is empty in that case). Otherwise ``choice``
    is always a model id present in ``options`` -- ``host`` when the host is
    chosen, a candidate id otherwise. Ties go to the host.
    """
    objective = config.get("objective", "balanced")
    cache_ttl_seconds = config.get("cache_ttl_seconds", 300)
    expected_calls = config.get("expected_calls", 3.5)
    output_tokens_per_call = config.get("output_tokens_per_call", 170)
    priors = config.get("priors") or {}
    cost_tolerance = config.get("cost_tolerance", 0.05)

    def option_for(model: str) -> dict[str, Any] | None:
        return _option(
            model,
            ctx=ctx,
            state=state,
            now=now,
            cache_ttl_seconds=cache_ttl_seconds,
            expected_calls=expected_calls,
            output_tokens_per_call=output_tokens_per_call,
            priors=priors,
            rates=rates,
        )

    host_option = option_for(host)
    if host_option is None:
        return {
            "ctx": ctx,
            "objective": objective,
            "host": host,
            "options": [],
            "choice": None,
            "abstained": True,
        }
    host_option["lookahead_cost"] = 0.0
    host_option["lookahead_time"] = 0.0

    # host_option is not None here, so its rate/prior are guaranteed to
    # exist too (that is exactly what _option looked up to build it) --
    # asserted, not just assumed, so a future refactor that breaks this
    # invariant fails loudly instead of silently mis-costing lookahead.
    host_rate = _rates_for(host, rates)
    host_prior = _prior_for(host, priors)
    assert host_rate is not None and host_prior is not None
    write_host, read_host = host_rate[3], host_rate[2]
    cold_s_per_100k_host = host_prior.get("cold_s_per_100k", 0.0)
    cold_host = host_option["cold"]

    options = [host_option]
    seen = {host}
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        option = option_for(candidate)
        if option is not None:
            option["lookahead_cost"] = p_continue * cold_host * (write_host - read_host) / 1_000_000
            option["lookahead_time"] = p_continue * cold_host / 100_000 * cold_s_per_100k_host
            options.append(option)

    def total_cost(opt: dict[str, Any]) -> float:
        return opt["cost"] + opt["lookahead_cost"]

    def total_time(opt: dict[str, Any]) -> float:
        return opt["time"] + opt["lookahead_time"]

    host_total_cost = total_cost(host_option)
    value_of_time = config.get("value_of_time_usd_per_hour", 36)

    if objective == "speed":
        best = min(total_time(opt) for opt in options)
        winners = [opt["model"] for opt in options if total_time(opt) == best]
    elif objective == "cost":
        best = min(total_cost(opt) for opt in options)
        winners = [opt["model"] for opt in options if total_cost(opt) == best]
    elif objective == "value":
        # Converts TOTAL time (base + lookahead) into a dollar figure at
        # value_of_time_usd_per_hour and adds it to TOTAL cost, so the
        # whole speed/cost/lookahead triangle collapses to ONE number to
        # minimise -- no separate cost_tolerance budget step is needed
        # (unlike balanced): value_of_time_usd_per_hour == 0 degenerates
        # to exactly the "cost" objective; an arbitrarily large value
        # degenerates to exactly "speed" (whichever option's utility that
        # value dominates is the one with the least total_time).
        for opt in options:
            opt["utility"] = total_cost(opt) + value_of_time / 3600 * total_time(opt)
        best = min(opt["utility"] for opt in options)
        winners = [opt["model"] for opt in options if opt["utility"] == best]
    else:  # balanced
        budget = host_total_cost * (1 + cost_tolerance)
        eligible = [opt for opt in options if opt["model"] == host or total_cost(opt) <= budget]
        best = min(total_time(opt) for opt in eligible)
        winners = [opt["model"] for opt in eligible if total_time(opt) == best]

    choice = host if host in winners else winners[0]

    result = {
        "ctx": ctx,
        "objective": objective,
        "host": host,
        "options": options,
        "choice": choice,
        "abstained": False,
    }
    if objective == "value":
        result["value_of_time_usd_per_hour"] = value_of_time
    return result
