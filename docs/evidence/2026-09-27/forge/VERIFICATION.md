# Forge / Amplifier acceptance

Run 2026-09-27 America/New_York against current main `ff343cc7d923ff5ce4fd4d4afac59ee8ab880b49` plus this integration, in an isolated release worktree. The earlier PR #38 checkout was preserved. [Sanitized session evidence](host-smoke.json).

- Full current-main regression through Forge: **1,144 tests, 49 skipped, zero failures**, 106.940 seconds. The earlier branch had additional evaluation tests, accounting for its different count.
- Installed Amplifier interpreter: **30 targeted tests, one optional AnyJev dependency skip, zero failures**. Covers the two adapters and actual installed core/loop contracts. Separate pinned AnyJev environment tests remain recorded in the earlier report.
- No-key synthetic demo passed through Forge.
- Actual Foundation CLI loaded the bundle root, optional jevgrep behavior, local module sources, and source package. Local overrides selected an isolated public workspace and disabled the observatory.
- Live TypeSafe retrieval: one native `jevgrep` call returned `queue.py`, complete and untruncated, in 1,483.36 ms. Anthropic Sonnet 5 correctly explained snapshot/write/clear ordering and preservation of pending events when the write raises. This is a single public-fixture acceptance task, not a comparative benchmark.
- Native denial: the real `tool:pre` hook returned deny; the callback was observed, no tool result/execution was recorded, and the model reported the denial without retrying. Early hook denial precedes the ordinary event logger, so the hook's own allowlisted observation was checked as well.
- AnyJev L2 mounted in that denial session. An intentionally unavailable loopback server produced the existing `rules_cheap` fallback; the session completed. This validates host loading and failure behavior, not calibrated AnyJev routing quality. L0 active mode remains rejected.

Host: Python 3.12.11, amplifier-core 2.0.1, Foundation 1.0.0, CLI 0.1.1, loop-streaming distribution 1.0.0, Anthropic SDK 1.8.0. The live model identity in provider receipts was `claude-sonnet-5`; the CLI's session default label can differ after routing, so it is not used as proof of the executed model.

## Reproduction and corrections

Forge skill: `~/.agents/skills/amplifier-skill-forge/SKILL.md`; run its `tools/forge.py doctor`, then `exec` the test commands from the release source directory:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src:tests ~/.local/share/uv/tools/amplifier/bin/python -m unittest test_jevgrep test_anyjev_backend test_upstream_contract -v
PYTHONPATH=src python3 -m amplifier_fast_decisions demo --record-only
```

The live profile/runner/public fixture are retained under `~/.amplifier/fast-decisions/studies/anyjev-20260927/forge-host/`. Runs use `amplifier run --bundle file:///absolute/profile.md --mode single --output-format json`, local `.amplifier/settings.local.yaml` with `bundle.app: []`, and `PYTHONPATH` selecting the candidate source. No global app bundle was replaced. Only the disposable source fixture was shared externally. Authentication used `jg auth --provider typesafe --stdin` with a temporary XDG directory removed on exit. Keys are absent from reports and were not saved as global Jevgrep credentials.

The first CLI attempt used a plain path instead of the required file URI. The first two denial attempts used a noncanonical test-hook package directory; the hook failed to mount and those attempts were rejected as denial evidence. Naming it `amplifier_module_hooks_fd_smoke_deny` fixed the test setup. The initial targeted unittest command used an unavailable `tests` package import; adding `tests` to PYTHONPATH fixed collection. None required a product-code workaround. Forge's one-shot observation timed out at 60 seconds during the full regression; the retained terminal was observed until the suite completed and then closed.

Jevgrep provider charges remain unknown to the wrapper. Reported generative costs exclude retrieval charges; no total-cost or speedup claim is made. Successful calibrated AnyJev routing still requires representative fitting data and held-out task evaluation. Broad steering/compaction acceptance is outside these targeted checks.
