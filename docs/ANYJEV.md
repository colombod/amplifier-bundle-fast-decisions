# AnyJev assessment and local integration

Reviewed 2026-09-27 against upstream commit
[`45add301a7aa60ed3420c83d15c061e84e5bce61`](https://github.com/nokia-applied-research/AnyJev/tree/45add301a7aa60ed3420c83d15c061e84e5bce61).

**Decision:** keep the existing measured Jev/rules default while making AnyJev
available as an opt-in, local, fixed-question judge. L2 is the most promising
new experiment: it can classify task difficulty from a fitted hidden-state
head without generating a reasoning answer. It is not yet established as the
best judge for our tasks. This change does not activate it in installed apps.

## What the current bundle already does

The current orchestrator asks a typed difficulty question once per request,
then routes to the cheaper model or the host model. It supports hosted Jev,
rules, Ollama, MLX, a hosted token scorer, and Laya. The default behavior uses
Jev, falling back to rules when unavailable. The read shortcut is disabled.
The local typed-question scorer already averages forward/reversed option
orders, using arithmetic means; the prepared-action scorer is separate.

This means we already have alternatives for **making decisions**, but the
selected generative model still performs the substantive coding/reasoning.
Changing the judge does not make those full tasks disappear.

## What is worth adopting from AnyJev

| Method | Mechanism | Relevance here |
|---|---|---|
| L0 | Cyclic option rotations, log-space averaging, optional prior correction | Stronger position-bias treatment than our two-order arithmetic average for multi-option questions; still uncalibrated and costs multiple prompts. |
| L1 | Temperature fitted on labeled examples of the same question | Useful when routing thresholds need measured uncertainty; cannot repair incorrect rankings. |
| L2 | A fitted linear head reading hidden states, with selectable model depth | The main candidate for a cheaper, genuinely nongenerative difficulty judge. Requires task-specific labels and compatible model features. |

The implementation and level contracts are documented in upstream
[levels](https://github.com/nokia-applied-research/AnyJev/blob/45add301a7aa60ed3420c83d15c061e84e5bce61/docs/levels.md)
and [decider.py](https://github.com/nokia-applied-research/AnyJev/blob/45add301a7aa60ed3420c83d15c061e84e5bce61/anyjev/decider.py).

We deliberately start with **prior correction off**. The proportion of easy
and hard requests can be very uneven; estimating a label prior from traffic
can mistake that real imbalance for bias. Upstream's own
[ablation study](https://github.com/nokia-applied-research/AnyJev/blob/45add301a7aa60ed3420c83d15c061e84e5bce61/docs/when_l0_helps.md)
documents substantial losses on some skewed tasks. `--prior batch` and
`--prior content_free` are explicit experiments, not automatic upgrades.

The upstream README also says its results score isolated decisions and that
agent-loop evaluation is unfinished. Some reported labels come from a teacher
model, and some competitor numbers were reported by their authors rather than
rerun. Those comparisons cannot select our production default by themselves.
[Source](https://github.com/nokia-applied-research/AnyJev/tree/45add301a7aa60ed3420c83d15c061e84e5bce61#-limitations)

## Implemented boundary

- A separate local server owns one model, shared across host sessions. Normal
  bundle installation does not install PyTorch or load model weights.
- The client accepts only literal loopback HTTP, uses no proxy, follows no
  redirects, bounds requests/responses and applies a deadline. Cancellation
  returns control to the host; a timed-out inference may finish on the server.
  The server rejects another inference while busy rather than queuing it.
- Choice and yes/no questions are supported. Prepared tool-action candidates
  and ordinal score questions are refused. Keep `read_shortcut: false`.
- The client checks the exact model identity, level, question hash, answer
  keys and probability distribution. Unavailable/malformed results enter the
  orchestrator's existing rules fallback; user model pins still take priority.
- Active mode refuses L0. L1/L2 require an artifact for the **exact** question
  and model snapshot. Merely having the same option labels is insufficient.
  Upstream can route a head across reworded questions with matching options;
  this adapter deliberately rejects that fallback for routing decisions.
- Online head adaptation is disabled. Fitted artifacts carry the preprocessing
  contract and system-prompt hash. No fitting states are exported. Token usage
  remains unknown rather than reporting invented zero-token savings.

L1/L2 describes the method and artifact requirement, not a correctness
certificate. Distribution shift and fitted-head quality still need evaluation.

## Run locally

From this checkout, use a separate environment and a pre-downloaded,
immutable Hugging Face snapshot directory. Only the server needs the extra.
It supports the upstream Transformers backend on CPU, MPS or CUDA. This first
integration does not expose upstream's vLLM pooling/depth-truncation pipeline.

```bash
uv venv .venv-anyjev --python 3.13
uv pip install --python .venv-anyjev/bin/python -e '.[anyjev]'

# Define AFAST_MODEL_SNAPSHOT as your local immutable HF snapshot directory.
# No fitting artifact is needed for an L0 evaluation server.
.venv-anyjev/bin/python -m amplifier_fast_decisions.anyjev_server serve \
  --model "$AFAST_MODEL_SNAPSHOT" --device mps --dtype float32 \
  --level L0 --served-model qwen-routing-study

# In another shell, score a PUBLIC labeled dataset with the existing probe.
PYTHONPATH=src python3 evals/difficulty/probe.py public-evaluation.jsonl \
  --judge anyjev:L0:qwen-routing-study --judge rules --out evaluation.json
```

`public-evaluation.jsonl` uses the existing probe schema:
`{"id":"case-1","source":"swe-verified","task":"...","label":"complex"}`.
The probe evaluates only decisions, not completed agent tasks. To reproduce
the small live integration check, which owns and stops its own server:

```bash
PYTHONPATH=src .venv-anyjev/bin/python evals/difficulty/anyjev_smoke.py \
  --model "$AFAST_MODEL_SNAPSHOT" --device mps --output smoke.json
```

For a fitted difficulty judge, collect a separate fitting-only JSONL file:
`{"state":{"task":"first 2500 characters of the request"},"label":"simple"}`.
Use at least 100 representative labels covering both classes. Do not copy
evaluation or holdout tasks into the fitting data. The fitter uses the exact
question constants from the orchestrator and the same state preprocessing.

```bash
.venv-anyjev/bin/python -m amplifier_fast_decisions.anyjev_server fit \
  --model "$AFAST_MODEL_SNAPSHOT" --device mps --level L2 \
  --dataset fitting-only.jsonl --artifacts difficulty-head.json

.venv-anyjev/bin/python -m amplifier_fast_decisions.anyjev_server serve \
  --model "$AFAST_MODEL_SNAPSHOT" --device mps --level L2 \
  --artifacts difficulty-head.json --served-model qwen-difficulty-v1
```

After evaluation, these are the settings for a host running **this source
revision** (an older installed module will not recognize `backend: anyjev`):

```yaml
overrides:
  loop-fast-decisions:
    config:
      backend: anyjev
      model: qwen-difficulty-v1
      anyjev_url: http://127.0.0.1:8091
      anyjev_level: L2
      allow_external_state: false
      read_shortcut: false
      timeout_ms: 3000
```

These settings use the existing difficulty-routing policy. Stopping the server
causes the existing rules fallback; remove the override to restore the shipped
Jev configuration. No user settings are changed by setup, fitting or tests.

## Evidence required before promoting AnyJev

1. Fit on representative, independently labeled requests, with fixed
   preprocessing, model snapshot, question, layer selection and artifact hash.
   Do not reuse upstream's unrelated bundled heads as our difficulty judge.
2. On untouched decision data, compare rules, current Jev, local Qwen and
   AnyJev L0/L1/L2. Measure complex-task false negatives, calibration, coverage,
   option-order stability, timeouts, and warm/cold p50/p95 wall time. Record all
   failed requests in the denominator and avoid selecting on the holdout.
3. Compare repeated **completed Amplifier tasks** with plain Amplifier and
   the current default, holding task/model/permission/test setup constant.
   Include long conversations, delegated sessions and repository-scale work;
   measure correctness, total time, actual calls, cache effects and cost.
4. Promote only if quality gates and task-level gains pass the existing
   [study protocol](../evals/STUDY-DESIGN.md). A method-level probability floor,
   a successful smoke, or a lower isolated scorer latency is not that result.

Test and live-smoke results are recorded in
[the verification record](evidence/2026-09-27/anyjev/VERIFICATION.md).
