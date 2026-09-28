"""Waste guards (guards.py): each guard fires on real waste and never on
legitimate work, receipts are exact, the orchestrator applies them, and the
Claude Code hook and installer behave."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from amplifier_fast_decisions import efficiency, waste
from amplifier_fast_decisions.contracts import Policy, TurnState
from amplifier_fast_decisions.demo import DemoCoordinator
from amplifier_fast_decisions.guards import GuardConfig, WasteGuard
from amplifier_fast_decisions.orchestrator import ObservedTool, RoutedProvider, waste_guard_for
from amplifier_fast_decisions.runtime import Runtime
from amplifier_fast_decisions.service import DecisionService
from amplifier_fast_decisions.telemetry import Emitter

OPUS = "claude-opus-5-5"
BIG = "x" * 2000


def bash(cmd):
    return {"command": cmd}


def call(guard, usd=0.10, model=OPUS, ids=None):
    guard.note_model_call(model=model, usage={}, cost_usd=usd, seconds=2.0, present_ids=ids)


def run(guard, tool, inp, out="ok", failed=False, call_id=None):
    """One tool call through the guard as a harness would do it."""
    d = guard.before(tool, inp)
    if d.action != "run":
        return d, None
    return d, guard.after(tool, inp, out, failed, call_id=call_id)


class ClassifierTests(unittest.TestCase):
    def test_readonly_and_polls(self):
        self.assertTrue(waste.is_readonly_command("cd /x && git status && git log --oneline -3"))
        self.assertTrue(waste.is_readonly_command("grep -rn foo src 2>/dev/null | head"))
        self.assertFalse(waste.is_readonly_command("echo hi > out.txt"))
        self.assertFalse(waste.is_readonly_command("pytest -q"))
        self.assertFalse(waste.is_readonly_command("sed -i s/a/b/ f"))
        self.assertEqual(waste.poll_check("sleep 30 && tail -5 build.log"), "tail -5 build.log")
        self.assertEqual(waste.poll_check("sleep 20"), "")
        self.assertIsNone(waste.poll_check("until test -f r; do sleep 5; done; cat r"))   # a blocking wait: good
        self.assertIsNone(waste.poll_check("sleep 5 && pytest"))                          # check mutates
        self.assertIsNone(waste.poll_check("tail -5 build.log"))
        self.assertEqual(waste.sleep_seconds("sleep 1m; sleep 5"), 65)
        # seen in the live A/B: a liveness probe inside the poll
        self.assertIsNotNone(waste.poll_check(
            "sleep 28 && tail -3 build.log; kill -0 8172 2>/dev/null && echo RUNNING || echo DONE"))
        self.assertFalse(waste.is_readonly_command("kill 8172"))

    def test_failure_kinds(self):
        self.assertEqual(waste.failure_kind("<tool_use_error>InputValidationError: x</tool_use_error>"), "validation")
        self.assertEqual(waste.failure_kind("Permission to use Bash has been denied"), "denied")
        self.assertIsNone(waste.failure_kind("Exit code 1\nAssertionError"))


class RepeatStopTests(unittest.TestCase):
    def test_fourth_identical_call_is_blocked_and_claims_after_moving_on(self):
        g = WasteGuard()
        for _ in range(3):
            call(g); run(g, "bash", bash("git status"), "clean")
        call(g, usd=0.12)
        d, _ = run(g, "bash", bash("git status"), "clean")
        self.assertEqual((d.action, d.guard), ("block", "identical_repeat"))
        self.assertIn("identical output", d.message)
        self.assertEqual(g.take_receipts(), [])          # not claimed until the model moves on
        call(g); run(g, "bash", bash("ls"), "a b")
        call(g)
        [r] = g.take_receipts()
        self.assertEqual((r["lever"], r["mechanism"], r["calls_saved"]), ("loop_stop", "guard:identical_repeat", 2))
        self.assertAlmostEqual(r["usd_saved"], 0.24)     # census median (2) x the live step that issued the call
        self.assertEqual(r["harness"], "Amplifier")

    def test_three_requested_runs_are_not_blocked(self):
        g = WasteGuard()
        for _ in range(3):
            call(g)
            d, _ = run(g, "bash", bash("python3 stats.py"), BIG)
            self.assertEqual(d.action, "run")

    def test_override_runs_and_is_charged(self):
        g = WasteGuard()
        for _ in range(3):
            call(g); run(g, "bash", bash("git status"), "clean")
        call(g); d, _ = run(g, "bash", bash("git status"), "clean")
        self.assertEqual(d.action, "block")
        call(g, usd=0.2)
        d, _ = run(g, "bash", bash("git status"), "clean")
        self.assertEqual(d.action, "run")                  # repeated right away: the override runs
        [r] = g.take_receipts()
        self.assertEqual((r["decision"], r["calls_saved"]), ("identical_repeat_overridden", -1))
        self.assertAlmostEqual(r["usd_saved"], -0.2)

    def test_must_not_fire_after_edit_new_turn_mutation_or_changed_output(self):
        g = WasteGuard()
        for _ in range(3):
            call(g); run(g, "bash", bash("git status"), "clean")
        call(g); run(g, "edit_file", {"file_path": "a.py"}, "ok")                 # a write
        call(g); self.assertEqual(g.before("bash", bash("git status")).action, "run")
        g2 = WasteGuard()
        for _ in range(3):
            call(g2); run(g2, "bash", bash("git status"), "clean")
        g2.new_turn()                                                               # the user spoke
        call(g2); self.assertEqual(g2.before("bash", bash("git status")).action, "run")
        g3 = WasteGuard()
        call(g3); run(g3, "bash", bash("git status"), "one")
        call(g3); run(g3, "bash", bash("git status"), "two")                       # output changed
        call(g3); self.assertEqual(g3.before("bash", bash("git status")).action, "run")
        g4 = WasteGuard()
        for _ in range(3):
            call(g4); run(g4, "bash", bash("git status"), "clean")
            call(g4); run(g4, "bash", bash("make build"), "built")                 # a mutating command between
        call(g4); self.assertEqual(g4.before("bash", bash("git status")).action, "run")

    def test_time_varying_check_with_background_job_waits_instead(self):
        g = WasteGuard()
        call(g); run(g, "bash", bash("nohup ./build.sh > build.log 2>&1 &"), "")
        for _ in range(3):
            call(g); run(g, "bash", bash("tail -1 build.log"), "step 3/12")
        call(g)
        d = g.before("bash", bash("tail -1 build.log"))
        self.assertEqual((d.action, d.guard), ("wait", "poll_wait"))


class RetryStopTests(unittest.TestCase):
    ERR = "Traceback (most recent call last):\nModuleNotFoundError: No module named 'foo'"

    def test_third_identical_failure_blocked(self):
        g = WasteGuard()
        for _ in range(2):
            call(g); run(g, "bash", bash("python3 app.py"), self.ERR, failed=True)
        call(g)
        d, _ = run(g, "bash", bash("python3 app.py"), self.ERR, failed=True)
        self.assertEqual((d.action, d.guard), ("block", "error_retry"))
        self.assertIn("No module named", d.message)

    def test_must_not_fire_rerun_after_edit_or_different_error_or_transient(self):
        g = WasteGuard()
        for _ in range(2):
            call(g); run(g, "bash", bash("pytest -q"), self.ERR, failed=True)
        call(g); run(g, "edit_file", {"file_path": "app.py"}, "ok")               # the fix attempt
        call(g); self.assertEqual(g.before("bash", bash("pytest -q")).action, "run")   # legit re-run
        g2 = WasteGuard()
        call(g2); run(g2, "bash", bash("pytest -q"), "E1: boom", failed=True)
        call(g2); run(g2, "bash", bash("pytest -q"), "E2: different failure", failed=True)
        call(g2); self.assertEqual(g2.before("bash", bash("pytest -q")).action, "run")
        g3 = WasteGuard()
        for _ in range(2):
            call(g3); run(g3, "bash", bash("./check.sh"), "error: build lock is held, retry shortly", failed=True)
        call(g3); self.assertEqual(g3.before("bash", bash("./check.sh")).action, "run")    # transient


class PollWaitTests(unittest.TestCase):
    def test_second_poll_waits_first_does_not(self):
        g = WasteGuard()
        call(g)
        self.assertEqual(g.before("bash", bash("sleep 30 && tail -3 build.log")).action, "run")
        g.after("bash", bash("sleep 30 && tail -3 build.log"), "step 2", False)
        call(g)
        d = g.before("bash", bash("sleep 25; tail -3 build.log"))
        self.assertEqual((d.action, d.interval_s), ("wait", 25))
        self.assertEqual(g.before("bash", bash("until grep -q OK build.log; do sleep 5; done")).action, "run")

    def test_poll_receipt_counts_polls_in_place(self):
        g = WasteGuard(project="p")
        call(g, usd=0.05)
        g.after_wait("bash", bash("sleep 30 && tail -1 log"), "done", False, polls=4, waited_s=120, changed=True)
        [r] = g.take_receipts()
        self.assertEqual((r["mechanism"], r["calls_saved"]), ("guard:poll_wait", 3))
        self.assertAlmostEqual(r["usd_saved"], 0.15)
        self.assertEqual(r["detail"]["polls"], 4)


class PointerTests(unittest.TestCase):
    def test_identical_output_pointer_and_receipt(self):
        g = WasteGuard(project="p")
        call(g, ids={"c1"})
        self.assertIsNone(g.after("grep", {"pattern": "x"}, BIG, False, call_id="c1"))
        call(g, ids={"c1"})
        rep = g.after("grep", {"pattern": "x"}, BIG, False, call_id="c2")
        self.assertIn("Unchanged", rep)
        call(g); call(g)                                   # two later model calls read the pointer
        [r] = g.end_turn()
        tokens = (len(BIG) - len(rep)) / 4
        self.assertEqual(r["lever"], "context_rightsize")
        self.assertAlmostEqual(r["usd_saved"], round(tokens * 5 / 1e6 + tokens * 0.2 / 1e6, 8))  # write + 1 read
        self.assertEqual(r["detail"]["later_calls"], 2)
        # after a pointer, the next identical call returns the full output (override)
        call(g)
        self.assertIsNone(g.after("grep", {"pattern": "x"}, BIG, False, call_id="c3"))

    def test_no_pointer_when_result_left_context_or_output_small(self):
        g = WasteGuard()
        call(g, ids={"c1"}); g.after("grep", {"pattern": "x"}, BIG, False, call_id="c1")
        call(g, ids={"other"})                             # compacted away
        self.assertIsNone(g.after("grep", {"pattern": "x"}, BIG, False, call_id="c2"))
        g2 = WasteGuard()
        call(g2); g2.after("grep", {"pattern": "x"}, "tiny", False)
        call(g2); self.assertIsNone(g2.after("grep", {"pattern": "x"}, "tiny", False))

    def test_file_reread_pointer_only_when_file_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d, "mod.py")
            f.write_text("\n".join(f"line {i}" for i in range(300)))
            g = WasteGuard(cwd=d)
            call(g); g.after("read_file", {"file_path": "mod.py"}, f.read_text(), False)
            call(g)
            rep = g.after("read_file", {"file_path": "mod.py", "offset": 10, "limit": 50}, BIG, False)
            self.assertIn("unchanged since", rep)          # covered by the whole-file read
            # the file changes: a re-read is legitimate and gets full output
            g3 = WasteGuard(cwd=d)
            call(g3); g3.after("read_file", {"file_path": "mod.py"}, f.read_text(), False)
            f.write_text(f.read_text() + "\nnew line")
            call(g3)
            self.assertIsNone(g3.after("read_file", {"file_path": "mod.py", "offset": 1, "limit": 5}, BIG, False))
            self.assertIsNone(g3.after("read_file", {"file_path": "mod.py"}, f.read_text(), False))

    def test_disabled_config(self):
        g = WasteGuard(GuardConfig.from_config(False))
        for _ in range(5):
            call(g); d, _ = run(g, "bash", bash("git status"), "clean")
        self.assertEqual(d.action, "run")


class AggregationTests(unittest.TestCase):
    def test_by_harness_and_new_fields_recompute(self):
        mk = lambda h, usd: {"event": efficiency.EVENT, "event_id": str(h) + str(usd), "timestamp": "2026-09-25T00:00:00Z",
                             "data": efficiency.guard_receipt(
                                 lever="loop_stop", mechanism="guard:poll_wait", decision="d", method="m",
                                 baseline=efficiency.side(OPUS, 1, usd, 1.0), actual=efficiency.side(OPUS, 0, 0.0, 0.0),
                                 project="p", traffic="production", harness=h, detail={"polls": 2})}
        rep = efficiency.aggregate([mk("Claude Code", 0.1), mk("Claude Code", 0.2), mk(None, 0.3)])
        self.assertAlmostEqual(rep["by_harness"]["Claude Code"]["usd_saved"], 0.3)
        self.assertAlmostEqual(rep["by_harness"]["Amplifier"]["usd_saved"], 0.3)
        self.assertEqual(rep["by_lever"]["loop_stop"]["calls_saved"], 3)


# ------------------------------------------------------------------ orchestrator
class FakeTool:
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), 0

    async def execute(self, input, **kwargs):
        out = self.outputs[min(self.calls, len(self.outputs) - 1)]
        self.calls += 1
        return NS(success=True, output=out, error=None)


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, guards=True):
        events = []
        coordinator = DemoCoordinator()
        emitter = Emitter(coordinator.session_id, callback=events.append)
        service = DecisionService(Policy(mode="off", read_shortcut=False, waste_guards=guards), NS(name="none",
                                  external=False), emitter, coordinator, [])
        service.turn = TurnState("t1")
        return service, Runtime(service), events

    async def _model_call(self, runtime, usd=0.1):
        provider = NS(name="anthropic", default_model=OPUS)

        async def complete(request, **kw):
            return NS(content=[], tool_calls=None, finish_reason="tool_use", model=OPUS,
                      usage=NS(input_tokens=1000, output_tokens=10, cost_usd=usd))
        provider.complete = complete
        facade = RoutedProvider(provider, runtime, {}, None, "anthropic")
        await facade.complete(NS(messages=[{"role": "user", "content": "x"}], tools=[]))

    async def test_block_returns_without_executing_and_receipt_emitted(self):
        service, runtime, events = self._service()
        tool = FakeTool(["clean"])
        wrapped = ObservedTool(tool, runtime, "bash")
        for _ in range(3):
            await self._model_call(runtime)
            await wrapped.execute({"command": "git status"})
        await self._model_call(runtime, usd=0.3)
        result = await wrapped.execute({"command": "git status"})
        self.assertEqual(tool.calls, 3)                                    # the fourth never ran
        self.assertFalse(result.success)
        self.assertIn("[fast-decisions]", result.output)
        self.assertTrue(any(e["event"] == "fast_decisions:waste_guard" for e in events))
        await self._model_call(runtime); await self._model_call(runtime)
        receipts = [e["data"] for e in events if e["event"] == efficiency.EVENT]
        self.assertEqual(len(receipts), 1)
        self.assertAlmostEqual(receipts[0]["usd_saved"], 0.6)   # 2 census repeats x $0.30

    async def test_poll_runs_in_place_until_output_changes(self):
        service, runtime, events = self._service()
        tool = FakeTool(["step 1", "step 1", "step 1", "BUILD OK"])
        wrapped = ObservedTool(tool, runtime, "bash")
        await self._model_call(runtime)
        await wrapped.execute({"command": "sleep 0.01 && tail -1 build.log"})
        await self._model_call(runtime, usd=0.2)
        result = await wrapped.execute({"command": "sleep 0.01 && tail -1 build.log"})
        self.assertEqual(tool.calls, 4)                                    # 3 polls in place in one call
        self.assertIn("BUILD OK", result.output)
        self.assertIn("Poll run in place", result.output)
        [r] = [e["data"] for e in events if e["event"] == efficiency.EVENT]
        self.assertEqual(r["calls_saved"], 2)
        self.assertAlmostEqual(r["usd_saved"], 0.4)

    async def test_poll_on_progress_log_waits_for_finish_then_settles(self):
        service, runtime, events = self._service()
        # progress lines change every poll: keep waiting until a finish word appears
        tool = FakeTool(["step 1/12 compiling", "step 2/12 compiling", "step 3/12 compiling", "step 4/12 compiling",
                         "BUILD OK: 12 targets"])
        wrapped = ObservedTool(tool, runtime, "bash")
        await self._model_call(runtime)
        await wrapped.execute({"command": "sleep 0.01 && tail -1 build.log"})
        await self._model_call(runtime)
        result = await wrapped.execute({"command": "sleep 0.01 && tail -1 build.log"})
        self.assertIn("BUILD OK", result.output)
        self.assertEqual(tool.calls, 5)
        # output that was changing and then stops changing for two polls: settled
        tool2 = FakeTool(["a 1", "a 2", "a 3", "a 3", "a 3", "never reached"])
        service2, runtime2, _ = self._service()
        wrapped2 = ObservedTool(tool2, runtime2, "bash")
        await self._model_call(runtime2)
        await wrapped2.execute({"command": "sleep 0.01 && tail -1 x.log"})
        await self._model_call(runtime2)
        await wrapped2.execute({"command": "sleep 0.01 && tail -1 x.log"})
        self.assertEqual(tool2.calls, 5)

    def test_poll_stop_rule(self):
        from amplifier_fast_decisions.guards import poll_should_stop, terminal_markers
        self.assertEqual(terminal_markers("BUILD OK: 12 targets"), frozenset({"OK"}))
        self.assertIn("finished", terminal_markers("job finished: 42"))
        self.assertEqual(terminal_markers("step 3/12 compiling... ok"), frozenset())
        self.assertIsNone(poll_should_stop(markers=frozenset(), baseline_markers=frozenset(), changed_once=False,
                                           unchanged_streak=5))       # still waiting for a change
        self.assertEqual(poll_should_stop(markers=frozenset({"done"}), baseline_markers=frozenset(),
                                          changed_once=True, unchanged_streak=0), "finished")
        self.assertIsNone(poll_should_stop(markers=frozenset({"error"}), baseline_markers=frozenset({"error"}),
                                           changed_once=True, unchanged_streak=1))   # not a NEW finish word

    async def test_guards_off_by_default_in_code(self):
        service, runtime, events = self._service(guards=None)
        self.assertIsNone(waste_guard_for(service))
        tool = FakeTool(["clean"])
        wrapped = ObservedTool(tool, runtime, "bash")
        for _ in range(4):
            await wrapped.execute({"command": "git status"})
        self.assertEqual(tool.calls, 4)

    def test_old_loop_stop_nudges_stand_down_when_guards_are_on(self):
        from amplifier_fast_decisions.levers import Levers
        ctx = lambda: ("p", "test")
        both = Levers(Policy(mode="off", loop_stop={"enabled": True}, waste_guards={"enabled": True}), None, ctx)
        self.assertIsNone(both.loop)
        nudges_only = Levers(Policy(mode="off", loop_stop={"enabled": True}, waste_guards={"enabled": False}), None, ctx)
        self.assertIsNotNone(nudges_only.loop)

    def test_behavior_enables_guards(self):
        import yaml
        data = yaml.safe_load(Path(__file__).resolve().parents[1].joinpath("behaviors/fast-decisions.yaml").read_text())
        self.assertTrue(data["session"]["orchestrator"]["config"]["waste_guards"]["enabled"])


# ------------------------------------------------------------------ Claude Code
class ClaudeHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.events = base / "events"
        self.env = mock.patch.dict(os.environ, {"AFAST_EVENTS_DIR": str(self.events),
                                                "AFAST_CC_STATE_DIR": str(base / "state"),
                                                "AFAST_TRAFFIC": "test"})
        self.env.start()
        self.transcript = base / "t.jsonl"
        self.transcript.write_text("")
        self.n = 0

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _assistant(self, tokens=100_000):
        self.n += 1
        with self.transcript.open("a") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {
                "id": f"msg_{self.n}", "model": OPUS, "usage": {"input_tokens": 2, "cache_read_input_tokens": tokens,
                                                              "cache_creation_input_tokens": 1000, "output_tokens": 50}}}) + "\n")

    def _hook(self, mode, **payload):
        from amplifier_fast_decisions.claude_hook import handle
        base = {"session_id": "s1", "transcript_path": str(self.transcript), "cwd": self.tmp.name}
        return handle(mode, {**base, **payload})

    def test_repeated_failure_denied_and_receipt_written(self):
        err = {"stdout": "", "stderr": "ModuleNotFoundError: No module named 'x'", "interrupted": False}
        self._assistant()                    # history before first hook run is not replayed
        self._hook("pre", tool_name="Bash", tool_input=bash("python3 a.py"))
        for _ in range(2):
            self._assistant()
            self.assertIsNone(self._hook("pre", tool_name="Bash", tool_input=bash("python3 a.py")))
            self._hook("post", hook_event_name="PostToolUseFailure", tool_name="Bash",
                       tool_input=bash("python3 a.py"), error=err["stderr"])
        self._assistant()
        out = self._hook("pre", tool_name="Bash", tool_input=bash("python3 a.py"))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("failed 2 times", out["hookSpecificOutput"]["permissionDecisionReason"])
        self._assistant(); self._assistant()
        self._hook("stop")
        lines = [json.loads(l) for f in self.events.glob("*.jsonl") for l in f.read_text().splitlines()]
        receipts = [e["data"] for e in lines if e["event"] == efficiency.EVENT]
        self.assertEqual(len(receipts), 1)
        r = receipts[0]
        self.assertEqual((r["harness"], r["mechanism"], r["traffic"]), ("Claude Code", "guard:error_retry", "test"))
        step = (2 * 4 + 100_000 * 0.2 + 1000 * 5 + 50 * 20) / 1e6    # the transcript's usage at list prices
        self.assertAlmostEqual(r["usd_saved"], round(step, 8))
        self.assertTrue(all(isinstance(e["event_id"], str) for e in lines))

    def test_identical_bash_output_replaced_for_the_model(self):
        resp = {"stdout": BIG, "stderr": "", "interrupted": False, "isImage": False}
        self._assistant()
        self._hook("post", tool_name="Bash", tool_input=bash("git diff"), tool_response=resp, tool_use_id="u1")
        self._assistant()
        out = self._hook("post", tool_name="Bash", tool_input=bash("git diff"), tool_response=resp, tool_use_id="u2")
        new = out["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIn("Unchanged", new["stdout"])
        self.assertEqual(set(new), set(resp))              # same shape as the tool's own output

    def test_hook_never_raises(self):
        from amplifier_fast_decisions.claude_hook import main
        with mock.patch("sys.stdin", io.StringIO("not json")), mock.patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(main(["pre"]), 0)
        self.assertEqual(out.getvalue(), "")


class InstallerTests(unittest.TestCase):
    def test_install_is_idempotent_keeps_other_hooks_and_uninstalls_only_ours(self):
        from amplifier_fast_decisions import hooks_install
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "settings.json")
            other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-linter"}]}
            path.write_text(json.dumps({"model": "opus", "hooks": {"PreToolUse": [other]}}))
            hooks_install.install(path, python="/usr/bin/python3")
            hooks_install.install(path, python="/usr/bin/python3")
            s = json.loads(path.read_text())
            self.assertEqual(s["model"], "opus")
            self.assertEqual(len(s["hooks"]["PreToolUse"]), 2)
            self.assertEqual(s["hooks"]["PreToolUse"][0], other)
            ours = s["hooks"]["PreToolUse"][1]
            self.assertIn("amplifier_fast_decisions.claude_hook pre", ours["hooks"][0]["command"])
            self.assertIn("Stop", s["hooks"])
            self.assertIn("post", s["hooks"]["PostToolUse"][0]["hooks"][0]["command"])
            hooks_install.uninstall(path)
            s = json.loads(path.read_text())
            self.assertEqual(s["hooks"], {"PreToolUse": [other]})


if __name__ == "__main__":
    unittest.main()
