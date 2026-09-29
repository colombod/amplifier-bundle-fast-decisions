# Delegation routing: the study behind the design

This is the evidence that shaped `docs/DELEGATION-ROUTING.md` and the v3 policy in
`src/amplifier_fast_decisions/policies/delegation_v3.json`. It summarises a paired A/B study run
before any code was written here. Numbers are copied from the study's report; nothing below is
re-estimated. Limits are at the end: read them before quoting anything.

## 1. Question

The routing matrix picks one model per agent role. Could a fast classifier look at each
*delegation* (the task handed to a sub-agent) and move it to cheaper effort, a cheaper provider or
model, or a stronger model, at no loss of quality?

## 2. Stage 0: what delegations look like (no model spend)

- Corpus: **10,816** recorded delegations; **7,860** were `context_depth="none"` (self-contained,
  replayable). **200** were sampled for study.
- Each sampled task was classified by Jev: `min_tier` (small / mid / frontier, with
  probabilities) and `answer_depth` (brief / standard / thorough). Jev cost for all of stage 0:
  about **$0.022**.
- Depth split of the 200: **thorough 165, standard 35, brief 0.** Depth tracked what the task
  asked for (a quick lookup vs a full review), not domain difficulty. Brief depth was never seen,
  which is why the shipped policy abstains on it.

## 3. Method (pre-registered before any paid data)

- **Arms.** A = today's routing (role -> routing matrix -> model, no pin). B = same anchor ->
  classifier -> down / keep / up -> context-window fit -> explicit provider/model/effort pin.
  When B decides *keep*, A and B are identical and nothing is replayed.
- **Unit:** the completed task (one replayed delegation), never the single call.
- **Ground truth:** the child session's own events (`provider:resolve`, request/response,
  errors), not the delegate tool's own report of what it routed.
- **Quality:** one blind pairwise judge call per pair (arm labels stripped, order randomised):
  winner, and acceptable yes/no for each side. A side that errored or timed out is not acceptable.
- **Cost:** cost per accepted task, including retries, failed tool loops and the classifier's
  own calls; a B attempt that is not accepted is charged a full arm-A rerun on top.
- **Pass rule:** not worse on acceptance (sign test, 10-point margin), at least 20% lower cost per
  accepted task, and median wall time within 2x.

Total paid spend across all rounds: **$120.17** (replays $116.23, judge $3.93), under a
pre-set cap.

## 4. Round 1: which levers work (n=22, verdict PASS)

Acceptance A 0.727 -> B 0.864 (diff +0.136, 90% CI -0.091 to +0.364); W/T/L for B 10/1/11;
cost per accepted task **$1.7504 -> $1.1209 (-36%)**; median wall 263 s -> 199 s (0.76x).

| Move : lever | n | Accept A / B | W/T/L for B | Cost per accepted A -> B | Wall |
|---|---|---|---|---|---|
| down : effort (same model, low reasoning effort) | 6 | 0.833 / 1.0 | 2/1/3 | $1.49 -> $0.51 (-66%) | 0.49x |
| down : provider (opus -> another provider's mid model) | 6 | 0.5 / 0.667 | 3/0/3 | $2.68 -> $0.87 (-67%) | 0.75x |
| down : model (opus -> sonnet) | 5 | 1.0 / 0.8 | 1/0/4 | $2.35 -> $1.92 (-18%) | 0.74x |
| up : model | 5 | 0.6 / 1.0 | 4/0/1 | $0.25 -> $1.35 (+4.4x) | 2.3x |

Reading: effort-down and provider-down buy most of the saving; model-down saves little and lost
4 of 5 pairs; up is expensive but rescues tasks the anchor fails.

Exploratory (outside the decision rule): small-tier Gemini Flash accepted 0/2; a local 27B model
never finished (all runs hit the 1800 s limit). Small-tier and local targets were removed.

## 5. Round 2: policy v2 (effort/provider down only; up at p >= 0.85), n=11

Acceptance 0.818 -> 1.0; cost per accepted task **$2.0048 -> $0.9285 (-53.7%)**; median wall
0.49x. Verdict: *preliminary* (n=11 < 20 by its own stopping rule): PASS.

## 6. The depth finding

A blind *sufficiency* re-judge ("is this answer sufficient for the depth the task requires?",
yes/no per answer, no preference) was added to the evaluation model before any depth data. Of the
11 pairs first scored "A preferred, both acceptable", **10 of B's answers were sufficient at the
required depth**: the judge's preference for A was largely preference for depth nobody asked for.

## 7. Round 3: policy v3 (depth-aware), n=21

Pre-registered in full before data: standard depth -> effort / provider / model down on
tier-down, effort-down on tier-keep; thorough depth -> effort-down only, no move on keep, up only
at p >= 0.85. Primary quality measure: depth sufficiency.

| Measure | Result |
|---|---|
| Depth sufficiency, standard (n=8) | A sufficient 5, B 6; cost per sufficient task **-62%** |
| Depth sufficiency, thorough (n=13) | A sufficient 8, B 11; cost per sufficient task **-63%** |
| Acceptance (same run) | A 0.857 / B 0.905 |
| Cost per accepted task | $1.3003 -> $0.5562 (-57.2%), median wall 0.36x |
| Generic preference rule | **FAIL: quality** (W/T/L 4/3/14) |

v3 loses preference pairs while staying sufficient for the depth each task required, at well under
half the cost. It moves about **22%** of delegations; the rest route exactly as today.

The one recorded run that went worse (s134: effort-down on a small-tier haiku anchor; A
acceptable, B not) became a guard: effort is never lowered on a small-tier anchor.

## 8. From policy to code: checks

- **Offline parity (no spend).** Replaying the recorded stage-0 judge answers through the shipped
  policy reproduces the decisions the paid run executed: **20/21 matched, 1 deliberate divergence
  (s134, the guard above), 0 unexplained.** This is `tests/test_delegation_parity_v3.py`.
- **End-to-end in an isolated environment** (a standalone prototype of the same decision, installed
  from a tagged git URL into a clean Amplifier install; total $0.62):
  - standard-depth task, enforce: effort high -> low on the anchor; the pin was confirmed in the
    parent's `delegate:agent_spawned` and in the child's own `session:config`;
  - brief-depth task: abstain (by design); classifier key missing: abstain with no network call;
  - shadow: decision recorded, child ran unchanged; classifier latency 274-336 ms;
  - no task text in any decision event.

## 9. Limits

- **Small samples:** n = 22 / 11 / 21. The round-1 acceptance difference's 90% CI spans zero. The
  cost reduction is the strong signal; the quality difference is not established.
- **v2 is preliminary** (n=11 < 20).
- **One judge model**, low effort; its pre-registered calibration against a stronger judge is not
  reported.
- **Read-only agents only** (explorers, analysts, reviewers). Nothing here covers code-writing
  delegations.
- **Classifier:** every number was measured with **Jev**. The default behavior uses Jev since
  #49. Explicit Laya or other judge overrides remain untested on these four questions; these
  study results do not establish their quality.
- Each round is a different policy on a different sample; do not pool them.
- Exclusions are symmetric and listed in the report: infrastructure failures before any turn,
  pairs where both arms failed, and B runs whose pin did not take effect.
