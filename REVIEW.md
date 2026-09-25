# Turn Planner Adversarial Review

Branch: `feat/turn-planner-review` (checked out from `feat/turn-planner`).
Scope: `src/amplifier_fast_decisions/{planner,orchestrator,contracts,runtime,privacy}.py`,
`src/amplifier_fast_decisions/observer.py`, `docs/proposals/TURN-PLANNER.md`, `docs/EVENTS.md`,
`schemas/event.schema.json`, `tests/test_turn_planner.py`.

Working notes/evidence trail for every step below (including RED/GREEN command output) live in
`/project/workspace/fd_notes/scratch.md`.

## Findings

### 1. Host model id resolution (Area 1) -- reviewed, no defect found
- **Location**: `src/amplifier_fast_decisions/orchestrator.py` -- `decide_start_tier()` (~line
  444-460) and the planner's `host_model` computation (~line 1190:
  `host_model = getattr(self._provider, "default_model", None) or start_model`).
- **Severity**: n/a (no defect).
- **Evidence**: `decide_start_tier()` returns `"strong"` (with `reason_code:
  "user_model_strong"`) whenever `_user_selected_model()` is truthy (either the
  `ui.model_override` session-state marker, or `provider.default_model` having changed since
  session start). A `"strong"` `start_tier` sets `strong_turn = True`, which short-circuits the
  `else` branch containing the planner call entirely (`reason_code: "start_strong"`). Therefore,
  whenever the planner branch actually executes, no user model override is in effect for that
  turn, by construction -- so `getattr(self._provider, "default_model", None)` at that point
  correctly reflects the model that will serve an un-overridden request. The `getattr` is read
  fresh (not cached) at the single point in the turn the planner runs; the once-per-turn caching
  of the resulting plan (`turn.planner_decided` / `turn.planner_plan`) matches the spec's explicit
  "decide once per turn" requirement (`docs/EVENTS.md` `turn_planned` row: "emitted at most ONCE
  per turn"; corroborated by mutant `b5` in `mutation-table.md`, already caught by an existing
  test, confirming once-per-turn is enforced elsewhere in the router).
- **Fix applied**: none needed -- correct by construction.
- **Closing test evidence (added in a follow-up pass)**: the analysis above was code-reading
  only when first written. It is now backed by an actual regression test rather than resting
  on inspection alone:
  `tests/test_turn_planner.py::CacheKeyRealModelIdRegressionTests::test_ui_model_override_bypasses_planner_with_planner_enabled`
  drives a `ui.model_override` session-state marker (the `--model`/in-session-pick
  representation) through `RoutedProvider.complete()` with the turn planner **enabled**, and
  asserts `start_tier == "strong"`, `turn.planner_decided` stays `False`, no `turn_planned`
  event is emitted, and the routed `reason_code` is `start_strong` -- proving the planner
  branch is truly bypassed, not just "should be" by reading the code. A companion test,
  `test_mid_session_default_model_change_keys_cache_by_new_model_not_stale_one`, drives two
  turns of the same session where `provider.default_model` changes between them with no
  explicit per-request override, and asserts turn 2 is also forced strong
  (`reason_code: "user_model_strong"`) and its cache entry is keyed by the *new* model, with
  turn 1's own cache entry left untouched. Both passed immediately on first run -- no bug
  found, Area 1's "correct by construction" conclusion stands, now proven rather than asserted.

### 2. Per-model cache state keying / placeholder fallback (Area 2) -- reviewed, no defect found; coverage gap identified and now closed
- **Location**: `src/amplifier_fast_decisions/orchestrator.py`, post-response cache-update block
  (~lines 1314-1331):
  ```python
  served = usage.get("served_model") or model
  if served in (None, "", "provider-default"):
      served = getattr(self._provider, "default_model", None)
  ```
- **Severity**: low (defensive code already correct; test gap only).
- **Evidence**: The code already explicitly guards against keying `planner_state` by the literal
  placeholder string `"provider-default"` (or `None`/`""`), falling back to a freshly-read
  `provider.default_model`. This block runs on every slow call whenever
  `effective_planner_config(model_routing)` is not `None` (i.e. whenever `model_routing.planner`
  is enabled), regardless of which routing branch fired that turn -- confirmed this is
  intentional/harmless: the fallback only triggers when the response reports no `served_model`
  *and* no explicit `request.model` was set, in which case `provider.default_model` at that
  instant is, by definition, the model that served the request (not cached/stale).
- **Coverage gap (as originally identified)**: all existing test doubles (`PricedProvider`,
  `DemoProvider`) always set `response.model` to the served model, so the
  `served in (None, "", "provider-default")` fallback branch was never exercised by any existing
  test. This was flagged but explicitly left open in the original pass (time/turn budget was
  prioritized toward the confirmed bug in Area 3 and the explicitly-requested coverage gap in
  Area 5).
- **Coverage gap: now closed (follow-up pass)**: added `NoServedModelPricedProvider` (a
  `PricedProvider` subclass whose response always reports a blank `model` and carries no
  `metadata`) plus
  `tests/test_turn_planner.py::CacheKeyRealModelIdRegressionTests::test_no_served_model_in_response_falls_back_to_real_provider_default`.
  The test drives a turn where the planner chooses the **host** (so `request.model` is never
  reassigned away from the `"provider-default"` placeholder set at request-construction time),
  with a response that reports no served model at all -- forcing `usage_fields()` to omit
  `served_model` entirely, so `served` resolves to the literal `"provider-default"` string,
  which must then hit the `getattr(self._provider, "default_model", None)` fallback. The test
  asserts `planner_state` is keyed by the real model id (`OPUS`, matching
  `provider.default_model` at call time) with correct `cached_tokens`/`last_used_at`, and that
  `planner_state` contains neither the literal `"provider-default"` string nor `None`/`""` as a
  key. **Result: passed immediately on first run** -- confirms the fallback branch is correct by
  construction, now proven by test execution rather than code-reading alone. No bug found; no
  source change was needed.

### 3. Context-length estimation & compaction (Area 3) -- CONFIRMED REAL BUG, FIXED
- **Location**: `src/amplifier_fast_decisions/observer.py` (bug was in the `observe()` closure,
  ~old line 505-509; fix now lives in standalone function `reset_planner_ctx_on_compaction()` at
  `observer.py:145`, called from `observe()` at `observer.py:527`). Also implicates
  `_estimate_ctx()` in `orchestrator.py` (~line 268-283) and `Runtime.planner_last_ctx`
  (`runtime.py`).
- **Severity**: HIGH -- silent, permanent cost/cache-warmth overestimation for the rest of a
  session after any real context compaction; exactly the "silent wrong routing/cost" failure mode
  this review targets.
- **Evidence (before fix)**: `runtime.planner_last_ctx` (the persisted last-known total prompt
  size) is set only at `Runtime.__init__` (`None`), loaded from disk, and overwritten after every
  real provider response (`orchestrator.py:~1330`). No code path reset or invalidated it on a
  `"context:compaction"` native event -- `observer.py`'s `observe()` handled that event (among
  others) only as inert telemetry passthrough (`await runtime.service.emit("health", ...)`).
  Consequently, `_estimate_ctx()` (`return runtime.planner_last_ctx + new_message_chars // 4`)
  kept adding new-message sizes on top of the stale, pre-compaction baseline indefinitely after a
  real compaction shrank the actual conversation.
- **Fix applied**: added `reset_planner_ctx_on_compaction(runtime, event)` in `observer.py`,
  called as the first line of `observe()`; sets `runtime.planner_last_ctx = None` when
  `event == "context:compaction"`, forcing the next `_estimate_ctx()` call to fall back to its
  designed cold-start path (a fresh per-request measurement) instead of compounding stale state.
  Extracted as a standalone function (rather than left inline) because `observer.mount()` hard
  -imports `amplifier_core.models.HookResult`, which is not installed in this sandbox -- the
  standalone function is unit-testable without that dependency.
- **Regression tests** (`tests/test_turn_planner.py`, class `ContextCompactionResetTests`, line
  859):
  - `test_compaction_event_resets_stale_planner_last_ctx` (line 872)
  - `test_compaction_reset_makes_next_estimate_fall_back_to_cold_start` (line 882)
  - `test_unrelated_events_do_not_reset_planner_last_ctx` (line 896) -- guards against over-broad
    event-name matching.
- **RED evidence** (fix temporarily neutralized):
  ```
  FAIL: test_compaction_event_resets_stale_planner_last_ctx -- AssertionError: 150000 is not None
  FAIL: test_compaction_reset_makes_next_estimate_fall_back_to_cold_start -- AssertionError: 151000 != 1013
  Ran 3 tests in 0.057s FAILED (failures=2)
  ```
- **GREEN evidence** (fix restored):
  ```
  test_compaction_event_resets_stale_planner_last_ctx ... ok
  test_compaction_reset_makes_next_estimate_fall_back_to_cold_start ... ok
  test_unrelated_events_do_not_reset_planner_last_ctx ... ok
  Ran 3 tests in 0.036s OK
  ```
- **Spec note**: `docs/proposals/TURN-PLANNER.md` does not mention context compaction at all.
  Recommend adding a sentence to the `## Behaviour` section (line 27) or `## Implementation notes`
  section (line 73) documenting that `planner_last_ctx` is invalidated on `context:compaction` and
  that the estimator falls back to a cold-start measurement in that case.

### 4. Balanced-choice tie-breaking, candidate-vs-candidate (Area 4) -- reviewed, no defect found; coverage gap closed
- **Location**: `src/amplifier_fast_decisions/planner.py`, `plan_turn()` (~lines 156-170):
  `winners = [...]`; `choice = host if host in winners else winners[0]`.
- **Severity**: n/a (no defect); documentation gap noted.
- **Evidence**: `options`/`eligible` preserve the caller-supplied `candidates` list order (not
  dict/hash ordering), so `winners[0]` deterministically resolves to the first-listed candidate
  (in the order the caller passed `candidates`) among those tied for best score, whenever the
  host itself is not among the winners. This is deterministic, not an ordering accident.
- **Spec-vs-code gap**: `docs/proposals/TURN-PLANNER.md`, `## Behaviour` section (heading at line
  27; the actual tie-break sentence "Ties go to the host." is at line 63) states only the
  host-vs-candidate tie rule and is silent on candidate-vs-candidate ties. **The exact section
  that needs updating is `## Behaviour` (line 63's sentence should be extended)** with, e.g.: "A
  tie between two non-host candidates is broken by their order in the configured `candidates`
  list (first listed wins)." This is a documentation gap, not a code bug -- the code's behavior is
  reasonable and deterministic.
- **Regression test added**: `tests/test_turn_planner.py`,
  `PlanTurnTests.test_candidate_vs_candidate_tie_goes_to_first_listed_candidate` (line 139) --
  constructs two synthetic candidates with identical rates/priors, both strictly cheaper than the
  host, and asserts the choice flips when the candidate list order is reversed (proving the
  tie-break is driven by caller-supplied list order, not an accidental ordering source). This test
  passes against the current, unmodified `planner.py` (coverage-gap closure, not a bugfix).

### 5. Planner-disabled byte-identical requirement (Area 5) -- reviewed, no defect found; coverage gap closed
- **Location**: `tests/test_turn_planner.py`, pre-existing class
  `PlannerDisabledByteIdenticalTests` only asserted `req.model` equality plus two event fields --
  not a full byte-identical comparison of the complete request and complete event stream, despite
  the spec's disabled-planner requirement calling for exactly that.
- **Severity**: n/a (no code defect found); the gap was in test coverage only.
- **Evidence**: Added a new class, `PlannerDisabledFullByteIdenticalTests`
  (`tests/test_turn_planner.py:446`), with two tests comparing a planner-ABSENT baseline run
  against a planner-enabled-but-`disabled` run via `json.dumps(..., sort_keys=True)` equality of
  the *entire* constructed request object and the *entire* emitted event stream:
  - `test_no_planner_key_vs_planner_disabled_full_request_payload_byte_identical` (line 471)
  - `test_no_planner_key_vs_planner_disabled_full_event_stream_byte_identical` (line 488)
  A recursive helper, `_strip_nondeterministic_event_fields`, removes only fields independently
  confirmed (via grep to their source) to be inherently non-deterministic per run: `event_id`,
  `timestamp`, `monotonic_ns`, `session_id`, `parent_session_id` (random/wall-clock fields in
  `telemetry.py`/`demo.py`), `decision_id` (`uuid4().hex` in `service.py:77`), `provider_call_id`
  (`"provider_" + uuid4().hex` in `orchestrator.py:914`/`:1360`), and `duration_ms`
  (`time.perf_counter()` wall-clock elapsed time, `orchestrator.py:1334` and similar). After
  stripping only those, the full request and full event stream are byte-identical between the two
  runs, confirming the disabled-planner byte-identical requirement genuinely holds in current code
  -- the previous tests just didn't check strongly enough to prove it.
- **Fix applied**: test-only; no `src/` change required.

### 6. Spec-vs-code cross-check (Area 6)
- `docs/EVENTS.md`'s `turn_planned` row (line 40) and Behaviour-equivalent prose (lines 218-249)
  corroborate Areas 1, 3, and 4's conclusions; no new findings.
- The one concrete spec update needed is the Area 4 item above:
  **`docs/proposals/TURN-PLANNER.md`, `## Behaviour` section** should gain a sentence describing
  candidate-vs-candidate tie-break order. (Not applied as part of this review -- scope was fixing
  code + tests, not editing the proposal doc; flagging per the task's instruction to name the
  exact section.)

### 7. Mutation-table cross-check (Area 7) -- context only, no re-litigation
- `docs/evidence/2026-09-24/followups/test-audit/mutation-table.md` rows `b3`/`b3b`/`b3c`/`b3d`
  (`_user_selected_model` correctness), `b4` (host_model in `slow_end`), and `b5` (start-tier
  decided once per turn) are all already fixed and covered by existing tests in
  `tests/test_orchestrator_primary.py`. These corroborate Area 1's conclusions but concern the
  difficulty-router/tier-decision layer upstream of the planner itself, not the planner's own
  logic -- no overlap requiring re-litigation.

### Style-only issues (not fixed, per scope discipline)
- None identified as in-scope-but-unfixed beyond the Area 2 coverage gap noted above (which is a
  test-coverage gap, not a style issue).

## Test Evidence

- `PYTHONPATH=src python3 -m unittest tests.test_turn_planner -v`: **60 tests, all OK** (zero
  failures/errors) -- 100% clean, as required.
- `PYTHONPATH=src python3 -m unittest discover -s tests` (full suite, after all changes):
  **1181 tests, FAILED (failures=11, errors=47, skipped=40)**.
- Baseline (before any change in this review, same command): **1175 tests, FAILED (failures=11,
  errors=47, skipped=40)**, all in `tests/test_local_backend.py` / `tests/test_polyglot_tasks.py`
  (missing go/rust/java toolchains and one unrelated local-backend config assertion -- confirmed
  pre-existing and unrelated to the turn planner).
- **Comparison**: failures (11=11), errors (47=47), skipped (40=40) all unchanged; +6 tests, all
  new and all passing. **Zero new failures or errors introduced.**

### Follow-up pass (Area 1/2 coverage-gap closure)
- Added `CacheKeyRealModelIdRegressionTests` (3 new tests) to `tests/test_turn_planner.py`,
  closing the Area 2 coverage gap and turning Area 1's "correct by construction" conclusion into
  an executed regression test.
- `PYTHONPATH=src python3 -m unittest tests.test_turn_planner -v`: **63 tests, all OK** (zero
  failures/errors) -- 100% clean.
- `PYTHONPATH=src python3 -m unittest discover -s tests` (full suite, after this follow-up):
  **1184 tests, FAILED (failures=11, errors=47, skipped=40)** -- identical failure/error/skipped
  counts to the baseline above; +3 tests (the new ones), all passing. **Zero new failures or
  errors introduced.**

## Summary Table

| # | Area | Verdict | Severity | Disposition |
|---|------|---------|----------|-------------|
| 1 | Host model id resolution | No defect | n/a | Reviewed, no fix needed |
| 2 | Cache keying placeholder fallback | No defect | low | Coverage gap closed (follow-up): `CacheKeyRealModelIdRegressionTests` |
| 3 | Ctx estimation / compaction reset | **Confirmed bug** | **high** | Fixed: `observer.py:145,527` + `ContextCompactionResetTests` |
| 4 | Candidate-vs-candidate tie-break | No defect | n/a (doc gap) | Coverage gap closed; TURN-PLANNER.md `## Behaviour` needs a sentence |
| 5 | Planner-disabled byte-identical | No defect | n/a | Coverage gap closed: `PlannerDisabledFullByteIdenticalTests` |
