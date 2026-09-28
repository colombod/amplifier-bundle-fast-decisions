#!/usr/bin/env python3
"""Waste census: classify and price wasted model steps in real agent sessions.

Reads Claude Code transcripts (``~/.claude/projects/*/*.jsonl`` plus their
``subagents/``), Amplifier session logs (``~/.amplifier/projects/*/sessions/*/
events.jsonl``) and the fast-decisions events dir, excludes test traffic, and
prices every wasted model step with that step's own recorded usage at list
prices (``savings.DEFAULT_RATES``).

Writes ONLY aggregates (counts, dollars, medians); no prompts, commands, file
contents, project names or paths leave the process.

    PYTHONPATH=src python3 evals/waste_census.py --out docs/evidence/2026-09-25/waste-census/summary.json

Categories (each wasted tool call is attributed to exactly one; the issuing
model step's cost is split evenly over the tool calls it issued):

a  identical tool call repeated, no state change between (same input, no
   write/command/helper since, and for commands the same output)
b  the same failing command retried with the same error and nothing changed
c  sleep/poll loops (``sleep N && check`` repeated; repeated status checks)
d  file re-read while still in context and unchanged (overlapping range, or
   cat/head/sed of a file already read)
e  helper launches for trivial lookups (helper with <= 3 model calls, no
   edits); waste = helper cost minus doing its tool calls inline at the
   parent's median step cost
f  tool calls that failed validation (f_validation) or were denied (f_denied)
g  sequential single read-only calls that could have been batched (the next
   call's target does not appear in the previous result); priced without the
   step's cache writes
h  oversized helper context: helper steps with prompts over 150k tokens,
   excess tokens priced as cache reads
x  oversized tool outputs (> 20k chars): excess over 8k chars carried as
   cache reads through the following steps until compaction (upper bound)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from amplifier_fast_decisions import waste  # noqa: E402
from amplifier_fast_decisions.savings import DEFAULT_RATES, _rates_for  # noqa: E402

# List prices from savings.DEFAULT_RATES (the task's table: opus-5-5 4/20/0.20/5,
# sonnet-5 3/15/0.30/3.75, haiku-4-5 1/5/0.10/1.25, fable-5-1 10/50/0.25/12.5)
# plus older Claude models seen in the history at their published list prices.
# Models with no price here (GPT, open-weight) use the provider's recorded
# cost_usd when present, else count as unpriced.
RATES = dict(DEFAULT_RATES)
RATES.update({
    "claude-opus-4-8": (5.0, 25.0, 0.50, 6.25), "claude-opus-4-6": (5.0, 25.0, 0.50, 6.25),
    "claude-opus-4-5": (5.0, 25.0, 0.50, 6.25), "claude-sonnet-4-5": (3.0, 15.0, 0.30, 3.75),
})
EXCLUDE = re.compile(r"tmp|private|worktree|afast|experiment|eval|holdout|bench|swebench|smoke|pilot|"
                     r"test-session|overhead-check|/fd-|-fd-", re.I)
HELPER_TRIVIAL_MAX_STEPS = 3
HELPER_OVERSIZE_TOKENS = 150_000
BIG_OUTPUT_CHARS = 20_000
BIG_OUTPUT_KEEP_CHARS = 8_000
CHARS_PER_TOKEN = 4.0
CATEGORIES = ("a_identical_repeat", "b_error_retry", "c_sleep_poll", "d_reread_in_context", "e_trivial_helper",
              "f_validation", "f_denied", "g_batchable_reads", "h_oversized_helper_context",
              "x_oversized_tool_output", "y_cache_rebuild_after_tool_wait", "y_cache_rebuild_after_user_idle",
              "y_cache_rewrite_churn")


@dataclass
class Call:
    tool: str
    input: Any
    id: str | None = None
    result: str = ""
    is_error: bool = False
    has_result: bool = False
    category: str | None = None


@dataclass
class Step:
    model: str | None
    tokens: dict
    cost: float | None
    calls: list = field(default_factory=list)
    compaction_before: bool = False
    ts: str | None = None

    def write_cost(self) -> float:
        rates = _rates_for(self.model, RATES)
        return (self.tokens.get("cache_write", 0) * rates[3] / 1e6) if rates else 0.0

    def prompt_tokens(self) -> int:
        t = self.tokens
        return t.get("input", 0) + t.get("cache_read", 0) + t.get("cache_write", 0)


@dataclass
class Session:
    harness: str
    sid: str
    parent: str | None = None
    steps: list = field(default_factory=list)
    agent_launches: int = 0

    @property
    def cost(self) -> float:
        return sum(s.cost or 0.0 for s in self.steps)


def price(model: str | None, t: dict, fallback: float | None = None) -> float | None:
    """t: input = uncached input tokens."""
    rates = _rates_for(model, RATES)
    if rates is None:
        return fallback
    return (t.get("input", 0) * rates[0] + t.get("output", 0) * rates[1] + t.get("cache_read", 0) * rates[2]
            + t.get("cache_write", 0) * rates[3]) / 1e6


def _int(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


# ---------------------------------------------------------------- Claude Code
def _cc_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def parse_claude_file(path: Path, sid: str, parent: str | None, seen_ids: set, stats: Counter) -> list[Session]:
    main = Session("claude_code", sid, parent)
    side = Session("claude_code", sid + "#sidechain", sid)
    by_msg: dict[str, Step] = {}
    calls_by_id: dict[str, Call] = {}
    cwd_checked = False
    pending_compaction = {id(main): False, id(side): False}
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return []
    with handle:
        for line in handle:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not cwd_checked and e.get("cwd"):
                cwd_checked = True
                if EXCLUDE.search(e["cwd"]):
                    stats["claude_sessions_excluded_cwd"] += 1
                    return []
            sess = side if e.get("isSidechain") and parent is None else main
            kind = e.get("type")
            if kind == "system" and e.get("subtype") == "compact_boundary" or e.get("isCompactSummary"):
                pending_compaction[id(sess)] = True
                continue
            msg = e.get("message") if isinstance(e.get("message"), dict) else None
            if kind == "assistant" and msg:
                mid = msg.get("id") or e.get("uuid")
                model = msg.get("model")
                if model == "<synthetic>":
                    continue
                step = by_msg.get(mid)
                if step is None:
                    if mid in seen_ids:
                        stats["claude_duplicate_messages"] += 1
                        by_msg[mid] = Step(model, {}, None)  # absorb blocks, count nothing
                        by_msg[mid].dup = True  # type: ignore[attr-defined]
                        continue
                    seen_ids.add(mid)
                    u = msg.get("usage") or {}
                    tokens = {"input": _int(u.get("input_tokens")), "output": _int(u.get("output_tokens")),
                              "cache_read": _int(u.get("cache_read_input_tokens")),
                              "cache_write": _int(u.get("cache_creation_input_tokens"))}
                    step = Step(model, tokens, price(model, tokens), ts=e.get("timestamp"),
                                compaction_before=pending_compaction[id(sess)])
                    pending_compaction[id(sess)] = False
                    by_msg[mid] = step
                    sess.steps.append(step)
                else:
                    u = msg.get("usage") or {}
                    if not getattr(step, "dup", False) and _int(u.get("output_tokens")) > step.tokens.get("output", 0):
                        step.tokens["output"] = _int(u.get("output_tokens"))
                        step.cost = price(step.model, step.tokens)
                if getattr(step, "dup", False):
                    continue
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        call = Call(str(block.get("name")), block.get("input"), block.get("id"))
                        step.calls.append(call)
                        if call.id:
                            calls_by_id[call.id] = call
                        if call.tool in waste.AGENT_TOOLS:
                            sess.agent_launches += 1
            elif kind == "user" and msg and isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        call = calls_by_id.get(block.get("tool_use_id"))
                        if call is not None:
                            call.result = _cc_result_text(block.get("content"))
                            call.is_error = bool(block.get("is_error"))
                            call.has_result = True
    return [s for s in (main, side) if s.steps]


def claude_sessions(root: Path, stats: Counter) -> list[Session]:
    sessions: list[Session] = []
    seen_ids: set = set()
    for project in sorted(root.iterdir()) if root.is_dir() else []:
        if not project.is_dir():
            continue
        if EXCLUDE.search(project.name):
            stats["claude_projects_excluded"] += 1
            continue
        stats["claude_projects_included"] += 1
        for path in sorted(project.glob("*.jsonl")):
            if path.name.startswith("test-session"):
                continue
            sid = path.stem
            sessions.extend(parse_claude_file(path, sid, None, seen_ids, stats))
            for sub in sorted((project / sid / "subagents").glob("*.jsonl")):
                sessions.extend(parse_claude_file(sub, sid + "/" + sub.stem, sid, seen_ids, stats))
    return sessions


# ------------------------------------------------------------------ Amplifier
_EVENT_RE = re.compile(r'"event":\s*"([^"]+)"')
_WANT = ("llm:response", "tool:pre", "tool:post", "session:fork", "session:start")


def _amp_result(result: Any) -> tuple[str, bool]:
    if not isinstance(result, dict):
        return (str(result) if result is not None else ""), False
    output, error = result.get("output"), result.get("error")
    failed = result.get("success") is False
    if isinstance(output, dict):
        rc = output.get("returncode", output.get("exit_code"))
        if isinstance(rc, int) and rc != 0:
            failed = True
        text = "\n".join(str(output.get(k) or "") for k in ("stdout", "stderr")) if (
            "stdout" in output or "stderr" in output) else json.dumps(output, default=str)
    else:
        text = output if isinstance(output, str) else json.dumps(output, default=str) if output is not None else ""
    if error:
        text = (text + "\n" + (error if isinstance(error, str) else json.dumps(error, default=str))).strip()
    return text, failed


def parse_amplifier_session(path: Path, sid: str, stats: Counter) -> Session | None:
    sess = Session("amplifier", sid)
    calls_by_id: dict[str, Call] = {}
    pending_compaction = False
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return None
    with handle:
        for line in handle:
            m = _EVENT_RE.search(line[:400])
            if not m:
                continue
            name = m.group(1)
            if "compact" in name and "fast_decisions" not in name:
                pending_compaction = True
                continue
            if name not in _WANT:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            data = e.get("data") or {}
            if sess.parent is None and isinstance(data.get("parent_id"), str):
                sess.parent = data["parent_id"]
            if name == "llm:response":
                usage = data.get("usage") or {}
                raw = ((data.get("raw") or {}).get("usage") or {}) if isinstance(data.get("raw"), dict) else {}
                cache_read = _int(usage.get("cache_read_tokens"))
                tokens = {"input": _int(raw.get("input_tokens")) if raw else max(0, _int(usage.get("input_tokens")) - cache_read),
                          "output": _int(usage.get("output_tokens")), "cache_read": cache_read,
                          "cache_write": _int(usage.get("cache_write_tokens"))}
                model = data.get("model")
                fallback = None
                try:
                    fallback = float(usage.get("cost_usd")) if usage.get("cost_usd") is not None else None
                except (TypeError, ValueError):
                    pass
                cost = price(model, tokens, fallback)
                if cost is None:
                    stats["amplifier_unpriced_calls"] += 1
                sess.steps.append(Step(model, tokens, cost, ts=e.get("ts"), compaction_before=pending_compaction))
                pending_compaction = False
            elif name == "tool:pre":
                if not sess.steps:
                    continue
                call = Call(str(data.get("tool_name")), data.get("tool_input"), data.get("tool_call_id"))
                sess.steps[-1].calls.append(call)
                if call.id:
                    calls_by_id[call.id] = call
                if call.tool in waste.AGENT_TOOLS:
                    sess.agent_launches += 1
            elif name == "tool:post":
                call = calls_by_id.get(data.get("tool_call_id"))
                if call is not None:
                    call.result, call.is_error = _amp_result(data.get("result"))
                    call.has_result = True
    return sess if sess.steps else None


def amplifier_sessions(root: Path, stats: Counter) -> list[Session]:
    out = []
    for project in sorted(root.iterdir()) if root.is_dir() else []:
        if not project.is_dir():
            continue
        if EXCLUDE.search(project.name) or not project.name.startswith("-"):
            # Non-path project names come from embedded hosts and test harnesses;
            # their working directory cannot be checked, so they are left out.
            stats["amplifier_projects_excluded"] += 1
            continue
        sessions_dir = project / "sessions"
        if not sessions_dir.is_dir():
            continue
        stats["amplifier_projects_included"] += 1
        for sdir in sorted(sessions_dir.iterdir()):
            events = sdir / "events.jsonl"
            if sdir.name.startswith("test-session") or not events.is_file():
                continue
            sess = parse_amplifier_session(events, sdir.name, stats)
            if sess:
                out.append(sess)
    return out


# ------------------------------------------------------------ classification
@dataclass
class Occ:
    category: str
    steps: int
    usd: float
    extra: dict = field(default_factory=dict)


def _share(step: Step) -> float:
    return (step.cost or 0.0) / max(1, len(step.calls))


def _overlap(a: tuple[int, int | None], b: tuple[int, int | None]) -> bool:
    a0, al = a
    b0, bl = b
    a1 = float("inf") if al is None else a0 + al
    b1 = float("inf") if bl is None else b0 + bl
    return a0 < b1 and b0 < a1


_CAT_FILE = re.compile(r"^(?:cd\s+\S+\s*&&\s*)?(cat|head|tail|sed\s+-n\s+\S+|bat|less)\s+(?:-[-\w]+\s+)*([^\s|;&<>]+)\s*$")


def classify(sess: Session, occs: list[Occ], dist: dict) -> None:
    epoch = 0
    cepoch = 0
    reads: dict[str, list] = defaultdict(list)       # path -> [(range, epoch, cepoch, step_i)]
    execs: dict[tuple, tuple] = {}                   # (kind,key) -> (epoch_after, cepoch, result_hash, failed, sig, step_i)
    fail_chain: dict[str, int] = {}
    dup_chain: dict[tuple, Occ] = {}
    poll: dict | None = None
    last_status: tuple | None = None

    def close_poll():
        nonlocal poll
        if poll and poll["n"] >= 2:
            usd = sum(poll["usd"][1:])
            occs.append(Occ("c_sleep_poll", poll["n"] - 1, usd, {"polls": poll["n"], "sleep_s": poll["sleep"]}))
            dist["poll_runs"].append(poll["n"])
            dist["poll_sleep_s"].extend(poll["sleeps"])
        poll = None

    for i, step in enumerate(sess.steps):
        if step.compaction_before:
            cepoch += 1
            reads.clear()
        share = _share(step)
        for call in step.calls:
            kind, key = waste.call_kind(call.tool, call.input)
            text = call.result or ""
            if call.is_error and waste.failure_kind(text):
                cat = "f_" + waste.failure_kind(text)
                call.category = cat
                occs.append(Occ(cat, 1, share))
                continue
            if kind == "bash" and key:
                check = waste.poll_check(key)
                status_repeat = (check is None and not call.is_error and waste.is_readonly_command(key)
                                 and waste.is_time_varying(key) and last_status is not None
                                 and last_status[0] == key and i - last_status[1] <= 2)
                if check is not None or status_repeat:
                    ck = check if check is not None else key
                    if poll and (poll["check"] == ck or not ck or not poll["check"]) and i - poll["last"] <= 2:
                        poll["n"] += 1
                        poll["usd"].append(share)
                        poll["last"] = i
                        poll["check"] = poll["check"] or ck
                        poll["sleeps"].append(waste.sleep_seconds(key))
                        call.category = "c_sleep_poll"
                    else:
                        close_poll()
                        poll = {"check": ck, "n": 1, "usd": [share], "last": i,
                                "sleep": waste.sleep_seconds(key), "sleeps": [waste.sleep_seconds(key)]}
                        if status_repeat:  # the earlier identical status check opened this run
                            poll["n"] = 2
                            call.category = "c_sleep_poll"
                    last_status = (key, i)
                    continue
                if waste.is_readonly_command(key) and waste.is_time_varying(key):
                    last_status = (key, i)
                sig = waste.error_signature(text) if call.is_error else None
                prev = execs.get(("bash", key))
                rhash = waste.text_hash(text)
                if call.is_error:
                    if prev and prev[3] and prev[4] == sig and prev[0] == epoch and prev[1] == cepoch:
                        fail_chain[key] = fail_chain.get(key, 1) + 1
                        call.category = "b_error_retry"
                        occs.append(Occ("b_error_retry", 1, share, {"chain_key": (sess.sid, key)}))
                    else:
                        if fail_chain.get(key, 1) >= 2:
                            dist["fail_chains"].append(fail_chain[key])
                        fail_chain[key] = 1
                elif prev and not prev[3] and prev[0] == epoch and prev[1] == cepoch and prev[2] == rhash:
                    call.category = "a_identical_repeat"
                    ck = ("bash", key)
                    occ = dup_chain.get(ck)
                    if occ and occ.extra.get("last_epoch") == epoch:
                        occ.steps += 1
                        occ.usd += share
                    else:
                        occ = Occ("a_identical_repeat", 1, share, {"last_epoch": epoch, "tokens": len(text) / CHARS_PER_TOKEN})
                        occs.append(occ)
                        dup_chain[ck] = occ
                    dist["dup_result_tokens"].append(len(text) / CHARS_PER_TOKEN)
                else:
                    m = _CAT_FILE.match(key)
                    if m and waste.is_readonly_command(key):
                        target = m.group(2)
                        hit = [p for p in reads if p == target or p.endswith("/" + target.lstrip("./"))]
                        if hit and any(r[1] == epoch for p in hit for r in reads[p]):
                            call.category = "d_reread_in_context"
                            occs.append(Occ("d_reread_in_context", 1, share))
                ro = waste.is_readonly_command(key)
                if not ro:
                    epoch += 1
                execs[("bash", key)] = (epoch, cepoch, rhash, call.is_error, sig, i)
                if not call.is_error and fail_chain.get(key, 1) >= 2:
                    dist["fail_chains"].append(fail_chain.pop(key))
                continue
            if kind == "read" and key:
                rng = waste.read_range(call.input)
                prior = [r for r in reads.get(key, []) if r[1] == epoch and r[2] == cepoch]
                if prior and not call.is_error:
                    exact = any(r[0] == rng for r in prior)
                    if exact or any(_overlap(r[0], rng) for r in prior):
                        cat = "a_identical_repeat" if exact else "d_reread_in_context"
                        call.category = cat
                        occs.append(Occ(cat, 1, share, {"tokens": len(text) / CHARS_PER_TOKEN}))
                        if exact:
                            dist["dup_result_tokens"].append(len(text) / CHARS_PER_TOKEN)
                if not call.is_error:
                    reads[key].append((rng, epoch, cepoch, i))
                continue
            if kind == "search" and key:
                prev = execs.get(("search", key))
                rhash = waste.text_hash(text)
                if prev and prev[0] == epoch and prev[1] == cepoch and prev[2] == rhash and not call.is_error:
                    call.category = "a_identical_repeat"
                    occs.append(Occ("a_identical_repeat", 1, share, {"tokens": len(text) / CHARS_PER_TOKEN}))
                execs[("search", key)] = (epoch, cepoch, rhash, call.is_error, None, i)
                continue
            if kind == "edit":
                epoch += 1
                continue
            if kind in ("agent", "other"):
                epoch += 1
        if poll and i - poll["last"] > 2:
            close_poll()
    close_poll()
    for key, n in fail_chain.items():
        if n >= 2:
            dist["fail_chains"].append(n)

    # g: sequential single read-only calls that did not depend on each other.
    run = 0
    for i in range(1, len(sess.steps)):
        a, b = sess.steps[i - 1], sess.steps[i]
        if len(a.calls) == 1 and len(b.calls) == 1 and _single_ro(a.calls[0]) and _single_ro(b.calls[0]) \
                and not b.compaction_before \
                and not waste.mentions(a.calls[0].result, b.calls[0].tool, b.calls[0].input):
            run += 1
            b.calls[0].category = "g_batchable_reads"
            occs.append(Occ("g_batchable_reads", 1, max(0.0, (b.cost or 0.0) - b.write_cost()),
                            {"run_id": (sess.sid, i - run)}))
        else:
            run = 0

    # x: oversized tool outputs carried through the following steps.
    cep_steps = []
    c = 0
    for s in sess.steps:
        c += 1 if s.compaction_before else 0
        cep_steps.append(c)
    for i, step in enumerate(sess.steps):
        for call in step.calls:
            if len(call.result or "") > BIG_OUTPUT_CHARS:
                excess = (len(call.result) - BIG_OUTPUT_KEEP_CHARS) / CHARS_PER_TOKEN
                carried = [s for j, s in enumerate(sess.steps[i + 1:], start=i + 1) if cep_steps[j] == cep_steps[i]]
                usd = 0.0
                for k, s in enumerate(carried):
                    rates = _rates_for(s.model, RATES)
                    if not rates:
                        continue
                    usd += excess * (rates[3] if k == 0 else rates[2]) / 1e6
                occs.append(Occ("x_oversized_tool_output", 0, usd, {"carried_steps": len(carried)}))


def _single_ro(call: Call) -> bool:
    if call.is_error or call.category:
        return False
    kind, key = waste.call_kind(call.tool, call.input)
    if kind in ("read", "search"):
        return True
    return kind == "bash" and bool(key) and waste.is_readonly_command(key) and waste.poll_check(key) is None


def cache_rewrites(sessions: list[Session], occs: list[Occ]) -> None:
    """y: cache writes beyond the prompt's growth since the previous call on
    the same model in the same session (the cached prefix was not reused),
    priced as the write-minus-read difference. After a gap over 5 minutes
    the cache expired: while a tool or helper ran (the previous call issued
    tool calls; the keep-alive lever) or while the user was away (the
    previous call ended the turn). Within 5 minutes the prefix itself changed
    between calls (churn). Upper bounds; not added to
    the waste total."""
    from datetime import datetime

    def ts(x):
        try:
            return datetime.fromisoformat(str(x).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    for sess in sessions:
        prev = None
        for st in sess.steps:
            rates = _rates_for(st.model, RATES)
            if rates and prev is not None and prev.model == st.model:
                excess = st.tokens.get("cache_write", 0) - max(0, st.prompt_tokens() - prev.prompt_tokens())
                if excess > 5000:
                    a, b = ts(prev.ts), ts(st.ts)
                    idle = a is not None and b is not None and b - a > 300
                    cat = ("y_cache_rebuild_after_tool_wait" if prev.calls else "y_cache_rebuild_after_user_idle") \
                        if idle else "y_cache_rewrite_churn"
                    occs.append(Occ(cat, 1, excess * (rates[3] - rates[2]) / 1e6))
            prev = st


def helper_waste(sessions: list[Session], occs: list[Occ]) -> dict:
    by_id = {s.sid: s for s in sessions}
    stats = Counter()
    for s in sessions:
        if not s.parent:
            continue
        parent = by_id.get(s.parent)
        stats["helper_sessions"] += 1
        stats["helper_cost"] += s.cost
        n_calls = sum(len(st.calls) for st in s.steps)
        edits = any(waste.call_kind(c.tool, c.input)[0] == "edit" for st in s.steps for c in st.calls)
        if len(s.steps) <= HELPER_TRIVIAL_MAX_STEPS and not edits:
            pcosts = [st.cost for st in parent.steps if st.cost] if parent else []
            pmed = statistics.median(pcosts) if pcosts else 0.0
            usd = max(0.0, s.cost - n_calls * pmed)
            stats["trivial_helpers"] += 1
            stats["trivial_helper_gross_cost"] += s.cost
            occs.append(Occ("e_trivial_helper", len(s.steps), usd))
        over = [st for st in s.steps if st.prompt_tokens() > HELPER_OVERSIZE_TOKENS]
        if over:
            usd = 0.0
            for st in over:
                rates = _rates_for(st.model, RATES)
                if rates:
                    usd += (st.prompt_tokens() - HELPER_OVERSIZE_TOKENS) * rates[2] / 1e6
            occs.append(Occ("h_oversized_helper_context", len(over), usd))
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in stats.items()}


def fast_decisions_summary(events_dir: Path) -> dict:
    reasons, receipts, files, test_files = Counter(), 0, 0, 0
    for path in sorted(events_dir.glob("*.jsonl")) if events_dir.is_dir() else []:
        if path.name.startswith("test-session"):
            test_files += 1
            continue
        files += 1
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if '"fast_decisions:model_routed"' in line or '"fast_decisions:difficulty_judged"' in line:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                code = (e.get("data") or {}).get("reason_code")
                if code:
                    reasons[e["event"].split(":")[1] + ":" + code] += 1
            elif '"fast_decisions:efficiency"' in line:
                receipts += 1
    return {"session_files": files, "test_session_files_skipped": test_files, "reason_codes": dict(reasons.most_common(20)),
            "efficiency_receipts": receipts}


def summarize(sessions: list[Session], occs: list[Occ], dist: dict, helper: dict) -> dict:
    total = sum(s.cost for s in sessions)
    steps = sum(len(s.steps) for s in sessions)
    by_h = defaultdict(lambda: {"sessions": 0, "helper_sessions": 0, "model_calls": 0, "usd": 0.0})
    tss = []
    for s in sessions:
        h = by_h[s.harness]
        h["sessions"] += 1
        h["helper_sessions"] += 1 if s.parent else 0
        h["model_calls"] += len(s.steps)
        h["usd"] += s.cost
        tss.extend(st.ts for st in (s.steps[0], s.steps[-1]) if st.ts)
    cats = {}
    for cat in CATEGORIES:
        rows = [o for o in occs if o.category == cat]
        if cat in ("b_error_retry",):
            # group retries into chains (one occurrence per failing-command chain)
            chains = defaultdict(lambda: [0, 0.0])
            for o in rows:
                chains[o.extra["chain_key"]][0] += 1
                chains[o.extra["chain_key"]][1] += o.usd
            groups = [(n, usd) for n, usd in chains.values()]
        elif cat == "g_batchable_reads":
            runs = defaultdict(lambda: [0, 0.0])
            for o in rows:
                runs[o.extra["run_id"]][0] += 1
                runs[o.extra["run_id"]][1] += o.usd
            groups = list(runs.values())
        else:
            groups = [(o.steps, o.usd) for o in rows]
        usd = sum(u for _, u in groups)
        cats[cat] = {
            "occurrences": len(groups),
            "wasted_model_steps": sum(n for n, _ in groups),
            "usd": round(usd, 2),
            "pct_of_total_spend": round(100 * usd / total, 2) if total else 0.0,
            "median_steps_per_occurrence": statistics.median([n for n, _ in groups]) if groups else 0,
        }
    fc = dist["fail_chains"]
    pr = dist["poll_runs"]
    unpriced = sum(1 for s in sessions for st in s.steps if st.cost is None)
    return {
        "window": {"first": min(tss) if tss else None, "last": max(tss) if tss else None},
        "unpriced_model_calls": unpriced,
        "totals": {"sessions": len(sessions), "model_calls": steps, "usd": round(total, 2),
                   "by_harness": {k: dict(v, usd=round(v["usd"], 2)) for k, v in by_h.items()},
                   "helpers": helper},
        "categories": cats,
        "deterministic_waste_total_a_to_g": {
            "usd": round(sum(c["usd"] for k, c in cats.items() if not k.startswith(("x_", "h_", "y_"))), 2),
            "pct": round(sum(c["pct_of_total_spend"] for k, c in cats.items()
                             if not k.startswith(("x_", "h_", "y_"))), 2)},
        "distributions": {
            "error_retry_chain_attempts": _dist(fc),
            "error_retry_extra_attempts_after_2_failures": _dist([n - 2 for n in fc if n >= 3]),
            "poll_run_polls": _dist(pr),
            "poll_extra_polls_after_2": _dist([n - 2 for n in pr if n >= 3]),
            "poll_sleep_seconds": _dist(dist["poll_sleep_s"]),
            "identical_repeat_result_tokens": _dist(dist["dup_result_tokens"]),
            "identical_repeat_chain_repeats": _dist([o.steps for o in occs if o.category == "a_identical_repeat"]),
            "identical_repeat_extra_after_2": _dist([o.steps - 2 for o in occs
                                                     if o.category == "a_identical_repeat" and o.steps >= 2]),
            "identical_repeat_extra_after_3": _dist([o.steps - 3 for o in occs
                                                     if o.category == "a_identical_repeat" and o.steps >= 3]),
        },
        "notes": [
            "Each wasted tool call is priced at its issuing model step's list-price cost split over the calls that step issued.",
            "g is priced without the step's cache writes; x, h and y are upper bounds (output capping, helper context right-sizing, cache reuse) and are not added to the deterministic waste total.",
            "Detection is deterministic and conservative: any write, non-read-only command or helper launch resets the no-state-change conditions.",
        ],
    }


def _days_before(iso: str, days: int) -> str | None:
    from datetime import datetime, timedelta
    try:
        end = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (end - timedelta(days=days)).strftime("%Y-%m-%d")


def run_window(sessions: list[Session], since: str | None) -> dict:
    """Classify and summarize the steps at or after ``since`` (all when None)."""
    trimmed = []
    for s in sessions:
        steps = [st for st in s.steps if since is None or (st.ts or "") >= since]
        if steps:
            t = Session(s.harness, s.sid, s.parent, steps, s.agent_launches)
            for st in steps:
                for c in st.calls:
                    c.category = None
            trimmed.append(t)
    occs: list[Occ] = []
    dist = {"fail_chains": [], "poll_runs": [], "poll_sleep_s": [], "dup_result_tokens": []}
    for s in trimmed:
        classify(s, occs, dist)
    helper = helper_waste(trimmed, occs)
    cache_rewrites(trimmed, occs)
    return summarize(trimmed, occs, dist, helper)


def _dist(values: list) -> dict:
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0}
    s = sorted(values)
    q = lambda p: s[min(len(s) - 1, int(p * len(s)))]
    return {"n": len(s), "mean": round(statistics.fmean(s), 3), "median": statistics.median(s), "p75": q(0.75),
            "p90": q(0.9), "max": s[-1]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--claude-root", default="~/.claude/projects")
    ap.add_argument("--amplifier-root", default="~/.amplifier/projects")
    ap.add_argument("--events-dir", default="~/.amplifier/fast-decisions/events")
    ap.add_argument("--since", default=None, help="ISO date for the recent window (default: 30 days before the last step)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    stats: Counter = Counter()
    sessions = claude_sessions(Path(args.claude_root).expanduser(), stats)
    sessions += amplifier_sessions(Path(args.amplifier_root).expanduser(), stats)
    last = max((st.ts or "" for s in sessions for st in s.steps), default="")
    recent_start = args.since or _days_before(last, 30)
    windows = {}
    for name, start in (("last_30_days", recent_start), ("all_history", None)):
        windows[name] = run_window(sessions, start)
        windows[name]["since"] = start
    summary = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "primary_window": "last_30_days",
        "prices_per_mtok": {k: dict(zip(("input", "output", "cache_read", "cache_write"), v))
                            for k, v in RATES.items()},
        "exclusions": "project dirs or session cwd matching " + EXCLUDE.pattern + "; test-session-* files; "
                      "Amplifier projects without a path-derived name; duplicate Claude Code message ids",
        "windows": windows,
        "fast_decisions_events": fast_decisions_summary(Path(args.events_dir).expanduser()),
        "parse_stats": dict(stats),
    }
    text = json.dumps(summary, indent=2, default=str)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
