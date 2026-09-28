# AnyJev integration verification

This initial local record is supplemented by the later [current-main Forge acceptance](../forge/VERIFICATION.md), including real host loading, retrieval and native denial. Deployment statements below describe the initial check only.

Date: 2026-09-27 (America/New_York). Source base: `c5ee79b` on
`feat/iron-triangle-tuning`, plus the working-tree AnyJev integration.
Upstream inspected and executed at `45add301a7aa60ed3420c83d15c061e84e5bce61`.

## Live local inference

[`live-smoke.json`](live-smoke.json) records actual Transformers/PyTorch model
inference, served over loopback and consumed by the new AnyJev bundle client.
Run: `evals/difficulty/anyjev_smoke.py`, Qwen/Qwen3-1.7B-Base snapshot
`ea980cb0a6c2ae4b936e82123acc929f1cec04c1`, MPS, float32, L0, prior correction
off. The model was already cached; no external inference service was used.
Python 3.13.12, AnyJev 0.1.0 at the pinned source commit, PyTorch 2.14.0,
Transformers 5.17.0, NumPy 2.5.3.

- Three public illustrative requests, both option orders, two repetitions:
  **12 requests, zero errors**.
- Probabilities were identical across option reversals and repetitions for
  these examples. The simple/complex ranking matched the illustrative cases.
- First request: **1090.22 ms**. Remaining 11: median **122.49 ms**, range
  **118.48–195.60 ms**, measured at the client. Model loading excluded.
- These are three hand-written examples, not an independently labeled quality
  test. L0 probabilities remain uncalibrated. No task was executed or graded.
  There was no matched live Jev comparison and no L1/L2 fit on real routing
  data in this verification.

SHA-256 of `live-smoke.json`:
`c5becf62a13cf26bad862c9b236dd0a586297dc0e79f0fad7113f62c1b5b0604`.

## Automated checks

- After the Jevgrep addition, the final combined regression also passed:
  **1,175 tests run, 49 skipped, zero failures** (96.394 seconds).
- The full regression suite passed after the final defensive mount-order
  guard: **1,162 tests, 47 skipped**. The skips include optional host/live
  dependencies and the upstream AnyJev test in dependency-free system Python.
- The final AnyJev-specific suite passes in the isolated upstream environment:
  **12 tests, no skips**. It exercises the real upstream Decider's L0/L1/L2
  implementations using synthetic logits/features, including fitting L1/L2
  artifacts. This establishes API compatibility, not model quality.
- Tests cover HTTP transport, batching, malformed distributions, mismatched
  models/questions/levels, exact head binding, cancellation, timeout, rules
  fallback, user model pins, and rejection of active L0 both at initial mount
  and when a shadow runtime is claimed by the orchestrator.
- The no-key synthetic demo passes. Source compilation and `git diff --check`
  pass. The earlier unprefixed baseline command failed to import the source
  package; rerunning with the documented `PYTHONPATH=src` resolved that setup
  issue (baseline: 1,150 tests, 46 skipped).

## Deployment boundary

The integration is in the working tree, not installed into Amplifier or
published. The existing installed Fast Decisions CLI was separately observed
at commit `cbe07f629b454080c273a814584ad4bfadc08426`; it was not replaced. User
settings were not changed. The live smoke server shut down after the run.

This verification does not establish an actual Amplifier host loading the new
backend, production approval/steering behavior with it, calibration on real
routing requests, or improvements in completed-task correctness/time/cost.
Promotion requires the evaluation described in [ANYJEV.md](../../../ANYJEV.md).

The isolated upstream checkout, environment and detailed test logs are under
`~/.amplifier/fast-decisions/studies/anyjev-20260927/` on the study machine.
