# Shared Laya on RunPod

Status: serving adapter and container contract tested locally; no hosted deployment
or GPU performance claim yet. The existing laptop service is unchanged.

This service serves the same fixed English Laya checkpoint to the portable tool
(Codex, Claude Code, OpenCode and Amplifier) and the native Amplifier bundle.
It returns typed decisions; each harness retains approvals and tool execution.
It is not an OpenAI chat-completion endpoint and should not be registered as one.
Hosted access does not establish decision quality or savings; see the
[current measured results](evidence/2026-09-29-laya/VERIFICATION.md).

## Deployment contract

- Use a separate GPU pod with an HTTPS reverse proxy. Expose only 8090/http for
  inference; RunPod's proxy supplies TLS. Never expose `laya_server.py` publicly.
- Build `deploy/laya/Dockerfile` for Linux amd64 and deploy an immutable image
  digest. The Dockerfile pins the upstream Laya SDK commit, Torch version and
  model checkpoint revision. CUDA inference must actually stay on CUDA.
- Mount the hash-store directory at `/run/laya` read-only and a writable model
  cache at `/workspace/huggingface`. Container UID/GID is 10001; ensure both mounts
  have appropriate ownership. Do not mount plaintext client keys into the pod.
- Run **one process / one worker**. Inference is serialized, with four in-flight
  requests including queued work. Multiple processes duplicate weights and limits.
- Defaults: 600 requests/minute per client, 64 KiB body, 3,000 state characters,
  16 questions, 16 alternatives. Authenticated clients receive 429 on rate limits
  and 503 on capacity exhaustion. Body upload has a five-second limit; inference
  has a separate ten-second response deadline. A running inference keeps its slot
  after timeout/disconnection; this deadline does not kill a stuck GPU kernel.
- Require `GET /health` to report loaded=true, the pinned revision and cuda:0
  before onboarding. Health includes aggregate counters, never request content.
  Prompt, output and exception text are not logged by the adapter. TLS proxy and
  upstream library logging/retention need separate operational review.
- Change limits through `LAYA_MAX_INFLIGHT`, `LAYA_REQUESTS_PER_MINUTE`, and
  `LAYA_DEADLINE_SECONDS`. Do not raise limits before a concurrent workload test.

For a Linux host with Docker and the NVIDIA runtime:

```sh
docker build --platform linux/amd64 -f deploy/laya/Dockerfile -t afast-laya:reviewed .
export LAYA_AUTH_DIR=/srv/laya/auth
docker compose -f deploy/laya/compose.yaml up -d
```

Compose binds loopback only; add your HTTPS proxy. A RunPod template instead uses
the built image digest and 8090/http, with URL
`https://<pod-id>-8090.proxy.runpod.net`. Docker image publication, GPU boot and
public reachability are deployment gates, not consequences of the local build.

## Client keys

Issue one key per person or harness, without printing it:

```sh
python3 scripts/laya-client-key.py --client alice \
  --store /private/admin/laya-auth/clients.json \
  --token-file /private/admin/laya-keys/alice.token
```

The helper writes files mode 0600, refuses overwrites, and stores only SHA-256
hashes in `clients.json`. Use one administrator at a time. Deploy the hash store
with UID/GID 10001 and mode 0600; its directory needs traverse permission for that
UID (for example owner 10001, mode 0700). Send each plaintext key through your
existing private credential-distribution channel, never a repository or chat.

To revoke access, remove that client's entry and atomically replace `clients.json`
**inside the mounted directory**. Directory mounts allow replacement to take effect
on the next request. An empty or invalid store disables all decisions with 503.
Rotation uses a fresh random key; do not share the administrator's token.

## Harness configuration

Set these in each harness's launch environment (the URL has no `/v1` suffix):

```sh
export FAST_DECISIONS_LAYA_URL='https://<pod-id>-8090.proxy.runpod.net'
export FAST_DECISIONS_LAYA_TOKEN="$(cat /private/client/laya.token)"
amplifier-fast-decisions select --backend laya --allow-external-state --input request.json
amplifier-fast-decisions search --backend laya --allow-external-state --root . --input search.json
amplifier-fast-decisions cua --backend laya --allow-external-state --input cua.json
```

These are the same portable commands for all four harnesses. Search sends bounded
source excerpts; CUA sends the supplied bounded observed state. Review project
privacy before enabling external state. No screenshot or click executor is hosted.
Input contracts are in [SMART_TOOL.md](../src/amplifier_fast_decisions/SMART_TOOL.md).

For the native Amplifier bundle, use a project profile with `backend: laya`,
`laya_url` set to the HTTPS origin, and `allow_external_state: true` on each enabled
Fast Decisions module. `laya_token_env: FAST_DECISIONS_LAYA_TOKEN` is the default.
Do not put the token value in YAML. Keep routing budgets tight: network RTT counts
against them. On timeout, overload or uncertainty, the harness continues its
ordinary reasoning path. Do not silently change every user's global settings.

## Acceptance and operating cost

Before general availability, record the deployed code/image/checkpoint revisions,
GPU and region; verify missing/wrong/revoked tokens fail; then run the three tool
operations from each harness against public fixtures. Record accepted decisions,
abstentions, correctness, p50/p95 end-to-end latency and concurrent load failures.
Compare with local Laya and no-judge baselines, including network and server cost.

The 2026-09-29 US Secure Cloud catalog returned RTX 4090 at $0.74/hour with low
availability. That is $17.76/day or $532.80/30 days in GPU charges, before any
additional storage charges; it is not a reserved price or evidence of utilization.
Recheck stock and pricing before provisioning. A dedicated always-on GPU may cost
more than this small model warrants; benchmark CPU/serverless if demand is sparse.

Run contract tests without downloading weights:

```sh
python -m pip install -e '.[serve,local,test]'
python -m unittest discover -s tests -p 'test_laya_hosted.py' -v
python -m unittest discover -s tests -p 'test_laya_client_key.py' -v
docker build --target contracts -f deploy/laya/Dockerfile .
```
