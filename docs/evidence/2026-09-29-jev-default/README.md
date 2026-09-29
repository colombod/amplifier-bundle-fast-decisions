# Restore Jev as the default

The owner requested Jev after the [Laya quality comparison](../2026-09-29-laya-hosted/README.md).
The active bundle now chooses Jev, default source retrieval uses upstream
Jevgrep 0.4.0, and the optional computer-use behavior chooses Jev. The standalone
CLI/library follows those defaults. Laya remains available explicitly; AnyJev
remains outside the normal path. Standalone external-state consent is still
required, and native tool execution and permission checks are unchanged.

The standalone selector now uses the same pinned default, `jev-1.13.0`, as the
bundle and CUA selector, rather than overriding it with `jev-latest`.
Explicit model/environment overrides still work. Upstream Jevgrep retains its
saved-provider contract and separately managed model selection.

Three real calls through the portable CLI, with **no `--backend` override**,
used public fixtures and explicit `--allow-external-state`:

| Capability | Result |
|---|---|
| select | Jev 1.13.0 selected `readme`, probability .99, 253 ms scoring |
| search | Upstream Jevgrep returned complete context for `retry.py`, 1,818 ms |
| cua | Jev 1.13.0 proposed CLICK on observed `weekly`, one judge call, 194 ms |

Raw responses are adjacent JSON files. These are integration smoke checks, not
an additional quality benchmark or proof of avoided reasoning calls. No browser
action was executed. API keys stayed in the child environment and are not in
these artifacts. Search used the existing TypeSafe key with a temporary private
credentials file; no saved Jevgrep provider configuration existed.

The initial full local run completed 1,552 tests with three configuration
assertion failures and 11 optional skips. The stale assertions were corrected,
then the composition, smart-tool, Jevgrep and CUA suites passed. The no-key
synthetic demo also passed. CI reruns the complete suites before merging.

New sessions must load the refreshed bundle and skill catalog. Already-running
sessions keep their loaded configuration; this change does not restart them.
