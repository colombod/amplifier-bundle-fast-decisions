# Event contract v1.0

`probability_kind` describes score semantics. The local scorer emits
`token_mass_with_abstention_residual`: raw one-token probability mass with all
unrecognized/missing mass assigned to abstention. This is uncalibrated and does
not estimate real-world correctness. Other backends default to `backend_reported`.

Schema: `schemas/event.schema.json`. Names are added through the native `observability.events` contribution channel. Native events are not renamed or replaced.

Every event has `schema_version`, globally random `event_id`, `event`, `session_id`, optional `parent_session_id`, optional `turn_id`/`decision_id`, per-emitter `seq`, UTC `timestamp`, process-local `monotonic_ns`, a `synthetic` flag, and allowlisted `data`.

Session identity is read from the coordinator/session if exposed. A generated fallback ID is used otherwise. Parent linkage appears only when available; this is not a complete distributed trace protocol. Monotonic values cannot be compared across processes or machines. Event IDs support deduplication; wall time is display order, not a causal guarantee. The local viewer retains at most 20,000 events and 20,000 retained deduplication IDs by default. Export reflects that retained window; archive original JSONL separately for larger traces.

| Event suffix | Meaning |
|---|---|
| `turn_start` / `turn_end` | Hybrid turn boundary, mode, configured backend, `allow_external_state`, final submission/provider counters and recorder health |
| `requested` | Eligible prepared candidates sent for a decision; includes state hash, `state_chars`, `candidate_count`, `question_count`, `candidate_order_hash`, `candidates_suppressed_already_read`, `domain`, no raw state |
| `scored` | Returned distribution/model/usage and measured backend wall time, whether or not acted on; carries `candidate_order_hash`, `domain` |
| `routed` | Actual fast submission or slow route, plus mechanical reason; shadow can include `proposed_route`; carries `domain` once a candidate set has been built (early guard-clause routes, e.g. `mode_off`, precede that and carry none) |
| `fallback` | Error/timeout/envelope incompatibility; exception type only |
| `slow_start` / `slow_end` | Actual original provider complete or stream invocation, destination, time and available usage |
| `tool_start` / `tool_end` | Actual `execute()` reached and outcome; a native pre-hook alone does not produce these |
| `cancelled` | Cancellation observed and propagated |
| `health` | Recorder or metadata-only native-hook bridge status |
| `shadow_proposed` | The shadow worker scored a snapshot taken from the mounted context manager (`state_source: "context_mount"`); off the critical path, includes `choice`, `probabilities`, `selected_probability`, `margin`, `duration_ms`, `state_source`, `candidate_count`, `domain`, `state_chars`; carries `allow_external_state` only on the first `shadow_proposed` emitted for a given `turn_id` (a per-turn context field, not repeated every decision) |
| `shadow_observed` | Emitted from the same `tool:pre` call that took the snapshot behind `shadow_proposed` for this decision; carries `tool`, `tool_call_id`, `arguments_hash` -- what the LLM actually did, not what was proposed |
| `shadow_agreement` | A `shadow_proposed` proposal and its matching `shadow_observed` outcome were joined; `agreement` is `match` / `mismatch` / `abstained` / `unobserved`, plus `proposed_candidate`, `actual_tool`, `would_have_avoided_llm_turn`, `domain` |
| `role_proposed` | The shadow-only model-role router (P4) proposed a role for a `delegate` call, or recorded why it abstained (`explicit_role_present`, `role_resolver_unavailable`, `role_router_disabled`); carries `proposed_model_role`, `eligible_roles`, `reason_code`, `domain` (always `"model-role"`) |
| `role_agreement` | A `role_proposed` proposal and the delegate's actual routing were joined; `agreement` is `match` / `mismatch` / `unobserved`, plus `proposed_model_role`, `actual_model_role` (`null` = resolver default), `domain` |
| `observatory` | The auto-observatory's once-per-session `session:start` bootstrap; `action` is `reused` / `started` / `skipped` / `failed`, plus `reason` and (when relevant) `port` -- never the token-bearing URL, which never enters event data |
| `source` | Once per session, at mount, in every mode including `off`: which source actually ran. `source_kind` is `installed-cache` / `worktree` / `site-packages` / `unknown`; `source_git_sha` (40-hex or `null`) and `source_tree_sha256` (a SHA-256 over every `*.py` file's relative path and bytes, skipping `__pycache__`) identify the exact code; `source_py_files`, `package_version`, `python`, `mode`, and `module` (`hooks-fast-decisions` or `loop-fast-decisions`) round it out. Never a filesystem path -- see `provenance.describe_source` and docs/PRIVACY.md |
| `effort_routed` | HC03 (opt-in, off unless `Policy.effort_routing` is configured): emitted once per slow (`RoutedProvider.complete`) request, before the upstream provider call; carries `phase` (`orient` / `explore` / `implement`), `requested_effort` (the string set on `request.reasoning_effort`, or `null` when left unchanged), `default_effort` (always `"provider_default"` -- the policy never claims to know the provider's actual default), `reason_code` (`phase_policy` / `default_effort` / `host_pinned` / `escalated_max_explore` / `escalated_after_error`), `explore_requests` (this turn's explore-phase request count so far), `provider_call_id`, and `mode` |
| `model_routed` | HC04 (opt-in, off unless `Policy.model_routing` is configured): emitted once per slow (`RoutedProvider.complete`) request, before the upstream provider call; carries `phase`, `requested_model` (the string set on `request.model`/`kwargs["model"]`, or `null` when left unchanged), `requested_effort` (the starting effort applied, or `null`), `reason_code` (`start_model` / `host_pinned` / `escalated_max_requests` / `escalated_test_failure` / `escalated_provider_error` / `escalated_judge`), `escalated` (this turn's latch, once tripped it stays tripped), `escalation_reason` (`max_requests` / `test_failure` / `provider_error` / `judge` / `null`), `model_routed_requests` (this turn's count of requests where `start_model` was actually applied), `provider_call_id`, and `mode` |
| `escalation_judged` | HC05 (opt-in, off unless `Policy.model_routing.escalation_judge == "judge"`): emitted once per slow request past the turn's first, while not yet escalated, immediately before the deterministic `model_routed` decision on the SAME request; carries `backend` (`service.backend.name`), `choice` (`continue_cheap` / `escalate` / `null` on abstain/blocked/error), `probability` (the judge's own probability for `choice`, or `null`), `decided` (`escalate` / `continue` / `fallback_rules`), `duration_ms`, `phase`, `slow_requests_seen`, `mode`, `gate` (HC09's stake-scaled confidence floor for this decision), and `passed_gate` (whether `choice == "escalate"` and `probability >= gate`). Never fires when a deterministic trigger (`test_failure` / `max_requests`) already escalated this same request -- those remain a floor regardless of the judge |
| `phase_judged` | HC05 (opt-in, off unless `Policy.effort_routing.phase_judge` is `true`): emitted once per slow request, immediately after the deterministic `effort.classify_phase(request)` call and before `effort_routed`; carries `backend`, `choice` (one of `orient`/`explore`/`implement`, or `null` on abstain/blocked/error), `probability`, `agreed_with_rules` (`choice == the deterministic phase`, or `null` when `choice` is `null`), `duration_ms`, `gate` (HC09's stake-scaled confidence floor for this decision, see below), and `passed_gate` (whether `probability >= gate`). A non-null `choice` that also `passed_gate` overrides the phase used for the rest of this request's effort/model routing; otherwise the deterministic classification stands |
| `decided_batch` | HC08 (opt-in, off unless `Policy.decision_batching` is `true`): emitted once per request where the phase judge AND the escalation judge were both due and combined into ONE `ask_many()` backend call instead of two separate `ask()` calls; carries `backend`, `question_ids` (the batched question names), `n_questions`, `duration_ms`, and `mode`. Never fires when only one judge is due (nothing to batch) or when the batched call itself failed (see `TurnState.batch_fallbacks`) |
| `escalation_signals` | HC10 (opt-in, off unless `Policy.model_routing.escalation_judge == "decomposed"`): emitted once per slow request past the turn's first, while not yet escalated, in place of `escalation_judged`; the five atomic yes/no signals (`plan_derailed`, `repeated_tool_errors`, `tests_failing`, `unfamiliar_code`, `beyond_tier`) are asked in ONE `ask_many()` call and combined in code via a weighted sum -- never a single trusted verdict. Carries `backend`, `signal_probabilities` (`{signal_name: probability_of_yes}`, each `null` on abstain/blocked/error), `score` (the weighted sum), `gate` (HC09's stake-scaled confidence floor for `escalation`), `band` (the uncertain-band half-width, `0.1`), `decided` (`escalate` / `continue` / `uncertain_rules_only` / `fallback_rules`), `duration_ms`, `phase`, `slow_requests_seen`, and `mode`. `escalate` only when `score >= gate + band`; `continue` only when `score <= gate - band`; the band in between is `uncertain_rules_only` -- deliberately not acted on. Never fires when a deterministic trigger (`test_failure` / `max_requests`) already escalated this same request |
| `tool_risk` | HC11 (opt-in, off unless `Policy.tool_risk_shadow` is `true`): emitted once per tool call, immediately before `ObservedTool.execute` invokes the real tool; asks three atomic questions (`destructive`, `touches_production`, `category`) in ONE `ask_many()` call. Carries `tool`, `destructive` (`yes`/`no`/`null`), `touches_production` (`yes`/`no`/`null`), `category` (`read`/`write`/`execute`/`network`/`other`/`null`), `probabilities` (per-question probability distributions), `latency_ms`, `backend`, and `mode`. Purely observational: the answer is never consulted to block, modify, or approve the call -- native approvals remain the sole authority. Only the tool name and argument KEYS are sent to the backend; argument VALUES never leave this process |
| `easy_turn_shaped` | HC12 (opt-in, off unless `model_routing.easy_turn_guidance` and/or `model_routing.easy_turn_hide_tools` is configured): emitted at most ONCE per turn, on the first slow (`RoutedProvider.complete`) request where shaping actually changed something, while `turn.start_tier == "cheap"`. Carries `guidance_chars` (the length of the guidance text actually appended to the system prompt this call, `0` when no guidance was applied -- e.g. no system message was found), `hidden_tools` (the tool NAMES actually removed from this call's advertised tool list, `[]` when none matched), `provider_call_id`, and `mode`. Never the guidance text itself, never tool arguments/outputs. `turn.start_tier == "strong"`, or a turn where `model_routing` never fires, is byte-for-byte untouched: no event, no shaped request |
| `turn_planned` | Turn planner (opt-in, off unless `model_routing.planner.enabled` is `true`): emitted at most ONCE per turn, on the first easy-turn (`turn.start_tier == "cheap"`) slow request, immediately after the pure `planner.plan_turn()` call decided something (never emitted on abstain -- see below). Carries `objective` (`speed`/`cost`/`balanced`/`value`), `ctx` (the estimated prompt-token size of this turn's first request), `session_kind` (`sub_session`/`first_turn`/`later_turn`, see "Lookahead" in docs/proposals/TURN-PLANNER.md), `p_continue` (the resolved continuation probability for that kind), `options` (one `{model, warm, cold, cost, time, lookahead_cost, lookahead_time}` entry per priced/priored option, host included -- `lookahead_cost`/`lookahead_time` are always `0` for the host; each option also carries `utility` when `objective: value`, see "Objectives" in docs/proposals/TURN-PLANNER.md), `choice` (the chosen model id, equal to `host_model` when the host itself was chosen), `host_model`, `provider_call_id`, and `mode`; a top-level `value_of_time_usd_per_hour` is present only when `objective: value`. Never raw messages, tool arguments or model output. Abstains (the host has no price or prior in `savings.DEFAULT_RATES`/`planner.priors`) emit nothing here -- the turn falls back to the pre-planner `model_routed` behavior (`reason_code: start_model`) unchanged |

Fast tool IDs match the synthesized core ToolCall ID when the argument fingerprint still matches. If upstream modifies a call, or for ordinary slow-path calls, the tool facade may allocate an `observed_*` correlation ID instead. The native hook bridge can carry the original native ID. Do not assume these are identical in every path.

`duration_ms` has a `latency_kind` on score/provider completion events. The decision duration measures the backend call, not candidate collection, serialization, UI polling or total turn duration. Provider time includes whatever the provider does inside `complete()`. No ratio is presented as measured whole-task acceleration. Missing usage stays unknown, not zero. The synthetic tool-call envelope uses zero generative tokens; Jev input usage is recorded separately.

A fast `routed` event means a valid response envelope was submitted to the upstream loop. It does not mean that a tool was permitted or executed. A `tool_end` with success/error status is the execution observation. A shadow result never increments the fast-submission counter.

`routed` / `slow_start` / `slow_end` carry `transport_measured`: `"provider-complete"` or `"provider-stream"`, set at the point the provider facade is actually entered -- never a claim made at `turn_start`, since a single turn can enter the facade through both transports. The fast path is attempted only on the `provider-complete` transport; a `provider-stream` entry always routes slow with reason code `fast_path_unavailable_on_transport`, because loop-streaming's streaming branch cannot dispatch a tool call it receives (see `docs/UPSTREAM_CONTRACT.md`). Failing closed (defer to the real provider) is deliberate: the alternative is a prepared action silently dropped by upstream.

The visualizer's explanation is `reason_code`, not private model reasoning. Probability is neither calibrated task accuracy nor authority. The optional native hook bridge emits metadata for `execution:start`, `execution:end`, `provider:request`, `tool:pre`, `tool:post`, `provider:error`, `provider:retry`, `session:end`, and `context:compaction`. Retry metadata includes an optional attempt, HTTP status, and exception type; error bodies are excluded. Native hook observations do not prove the final outcome of the hook chain.

At mount, `health` with `phase: configuration` records effective mode, backend, external-state setting, policy version, an optional configured `session_label`, and `workspace_name`: the basename of the process working directory (or the hook's `workspace_name` config), a single path component and never a full path. The viewer names a session by `session_label`, then `workspace_name`, then its short id. A `session_heartbeat` is emitted every 15 seconds while the observer is mounted; cleanup cancels that task and emits `session_closed`. A heartbeat proves recent observer presence, not that the session is doing work. The viewer uses native turn lifecycle records for working/idle state and treats presence older than 45 seconds as unknown. Existing sessions need to reload the updated observer before they can emit these heartbeats.

The viewer's live view is a decision ledger: one row per decision key (session and `decision_id`), showing what the scorer proposed, what the recorded events say happened, and a labelled verdict (fast executed / submitted / failed, reasoning model, match / mismatch / abstained, no candidate, fallback, advisory, scripted, in flight, not scored). A native `tool:pre` / `tool:post` pair joins the decision whose `shadow_observed`, `tool_start` or `tool_end` recorded the same `tool_call_id` in the same session; otherwise it forms its own hook-observation row. A row is "in flight" only while the viewer is live, its latest record is under 90 seconds old, and no terminal record (`tool_end`, `slow_end`, `shadow_agreement`, `fallback`, `role_agreement`, `advisory_result`, or a native `tool:post`) has arrived; that is a display state, never an inference about execution. The viewer defaults to parent sessions, with children included in aggregate activity by default and expanded or selected separately on demand. The connection indicator describes the viewer connection, independently of each session's presence. Its default time window is one hour. Saved traces are labeled and do not receive live updates. Live records are polled every 500 ms. Live-flow cards highlight newly arriving recorded stages only; history has no animated execution clock. Replay is inspection of saved records, not a simulated execution.

Scripted and explicitly synthetic decision chains are hidden by default and never counted as model decisions or measured improvements. Native observations from a session using the scripted shadow scorer remain visible: the surrounding Amplifier execution is not thereby synthetic. `shadow_proposed` includes backend/model identity and score provenance; shadow proposals never count as fast submissions or executions. Unlabeled historical fixtures cannot be reliably identified as fixtures after the fact; producers must mark their provenance.

The mechanics view projects the recorded sequence for one session and decision ID. Native pre/post observations may also form a separate tool flow when their session and native tool-call IDs match; they remain hook observations, not measured execution. Missing stages stay missing; events from another session cannot complete a path even if their decision IDs match. A successful instrumented `tool_end` paired with a fast `routed` event establishes a completed fast-path tool. Counters of native tool results are labeled as reports; they do not imply equivalent execution instrumentation. Decision p95 measures backend scoring duration in the selected window, including shadow model scores. A `routed` fast event with `status: submitted_to_upstream` records a generative provider invocation bypassed at that boundary. It does not establish that a tool call was eliminated. Net task time, monetary savings, and task-quality parity require separate baseline/outcome evidence and are not inferred from counts or shadow agreement.

Portable Smart Tool calls emit `requested`, `scored`, and `health` with `phase: advisory_result`, `mode: advisory`, and `event_source: portable-smart-tool`. These prove only scoring and a returned suggestion. The caller supplies lineage IDs; no persistent session heartbeat, execution, or provider bypass is inferred. Failure returns typed abstention and is not counted as successful advice.

**Backend batching is a capability, not a latency guarantee.** Jev submits the
primary action and contributed questions in one `system_one` API call. The local
Ollama scorer supports only the primary action and explicitly rejects contributed
questions. An API batch does not prove shared KV computation, independent answers,
or a latency/cost improvement; those require backend-specific measurement.

`candidate_order_hash` retains its existing meaning: candidate IDs/origins sorted
into canonical order. It cannot detect option permutations or changed descriptions.
New `scored` and `shadow_proposed` records carry `option_set_hash` when the backend
supplies it. This SHA-256 digest covers the exact ordered action-option presentation,
including abstention and ID bindings, after backend rendering. Ollama hashes its
rendered action text and letter bindings (`ollama-options-v1`); Jev hashes its ordered
criteria entries (`jev-options-v1`). These hashes describe different representations
and must not be compared across backends. No raw target path is logged. Hashes are
identifiers, not encryption or anonymization. They do not fingerprint the shared
state, system prompt, contributed questions, model revision, or complete request.

Jev's stdlib (urllib-fallback) transport reuses one persistent keep-alive
connection per backend instance instead of opening a new TCP+TLS connection
per decision. `scored` records from that transport carry `connect_ms` (0.0
when an existing connection was reused, the measured handshake time when a
new one had to be opened) and `reused_connection` (whether this decision's
request ran on a connection already open from a prior decision). The SDK
transport sets `reused_connection` from whether its own client instance was
already constructed, but leaves `connect_ms` unset -- the SDK does not expose
a per-call connection/handshake timing hook.

`probability_kind`, `reported_confidence`, and `confidence_kind` are separate fields.
Ollama reports token mass with abstention residual and `confidence_kind: not_reported`.
Jev confidence remains `typesafe_reported_unspecified`: this adapter does not know
the remote formula/version. Neither statistic establishes empirical calibration.
Old records lacking these fields remain readable; missing metadata is unknown.

`requested` also carries `candidates_suppressed_already_read` (HC02a): the count of fast_workspace read/list candidates dropped this decision because their (path, revision) was already present in the turn's completed-read ledger (fed by both fast submissions and successful native/provider-selected reads -- see docs/ARCHITECTURE.md's Completed-read ledger section). When every remaining candidate is suppressed and none are left to even reach eligibility, `routed` fires with `reason_code: already_read_unchanged` instead of the generic `no_eligible_candidates` -- still with no backend scoring call. Suppression is gated by the `suppress_completed_reads` policy flag (default `True`).

`requested` also carries observation-loss telemetry from `build_state` (HC01): `observation_count` (messages actually included), `observations_available` (candidate messages the window/task-anchor step considered, before role or budget filtering), `observations_dropped` (available minus included, for any reason), `observations_clipped` (how many included messages were truncated by the budget's binary search), `task_anchored` (whether a user task message was found and survived into the state), and `truncation_reason` (`no_messages` / `budget` / `none`). These make state loss visible on every decision, not just inferable from `state_chars`.

`question_count` is the number of contributed judgment questions. A
misbehaving contributor (wrong shape, conflicting identifiers, or over the
`max_candidates`/`max_questions` bound) is dropped and counted via
`fallback` events with reason codes `contribution_shape_invalid`,
`contribution_conflict`, `contribution_truncated`, `question_criteria_invalid`
-- it can never disable the fast path for the other contributors.

**Three bench measurement fields, all small privacy-safe scalars.** `domain`
(one of `tool-choice` / `read-target` / `model-role`) is decided once, at the
point a decision's candidate set is built, by the single classifier
`contracts.classify_domain`: `"model-role"` for router (model-role)
decisions; `"read-target"` when every candidate targets `fast_workspace`;
`"tool-choice"` otherwise. `state_chars` (an integer, never the state text)
is `len(canonical(state))` for the state actually sent to the backend, on
`requested` (the main decision path) and `shadow_proposed` (the shadow
path). `allow_external_state` (a boolean, the shipped policy's own
external-state opt-in) is on `turn_start`, and on the *first*
`shadow_proposed` per `turn_id` only -- it is turn-level context, not
per-decision, so it is not repeated on every shadow proposal within the
same turn. All three are in `privacy.SAFE_FIELDS`.

**Shadow scoring runs off the critical path; the snapshot does not.** `hooks.emit` applies no per-handler timeout, so only the *backend scoring* that produces `shadow_proposed`/`shadow_agreement` is deferred to a background worker owned by `Runtime`. The *snapshot* (reading the mounted context manager, collecting candidates, hashing) runs inline in the `tool:pre` handler, immediately before tool dispatch, and is hard-bounded by `shadow_max_messages`, `max_state_chars` and `shadow_snapshot_budget_ms`. Exceeding the wall-clock budget is a normal, counted outcome -- the snapshot is abandoned and `fallback` fires with `reason_code: shadow_snapshot_budget_exceeded`, never an exception into the hook chain. No handler in the shadow scorer can raise into the hook chain or change the turn; every handler returns `continue` unconditionally.

**Why `tool:pre`, not `provider:request`.** The snapshot used to run in the `provider:request` handler. A live DTU trace against the real Amplifier CLI showed `amplifier_module_loop_streaming` emits iteration 1's `provider:request` *before* appending the turn's own user message to the mounted context (see `docs/UPSTREAM_CONTRACT.md`), so `context.get_messages()` was empty on that call for every turn: no candidates were ever found, and the real tool call that followed was never scored (no `shadow_observed`). The next iteration's snapshot then produced an unmatched, one-iteration-stale `shadow_proposed` that `execution:end`'s `sweep_unobserved()` silently discarded. Moving the snapshot to `on_tool_pre` -- the earliest point observed to reliably include this iteration's own messages -- fixed both: see `docs/design/redesign-2026-09-17.md` P3 postmortem addendum.

**The shipped `backend: deterministic` default actually measures something.** `allow_external_state` gates any call to an *external* backend (`backend.external == True`, i.e. `jev`) -- it never gated the in-process `deterministic` backend (`ScriptedBackend`, `external == False`), but before this fix `get_runtime` only accepted `jev` or `unavailable` as config values, and the shipped `behaviors/fast-decisions.yaml` set `backend: jev` alongside `allow_external_state: false`. That combination is internally consistent (the gate correctly refuses to call Jev without opt-in) but means the shadow rung never produced a `shadow_proposed`/`shadow_agreement` -- only a `fallback` with `reason_code: external_state_not_enabled`, forever. `backend: deterministic` is now a first-class option: an in-process, offline scorer that is never subject to the external-state gate, matching the rung ladder in `docs/design/redesign-2026-09-17.md` P3 (`off -> shadow, external=false (deterministic only) -> shadow, external=true (real jev) -> active`). Set `backend: jev` and `allow_external_state: true` together to move to the next rung.

**The model-role router's `actual_model_role` is read from the tool's own return value, not a top-level hook field.** `tool:post`'s real payload (`loop-streaming:6348-6357`) carries the tool's dumped `ToolResult` under `result`; `tool-delegate` places its routing summary at `result.output.provider_routing.model_role`, never at a top-level `data["provider_routing"]`. Reading the wrong location always returned `None` for `actual`, so every observed `role_agreement` reported `"mismatch"` regardless of what the delegate actually resolved to. `router.on_delegate_post` now reads the nested field via `field_value`, which duck-types across a plain dict or an attribute-bearing object.

**The router is never silent, even when disabled.** `role_router: false` (the unconfigured library default; the shipped `behaviors/fast-decisions.yaml` sets `role_router: true`) previously made `on_delegate_pre` return with no event at all for a `delegate` call -- indistinguishable from a crash or a missed registration. It now emits `role_proposed` with `reason_code: role_router_disabled` (no job enqueued, no probe, no turn behaviour change) so "no telemetry" never has to be interpreted as "the router isn't wired up".

**HC03 (phase-specific effort routing) never touches the model or approvals.**
`effort_routed` is the only new surface: `orchestrator.effort.classify_phase`
deterministically reads `request.messages` for the CURRENT turn only (messages
after the last `user` message), classifying `orient` (no assistant message
yet), `explore` (an assistant message exists, no write-like tool call has
occurred this turn, and the most recent assistant message's tool calls are
all read-like), or `implement` (a write-like tool call has occurred -- this
also covers "verify" requests such as running tests via `bash`, which are
write-like and therefore fold into `implement`'s default-effort treatment).
When the policy's `effort_routing.explore` effort applies, `RoutedProvider`
sets `request.reasoning_effort` via `setattr`/item-assignment before
forwarding to the real provider; it never sets `request.model`. An explicit
per-request `request.reasoning_effort` already present is always honored
untouched (`reason_code: host_pinned`). `Policy.effort_routing` defaults to
`None`: routing is fully opt-in, and disabled routing emits no
`effort_routed` event at all and never reads or writes
`request.reasoning_effort`. See docs/ARCHITECTURE.md's "Phase-specific
effort routing (HC03, opt-in)" section.

**HC05 (judge-driven escalation and phase classification) reuses the
read-shortcut's own contract, never `DecisionService.choose`.** Both
`escalation_judged` and `phase_judged` are produced by
`orchestrator._ask_judge_choice`, which builds one `DecisionRequest` with
`candidates=()` and a single contributed `Question` (type `choice`), then
calls `service.backend.ask()` directly -- the same `DecisionRequest` /
`DecisionResult` / `Question` / `Answer` contract the fast-path read
decision (`DecisionService.choose`) uses, so it inherits `Policy.timeout_ms`
and the identical `backend.external and not allow_external_state` gate
(Jev refuses without consent, exactly as for a read candidate). It never
calls `DecisionService.choose` itself: there is no prepared action to
submit, only a judgment. State sent to the judge is deliberately tiny and
bounded (`orchestrator._judge_state`): a 300-char head of the first user
message, the phase, this turn's slow-request count, tool names used so far,
a 600-char excerpt of the last tool result, and the two HC04 failure
signals (`test_failure_seen`, `provider_errors_seen`) -- trimmed further if
the serialized state would still exceed `Policy.max_state_chars`. Never the
full conversation, tool arguments, or model output.

## Correlated execution receipts

`slow_start` and `slow_end` now include a unique `provider_call_id` per invocation.
`slow_end.status` distinguishes success, error and cancellation; stream usage stays
unknown unless measured. `routed:fast` includes the prepared `tool_call_id`.
`turn_end` records status and execution wall time, never task-quality success.
The instrumented `off` mode records ordinary execution without active or background
shadow inference. See [OPERATIONS.md](OPERATIONS.md) for aggregation and completeness.

**HC08 ("one call per decision point", opt-in) combines HC05's two judge
asks into one backend call.** `Policy.decision_batching` (default `False`)
lets the orchestrator ask the phase judge (`effort_routing.phase_judge`)
and the escalation judge (`model_routing.escalation_judge == "judge"`) in
ONE `ask_many()` call, instead of two separate `ask()` calls, whenever
BOTH are due for the same request. `backends.ask_many()` provides this as
a mixin: a backend with its own `ask_many` (Jev, and the in-repo
`ScriptedBackend` test double) answers every question in one wire call;
any other backend (ollama/mlx/hosted/laya) gets the identical capability
via a generic fallback that runs one single-question `ask()` per question
concurrently and merges the answers -- no change required in those
modules. The `fast_decisions:decided_batch` receipt records the batch;
`phase_judged`/`escalation_judged` are still emitted exactly as before,
using the batched answers instead of asking again. If the batched call
raises or times out, the request falls back to the pre-HC08 sequential
path (unchanged) and `TurnState.batch_fallbacks` is incremented. Batching
never changes which candidates or questions are asked, only how many
backend calls it takes to ask them; it is a no-op whenever fewer than two
judge mechanisms are configured/due for the same request.

**HC09 ("stake-scaled confidence gates", opt-in) is a per-judged-decision
probability floor.** `Policy.confidence_gates` (default `None`) maps up to
three kinds -- `read_shortcut` (the fast-path action choice), `phase`, and
`escalation` -- to a probability in `(0, 1]` below which the judge's
answer is NOT acted on: `read_shortcut` lets the model run instead of
submitting the prepared action, `phase` keeps the deterministic
classification, and `escalation` stays on the deterministic rules. Any
kind absent from `confidence_gates` (or the whole policy omitting it)
falls back to its pre-HC09 legacy source byte-for-byte:
`Policy.min_probability` for `read_shortcut` (unchanged, already the
existing `DecisionService.choose` threshold), `model_routing.escalate_min_probability`
(default `0.7`) for `escalation`, and `0.0` for `phase` -- phase
classification never had a probability floor before HC09, so any
non-abstain judge answer still applies by default. A `confidence_gates`
key always overrides its legacy alias when both are set. `gate` and
`passed_gate` are added to `scored` (read-shortcut), `phase_judged`, and
`escalation_judged` so every judged decision's receipt shows the floor it
was measured against and whether it cleared it.

**HC12 (easy-turn shaping) builds a shaped COPY of the request per call; it never mutates the original.** `model_routing.easy_turn_guidance` (a string) and `model_routing.easy_turn_hide_tools` (a list of tool names) apply only while `turn.start_tier == "cheap"` -- decided once by the turn-start difficulty router (HC04, above) and never re-evaluated mid-turn. `orchestrator._shape_easy_turn_request` returns a new request object with the guidance text appended to the END of the last system message's text content (a fixed suffix of an otherwise-identical prefix, so every call of the turn sends byte-identical system-prompt text -- the least cache-disruptive place to add it) and/or the named tools removed from `request.tools` for that one call; the original `request`, its `messages` list, and every message/content object it references are left untouched, so a request object reused by reference across calls (or turns) is never corrupted. A tool hidden this way still exists in the session: if the model calls it anyway, nothing special happens, only that one call's advertised list was smaller. Fails closed (no-op) when there is no system message, or its content shape isn't a plain string or a list with a `type: "text"` block -- it never invents a system message. Both knobs default to `None`/`[]` (fully inert): no request field is read or written and no `easy_turn_shaped` event is emitted.

**The turn planner (opt-in) picks the model for one easy turn by comparing
a small cost/time model, not a fixed `start_model`.** `model_routing.planner`
(default off -- the key is optional and `enabled` defaults to `False`) runs
`planner.plan_turn()`, a pure function, once per turn at the first request
where `turn.start_tier == "cheap"` and the turn is otherwise untouched
(unescalated, provider-matched, not host-pinned) -- exactly the same
decision point that used to unconditionally assign `start_model`. It never
runs for a hard, scope-gated, or user-pinned turn; those are decided
entirely by the turn-start difficulty router (HC04, above) before the
planner is ever consulted. For each option (the host plus each configured
candidate, defaulting to `[start_model]` when `planner.candidates` is
empty), it estimates whether that model is still "warm" (served a call in
this session within `planner.cache_ttl_seconds`) using per-session cache
state recorded from every real provider response's usage
(`orchestrator.RoutedProvider`'s `last_used_at` + reported prompt-token
size, keyed by model), then prices the turn's expected calls at
`savings.DEFAULT_RATES` and the configured `priors` (per-model-family
latency/cold-cache measurements). `speed`/`cost` pick the least time/cost
option; `balanced` (the default objective) picks the least time among
options within `cost_tolerance` of the host's own cost -- the host always
qualifies, and ties go to the host. Choosing the host behaves exactly like
a hard turn (`reason_code: planner_host`, no `request.model`/`kwargs["model"]`
override); choosing a candidate behaves exactly like today's easy turn
with that model as the start model for the whole turn
(`reason_code: planner_<objective>`). When the host itself has no price or
prior, the planner abstains for that turn and the pre-planner
`start_model` assignment applies unchanged (`reason_code: start_model`) --
this is the only case where `planner.enabled: true` produces no
`turn_planned` receipt and no behavior change. HC12's easy-turn shaping
(`easy_turn_guidance`/`easy_turn_hide_tools`, above) still keys off
`turn.start_tier == "cheap"` only -- it is NOT gated on which model the
planner chose, so a `planner_host` turn (which runs on the host, same as
`start_strong`) does not receive easy-turn shaping even though
`turn.start_tier` reads `"cheap"`; shaping only ever fires on a request
that actually carries a routed (non-host) model override.

**HC04 (opt-in model routing with escalation) never bypasses approvals or the upstream tool-call loop.**
`Policy.model_routing` (default `None`) lets a host start a turn's generative
requests pinned to a cheaper/faster `start_model` (and optionally a starting
`request.reasoning_effort`), then escalate to the host's normal provider
default the moment risk signals appear: too many slow requests in the turn,
an observed test-tool failure, or an upstream provider exception. Once
`turn.escalated` is set it never resets mid-turn -- every subsequent slow
request in that turn is left untouched (`reason_code`
`escalated_max_requests` / `escalated_test_failure` / `escalated_provider_error`).
An explicit host-set `request.model` is always respected
(`reason_code: host_pinned`) unless `model_routing["override_explicit_model"]`
is `true`. `model_routed` is the only new event surface; it never changes
tool approvals, never touches `stream()`, and never overrides an
already-applied HC03 `effort_routed` result on the same request.
