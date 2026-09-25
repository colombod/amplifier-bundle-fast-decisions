"""Waste guards: deterministic per-tool-call checks that remove wasted model
steps (measured in docs/evidence/2026-09-25/waste-census).

Harness-neutral core, used by the Amplifier orchestrator (``ObservedTool``)
and the Claude Code hook (``claude_hook.py``). Each guard is safe by
construction, can be overridden by the model, and leaves an efficiency
receipt (``efficiency.guard_receipt``):

* **Repeat stop** (``loop_stop`` / ``guard:identical_repeat``): a call whose
  last ``repeat_limit`` runs returned byte-identical output with nothing
  changed in between (no write, no mutating command, no helper, no new user
  turn) is not run again; the model is told to use the earlier result. When a
  background job is running and the command reads time-varying state, the
  call is instead run in place until its output changes (poll wait).
* **Retry stop** (``loop_stop`` / ``guard:error_retry``): the next run of a
  command that failed ``retry_after_failures`` times in a row with the same
  error and nothing changed is not run; the model is told to change approach.
  Errors that read as transient (locks, rate limits, timeouts, network) are
  exempt.
* **Poll wait** (``loop_stop`` / ``guard:poll_wait``): the second
  ``sleep N && <read-only check>`` poll of the same check within a few steps is
  run in place, at the model's own interval, until the check's output changes
  or ``poll_max_wait_s`` passes (kept under the 5-minute prompt-cache
  lifetime). One model step per poll is replaced by one step per wait.
* **Unchanged output** (``context_rightsize`` / ``guard:identical_result``):
  a read, search or command whose output is identical to the same call's
  output still in context (or a file read whose range an earlier read of the
  unchanged file already covers) is answered with a one-line pointer instead
  of a second copy. The call runs first; the pointer is sent only after the
  comparison.

Override: a blocked call runs normally when the model issues it again right
away (one block per chain); a pointer-answered call returns the full output
the next time. Receipts: blocks claim the waste-census median of further
repeats at the live step's cost only once the model has moved on (two model
calls later without re-issuing); an override is charged as one extra model
call. Poll waits claim one model call per poll run in place. Pointers claim
the elided tokens as a cache write on the next call plus cache reads on the
later calls of the turn. Nothing here raises; on any internal error the call
runs as usual.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Any

from . import efficiency, waste
from .savings import DEFAULT_RATES, _rates_for

# Waste-census medians (docs/evidence/2026-09-25/waste-census/summary.json):
# further repeats after the point where each guard fires; for each the smallest
# of the last-30-days and all-history medians (repeats: 2, the all-history
# median after the 2nd identical run, below 5 after the 3rd). Receipts name them.
CENSUS_REF = "waste-census 2026-09-25"
CENSUS_EXTRA_RETRIES = 1.0
CENSUS_EXTRA_REPEATS = 2.0
CENSUS_EXTRA_POLLS = 1.0
CHARS_PER_TOKEN = 4.0
PREFIX = "[fast-decisions] "
_TRANSIENT = re.compile(r"lock|locked|busy|rate.?limit|too many requests|\b429\b|\b50[234]\b|timed? ?out|timeout|"
                        r"temporar|try again|unavailable|connection (reset|refused)|network|EAGAIN|ECONN",
                        re.I)
_BACKGROUND = re.compile(r"(?<![&|>])&\s*($|\n|;|\))|\bnohup\b|\bsetsid\b|\bdisown\b")


@dataclass
class GuardConfig:
    enabled: bool = True
    repeat_stop: bool = True
    retry_stop: bool = True
    poll_wait: bool = True
    identical_results: bool = True
    repeat_limit: int = 3
    retry_after_failures: int = 2
    poll_max_wait_s: float = 240.0
    poll_min_interval_s: float = 5.0
    poll_max_interval_s: float = 60.0
    poll_status_interval_s: float = 10.0
    poll_max_gap_steps: int = 3
    window_steps: int = 40
    min_elide_chars: int = 600
    whole_read_max_lines: int = 1500
    expected_extra_retries: float = CENSUS_EXTRA_RETRIES
    expected_extra_repeats: float = CENSUS_EXTRA_REPEATS
    expected_extra_polls: float = CENSUS_EXTRA_POLLS

    @classmethod
    def from_config(cls, value: Any) -> "GuardConfig":
        if value is None or value is True:
            return cls()
        if value is False:
            return cls(enabled=False)
        if not isinstance(value, dict):
            return cls()
        return cls(**{k: v for k, v in value.items() if k in cls.__dataclass_fields__})


@dataclass
class Decision:
    action: str                    # "run" | "block" | "wait"
    guard: str | None = None
    message: str | None = None
    key: str | None = None
    interval_s: float = 0.0
    baseline_hash: str | None = None
    baseline_markers: frozenset = frozenset()


@dataclass
class _Exec:
    step: int
    epoch_after: int
    result_hash: str
    chars: int
    failed: bool
    sig: str | None
    err_line: str
    call_id: str | None
    fail_count: int = 0
    same_count: int = 1
    markers: frozenset = frozenset()
    elided: bool = False
    blocked: bool = False


@dataclass
class _Read:
    offset: int
    limit: int | None
    fingerprint: tuple
    step: int
    call_id: str | None
    lines: int | None


@dataclass
class _Pending:
    kind: str                      # "elide" | "block"
    step: int
    data: dict = field(default_factory=dict)


def file_fingerprint(path: str) -> tuple | None:
    """(size, mtime_ns, sha1 of the first 4 MB) or None when unreadable."""
    try:
        st = os.stat(path)
        with open(path, "rb") as handle:
            digest = hashlib.sha1(handle.read(4 * 1024 * 1024)).hexdigest()
        return st.st_size, st.st_mtime_ns, digest
    except OSError:
        return None


def _line_count(path: str) -> int | None:
    try:
        with open(path, "rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return None


class WasteGuard:
    """Per-session guard state. Feed it every model call (``note_model_call``)
    and every tool call (``before`` / ``after``); emit the receipts it returns
    (``take_receipts``)."""

    def __init__(self, config: GuardConfig | None = None, *, harness: str = "Amplifier",
                 project: str | None = None, traffic: str = "production", cwd: str | None = None,
                 rates: dict | None = None):
        self.config = config or GuardConfig()
        self.harness = harness
        self.project = project
        self.traffic = traffic
        self.cwd = cwd
        self.rates = rates or DEFAULT_RATES
        self.step = 0
        self.epoch = 0
        self.execs: dict[str, _Exec] = {}
        self.reads: dict[str, list[_Read]] = {}
        self.polls: dict[str, tuple[int, str, frozenset]] = {}
        self.last_call: dict = {}
        self.present_ids: set[str] | None = None
        self.pending: list[_Pending] = []
        self.receipts: list[dict] = []
        self.background = False
        self.firings: dict[str, int] = {}
        # Claude Code writes the turn's last model call to the transcript
        # after its Stop hook runs; a tool result is always followed by at
        # least one model call, so the hook counts that one call.
        self.assume_followup_call = False

    # ------------------------------------------------------------ lifecycle
    def new_turn(self) -> None:
        """A new user turn is a state change: the user may have fixed
        something, so earlier failures and repeats no longer count."""
        self.epoch += 1
        self.polls.clear()

    def note_model_call(self, *, model: str | None, usage: dict | None, cost_usd: float | None,
                        seconds: float | None, present_ids: set[str] | None = None) -> None:
        usage = usage or {}
        if cost_usd is None:
            cost_usd = self._price(model, usage)
        self.step += 1
        self.last_call = {"model": model, "cost_usd": cost_usd, "seconds": seconds}
        if present_ids is not None:
            self.present_ids = present_ids
        for p in self.pending:
            if p.kind == "elide":
                p.data.setdefault("later", []).append(model)
        for p in list(self.pending):
            if p.kind == "block" and self.step - p.step >= 2:
                self.pending.remove(p)
                self.receipts.append(self._block_receipt(p, overridden=False))

    def end_turn(self) -> list[dict]:
        for p in self.pending:
            self.receipts.append(self._elide_receipt(p) if p.kind == "elide" else self._block_receipt(p, False))
        self.pending = []
        return self.take_receipts()

    def take_receipts(self) -> list[dict]:
        out, self.receipts = [r for r in self.receipts if r], []
        return out

    # ------------------------------------------------------------- keys
    def _resolve(self, path: str) -> str:
        if not path:
            return path
        path = os.path.expanduser(path)
        if not os.path.isabs(path) and self.cwd:
            path = os.path.join(self.cwd, path)
        return os.path.normpath(path)

    def _key(self, tool: str, tool_input: Any) -> tuple[str, str | None, str]:
        kind, key = waste.call_kind(tool, tool_input)
        raw = key or ""
        if kind == "read" and key:
            off, lim = waste.read_range(tool_input)
            key = f"{self._resolve(key)}#{off}:{lim}"
        return kind, (f"{kind}:{tool}:{key}" if key else None), raw

    # ------------------------------------------------------------- before
    def before(self, tool: str, tool_input: Any) -> Decision:
        if not self.config.enabled:
            return Decision("run")
        try:
            return self._before(tool, tool_input)
        except Exception:  # noqa: BLE001 -- a guard error means: run as usual
            return Decision("run")

    def _before(self, tool: str, tool_input: Any) -> Decision:
        kind, key, raw = self._key(tool, tool_input)
        if not key:
            return Decision("run")
        for p in list(self.pending):
            if p.kind == "block" and p.data.get("key") == key:
                # Issued again right after the block: the override. It runs.
                self.pending.remove(p)
                self.receipts.append(self._block_receipt(p, overridden=True))
                return Decision("run", key=key)
        command = raw if kind == "bash" else ""
        if kind == "bash":
            check = waste.poll_check(command)
            if check is not None:
                seen = self.polls.get(check) if check else None
                if self.config.poll_wait and seen and self.step - seen[0] <= self.config.poll_max_gap_steps:
                    interval = min(max(waste.sleep_seconds(command), self.config.poll_min_interval_s),
                                   self.config.poll_max_interval_s)
                    return self._fire(Decision("wait", guard="poll_wait", key=key, interval_s=interval,
                                               baseline_hash=seen[1], baseline_markers=seen[2]))
                return Decision("run", key=key)
        prev = self.execs.get(key)
        if not prev or prev.blocked or prev.epoch_after != self.epoch or self.step - prev.step > self.config.window_steps:
            return Decision("run", key=key)
        if (self.config.retry_stop and kind == "bash" and prev.failed
                and prev.fail_count >= self.config.retry_after_failures
                and not _TRANSIENT.search(prev.err_line or "")):
            prev.blocked = True
            msg = (f"{PREFIX}Not run: this exact command already failed {prev.fail_count} times in a row with the "
                   "same error, and nothing has changed since (no edits or other commands)"
                   + (f": {prev.err_line}" if prev.err_line else "") + ". Running it again will fail the same way. "
                   "Change approach: fix the cause, try a different command, or report the blocker. "
                   "(If it really must run again unchanged, issue it once more and it will run.)")
            self._pend_block(key, "error_retry", prev.fail_count)
            return self._fire(Decision("block", guard="error_retry", message=msg, key=key))
        if (self.config.repeat_stop and not prev.failed and prev.same_count >= self.config.repeat_limit):
            if kind == "bash" and self.background and waste.is_time_varying(command) and self.config.poll_wait:
                return self._fire(Decision("wait", guard="poll_wait", key=key,
                                           interval_s=self.config.poll_status_interval_s,
                                           baseline_hash=prev.result_hash,
                                           baseline_markers=frozenset(prev.markers)))
            prev.blocked = True
            msg = (f"{PREFIX}Not run: this exact call returned identical output the last {prev.same_count} times "
                   "and nothing has changed since (no edits, commands, helpers or new user message), so it would "
                   "return the same again. Use that earlier result. If you are waiting for something outside this "
                   "session to change, wait on it in one command (e.g. `timeout 600 bash -c 'until <check>; do sleep "
                   "10; done'`). (If it really must run again, issue it once more and it will run.)")
            self._pend_block(key, "identical_repeat", prev.same_count)
            return self._fire(Decision("block", guard="identical_repeat", message=msg, key=key))
        return Decision("run", key=key)

    def _fire(self, decision: Decision) -> Decision:
        self.firings[decision.guard or "?"] = self.firings.get(decision.guard or "?", 0) + 1
        return decision

    def _pend_block(self, key: str, guard: str, count: int) -> None:
        self.pending.append(_Pending("block", self.step, {
            "key": key, "guard": guard, "count": count, "model": self.last_call.get("model"),
            "step_usd": self.last_call.get("cost_usd"), "step_s": self.last_call.get("seconds")}))

    # -------------------------------------------------------------- after
    def after(self, tool: str, tool_input: Any, result_text: str, failed: bool, *,
              call_id: str | None = None) -> str | None:
        """Record a call that actually ran. Returns replacement text for the
        model when the unchanged-output guard applies, else None."""
        if not self.config.enabled:
            return None
        try:
            return self._after(tool, tool_input, result_text or "", bool(failed), call_id)
        except Exception:  # noqa: BLE001
            return None

    def _after(self, tool, tool_input, text, failed, call_id) -> str | None:
        kind, key, raw = self._key(tool, tool_input)
        epoch_before = self.epoch
        if kind in ("edit", "agent", "other"):
            self.epoch += 1
            if kind == "edit" and raw:
                self.reads.pop(self._resolve(raw), None)
        elif kind == "bash" and raw:
            if _BACKGROUND.search(raw):
                self.background = True
            if not waste.is_readonly_command(raw):
                self.epoch += 1
                self.reads.clear()
            check = waste.poll_check(raw)
            if check:
                self.polls[check] = (self.step, waste.text_hash(text), terminal_markers(text))
        if not key:
            return None
        rhash = waste.text_hash(text)
        prev = self.execs.get(key)
        sig = waste.error_signature(text) if failed else None
        same = bool(prev and prev.result_hash == rhash and prev.epoch_after == epoch_before and not failed)
        replacement = None
        if self.config.identical_results and not failed and len(text) >= self.config.min_elide_chars \
                and not (kind == "bash" and waste.poll_check(raw)):
            if same and not prev.elided and self.step - prev.step <= self.config.window_steps \
                    and self._in_context(prev.call_id) and kind in ("read", "search", "bash"):
                replacement = self._pointer(len(text), self.step - prev.step, "returned exactly the same output as "
                                            "the same call")
            elif kind == "read":
                replacement = self._covered_read(raw, tool_input, len(text), prev)
        if replacement:
            tokens = max(0.0, (len(text) - len(replacement)) / CHARS_PER_TOKEN)
            self.pending.append(_Pending("elide", self.step, {"tokens": tokens, "tool": tool}))
            self.firings["identical_result"] = self.firings.get("identical_result", 0) + 1
        if kind == "read" and not failed:
            self._record_read(raw, tool_input, call_id)
        fail_count = (prev.fail_count + 1 if (failed and prev and prev.failed and prev.sig == sig
                                              and prev.epoch_after == epoch_before) else (1 if failed else 0))
        self.execs[key] = _Exec(self.step, self.epoch, rhash, len(text), failed, sig,
                                waste.first_line(_last_error_line(text)) if failed else "", call_id,
                                fail_count=fail_count, same_count=(prev.same_count + 1) if same else 1,
                                elided=replacement is not None,
                                markers=terminal_markers(text) if kind == "bash" else frozenset())
        return replacement

    def _pointer(self, chars: int, ago: int, what: str) -> str:
        return (f"{PREFIX}Unchanged: this call {what} {ago} model step(s) ago ({chars:,} chars), which is still in "
                "your context, and nothing was changed in between. Use that earlier result. (Issue the call once "
                "more to get the full output again.)")

    def _covered_read(self, raw: str, tool_input: Any, chars: int, prev: _Exec | None) -> str | None:
        if prev is not None and prev.elided:
            return None  # the model asked again after a pointer: full output
        path = self._resolve(raw)
        off, lim = waste.read_range(tool_input)
        fp = file_fingerprint(path)
        if fp is None:
            return None
        for r in reversed(self.reads.get(path, [])):
            if r.fingerprint != fp or self.step - r.step > self.config.window_steps or not self._in_context(r.call_id):
                continue
            whole = r.limit is None and r.offset == 0 and r.lines is not None \
                and r.lines <= self.config.whole_read_max_lines
            covers = whole or (r.limit is not None and lim is not None and r.offset <= off
                               and off + lim <= r.offset + r.limit)
            if covers and not (r.offset == off and r.limit == lim):
                return self._pointer(chars, self.step - r.step, "asked for lines that an earlier read of this "
                                     "file (unchanged since: same size, time and content hash) already returned")
        return None

    def _record_read(self, raw: str, tool_input: Any, call_id: str | None) -> None:
        path = self._resolve(raw)
        fp = file_fingerprint(path)
        if fp is None:
            return
        off, lim = waste.read_range(tool_input)
        lines = _line_count(path) if lim is None and off == 0 else None
        self.reads.setdefault(path, []).append(_Read(off, lim, fp, self.step, call_id, lines))
        del self.reads[path][:-8]

    def _in_context(self, call_id: str | None) -> bool:
        if self.present_ids is None or not call_id:
            return True
        return call_id in self.present_ids

    # ------------------------------------------------------------ poll wait
    def after_wait(self, tool: str, tool_input: Any, result_text: str, failed: bool, *, polls: int,
                   waited_s: float, changed: bool, call_id: str | None = None) -> None:
        """Record a poll run in place (``Decision.action == "wait"``)."""
        try:
            self._after(tool, tool_input, result_text or "", failed, call_id)
            _, key, _ = self._key(tool, tool_input)
            if key and key in self.execs:
                self.execs[key].same_count = 1  # the wait itself is not a repeat
            if polls > 1:
                step_usd, step_s = self.last_call.get("cost_usd"), self.last_call.get("seconds")
                saved = polls - 1
                model = self.last_call.get("model")
                self.receipts.append(efficiency.guard_receipt(
                    lever="loop_stop", mechanism="guard:poll_wait", decision="poll_run_in_place",
                    baseline=efficiency.side(model, saved, None if step_usd is None else saved * step_usd,
                                             None if step_s is None else saved * step_s),
                    actual=efficiency.side(model, 0, 0.0, 0.0),
                    method=(f"{saved} extra polls run in place x live step cost (the model call that issued the "
                            f"poll); waited {waited_s:.0f}s, output {'changed' if changed else 'unchanged'}"),
                    project=self.project, traffic=self.traffic, harness=self.harness,
                    detail={"polls": polls, "waited_s": round(waited_s, 1), "changed": changed,
                            "step_usd": step_usd, "step_seconds": step_s}))
        except Exception:  # noqa: BLE001
            return

    # --------------------------------------------------------------- receipts
    def _price(self, model: str | None, usage: dict) -> float | None:
        rates = _rates_for(model, self.rates)
        if not rates:
            return None
        cr = int(usage.get("cache_read_tokens") or 0)
        inp = max(0, int(usage.get("input_tokens") or 0) - cr)
        return (inp * rates[0] + int(usage.get("output_tokens") or 0) * rates[1] + cr * rates[2]
                + int(usage.get("cache_write_tokens") or 0) * rates[3]) / 1e6

    def _elide_receipt(self, p: _Pending) -> dict | None:
        later = p.data.get("later") or []
        if not later and getattr(self, "assume_followup_call", False) and self.last_call.get("model"):
            later = [self.last_call["model"]]
        if not later:
            return None  # no model call read the pointer: nothing to claim
        tokens = p.data["tokens"]
        usd = 0.0
        for i, model in enumerate(later):
            rates = _rates_for(model, self.rates)
            if rates:
                usd += tokens * (rates[3] if i == 0 else rates[2]) / 1e6
        return efficiency.guard_receipt(
            lever="context_rightsize", mechanism="guard:identical_result", decision="duplicate_output_elided",
            baseline=efficiency.side(later[0], 0, usd, None), actual=efficiency.side(later[0], 0, 0.0, None),
            method=(f"{tokens:.0f} elided tokens (chars/{CHARS_PER_TOKEN:g}) priced as a cache write on the next "
                    f"call and cache reads on {len(later) - 1} later calls in the turn, list prices"),
            project=self.project, traffic=self.traffic, harness=self.harness,
            detail={"tokens": round(tokens, 1), "later_calls": len(later), "tool": p.data.get("tool")})

    def _block_receipt(self, p: _Pending, overridden: bool) -> dict:
        guard = p.data.get("guard") or "identical_repeat"
        model = p.data.get("model") or self.last_call.get("model")
        if overridden:
            step_usd, step_s = self.last_call.get("cost_usd"), self.last_call.get("seconds")
            return efficiency.guard_receipt(
                lever="loop_stop", mechanism="guard:" + guard, decision=guard + "_overridden",
                baseline=efficiency.side(model, 0, 0.0, 0.0), actual=efficiency.side(model, 1, step_usd, step_s),
                method="override: the blocked call was issued again and ran; one extra model call at the live step cost",
                project=self.project, traffic=self.traffic, harness=self.harness, detail={"count": p.data.get("count")})
        n = {"error_retry": self.config.expected_extra_retries,
             "poll_wait": self.config.expected_extra_polls}.get(guard, self.config.expected_extra_repeats)
        step_usd, step_s = p.data.get("step_usd"), p.data.get("step_s")
        return efficiency.guard_receipt(
            lever="loop_stop", mechanism="guard:" + guard, decision=guard + "_blocked",
            baseline=efficiency.side(model, int(round(n)), None if step_usd is None else n * step_usd,
                                     None if step_s is None else n * step_s),
            actual=efficiency.side(model, 0, 0.0, 0.0),
            method=f"{CENSUS_REF} median further repeats ({n:g}) x live step cost; the call was not issued again",
            project=self.project, traffic=self.traffic, harness=self.harness,
            detail={"count": p.data.get("count"), "expected_extra": n, "step_usd": step_usd})


_TERMINAL_WORDS = re.compile(r"\b(done|finished|completed?|succeeded|successful(?:ly)?|success|passed|failed|"
                             r"failure|error|exited|ready|aborted|killed|terminated)\b", re.I)
_TERMINAL_OK = re.compile(r"\bOK\b")


def terminal_markers(text: str) -> frozenset:
    """Words that signal a job finished (done, failed, OK, ...) found in a
    check's output: a new one ends a poll wait."""
    found = {m.group(1).lower() for m in _TERMINAL_WORDS.finditer(text or "")}
    if _TERMINAL_OK.search(text or ""):
        found.add("OK")
    return frozenset(found)


def poll_should_stop(*, markers: frozenset, baseline_markers: frozenset, changed_once: bool,
                     unchanged_streak: int) -> str | None:
    """Why a poll run in place should stop now, or None to keep waiting.
    A new finish word stops it; output that was changing (progress lines)
    and then stops changing for two polls stops it (the job ended or
    stalled); output that has not changed yet keeps it waiting (the model
    is waiting for a change)."""
    if markers - baseline_markers:
        return "finished"
    if changed_once and unchanged_streak >= 2:
        return "settled"
    return None


def _last_error_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    for line in reversed(lines):
        if re.search(r"error|fail|not found|denied|cannot|no such|exception|traceback", line, re.I):
            return line
    return lines[-1] if lines else ""


def poll_note(polls: int, waited_s: float, changed: bool) -> str:
    return (f"{PREFIX}Poll run in place: the check ran {polls} times over {waited_s:.0f}s "
            + ("until its output changed. " if changed else "and its output did not change. ")
            + "To wait longer, wait on the condition in one command (e.g. `timeout 600 bash -c 'until <check>; "
            "do sleep 10; done'`) instead of polling step by step.")
