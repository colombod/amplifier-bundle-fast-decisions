# Jevgrep source retrieval

The bundle mounts `jevgrep` by default through its main behavior for questions such as “where
are events buffered and flushed?” It calls the actual upstream `jg` CLI and
returns file references and source excerpts through Amplifier's ordinary tool
path. It does not bypass the host's tool hooks or select itself as a fast-path
action. It complements the AnyJev/Jev difficulty judge: the judge routes a
request; this tool locates source needed to work on it.

Reviewed upstream commit
[`24adac80dd57b673eafd8e8c477e4800b39c6c01`](https://github.com/dzhng/jevgrep/tree/24adac80dd57b673eafd8e8c477e4800b39c6c01).
The wrapper requires the published **`@dzhng/jevgrep@0.4.0`**, checked before
every search. A different version requires deliberate revalidation. The CLI
is MIT licensed and is installed separately; its source is not vendored here.

## Setup

Requires Node.js 22+ on macOS or Linux. Install the pinned CLI; the tool never installs software or changes saved credentials:

```bash
npm install --global @dzhng/jevgrep@0.4.0
jg --version
jg auth  # only needed if using a saved provider instead of TYPESAFE_API_KEY
```

`jg auth` uses a hidden local prompt. Do not paste keys into a model prompt.
The upstream CLI uses its saved provider and credentials under
`$XDG_CONFIG_HOME/jevgrep` (or `~/.config/jevgrep`); it does **not** read
`TYPESAFE_API_KEY`. When no saved credentials exist, the wrapper uses `TYPESAFE_API_KEY` in an owner-only temporary CLI configuration, deleted when the invocation finishes or is cancelled. A saved provider always takes precedence; no global credential file is created or replaced.
`jg doctor` sends a synthetic question to check provider access.
[Upstream authentication implementation](https://github.com/dzhng/jevgrep/blob/24adac80dd57b673eafd8e8c477e4800b39c6c01/apps/cli/src/auth.ts)

`behaviors/fast-decisions.yaml` includes this behavior, so both the root bundle
and the usual app behavior mount it. Source sharing is enabled in the shipped
Jevgrep behavior: searches send eligible source under the workspace root to the
saved provider, or to TypeSafe when using the existing environment key.

Disable retrieval source sharing for a workspace with:

```yaml
overrides:
  tool-jevgrep:
    config:
      allow_external_state: false
```

The lower-level Python tool constructor remains off unless configured explicitly.
The router's separate permission setting does not control retrieval. Update the
bundle and start a new session to load the tool; an existing session's tool list
is not rewritten in place.

Use the mounted tool with:

```json
{"query":"Where are telemetry events recorded and flushed?","path":"src"}
```

For a known symbol/path, ordinary grep or a direct read is usually sufficient.
The companion [context](../context/jevgrep.md) teaches that distinction and how
to handle incomplete results; no separate `jg skill` installation is needed
inside Amplifier. Other assistants can use upstream's skill separately.

## Boundaries and accounting

| Setting | Default | Meaning |
|---|---|---|
| `root` | `.` | Trusted workspace directory. Tool inputs may only narrow this scope. |
| `executable` | `jg` | Installed CLI on PATH, or trusted absolute executable path. |
| `allow_external_state` | `true` in shipped behavior | Permission to send source content; set false to opt out. |
| `timeout_ms` | `60000` | Deadline including version check and retrieval; range 100–120000. |
| `max_source_bytes` | `32768` | Source excerpt allocation passed to upstream, never unlimited. |
| `max_output_bytes` | `65536` | Entire response cap; excess output stops the invocation and marks it incomplete. |
| `concurrency` | `4` | At most this many concurrent provider requests, range 1–8. |

`max_source_bytes` limits **returned excerpts**, not source uploaded or total
provider spend. The upstream CLI has no per-search dollar/request-budget flag.
Timeout and concurrency bound execution, but do not constitute a spending cap.
Provider usage/cost is unknown to this wrapper and is not included in router
savings estimates. A future combined benchmark must count it separately.

The wrapper passes arguments directly, without a shell, and supports no flags
for broadening hidden/sensitive/dependency/ignore exclusions. Relative or absolute workspace
search directories cannot traverse outside the configured root or use symlink
components. Upstream handles source traversal and filtering inside that root.
It excludes common sensitive files and respects ignores, but this is not a
guarantee that eligible source contains no secrets. Scope the root accordingly.
[Upstream filesystem implementation](https://github.com/dzhng/jevgrep/blob/24adac80dd57b673eafd8e8c477e4800b39c6c01/packages/core/src/filesystem.ts)

The wrapper always requests `--no-cache` to avoid creating persistent retrieval
cache entries. Source is returned as a normal tool result; the host's existing
transcript/logging policy still applies. Stderr and raw failed-provider output
are not returned in errors. Unrelated API keys and Node preload settings are
not forwarded to the child environment. Cancellation stops the process group.

Exit 2/130, missing completion markers, or output truncation yield
`status: incomplete`; the agent may use the partial evidence but cannot infer
that unreturned code is absent. A source budget can also omit excerpts while
discovery completes: upstream's output labels those file locations as reading
leads. A failed/disabled tool does not automatically trigger another external
retrieval. The agent can continue with its ordinary search tools.

## Evidence and promotion

The originally reviewed upstream snapshot reported a cost reduction alongside a lower solve
rate and a failed quality gate. The current upstream README reports 8/10 solves
in both arms and 28.6% lower generative cost, explicitly excluding Jev charges.
Neither result establishes faster search for every task; exact symbols still use
ordinary grep. [Current upstream comparison](https://github.com/dzhng/jevgrep#what-we-measured). [Upstream results](https://github.com/dzhng/jevgrep/tree/24adac80dd57b673eafd8e8c477e4800b39c6c01#what-we-measured)

Verification includes wrapper tests, upstream synthetic-provider tests, and
actual Amplifier sessions launched through Forge. A real TypeSafe retrieval
returned the relevant public fixture source and a correct explanation; a
separate native hook denied execution. These are integration checks, not a
completed-task performance comparison. See the [Forge record](evidence/2026-09-27/forge/VERIFICATION.md). Default inclusion is a product choice, not a speed guarantee. Matched retrieval and prepared-action results are recorded in [the September 28 study](evidence/2026-09-28-decisions/REPORT.md), including failures and unknown retrieval charges.

See [verification](evidence/2026-09-27/jevgrep/VERIFICATION.md).
