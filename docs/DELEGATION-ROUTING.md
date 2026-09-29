# Per-delegation model routing (opt-in, off by default)

One routing decision per `delegate` tool call, taken **before the child session
exists**. Four batched judge questions, a policy that is data, and -- in
`enforce` mode only -- an explicit `provider_preferences` pin handed to the real
delegate tool.

**Off by default.** `Policy.delegation_routing` is `None` unless you configure
it: no facade is built, no judge call is made, the `delegate` entry of the tool
mapping is the same object it was before, and no event is emitted.

## Why a second routing point

`RoutedProvider` routes **requests inside a session that is already running on a
chosen model**: start tier, escalation, effort per phase. It cannot change what a
delegated child starts as, because by the time the child's first request reaches
a provider facade, the child's provider and model are already fixed.

A delegation is the one moment where all four questions are still open at once:

| Decision | Per-turn router (`RoutedProvider`) | Per-delegation (this) |
|---|---|---|
| Which model the work starts on | the host's, or `start_model` | any model in the ladder |
| Which **provider** | fixed | can cross providers |
| Reasoning effort | yes, per phase | yes, as a lever |
| **Capability requirements** | n/a | yes -- a computer-use delegation is never routed onto a model verified to reject the computer-use tool type |

It also complements `jev_cua`: that decides how a computer-use *task* is driven;
this decides whether the delegated agent is even given a model that can drive it.

And it complements the shadow role router in `router.py`. That one proposes a
`model_role` and records proposed-vs-actual; it cannot act, because `modify` is
not honored at `tool:pre` (docs/UPSTREAM_CONTRACT.md). This layer sits in the
tool mapping instead of the hook chain, so it can actually pin -- and records the
same proposed-vs-actual pair.

The full study behind these numbers (method, per-lever results, rounds v1-v3, limits) is in
[`docs/evidence/DELEGATION-ROUTING-STUDY.md`](evidence/DELEGATION-ROUTING-STUDY.md).

## How it works

```
delegate(agent=..., instruction=...)
   |
   +-- anchor: what this delegation would have used WITHOUT us
   |     call's model_role  -> model_role_resolver capability (routing-matrix)
   |     else agent frontmatter -> the agent's own pre-resolved preferences
   |
   +-- judge: ONE ask_many() with four questions (min_tier, answer_depth,
   |          needs_computer_use, needs_vision), bounded by deadline_ms
   |
   +-- policy: anchor tier x min_tier -> down/keep/up, overlaid by depth,
   |           then the first candidate that passes the guards, the context
   |           window check and the capability requirements
   |
   +-- shadow  -> record the proposal, run the call unchanged
       enforce -> run the call with provider_preferences = [pin, *anchor[1:]]
       abstain -> run the call unchanged, with the reason recorded
```

Where it hooks in: `orchestrator.py`, `HybridOrchestrator.execute` -- the ONE
`delegate` entry of `wrapped_tools` becomes a `DelegateFacade` around its own
`ObservedTool`. The observation, its `tool_start`/`tool_end` receipts and the
waste guards are untouched; the facade only chooses the arguments the observed
tool is called with. It is deliberately not a generic tool wrapper and never
declares `native_tool_spec` (`delegate` has none, and advertising one would let
the provider bypass the arguments just decided).

The facade awaits the real tool **in the same asyncio task**: tool-delegate reads
its dispatch context from `coordinator._tool_dispatch_contexts[asyncio.current_task()]`,
so running it in another task would silently lose the parent's `tool_call_id`.

**Nothing here can fail a delegation.** Every failure -- judge timeout,
unavailable backend, unloadable policy, malformed answer, missing resolver --
abstains and returns the original arguments.

## Privacy

The delegated instruction is sent to **the session's configured judge backend and
nowhere else**, truncated to 2500 characters and bounded again by
`max_state_chars` (`delegation.judge_state`). It never enters an event: receipts
carry `instruction_chars`, a count, and `privacy.SAFE_FIELDS` enforces that a
second time at the emit boundary.

The judge ask honors the identical gate every other judged decision in this
package honors: with an external backend and `allow_external_state: false`, **no
call is attempted** and the decision abstains (`external_state_not_allowed`). See
docs/PRIVACY.md.

## Configuration

```yaml
delegation_routing:
  mode: shadow        # off (default) | shadow | enforce
  policy: v3          # shipped policy name, or a path to a JSON policy file
  deadline_ms: 750    # bound on the whole batched judge call
```

| Key | Default | Meaning |
|---|---|---|
| `mode` | `"off"` | `off`: inert. `shadow`: decide and record, change nothing. `enforce`: pin `provider_preferences` on the call. |
| `policy` | `"v3"` | `policies/delegation_v3.json`, or a path. An unloadable policy disables the feature rather than breaking the turn. |
| `deadline_ms` | `750` | 10..60000. Bounds the whole four-question ask. |

`None` (the default) and `{}` are both off. Unknown keys, an unknown mode and an
out-of-range deadline all raise at mount (`contracts.validate_delegation_routing`).

**`deadline_ms` and your backend.** Jev answers all four questions in one
`system_one` call. A backend without its own `ask_many` -- laya, ollama, mlx,
hosted -- is fanned out by `backends.ask_many` into **four concurrent** single
question asks, so the deadline must cover the slowest of four concurrent asks on
that backend, not one. 750 ms is the Jev-measured default (274-336 ms observed);
with local Laya, start higher -- `behaviors/fast-decisions.yaml` already runs
`timeout_ms: 3000` for its own judge asks -- and read the `duration_ms` on the
receipts before tightening it.

The policy ships as JSON, not YAML, because this package declares no runtime
dependencies and the file is read on a delegation's critical path.

## Events

One `fast_decisions:delegation_routed` per `delegate` call while `mode` is
`shadow` or `enforce`; none at all while off (inert, like every other opt-in
seam). Declared on `observability.events` via `contracts.EVENT_NAMES` and in
`schemas/event.schema.json`.

| Field | Meaning |
|---|---|
| `mode` | `shadow` / `enforce` |
| `delegation_policy` | the policy name that decided (`v3`) |
| `backend` | the judge backend asked (`service.backend.name`) |
| `agent` | the delegated agent's name |
| `model_role` / `role_source` | the role, and whether it came from the `call` or the agent's `agent_frontmatter` (a fallback chain is recorded as a list) |
| `anchor_source` / `anchor` | `resolver` or `agent_preresolved`, and the `{provider, model, config}` this delegation would have used without us |
| `answers` | `min_tier`, `min_tier_p`, `answer_depth`, `needs_computer_use`, `needs_vision` |
| `probabilities` | the full per-question distributions |
| `requirements` | the two requirement answers as booleans (`null` = the judge abstained; unknown blocks nothing) |
| `instruction_chars` | the LENGTH of the delegated instruction -- never the instruction |
| `proposed_preference` | what the policy picked |
| `actual_preference` | what was **actually** pinned on the call: `null` in `shadow` and on every abstain |
| `lever` / `nudge` / `move` | `effort`/`model`/`provider`, its receipt name, and `down`/`keep`/`up` |
| `guard` / `capability_conflict` | which guard fired, and any verified "this model cannot do X" that skipped a candidate |
| `action` / `reason` | `adjust` / `shadow` / `abstain`, and why |
| `duration_ms` / `latency_ms` | the judge call, and the whole decision |

A `judge_usage` receipt is emitted for the ask itself, exactly as for every other
judged decision point.

## How to try it safely

1. **Shadow first.** Set `delegation_routing: {mode: shadow}` and run a normal
   session. Nothing changes; every decision is recorded.
2. Read the receipts: how often `action` is `adjust` vs `abstain`, what
   `proposed_preference` would have done, and what `duration_ms` your judge
   backend actually costs.
   ```bash
   jq -c 'select(.event=="fast_decisions:delegation_routed") | .data
          | {action, reason, lever, anchor: .anchor.model,
             proposed: .proposed_preference.model, duration_ms}' events.jsonl
   ```
3. Only then `mode: enforce`, and compare the `actual_preference` on the receipt
   with the child's own `delegate:agent_spawned` / `session:config` records --
   the pin is only real if the child says so.

## Evidence

**Read this before turning on `enforce`.** The numbers below are exactly what was
measured; the limits are not hedging.

Policy v3 (the shipped one), measured on a paid A/B evaluation of 21 real
delegations drawn from 200 sampled ones, primary measure = **depth sufficiency**:

| Depth | n | Sufficient A / B | Cost per sufficient task |
|---|---|---|---|
| standard | 8 | 5 / 6 | **-62%** |
| thorough | 13 | 8 / 11 | **-63%** |

It moves about **22%** of delegations; the rest abstain.

Per-lever verdicts from the same evaluation (these are what the policy encodes):

| Lever | Verdict |
|---|---|
| Effort down (same model, `reasoning_effort: low`) | Safe at standard and thorough depth. **Never on a small-tier anchor** -- sample s134 (haiku) failed exactly this way, which is why `never_lower_effort_on_tiers: [small]` exists. |
| Provider down (opus -> OpenAI terra) | Standard depth only; a coin flip at thorough. |
| Model down (opus -> sonnet) | Standard depth only; failed at thorough (0/2). |
| Up | Only at p >= 0.85 (thorough) / 0.70 (standard); pays off when the anchor would have failed, at ~4-5x cost. |
| Small tier (Flash) and local models | Out for multi-step agent tasks; local targets removed. |

Observed depth mix over the 200 sampled delegations: thorough 165, standard 35,
**brief 0** -- which is why `brief` abstains rather than guessing.

### Honest limits

- **It FAILED its own pre-registered measure.** The evaluation was pre-registered
  on preference/acceptance, and on that measure v3 is `FAIL: quality`: acceptance
  0.857 -> 0.905, cost per accepted task -57%, n=21. The -62%/-63% figures above
  are the **secondary** depth-sufficiency measure, recomputed offline afterwards.
  Both are reported here because reporting only the favourable one would be the
  whole problem.
- **n=21.** Twenty-one delegations, split 8/13 across two depths. Nothing here
  supports a per-lever claim at any useful confidence.
- **A single, uncalibrated judge.** One model's answers, no calibration study.
  `min_tier_p` is a reported probability, not a measured accuracy.
- **Measured with Jev as the classifier**, not with this repo's current default
  judge -- see the open question below.
- **Not measured:** the decision mix in real shadow use, the computer-use
  conflict case in the wild, and the judge's own cost per delegation.

`tests/test_delegation_parity_v3.py` replays the recorded judge answers through
the shipped policy and asserts it reproduces the queue that actually ran: **20 of
21 matched, 1 divergence (s134) documented by name, 0 unexplained.** The
divergence is the policy refusing a move the evaluation proved harmful.

## Open questions

1. **Judge applicability.** Every number above was measured with Jev. The
   default behavior uses Jev with external-state consent enabled since #49.
   This feature uses the session's configured judge and honors its consent
   gate. Explicit Laya or other backend overrides need their own validation
   on these four questions; the Jev study does not establish their quality.
2. **Double routing.** A pin lands in the child as its session model. If the
   child also runs this orchestrator with `model_routing.start_policy`, its own
   turn-start router decides the child's start tier independently, and may route
   away from the pin. **No existing early return protects the pin today**: the
   explicit-user-model path (`RoutedProvider._user_selected_model`) fires on a
   `ui.model_override` session marker or on `default_model` changing
   *mid-session*; a delegation pin is the child's default from its first
   request, so neither signal fires. `DoubleRoutingTests` in
   `tests/test_delegation_routing.py` records both halves of that behavior. This
   PR deliberately does **not** change the child router: whether a delegation
   pin should suppress the child's start-tier decision (and if so, by which
   signal) is a call for the maintainer.
3. **Deadline under a fanned-out backend.** 750 ms was measured against Jev's
   single batched call. What it should be for a four-way concurrent Laya ask is
   unmeasured.
4. **Registry attachment.** Delegation routing is attached by
   `HybridOrchestrator` only. The registry mode introduced in #51 does not
   install this delegation facade; enabling `delegation_routing` there does
   not activate it. A separate integration and validation are required.
