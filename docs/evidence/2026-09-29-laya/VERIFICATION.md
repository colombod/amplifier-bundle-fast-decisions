# Laya across four harnesses — live acceptance, September 29, 2026

**The installed smart tool ran all three capabilities from Codex, Claude Code,
Amplifier and OpenCode. Retrieval found the relevant fixture. Prepared-action
selection and computer-use selection abstained in every harness. No avoided
reasoning calls, dollar savings or end-to-end speedup are demonstrated.**

[Results page](index.html) · [Machine-readable evidence](summary.json)

## What was exercised

Real CLI sessions ran through the Amplifier + Forge skill. Each harness invoked
`select`, `search` and `cua` through the same installed executable, on public
fixtures in separate directories. This proves explicit invocation, not automatic
skill adoption or interception of those harnesses' internal reasoning loops.
Laya was a running local model service at loopback port 8090; the returned model
was `laya-rl-agent`. No AnyJev service or Jev API was used for these calls.

| Harness | Select judge | Search (3 windows) | CUA judge | Result |
|---|---:|---:|---:|---|
| Codex 0.157.1 | 83.65 ms | 107.48 ms | 51.73 ms | Abstain / found retry.py / abstain |
| Claude Code 2.1.284 | 74.70 ms | 96.84 ms | 56.24 ms | Abstain / found retry.py / abstain |
| Amplifier CLI | 68.34 ms | 125.50 ms | 51.54 ms | Abstain / found retry.py / abstain |
| OpenCode 1.18.30 | 71.86 ms | 131.64 ms | 88.55 ms | Abstain / found retry.py / abstain |

Timings are single observations per capability, excluding CLI startup. They are
not throughput, p95, or baseline comparisons. The whole harness runs took
18.64, 14.67, 33.10 and 45.92 seconds respectively; those include the ordinary
reasoning model. CLI startup and per-call wall times remain in `summary.json`.

The local `search` backend is bounded source relevance scoring with Laya, **not
upstream Jevgrep running on a different model**. The explicit `jev` backend still
uses upstream Jevgrep and requires source-sharing consent. Local retrieval reads
at most 32 KiB of eligible source, respects ignore rules and rejects hidden,
sensitive and symlink paths. Its returned matches can contain false positives or
miss relevant code; this tiny fixture is not an accuracy benchmark against grep.

## Native Amplifier bundle

Native `jevgrep`, `jev_cua` and `fast_workspace` tools ran in the actual isolated
Amplifier host. A subsequent run verified active `loop-fast-decisions` routing:
Laya judged the request `cheap` in 56.05 ms; the host was already using a cheaper
model, so it recorded `host_already_cheaper`. The completed turn made three
provider calls, zero fast actions and zero model switches. The default read
shortcut remains disabled; the portable selection gate remains 0.90 with a 0.20
margin. CUA retains its 0.75 gate.

The first minimal native profiles had a real loader failure: an implicit
filesystem tool source resolved to an installed directory whose fallback package
selection mounted `loop-streaming`, replacing Fast Decisions. The corrected
acceptance profile explicitly declares the filesystem source, as the normal
Foundation bundle does. The successful run verifies the corrected profile;
it does not claim to fix Amplifier's global module loader. Diagnostic attempts
are retained locally.

OpenCode initially failed with its configured Kimi model (404), then with an
unavailable local proxy and a provider-disallowed Sonnet model. A run using the
existing `runpod/Qwen/Qwen3.8-27B-FP8` configuration succeeded. No global OpenCode
provider settings were changed. Failed attempts are retained, not counted as
successful capability checks.

## Computer-use boundary

On a real browser fixture, Codex observed Home → Reports → Weekly → Laya and
verified the visible receipt `REPORT: LAYA-WEEKLY-42`. Each of the three Laya
proposals fell below the confidence gate. **Codex performed all three fallback
clicks; Laya executed zero actions.** This proves observation/proposal/fallback
integration, not autonomous Laya navigation or a reasoning-call reduction.

Separately, Codex computer control opened the requested N1 Incubator conversation
in Microsoft Teams. No Teams messages were sent. That was a host navigation
result, not evidence of Laya control; no Teams message text is in this report or
in the public fixture sent to the judge.

## Shipping scope and robustness

The bundle's primary behavior, retrieval behavior, CUA behavior and portable
selection now default to local Laya, with external sharing off. AnyJev remains
an explicit research adapter and is absent from the default path. Its weights
and the frozen SWE-bench inputs were preserved.

The installed portable CLI was rebuilt from this checkout. Canonical discovery
skills were updated for all four harnesses, with previous files backed up. A
project-local Amplifier override selects Laya. Global Amplifier settings and its
frozen benchmark host were not changed; already-running sessions require a new
session to pick up updated code/configuration. Other checkouts using cached
bundles must refresh their bundle source before inheriting the new default.

Robustness changes reject absent model identity, keep escaped source windows
within Laya's input bound, omit nested generated directories, and refuse
oversized CUA observations instead of silently truncating them. Host freshness,
execution permissions and completion verification remain mandatory.

## Reproduce

With the installed CLI, `rg`, and a healthy local Laya service:

```bash
python3 scripts/laya-smoke.py --output /tmp/laya-smoke-new
```

Run that command through each harness's native shell tool for invocation checks.
Use `--capability select`, `search`, or `cua` to separate the features. It uses
public fixtures, marks traffic as test, saves raw results, checks model identity
and requires retrieval of `retry.py`. A successful smoke can include abstention;
inspect the returned status before claiming an accepted action.

Native Amplifier acceptance additionally requires verifying `difficulty_judged`
and `turn_end` receipts from the actual orchestrator, rather than only the
presence of a module in a profile. Browser execution additionally requires fresh
host observations and post-action verification; the smoke never drives a desktop.

## Evidence and remaining gates

Raw public-fixture runs and private host diagnostics are retained under
`~/.amplifier/fast-decisions/studies/laya-cross-harness-20260929/`; committed
`summary.json` contains allowlisted results and SHA-256 fingerprints. Raw harness
logs are deliberately excluded because they may contain account/provider data.

The installed CLI passed all 16 checks in the upstream Smart Tool conformance
kit pinned at `70432044f26e2094b5894516adab68aa14f88592`. The final full suite in the actual Amplifier host ran 1,526 tests in 115.52 seconds
with zero failures and two optional skips. The rebuilt installed CLI then passed
the public smoke, with the same abstain / retrieval / abstain outcome. The results
page was opened in Edge and its matrix and reproduction disclosure verified.

Still unproven: reliable automatic use in Codex/Claude/OpenCode, accepted Laya
computer actions on real applications, broad retrieval quality, and savings at
equal task quality. The earlier SWE-bench campaign is separate and remains
`needs_reconciliation`; these acceptance runs neither complete nor restart it.
