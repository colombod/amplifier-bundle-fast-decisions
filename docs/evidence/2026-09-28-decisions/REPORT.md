# Default retrieval and measured decision alternatives

Jevgrep is now included and enabled by the main Fast Decisions behavior. The
installed CLI is pinned to 0.4.0. When no CLI provider is saved, the wrapper uses
`TYPESAFE_API_KEY` through a temporary owner-only credentials file and removes it
when the invocation ends. Existing saved provider choices take precedence.
Absolute directories inside the workspace are accepted without a corrective tool
retry; traversal, symlink and sensitive-path checks remain in force.

This adds semantic code retrieval to ordinary Amplifier sessions. It does not
replace every generative call. Exact symbols and known paths should still use
ordinary grep or direct reads. Prepared actions can replace individual model
turns, while a classifier chooses among supplied alternatives rather than
inventing a complete implementation.

## Completed task comparisons

Two repetitions per task/arm, reversed order, same public starting files,
`claude-sonnet-5`, extended thinking disabled, native Amplifier execution through
Forge. Every completed task passed independent checks. Means below include CLI
startup; model residency/download is excluded. These small fixtures establish
examples, not general speed or quality guarantees.

| Task / intervention | Plain seconds | Fast seconds | Plain model calls | Fast model calls | Plain estimated cost | Fast estimated cost |
|---|---:|---:|---:|---:|---:|---:|
| Follow code to audit key / Jev prepared read | 28.10 | 17.25 | 5 | 3 | $0.06366 | $0.03470 |
| Repair duration parser / Jev prepared read | 68.65 | 74.22 | 6.5 | 8 | $0.15963 | $0.16678 |
| Locate audit behavior / Jevgrep retrieval | 52.12 | 32.25 | 8.5 | 7 | $0.09530 | $0.07296 + unknown retrieval charge |

The first example was **39% faster, with 45% lower estimated cost**. Each Jev
run recorded one actual prepared-action bypass; the total model-call difference
was two per task. Those are different measurements. The repair averaged **8%
slower** despite a bypass in each run, because the subsequent agent trajectory
made more calls. Prepared actions therefore remain opt-in.

The semantic retrieval example averaged **38% faster and 23% less generative
cost**, but the ordinary runs varied from 28.8 to 75.4 seconds. Each retrieval
run actually invoked Jevgrep successfully; each also first tried an absolute
workspace path, which the old wrapper rejected. These retry costs remain in
the results. The path handling fix is subsequent to this measurement. Jevgrep
charges are unavailable, so this does **not** establish total dollar savings.

The initial exact-symbol task did not invoke Jevgrep despite its availability.
All four of those runs are retained in `results.json`; their timing difference
is not attributed to semantic retrieval.

## Laya

Downloaded checkpoint: `55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851`,
421,293,830 parameters, Apache-2.0; upstream `laya==0.3.21`, PyTorch MPS on this
Mac. The client now supports typed choice/noul batches. Confidence remains
model-reported, not calibrated probability.

On 12 hand-labelled operation-selection examples, two option orders and two
repetitions (48 calls per backend), Laya had **24 ms median latency versus Jev's
161 ms**, but **30/48 correct versus 48/48**. Thus about 6.7 times faster here
means a faster classifier call with weaker accuracy; it does not mean a task
completed 6.7 times faster. See `judges.json` for every case and result.

The first task series encountered a local MPS process abort while multiple
clients shared the model. Its four Laya runs fell back before scoring, so their
elapsed time is not evidence of Laya benefit. They remain in `results.json`.
The server now serializes inference across HTTP clients, with a concurrent-client
regression test. Eight real requests from four simultaneous clients completed,
and the server stayed healthy. In the separate matched task rerun, all four Laya
scorings completed; none cleared the unchanged 0.90 probability / 0.20 margin
gates. Consequently Laya bypassed **zero model calls** on these tasks. Faster
classifier latency did not translate into demonstrated avoided generation.
The four Laya tasks passed, including all 32 independent checks in each repair;
see [repaired-server task results](laya-results.json).

Raw rerun means were 28.16 vs 26.04 seconds for reading and 67.95 vs 52.22
seconds for repair (plain vs Laya). Because all four Laya selections abstained
and no model calls were bypassed, these differences are not attributed to Laya.
Local hardware/energy costs are not priced in the API-cost totals.

## Evidence and reproduction

- [Importable prepared-read trace](prepared-read-trace.json): choose **Open trace**
  in the observatory to inspect an actual Jev decision and native execution.
  This is isolated experimental traffic, not production savings.
- [Initial task results](results.json), including failures of the Laya service
  and the unused retrieval tool.
- [Actual semantic retrieval results](retrieval-results.json).
- [Classifier cases and raw measurements](judges.json).
- [Setup pilot accounting](setup-pilots.json): at least $0.12232 in recorded
  generative setup costs; missing logging means additional usage is unknown.
- [Method and limits](method.md) and [host environment](environment.json).

Native events/transcripts and manifests remain under
`~/.amplifier/fast-decisions/studies/alternatives-20260928-v6`,
`retrieval-20260928` and `laya-serialized-20260928`. Every graded run checks the
loaded runtime source hash, mode, protected files and independently evaluated
answer/repair. The lean harness was repaired to mount context and logging and
to recover the final answer from the canonical transcript when raw event text
is unavailable. Infrastructure failures now halt the batch before further paid
runs are launched.

```sh
# Uses configured provider keys; these are paid live tests.
PYTHONPATH=src python scripts/decision_alternatives_live.py --output /tmp/fd-alternatives
PYTHONPATH=src python scripts/decision_alternatives_live.py --retrieval-only --output /tmp/fd-retrieval
PYTHONPATH=src python scripts/decision_alternatives_live.py --laya-only --output /tmp/fd-laya
PYTHONPATH=src python scripts/decision_judges_live.py --output /tmp/fd-judges.json
python scripts/summarize_decision_alternatives.py /tmp/fd-alternatives --output /tmp/results.json
```

Use the actual Amplifier host interpreter and a running local Laya server for
these commands. Future runs can differ due to provider latency, model updates,
cache state and sampling. Source hashes and per-run receipts are the comparison
boundary, not the availability of a tool or a dashboard counter.

## Validation

The actual installed Amplifier host interpreter passed 1,469 tests (2 optional
skips), including native envelope/approval contracts and viewer browser checks.
The viewer JavaScript suite passed 30 tests. Concurrent local Laya smoke sent
eight real requests through four clients and checked server health afterward;
see [concurrency evidence](laya-concurrent.json).

The [default-bundle acceptance run](default-acceptance.json) composed the root
bundle without a retrieval-tool override, invoked Jevgrep once using an absolute
workspace path (1,896 ms, complete), and answered correctly. It recorded four
direct reads and one Jevgrep call, with matching runtime source and mode. This
verifies default availability and the retry fix; it is not a paired timing result.
