"""Run this bounded live acceptance suite INSIDE a Forge exec terminal.

Requires an isolated Amplifier Python environment with Playwright Chromium and
privately inherited provider keys. Artifacts retain failed attempts. The browser
host is a test fixture, not an installed trycua VM. No arbitrary websites open.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from forge_e2e import native_summary, effort_summary


def dump(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--base-profile", type=Path, required=True)
    parser.add_argument("--only", help="Run one named case")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    source = args.source.resolve()
    fixture = Path(__file__).resolve().parent / "fixtures/forge_cua"
    cases = ["native-selector", "denied-selector", "plain-1", "driver-1", "driver-2", "plain-2", "denied-driver",
             "timeout-driver", "stale-driver", "loop-driver", "verify-driver"]
    if args.only:
        assert args.only in cases
        cases = [args.only]
    for name in cases:
        root = args.output / name
        if (root / "result.json").exists():
            continue
        if (root / "stdout.json").exists() or (root / "native").exists():
            raise SystemExit(f"Unreconciled attempt at {root}; inspect it before launching another paid session")
        root.mkdir(parents=True, exist_ok=True)
        workspace = root / "workspace"
        (workspace / ".amplifier").mkdir(parents=True, exist_ok=True)
        (workspace / ".amplifier/settings.local.yaml").write_text("bundle:\n  app: []\n")
        base = json.loads(args.base_profile.read_text().split("---")[1])
        base["bundle"]["name"] = "forge-cua-acceptance"
        base["hooks"] = [{"module": "hooks-logging", "source": "git+https://github.com/microsoft/amplifier-module-hooks-logging@main",
                          "config": {"mode": "session-only", "session_log_template": str(root / "native/{session_id}/events.jsonl")}}]
        base["session"]["orchestrator"]["config"]["max_iterations"] = 16
        base["tools"] = [{"module": "tool-forge-cua-fixture", "source": fixture.as_uri(),
                          "config": {"output": str(root), "deny_selector": name == "denied-selector", "deny_actions": name == "denied-driver",
                                     "judge_timeout_ms": 100 if name == "timeout-driver" else 3000,
                                     "approval_stales": name == "stale-driver", "freeze_clicks": name == "loop-driver", "verify_fail": name == "verify-driver"}}]
        if "selector" in name:
            base["tools"].append({"module": "tool-jev-cua", "source": (source / "modules/tool-jev-cua").as_uri(),
                                  "config": {"allow_external_state": True}})
        if name == "native-selector":
            base["includes"] = [{"bundle": source.as_uri()}]
            base["hooks"].append({"module": "hooks-fast-decisions", "source": (source / "modules/hooks-fast-decisions").as_uri(),
                                  "config": {"mode": "off", "observatory": {"enabled": False}, "events_dir": str(root / "events")}})
        (root / "profile.md").write_text("---\n" + json.dumps(base, indent=2) + "\n---\n")
        common = ("Use only fixture_browser and, if available, jev_cua. This is an authorized public disposable website fixture; "
                  "sharing its labels with TypeSafe is authorized. Do not delegate or use bash/filesystem. "
                  "Open the weekly Laya report and verify REPORT: LAYA-WEEKLY-42 is visible. ")
        if name == "denied-selector":
            prompt = common + "Observe once, pass the exact snapshot to jev_cua once. If native approval denies it, stop and report denial; do not click or bypass."
        elif name == "native-selector":
            prompt = common + "Observe once and call jev_cua once with that exact snapshot to choose the first click. Pass its unchanged proposal to fixture_browser execute_proposal. Then stop and report the observed resulting screen. This acceptance case tests only the first click."
        elif "driver" in name:
            prompt = common + "Call fixture_browser fast_run once. Then call verify and report the exact observed result. Do not perform additional clicks or retry if it stops."
        else:
            prompt = common + "Use observe, then individual click calls based on observed target IDs, observing between clicks, and finally verify. Do not use fast_run or execute_proposal."
        (root / "prompt.txt").write_text(prompt)
        env = {**os.environ, "PYTHONPATH": str(source / "src"), "AFAST_TRAFFIC": "test", "AFAST_EVENTS_DIR": str(root / "events"),
               "AFAST_OBSERVATORY": "off", "AMPLIFIER_MEMORY_CAPTURE": "off", "AMPLIFIER_NO_BROWSER": "1"}
        started = time.monotonic()
        print("LAUNCH " + name, flush=True)
        timed_out = False
        with (root / "stdout.json").open("w") as out, (root / "stderr.log").open("w") as err:
            try:
                result = subprocess.run([str(Path(sys.executable).parent / "amplifier"), "run", "--bundle", (root / "profile.md").as_uri(),
                                         "--mode", "single", "--provider", "anthropic", "--model", "claude-sonnet-5", "--output-format", "json", prompt],
                                        cwd=workspace, env=env, stdout=out, stderr=err, timeout=240)
                code = result.returncode
            except subprocess.TimeoutExpired:
                code, timed_out = None, True
        state = json.loads((root / "browser-state.json").read_text()) if (root / "browser-state.json").exists() else None
        events = [json.loads(line) for line in (root / "browser-events.jsonl").read_text().splitlines()] if (root / "browser-events.jsonl").exists() else []
        native = list((root / "native").glob("*/events.jsonl"))
        if name == "native-selector":
            passed = bool(state and state["history"] == ["reports"])
        elif name == "denied-selector":
            passed = bool(state and not state["history"] and any(e.get("denied") for e in events))
        elif name == "denied-driver":
            passed = bool(state and not state["history"] and any(e.get("status") == "denied" for e in events))
        elif name in {"timeout-driver", "stale-driver", "loop-driver", "verify-driver"}:
            expected = {"timeout-driver": "reason", "stale-driver": "stale", "loop-driver": "loop_stopped", "verify-driver": "verification_failed"}[name]
            driver = json.loads((root / "driver-result.json").read_text()) if (root / "driver-result.json").exists() else {}
            passed = bool(state and not state["verified"] and driver.get("status") == expected)
            if name != "verify-driver":
                passed = passed and not state["history"]
        else:
            passed = bool(state and state["verified"] and state["history"] == ["reports", "weekly", "laya"])
        executed_tools = [e.get("action") for e in events if e.get("event") == "tool_execute"]
        if "driver" in name:
            passed = passed and executed_tools.count("fast_run") == 1 and set(executed_tools) <= {"fast_run", "verify"}
        if name.startswith("plain-"):
            passed = passed and "fast_run" not in executed_tools and "execute_proposal" not in executed_tools
        if name == "denied-selector":
            passed = passed and not any("cua_decided" in p.read_text() for p in (root / "events").glob("*.jsonl"))
        record = {"name": name, "exit_code": code, "timed_out": timed_out, "wall_seconds": time.monotonic() - started,
                  "passed": passed and code == 0, "state": state,
                  "native": native_summary(native[0].parent) if len(native) == 1 else None,
                  "effort": effort_summary(native[0].parent) if len(native) == 1 else None,
                  "source": subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip(),
                  "profile_sha256": hashlib.sha256((root / "profile.md").read_bytes()).hexdigest(),
                  "fixture_sha256": {str(p.relative_to(fixture)): hashlib.sha256(p.read_bytes()).hexdigest()
                                     for p in fixture.rglob("*") if p.suffix in {".py", ".html", ".toml"}},
                  "fixture_kind": "real Chromium via Playwright; TryCuaHost protocol adapter; no trycua VM"}
        dump(root / "result.json", record)
        print("RESULT " + json.dumps(record), flush=True)
        if code != 0 or not native:
            raise SystemExit("Inspect failed host setup before spending on more runs")


if __name__ == "__main__":
    main()
