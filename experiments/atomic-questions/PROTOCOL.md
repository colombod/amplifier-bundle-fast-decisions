# Parallel development experiment: atomic Jev questions

This experiment is separate from the running 432-session Codex/Amplifier study. Its purpose is to screen one concrete alternative cheaply before a later full-task comparison. It is a synthetic decision test, not evidence of faster completed coding tasks or production safety.

## Frozen comparison

Both treatments use Jev 1.13.0, identical state and candidate information, the same two-second request timeout, and the same connection lifecycle. The direct treatment uses the current adapter's next-action Choice question and option construction. The alternative sends six Nouls in one request: target match, requested operation match, and whether the action is still needed, for each of two candidates. Code takes each candidate's lowest signal, chooses it only at 0.90 or above with a 0.10 lead, and otherwise returns to reasoning. The direct treatment applies those same numerical gates to its Choice distribution, including the reasoning option. These are different scores, not equal calibrated risk levels. The alternative score is explicitly not presented as a calibrated probability. Thresholds and all questions are fixed before any live responses; no fitting or tuning occurs in this run.

The shared state explicitly defines a helper that directly fulfills read/list requests. It includes the candidate descriptions. This controlled envelope differs from natural host traffic. This is a mechanism comparison using the current question, not a replay of the whole installed smart tool or native orchestrator. Neither treatment executes any selected action or changes authorization.

## Cases and controls

There are 32 author-specified synthetic cases: 16 situation families with two filename variants each. Families include explicit and semantic read/list requests, corrections, negation, distractors, untrusted tool text, missing or ambiguous targets, write/delete requests, completed reads, acknowledgment, and remote targets. Eight families are assigned to development and eight to evaluation using seed 20260922. Both filename variants stay together. These are prospective author-defined evaluation cases, not an independently blinded dataset, not representative samples of real coding work, and not human-adjudicated production labels.

Development runs first, evaluation second. No modification or selection occurs between them. Each case runs under both treatments and both candidate-question orderings, for 128 API calls. The shared state's candidate listing retains its original order in both treatments; this checks question/option order sensitivity, not every serialization permutation. Treatment order is randomized by the seeded schedule, not chosen from outcomes. Family is the independent grouping; filename/order variants must not be represented as 32 independent new tasks per treatment per split.

A fixed exact-path/verb rule is scored on the same cases. A uniform random selector over both candidates plus abstention supplies an analytic chance reference, not a fitted model or a shuffled-Jev intervention. Both controls reveal how much of this small suite is simple matching. No current native-study task, output, or holdout is used to design this alternative.

## Outcomes and error accounting

Report correct decisions including correct abstentions, incorrect non-abstaining decisions, abstention frequency, provider/validation errors, and order consistency, separately by development/evaluation and treatment. Provider errors remain failures even when abstention is the expected answer. Preserve typed responses, model version, request hashes, start/end timestamps, latency, usage, and the concurrent native job. Latency is diagnostic only because this experiment is concurrent; do not claim a speed win. The small authored sample supports feasibility findings only, without headline calibration or statistical significance claims. A full-task evaluation on independent tasks is required before claiming harness benefit.

## Isolation and limits

Code, cases, schedule, protocol, and relevant adapter files are hashed before launch. The isolated worktree inherits the main checkout's current tracked changes, with their patch hash retained, but does not install anything or alter global skills/settings. No local model, GPU job, Codex session, Amplifier session, or shared observatory is launched. One Jev request is in flight at a time, with at least one second between requests. Before each request the runner waits while the native study reports an FD-Jev job. A native job can change during a request; start/end job IDs retain that overlap. Shared host/network effects cannot be eliminated by a worktree, so the overlap is documented rather than claiming physical isolation.

At most 128 requests, no retries, a six-hour wall limit, and a $1 usage/reserve ceiling drawn from the existing study's $50 Jev planning reserve. Unknown usage reserves $0.01 per request. Charges are estimates from returned input tokens at the published $0.042 per million, not invoices. Stop on authentication, throttling, overload, or model-version change; other failures remain in results. Credentials are loaded only into process memory from the authorized environment source, transmitted only in the Authorization header, and never logged. Only synthetic public content is sent.

`python3 experiment.py prepare` freezes the inputs. `python3 experiment.py run` performs collection. `python3 experiment.py analyze` produces descriptive reporting. A `STOP` file in this directory stops before the next call. Existing results require an audited resume; there is no automatic rerun or replacement of failures.
