# Jevgrep integration verification

This initial local record is supplemented by the later [current-main Forge acceptance](../forge/VERIFICATION.md), including real host loading, retrieval and native denial. Deployment statements below describe the initial check only.

Verified 2026-09-27 (America/New_York). Reviewed source at
`dzhng/jevgrep@24adac80dd57b673eafd8e8c477e4800b39c6c01`.
Executed the published npm package `@dzhng/jevgrep@0.4.0`, installed in an
isolated study prefix, with Node.js 22.22.0.

Registry integrity:
`sha512-i+veX7/V57RpmAmJeuji+zgagzBW0Py1usJGDmEuv/ruVMZbF9rzA/O7zvuyhyPQJoQwSF/Y6/ah1TtCgGirXg==`.

## Checks

- Final combined bundle regression: **1,175 tests run, 49 skipped, zero
  failures**, in 96.394 seconds. Optional upstream/host tests skipped in this
  dependency-free run are reported separately below and in the AnyJev record.
- **13 wrapper tests passed in the existing Amplifier Python environment**,
  no skips. Includes its actual `amplifier_core.models.ToolResult`, ordinary
  tool mounting against a coordinator double, published CLI version/auth
  failure behavior, disabled source sharing, path/symlink containment,
  argument construction, output limits, real process cancellation, incomplete
  results and sanitized errors. This is not a full session acceptance run.
  Host versions: Python 3.12.11, amplifier-core 2.0.1, Foundation 1.0.0,
  Amplifier CLI 0.1.1.
- In dependency-free Python, the same targeted run passed with the host
  envelope test skipped. The optional installed-CLI test was enabled by
  `AFAST_TEST_JEVGREP` pointing at the isolated published binary.
- **Five upstream installed-package fixture tests passed**, executing the
  published CLI against a local synthetic provider: Python declaration
  retrieval across all fixture branches; malformed answers preserving partial
  context with exit 2; interrupt stopping requests; excerpt budget preserving
  file leads; saved credentials taking precedence over environment variables.
- Upstream's test harness assumes Node is on its container PATH. On this Mac,
  an initial fixture run failed before execution because that PATH omitted
  Homebrew. A study-only launcher selected the absolute installed Node binary
  and executed the unmodified package entry point. The five passing tests use
  that launcher; this is not a Docker/Node-only portability validation.

All fixture credentials and repositories were synthetic. No real credential
was created/copied, no private workspace was sent externally, and no paid
retrieval was run. There was no existing Jevgrep auth directory. The npm
package was installed only in the study directory; it was not installed
globally or activated in Amplifier settings.

Detailed logs and the isolated package are under
`~/.amplifier/fast-decisions/studies/anyjev-20260927/`, particularly
`jevgrep-host-tests.log`, `jevgrep-tests.log`, and
`jevgrep-upstream-fixtures.log`. Source:
[`tests/test_jevgrep.py`](../../../../tests/test_jevgrep.py).

## Outstanding evidence

No authenticated real-provider retrieval, fully mounted Amplifier session,
or completed-task correctness/time/cost comparison is established here.
The tool remains an optional behavior with source sharing disabled. Native
host approvals remain on the normal tool path, and the tool is not added to
the fast-action allowlist. See [setup and limits](../../../JEVGREP.md).
