"""Two per-step efficiency levers inside the hybrid orchestrator (both off by default).

* **Cache keep-alive** (``Policy.cache_keepalive``): while a tool or helper
  agent runs past ``interval_s`` since the prompt cache was last used, re-send
  the last model request unchanged (byte-identical messages, same model,
  tools and thinking settings; output capped) so the provider's 5-minute
  prompt cache stays warm. Stops when the tool finishes or after
  ``max_refreshes``. One ``cache_keepalive`` receipt per episode, settled
  against the next real call's cache reads.
* **Loop stop** (``Policy.loop_stop``): detect an identical tool call repeated
  in a turn with no edit in between, consecutive tool failures, and ``sleep``
  used as a timer; queue a short note for the next model request (delivered as
  a ``tool:post`` context injection) instead of stopping anything. One
  ``loop_stop`` receipt per turn from the first note on, with the model calls
  measured after the note against the real-session remaining-call baseline.

Measured motivation: docs/evidence/2026-09-25/step-opportunity/summary.json.
Neither lever records prompts, arguments or results: receipts carry tool
names, token counts and prices only.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Callable

from . import efficiency
from .contracts import (EXPECTED_FURTHER_CALLS, EXPECTED_SOURCE, KEEPALIVE_DEFAULTS, LOOP_STOP_DEFAULTS, digest,
                        field_value)
from .savings import DEFAULT_RATES, _rates_for, price

EDIT_TOOLS = frozenset({"write_file", "edit_file", "apply_patch", "multi_edit"})
_SLEEP = re.compile(r"(?<![\w.-])sleep\s+(\d+(?:\.\d+)?)([smh]?)\b")
_LOOP_WORDS = re.compile(r"\b(while|until|for)\b")
_BASH_EDIT = re.compile(r"sed\s+-i|(?<![0-9&])>>?\s*(?!&|/dev/null)\S|\btee\b|\bgit\s+(apply|checkout|restore|commit|reset)\b")
# ~ tokens of one note; priced once at the cache-write rate.
NOTE_TOKENS = 90


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _enabled(cfg: dict | None) -> bool:
    return isinstance(cfg, dict) and cfg.get("enabled", True) is True


def sleep_seconds(command: str) -> float | None:
    """Largest ``sleep N`` in a shell command used as a timer, or None. A
    sleep inside a while/until/for loop is a blocking wait, not a timer."""
    if not isinstance(command, str) or _LOOP_WORDS.search(command):
        return None
    best = None
    for m in _SLEEP.finditer(command):
        n = float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]
        best = n if best is None else max(best, n)
    return best


def _command(input: Any) -> str:
    if isinstance(input, dict):
        cmd = input.get("command")
        if isinstance(cmd, str):
            return cmd
    return ""


class LoopWatch:
    """Per-turn loop detector. ``observe`` is called after every tool call."""

    def __init__(self, cfg: dict):
        self.cfg = {**LOOP_STOP_DEFAULTS, **cfg}
        self.expected = {**EXPECTED_FURTHER_CALLS, **(cfg.get("expected_further_calls") or {})}
        self.counts: dict[str, int] = {}
        self.fail_streak = 0
        self.fail_tools: list[str] = []
        self.sleep_calls = 0
        self.pending: list[str] = []
        self.interventions: list[dict] = []

    def _intervene(self, kind: str, tool: str, note: str, sig: str | None = None) -> None:
        self.pending.append(note)
        self.interventions.append({"kind": kind, "tool": tool, "sig": sig, "further": 0, "active": True,
                                   "delivered": False, "calls": set()})

    def observe(self, tool: str, input: Any, success: bool | None, call_index: int = 0) -> None:
        """``call_index`` identifies the model call that issued this tool call
        (parallel tool calls share one), so continuation is counted in model
        calls, not tool calls."""
        command = _command(input) if tool == "bash" else ""
        is_edit = tool in EDIT_TOOLS or bool(command and _BASH_EDIT.search(command))
        sig = tool + ":" + digest(input if isinstance(input, dict) else {"_": str(input)})
        failed = success is False
        timer = sleep_seconds(command) if command else None
        is_timer = timer is not None and timer >= float(self.cfg["min_sleep_s"])
        # Measure: pattern calls after an already-delivered (or queued) note.
        for iv in self.interventions:
            if not iv["active"]:
                continue
            hit = False
            if iv["kind"] == "repeat":
                if is_edit:
                    iv["active"] = False
                else:
                    hit = sig == iv["sig"]
            elif iv["kind"] == "failures":
                if failed:
                    hit = True
                else:
                    iv["active"] = False
            elif iv["kind"] == "sleep_timer":
                hit = is_timer
            if hit:
                iv["further"] += 1
                iv["calls"].add(call_index)
        # Detect.
        if is_edit:
            self.counts.clear()
        else:
            n = self.counts[sig] = self.counts.get(sig, 0) + 1
            if n == int(self.cfg["repeat_threshold"]):
                self._intervene("repeat", tool, (
                    f"The last {n} `{tool}` calls in this turn were identical, with no file edit in between. "
                    "Repeating it again is unlikely to change the result: change approach, or stop and report "
                    "what you found."), sig)
        if failed:
            self.fail_streak += 1
            self.fail_tools.append(tool)
            if self.fail_streak == int(self.cfg["failure_threshold"]):
                names = ", ".join(self.fail_tools[-self.fail_streak:])
                self._intervene("failures", tool, (
                    f"The last {self.fail_streak} tool calls failed ({names}). Diagnose the cause before retrying, "
                    "change approach, or stop and report the blocker."))
        else:
            self.fail_streak, self.fail_tools = 0, []
        if is_timer:
            self.sleep_calls += 1
            if self.sleep_calls == int(self.cfg["sleep_threshold"]):
                self._intervene("sleep_timer", tool, (
                    f"You have used `sleep` as a timer {self.sleep_calls} times this turn. Each check costs a full "
                    "model call. Prefer one blocking command that waits for the condition itself (for example "
                    "`timeout 900 sh -c 'until <check>; do sleep 10; done'`, or `wait` on the process), or stop "
                    "polling and report."))

    def take_note(self) -> str | None:
        if not self.pending:
            return None
        note = "[fast-decisions loop check]\n" + "\n".join(self.pending)
        self.pending = []
        for iv in self.interventions:
            iv["delivered"] = True
        return note


def _is_side_call(request: Any) -> bool:
    meta = field_value(request, "metadata", None)
    return isinstance(meta, dict) and meta.get("stream") is False


def _thinking_requested(provider: Any, request: Any, kwargs: dict) -> bool:
    """Mirror of the Anthropic provider's thinking switch: kwargs win, then
    the provider config, then any reasoning effort."""
    if "extended_thinking" in kwargs:
        return bool(kwargs.get("extended_thinking"))
    config = getattr(provider, "config", None)
    config = config if isinstance(config, dict) else {}
    if "extended_thinking" in config:
        value = config.get("extended_thinking")
        return value is True or str(value).strip().lower() in ("1", "true", "yes", "on")
    effort = field_value(request, "reasoning_effort", None) or kwargs.get("reasoning_effort")
    return bool(effort or config.get("reasoning_effort") or config.get("effort"))


async def _output_cap_safe(provider: Any, model: str | None, request: Any, kwargs: dict) -> bool:
    """True when the refresh's output cap will be honored. With thinking on,
    the Anthropic provider raises max_tokens to budget + buffer unless the
    model's thinking is adaptive-only (requires_adaptive_thinking, or always
    on); a refresh could then generate a whole thinking budget. Dropping
    thinking instead would invalidate the cached messages. Unknown -> unsafe."""
    if not _thinking_requested(provider, request, kwargs):
        return True
    caps_fn = getattr(provider, "_get_request_capabilities", None)
    if not callable(caps_fn) or not isinstance(model, str):
        return False
    try:
        caps = caps_fn(model)
        if asyncio.iscoroutine(caps) or isinstance(caps, asyncio.Future):
            caps = await caps
    except Exception:  # noqa: BLE001
        return False
    if getattr(caps, "supports_thinking", True) is False:
        return True
    return bool(getattr(caps, "requires_adaptive_thinking", False) or getattr(caps, "thinking_always_on", False))


class Levers:
    """Per-execute state for both levers. Every public method never raises."""

    def __init__(self, policy: Any, service: Any, context: Callable[[], tuple[str, str]],
                 rates: dict | None = None, usage_fn: Callable[[Any], dict] | None = None,
                 shared_warm: int | None = None):
        ka, ls = getattr(policy, "cache_keepalive", None), getattr(policy, "loop_stop", None)
        self.keepalive_cfg = {**KEEPALIVE_DEFAULTS, **ka} if _enabled(ka) else None
        # The waste guards (guards.py) act on the same loops and report on the
        # same loop_stop lever; with both on, one loop would be nudged and
        # stopped and receipted twice. The guards win: the nudges stand down.
        wg = getattr(policy, "waste_guards", None)
        guards_on = bool(wg) and not (isinstance(wg, dict) and wg.get("enabled") is False)
        self.loop = LoopWatch(ls) if _enabled(ls) and not guards_on else None
        self.service = service
        self.context = context
        self.usage_fn = usage_fn
        self.rates = rates or DEFAULT_RATES
        self.ttl_s = efficiency.CACHE_TTL_S
        # Cache tokens the session's first model call read (written by other
        # sessions: tools + static system prompt). Kept across turns by the
        # orchestrator; None until the session's first call is seen.
        self.shared_warm = shared_warm
        # Last real model call (for keep-alive) and this turn's call averages.
        self.last: dict | None = None
        self.calls = 0
        self.sum_usd = 0.0
        self.sum_s = 0.0
        self.priced = 0
        # (cost, seconds) of every real model call this turn, in order, and the
        # call count when the first loop note was delivered.
        self.call_log: list[tuple[float | None, float]] = []
        self.note_at: int | None = None
        self.note_kinds: list[str] = []
        self.note_tools: list[str] = []
        self.host_model: str | None = None
        # Keep-alive runtime.
        self.running: dict[int, str] = {}
        self.episode: dict | None = None
        self._task: asyncio.Task | None = None
        self._refreshing = False
        # Set by finish(): no refresh (or new keep-alive task) after the turn.
        self._closed = False

    @property
    def active(self) -> bool:
        return bool(self.keepalive_cfg or self.loop)

    # -- model calls ------------------------------------------------------
    async def after_call(self, provider: Any, request: Any, kwargs: dict, usage: dict, started: float,
                         seconds: float, provider_key: str | None = None) -> None:
        try:
            if self._closed or _is_side_call(request):
                # Background side calls (goal evaluator, summaries) mark
                # themselves stream=False: they are not the conversation's
                # next step, so they neither settle nor replace the capture.
                return
            model = kwargs.get("model") or getattr(provider, "default_model", None)
            model = model if isinstance(model, str) else None
            self.host_model = self.host_model or model
            self.calls += 1
            if self.shared_warm is None:
                self.shared_warm = int(usage.get("cache_read_tokens") or 0)
            cost = _num(usage.get("cost_usd"))
            if cost is None:
                cost = price(model, {"input": usage.get("input_tokens") or 0, "output": usage.get("output_tokens") or 0,
                                     "cache_read": usage.get("cache_read_tokens") or 0,
                                     "cache_write": usage.get("cache_write_tokens") or 0}, self.rates)
            self.call_log.append((cost, seconds))
            if cost is not None:
                self.sum_usd += cost
                self.sum_s += seconds
                self.priced += 1
            if self.episode is not None:
                await self._settle(next_usage=usage, next_model=model, next_started=started)
            if self.keepalive_cfg:
                prefix = int(usage.get("cache_read_tokens") or 0) + int(usage.get("cache_write_tokens") or 0)
                reason = self._ineligible(provider, provider_key, model)
                if reason is None and not await _output_cap_safe(provider, model, request, kwargs):
                    reason = "manual_thinking"
                self.last = {"provider": provider, "request": request,
                             "messages": list(field_value(request, "messages", []) or []),
                             "kwargs": dict(kwargs), "model": model, "prefix": prefix, "anchor": started,
                             "skip": reason}
        except Exception:  # noqa: BLE001 -- levers never break the loop
            return

    def _ineligible(self, provider: Any, provider_key: str | None, model: str | None) -> str | None:
        """Keep-alive is Anthropic-only: the 5-minute TTL and the cache-write
        premium the receipt prices are Anthropic's, and other APIs may reject
        a 1-token output cap."""
        names = (provider_key, getattr(provider, "name", None))
        if not any(isinstance(n, str) and "anthropic" in n.lower() for n in names):
            return "not_anthropic"
        r = _rates_for(model, self.rates)
        if r is None or not r[3] > r[2]:
            return "no_write_premium"
        return None

    def avg_call(self) -> tuple[float | None, float | None]:
        if not self.priced:
            return None, None
        return self.sum_usd / self.priced, self.sum_s / self.priced

    # -- tools ------------------------------------------------------------
    def tool_started(self, key: int, name: str) -> None:
        if not self.keepalive_cfg or self._closed:
            return
        self.running[key] = name
        if self._task is None or self._task.done():
            try:
                self._task = asyncio.get_running_loop().create_task(self._keepalive_loop())
            except RuntimeError:
                self._task = None

    def tool_finished(self, key: int) -> None:
        self.running.pop(key, None)
        if not self.running and self._task is not None and not self._task.done() and not self._refreshing:
            self._task.cancel()

    async def tool_observed(self, tool: str, input: Any, success: bool | None) -> None:
        if self.loop is None:
            return
        try:
            before = len(self.loop.interventions)
            self.loop.observe(tool, input, success, call_index=len(self.call_log))
            for iv in self.loop.interventions[before:]:
                await self.service.emit("loop_note", {"reason_code": "loop_" + iv["kind"], "tool": iv["tool"]})
        except Exception:  # noqa: BLE001
            return

    def take_note(self) -> str | None:
        if self.loop is None:
            return None
        pending = [iv for iv in self.loop.interventions if not iv["delivered"]]
        note = self.loop.take_note()
        if note:
            if self.note_at is None:
                self.note_at = len(self.call_log)
            for iv in pending:
                if iv["kind"] not in self.note_kinds:
                    self.note_kinds.append(iv["kind"])
                self.note_tools.append(iv["tool"])
        return note

    def marginal_call(self) -> tuple[float | None, float | None]:
        """Mean cost and seconds of this turn's calls after its first (the
        first carries the cold cache write), or of the first when alone."""
        rows = [(c, s) for c, s in self.call_log[1:] if c is not None] or \
               [(c, s) for c, s in self.call_log[:1] if c is not None]
        if not rows:
            return None, None
        return sum(c for c, _ in rows) / len(rows), sum(s for _, s in rows) / len(rows)

    # -- keep-alive -------------------------------------------------------
    async def _keepalive_loop(self) -> None:
        cfg = self.keepalive_cfg or {}
        interval = float(cfg["interval_s"])
        while self.running and not self._closed:
            last = self.last
            if last is None or last.get("skip"):
                return
            # Only the unshared part of the prefix is at risk (the receipt
            # credits nothing for the shared-warm tools + system prompt).
            if last["prefix"] - (self.shared_warm or 0) < int(cfg["min_prefix_tokens"]):
                return
            episode = self.episode
            if episode is not None and len(episode["refreshes"]) >= int(cfg["max_refreshes"]):
                return
            anchor = episode["anchor"] if episode is not None else last["anchor"]
            await asyncio.sleep(max(0.0, anchor + interval - time.monotonic()))
            if not self.running or self._closed:
                return
            self._refreshing = True
            try:
                ok = await self._refresh()
            finally:
                self._refreshing = False
            if not ok:
                return
        return

    async def quiesce(self, timeout: float = 5.0) -> None:
        """Before a real model call: let an in-flight refresh finish (bounded)
        so its cost lands in the episode before the call settles it, then
        stop the keep-alive task. Guarantees no refresh overlaps a real call."""
        task = self._task
        if task is None or task.done():
            return
        try:
            if self._refreshing:
                await asyncio.wait({task}, timeout=timeout)
        finally:
            if not task.done():
                task.cancel()

    def _clone(self, last: dict) -> Any:
        """The captured request, byte-identical messages (copied at capture),
        output capped, and never streamed: stream=False keeps the refresh off
        the UI's stream events, and the fast_decisions marker lets request /
        response observers tell it from a real step."""
        request = last["request"]
        cap = int((self.keepalive_cfg or {}).get("max_output_tokens", 1))
        messages = list(last.get("messages") or field_value(request, "messages", []) or [])
        original = field_value(request, "metadata", None)
        metadata = {**(original if isinstance(original, dict) else {}), "stream": False,
                    "fast_decisions": "cache_keepalive"}
        update = {"messages": messages, "max_output_tokens": cap, "metadata": metadata}
        copier = getattr(request, "model_copy", None)
        if callable(copier):
            return copier(update=update)
        import copy
        clone = copy.copy(request)
        if isinstance(clone, dict):
            clone.update(update)
        else:
            for key, value in update.items():
                setattr(clone, key, value)
        return clone

    async def _refresh(self) -> bool:
        last = self.last
        if last is None or self._closed:
            return False
        started = time.monotonic()
        if self.episode is None:
            self.episode = {"model": last["model"], "prefix": last["prefix"], "refreshes": [],
                            "tools": set(), "cache_used": last["anchor"], "anchor": started}
        episode = self.episode
        episode["tools"].update(self.running.values())
        call_id = "keepalive_" + digest({"t": started})[:16]
        try:
            response = await last["provider"].complete(self._clone(last), **last["kwargs"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await self.service.emit("cache_refresh", {"provider_call_id": call_id, "model": last["model"],
                                                       "status": "error", "exception_type": type(exc).__name__,
                                                       "duration_ms": (time.monotonic() - started) * 1000})
            return False
        usage = self.usage_fn(response) if self.usage_fn else {}
        cost = _num(usage.get("cost_usd"))
        if cost is None:
            cost = price(last["model"], {"input": usage.get("input_tokens") or 0,
                                         "output": usage.get("output_tokens") or 0,
                                         "cache_read": usage.get("cache_read_tokens") or 0,
                                         "cache_write": usage.get("cache_write_tokens") or 0}, self.rates)
        episode["refreshes"].append(cost)
        episode["anchor"] = started
        cap = int((self.keepalive_cfg or {}).get("max_output_tokens", 1))
        over = (usage.get("output_tokens") or 0) > cap
        await self.service.emit("cache_refresh", {
            "provider_call_id": call_id, "model": last["model"], "status": "cap_exceeded" if over else "ok",
            "duration_ms": (time.monotonic() - started) * 1000, "tools": sorted(episode["tools"])[:8],
            **{k: v for k, v in usage.items() if k != "served_model"}, "cost_usd": cost})
        if over:
            # The output cap was not honored (e.g. thinking raised it): stop
            # this episode and never refresh this capture again.
            last["skip"] = "cap_exceeded"
            return False
        return True

    async def _settle(self, *, next_usage: dict | None, next_model: str | None,
                      next_started: float | None) -> None:
        episode, self.episode = self.episode, None
        if episode is None or not episode["refreshes"]:
            return
        project, traffic = self.context()
        gap = None if next_started is None else next_started - episode["cache_used"]
        data = efficiency.cache_keepalive(
            model=episode["model"], prefix_tokens=episode["prefix"], refresh_costs=episode["refreshes"],
            tools=sorted(episode["tools"]), gap_s=gap,
            next_cache_read=None if next_usage is None else int(next_usage.get("cache_read_tokens") or 0),
            next_model=next_model if next_usage is not None else None,
            project=project, traffic=traffic, shared_warm_tokens=self.shared_warm or 0, ttl_s=self.ttl_s,
            rates=self.rates)
        await self.service.emit("efficiency", data)

    # -- turn end ---------------------------------------------------------
    async def finish(self, status: str = "ok") -> None:
        """Turn end: stop refreshing, settle an open keep-alive episode (no
        later call reused it), and emit one loop_stop receipt for the turn if a
        note was delivered (measurable only when the turn ended normally)."""
        self._closed = True
        self.running.clear()
        task = self._task
        if task is not None and not task.done():
            try:
                if self._refreshing:
                    # A sent refresh is billed either way; wait briefly only
                    # so its cost is recorded, never long.
                    await asyncio.wait({task}, timeout=2.0)
            except BaseException:  # noqa: BLE001 -- a second cancel must not skip cleanup
                pass
            task.cancel()
        try:
            if self.episode is not None:
                await self._settle(next_usage=None, next_model=None, next_started=None)
            if self.loop is not None and self.note_at is not None:
                project, traffic = self.context()
                delivered = [iv for iv in self.loop.interventions if iv["delivered"]]
                idx = sorted(set().union(*(iv["calls"] for iv in delivered)))
                # call_index is the number of calls made when the tool ran: the
                # issuing call is call_log[index - 1].
                rows = [self.call_log[i - 1] for i in idx if 0 < i <= len(self.call_log)]
                r = _rates_for(self.host_model, self.rates)
                marginal_usd, marginal_s = self.marginal_call()
                first = self.note_kinds[0] if self.note_kinds else "loop"
                measured = status == "ok"
                costs = [c for c, _ in rows]
                data = efficiency.loop_stop(
                    kinds=self.note_kinds, tools=self.note_tools,
                    expected_further_calls=int(self.loop.expected.get(first, 0)),
                    observed_calls=len(rows) if measured else None,
                    observed_usd=(sum(costs) if all(c is not None for c in costs) else None) if measured else None,
                    observed_seconds=sum(t for _, t in rows) if measured else None,
                    marginal_call_usd=marginal_usd, marginal_call_seconds=marginal_s,
                    note_usd=None if r is None else NOTE_TOKENS * len(self.note_kinds) * r[3] / 1e6,
                    host_model=self.host_model, source=EXPECTED_SOURCE, project=project, traffic=traffic)
                await self.service.emit("efficiency", data)
        except Exception:  # noqa: BLE001
            return
