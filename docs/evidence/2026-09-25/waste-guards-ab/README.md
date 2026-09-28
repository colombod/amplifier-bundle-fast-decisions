# Waste guards live A/B (2026-09-25)

`evals/waste_guards_live.py`: real `amplifier run`, Opus 5.5, plain Amplifier (no judge, no routing), this
checkout's orchestrator, `waste_guards` off vs on, 3 reps per arm, arm order alternated, fresh disposable
workspace per run, `AFAST_TRAFFIC=test`, dedicated events dir outside any repo. `summary.json` holds per-run
calls, provider-reported cost, guard actions and receipts (no prompts or outputs).

| Task | Cost off | Cost on | Measured off-on | Receipts (mean per on run) | Calls off → on | Quality off / on |
|---|---:|---:|---:|---:|---:|---:|
| monitor (poll a 3-min background build) | $0.3181 | $0.2623 | **$0.0558** | **$0.0514** | 9.33 → 5.67 (receipts: 3.33) | 3/3 / 3/3 |
| stable (same command 3 times) | $0.2659 | $0.2621 | **$0.0039** | **$0.0037** | 4 → 4 | 3/3 / 3/3 |
| flaky (identical failure, retries allowed) | $0.2323 | $0.2329 | -$0.0006 (noise) | $0 (never fired) | 3 → 3 | 3/3 / 3/3 |

Receipts reconcile with the measured differences within ~10% on the tasks where a guard fired, and quality is
unchanged (9/9 both arms). Wall time unchanged (monitor 214 s vs 212 s).

Notes:
* monitor: the poll wait fired in 2 of 3 on-runs (each 4 calls instead of ~9, receipts 5 calls / $0.077; measured
  vs the off mean: $0.083). In the third the model polled with `...; kill -0 PID && echo RUNNING`, which the
  read-only classifier did not accept then; fixed afterwards (`kill -0` is a probe) with a unit test, not re-run.
* flaky: Opus retried once and then reported the error, so the retry stop (third identical failure) never fired;
  it shows no harm. Consistent with the census: error loops are 0.2% of recent spend.
* stable: the second run's output was replaced by a pointer (709 tokens elided); the third, explicitly requested
  run was not blocked (repeat stop needs 3 identical runs first). A pilot with a 2-run limit blocked it, the
  model overrode, and the receipt charged the extra call (-$0.014 vs measured -$0.014): that limit was raised.
* Claude Code: the hook was exercised with `claude -p --settings <temp file>` (Haiku): the retry stop denied the
  third and fifth identical failure (one override, receipts written with `harness: "Claude Code"`), and the
  identical Bash output pointer was accepted via `updatedToolOutput`.
