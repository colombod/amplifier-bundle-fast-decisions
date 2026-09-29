# Real Amplifier acceptance through Forge — 2026-09-29

The bounded Jev-CUA driver completed a real browser workflow without reasoning
calls between clicks. Prepared actions also bypassed real provider calls. Laya
completed the coding tasks through the host model but did not pass the prepared
action confidence gate. AnyJev's local shadow and fallback boundaries worked.

Forty real sessions produced 38 accepted checks and two retained failed attempts
(described below). The generative-provider estimate across all attempts was
$3.5641; Jev/Jevgrep charges and local compute costs remain unknown.

These are small feature acceptance comparisons, not a completed SWE-bench result
or evidence that the project's production savings targets have been met.

## Method and retained evidence

Source under test: `60bc2adc2c8f426b2f14271fcd1d5f5c5dc82ace`.
All sessions used real `amplifier run` processes launched by Forge, real Foundation
module loading, native core envelopes/hooks, and Anthropic Claude Sonnet 5.
Native receipts recorded thinking enabled with a 32,000-token budget on both
sides, despite the orchestrator's `extended_thinking: false` setting. We report
the observed setting, not the intended one.

Disposable public file fixtures and an isolated Chromium report website were
used. Independent answer/code graders and browser state/history established
outcomes; the model's final prose was not the grader. Two repetitions reversed
arm order. Source, profiles, task hashes, raw session logs, failed attempts,
screenshots, usage and outcomes remain locally under:

`~/.amplifier/fast-decisions/studies/forge-acceptance-20260929/`

The portable [summary](summary.json) excludes private host context. Browser
comparisons use only `cua-confirm`'s four successful paired runs, whose harness
and fixture were copied and hashed before launch. Earlier `cua` trials are
exploratory and retained separately. Native and local caches were warm; the
background SWE-bench campaign shared the machine. These two-repetition means
are descriptive, with no significance or broad generalization claim.

## Browser comparison

Task: Reports → Weekly reports → Laya report; independently verify
`REPORT: LAYA-WEEKLY-42` and the three-click history.

| Mean per completed workflow | Plain Amplifier | Jev bounded driver |
| --- | ---: | ---: |
| Correct workflows | 2/2 | 2/2 |
| Generative provider calls | 9 | 3 |
| Wall time including host startup | 28.96 s | 23.82 s |
| Generative-provider estimate | $0.05590 | $0.04530 |
| Jev calls | 0 | 4 |

This is **67% fewer generative calls, 18% less wall time, and 19% less reported
generative-provider cost** in this fixture. It is not a total-dollar saving:
Jev usage is retained, but its billed cost is unknown. One paired driver run
cost more on the generative provider than its corresponding plain run. The
driver spends three Jev calls choosing clicks and a fourth proposing DONE;
the host verifies completion. All eight confirmation judgments named
`jev-1.13.0`. No generative call occurs inside that bounded sequence.

The driver was wrapped in an acceptance-only Amplifier tool with a fixture-scoped
authorization callback. Actual clicks went through Playwright Chromium and the
production `TryCuaHost` protocol adapter. This proves that adapter against a real
browser with a compatible `left_click` interface. It **does not** prove a real
trycua `Computer` VM, arbitrary desktop applications, or general browsing.
The shipped `jev_cua` tool remains proposal-only; the acceptance wrapper is not
silently installed as a production browser executor.

## Prepared actions and local Laya

All four read/repair tasks passed independently in each arm.

| Task / arm, two repetitions | Mean calls | Mean wall time | Mean generative-provider estimate | Receipted bypasses, both runs |
| --- | ---: | ---: | ---: | ---: |
| Audit-key answer / plain | 4.5 | 26.16 s | $0.08325 | 0 |
| Audit-key answer / Jev | 4 | 22.22 s | $0.04897 | 2 |
| Audit-key answer / Laya | 4 | 22.21 s | $0.05756 | 0 |
| Duration parser repair / plain | 6.5 | 61.47 s | $0.13448 | 0 |
| Duration parser repair / Jev | 5 | 49.07 s | $0.11682 | 2 |
| Duration parser repair / Laya | 6 | 71.15 s | $0.15571 | 0 |

Jev actually bypassed one provider call in every task. Native receipts show
prepared `fast_workspace` execution and subsequent correct answers/repairs.
Its four bypasses should not be confused with every between-run difference in
tool count or cost. Per-decision efficiency counterfactuals are estimates;
the table uses whole-run observations.

Laya's local scorer (`laya-rl-agent`) was genuinely invoked, but its selected
probabilities missed the 0.9 gate. In the first repair run it selected a different
read than Jev, with probability 0.3399 and margin 0.0741. That judgment took
53.8 ms versus Jev's 159.8 ms in the corresponding run. Faster scoring did not
produce a useful bypass. Lowering the gate on this evidence would be unjustified.
The host model's successful repair is not evidence that Laya's chosen action
was correct. These tests do not substantiate a general "7× faster" claim.

## AnyJev inside Amplifier

- Real local Qwen3-1.7B-Base snapshot `ea980cb0a6c2ae4b936e82123acc929f1cec04c1`,
  AnyJev L0, CPU float32: a native orchestrator shadow judgment completed in
  1,919 ms, followed by a correct audit-key answer. The start and host models were
  deliberately identical in this shadow test; no model savings are attributed.
- L2 requested against the L0 endpoint: the incompatible level was rejected,
  native `difficulty_judged` recorded `rules_cheap` with null probabilities,
  and Amplifier still answered correctly. This is a fallback test, not L2 inference.
- Active L0: actual Amplifier startup exited 1 with
  `AnyJev L0 is uncalibrated`; no answering-model invocation was needed.

No fitted, independently validated L1/L2 artifact exists in this acceptance
study. AnyJev is therefore not established as the best active router.

## Default Jevgrep

The composed default bundle exposed `jevgrep` without adding a separate retrieval
behavior. A real Foundation session called it once, received a successful native
tool result, read the relevant files, and answered correctly. Two additional
retrieval-required sessions also called it successfully and answered correctly.

The original four SQLite-module runs did not invoke Jevgrep at all, so they do
not measure it. The retrieval-required follow-up exposed another confound: its
first plain run searched for the unavailable executable and loaded a discovery
skill, taking 127 seconds. It must not be used to claim a Jevgrep speedup. Raw
logs stay local because the model's capability search inspected paths outside
the intended public fixture. No clean retrieval speed/cost conclusion is claimed.
The reusable prompt now defines availability as an already-listed callable tool
and explicitly forbids installing/discovering tools or leaving the fixture.

## Browser robustness and failures

Live Amplifier sessions verified native selector denial, per-action host denial,
an observation becoming stale during approval, an unchanged repeated action,
and host rejection of DONE. All stopped without additional browser actions.
The loop test allowed one no-op host click and stopped before a second identical
execution, regardless of the final model prose describing it as two clicks.
A real 100 ms Jev deadline returned a timeout, unknown judge usage, and no click.

Two failed acceptance attempts are retained, including their provider costs:

1. The complete Foundation selector session returned the correct `CLICK reports`
   in 309 ms. The reasoning model relayed its unchanged proposal after the
   15-second expiry. The host refused it. This exposes the overhead of routing
   every selector result back through a reasoning model; extending expiry would
   weaken freshness. The bounded driver was the successful execution path.
2. The first timeout fixture used an invalid 1 ms configuration. Its tool failed
   to mount, and Amplifier correctly said the browser tool was unavailable.
   The corrected, supported 100 ms case exercised the actual timeout path.

Native denials and failed browser cases are acceptance checks, not successful
browser-task examples and not savings observations.

## Harness fixes found through live use

`host_python` previously selected only the Forge worker's interpreter, while its
bare `amplifier` subprocess used Forge's global PATH. Both single- and multi-turn
workers now invoke the selected Python with `-m amplifier_app_cli`. They also set
`AFAST_TRAFFIC=test` and disable memory capture explicitly in the child environment.
Setting these only in the controller did not cross the Forge daemon boundary.

The first seven read/repair runs used the original host before the executable
fix; subsequent runs used the isolated copy. Their source/host provenance is
retained. Earlier isolated study efficiency records incorrectly say `production`;
they remain raw evidence and must be treated as test traffic. An independent
search found no study session records in the production event directory.
The SWE-bench host fingerprint matched its frozen manifest after these runs.
No frozen campaign model, task, source, deadline or grading input was changed.

Validation: 1,518 local tests passed with two optional-dependency skips; all 50
Forge regressions passed after the child-environment fix. The new browser fixture
is deliberately opt-in and never runs paid tests in ordinary CI.

## Reproduce the browser acceptance

Use a separate Amplifier environment with Playwright Chromium, copy/freeze this
script and `scripts/fixtures/forge_cua`, and run through the
Forge skill at `~/.agents/skills/amplifier-skill-forge/SKILL.md`.
The base profile is a minimal real Amplifier profile with provider, streaming
orchestrator and context modules; the script replaces its tools/hooks. For example:

```sh
python3 "$FORGE" doctor
python3 "$FORGE" exec \
  '/path/to/isolated/python scripts/forge_cua_acceptance.py --output /path/to/new-results --source /path/to/frozen-source --base-profile /path/to/base-profile.md' \
  --cwd /path/to/repo --timeout 300000
```

Provide credentials privately in the child shell, not in command arguments or
committed files. Forge's 60-second observation deadline can leave the session
running: retain its ID, inspect result files, and close only the completed owned
session. The acceptance runner refuses to overwrite an unreconciled attempt.
