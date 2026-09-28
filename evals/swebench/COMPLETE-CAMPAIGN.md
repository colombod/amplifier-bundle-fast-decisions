# Complete SWE-bench Verified comparison

The selected scope is 500 issues in each of four arms, one repetition: 2,000
agent runs. The campaign is not complete until each scheduled run has an agent
result and an official grading disposition. Empty patches are unresolved;
infrastructure errors remain unknown, never successful or silently excluded.

| Arm | Generative model | Treatment |
| --- | --- | --- |
| `plain-matched` | `claude-sonnet-5` | Ordinary Amplifier loop |
| `jev-prepared` | same | Jev selects bounded prepared workspace actions |
| `laya-prepared` | same | Local Laya selects those same actions |
| `jevgrep` | same | Semantic source retrieval through Jevgrep |

These are component comparisons. They do not measure the combined shipped
bundle, or substitute Laya for the model that writes patches. Prepared actions
can bypass individual model calls; Jevgrep can improve retrieval. Neither is
assumed to improve completed-task speed, cost or correctness.

All arms use identical issue prompts, foundation context, iteration limits,
extended thinking, and ordinary tools. Semantic retrieval is exposed only in
its treatment arm. Model routing, effort routing, cache keepalive and waste
guards are disabled in the two prepared-action arms. Thresholds stay fixed;
do not tune them on Verified outcomes. Task blocks and arm order are shuffled
with the recorded seed. Every arm gets a fresh base-commit checkout.

## Pinned inputs and accounting

- Dataset: `princeton-nlp/SWE-bench_Verified`, revision
  `c104f840cc67f8b6eec6f759ebc8b2693d585d4a` (500 test instances).
- Runtime candidate: `e1039f51115e4ee98f8c19febfca4e6b4778fccc`.
- Foundation: `89575c3482e3e8afe5a03df72e723cf815fa1f6c`.
- Official grader installed for preflight: `swebench 4.1.0`.
- Jev: `jev-1.13.0`; Jevgrep: `0.4.0`.
- Local Laya checkpoint revision:
  `55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851`.

The manifest records profiles, prompts, dataset revision, runner hash, runtime
source hash, installed host package fingerprints and user settings hash.
Changed inputs stop subsequent runs. Pinning Foundation's root does not by
itself pin every transitive dependency; resolved host fingerprints must remain
unchanged. Do not upgrade packages during this campaign.

Provider cost is the sum of native `llm:response` usage estimates. Jev and
Jevgrep input usage is included at $0.042 per million input tokens. The
benchmark-only Node preload records usage/status metadata without retaining
request content, headers or answers. A private per-run CLI configuration selects
the metered TypeSafe provider without modifying the user's saved provider.
Missing usage is unknown and stops additional paid launches. Local inference
hardware cost is unpriced. Estimates are not billing records.

Report resolved/planned, grading coverage, failures, empty patches, timeouts,
all provider calls, semantic-search calls, judge scores, fast submissions,
working time, wall time, total estimated cost and cost per resolved issue.
Compare paired task time/cost ratios against `plain-matched`; retain all
unsuccessful runs. Working time spans first request to last response; wall time
includes Amplifier startup. Image pulls and repository preparation occur before
timing. Fast submissions are mechanism evidence, not proof of net savings.
One repetition is a full-suite screen; repeat before making a production
performance claim under `evals/STUDY-DESIGN.md`.

## Execution

Use the actual Amplifier Python and the Forge skill. `prepare` spends no model
money. `--lazy` avoids pulling 500 images or cloning 2,000 workspaces in advance.
An explicit Docker endpoint keeps user Docker contexts unchanged.

```sh
PYTHONPATH=src ~/.local/share/uv/tools/amplifier/bin/python \
  evals/swebench/forge_swebench.py prepare \
  --root "$CAMPAIGN_ROOT" --instances all --dataset verified \
  --dataset-revision c104f840cc67f8b6eec6f759ebc8b2693d585d4a \
  --candidate-sha e1039f51115e4ee98f8c19febfca4e6b4778fccc \
  --baseline-source "$PWD" --arms plain-matched,jev-prepared,laya-prepared,jevgrep \
  --foundation-source git+https://github.com/microsoft/amplifier-foundation@89575c3482e3e8afe5a03df72e723cf815fa1f6c \
  --docker-host unix:///Users/michaeljabbour/.colima/afast-swebench/docker.sock \
  --seed 20260928 --reps 1 --lazy
```

Start and health-check the local Laya server before its first run. The selected
Colima VM has 8 CPUs, 24 GiB memory and a 200 GiB disk. Only the study parent
directory is mounted into it. Official x86 images run through emulation on this
Apple Silicon host. Agents run on the host in fresh checkouts; the Docker helper
isolates test environments, not the entire agent process. Prompts forbid external
solutions and benchmark files; gold patches/test metadata are kept in grading
files outside agent inputs. This is not a sealed leaderboard submission.

After the user supplies the campaign limit, run a four-arm live preflight before
launching the rest. Check served model identity, real tool use, Jev/Laya scoring,
actual prepared-action submissions, retrieval metering and official patch grades.
Report zero engagement honestly. Any repaired configuration starts a new frozen
campaign; do not mix attempts or delete paid receipts.

```sh
PYTHONPATH=src ~/.local/share/uv/tools/amplifier/bin/python \
  evals/swebench/forge_swebench.py run --root "$CAMPAIGN_ROOT" \
  --parallel 1 --max-cost-usd "$CAMPAIGN_CAP_USD" --reserve-per-run-usd 10 \
  --max-runs 4
```

This is a launch guard, not a hard billing limit: an in-flight run can exceed
the reservation. Only one controller can own the campaign. Resume uses the same
command and skips recorded results. Unknown costs or infrastructure failures
require reconciliation before resuming. The controller does not automatically
repair or discard failed attempts.
After inspecting and grading the four-run preflight, omit `--max-runs` to
continue through the full schedule under the same total limit.

```sh
PYTHONPATH=src ~/.local/share/uv/tools/amplifier/bin/python \
  evals/swebench/forge_swebench.py grade --root "$CAMPAIGN_ROOT"
PYTHONPATH=src ~/.local/share/uv/tools/amplifier/bin/python \
  evals/swebench/forge_swebench.py report --root "$CAMPAIGN_ROOT"
```

Grade IDs include prediction hashes to avoid stale grades after changed patches.
The pinned dataset is also supplied to the grader. Monitor VM disk usage and
grade completed blocks before removing their owned instance images; there is
no automatic image pruning. Never prune the user's Docker context or unrelated
containers. Full-campaign storage and sustained-run operation remain to be
validated during live preflight.

## Verified preflight evidence, 2026-09-28

Private evidence root:
`~/.amplifier/fast-decisions/studies/swebench-complete-20260928/preflight`.

- Official grader, `psf__requests-1142`: gold patch **resolved**, 0 errors.
- Empty-patch control: **0 resolved**, 1 empty patch, 0 errors.
- Initial automatic pull failed on ARM; explicit `--platform linux/amd64`
  pre-pull fixed it. The runner already uses this explicit pull.
- Live Jevgrep metadata metering: completed, estimated API cost **$0.000109326**.
- SWE-bench harness regression tests: **58 passed**.

No paid SWE-bench agent runs have started. The user selected Verified; the total
spending limit is still pending. A prepared manifest is not a benchmark result.
