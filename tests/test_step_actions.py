"""Per-step action set: classification, prepared candidates, price/cache math,
the workspace's read-only additions, and the loop's per-step decisions and
receipts (step_actions.py, orchestrator.RoutedProvider.complete)."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from amplifier_fast_decisions import efficiency, step_actions as sa
from amplifier_fast_decisions.contracts import Answer, Decision, DecisionResult, Policy, SLOW, TurnState
from amplifier_fast_decisions.demo import DemoCoordinator, demo_response
from amplifier_fast_decisions.orchestrator import ObservedTool, RoutedProvider
from amplifier_fast_decisions.runtime import Runtime
from amplifier_fast_decisions.savings import DEFAULT_RATES, price
from amplifier_fast_decisions.service import DecisionService
from amplifier_fast_decisions.telemetry import Emitter
from amplifier_fast_decisions.workspace import WorkspaceTool

OPUS, HAIKU, SONNET = "claude-opus-5-5", "claude-haiku-4-5", "claude-sonnet-5"


def user(text):
    return {"role": "user", "content": text}


def assistant(*calls):
    return {"role": "assistant", "content": [],
            "tool_calls": [{"id": f"c{i}", "tool": name, "arguments": args} for i, (name, args) in enumerate(calls)]}


def tool(content, name="grep"):
    return {"role": "tool", "name": name, "tool_call_id": "c0",
            "content": content if isinstance(content, str) else json.dumps(content)}


class BashClassificationTests(unittest.TestCase):
    def test_read_only_pipelines(self):
        for cmd in ("grep -rn foo src | head -5", "git status --porcelain && git diff --stat HEAD",
                    "cd /tmp/x && git log -1 --format=%s", "sed -n 1,40p a.py", "ls -la 2>/dev/null",
                    "find . -name '*.py' | wc -l", "git branch --show-current", "rg -n x 2>&1 | tail -3",
                    "sleep 30", "git rev-list --count HEAD"):
            self.assertIs(sa.bash_readonly(cmd), True, cmd)

    def test_mutating_commands(self):
        for cmd in ("echo x > a.txt", "sed -i 's/a/b/' f.py", "git commit -am x", "rm -rf build",
                    "cat a | tee b", "git branch new-feature", "git stash push -m x", "find . -delete",
                    "mkdir out && ls"):
            self.assertIs(sa.bash_readonly(cmd), False, cmd)

    def test_polling_loops_and_nested_scripts(self):
        loop = ('for i in $(seq 1 20); do s=$(gh run view 12 --json status -q .status); echo "$s"; '
                '[ "$s" = completed ] && break; sleep 45; done')
        self.assertIs(sa.bash_readonly(loop), True)
        self.assertIs(sa.bash_readonly('timeout 900 bash -c "for i in {1..30}; do gh pr checks 21 && break || sleep 30; done"'), True)
        self.assertIs(sa.bash_readonly('bash -c "rm -rf build"'), False)
        self.assertIs(sa.bash_readonly("gh pr merge 3 --squash"), False)
        self.assertIs(sa.bash_readonly("gh api -X POST repos/o/r/issues"), False)
        self.assertIs(sa.bash_readonly("x=$(rm -f a)"), False)

    def test_unknown_is_ambiguous(self):
        self.assertIsNone(sa.bash_readonly("python3 script.py"))
        self.assertIsNone(sa.bash_readonly("./run.sh"))
        self.assertIsNone(sa.bash_readonly(""))

    def test_tools(self):
        self.assertIs(sa.tool_readonly("read_file", {}), True)
        self.assertIs(sa.tool_readonly("edit_file", {}), False)
        self.assertIs(sa.tool_readonly("bash", {"command": "git status"}), True)
        self.assertIsNone(sa.tool_readonly("mystery", {}))
        self.assertTrue(sa.is_test_call("bash", {"command": "PYTHONPATH=src python3 -m unittest discover"}))
        self.assertFalse(sa.response_readonly([("bash", {"command": "pytest -q"})]))


class StepClassificationTests(unittest.TestCase):
    def kind(self, messages, **cfg):
        return sa.classify(sa.analyze({"messages": messages}), cfg)[0]

    def test_turn_start_ignores_reminders_and_prior_turns(self):
        msgs = [user("old"), assistant(("grep", {"pattern": "x"})), tool("{}"), {"role": "assistant", "content": "done"},
                user("What is timeout_ms?"), user("<system-reminder>todo</system-reminder>")]
        view = sa.analyze({"messages": msgs})
        self.assertTrue(view.turn_start)
        self.assertEqual(view.prompt, "What is timeout_ms?")

    def test_injected_reminder_envelope_after_a_tool_result_is_not_a_new_turn(self):
        envelope = ("<system-reminders>\nThe blocks below were injected by the system. They are NOT from the user.\n"
                    "<system-reminder source=\"hooks-status-context\">\nToday's date\n</system-reminder>\n"
                    "</system-reminders>")
        msgs = [user(envelope), user("Which function picks the tier?"), assistant(("grep", {"pattern": "x"})),
                tool('{"results": []}'), user(envelope)]
        view = sa.analyze({"messages": msgs})
        self.assertFalse(view.turn_start)
        self.assertEqual(view.prompt, "Which function picks the tier?")
        self.assertEqual(sa.classify(view)[0], sa.ROUTINE)

    def test_judge_state_task_skips_injected_envelopes(self):
        from amplifier_fast_decisions.state import build_state
        envelope = "<system-reminders>\nInjected.\n<system-reminder>todo</system-reminder>\n</system-reminders>"
        msgs = [user("Which function picks the tier?"), assistant(("grep", {"pattern": "x"})),
                tool('{"results": []}'), user(envelope)]
        state = build_state({"messages": msgs}, 4000, None, sa.STEP_INSTRUCTION)
        texts = [o["text"] for o in state["observations"]]
        self.assertIn("Which function picks the tier?", texts)
        self.assertFalse(any("Injected" in t for t in texts))
        self.assertEqual(state["instruction"], sa.STEP_INSTRUCTION)

    def test_kinds(self):
        base = [user("q")]
        self.assertEqual(self.kind(base + [assistant(("grep", {"pattern": "x"})), tool('{"results": []}')]), sa.ROUTINE)
        self.assertEqual(self.kind(base + [assistant(("edit_file", {"file_path": "a"})), tool("ok")]), sa.AFTER_EDIT)
        self.assertEqual(self.kind(base + [assistant(("bash", {"command": "pytest"})), tool("1 failed")]), sa.AFTER_TEST)
        self.assertEqual(self.kind(base + [assistant(("read_file", {"file_path": "a"})),
                                           tool('{"success": false, "error": {"message": "nope"}}')]), sa.AFTER_ERROR)
        self.assertEqual(self.kind(base + [assistant(("bash", {"command": "python3 x.py"})), tool("hi")]), sa.AMBIGUOUS)
        self.assertEqual(self.kind(base + [assistant(("delegate", {"agent": "x"})), tool("hi")]), sa.DELEGATION)
        self.assertEqual(self.kind(base + [assistant(("grep", {"pattern": "x"})), tool("x" * 50)],
                                   routine_max_result_chars=10), sa.LARGE_RESULT)
        rep = base + [assistant(("grep", {"pattern": "x"})), tool("{}"), assistant(("grep", {"pattern": "x"})), tool("{}")]
        self.assertEqual(self.kind(rep), sa.REPEAT)

    def test_reason_carries_tool_names_only(self):
        _, reason = sa.classify(sa.analyze({"messages": [user("q"), assistant(("bash", {"command": "cat SECRET"})),
                                                         tool("x")]}))
        self.assertEqual(reason, "readonly:bash")

    def test_pydantic_like_objects(self):
        msg = NS(role="assistant", content=[NS(type="tool_call", id="1", name="glob", input={"pattern": "*"})],
                 tool_calls=None)
        view = sa.analyze(NS(messages=[NS(role="user", content="q"), msg, NS(role="tool", content="a.py")]))
        self.assertEqual(view.calls, [("glob", {"pattern": "*"})])


class WorkspaceAdditionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src").mkdir()
        lines = [f"line {i}" for i in range(1, 301)]
        lines[199] = "def target_function(x):"
        (self.root / "src/mod.py").write_text("\n".join(lines) + "\n")
        self.ws = WorkspaceTool(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_window_read_and_find_line(self):
        self.assertEqual(self.ws.find_line("src/mod.py", ["target_function"]), 200)
        out = self.ws._read({"operation": "read", "path": "src/mod.py", "line": 200})
        self.assertEqual((out["start_line"], out["end_line"], out["total_lines"]), (185, 284, 300))
        self.assertIn("   200\tdef target_function(x):", out["text"])
        cand = self.ws.candidate_for_path("src/mod.py", 0, line=200)
        self.assertTrue(cand.id.startswith("win_"))
        self.assertTrue(self.ws.validate_candidate(cand))
        with self.assertRaises(ValueError):
            self.ws._read({"operation": "read", "path": "src/mod.py", "line": 0})

    def test_published_schema_is_unchanged(self):
        # Part of every request's cached prompt prefix: must stay byte-identical.
        self.assertEqual(self.ws.input_schema["properties"]["operation"]["enum"], ["read", "list"])
        self.assertEqual(set(self.ws.input_schema["properties"]), {"operation", "path"})
        self.assertEqual(self.ws.description, "Read or list non-hidden text files within the configured "
                                              "workspace. No writes, shell, or network.")

    def test_git_summary(self):
        run = lambda *a: subprocess.run(["git", *a], cwd=self.root, check=True, capture_output=True)
        self.assertIsNone(self.ws.git_candidate())
        run("init", "-q", "-b", "main")
        run("-c", "user.name=t", "-c", "user.email=t@x", "add", ".")
        run("-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "first commit")
        (self.root / "new.txt").write_text("x")
        cand = self.ws.git_candidate()
        self.assertTrue(self.ws.validate_candidate(cand))
        out = self.ws._read(cand.arguments)
        self.assertEqual(out["branch"], "main")
        self.assertEqual(out["commit_count"], 1)
        self.assertIn("?? new.txt", out["uncommitted"])
        self.assertTrue(out["recent_commits"][0].endswith("first commit"))
        with self.assertRaises(ValueError):
            self.ws._read({"operation": "git", "path": "src"})

    def test_relative(self):
        self.assertEqual(self.ws.relative(str(self.root / "src/mod.py")), "src/mod.py")
        self.assertIsNone(self.ws.relative("/etc/passwd"))


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "pkg").mkdir()
        (self.root / "pkg/contracts.py").write_text("x = 1\nclass Policy:\n    timeout_ms: int = 750\n")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.ws = WorkspaceTool(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_turn_start_named_file_gets_a_window_at_the_named_symbol(self):
        view = sa.analyze({"messages": [user("What is the default timeout_ms in pkg/contracts.py?")]})
        cands = sa.candidates_for(view, sa.TURN_START, self.ws)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].arguments, {"operation": "read", "path": "pkg/contracts.py", "line": 3})

    def test_turn_start_git_question(self):
        view = sa.analyze({"messages": [user("Which branch am I on?")]})
        self.assertEqual([c.id for c in sa.candidates_for(view, sa.TURN_START, self.ws)], ["git_status_summary"])
        self.assertEqual(sa.candidates_for(sa.analyze({"messages": [user("Explain caching")]}), sa.TURN_START, self.ws), [])

    def test_routine_after_search_points_at_the_hit(self):
        hit = {"output": {"results": [{"file": str(self.root / "pkg/contracts.py"), "line_number": 2,
                                        "content": "class Policy:"}]}}
        msgs = [user("where is Policy?"), assistant(("grep", {"pattern": "class Policy"})), tool(hit)]
        cands = sa.candidates_for(sa.analyze({"messages": msgs}), sa.ROUTINE, self.ws)
        self.assertEqual(cands[0].arguments, {"operation": "read", "path": "pkg/contracts.py", "line": 2})
        self.assertEqual(cands[0].origin, "last_tool_result")
        # After a read of that file there is nothing to prepare.
        msgs = [user("q"), assistant(("read_file", {"file_path": "pkg/contracts.py"})), tool("pkg/contracts.py:2")]
        self.assertEqual(sa.candidates_for(sa.analyze({"messages": msgs}), sa.ROUTINE, self.ws), [])

    def test_repeats_prepared(self):
        args = {"operation": "read", "path": "pkg/contracts.py", "line": 2}
        self.assertTrue(sa.repeats_prepared(args, [("read_file", {"file_path": str(self.root / "pkg/contracts.py")})],
                                            self.ws))
        self.assertFalse(sa.repeats_prepared(args, [("grep", {"pattern": "x"})], self.ws))
        self.assertTrue(sa.repeats_prepared({"operation": "git", "path": "."},
                                            [("bash", {"command": "git status"})], self.ws))


class PollRepeatTests(unittest.TestCase):
    POLL = ("bash", {"command": "sleep 30 && gh run view 1 --json status"})
    CHECK = ("bash", {"command": "sleep 20; curl -s localhost/health"})

    def view(self, *pairs):
        msgs = [user("wait for the deploy")]
        for call, result in pairs:
            msgs += [assistant(call), tool(result, "bash")]
        return sa.analyze({"messages": msgs})

    def test_pending_output_is_repeated(self):
        poll = ("bash", {"command": "sleep 30 && tail -1 build.log"})
        self.assertEqual(sa.poll_repeat(self.view((poll, "status: running")), {"repeat_polls": True}), poll)

    def test_unchanged_output_is_repeated_changed_or_done_is_not(self):
        poll = ("bash", {"command": "sleep 30 && tail -1 build.log"})
        on = {"repeat_polls": True}
        self.assertEqual(sa.poll_repeat(self.view((poll, "step 3/9"), (poll, "step 3/9")), on), poll)
        self.assertIsNone(sa.poll_repeat(self.view((poll, "step 3/9")), on))              # first sight, no signal
        self.assertIsNone(sa.poll_repeat(self.view((poll, "step 3/9"), (poll, "step 4/9")), on))
        self.assertIsNone(sa.poll_repeat(self.view((poll, "running"), (poll, "build finished")), on))
        self.assertIsNone(sa.poll_repeat(self.view((poll, "status: running")), {}))        # off by default

    def test_only_read_only_sleeping_single_calls(self):
        on = {"repeat_polls": True}
        self.assertIsNone(sa.poll_repeat(self.view((("bash", {"command": "tail -1 build.log"}), "running")), on))
        self.assertIsNone(sa.poll_repeat(self.view((("bash", {"command": "sleep 5; ./deploy.sh"}), "running")), on))
        self.assertIsNone(sa.poll_repeat(self.view((("bash", {"command": "sleep 5; rm x"}), "running")), on))

    def test_repeat_cap(self):
        poll = ("bash", {"command": "sleep 30 && tail -1 build.log"})
        pairs = [(poll, "running")] * 4
        self.assertIsNone(sa.poll_repeat(self.view(*pairs), {"repeat_polls": True, "poll_max_repeats": 3}))
        self.assertEqual(sa.poll_repeat(self.view(*pairs), {"repeat_polls": True, "poll_max_repeats": 4}), poll)


class PriceMathTests(unittest.TestCase):
    def test_host_saving_is_one_cached_read_plus_output(self):
        s = sa.host_step_saving(OPUS, prompt_tokens=100_000, output_tokens=100, cheap_output_tokens=100,
                                rates=DEFAULT_RATES)
        self.assertAlmostEqual(s, (100_000 * 0.20 + 100 * 20) / 1e6)

    def test_cold_haiku_costs_more_than_a_warm_opus_read(self):
        d = sa.cheaper_step(host=OPUS, cheap_models=[HAIKU, SONNET], prompt_tokens=70_000, cache_prefix={},
                            output_tokens=150, windows={HAIKU: 200_000}, rates=DEFAULT_RATES)
        self.assertIsNone(d["model"])
        self.assertEqual(d["reason"], "cold_cache_costs_more")

    def test_warm_haiku_wins(self):
        d = sa.cheaper_step(host=OPUS, cheap_models=[HAIKU], prompt_tokens=70_000, cache_prefix={HAIKU: 68_000},
                            output_tokens=150, windows={HAIKU: 200_000}, rates=DEFAULT_RATES)
        self.assertEqual(d["model"], HAIKU)
        self.assertGreater(d["saving_usd"], 0)

    def test_sonnet_never_wins_a_routine_step_against_opus(self):
        d = sa.cheaper_step(host=OPUS, cheap_models=[SONNET], prompt_tokens=263_000, cache_prefix={SONNET: 262_000},
                            output_tokens=150, windows={}, rates=DEFAULT_RATES)
        self.assertIsNone(d["model"])

    def test_context_window_and_never_route_up(self):
        d = sa.cheaper_step(host=OPUS, cheap_models=[HAIKU], prompt_tokens=190_000, cache_prefix={HAIKU: 189_000},
                            output_tokens=150, windows={HAIKU: 200_000}, rates=DEFAULT_RATES)
        self.assertEqual(d["reason"], "context_window")
        up = sa.cheaper_step(host=HAIKU, cheap_models=[SONNET, OPUS], prompt_tokens=10_000, cache_prefix={},
                             output_tokens=150, windows={}, rates=DEFAULT_RATES)
        self.assertEqual(up["reason"], "not_cheaper_tier")

    def test_amortized_cold_start(self):
        kw = dict(host=OPUS, cheap_models=[HAIKU], prompt_tokens=70_000, cache_prefix={}, output_tokens=150,
                  windows={HAIKU: 200_000}, rates=DEFAULT_RATES)
        self.assertIsNone(sa.cheaper_step(**kw, amortize_steps=2)["model"])
        self.assertEqual(sa.cheaper_step(**kw, amortize_steps=30)["model"], HAIKU)

    def test_prepared_results_must_pay_for_themselves(self):
        small = sa.max_prepared_tokens(OPUS, prompt_tokens=70_000, output_tokens=150, rates=DEFAULT_RATES)
        big = sa.max_prepared_tokens(OPUS, prompt_tokens=263_000, output_tokens=150, rates=DEFAULT_RATES)
        self.assertAlmostEqual(small, (70_000 * 0.20 + 150 * (20 + 5)) / 5.0, delta=1)
        self.assertGreater(big, small)
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "small.py").write_text("x = 1\n")
            Path(tmp, "huge.py").write_text("y = 2\n" * 20_000)
            ws = WorkspaceTool(tmp, max_bytes=262_144)
            cands = [ws.candidate_for_path("small.py", 0), ws.candidate_for_path("huge.py", 1),
                     ws.candidate_for_path("huge.py", 2, line=500)]
            kept = sa.affordable(cands, ws, small)
        self.assertEqual([c.arguments.get("path") for c in kept], ["small.py", "huge.py"])
        self.assertEqual(kept[1].arguments.get("line"), 500)          # a window fits, the whole file does not

    def test_expected_saving_gate(self):
        self.assertGreater(sa.expected_prepared_saving_s(asked=0, accepted=0, prior_accept=0.5, host_call_s=3.0,
                                                         judge_s=0.15), 0)
        self.assertLess(sa.expected_prepared_saving_s(asked=20, accepted=0, prior_accept=0.5, host_call_s=3.0,
                                                      judge_s=0.15), 0.3)
        self.assertLess(sa.expected_prepared_saving_s(asked=0, accepted=0, prior_accept=0.0, host_call_s=3.0,
                                                      judge_s=0.15), 0)


class PolicyTests(unittest.TestCase):
    def test_validation(self):
        Policy(step_actions={"prepared": True, "cheap_models": [HAIKU], "context_windows": {HAIKU: 200_000}})
        for bad in ({"nope": 1}, {"prepared": "yes"}, {"amortize_steps": 0}, {"cheap_models": "haiku"},
                    {"prior_accept": 2}):
            with self.assertRaises(ValueError, msg=bad):
                Policy(step_actions=bad)


# --- loop integration ----------------------------------------------------------
class ScriptedProvider:
    name = "anthropic-primary"
    default_model = OPUS

    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    async def complete(self, request, **kwargs):
        self.calls.append(kwargs.get("model"))
        return self.responses.pop(0)

    def get_info(self):
        return {"name": self.name}


def reply(calls=(), *, inp=70_000, cr=69_000, cw=1_000, out=150, cost=None):
    usage = dict(input_tokens=inp, output_tokens=out, cache_read_tokens=cr, cache_write_tokens=cw)
    if cost is not None:
        usage["cost_usd"] = cost
    return NS(content=[], tool_calls=[NS(id=f"t{i}", name=n, arguments=a) for i, (n, a) in enumerate(calls)],
              finish_reason="tool_use" if calls else "end_turn", usage=NS(**usage), model=None)


class ExecWorkspace:
    """WorkspaceTool.execute without amplifier_core (not installed in unit tests)."""
    def __init__(self, ws):
        self.ws = ws

    async def execute(self, input, **kwargs):
        return NS(success=True, output=self.ws._read(input))


class PickJudge:
    """next_action: pick the first candidate whose id starts with ``prefix``."""
    name = "jev"
    external = False

    def __init__(self, prefix=None, next_step=None):
        self.prefix, self.next_step, self.asked = prefix, next_step, []

    async def ask(self, request):
        self.asked.append(request)
        if request.questions and request.questions[0].name == sa.NEXT_STEP_QUESTION:
            return DecisionResult(action=Decision(choice=SLOW, probabilities={SLOW: 1.0}),
                                  answers={sa.NEXT_STEP_QUESTION: Answer(probabilities=self.next_step)})
        ids = [c.id for c in request.candidates] + [SLOW]
        pick = next((i for i in ids if self.prefix and i.startswith(self.prefix)), SLOW)
        probs = {i: (0.95 if i == pick else 0.05 / (len(ids) - 1)) for i in ids}
        return DecisionResult(action=Decision(choice=pick, probabilities=probs))

    async def close(self):
        pass


class LoopTests(unittest.IsolatedAsyncioTestCase):
    def setup(self, judge, step_actions, responses, root=None):
        events = []
        coordinator = DemoCoordinator()
        emitter = Emitter(coordinator.session_id, callback=events.append)
        policy = Policy(mode="active", read_shortcut=False, allowed_tools=("fast_workspace",),
                        step_actions=step_actions)
        service = DecisionService(policy, judge, emitter, coordinator, [])
        runtime = Runtime(service)
        provider = ScriptedProvider(responses)
        tools = {"fast_workspace": WorkspaceTool(root)} if root else {}
        facade = RoutedProvider(provider, runtime, tools, demo_response, "anthropic-primary")
        service.turn = TurnState("t1")
        return service, facade, provider, events

    def receipts(self, events):
        return [e["data"] for e in events if e["event"] == efficiency.EVENT]

    def steps(self, events):
        return [e["data"] for e in events if e["event"] == "fast_decisions:step_decided"]

    async def test_off_by_default_emits_no_step_events(self):
        service, facade, provider, events = self.setup(PickJudge(), None, [reply()])
        await facade.complete({"messages": [user("hi")], "tools": [], "tool_choice": "auto"})
        self.assertEqual(self.steps(events), [])

    async def test_prepared_git_summary_skips_a_call_and_is_priced_from_the_next_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
            service, facade, provider, events = self.setup(PickJudge("git_"), {"prepared": True},
                                                           [reply(inp=71_000, cr=0, cw=40_000, out=40)], root=tmp)
            req = {"messages": [user("Which git branch am I on?")], "tools": [{"name": "fast_workspace"}],
                   "tool_choice": "auto"}
            first = await facade.complete(req)
            self.assertEqual(first.tool_calls[0].name, "fast_workspace")
            self.assertEqual(provider.calls, [])                       # no model call
            call_id = first.tool_calls[0].id
            observed = ObservedTool(ExecWorkspace(facade._tools["fast_workspace"]), facade._runtime, "fast_workspace")
            result = await observed.execute({"operation": "git", "path": "."})
            self.assertTrue(result.success)
            chars = service.turn.tool_decisions[call_id]["result_chars"]
            req2 = {"messages": req["messages"] + [assistant(("fast_workspace", {"operation": "git", "path": "."})),
                                                   tool(json.dumps(result.output), "fast_workspace")],
                    "tools": [{"name": "fast_workspace"}], "tool_choice": "auto"}
            await facade.complete(req2)
        self.assertEqual(provider.calls, [None])
        steps = self.steps(events)
        self.assertEqual([s["step_action"] for s in steps], ["prepared", "full"])
        self.assertEqual(steps[0]["step_class"], sa.TURN_START)
        self.assertEqual(steps[0]["mechanism"], "jev:next_action")
        receipt = next(r for r in self.receipts(events) if r["lever"] == "prepared_action")
        self.assertEqual(receipt["calls_saved"], 1)
        self.assertEqual(receipt["mechanism"], "jev:next_action")
        skipped = 111_000 - chars // 4
        expected = price(OPUS, {"input": skipped, "cache_read": skipped, "output": 150}, DEFAULT_RATES)
        self.assertAlmostEqual(receipt["usd_saved"], round(expected, 8))
        self.assertEqual(receipt["baseline"]["model"], OPUS)

    async def test_model_refetching_the_prepared_result_saves_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a.py").write_text("def foo_bar():\n    return 1\n")
            service, facade, provider, events = self.setup(
                PickJudge("win_"), {"prepared": True},
                [reply([("read_file", {"file_path": str(Path(tmp, "a.py"))})])], root=tmp)
            req = {"messages": [user("What does foo_bar in a.py return?")], "tools": [{"name": "fast_workspace"}],
                   "tool_choice": "auto"}
            first = await facade.complete(req)
            req["messages"] += [assistant(("fast_workspace", first.tool_calls[0].arguments)), tool("...", "fast_workspace")]
            await facade.complete(req)
        receipt = next(r for r in self.receipts(events) if r["lever"] == "prepared_action")
        self.assertEqual(receipt["decision"], "prepared_repeated_by_model")
        self.assertEqual(receipt["calls_saved"], 0)
        self.assertLessEqual(receipt["usd_saved"], 0)

    async def test_judge_declining_is_charged_as_overhead_and_the_model_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
            service, facade, provider, events = self.setup(PickJudge(None), {"prepared": True}, [reply()], root=tmp)
            await facade.complete({"messages": [user("which branch?")], "tools": [{"name": "fast_workspace"}],
                                   "tool_choice": "auto"})
        self.assertEqual(provider.calls, [None])
        overhead = [r for r in self.receipts(events) if r["decision"] == "judge_declined_prepared"]
        self.assertEqual(len(overhead), 1)
        self.assertEqual(overhead[0]["lever"], "prepared_action")
        self.assertLess(overhead[0]["seconds_saved"], 0)
        self.assertTrue(self.steps(events)[0]["judge_asked"])

    async def test_expected_saving_gate_skips_the_judge(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
            judge = PickJudge("git_")
            service, facade, provider, events = self.setup(judge, {"prepared": True, "prior_accept": 0.0},
                                                           [reply()], root=tmp)
            await facade.complete({"messages": [user("which branch?")], "tools": [{"name": "fast_workspace"}],
                                   "tool_choice": "auto"})
        self.assertEqual(judge.asked, [])
        self.assertEqual(provider.calls, [None])

    async def test_sleep_poll_is_repeated_without_a_model_call(self):
        poll = ("bash", {"command": "sleep 30 && tail -1 build.log"})
        service, facade, provider, events = self.setup(PickJudge(), {"repeat_polls": True}, [reply()])
        facade._tools["bash"] = object()
        req = {"messages": [user("wait for the build"), assistant(poll), tool("status: running", "bash")],
               "tools": [{"name": "bash"}], "tool_choice": "auto"}
        response = await facade.complete(req)
        self.assertEqual(provider.calls, [])
        self.assertEqual((response.tool_calls[0].name, response.tool_calls[0].arguments), poll)
        step = self.steps(events)[0]
        self.assertEqual((step["step_action"], step["mechanism"]), ("prepared", "rule:poll_repeat"))
        req["messages"] += [assistant(poll), tool("status: done", "bash")]
        await facade.complete(req)                                  # changed output: the model runs
        self.assertEqual(provider.calls, [None])
        receipt = next(r for r in self.receipts(events) if r["lever"] == "prepared_action")
        self.assertEqual((receipt["mechanism"], receipt["calls_saved"]), ("rule:poll_repeat", 1))

    def _routine_request(self):
        return {"messages": [user("find x"), assistant(("grep", {"pattern": "x"})), tool('{"results": []}')],
                "tools": [], "tool_choice": "auto"}

    async def _warm(self, facade, service, haiku_prefix):
        st = facade._step_state(service)
        import time
        st["last_prompt"] = 70_000
        st["cache"][HAIKU] = {"prefix": haiku_prefix, "t": time.monotonic()}
        st["host_last"] = time.monotonic()

    async def test_cold_cheap_cache_keeps_the_host(self):
        service, facade, provider, events = self.setup(PickJudge(), {"cheaper_model": True}, [reply()])
        await self._warm(facade, service, 0)
        facade._step_state(service)["cache"].clear()
        await facade.complete(self._routine_request())
        self.assertEqual(provider.calls, [None])
        step = self.steps(events)[0]
        self.assertEqual((step["step_class"], step["step_action"], step["reason_code"]),
                         (sa.ROUTINE, "full", "cold_cache_costs_more"))

    async def test_routine_step_on_a_warm_cheaper_model_with_receipt(self):
        service, facade, provider, events = self.setup(
            PickJudge(), {"cheaper_model": True}, [reply([("read_file", {"file_path": "a"})], cr=68_000, cw=2_000)])
        await self._warm(facade, service, 68_000)
        await facade.complete(self._routine_request())
        self.assertEqual(provider.calls, [HAIKU])
        step = self.steps(events)[0]
        self.assertEqual((step["step_action"], step["mechanism"], step["cheap_model"]),
                         ("cheaper_model", "rule:routine_readonly", HAIKU))
        receipt = self.receipts(events)[0]
        self.assertEqual((receipt["lever"], receipt["mechanism"]), ("cheaper_model", "rule:routine_readonly"))
        self.assertEqual((receipt["baseline"]["model"], receipt["actual"]["model"]), (OPUS, HAIKU))
        self.assertGreater(receipt["usd_saved"], 0)

    async def test_cheaper_model_edit_is_discarded_and_the_host_reruns(self):
        service, facade, provider, events = self.setup(
            PickJudge(), {"cheaper_model": True},
            [reply([("edit_file", {"file_path": "a"})]), reply([("edit_file", {"file_path": "a"})])])
        await self._warm(facade, service, 68_000)
        req = self._routine_request()
        response = await facade.complete(req)
        self.assertEqual(provider.calls, [HAIKU, None])
        self.assertIsNone(req.get("model"))
        self.assertEqual(response.tool_calls[0].name, "edit_file")
        decisions = [r["decision"] for r in self.receipts(events)]
        self.assertIn("cheap_step_discarded_not_read_only", decisions)
        discarded = next(r for r in self.receipts(events) if r["decision"].startswith("cheap_step_discarded"))
        self.assertLess(discarded["usd_saved"], 0)
        self.assertEqual(discarded["calls_saved"], -1)

    async def test_ambiguous_step_asks_the_judge_changes_code(self):
        judge = PickJudge(next_step={"read_only": 0.9, "changes_code": 0.1})
        service, facade, provider, events = self.setup(
            judge, {"cheaper_model": True, "judge_ambiguous": True, "min_judge_saving_usd": 0.0},
            [reply([("grep", {"pattern": "y"})])])
        await self._warm(facade, service, 68_000)
        req = {"messages": [user("q"), assistant(("bash", {"command": "python3 x.py"})), tool("out")],
               "tools": [], "tool_choice": "auto"}
        await facade.complete(req)
        self.assertEqual(provider.calls, [HAIKU])
        self.assertEqual(self.steps(events)[0]["mechanism"], "jev:changes_code")
        self.assertEqual(self.receipts(events)[0]["mechanism"], "jev:changes_code")

    async def test_ambiguous_without_judge_stays_on_host(self):
        service, facade, provider, events = self.setup(PickJudge(), {"cheaper_model": True}, [reply()])
        await self._warm(facade, service, 68_000)
        await facade.complete({"messages": [user("q"), assistant(("bash", {"command": "python3 x.py"})),
                                            tool("out")], "tools": [], "tool_choice": "auto"})
        self.assertEqual(provider.calls, [None])
        self.assertEqual(self.steps(events)[0]["reason_code"], "ambiguous_not_judged")

    async def test_receipts_sum_to_the_counterfactual_difference(self):
        """A prepared step plus a cheap step: the receipts' usd_saved equals
        host-only cost (the same steps on the default model, as priced by the
        receipts' own method) minus what actually ran."""
        service, facade, provider, events = self.setup(
            PickJudge(), {"cheaper_model": True}, [reply([("grep", {"pattern": "z"})], cr=68_000, cw=2_000)])
        await self._warm(facade, service, 68_000)
        await facade.complete(self._routine_request())
        r = self.receipts(events)[0]
        self.assertAlmostEqual(r["usd_saved"], r["baseline"]["cost_usd"] - r["actual"]["cost_usd"], places=7)
        haiku_cost = price(HAIKU, {"input": 70_000, "cache_read": 68_000, "cache_write": 2_000, "output": 150},
                           DEFAULT_RATES)
        self.assertAlmostEqual(r["actual"]["cost_usd"], haiku_cost, places=7)


if __name__ == "__main__":
    unittest.main()
