# Laya deployment and decision-quality study

Measured September 29, 2026. [Interactive results](index.html).

**Laya is fast locally, but neither downloaded checkpoint qualifies as a general
replacement for Jev on these Fast Decisions screening tasks.** Hosting preserves
the decisions and adds network latency. No end-to-end agent savings or SWE-bench
result is established here. No model was fine-tuned during this study.

## Decision quality

| Checkpoint | Original screen | Fresh transfer screen | Fresh automatic decisions at p ≥ .75 | Wrong automatic decisions |
|---|---:|---:|---:|---:|
| Jev 1.13.0 | 56/60 (93.3%) | 29/30 (96.7%) | 26/30 | 1 |
| Laya base, local | 35/60 (58.3%) | 19/30 (63.3%) | 13/30 | 3 |
| Laya base, hosted | 35/60 (58.3%) | Not rerun | — | — |
| Laya typed-decisions, local | 34/60 (56.7%) | 22/30 (73.3%) | 0/30 | 0 |

The original screen has 20 prepared-action selections, 20 source-code relevance
judgments, and 20 computer-use target selections. The fresh screen has ten each.
These are text decisions over public, constructed observations, **not live browser
tasks or actual tool executions**. Labels were hand-authored, not independently
human audited. They are diagnostic tests, not a representative production estimate.

Each case was tested twice, reversing Choice-option order on the second pass.
Tables use **only the first pass**: repeats are not independent additional cases.
On the original 60, Jev changed one answer under this check, base Laya nine, and
typed Laya eight. Local and hosted base Laya agreed on all 120 responses.

The original manifest was frozen before querying any arm. After seeing base/Jev
results, we downloaded the specialized checkpoint and tested it without changing
instructions. The fresh 30-case manifest was frozen before inspecting specialized
checkpoint results. It is therefore an exploratory transfer screen, not a blind
independent benchmark. Neither screen may be reused as an unseen test set for
future tuning. All requests, labels, outputs and manifest hashes are included.

“Automatic” simulates one common raw-probability cutoff. Choice must select a
non-reason action at ≥.75; Noul uses max(p, 1−p) ≥.75. This is **not the actual
bundle policy** and does not simulate native authorization or structural guards.
The three wrong automatic base-Laya decisions on the fresh set are evidence
against trusting this cutoff alone. Zero accepted typed decisions establishes
zero coverage, not perfect useful safety. Jev also needs host guards and fallback.

The original screen deliberately includes 16 stale/ambiguous/side-effect or other
fallback cases. A **post-hoc diagnostic split**, excluding these and retaining all
source-code cases, gives Jev 43/44 and base Laya 33/44. This does not replace the
original score. Implement deterministic eligibility checks in the host, then ask
a model only questions requiring semantic judgment.

## Why public numbers differ

The [upstream model card](https://huggingface.co/convaiinnovations/laya#fine-tune-for-better-accuracy)
reports 36.2% for base English Laya versus 76.6% for its specialized typed-decisions
checkpoint on its own 2,000-decision benchmark. Its four workflows differ from
our code-search and computer-use tasks. Those vendor results do not establish
quality on this bundle. Our downloaded specialized checkpoint did not close the
gap. Treat domain-specific fine-tuning as an experiment, not a promised fix.

We audited tokenization for the original 60 cases: increasing budgets left all
encoded inputs unchanged; the longest was 127 tokens and no options collapsed.
The measured failure is not explained by truncation. The SDK warned about a
clamped calibration temperature for choices with 11+ options in the specialized
checkpoint; our Choice questions have three options, so that warning does not
explain their argmax errors. Adjusting confidence temperatures alone cannot repair
incorrect top-choice predictions.

## HTTP latency

| Transport | Endpoint | n | p50 | p95 | p99 | Errors |
|---|---|---:|---:|---:|---:|---:|
| Fresh connection, current client style | Local | 144 | 37.21 ms | 51.07 ms | 57.04 ms | 0 |
| Fresh connection, current client style | RunPod | 144 | 113.37 ms | 172.22 ms | 210.23 ms | 0 |
| Pooled connection, experimental | Local | 144 | 32.79 ms | 46.58 ms | 49.19 ms | 0 |
| Pooled connection, experimental | RunPod | 144 | 68.00 ms | 83.40 ms | 133.77 ms | 0 |

826 latency requests total: two first-observed calls, 24 warmups, 576 serial calls,
and 224 concurrent calls. Twelve fixtures span select/search/CUA and approximate
state sizes 200/600/1,500/2,800 characters. Serial runs repeat each fixture 12 times
per transport, alternating endpoint order and using identical matched payloads.
The fixture answers are simple positives; their success is not a quality result.

Hosted was slower in all 144 pairs per transport. Median paired hosted-minus-local
latency was 77.90 ms fresh and 35.77 ms pooled. Descriptive request-bootstrap 95%
intervals were [75.20, 83.39] and [34.19, 38.27] ms. Repeated fixtures, one client,
one pod and a short window limit generalization. Per-fixture n=12 is too small
for stable tail estimates. First-observed requests are not controlled cold starts.

Hosted serial server-header medians were 26.48 ms fresh and 19.82 ms pooled;
median per-request non-server remainder was 88.88 and 47.55 ms. The server timer
includes queue/thread/Python work plus inference, not just GPU kernels. The
remainder includes network, TLS, proxy and client processing. Medians of parts
need not sum to the median total. Local lacks that server timer.

| Concurrency | Local successes / requests | Local successes per active second | Hosted successes / requests | Hosted successes per active second |
|---|---:|---:|---:|---:|
| 2 | 16/16 | 36.34 | 16/16 | 16.32 |
| 4 | 32/32 | 34.24 | 32/32 | 32.72 |
| 8 | 64/64 | 27.28 | 46/64 | 35.01 |

At concurrency eight, hosted rejected 18 requests with HTTP 503 under its
configured four-inflight limit. Successful-request percentiles exclude errors;
failure counts remain visible. These short bursts are not a sustained capacity
test. No retries hid failures. Pooling is a measured experiment, **not a shipped
transport change**. Typed-checkpoint in-process latency is not compared to HTTP.

## Runtime and cost provenance

- Local: Apple M5 Max, 128 GiB RAM, existing PyTorch MPS service; Python 3.12.11,
  Laya 0.3.21, PyTorch 2.14.0, Transformers 5.17.0. Service not restarted.
- Hosted: US-NE-1, NVIDIA RTX PRO 6000 Blackwell Server Edition MIG 1g.24gb;
  PyTorch 2.11.0+cu128. Same base model weights and matching SDK agent source.
- Base model revision: `55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851`.
- Typed model revision: `1a793eb568e6718f15941d08f85432581df534e3`.
- Hosted image: `ghcr.io/michaeljabbour/amplifier-laya@sha256:05e611850d6c819993214aa2f6d4547249189694de0463d41e7954a89e83dc92`.
- Hosted support source: main `027bf0aaec31e480dedafba5adc92624604b483d`.
- Pod `772szw7680zjyh`: created 15:12:04 UTC, deleted 15:19:47 UTC;
  deletion returned 204, subsequent read 404. Historical endpoint is inactive.
- 463.11 seconds at $0.59/hour estimates **$0.0759 GPU cost**. This is not an
  authoritative invoice and excludes unresolved storage/API billing. A billing
  read returned no settled records; that does not mean zero cost.
- No ongoing pod or volume was retained. Existing shared services were untouched.

The study exposed a RunPod proxy compatibility issue: generic Python-urllib
User-Agent requests returned 403. The client and doctor probe now identify the
application; direct real-backend calls succeeded after the change. Authentication
checks returned 401 for absent/incorrect tokens and 200 for the valid token.
No auth files or credentials are included in this evidence directory.

## What tuning would have to prove

1. Keep deterministic validity, freshness, side-effect and permission checks in
   code. For exact paths and exact UI targets, avoid a model when a rule suffices.
2. Create separate domain datasets for prepared-action selection, source relevance
   and visible-control selection. Use reviewed labels, hard negatives, negation,
   misleading source text and option-order variation. Jev can propose labels;
   its errors show why it cannot be the unquestioned ground truth.
3. Split by repository/task/UI flow before training to prevent near-duplicate
   leakage. Reserve separate training, calibration and untouched test sets.
   The 90 cases here are now development data, never future holdout data.
4. Train the smallest candidate that helps, fit calibration only on the separate
   calibration split, and freeze per-task abstention thresholds before testing.
5. Promote a task only after enough accepted held-out decisions demonstrate the
   error target at useful coverage. A proposed initial screen is ≤1% error with
   a one-sided 95% confidence bound and ≥30% coverage; zero errors requires at
   least 299 independent accepted cases to bound error below 1%. This is an
   evaluation proposal, not proof of safety or a universal target.
6. Then run matched end-to-end Amplifier tasks and measure total latency, fallback
   overhead, calls and billed cost at equal task success. A 30 ms classifier that
   makes us redo work has no established efficiency benefit.

**No default was switched, no fine-tuning was performed, and no savings claim is
made.** Broad automatic Laya promotion is not supported by these results.

## Reproduction

From the repository root, with the matching dependencies, local service and an
authorized temporary hosted endpoint:

```sh
PYTHONPATH=src:. python -m evals.laya_latency --help
PYTHONPATH=src:. python -m evals.laya_quality --help
PYTHONPATH=src:. python -m evals.laya_transfer --help
```

Supply keys privately through environment variables. The deleted pilot endpoint
cannot be reused. Raw latency data are in `latency/requests.jsonl`, original
quality data in `quality/requests.jsonl`, the specialized repeat in
`quality/typed-exploratory.jsonl`, and fresh comparisons in `holdout/requests.jsonl`.
