"""Human- and agent-friendly entrypoints, with a zero-dependency offline demo."""

from __future__ import annotations

import argparse
import asyncio
import errno
import importlib
import importlib.metadata
import inspect
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .bench import (
    build_report,
    load_suite,
    percentile,
    render_markdown,
    replay_events,
    run_suite,
    write_jsonl,
)
from .bench.calibration import calibration_report, joined_pairs
from .bench.suite import DeterministicSuiteBackend, ForbiddenLabelSource
from .demo import run_demo
from .observatory import (
    build_id,
    pid_looks_like_viewer,
    read_state,
    remove_state,
    viewer_is_alive,
    write_state_atomic,
)
from .server import STATIC, EventIndex, ViewerServer

DEFAULT_EVENTS = Path.home() / ".amplifier" / "fast-decisions" / "events"
DEFAULT_SUITE = Path(__file__).resolve().parents[2] / "suites" / "v1.jsonl"
DEFAULT_STATE_FILE = Path.home() / ".amplifier" / "fast-decisions" / "serve.json"


DEFAULT_MLX_URL = "http://127.0.0.1:8080"


def _mlx_server_check() -> dict:
    """Read-only GET /health probe against a locally running mlx_lm.server.

    Never required (Apple Silicon + mlx-lm is one of two supported local
    judge hosts, alongside Ollama); an unreachable/absent server is reported
    ``ok: False`` with a short reason, never raised.
    """
    url = os.getenv("FAST_DECISIONS_MLX_URL", DEFAULT_MLX_URL)
    try:
        req = urllib.request.Request(f"{url}/health", method="GET")
        with urllib.request.urlopen(req, timeout=1) as response:
            ok = response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        ok = False
    except Exception:  # noqa: BLE001 -- a probe must never raise
        ok = False
    return {
        "check": "mlx_server",
        "ok": ok,
        "value": url,
        "note": "GET /health on the configured mlx_lm.server; not required unless using --backend mlx",
    }


def _hosted_server_check() -> dict:
    """Read-only GET {base}/models probe against the configured hosted
    judge backend (any OpenAI-compatible endpoint that returns
    ``top_logprobs``, e.g. a LiteLLM/vLLM deployment). Never required;
    state is one of ``reachable``, ``auth_failed``, ``unreachable`` or
    ``not_configured`` -- the token value itself is never reported, only
    whether it authenticated.
    """
    from .local_backend import HOSTED_DEFAULT_TOKEN_ENV, HOSTED_DEFAULT_URL

    url = (
        os.getenv("FAST_DECISIONS_HOSTED_URL")
        or os.getenv("FAST_DECISIONS_GATEWAY_URL")  # legacy alias
        or HOSTED_DEFAULT_URL
    )
    if not url:
        return {
            "check": "hosted_judge",
            "ok": False,
            "value": None,
            "state": "not_configured",
            "note": "Set hosted_url (config, alias gateway_url) or "
                    "FAST_DECISIONS_HOSTED_URL (env, alias FAST_DECISIONS_GATEWAY_URL) "
                    "to your OpenAI-compatible host; not required unless using "
                    "--backend hosted.",
        }
    url = url.rstrip("/")
    key_env = (
        os.getenv("FAST_DECISIONS_HOSTED_TOKEN_ENV")
        or os.getenv("FAST_DECISIONS_GATEWAY_KEY_ENV")  # legacy alias
        or HOSTED_DEFAULT_TOKEN_ENV
    )
    api_key = os.getenv(key_env)
    state = "unreachable"
    try:
        req = urllib.request.Request(f"{url}/models", method="GET")
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=2) as response:
            state = "reachable" if response.status == 200 else "unreachable"
    except urllib.error.HTTPError as exc:
        state = "auth_failed" if exc.code in (401, 403) else "unreachable"
        exc.close()
    except (urllib.error.URLError, TimeoutError, OSError):
        state = "unreachable"
    except Exception:  # noqa: BLE001 -- a probe must never raise
        state = "unreachable"
    return {
        "check": "hosted_judge",
        "ok": state == "reachable",
        "value": url,
        "state": state,
        "note": f"GET {{base}}/models using ${key_env}; not required unless using "
                "--backend hosted. The token value is never reported.",
    }


def _gateway_server_check() -> dict:
    """Legacy alias for :func:`_hosted_server_check`."""
    return _hosted_server_check()


def _laya_server_check() -> dict:
    """Read-only GET /health probe against the configured Laya decide
    server (laya_server.py). Never required (opt-in decide backend); state
    is one of ``reachable``, ``auth_failed``, or ``unreachable`` -- a token
    value, if configured, is never reported, only whether it authenticated.
    """
    from .local_backend import LAYA_DEFAULT_TOKEN_ENV, LAYA_DEFAULT_URL

    url = os.getenv("FAST_DECISIONS_LAYA_URL") or LAYA_DEFAULT_URL
    token_env = os.getenv("FAST_DECISIONS_LAYA_TOKEN_ENV") or LAYA_DEFAULT_TOKEN_ENV
    token = os.getenv(token_env)
    state = "unreachable"
    try:
        req = urllib.request.Request(
            f"{url}/health", method="GET",
            headers={"User-Agent": "amplifier-fast-decisions/0.1"},
        )
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=1) as response:
            state = "reachable" if response.status == 200 else "unreachable"
    except urllib.error.HTTPError as exc:
        state = "auth_failed" if exc.code in (401, 403) else "unreachable"
        exc.close()
    except (urllib.error.URLError, TimeoutError, OSError):
        state = "unreachable"
    except Exception:  # noqa: BLE001 -- a probe must never raise
        state = "unreachable"
    return {
        "check": "laya_judge",
        "ok": state == "reachable",
        "value": url,
        "state": state,
        "note": f"GET {{base}}/health using ${token_env}; not required unless using "
                "--backend laya. The token value is never reported.",
    }


def doctor(require_amplifier: bool = False) -> int:
    checks: list[dict] = []
    checks.append(
        {
            "check": "python",
            "ok": sys.version_info >= (3, 11),
            "value": sys.version.split()[0],
        }
    )
    required_ok = True
    for module in ("amplifier_core", "amplifier_module_loop_streaming", "typesafe_sdk"):
        try:
            imported = importlib.import_module(module)
            ok = True
            value = getattr(imported, "__version__", "importable")
        except Exception as exc:
            ok, value = False, type(exc).__name__
        checks.append({"check": module, "ok": ok, "value": value})
        if module != "typesafe_sdk" and not ok:
            required_ok = False
    if required_ok:
        try:
            from amplifier_module_loop_streaming import StreamingOrchestrator

            from .contracts import Candidate
            from .orchestrator import action_response

            action_response(
                Candidate(
                    "doctor",
                    "Doctor",
                    "fast_workspace",
                    {"operation": "list", "path": "."},
                ),
                "doctor",
            )
            instance = StreamingOrchestrator({})
            params = inspect.signature(instance.execute).parameters
            required_ok = all(
                p in params
                for p in ("prompt", "context", "providers", "tools", "hooks")
            )
            checks.append(
                {"check": "real_envelopes_and_execute_signature", "ok": required_ok}
            )
        except Exception as exc:
            required_ok = False
            checks.append(
                {
                    "check": "real_envelopes_and_execute_signature",
                    "ok": False,
                    "value": type(exc).__name__,
                }
            )
    try:
        importlib.import_module("amplifier_core._engine")
        checks.append({"check": "rust_extension_importable", "ok": True})
    except Exception:
        checks.append(
            {
                "check": "rust_extension_importable",
                "ok": False,
                "note": "Not required for the offline demo",
            }
        )
    checks.append(
        {
            "check": "TYPESAFE_API_KEY_present",
            "ok": bool(os.getenv("TYPESAFE_API_KEY")),
            "note": "Value is never displayed",
        }
    )
    checks.append(_mlx_server_check())
    checks.append(_hosted_server_check())
    checks.append(_laya_server_check())
    entries = {
        e.name for e in importlib.metadata.entry_points(group="amplifier.modules")
    }
    checks.append(
        {
            "check": "module_entry_points",
            "ok": {"loop-fast-decisions", "hooks-fast-decisions", "tool-fast-workspace"}
            <= entries,
            "value": sorted(e for e in entries if "fast-" in e),
            "note": "Provided by the modules/* packages, which the Amplifier loader installs when the bundle is loaded; absent in a bare source checkout",
        }
    )
    print(
        json.dumps(
            {
                "version": __version__,
                "checks": checks,
                "note": "Import/schema checks are not a full Rust-kernel or live-Jev integration test.",
            },
            indent=2,
        )
    )
    return 1 if require_amplifier and not required_ok else 0


def export_html(directory: str | Path, output: str | Path):
    index = EventIndex(directory)
    index.scan()
    events = list(index.events)
    if not events:
        raise ValueError("No valid events found. Run demo --record-only first.")
    html = (STATIC / "index.html").read_text()
    css = (STATIC / "style.css").read_text()
    script = (STATIC / "app.js").read_text()
    embedded = json.dumps(events, ensure_ascii=True).replace("<", "\\u003c")
    html = html.replace(
        '<link rel="stylesheet" href="style.css">', "<style>" + css + "</style>"
    )
    html = html.replace(
        '<script src="app.js"></script>',
        "<script>window.AFAST_EMBEDDED="
        + embedded
        + ";</script>\n<script>"
        + script
        + "</script>",
    )
    output = Path(output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html)
    print(str(output.resolve()))


def bench_replay(args) -> int:
    events_path = args.events_path or args.events
    if not events_path:
        raise ValueError("Pass an events directory/file positionally or via --events")
    try:
        result = asyncio.run(replay_events(events_path, session_id=args.session))
    except FileNotFoundError as exc:
        raise ValueError(str(exc)) from exc
    report = build_report(
        "replay",
        result,
        session_id=args.session or "replay",
        model="jev-latest",
        provider="typesafe",
    )
    if args.out:
        write_jsonl(report, args.out)
    if args.md:
        Path(args.md).expanduser().write_text(render_markdown(report), encoding="utf-8")
    if args.json:
        print(json.dumps(report, sort_keys=True, allow_nan=False))
    else:
        print(render_markdown(report))
    return 0


class _TimingBackend:
    """Wraps any backend to record per-call wall-clock ``ask()`` latency.

    Bench-suite CLI wiring only -- does not touch bench/suite.py's
    ``SuiteItemResult`` shape or the suite format. ``run_suite`` calls
    ``ask()`` once per case (canonical order) plus once per extra
    permutation, in that fixed order; ``latencies_ms`` is a flat,
    call-ordered record the caller can slice back into per-case groups of
    size ``permutations`` (see ``_augment_suite_report``).
    """

    def __init__(self, inner):
        self._inner = inner
        self.name = getattr(inner, "name", "timed")
        self.external = getattr(inner, "external", False)
        self.latencies_ms: list[float] = []

    async def ask(self, request):
        start = time.perf_counter()
        try:
            return await self._inner.ask(request)
        finally:
            self.latencies_ms.append((time.perf_counter() - start) * 1000.0)

    async def close(self) -> None:
        await self._inner.close()


def _augment_suite_report(
    report: dict,
    timed_backend: "_TimingBackend",
    permutations: int,
    *,
    errors: int = 0,
    calibration_pairs: list[tuple[float, bool]] | None = None,
) -> dict:
    """Add per-case decision latency (p50/p95, canonical order only), an
    explicit ``agreement_with_expected`` alias, and an ``errors``/
    ``error_rate`` pair to a suite report, in place. Bench-suite CLI wiring
    only; ``report`` is the plain dict returned by ``build_report``, mutated
    here rather than in bench/report.py. ``errors`` counts per-case
    ``TimeoutError``/``BackendUnavailable`` outcomes that ``run_suite``
    recorded as abstentions rather than aborting the suite (see
    bench/suite.py's ``SuiteResult.errors``). ``calibration_pairs`` (each
    case's canonical chosen-option probability, agreement with
    ``expected_choice``), when given, adds a top-level ``calibration``
    key: ECE/bins plus the gate-coverage curve from
    ``bench.calibration.calibration_report`` -- the same shape
    ``afast bench calibrate`` prints from judged receipts."""
    k = max(1, permutations)
    canonical_latencies = timed_backend.latencies_ms[0::k]
    report["decision"]["decision_latency_ms_p50"] = percentile(canonical_latencies, 50)
    report["decision"]["decision_latency_ms_p95"] = percentile(canonical_latencies, 95)
    report["accuracy_proxy"]["agreement_with_expected"] = report["accuracy_proxy"].get(
        "agreement_rate"
    )
    n_observed = report["accuracy_proxy"].get("n_observed") or 0
    total_attempts = errors + n_observed
    report["accuracy_proxy"]["errors"] = errors
    report["accuracy_proxy"]["error_rate"] = (errors / total_attempts) if total_attempts else 0.0
    if calibration_pairs is not None:
        report["calibration"] = calibration_report(calibration_pairs)
    return report


def bench_suite(args) -> int:
    suite_path = args.suite_path or args.suite
    try:
        cases = load_suite(suite_path)
    except ForbiddenLabelSource as exc:
        print("afast bench suite: " + str(exc), file=sys.stderr)
        return 2
    if args.domain:
        cases = [c for c in cases if c.domain == args.domain]
    live_requested = args.live
    both_gates = bool(
        os.getenv("FAST_DECISIONS_LIVE") == "1" and os.getenv("TYPESAFE_API_KEY")
    )
    hosted_token_env = (
        os.getenv("FAST_DECISIONS_HOSTED_TOKEN_ENV")
        or os.getenv("FAST_DECISIONS_GATEWAY_KEY_ENV")  # legacy alias
        or "FAST_DECISIONS_HOSTED_TOKEN"
    )
    hosted_api_key = os.getenv(hosted_token_env)
    hosted_gates = bool(live_requested and hosted_api_key)
    # Legacy names, same values -- kept for readability at call sites below.
    gateway_key_env = hosted_token_env
    gateway_gates = hosted_gates
    backend_name = args.backend
    model_arg = getattr(args, "model", None)
    if live_requested and backend_name == "jev" and both_gates:
        from .backends import DEFAULT_JEV_MODEL, JevBackend

        backend = JevBackend(model=model_arg)
        backend_external = True
        model_name = model_arg or DEFAULT_JEV_MODEL
    elif backend_name in ("hosted", "gateway") and hosted_gates:
        from .local_backend import HostedBackend

        if not model_arg:
            print("afast bench suite: --backend hosted requires --model", file=sys.stderr)
            return 2
        backend = HostedBackend(model=model_arg, url=getattr(args, "hosted_url", None), api_key=os.getenv(hosted_token_env))
        backend_external = True
        model_name = model_arg
    elif backend_name == "ollama":
        from .local_backend import OllamaBackend

        model_name = model_arg or "qwen3:0.6b"
        backend = OllamaBackend(model=model_name)
        backend_external = False
    elif backend_name == "mlx":
        from .local_backend import MLX_DEFAULT_MODEL, MlxBackend

        model_name = model_arg or MLX_DEFAULT_MODEL
        backend = MlxBackend(model=model_name)
        backend_external = False
    elif backend_name == "laya":
        from .local_backend import LayaBackend

        backend = LayaBackend(url=getattr(args, "laya_url", None))
        backend_external = backend.external
        model_name = model_arg or "laya-rl-agent"
    else:
        if live_requested and backend_name == "jev" and not both_gates:
            print(
                "afast bench suite: --live requires both FAST_DECISIONS_LIVE=1 and "
                "TYPESAFE_API_KEY; running the offline deterministic backend instead.",
                file=sys.stderr,
            )
        elif live_requested and backend_name in ("hosted", "gateway") and not hosted_gates:
            print(
                "afast bench suite: --backend hosted requires --live and "
                f"{hosted_token_env} to be set; running the offline deterministic "
                "backend instead.",
                file=sys.stderr,
            )
        backend = DeterministicSuiteBackend()
        backend_external = False
        model_name = "deterministic-suite-backend"
    warmup_fn = getattr(backend, "warmup", None)
    if callable(warmup_fn):
        try:
            asyncio.run(warmup_fn())
        except Exception as exc:  # noqa: BLE001 -- warmup is best-effort; a genuinely
            # broken backend still fails on the first real ask() inside run_suite.
            print(f"afast bench suite: warmup failed ({exc}); continuing", file=sys.stderr)
    timed_backend = _TimingBackend(backend)
    result = asyncio.run(run_suite(cases, timed_backend, permutations=args.permutations))
    report = build_report(
        "suite",
        result,
        session_id="suite-" + Path(suite_path).stem,
        model=model_name,
        provider="typesafe" if backend_external else "offline",
        backend_external=backend_external,
    )
    calibration_pairs = [
        (item.canonical_probability, item.correct) for item in result.items
    ]
    report = _augment_suite_report(
        report,
        timed_backend,
        args.permutations,
        errors=result.errors,
        calibration_pairs=calibration_pairs,
    )
    swing = report["accuracy_proxy"]["max_probability_swing"]
    if isinstance(swing, (int, float)) and swing > 0.15:
        print(
            "afast bench suite: WARNING max_probability_swing="
            + f"{swing:.3f} > 0.15 -- thresholds may be measuring candidate "
            "serialisation, not the task.",
            file=sys.stderr,
        )
    if args.out:
        write_jsonl(report, args.out)
    if args.md:
        Path(args.md).expanduser().write_text(render_markdown(report), encoding="utf-8")
    if args.json:
        print(json.dumps(report, sort_keys=True, allow_nan=False))
    else:
        print(render_markdown(report))
    return 0


def bench_calibrate(args) -> int:
    """``afast bench calibrate``: join judged-decision receipts (the same
    telemetry ``bench replay`` reads, ``decision_id`` ->
    ``scored.selected_probability``) with an external label file
    (``decision_id`` -> ``correct: bool``), and print the same
    ECE/gate-curve report ``bench suite --json``'s ``calibration`` key
    carries -- so real judged data, not the synthetic suite, can set the
    gates (see docs/BENCH.md, docs/EVIDENCE.md)."""
    try:
        pairs = joined_pairs(args.receipts, args.labels)
    except FileNotFoundError as exc:
        print("afast bench calibrate: " + str(exc), file=sys.stderr)
        return 2
    report = calibration_report(pairs)
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


def stop_server(state_file: str | Path) -> int:
    """``afast serve --stop``: read the state file, signal the pid, remove it.

    Exit 1 with a clear message only when there is nothing running (no state
    file). Otherwise best-effort SIGTERM (a pid that is already gone is not
    an error -- the end state, "no viewer running", is already achieved) and
    always remove the state file, exit 0.

    Before signalling, check the pid's own command line
    (``pid_looks_like_viewer``) -- a state file can outlive the process it
    names, and if the OS has since recycled that pid for an unrelated
    process, blindly signalling it would kill the wrong thing. When the
    check identifies a clear mismatch, the signal is skipped (the stale
    state is still removed either way).
    """
    state = read_state(state_file)
    if state is None:
        print(
            f"afast: no viewer running (no state file at {state_file})", file=sys.stderr
        )
        return 1
    pid = state.get("pid")
    was_alive = viewer_is_alive(state)
    if isinstance(pid, int):
        if pid_looks_like_viewer(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError as exc:
                print(f"afast: failed to signal viewer (pid {pid}): {exc}", file=sys.stderr)
        else:
            print(
                f"afast: pid {pid} no longer looks like a viewer process "
                "(likely pid reuse); skipping signal.",
                file=sys.stderr,
            )
    remove_state(state_file)
    status = "was" if was_alive else "was not (already stopped)"
    print(f"Stopped viewer (pid {pid}); it {status} responding before the signal.")
    return 0


def _load_module_bundle_yaml(path: Path) -> dict:
    """Parse a module-source bundle file: plain YAML, or YAML frontmatter (.md).

    Requires PyYAML, which is not a runtime dependency of the zero-dependency
    offline demo path (doctor/demo/serve/export/bench) -- only ``configure``
    needs it, and only to read this project's own behaviors/*.yaml and
    bundles/*.yaml so their module ``source:`` values are never duplicated
    (and therefore never drift) in cli.py.
    """
    try:
        import yaml
    except ImportError as exc:
        raise ValueError(
            f"configure requires PyYAML to read {path}; "
            "install it with `pip install pyyaml`"
        ) from exc
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".md":
        match = re.match(r"^---\n(.*?)\n---\n?", text, re.DOTALL)
        if not match:
            raise ValueError(f"No YAML frontmatter found in {path}")
        text = match.group(1)
    return yaml.safe_load(text)


def configure(args) -> int:
    root = Path(args.bundle_root or Path(__file__).resolve().parents[1]).resolve()
    if not (root / "bundle.md").is_file():
        raise ValueError("Pass --bundle-root pointing to the extracted source bundle")
    backend = getattr(args, "backend", None) or (
        "jev" if args.mode == "active" or args.allow_external_state else "deterministic"
    )
    if args.mode == "active" and backend == "jev" and not args.allow_external_state:
        raise ValueError(
            "Active Jev requires --allow-external-state; review docs/PRIVACY.md first"
        )
    workspace = Path(args.workspace).expanduser().resolve(strict=True)
    events_dir = str(Path(args.events).expanduser().resolve())

    # Foundation + the observer-only behavior rather than bundle.md: the root
    # bundle's behavior now carries its own orchestrator config, which would
    # otherwise deep-merge into (active) or replace (shadow/off) this
    # profile's intended orchestrator.
    root_yaml = _load_module_bundle_yaml(root / "bundle.md")
    foundation = next(
        inc for inc in root_yaml["includes"] if "amplifier-foundation" in inc["bundle"]
    )
    data = {
        "bundle": {"name": "fast-decisions-" + args.mode, "version": __version__},
        "includes": [
            dict(foundation),
            {"bundle": (root / "behaviors" / "fast-decisions-shadow.yaml").as_uri()},
        ],
        "tools": [
            {"module": "tool-fast-workspace", "config": {"root": str(workspace)}}
        ],
    }

    if args.mode == "active":
        # Active swaps the orchestrator, exactly as bundles/active.yaml does.
        # Read it rather than hardcode it so the two never drift.
        active_yaml = _load_module_bundle_yaml(root / "bundles" / "active.yaml")
        orchestrator = dict(active_yaml["session"]["orchestrator"])
        config = dict(orchestrator.get("config") or {})
        config.update(
            {
                "backend": backend,
                "allow_external_state": args.allow_external_state,
                "events_dir": events_dir,
                "timeout_ms": args.timeout_ms,
            }
        )
        orchestrator["config"] = config
        data["session"] = {"orchestrator": orchestrator}
    else:
        # shadow/off: no orchestrator swap (P3) -- shadow measurement lives on
        # hooks-fast-decisions and composes onto whatever orchestrator is
        # already mounted. Re-declare the hook (same source the behavior
        # uses) with our overrides; compose()'s merge_module_lists deep-merges
        # by module id with the later (this profile's) declaration winning.
        behavior_yaml = _load_module_bundle_yaml(
            root / "behaviors" / "fast-decisions-shadow.yaml"
        )
        hook_entry = next(
            h for h in behavior_yaml["hooks"] if h["module"] == "hooks-fast-decisions"
        )
        config = dict(hook_entry.get("config") or {})
        config.update(
            {
                "mode": args.mode,
                "backend": backend,
                "allow_external_state": args.allow_external_state,
                "events_dir": events_dir,
                "timeout_ms": args.timeout_ms,
                "role_router": True,
            }
        )
        data["hooks"] = [
            {
                "module": "hooks-fast-decisions",
                "source": hook_entry["source"],
                "config": config,
            }
        ]

    if getattr(args, "model", None):
        config["model"] = args.model
    if backend == "ollama":
        config["ollama_url"] = args.ollama_url
        config["max_state_chars"] = 2048
    if getattr(args, "local_sources", False):
        for entry in data.get("hooks", []) + data["tools"]:
            entry["source"] = (root / "modules" / entry["module"]).as_uri()
        if args.mode == "active":
            orchestrator["source"] = (root / "modules" / orchestrator["module"]).as_uri()
            # Override the included behavior's remote hook source as well.
            data["hooks"] = [{"module": "hooks-fast-decisions", "source":
                              (root / "modules" / "hooks-fast-decisions").as_uri()}]

    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    # JSON is a YAML subset: no extra dependency needed for valid frontmatter.
    output.write_text("---\n" + json.dumps(data, indent=2) + "\n---\n")
    print(str(output.resolve()))
    print(
        "This only wrote a local bundle profile. It did not modify your installed app or send data."
    )
    return 0


def _read_rubric_requests(text: str) -> list[dict]:
    text = text.strip()
    if not text:
        return []
    if text[0] in "[{":
        try:
            loaded = json.loads(text)
            return loaded if isinstance(loaded, list) else [loaded]
        except json.JSONDecodeError:
            pass
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def rubric_command(args) -> int:
    from .backends import JevBackend
    from .rubric import score_many

    text = sys.stdin.read() if args.requests == "-" else Path(args.requests).read_text(encoding="utf-8")
    requests = _read_rubric_requests(text)
    if not os.getenv("TYPESAFE_API_KEY"):
        print("afast rubric: TYPESAFE_API_KEY is not set", file=sys.stderr)
        return 2
    backend = JevBackend(model=args.model, timeout_ms=args.timeout_ms)

    async def run():
        try:
            return await score_many(backend, requests)
        finally:
            await backend.close()

    results = asyncio.run(run())
    print(json.dumps(results, indent=2))
    return 1 if any("error" in r for r in results) else 0


def _since_day(value: str | None) -> str | None:
    if not value:
        return None
    if value.endswith("d") and value[:-1].isdigit():
        from datetime import timedelta
        return (datetime.now(UTC) - timedelta(days=int(value[:-1]))).date().isoformat()
    datetime.strptime(value, "%Y-%m-%d")
    return value


def hooks_command(args) -> int:
    """``afast hooks install|uninstall|show|run claude-code``."""
    from . import hooks_install

    if args.action == "run":
        from .claude_hook import main as hook_main
        return hook_main([args.mode or "pre"])
    target = args.settings or str(hooks_install.DEFAULT_SETTINGS)
    if args.action == "show":
        settings = hooks_install.install(target, python=args.python, dry_run=True)
        print(json.dumps({"hooks": settings.get("hooks", {})}, indent=2))
        return 0
    if args.action == "install":
        hooks_install.install(target, python=args.python)
        print(f"Installed the fast-decisions waste guards (PreToolUse, PostToolUse, Stop) in {target}.\n"
              "Receipts go to ~/.amplifier/fast-decisions/events (harness: Claude Code); "
              "see them with: afast efficiency. Remove with: afast hooks uninstall claude-code"
              + (f" --settings {target}" if args.settings else ""))
        return 0
    hooks_install.uninstall(target)
    print(f"Removed the fast-decisions hooks from {target} (other hooks kept).")
    return 0


def efficiency_command(args) -> int:
    from .efficiency import LEVER_LABELS, summarize

    report = summarize(args.events, include_test=args.include_test, since=_since_day(args.since))
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    t = report["totals"]
    print(f"Efficiency receipts: {t['receipts']} ({'including' if args.include_test else 'excluding'} test traffic; "
          f"{report['excluded_test_receipts']} test receipts excluded)")
    print(f"Total: {t['calls_saved']} model calls, ${t['usd_saved']:.4f}, {t['seconds_saved']:.1f} s saved "
          "(negative = cost more)")
    for lever, v in report["by_lever"].items():
        print(f"  {LEVER_LABELS[lever]:<34} {v['receipts']:>5} receipts  {v['calls_saved']:>5} calls  "
              f"${v['usd_saved']:>9.4f}  {v['seconds_saved']:>8.1f} s")
    for project, levers in report["by_project"].items():
        tot = {k: sum(v[k] for v in levers.values()) for k in ("receipts", "calls_saved", "usd_saved", "seconds_saved")}
        print(f"  project {project}: {tot['receipts']} receipts, {tot['calls_saved']} calls, ${tot['usd_saved']:.4f}, "
              f"{tot['seconds_saved']:.1f} s")
    print(report["method"])
    return 0


def savings_command(args) -> int:
    from .savings import summarize

    events = Path(args.events).expanduser()
    report = summarize(events, host_model=args.host_model, since=_since_day(args.since),
                       cache_path=events.parent / "savings-cache.json")
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    t, c, tm = report["turns"], report["cost"], report["time"]
    print(f"Turns routed: {t['total']} ({t['cheap']} to the cheaper model, {t['strong']} kept on the host model)")
    if t["total"]:
        print(f"Estimated cost saved: ${c['saved_usd']:.2f} "
              f"(cheaper-model turns cost ${c['cheap_turns_actual_usd']:.2f}; on {report['host_model']}: "
              f"${c['cheap_turns_on_host_usd']:.2f})")
        if tm["available"]:
            print(f"Estimated model time saved: {tm['saved_seconds'] / 60:.1f} min "
                  f"(cheaper-model turns {tm['cheap_turns_model_seconds'] / 60:.1f} min; on the host model "
                  f"{tm['cheap_turns_on_host_seconds'] / 60:.1f} min)")
        else:
            print("Estimated model time saved: not yet available (" + tm["reason"] + ")")
        if c["requests_without_cache_data"]:
            print(f"Note: {c['requests_without_cache_data']} older requests lack cache data; their dollars are overstated.")
    print("Estimates only: " + report["method"])
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="afast",
        description=(
            "Fast Decisions -- bounded local/hosted decision judge and observatory "
            "for coding-agent harnesses; local and read-only by default"
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("demo", "serve"):
        command = commands.add_parser(name)
        command.add_argument(
            "--events",
            default=str(
                DEFAULT_EVENTS if name == "serve" else Path.cwd() / ".afast-demo-events"
            ),
        )
        command.add_argument("--port", type=int, default=8765)
        command.add_argument("--open", action="store_true")
        if name == "demo":
            command.add_argument("--record-only", action="store_true")
        if name == "serve":
            command.add_argument('--study', default=None, help='Include live receipts and progress from a native-study directory (read only)')
            command.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
            command.add_argument("--stop", action="store_true")
            command.add_argument("--no-fallback", action="store_true")
    command = commands.add_parser("export")
    command.add_argument("--events", required=True)
    command.add_argument("--output", default="decision-observatory.html")
    command = commands.add_parser("doctor")
    command.add_argument("--require-amplifier", action="store_true")
    command = commands.add_parser("configure")
    command.add_argument("--backend", choices=["jev", "deterministic", "ollama"])
    command.add_argument("--model")
    command.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    command.add_argument("--local-sources", action="store_true")
    command.add_argument("--bundle-root")
    command.add_argument("--workspace", default=".")
    command.add_argument(
        "--mode", choices=["off", "shadow", "active"], default="shadow"
    )
    command.add_argument("--allow-external-state", action="store_true")
    command.add_argument("--events", default=str(DEFAULT_EVENTS))
    command.add_argument("--timeout-ms", type=int, default=750)
    command.add_argument("--output", default=".amplifier/fast-decisions-local.md")
    bench = commands.add_parser(
        "bench", help="Offline analysis of fast_decisions telemetry"
    )
    bench_commands = bench.add_subparsers(dest="bench_command", required=True)
    replay = bench_commands.add_parser("replay")
    replay.add_argument(
        "events_path",
        nargs="?",
        default=None,
        help="Events directory or a single JSONL file",
    )
    replay.add_argument(
        "--events", default=None, help="Same as the positional argument"
    )
    replay.add_argument("--session", default=None)
    replay.add_argument(
        "--out", default=None, help="Append the JSONL record to this path"
    )
    replay.add_argument(
        "--md", default=None, help="Write the Markdown summary to this path"
    )
    replay.add_argument(
        "--json", action="store_true", help="Print the JSONL record to stdout"
    )
    suite = bench_commands.add_parser("suite")
    suite.add_argument("suite_path", nargs="?", default=None, help="Suite JSONL file")
    suite.add_argument("--suite", default=str(DEFAULT_SUITE))
    suite.add_argument(
        "--backend",
        choices=["deterministic", "jev", "ollama", "mlx", "hosted", "gateway", "laya"],
        default="deterministic",
        help="'gateway' is a legacy alias for 'hosted'"
    )
    suite.add_argument(
        "--model", default=None,
        help="Model name, passed to the jev/ollama/mlx/hosted backend (unused for laya)"
    )
    suite.add_argument(
        "--hosted-url", "--gateway-url", dest="hosted_url", default=None,
        help="Override the hosted judge base URL (else FAST_DECISIONS_HOSTED_URL, "
             "FAST_DECISIONS_GATEWAY_URL (legacy alias), or your configured host; "
             "required if none of those are set)"
    )
    suite.add_argument(
        "--laya-url", dest="laya_url", default=None,
        help="Override the Laya judge base URL (else FAST_DECISIONS_LAYA_URL or "
             "the local default http://127.0.0.1:8090)"
    )
    suite.add_argument("--live", action="store_true")
    suite.add_argument("--permutations", type=int, default=4)
    suite.add_argument(
        "--domain", choices=["tool-choice", "read-target", "model-role"], default=None
    )
    suite.add_argument("--out", default=None)
    suite.add_argument("--md", default=None)
    suite.add_argument("--json", action="store_true")
    calibrate = bench_commands.add_parser(
        "calibrate",
        help="ECE + gate curve over judged receipts joined with labels",
    )
    calibrate.add_argument(
        "--receipts",
        required=True,
        help="Events directory or a single JSONL file (same format as "
             "'bench replay'); chosen-option probability comes from each "
             "decision's scored.selected_probability",
    )
    calibrate.add_argument(
        "--labels",
        required=True,
        help="JSONL file of {'decision_id': ..., 'correct': bool} rows",
    )
    rubric = commands.add_parser(
        "rubric",
        help="Score (input, output) pairs against a yes/no rubric with Jev",
        description=(
            "Score rubric requests with Jev noul questions (one batched call per request). "
            "Input: a JSON object, a JSON array, or JSONL of "
            "{scoring_spec: [{question, label?, weight?}], llm_input, llm_output, aggregation_method?}. "
            "Needs TYPESAFE_API_KEY; sends the input and output text to the Jev endpoint."
        ),
    )
    rubric.add_argument("requests", help="Path to the request file, or - for stdin")
    rubric.add_argument("--model", default=None, help="Jev model (default: TYPESAFE_DEFAULT_MODEL or the pinned default)")
    rubric.add_argument("--timeout-ms", type=int, default=10000)
    eff = commands.add_parser(
        "efficiency",
        help="Efficiency receipts summed by lever and project (production traffic only by default)",
    )
    eff.add_argument("--events", default=str(DEFAULT_EVENTS))
    eff.add_argument("--since", default=None, help="YYYY-MM-DD, or Nd for the last N days")
    eff.add_argument("--include-test", action="store_true", help="Also count test/dev/benchmark traffic")
    eff.add_argument("--json", action="store_true")
    savings = commands.add_parser(
        "savings",
        help="Estimated time and cost saved by turn routing, from recorded events",
    )
    savings.add_argument("--events", default=str(DEFAULT_EVENTS))
    savings.add_argument("--since", default=None, help="YYYY-MM-DD, or Nd for the last N days")
    savings.add_argument("--host-model", default=None, help="Host model id (default: the recorded provider default model, else claude-fable-5-1)")
    savings.add_argument("--json", action="store_true")
    hooks = commands.add_parser(
        "hooks",
        help="Install the waste guards into another harness (Claude Code) or run its hook",
    )
    hooks.add_argument("action", choices=("install", "uninstall", "show", "run"))
    hooks.add_argument("harness", choices=("claude-code",))
    hooks.add_argument("mode", nargs="?", choices=("pre", "post", "stop"), help="for run: which hook")
    hooks.add_argument("--settings", default=None,
                       help="settings.json to edit (default ~/.claude/settings.json; use .claude/settings.json for one project)")
    hooks.add_argument("--python", default=None, help="interpreter the hook runs with (default: this one)")
    args = parser.parse_args(argv)
    try:
        if args.command == "hooks":
            return hooks_command(args)
        if args.command == "efficiency":
            return efficiency_command(args)
        if args.command == "savings":
            return savings_command(args)
        if args.command == "rubric":
            return rubric_command(args)
        if args.command == "doctor":
            return doctor(args.require_amplifier)
        if args.command == "configure":
            return configure(args)
        if args.command == "export":
            export_html(args.events, args.output)
            return 0
        if args.command == "bench" and args.bench_command == "replay":
            return bench_replay(args)
        if args.command == "bench" and args.bench_command == "suite":
            return bench_suite(args)
        if args.command == "bench" and args.bench_command == "calibrate":
            return bench_calibrate(args)
        if args.command == "serve" and args.stop:
            return stop_server(args.state_file)
        if args.command == "demo" and args.record_only:
            asyncio.run(run_demo(args.events))
            print("Synthetic demo recorded in " + str(Path(args.events).resolve()))
            return 0
        requested_port = args.port
        no_fallback = args.command == "serve" and getattr(args, "no_fallback", False)
        try:
            server = ViewerServer(args.events, requested_port, study_dir=getattr(args, 'study', None))
        except OSError as exc:
            can_fall_back = (
                args.command == "serve"
                and requested_port != 0
                and not no_fallback
                and exc.errno == errno.EADDRINUSE
            )
            if not can_fall_back:
                raise
            server = ViewerServer(args.events, 0, study_dir=getattr(args, 'study', None))
            print(
                f"afast: port {requested_port} was busy; using port "
                f"{server.server_port} instead",
                file=sys.stderr,
            )
        stop = threading.Event()
        worker = None
        if args.command == "demo":
            print("SYNTHETIC DEMO: scripted decisions and delays, not Jev benchmarks.")
            worker = threading.Thread(
                target=lambda: asyncio.run(
                    run_demo(args.events, stop=stop, repeat=True)
                ),
                daemon=True,
            )
            worker.start()
        state_file = (
            Path(args.state_file).expanduser() if args.command == "serve" else None
        )
        if state_file is not None:
            write_state_atomic(
                state_file,
                {
                    "pid": os.getpid(),
                    "port": server.server_port,
                    "url": server.url,
                    "events_dir": str(Path(args.events).expanduser().resolve()),
                    "study_dir": str(Path(args.study).expanduser().resolve()) if getattr(args, 'study', None) else None,
                    "started_at": datetime.now(UTC).isoformat(),
                    "version": __version__,
                    "build": build_id(),
                },
            )
        print("Read-only local viewer: " + server.url, flush=True)
        print(
            "Telemetry directory: " + str(Path(args.events).expanduser().resolve()),
            flush=True,
        )
        if args.open:
            webbrowser.open(server.url)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            server.server_close()
            if worker:
                worker.join(timeout=3)
            if state_file is not None:
                remove_state(state_file)
        return 0
    except (ValueError, OSError) as exc:
        print("afast: " + (str(exc) or type(exc).__name__), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
