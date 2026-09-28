"""Jevgrep wrapper contracts; no paid provider calls or real credentials."""
import asyncio
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from amplifier_fast_decisions.jevgrep import JevgrepTool, _run_bounded, mount


class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / "src").mkdir()
        self.tool = JevgrepTool(root=self.root, allow_external_state=True)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_source_permission_is_independent_and_disabled_by_default(self):
        with patch("amplifier_fast_decisions.jevgrep._run_bounded", new_callable=AsyncMock) as run:
            result = await JevgrepTool(root=self.root).search({"query": "Find code"})
            self.assertEqual(result["status"], "disabled")
            run.assert_not_awaited()

    async def test_argv_has_no_shell_and_keeps_exclusions_and_limits(self):
        query = 'where is auth? $(touch /tmp/never-execute) --include-sensitive'
        responses = [(0, "0.4.0\n", False), (0, "Source\nEnd context.\n", False)]
        with patch("shutil.which", return_value="/trusted/jg"), \
             patch("amplifier_fast_decisions.jevgrep._run_bounded", new_callable=AsyncMock,
                   side_effect=responses) as run:
            result = await self.tool.search({"query": query, "path": "src"})
        self.assertEqual(result["status"], "complete")
        argv = run.await_args_list[1].args[0]
        self.assertEqual(argv, ["/trusted/jg", "--no-cache", "--concurrency", "4",
                                "--max-source-bytes", "32768", "--", query, str(self.root / "src")])
        self.assertEqual(run.await_args_list[1].kwargs["max_bytes"], 65536)
        self.assertIn("unknown", result["usage"])

    async def test_wrong_version_stops_before_source_search(self):
        with patch("shutil.which", return_value="/trusted/jg"), \
             patch("amplifier_fast_decisions.jevgrep._run_bounded", new_callable=AsyncMock,
                   return_value=(0, "0.3.0\n", False)) as run:
            result = await self.tool.search({"query": "Find code"})
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(run.await_count, 1)

    async def test_absolute_workspace_directory_does_not_require_retry(self):
        with patch("shutil.which", return_value="/trusted/jg"), \
             patch("amplifier_fast_decisions.jevgrep._run_bounded", new_callable=AsyncMock,
                   side_effect=[(0, "0.4.0", False), (0, "End context.", False)]) as run:
            result = await self.tool.search({"query": "Find code", "path": str(self.root / "src")})
        self.assertEqual(result["status"], "complete")
        self.assertEqual(run.await_args_list[1].args[0][-1], str(self.root / "src"))

    async def test_incomplete_truncation_and_failure_are_not_complete(self):
        for code, text, truncated, status in [
            (2, "Some source\nEnd context.", False, "incomplete"),
            (130, "Interrupted source", False, "incomplete"),
            (0, "Partial response", False, "incomplete"),
            (0, "Partial bytes", True, "incomplete"),
            (1, "sensitive provider diagnostic", False, "failed"),
        ]:
            with self.subTest(code=code, truncated=truncated), \
                 patch("shutil.which", return_value="/trusted/jg"), \
                 patch("amplifier_fast_decisions.jevgrep._run_bounded", new_callable=AsyncMock,
                       side_effect=[(0, "0.4.0", False), (code, text, truncated)]):
                result = await self.tool.search({"query": "Find code"})
                self.assertEqual(result["status"], status)
                if status == "failed":
                    self.assertNotIn("sensitive", str(result))

    async def test_invalid_inputs_never_start_cli(self):
        (self.root / "link").symlink_to(self.root / "src", target_is_directory=True)
        (self.root / "file.py").touch()
        bad = [{"query": "x", "path": "../"}, {"query": "x", "path": "/tmp"},
               {"query": "x", "path": "link"}, {"query": "x", "path": str(self.root / "link")},
               {"query": "x", "path": str(self.root / "../")}, {"query": "x", "path": ".git"},
               {"query": "x", "path": "file.py"}, {"query": "x", "include_sensitive": True},
               {"query": " "}, {"query": "x" * 2001}, {"query": "x\x00y"}, []]
        with patch("amplifier_fast_decisions.jevgrep._run_bounded", new_callable=AsyncMock) as run:
            for value in bad:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    await self.tool.search(value)
            run.assert_not_awaited()

    async def test_missing_executable_and_timeout_are_actionable(self):
        with patch("shutil.which", return_value=None):
            self.assertEqual((await self.tool.search({"query": "x"}))["status"], "unavailable")
        async def slow(*args, **kwargs):
            await asyncio.sleep(5)
        self.tool.timeout_ms = 20
        with patch("shutil.which", return_value="/trusted/jg"), \
             patch("amplifier_fast_decisions.jevgrep._run_bounded", side_effect=slow):
            self.assertEqual((await self.tool.search({"query": "x"}))["status"], "timeout")

    async def test_mount_adds_ordinary_tool_and_rejects_unknown_config(self):
        coordinator = AsyncMock()
        await mount(coordinator, {"root": str(self.root)})
        args = coordinator.mount.await_args
        self.assertEqual(args.args[0], "tools")
        self.assertIsInstance(args.args[1], JevgrepTool)
        self.assertEqual(args.kwargs["name"], "jevgrep")
        with self.assertRaises(ValueError):
            await mount(coordinator, {"root": str(self.root), "include_sensitive": True})

    async def test_real_child_output_is_bounded(self):
        code, text, truncated = await _run_bounded(
            [sys.executable, "-c", "print('x'*1000000)"], cwd=self.root, max_bytes=1024)
        self.assertTrue(truncated)
        self.assertEqual(len(text), 1024)

    async def test_cancel_kills_the_real_child(self):
        marker = self.root / "pid"
        program = "import os,time,pathlib; pathlib.Path('pid').write_text(str(os.getpid())); time.sleep(60)"
        task = asyncio.create_task(_run_bounded([sys.executable, "-c", program], cwd=self.root, max_bytes=1024))
        async with asyncio.timeout(5):
            while not marker.exists():
                await asyncio.sleep(0.01)
        pid = int(marker.read_text())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_child_does_not_receive_unrelated_keys_or_preload_hooks(self):
        program = "import os; print(','.join(k for k in ['UNRELATED_API_KEY','NODE_OPTIONS'] if k in os.environ))"
        with patch.dict(os.environ, {"UNRELATED_API_KEY": "synthetic-test-only", "NODE_OPTIONS": "not forwarded"}):
            code, text, _ = await _run_bounded([sys.executable, "-c", program], cwd=self.root, max_bytes=1024)
        self.assertEqual(code, 0)
        self.assertEqual(text.strip(), "")

    @unittest.skipUnless(importlib.util.find_spec("amplifier_core"), "actual host ToolResult unavailable")
    async def test_real_host_tool_result_envelope(self):
        from amplifier_core.models import ToolResult
        with patch.object(self.tool, "search", return_value={"status": "incomplete", "content": "source"}):
            result = await self.tool.execute({"query": "x"})
        self.assertIsInstance(result, ToolResult)
        self.assertTrue(result.success)
        self.assertEqual(result.output["status"], "incomplete")

    @unittest.skipUnless(os.getenv("AFAST_TEST_JEVGREP"), "optional installed upstream CLI check")
    async def test_real_pinned_cli_refuses_missing_auth_without_searching(self):
        # No key is provisioned: this exercises installed CLI flags/version and
        # the error boundary, not provider-backed retrieval.
        tool = JevgrepTool(root=self.root, executable=os.environ["AFAST_TEST_JEVGREP"],
                           allow_external_state=True)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "empty-config")}):
            result = await tool.search({"query": "Where are events recorded?"})
        self.assertEqual(result["status"], "failed")


class ConfigTests(unittest.TestCase):
    def test_environment_credential_is_private_ephemeral_and_saved_choice_wins(self):
        import json
        import stat
        from amplifier_fast_decisions.jevgrep import _credentials_env
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
                "XDG_CONFIG_HOME": root, "TYPESAFE_API_KEY": "fixture-not-a-secret"}):
            with self.assertRaises(RuntimeError):
                with _credentials_env() as env:
                    temporary = Path(env["XDG_CONFIG_HOME"])
                    credential = temporary / "jevgrep/credentials.json"
                    self.assertEqual(stat.S_IMODE(credential.stat().st_mode), 0o600)
                    self.assertEqual(json.loads(credential.read_text())["provider"], "typesafe")
                    raise RuntimeError("cancelled search")
            self.assertFalse(temporary.exists())
            saved = Path(root) / "jevgrep/credentials.json"
            saved.parent.mkdir()
            saved.write_text("saved provider remains untouched")
            with _credentials_env() as env:
                self.assertEqual(env, {})
            self.assertEqual(saved.read_text(), "saved provider remains untouched")

    def test_unbounded_settings_are_refused(self):
        for args in [{"max_source_bytes": 0}, {"concurrency": 100}, {"timeout_ms": False},
                     {"allow_external_state": "true"}, {"max_output_bytes": 0}]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                JevgrepTool(**args)


if __name__ == "__main__":
    unittest.main()
