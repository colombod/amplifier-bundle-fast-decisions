# Architecture and decisions

## Execution boundary

```text
Amplifier Rust-backed session / coordinator
    -> HybridOrchestrator.execute(...)
       -> StreamingOrchestrator.execute(...)  [unchanged implementation]
          -> RoutedProvider.complete(actual_chat_request, **actual_kwargs)
             -> deterministic eligibility/budget checks
             -> one bounded Jev choice over prepared candidates + reason
             -> shadow/threshold/state/revision checks
             -> fast: real ChatResponse + ToolCall envelope
             -> slow: original_provider.complete(same_request, **same_kwargs)
          -> upstream tool hooks and approvals
          -> ObservedTool.execute -> original_tool.execute
          -> upstream context bookkeeping / next iteration
```

The provider and tool mappings are copied per turn. No monkeypatching, global mounts, provider key changes, or kernel modifications are used. Upstream provider selection, model overrides and provider pins remain its responsibility; the facade does not choose another provider. Full compatibility with those upstream behaviors is a required host integration test, not inferred solely from this design.

The facade supplies only prepared arguments from trusted candidates, never arguments parsed from Jev-generated text. A synthetic response is recognizable by object identity inside the facade and parsed as its typed tool calls; native provider responses retain their original identity and parser.

The upstream loop receives tool requests in its ordinary format. A submission can still be denied or modified by native hooks. We measure actual execution in the tool facade, not in `tool:pre` because that event alone does not prove that execution occurred.

## Session-local service

Capabilities registered during mounting:

- `fast_decisions.service`: the `DecisionService` instance.
- `fast_decisions.runtime`: a `Runtime` holding that service, recorder, lifecycle and per-session execution lock.

The hook bridge reuses the existing runtime when the orchestrator already mounted. Cleanup is idempotent. Each executed turn resets its candidate-use set, fast counters and decision ID. A session-local circuit breaker can survive between turns for five seconds after a backend error.

The service's `choose(request, tools)` is orchestration-sensitive: it requires an active turn and serial use. It is not a stateless, concurrent general-purpose RPC API. Consumers should contribute prepared candidates (and questions) rather than call it from parallel hooks. The independently reusable backend interface is `ask(request: DecisionRequest) -> DecisionResult`: one batched call carrying the action candidates plus every contributed judgment question, scored independently by the vendor in a single request.

## Shadow measurement (the hook, not the orchestrator)

`hooks-fast-decisions` composes onto **any** orchestrator, including the untouched upstream loop, to measure "what would we have chosen" without ever touching the turn. This is the shipped default (`bundle.md`, `behaviors/fast-decisions.yaml`, config `mode: shadow`): no orchestrator swap is required to get shadow telemetry.

```text
provider:request  -> snapshot (context mount, candidates, hashing) -- ON the critical path, hard-bounded
                   -> enqueue ShadowJob onto Runtime's shadow worker -- OFF the critical path
tool:pre          -> resolve the pending job into a ShadowOutcome; emit shadow_observed
worker (background) -> score the job against the backend; emit shadow_proposed, then shadow_agreement
execution:end     -> sweep any proposal that never saw a matching tool:pre this turn
```

The snapshot reads the mounted context manager (`coordinator.get("context")`), not the request object -- `provider:request` carries no request, so this is the only available seam. That view is close to, but not identical to, what the orchestrator actually dispatches (injections/compaction apply later); every shadow record carries `state_source` so an eval never silently mixes the two. The snapshot is bounded by `shadow_max_messages` (messages read), `max_state_chars` (total snapshot size) and `shadow_snapshot_budget_ms` (wall clock, enforced with `asyncio.timeout`); exceeding any of them abandons the snapshot, counts `shadow_snapshot_budget_exceeded`, and returns `continue` -- never an exception into the hook chain.

Policy ownership: the orchestrator, when present, is the sole owner of `Policy` (mode, thresholds, allowed tools) via `get_runtime(..., owner=True)`. The hook never requests ownership and cannot make `mode: active` stick (it downgrades to `shadow` and emits `fallback` with `hook_cannot_own_active` if its own config asks for it). Mount order across module types is a documented, guaranteed kernel contract (orchestrator mounts before hooks within a session), so no race exists between the two mounting the shared `Runtime` -- the owner re-apply in `get_runtime` is defensive, covering only out-of-session construction (unit tests, `afast demo`, a future non-kernel host).

The shadow worker is a single background `asyncio.Task` owned by `Runtime`: created lazily (on first submit, so construction outside a running event loop is safe), drained with a bounded budget (`shadow_drain_ms`, default 2s) and then cancelled in `Runtime.close()`, so no shadow work outlives the session and no task is left pending at interpreter exit.

`bundles/shadow.yaml` is retained for one release cycle as a **deprecated forwarding bundle**: it composes `bundle.md` unchanged and no longer swaps `session.orchestrator`. `bundles/active.yaml` remains the only standalone bundle that swaps the orchestrator, because that is a root-bundle concern.

## State and candidates

The snapshot uses at most the latest 12 messages, bounded per-message and total size. It excludes system/developer text and private thinking blocks. It is not a rolling external memory cache. The actual request fingerprint includes messages/tools/model; no raw request is recorded in telemetry.

Candidate sources are trusted config, the `fast_decisions.candidates` capability callback, the native contribution channel of the same name, and explicitly mentioned workspace text files. Candidate arguments are copied. Candidates are collected as groups, one per contributor; a group with an internal or cross-group conflicting ID is dropped in full and counted (`contribution_conflict`) -- it cannot remove another contributor's candidates. The surviving set is serialised in canonical `(origin, id)` order; a `candidate_order_hash` digest of that order is computed once per request and appears on `requested`/`scored`. Tools must be present both in the request's advertised tools and the actual mounted tools and also appear in the policy allowlist.

Judgment questions (`fast_decisions.questions`, new) follow the identical per-contributor rejection model, bounded by `max_questions` (default 8) with `choice` questions further bounded to 2..255 criteria. Both channels are validated and truncated inside the same shared decision deadline, and both feed the single batched `ask()` call -- one backend request per state regardless of question count.

The model-role router (P4) is a second, independent consumer of the same shadow measurement path: at `delegate` calls it proposes a `model_role`, records it against what the delegate actually resolved, and never mutates the call (the upstream loop does not honor `modify` at `tool:pre`, so an active router through this seam is not just undesired but structurally impossible today). It ships on by default (`role_router: true` in `behaviors/fast-decisions.yaml`; the library-level default when unconfigured is `false`), reads `model_role_resolver` read-only to enumerate live roles once per session, and never reads or writes `conversation.provider_pin`.

Non-workspace tools additionally need `fast_decisions.validate_candidate`. The bundled workspace tool validates containment, excluded names, permitted operations, file revisions and size. File and conversation state are rechecked after inference. Neither eligibility nor confidence is an approval token.

### Completed-read ledger (HC02a)

Each turn carries a small ledger (`TurnState.completed_reads`, normalized path -> revision) of what has already been read this turn. It is fed from two places: a fast-submitted `fast_workspace` read/list candidate (`orchestrator.RoutedProvider.complete`), and any *successful* `fast_workspace` or native `read_file` execution actually reached (`orchestrator.ObservedTool.execute`) -- including provider-selected reads the model made on its own, not just fast-routed ones. A denied or failed call is never recorded: `ObservedTool` only records after `execute()` returns without raising and `success is not False`, and a denied call never reaches `execute()` at all.

Identity is `(normalized path, revision)`, computed once by `workspace.WorkspaceTool.read_identity` and reused everywhere -- candidate construction, eligibility, and ledger recording all call the same function, so a changed file (new revision) is eligible again immediately. The ledger is strictly per-turn: it lives on `TurnState` and is discarded with the rest of that state when the turn ends, exactly like `used`.

At candidate-eligibility time (`DecisionService.choose`), a `fast_workspace` read/list candidate whose identity is already in the ledger is dropped and counted (`candidates_suppressed_already_read`), never sent to the backend for scoring. When every surviving candidate is suppressed this way, the route goes slow with `reason_code: already_read_unchanged` -- still with no backend call, the same cheap path as `no_eligible_candidates`. Gated by the `suppress_completed_reads` policy flag (default `True`); when `False` the ledger is neither fed nor consulted and behavior is unchanged from before HC02a.

## Phase-specific effort routing (HC03, opt-in)

`Policy.effort_routing` (default `None`) lets a host lower generative
effort during exploration on the SAME model/provider pin, entirely inside
`RoutedProvider.complete` -- the one seam this module owns before the
upstream provider call. When it is `None`/empty, this feature is inert:
no attribute is read from or written to the request and no
`effort_routed` event is emitted.

`effort.classify_phase(request)` reads `request.messages` and classifies
only the CURRENT turn (messages after the last `user` message):

- `orient` -- no assistant message yet in the turn (its first request).
- `explore` -- an assistant message exists, no write-like tool call has
  occurred yet this turn, and the most recent assistant message's tool
  calls (if any) are all read-like (`read_file`, `fast_workspace`, `glob`,
  `grep`, `list_dir`, `ls`, `search`, `todo`). Any tool name outside that
  allowlist is treated as write-like -- deliberately conservative.
- `implement` -- a write-like tool call has occurred anywhere in the turn.
  This also covers "verify" requests (e.g. running tests via `bash`),
  which are write-like and therefore receive the same default-effort
  treatment as any other implementation step.

When the current request classifies as `explore`, `effort.decide_effort`
applies `effort_routing["explore"]` to `request.reasoning_effort` unless:
the host already pinned an explicit `request.reasoning_effort`
(`reason_code: host_pinned`, always wins), this turn has already seen
`effort_routing["escalate_after_provider_errors"]` upstream provider
exceptions (`escalated_after_error`), or this turn's explore-phase request
count has exceeded `effort_routing["max_explore_requests"]`
(`escalated_max_explore`). The model pin, tool approvals, and every other
policy dimension are untouched -- only `request.reasoning_effort` is ever
set, via `setattr` (or item assignment for a dict-shaped request), never
`request.model`.

`TurnState` tracks `explore_requests`, `effort_routed_requests`, and
`provider_errors_seen` per turn, reset alongside the rest of `TurnState` at
turn start. `Policy.__post_init__` validates `effort_routing` at
construction (i.e. at mount, via `Policy.from_config`): an unrecognized
`explore` effort string or a non-positive `max_explore_requests` /
`escalate_after_provider_errors` raises `ValueError` immediately -- never
silently ignored. Receipts (the native `llm:request` event on the host
side) carry the requested effort; the upstream Anthropic/OpenAI providers
read `request.reasoning_effort` before falling back to their own
provider-level config default, so a lowered `explore` effort applies to
that one request only.

## Model routing with escalation (HC04, opt-in)

`Policy.model_routing` (default `None`) lets a host start a turn on a
cheaper/faster `start_model` (and optionally a starting
`start_effort`), entirely inside `RoutedProvider.complete` -- the same
seam HC03 (`effort_routing`, above) owns -- and escalate to the host's
normal provider-configured default the moment risk appears. When it is
`None`, this feature is inert: no attribute is read from or written to
the request, `kwargs` is not touched, and no `model_routed` event is
emitted.

**What is routed.** For each slow (non-fast-path) request, while
`turn.escalated` is `False`: an explicit host-set `request.model` is
always respected (`reason_code: host_pinned`) unless
`model_routing["override_explicit_model"]` is `true`. Otherwise
`request.model` is set to `start_model`, and (verified against the
installed Anthropic provider, which reads the effective model from
`kwargs.get("model", self.default_model)` and never from
`request.model`) `kwargs["model"]` is set too, so the routed model is
the one actually served, not just a label on the request object the
real provider ignores. If `start_effort` is configured, it is applied
to `request.reasoning_effort` only when nothing has already set it
this request -- neither this same call's HC03 `effort_routing` nor a
host pin -- so HC04 never clobbers a decision HC03 or the host already
made.

**Escalation triggers (any one flips `turn.escalated` permanently for
the rest of the turn):**

- `max_requests_before_escalation`: this turn's slow-request count
  (`turn.slow_requests_seen`, incremented once per slow request while
  model_routing is enabled) exceeds the configured value
  (`escalated_max_requests`).
- `escalate_on_test_failure`: `ObservedTool.execute` observed a
  *successful* execution (no exception raised) of a test-shaped tool
  (`bash`, `python_check`, `run_tests`, or any tool whose name contains
  `"test"`) whose own result text matches a failure signature (`FAILED
  (`, `FAIL:`, a Python traceback header, an `Error:` line, or an `N
  failed` count) -- `turn.test_failure_seen` latches, and the *next*
  slow request escalates (`escalated_test_failure`).
- `escalate_on_provider_error`: the upstream provider call raised (the
  existing `except Exception` branch already incrementing
  `turn.provider_errors_seen`) -- escalates immediately for the
  *following* request (`escalated_provider_error`); the failed request
  itself already got its `model_routed` event before the exception.

Once escalated, `request.model`/`kwargs["model"]` and
`request.reasoning_effort` are left untouched by this feature -- the
provider's own configured default takes over, exactly as if
`model_routing` had never been set.

**Never bypasses approvals or the loop.** Only `request.model`,
`kwargs["model"]`, and (conditionally) `request.reasoning_effort` are
ever touched; tool approvals, `stream()`, and every other policy
dimension are untouched. `Policy.__post_init__` validates
`model_routing` at construction (i.e. at mount, via `Policy.from_config`):
unlike `effort_routing`, an empty dict is *not* a valid off-state --
`start_model` is required whenever a dict is supplied at all, because a
model pin with no starting model is meaningless. `TurnState` tracks
`slow_requests_seen`, `model_routed_requests`, `test_failure_seen`,
`escalated`, and `escalation_reason` per turn, reset alongside the rest
of `TurnState` at turn start.

Receipts: the `fast_decisions:model_routed` event proves what this
feature *requested* (`requested_model`, `requested_effort`,
`reason_code`, `escalated`); the native `llm:request` receipt on the
host side shows what the provider actually served, since `kwargs["model"]`
(not just `request.model`) carries the override into the real
`complete()` call.

## Judge-driven escalation and phase classification (HC05, opt-in)

Two independent opt-ins let the CONFIGURED `DecisionBackend` -- the same
judge used for the read-shortcut -- answer a bounded judgment question
instead of (or alongside) the deterministic HC03/HC04 rules. Both are ask
a single Choice `Question` via `orchestrator._ask_judge_choice`, which
builds one `DecisionRequest` (`candidates=()`, one contributed `Question`)
and calls `service.backend.ask()` directly -- never `DecisionService.choose`,
since there is no prepared action to submit here, only a judgment. This
reuses the read-shortcut's own contract and constraints: `Policy.timeout_ms`
bounds the call, and `backend.external and not Policy.allow_external_state`
blocks it before any external call is attempted (Jev refuses without
consent, exactly as for a read candidate) -- the caller sees this as an
ordinary abstain and falls back to its deterministic rule.

**Compact judge state (`orchestrator._judge_state`).** Both mechanisms send
the same small, bounded, JSON-able state: a 300-char head of the first user
message (`task_prompt_head`), the phase, this turn's slow-request count
(`slow_requests_seen`), the tool names used so far this turn
(`tool_names_used`), a 600-char excerpt of the last tool result
(`last_tool_result_excerpt`), and the two HC04 failure signals
(`test_failure_seen`, `provider_errors_seen`). If the canonical
serialization would still exceed `Policy.max_state_chars`, the excerpt is
dropped first, then the prompt head -- never the full conversation, tool
arguments, or model output. `tool_names_used`/`last_tool_result_text` are
fed onto `TurnState` by `ObservedTool.execute`, but ONLY while a judge
mechanism is actually configured (`_judge_context_needed`) -- inert
otherwise, matching every other HC0x seam.

**Escalation judge (`model_routing.escalation_judge`).** `"rules"` (the
default) is today's HC04 behavior unchanged. `"judge"` asks the judge a
two-criteria Choice question (`continue_cheap` / `escalate`) before every
slow request past the turn's first, while `turn.escalated` is still
`False` -- but ONLY once neither deterministic trigger (`test_failure`,
`max_requests`) has already fired for this same request: those triggers
remain a floor, escalating regardless of what the judge would have said.
The judge escalates when its answer's `choice == "escalate"` AND its
probability for that choice is at least `model_routing.escalate_min_probability`
(default `0.7`); anything else (an explicit `continue_cheap`, a
below-threshold `escalate`, or a `None` choice from an abstain/blocked/error
answer) leaves the request to the deterministic rules for that request only
(`decided: "fallback_rules"` or `"continue"`). A judge-caused escalation
sets `turn.escalation_reason = "judge"` (`reason_code:
escalated_judge` on the next `model_routed`), latches exactly like every
other HC04 trigger, and increments `TurnState.escalations_by_judge`;
`TurnState.escalation_judgements` counts every time the judge was actually
asked, regardless of its answer. The `fast_decisions:escalation_judged`
event is emitted once per ask, immediately before that request's
`model_routed` event, and always precedes it in decision order.

**Phase judge (`effort_routing.phase_judge`).** When `true`, the
deterministic `effort.classify_phase(request)` result is still computed
first (it is always the fallback and the comparison baseline), then the
judge is asked a three-criteria Choice question (`orient` / `explore` /
`implement`, worded from `effort.PHASE_CRITERIA`). A non-null answer
REPLACES the phase used for the rest of this request's effort routing
(`effort.decide_effort`) and, if `model_routing` is also configured, the
`phase` recorded on that same request's `model_routed`/`escalation_judged`
events. An abstain/blocked/error answer (`choice is None`) leaves the
deterministic phase in place -- there is no separate "escalation" concept
for phase classification, only override-or-fall-back. The
`fast_decisions:phase_judged` event carries `agreed_with_rules` (whether the
judge's choice matched the deterministic phase, or `null` when the judge
abstained) so a benchmark can measure judge/rules agreement independent of
which one "won".

**Never bypasses approvals, the loop, or HC03/HC04's own contracts.** Both
mechanisms are pure decision inputs: they change which phase or escalation
state HC03/HC04 act on, never `request.model`/`request.reasoning_effort`
directly, never tool approvals, and never `stream()`. `Policy.__post_init__`
validates `model_routing.escalation_judge` (`"rules"` / `"judge"`),
`model_routing.escalate_min_probability` (a number in `[0, 1]`), and
`effort_routing.phase_judge` (a bool) at construction, alongside the
existing HC03/HC04 validation.

## One call per decision point (HC08, opt-in)

`Policy.decision_batching` (default `False`) lets `RoutedProvider.complete`
combine HC05's two judge-driven asks -- the phase judge
(`effort_routing.phase_judge`) and the escalation judge
(`model_routing.escalation_judge == "judge"`) -- into ONE `ask_many()`
backend call instead of two separate `ask()` calls, whenever BOTH are due
for the same request. `_escalation_judge_due` mirrors the escalation
elif-chain (deterministic triggers first, then the judge) without
mutating turn state, so it is the single source of truth for "is the
escalation judge due" consulted both by the batching pre-check and the
unbatched sequential path -- the two paths cannot silently diverge in
when they ask.

**Stake:** whether a decision point is worth batching scales with how
much state/latency it would otherwise cost to ask twice. Batching is a
no-op (falls through to the identical pre-HC08 sequential path) whenever
fewer than two judge mechanisms are configured, or only one is due on
this particular request (e.g. the escalation judge is never due before
the turn's first slow request).

**The backend interface.** `DecisionBackend.ask_many(request)` asks every
`Question` in one `DecisionRequest` in a single logical call.
`backends.ask_many(backend, request)` is the call site every consumer
uses: if the backend defines its own `ask_many` (Jev, and the in-repo
`ScriptedBackend` test double -- both already answer every question in
`request.questions` in ONE wire call via their own `ask()`), that is
called directly. Otherwise a generic mixin runs one single-question
`ask()` per question concurrently (`asyncio.gather`) and merges the
answers into one `DecisionResult`. This means ollama/mlx/hosted/laya
backends get HC08's batching capability at the call site with zero
changes to their own modules -- they simply never define `ask_many`.

**Fallback.** If the combined call raises (backend error, timeout) the
request falls back to the exact pre-HC08 sequential path for that
request -- both judges are asked separately, exactly as if batching were
off -- and `TurnState.batch_fallbacks` is incremented. A policy-blocked
call (`backend.external and not allow_external_state`) is NOT a fallback:
it returns every question as an abstain, identically to what the
sequential path's own `_ask_judge_choice` would already do for the same
gate, so the two paths agree even when blocked.

**Receipts.** `fast_decisions:decided_batch` is emitted once per batched
call, carrying `backend`, `question_ids`, `n_questions`, `duration_ms`,
and `mode`. `phase_judged`/`escalation_judged` are still emitted exactly
as before (same shape, same thresholds) whether the answer came from a
batched or sequential ask -- batching changes how many backend calls are
made, never what is asked or how the answer is applied.

## Stake-scaled confidence gates (HC09, opt-in)

`Policy.confidence_gates` (default `None`) is a per-judged-decision
probability floor for the three points a `DecisionBackend` judgment can
gate: `read_shortcut` (the fast-path action choice in
`DecisionService.choose`), `phase`, and `escalation` (HC05's two judge
mechanisms). Below the gate, the judged decision is deliberately a
"do-nothing" middle band: `read_shortcut` lets the model run instead of
submitting the prepared action (the existing `selection_threshold` slow
route), `phase` keeps the deterministic classification, and `escalation`
stays on the deterministic rules.

**Legacy aliases, byte-identical by default.** `contracts.effective_gate(policy,
kind)` is the single resolver every call site uses. A kind explicitly
present in `confidence_gates` always wins. A kind absent from it -- or
the whole policy omitting `confidence_gates` -- falls back to its
pre-HC09 source unchanged: `Policy.min_probability` for `read_shortcut`
(the same threshold `DecisionService.choose` already enforced),
`model_routing.escalate_min_probability` (default `0.7`) for
`escalation`, and `0.0` for `phase` (phase classification had no
probability floor before HC09, so any non-abstain answer still applies).
This is why every pre-HC09 test in this repo passes unchanged: nothing
about the defaults moved, only a name got attached to the concept and an
override path got added.

**Receipts.** `gate` and `passed_gate` are added to `scored` (the
read-shortcut decision), `phase_judged`, and `escalation_judged`, so a
report can see the exact threshold a judged decision was measured against
and whether it cleared it -- not just infer it from a comparison against
policy config after the fact.

## Decomposed escalation signals (HC10, opt-in)

`model_routing.escalation_judge: "decomposed"` replaces HC05's single
"should we escalate?" verdict with five atomic yes/no probabilities
(`plan_derailed`, `repeated_tool_errors`, `tests_failing`,
`unfamiliar_code`, `beyond_tier`), asked in ONE batched `ask_many()` call
and combined in code as `score = sum(weight[signal] * p[signal])`
(`model_routing.escalation_weights`, default `DEFAULT_ESCALATION_WEIGHTS`
in `contracts.py`) -- never a single trusted judged choice. `score` is
compared against `effective_gate(policy, "escalation")` with an explicit
±0.1 uncertain band: above it escalates, below it continues, and inside it
is deliberately left to the deterministic rules (`fast_decisions:escalation_signals`,
`decided: "uncertain_rules_only"`). The deterministic triggers
(`escalate_on_test_failure`, `max_requests_before_escalation`,
`escalate_on_provider_error`) remain a floor exactly as in HC05's `"judge"`
mode. See `docs/CONFIGURATION.md` and `docs/EVENTS.md`.

## Pre-tool risk classification in shadow mode (HC11, opt-in)

`Policy.tool_risk_shadow` asks a batched `destructive` /
`touches_production` / `category` classification immediately BEFORE
`ObservedTool.execute` invokes the real tool, and records a
`fast_decisions:tool_risk` receipt. This is observation only: the answer
is never consulted to block, modify, or approve the call -- native
approvals remain the sole authority, and only the tool name plus argument
KEYS (never values) reach the backend. A future deny/ask mode built on
these receipts would require its own preregistered evaluation against
labeled outcomes; none exists yet.

## Easy-turn shaping (HC12, opt-in)

`model_routing.easy_turn_guidance` (a string, default `None`) and
`model_routing.easy_turn_hide_tools` (a list of tool names, default `[]`)
address a specific benchmark finding: on turns the difficulty router (HC04,
above) judges `"cheap"`, a cheap model can still take several more provider
round trips than the host model would for the same turn, because it issues
one tool call per response and reaches for planning/checklist tools (e.g.
`todo`) before and after real work, where the host model batches independent
tool calls into a single response and verifies once. Each round trip costs
wall-clock time dominated by cached-prompt prefill, so the cheap model can
end up slower despite lower per-call latency. Both knobs apply only while
`turn.start_tier == "cheap"` -- decided once at turn start and never
re-evaluated mid-turn, so this is inert on every strong turn, stops once an easy turn escalates to the host, and everywhere
`model_routing` itself is not configured.

`RoutedProvider.complete` builds a shaped COPY of the request
(`_shape_easy_turn_request`) immediately before every slow provider call of
an easy turn: the guidance text is appended to the END of the last system
message's text content -- a fixed suffix of an otherwise-identical prefix,
so every call of the turn sends byte-identical system-prompt text, the
least cache-disruptive place to add it -- and/or any tool named in
`easy_turn_hide_tools` is removed from `request.tools` for that one call.
The original `request` object, its `messages` list, and every message/
content object it references are left untouched: only new objects are
returned, so a request reused by reference across calls (or turns, if the
host does that) is never corrupted. A hidden tool still exists in the
session; if the model calls it anyway nothing special happens, only that
call's advertised list was smaller. Fails closed (a no-op, `call_request is
request`) when there is no system message, or its content shape isn't a
plain string or a list with a `type: "text"` block to append to -- it never
invents a system message or a content shape a real provider was never sent
before.

The `fast_decisions:easy_turn_shaped` receipt fires at most once per turn,
on the first call where shaping actually changed something, carrying
`guidance_chars` (the length of the guidance text actually applied, never
the text itself) and `hidden_tools` (the tool names actually removed).
Shipped default in `behaviors/fast-decisions.yaml`: both knobs commented
out, so this feature is off until a benchmark demonstrates it helps. See
docs/EVENTS.md.

## Deadlines, budgets and failures

## Deadlines, budgets and failures

A single cooperative asynchronous deadline covers candidate collection, inference and revalidation. Synchronous callbacks, local file-system operations or native hooks that block the event loop can exceed the wall-clock budget; this is not a hard real-time scheduler. Backend HTTP retries are disabled. A backend failure opens a five-second cooldown. Invalid labels, malformed probabilities, an abstention, stale state, missing key, unknown alternatives, no eligible candidate or a timeout return to the original generative provider.

Cancellation propagates. It does not authorize a pending action. SDK outages are not silently replaced with a scripted model. Production configuration exposes three `backend` choices -- `jev`, `deterministic` (in-process, offline, `external=False`, never gated by `allow_external_state`; the shipped shadow default) and `unavailable`. The active path cannot submit a synthetic decision unless the explicit `allow_synthetic_active` policy flag is set, regardless of which backend produced it.

The default limits are three consecutive fast submissions, twelve per turn, twelve candidate actions and a 750 ms decision deadline. Submission budgets count denied submissions too, to avoid repeating them indefinitely. Used candidate identity includes its revision and arguments.

## Observability

One event envelope is written to the recorder and emitted through `coordinator.hooks.emit`. Additional event names are contributed through Amplifier's `observability.events` channel. Event hooks carry observation only; their responses do not control the action. Native execution hooks remain upstream and retain their own semantics.

JSONL recording uses a bounded queue and a writer thread. Recorder failure is visible as health metadata but is not an authorization mechanism or durable audit guarantee. Mount failure can fail the experimental module; the baseline orchestrator should remain available as a rollback.

The web viewer polls an authenticated local read endpoint every 250 ms. Polling is intentionally simple and separate from routing latency. It supports replay without a running session. There is no browser control endpoint and no browser-to-agent execution channel.

## Deliberately not in v0.1.0

No local Jev weights, Rust kernel changes, learned action generation, autonomous stopping, an *active* model-role router (shadow-only only -- see P4), production accuracy claim, forced fleet-wide rollout, automatic transcript export, or mandatory replacement of Foundation. These are separate experiments with separate acceptance criteria.

## Diagrams

- [System diagram](architecture.svg) (source: [architecture.dot](architecture.dot))
- [Layered view](architecture-layered.svg) (source: [architecture-layered.dot](architecture-layered.dot))

The 2026-09-18 hill-climbing campaign specification, launch prompt and companion defaults are kept verbatim under [docs/design/hill-climb-2026-09-18/](design/hill-climb-2026-09-18/START-HERE.md); results live in [docs/EVIDENCE.md](EVIDENCE.md) and `evals/`.
