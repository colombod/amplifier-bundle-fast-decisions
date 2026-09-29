# Privacy, security and operating boundary

## Three different data paths

**Orchestrator default (`behaviors/fast-decisions.yaml`).** The turn-start difficulty router ships with
`backend: jev` and `allow_external_state: true`: when `TYPESAFE_API_KEY` is set, each top-level and
delegated turn sends one typed question with the first 2,500 characters of the turn's user request to the
Jev endpoint. Without the key, or on any Jev error or timeout, the turn falls back to the local length rule
and nothing is sent. In workspaces over `cheap_max_workspace_files` the turn starts on the host model
without asking. To keep everything local, set `backend: none` (or a local Ollama judge) with
`allow_external_state: false` in `overrides.loop-fast-decisions.config`. `afast rubric` sends the input
and output text you give it to the same endpoint. The observer-only behavior
(`behaviors/fast-decisions-shadow.yaml`) keeps the external path off, as described below.

1. **TypeSafe:** external requests are disabled by default. Opt-in sends the bounded current task snapshot, prepared candidate descriptions, and any contributed judgment questions (`fast_decisions.questions`) -- all batched into a single request. The state projection excludes system/developer messages, private thinking blocks, images and executable argument objects. Public user/tool/assistant text can contain confidential information. Labels, rationales and question instructions can also contain sensitive details. This applies identically to shadow measurement running in `hooks-fast-decisions`: with `allow_external_state: false` (the shipped default) no TypeSafe/Jev client is ever constructed for shadow scoring; the snapshot it reads comes from the mounted context manager, bounded by `shadow_max_messages` and `max_state_chars`, same bounds and same opt-in gate as the active path. The shadow-only model-role router (`role_router`, on by default in `behaviors/fast-decisions.yaml`) sends no additional state to TypeSafe -- it reads the `model_role_resolver` capability locally to enumerate live roles and never contacts an external backend itself.
2. **Local observatory:** stores allowlisted metadata, probability distributions, model/tool names, hashes, IDs and timing. It excludes raw prompts, arguments, tool outputs, exception messages and private reasoning. Metadata can still be sensitive. Review it before exporting.
3. **Other Amplifier modules:** their existing tracing/logging policies are unchanged. This bundle cannot promise that unrelated native loggers redact all content.

Scrubbing masks common key, bearer, secret-assignment and private-key patterns. **It is not a DLP system, prompt-injection defense or guarantee of anonymization.** Test only public/disposable workspaces initially. Review TypeSafe's data handling separately before using private company material. The SDK documents header redaction but not body redaction; do not enable HTTP/SDK debug body logging with private state.

4. **Hosted judge backend (`--backend hosted`, alias `gateway`):** an OpenAI-compatible judge host reached over HTTPS with an API key (any endpoint you control that returns `top_logprobs`), external exactly like TypeSafe and gated by the same `allow_external_state` consent check -- it is off by default and requires explicit opt-in (`--allow-external-state`; the shipped `evals/cells.yaml` hosted cell declares it explicitly). It receives the same bounded state as the local backends -- the compact task snapshot, prepared candidate descriptions and any contributed questions -- never raw file contents beyond the prepared excerpt already built for those backends. The API key itself is read from the environment variable named by `hosted_token_env` (alias `gateway_key_env`; default `FAST_DECISIONS_HOSTED_TOKEN`) and placed only on the outbound `Authorization` header for that one request; it is never a config field, never logged, and never appears in receipts, profiles, or the `afast doctor` hosted check (which reports `reachable`/`auth_failed`/`unreachable`/`not_configured`, never the token). See [Model setup](MODEL-SETUP.md#hosted-judge) for configuration and comparison against the local judge.

## Actions and authority

**Default Jevgrep retrieval:** the main behavior includes `behaviors/jevgrep.yaml`
with `tool-jevgrep.allow_external_state: true`. Searches send eligible source
under the configured workspace root to the provider saved by `jg auth`, or to
TypeSafe using `TYPESAFE_API_KEY` if no saved provider exists. Set the tool setting
to false to disable source sharing. Environment credentials use an owner-only
temporary CLI configuration deleted after execution; saved credentials are unchanged. The router's external-state setting does not enable source retrieval.
The wrapper disables retrieval caching, retains default source exclusions,
bounds output and stops child processes on timeout/cancellation. Retrieved source
is a normal host tool result and may appear in host transcripts. Returned-source
limits do not cap upload bytes or provider charges. See [Jevgrep](JEVGREP.md).

**Optional AnyJev judge:** `backend: anyjev` sends bounded, scrubbed state and
typed questions only to its literal loopback server. That server loads an
already-downloaded local model snapshot with remote code disabled. It logs no
requests or inference exception text and exposes no fitting or file-writing
HTTP endpoints. Its offline fitter exports model artifacts without raw states;
protect the fitting input separately. There is no online observation collection
or head adaptation. See [AnyJev setup](ANYJEV.md).

The bundled workspace tool supports read/list only, resolves paths under an explicit root, rejects symlink components, traversal, absolute paths, hidden/sensitive filenames, binary data and unsupported file types, bounds output and rechecks revisions. It is a convenience guard, not an OS-level sandbox against an adversary racing filesystem changes. Use a disposable checkout or container with appropriate file permissions.

Other tools require explicit allowlisting and a trusted validator. Those controls restrict routing eligibility, but native permission hooks are still required for actions that need them. Do not base mandatory authorization on an optional observer hook or a model confidence score. Do not treat the read-only default as authorization to enable writes globally.

## Viewer

The server binds only to 127.0.0.1, checks Host/Origin, applies a content-security policy and serves a fixed static asset allowlist. The ordinary loopback viewer is open to local browser tabs because it has no network listener and no execution, approval, configuration or write API. A caller that supplies an explicit token enables the optional bearer gate for tunneled or shared deployments. Loopback access does not defend against compromised local processes.

When the optional token gate is enabled, a token-authorized request sets an
HttpOnly, SameSite=Strict session cookie for that viewer port. The cookie is
local viewer access, never a model API key, and expires with the browser session
or becomes invalid when the viewer restarts with a new token.

`afast serve --study DIRECTORY` additionally reads the selected study's schedule,
state, completed results and root-session FD receipts. The API exposes only
counts, durations and fixed condition labels; it does not serve the study files,
prompts, solutions, native transcripts, full paths or grader output. Unit-test
receipts inside study workspaces are excluded by matching the actual session ID.

**Auto-observatory state file:** when the hook auto-starts the viewer (`observatory.enabled: true`, the default), it writes `~/.amplifier/fast-decisions/serve.json` (`--state-file` to override) containing the viewer's pid, port, URL, events directory, start time and version. This file is written mode `0600` via atomic tmp-file-plus-rename, and is removed on clean shutdown (`afast serve --stop`, or the server's own exit handler).

The configuration event carries `workspace_name`, the basename of the session's working directory (for example `my-repo`), so the viewer can name sessions; parent directories, home paths and full paths are never recorded. Set `workspace_name` in the hook config to override it.

**Source provenance:** the once-per-session `fast_decisions:source` event carries hashes, counts and version strings only -- a SHA-256 of the package's own source tree, an optional git commit sha, a file count, package/Python versions -- and never a filesystem path. See `provenance.describe_source` and docs/EVENTS.md.

The original logs use private file permissions where supported. The queue is bounded and best-effort, not a regulatory-grade audit ledger. Offline HTML and JSONL exports are deliberately shareable files and are not encrypted. Protect or delete them according to your organization's retention policy.

## Optional Jev computer use

`behaviors/jev-cua.yaml` separately opts in to sending a caller-sanitized UI goal,
summary and bounded control labels to TypeSafe. It does not capture screenshots or
read the desktop itself. Marked sensitive controls are excluded; callers must also
remove private text from the goal and summary. Native `cua_decided` telemetry keeps
model, operation, status, probabilities, latency, usage and a state hash, not UI
labels, values, coordinates, surface identifiers or goals. See [Jev-CUA](JEV-CUA.md).
