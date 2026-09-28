# Measured Amplifier example — September 27, 2026

**Fast Decisions completed this repair 46.1% faster at 63.3% lower estimated model cost.**
Both arms passed all 32 independent checks in all four repetitions (128/128 per arm).
This is one small, repeated development task; it does not establish general savings.

| Four runs per arm | Plain Amplifier | Fast Decisions |
|---|---:|---:|
| Mean elapsed time | 37.52 s | 20.24 s |
| Median elapsed time | 35.21 s | 19.70 s |
| Mean estimated cost | $0.743723 | $0.273033 |
| Total elapsed time | 150.09 s | 80.96 s |
| Total estimated cost | $2.974893 | $1.092134 |
| Generative provider calls | 18 | 19 |
| Routing judge calls | 0 | 4 |
| Tool calls | 23 | 23 |

## What the agents did

The starter `parse_duration(s)` incorrectly returned `len(s)`. Both agents had to
implement ordered optional integer hours, minutes, and seconds: `1h30m15s` must
return `5415`; malformed strings raise `ValueError`, and nonstrings raise
`TypeError`. See the public [specification](fast-1/README.md),
[plain solution](baseline-1/solution.py), and [Fast Decisions solution](fast-1/solution.py).

Plain Amplifier used the standard streaming loop and `claude-fable-5-1`.
Fast Decisions used the actual composed default bundle. In every FD run, Jev
classified the task as cheap and the loop requested `claude-sonnet-5`; native
provider response metadata confirms Sonnet. This example exercises cheaper-model
routing. It does not establish benefits from optional AnyJev, jevgrep, turn planning,
or read shortcuts. Waste-guard actions observed: 0.

## Every run, in execution order

| Run | Seconds | Estimated cost incl. judge | Model calls | Tool calls | Checks |
|---|---:|---:|---:|---:|---:|
| baseline-1 | 37.77 | $1.036296 | 4 | 5 | 32/32 |
| fast-1 | 18.82 | $0.360020 | 5 | 6 | 32/32 |
| fast-2 | 19.53 | $0.251472 | 5 | 6 | 32/32 |
| baseline-2 | 28.06 | $0.596665 | 4 | 5 | 32/32 |
| baseline-3 | 32.65 | $0.609797 | 4 | 5 | 32/32 |
| fast-3 | 22.75 | $0.252171 | 5 | 6 | 32/32 |
| fast-4 | 19.86 | $0.228470 | 4 | 5 | 32/32 |
| baseline-4 | 51.62 | $0.732134 | 6 | 8 | 32/32 |

No run was discarded or retried. The same 32 independently administered checks
(24 generated with evaluator seed 17, plus 8 boundary cases) were used in every
run. Manifest seeds are scheduling metadata and do not seed remote generation. Both arms used identical starting
workspace and prompt hashes within every pair. Source and mode receipts matched.

## Measurement boundaries

- Elapsed time runs from spawning `amplifier run` to process exit. It includes
  CLI startup and execution, but excludes Forge setup and post-run grading.
- Dollars are provider-reported model estimates plus Jev input usage at
  [$0.042 per million input tokens, output free](https://docs.typesafe.ai/models),
  verified September 27. These are not reconciled invoice charges.
- All native `llm:response` records are counted, including any background calls.
  Each FD run has one measured Jev attempt; no failed/unknown judge usage or cache
  keepalive refresh occurred. Full usage metadata is in [results.json](results.json).
- Cache writes and reads contribute to provider estimates. Cache state was not
  forcibly reset; alternating pair order reduces order bias but does not remove
  it. The first call in each arm includes more cache creation. Do not extrapolate
  the aggregate ratio to every task or cache condition.
- Independent checks run outside the agent workspace. Public spec/tests remained
  unchanged; public tests also passed. The fixture is an existing development
  example, not a fresh holdout. Four repetitions do not establish statistical
  confidence across workloads.

## Repeat it

Implementation tested: `c75499c197670fe101771ba544b6414269f6a651`. Run from this repository
with the installed Amplifier Python and Forge skill. The command below launches
**eight paid real sessions** using configured Anthropic and TypeSafe credentials:

```sh
~/.local/share/uv/tools/amplifier/bin/python \
  docs/evidence/2026-09-27-integration/run_example.py \
  --output /tmp/fast-decisions-matched-example
```

Use a new output path. Pass `--source /path/to/frozen/checkout` to compare the
recorded implementation exactly. Source, workspace, prompt hashes, per-run
profiles, native session references, and receipts are retained in that output.
The reference run used the [Amplifier Forge skill](/Users/michaeljabbour/.agents/skills/amplifier-skill-forge/SKILL.md).

Final integration validation: 1,463 installed-host tests passed (2 skipped),
plus the separate historical atomic study's 7 tests and viewer JavaScript tests.
The no-key demo and doctor check also passed. GitHub checks must pass before merge.
