"""Tests for scripts/forge_e2e.py's multi-turn scenario launcher path
(worker() branching to _worker_scenario/_run_scenario_turns for kind='scenario'
tasks). Never invokes a real amplifier binary or Forge daemon: `amplifier` is a
fake `subprocess.Popen` that records argv and simulates the session directory
(events.jsonl) a real `amplifier run` would produce.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import forge_e2e  # noqa: E402
import battery_tasks as bt  # noqa: E402


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


class _FakeProcess:
    def __init__(self, code=0):
        self.pid = 999
        self._code = code

    def wait(self, timeout=None):
        return self._code


def _setup_scenario_run(tmp, task_name="scn_dev_1", extra_manifest=None):
    """Build a run directory + manifest for one scenario task, ready for
    forge_e2e.worker(root, name). Returns (root, run, workspace, source_root)."""
    root = Path(tmp)
    source_root = root / "side-source"
    (source_root / "src" / "amplifier_fast_decisions").mkdir(parents=True)
    (source_root / "src" / "amplifier_fast_decisions" / "mod.py").write_text("x = 1\n")
    tree_sha = forge_e2e.tree_sha256(source_root / "src" / "amplifier_fast_decisions")

    task = bt.TASKS[task_name]
    run = root / "run1"
    workspace = run / "workspace"
    for relpath, content in task.files.items():
        p = workspace / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    (workspace / ".amplifier").mkdir(parents=True, exist_ok=True)
    (run / "profile.md").write_text("---\n{}\n---\n")
    workspace_hash = forge_e2e.hash_files(workspace)

    prompt = task.prompt
    manifest = {
        "runs": {
            "run1": {
                "task": task_name, "side": "fast", "workspace_hash": workspace_hash,
                "attempt": 1, "prompt": prompt,
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            }
        },
        "sides": {"fast": {"source_root": str(source_root), "mode": "active",
                            "source_git_sha": None, "source_tree_sha256": tree_sha}},
        "provider": "anthropic", "model": "claude-fable-5-1", "prompt": prompt,
        "events_dir": str(root / "events"), "limits": {"timeout_seconds": 30},
    }
    if extra_manifest:
        manifest.update(extra_manifest)
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root, run, workspace, source_root


class ScenarioTurnSequencingTests(unittest.TestCase):
    def _run_with_fake_amplifier(self, root, run, workspace, *, models_by_turn=None, fail_turn=None):
        fake_home = Path(root).parent / "home"
        slug = str(workspace.resolve()).replace("/", "-").replace("\\", "-").replace(":", "")
        sessions_dir = fake_home / ".amplifier/projects" / slug / "sessions"
        sid = "sess-abc123"
        captured = []
        state = {"turn": 0}
        sleeps = []

        real_popen = forge_e2e.subprocess.Popen

        def fake_popen(command, cwd=None, env=None, **kwargs):
            if not (isinstance(command, list) and command[:2] == ["amplifier", "run"]):
                # Real subprocess.run() calls elsewhere in this process (battery_tasks'
                # evaluator snippets, pytest/unittest workspace-test runs, `amplifier bundle
                # remove`) are implemented ON TOP of subprocess.Popen -- only the direct
                # `amplifier run ...` launch this test is exercising gets simulated.
                return real_popen(command, cwd=cwd, env=env, **kwargs)
            captured.append(command)
            state["turn"] += 1
            t = state["turn"]
            sess_dir = sessions_dir / sid
            sess_dir.mkdir(parents=True, exist_ok=True)
            model = (models_by_turn or {}).get(t, "model-a")
            text = "ANSWER: AUD-ORDER-7781" if t == 2 else f"DONE: turn {t} complete"
            events = [
                {"event": "llm:request", "ts": _now_iso(), "data": {"model": model}},
                {"event": "llm:response", "ts": _now_iso(),
                 "data": {"raw": {"content": [{"type": "text", "text": text}]},
                          "usage": {"cost_usd": 0.01}}},
            ]
            with (sess_dir / "events.jsonl").open("a") as f:
                for e in events:
                    f.write(json.dumps(e) + "\n")
            code = 1 if (fail_turn is not None and t == fail_turn) else 0
            return _FakeProcess(code)

        def fake_sleep(seconds):
            sleeps.append(seconds)

        with patch("forge_e2e.Path.home", return_value=fake_home), \
             patch.object(forge_e2e.subprocess, "Popen", fake_popen), \
             patch.object(forge_e2e.time, "sleep", fake_sleep), \
             patch.object(forge_e2e.urllib.request, "urlopen") as fake_urlopen, \
             patch.object(forge_e2e, "extract_receipts", lambda *a, **k: None), \
             patch.object(forge_e2e, "_unregister_benchmark_bundle", lambda *a, **k: None):
            fake_urlopen.return_value.__enter__.return_value = SimpleNamespace()
            with patch("json.load", return_value={}):
                forge_e2e.worker(root, "run1")

        result = json.loads((run / "result.json").read_text())
        return captured, result, sid, sleeps

    def test_turn_sequencing_resumes_with_same_bundle_provider_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, run, workspace, _source = _setup_scenario_run(tmp)
            captured, result, sid, _sleeps = self._run_with_fake_amplifier(root, run, workspace)

            self.assertEqual(len(captured), 4)  # scn_dev_1 has 4 turns
            bundle_uri = (run / "profile.md").as_uri()
            for i, argv in enumerate(captured, start=1):
                self.assertEqual(argv[0], "amplifier")
                self.assertEqual(argv[1], "run")
                self.assertIn("--bundle", argv)
                self.assertEqual(argv[argv.index("--bundle") + 1], bundle_uri)
                self.assertIn("--provider", argv)
                self.assertEqual(argv[argv.index("--provider") + 1], "anthropic")
                self.assertIn("--model", argv)
                self.assertEqual(argv[argv.index("--model") + 1], "claude-fable-5-1")
                if i == 1:
                    self.assertNotIn("--resume", argv)
                else:
                    self.assertIn("--resume", argv)
                    self.assertEqual(argv[argv.index("--resume") + 1], sid)

            self.assertEqual(result["session_id"], sid)
            self.assertEqual(len(result["turns"]), 4)
            for i, turn in enumerate(result["turns"], start=1):
                self.assertEqual(turn["index"], i)
                self.assertFalse(turn["skipped"])
                self.assertEqual(turn["exit_code"], 0)

    def test_final_message_and_quality_are_folded_with_turn_prefixed_labels(self):
        """Turn 1 (repair) is left unfixed (starter fails); turn 2 (answer)
        gets the correct ANSWER: line from the fake amplifier's response.
        The folded quality must show turn 1's failure under its own turn
        label and turn 2 passing."""
        with tempfile.TemporaryDirectory() as tmp:
            root, run, workspace, _source = _setup_scenario_run(tmp)
            _captured, result, _sid, _sleeps = self._run_with_fake_amplifier(root, run, workspace)

            task = bt.TASKS["scn_dev_1"]
            turn1_dir = bt.scenario_turn_dir(1, task.subtasks[0])
            turn2_dir = bt.scenario_turn_dir(2, task.subtasks[1])
            self.assertGreater(result["quality"]["failed"], 0)
            self.assertTrue(any(label.startswith(f"{turn1_dir}:") for label in result["quality"]["failure_labels"]))
            self.assertFalse(any(label.startswith(f"{turn2_dir}:") for label in result["quality"]["failure_labels"]))
            self.assertFalse(result["outcome_passed"])  # turn 1's starter is unfixed

    def test_model_switch_across_turns_is_visible_in_effort_receipts(self):
        """The whole point of s1m: per-turn routing can switch models between
        turns of the SAME session. effort_receipts (summed over the whole
        session, unchanged machinery) must show both models."""
        with tempfile.TemporaryDirectory() as tmp:
            root, run, workspace, _source = _setup_scenario_run(tmp)
            models = {1: "model-a", 2: "model-a", 3: "model-b", 4: "model-b"}
            _captured, result, _sid, _sleeps = self._run_with_fake_amplifier(root, run, workspace, models_by_turn=models)
            seen_models = {e["model"] for e in result["effort_receipts"]}
            self.assertEqual(seen_models, {"model-a", "model-b"})

    def test_turn_failure_stops_launching_and_marks_remaining_turns_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, run, workspace, _source = _setup_scenario_run(tmp)
            captured, result, _sid, _sleeps = self._run_with_fake_amplifier(root, run, workspace, fail_turn=2)

            # Only turns 1 and 2 ever launch amplifier; 3 and 4 are skipped, not guessed at.
            self.assertEqual(len(captured), 2)
            turns = result["turns"]
            self.assertEqual(len(turns), 4)
            self.assertFalse(turns[0]["skipped"])
            self.assertEqual(turns[1]["exit_code"], 1)
            self.assertFalse(turns[1]["skipped"])
            self.assertTrue(turns[2]["skipped"])
            self.assertTrue(turns[3]["skipped"])
            self.assertFalse(result["outcome_passed"])

    def test_turn_gap_seconds_sleeps_between_turns_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, run, workspace, _source = _setup_scenario_run(tmp, extra_manifest={"turn_gap_seconds": 5})
            _captured, _result, _sid, sleeps = self._run_with_fake_amplifier(root, run, workspace)
            # 4 turns -> 3 gaps (never before turn 1).
            self.assertEqual(sleeps, [5, 5, 5])


class BackgroundNamingCallRegressionTests(unittest.TestCase):
    """Reproduces the real 2026-09-25 mt-smoke bug: on a resumed turn,
    Amplifier's session-naming hook makes an extra background llm:request
    (claude-haiku-4-5, purpose='session-naming') AFTER the turn's real
    answer, landing inside the turn's own [started_at, ended_at] window.
    Window-based final_message extraction picks up that later (wrong)
    response; stdout-based extraction (the fix) does not, because the CLI's
    stdout JSON's 'response' field is captured before the naming hook ever runs.
    """

    def test_stdout_wins_over_a_later_background_response_in_the_same_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, run, workspace, _source = _setup_scenario_run(tmp)
            fake_home = Path(root).parent / "home"
            slug = str(workspace.resolve()).replace("/", "-").replace("\\", "-").replace(":", "")
            sessions_dir = fake_home / ".amplifier/projects" / slug / "sessions"
            sid = "sess-abc123"
            state = {"turn": 0}
            real_popen = forge_e2e.subprocess.Popen

            def fake_popen(command, cwd=None, env=None, stdout=None, **kwargs):
                if not (isinstance(command, list) and command[:2] == ["amplifier", "run"]):
                    return real_popen(command, cwd=cwd, env=env, stdout=stdout, **kwargs)
                state["turn"] += 1
                t = state["turn"]
                sess_dir = sessions_dir / sid
                sess_dir.mkdir(parents=True, exist_ok=True)
                real_answer = "ANSWER: AUD-ORDER-7781" if t == 2 else f"DONE: turn {t} complete"
                events = [
                    {"event": "llm:request", "ts": _now_iso(), "data": {"model": "model-a"}},
                    {"event": "llm:response", "ts": _now_iso(),
                     "data": {"raw": {"content": [{"type": "text", "text": real_answer}]},
                              "usage": {"cost_usd": 0.01}}},
                ]
                if t == 2:
                    # The background naming call: fires AFTER the real answer,
                    # same turn window, wrong content, distinguishable only by
                    # its 'purpose'/'origin_module'/model.
                    events += [
                        {"event": "llm:request", "ts": _now_iso(),
                         "data": {"model": "claude-haiku-4-5-20251001", "purpose": "session-naming",
                                  "origin_module": "hooks-session-naming"}},
                        {"event": "llm:response", "ts": _now_iso(),
                         "data": {"purpose": "session-naming", "origin_module": "hooks-session-naming",
                                  "raw": {"content": [{"type": "text",
                                                        "text": '```json\n{"description": "not the answer"}\n```'}]},
                                  "usage": {"cost_usd": 0.003}}},
                    ]
                with (sess_dir / "events.jsonl").open("a") as f:
                    for e in events:
                        f.write(json.dumps(e) + "\n")
                # The fix: write the CLEAN stdout JSON blob (what --output-format
                # json actually produces) to the file forge_e2e redirected stdout to.
                if hasattr(stdout, "write"):
                    stdout.write(json.dumps({"status": "success", "response": real_answer,
                                              "session_id": sid}))
                    stdout.flush()
                return _FakeProcess(0)

            with patch("forge_e2e.Path.home", return_value=fake_home), \
                 patch.object(forge_e2e.subprocess, "Popen", fake_popen), \
                 patch.object(forge_e2e.time, "sleep", lambda *_a, **_k: None), \
                 patch.object(forge_e2e.urllib.request, "urlopen") as fake_urlopen, \
                 patch.object(forge_e2e, "extract_receipts", lambda *a, **k: None), \
                 patch.object(forge_e2e, "_unregister_benchmark_bundle", lambda *a, **k: None):
                fake_urlopen.return_value.__enter__.return_value = SimpleNamespace()
                with patch("json.load", return_value={}):
                    forge_e2e.worker(root, "run1")

            result = json.loads((run / "result.json").read_text())
            turn2 = result["turns"][1]
            self.assertEqual(turn2["final_message"], "ANSWER: AUD-ORDER-7781")
            self.assertEqual(turn2["final_message_source"], "stdout_response")
            # And grading actually passes turn 2 now (it would fail on the
            # polluted window-only extraction -- no ANSWER: line in the JSON blob).
            self.assertTrue(turn2["turn_passed"], turn2)


class WindowedFinalMessageTests(unittest.TestCase):
    """Unit tests for _extract_final_message_window against a synthetic
    events.jsonl mixing several turns' events together (as a real --resume'd
    session's events.jsonl does)."""

    def _write_events(self, path, events):
        path.write_text("".join(json.dumps(e) + "\n" for e in events))

    def test_window_isolates_the_correct_turns_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp)
            events = [
                {"event": "llm:response", "ts": "2026-01-01T00:00:01+00:00",
                 "data": {"raw": {"content": [{"type": "text", "text": "turn1 text"}]}}},
                {"event": "llm:response", "ts": "2026-01-01T00:00:11+00:00",
                 "data": {"raw": {"content": [{"type": "text", "text": "turn2 text"}]}}},
                {"event": "llm:response", "ts": "2026-01-01T00:00:21+00:00",
                 "data": {"raw": {"content": [{"type": "text", "text": "turn3 text"}]}}},
            ]
            self._write_events(session_dir / "events.jsonl", events)

            turn2 = forge_e2e._extract_final_message_window(
                session_dir, "2026-01-01T00:00:10+00:00", "2026-01-01T00:00:15+00:00")
            self.assertEqual(turn2, "turn2 text")

            turn1 = forge_e2e._extract_final_message_window(
                session_dir, None, "2026-01-01T00:00:05+00:00")
            self.assertEqual(turn1, "turn1 text")

    def test_window_with_no_matching_events_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp)
            self._write_events(session_dir / "events.jsonl", [
                {"event": "llm:response", "ts": "2026-01-01T00:00:01+00:00",
                 "data": {"raw": {"content": [{"type": "text", "text": "turn1 text"}]}}},
            ])
            result = forge_e2e._extract_final_message_window(
                session_dir, "2026-06-01T00:00:00+00:00", "2026-06-01T00:01:00+00:00")
            self.assertIsNone(result)


class SingleTurnPathUnchangedTests(unittest.TestCase):
    """A non-scenario ('code'/'answer'/legacy SPECS) task must take exactly
    the pre-existing single-shot code path in worker() -- _task_kind() must
    route it away from _worker_scenario."""

    def test_code_task_does_not_branch_to_scenario_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "side-source"
            (source_root / "src" / "amplifier_fast_decisions").mkdir(parents=True)
            (source_root / "src" / "amplifier_fast_decisions" / "mod.py").write_text("x = 1\n")
            tree_sha = forge_e2e.tree_sha256(source_root / "src" / "amplifier_fast_decisions")
            run = root / "one"
            workspace = run / "workspace"
            (workspace / ".amplifier").mkdir(parents=True)
            (workspace / "README.md").write_text(forge_e2e.SPECS["scheduler"])
            (workspace / "solution.py").write_text(forge_e2e.STARTERS["scheduler"])
            (workspace / "test_public.py").write_text(forge_e2e.PUBLIC["scheduler"])
            (run / "profile.md").write_text("---\n{}\n---\n")
            workspace_hash = forge_e2e.hash_files(workspace)
            manifest = {
                "runs": {"one": {"task": "scheduler", "side": "fast", "workspace_hash": workspace_hash, "attempt": 1}},
                "sides": {"fast": {"source_root": str(source_root), "mode": "active",
                                    "source_git_sha": None, "source_tree_sha256": tree_sha}},
                "provider": "anthropic", "model": "claude-fable-5-1", "prompt": "do it",
                "events_dir": str(root / "events"),
                "limits": {"timeout_seconds": 5},
            }
            (root / "manifest.json").write_text(json.dumps(manifest))

            scenario_worker_called = []
            real_worker_scenario = forge_e2e._worker_scenario

            def spy(*a, **k):
                scenario_worker_called.append(True)
                return real_worker_scenario(*a, **k)

            real_popen = forge_e2e.subprocess.Popen

            def fake_popen(command, cwd=None, env=None, **kwargs):
                if isinstance(command, list) and command[:2] == ["amplifier", "run"]:
                    return _FakeProcess(0)
                return real_popen(command, cwd=cwd, env=env, **kwargs)

            with patch.object(forge_e2e.subprocess, "Popen", fake_popen), \
                 patch.object(forge_e2e.urllib.request, "urlopen") as fake_urlopen, \
                 patch.object(forge_e2e, "_worker_scenario", spy), \
                 patch.object(forge_e2e, "_unregister_benchmark_bundle", lambda *a, **k: None):
                fake_urlopen.return_value.__enter__.return_value = SimpleNamespace()
                with patch("json.load", return_value={}):
                    forge_e2e.worker(root, "one")

            self.assertEqual(scenario_worker_called, [])
            result = json.loads((run / "result.json").read_text())
            self.assertNotIn("turns", result)


if __name__ == "__main__":
    unittest.main()
