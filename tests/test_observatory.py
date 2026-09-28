"""Unit tests for the auto-observatory.

Three parts:

1. ``observatory.py``'s pure state-file/spawn/health helpers, and
   ``cli.stop_server`` -- fully offline, fakes injected for spawner/alive/
   sleep/opener. No real browser, no real network, and no real detached
   subprocess except in the two ``StopServerTests`` cases that need a real
   pid to signal (a short-lived ``sleep`` helper process we spawn and clean
   up ourselves).
2. ``observer.py``'s ``session:start`` wiring: skip rules (child session,
   env kill-switches, non-TTY), ``open_browser`` modes, once-per-session,
   and "the handler itself never raises". These patch
   ``observer.ensure_viewer``/``observer.open_page`` so no real subprocess
   or browser is ever touched. ``observer.mount`` imports ``amplifier_core``
   unconditionally (same as every other ``observer.mount`` caller in this
   suite -- see ``tests/test_shadow_no_behaviour_change.py``), so this
   section is real-kernel-lane only (``skipUnless(HAS_CORE, ...)``).
3. A real-kernel lane test that fires the trigger through an actual
   ``StreamingOrchestrator`` turn (``amplifier_core``'s real
   ``MockCoordinator``/hook registry, not the offline ``DemoCoordinator``),
   confirming the wiring holds against the real mount-point/event contract,
   not just our own fake harness.
"""

from __future__ import annotations

import asyncio
import importlib.util
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from amplifier_fast_decisions import observatory
from amplifier_fast_decisions.cli import stop_server
from amplifier_fast_decisions.demo import DemoCoordinator

HAS_CORE = importlib.util.find_spec("amplifier_core") is not None
HAS_LOOP = importlib.util.find_spec("amplifier_module_loop_streaming") is not None


# ---------------------------------------------------------------------------
# observatory.py: pure helpers
# ---------------------------------------------------------------------------


class EnsureViewerTests(unittest.TestCase):
    def test_reuse_when_state_file_says_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "serve.json"
            observatory.write_state_atomic(
                state_file,
                {"pid": 1, "port": 8765, "url": "http://127.0.0.1:8765/#token=abc",
                 "build": observatory.build_id()},
            )
            spawner = mock.Mock()
            url = observatory.ensure_viewer(
                tmp,
                8765,
                state_file,
                spawner=spawner,
                alive=lambda state: True,
                sleep=lambda seconds: None,
            )
            self.assertEqual(url, "http://127.0.0.1:8765/#token=abc")
            spawner.assert_not_called()

    def test_replaces_a_live_viewer_from_another_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "serve.json"
            observatory.write_state_atomic(
                state_file, {"pid": 1, "port": 8765, "url": "http://old/", "build": "stale0000000"})
            spawned = []

            def spawner(argv):
                spawned.append(argv)
                observatory.write_state_atomic(
                    state_file, {"pid": 2, "port": 8765, "url": "http://new/", "build": observatory.build_id()})

            with mock.patch.object(observatory.os, "kill") as kill:
                url = observatory.ensure_viewer(tmp, 8765, state_file, spawner=spawner,
                                                alive=lambda state: True, sleep=lambda seconds: None)
            self.assertEqual(url, "http://new/")
            self.assertEqual(len(spawned), 1)
            kill.assert_not_called()          # pid 1 is never signalled

    def test_build_id_is_stable_and_short(self):
        self.assertEqual(observatory.build_id(), observatory.build_id())
        self.assertEqual(len(observatory.build_id()), 12)

    def test_stale_viewer_is_never_returned_after_replacement(self):
        # The replacement spawn does not come up: the stale viewer's state
        # must be gone, not re-read and handed back as if it were current.
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "serve.json"
            observatory.write_state_atomic(
                state_file, {"pid": 4242, "port": 8765, "url": "http://old/", "build": "stale0000000"})
            with mock.patch.object(observatory.os, "kill"):
                url = observatory.ensure_viewer(tmp, 8765, state_file, spawner=lambda argv: None,
                                                alive=lambda state: True, sleep=lambda seconds: None,
                                                deadline_s=0.3)
            self.assertIsNone(url)
            self.assertIsNone(observatory.read_state(state_file))

    def test_build_id_changes_when_any_dashboard_asset_changes(self):
        import shutil

        package = Path(observatory.__file__).resolve().parent
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "pkg"
            shutil.copytree(package / "static", copy / "static")
            for name in ("server.py", "savings.py", "operations.py"):
                shutil.copy(package / name, copy / name)
            fake_module = str(copy / "observatory.py")
            with mock.patch.object(observatory, "__file__", fake_module):
                base = observatory.build_id()
                for asset in ("static/app.js", "static/index.html", "static/style.css", "server.py", "savings.py"):
                    original = (copy / asset).read_bytes()
                    (copy / asset).write_bytes(original + b"\n/* changed */\n")
                    self.assertNotEqual(observatory.build_id(), base, asset)
                    (copy / asset).write_bytes(original)
                self.assertEqual(observatory.build_id(), base)

    def test_spawns_when_no_state_file_and_becomes_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "serve.json"
            calls = []

            def fake_spawner(argv):
                calls.append(argv)
                observatory.write_state_atomic(
                    state_file,
                    {
                        "pid": 999,
                        "port": 8765,
                        "url": "http://127.0.0.1:8765/#token=xyz",
                    },
                )

            url = observatory.ensure_viewer(
                tmp,
                8765,
                state_file,
                spawner=fake_spawner,
                alive=lambda state: True,
                sleep=lambda seconds: None,
            )
            self.assertEqual(url, "http://127.0.0.1:8765/#token=xyz")
            self.assertEqual(len(calls), 1)
            self.assertIn("serve", calls[0])
            self.assertIn("--state-file", calls[0])

    def test_returns_none_when_never_becomes_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "serve.json"
            url = observatory.ensure_viewer(
                tmp,
                8765,
                state_file,
                spawner=lambda argv: None,
                alive=lambda state: False,
                sleep=lambda seconds: None,
                deadline_s=0.05,
            )
            self.assertIsNone(url)

    def test_never_raises_on_spawner_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "serve.json"

            def bad_spawner(argv):
                raise RuntimeError("boom")

            url = observatory.ensure_viewer(
                tmp,
                8765,
                state_file,
                spawner=bad_spawner,
                sleep=lambda seconds: None,
            )
            self.assertIsNone(url)

    def test_never_raises_on_alive_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "serve.json"
            observatory.write_state_atomic(state_file, {"pid": 1, "port": 8765})

            def bad_alive(state):
                raise RuntimeError("boom")

            url = observatory.ensure_viewer(
                tmp,
                8765,
                state_file,
                spawner=lambda argv: None,
                alive=bad_alive,
                sleep=lambda seconds: None,
            )
            self.assertIsNone(url)


class OpenPageTests(unittest.TestCase):
    def test_calls_opener(self):
        calls = []
        observatory.open_page("http://x", opener=calls.append)
        self.assertEqual(calls, ["http://x"])

    def test_never_raises(self):
        def bad_opener(url):
            raise RuntimeError("boom")

        observatory.open_page("http://x", opener=bad_opener)  # must not raise


class StateFileTests(unittest.TestCase):
    def test_atomic_write_then_read_mode_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "sub" / "serve.json"
            observatory.write_state_atomic(state_file, {"pid": 1, "port": 2})
            self.assertEqual(state_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(observatory.read_state(state_file), {"pid": 1, "port": 2})

    def test_read_missing_returns_none(self):
        self.assertIsNone(observatory.read_state("/nonexistent/path/serve.json"))

    def test_remove_missing_is_a_noop(self):
        observatory.remove_state("/nonexistent/path/serve.json")  # must not raise


class StopServerTests(unittest.TestCase):
    def test_no_state_file_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = stop_server(Path(tmp) / "serve.json")
            self.assertEqual(code, 1)

    def test_stops_a_live_pid_and_removes_state(self):
        # The command line must contain "amplifier_fast_decisions" so
        # ``pid_looks_like_viewer`` (identity check before signalling, see
        # ``observatory.py``) treats this stand-in process as a plausible
        # viewer -- a real ``afast serve`` invocation always carries that
        # module name in its argv.
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)  # amplifier_fast_decisions"]
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_file = Path(tmp) / "serve.json"
                observatory.write_state_atomic(
                    state_file,
                    {
                        "pid": proc.pid,
                        "port": 8799,
                        "url": "http://127.0.0.1:8799/#token=t",
                    },
                )
                code = stop_server(state_file)
                self.assertEqual(code, 0)
                self.assertFalse(state_file.exists())
                proc.wait(timeout=5)
                self.assertNotEqual(proc.returncode, 0)  # killed by SIGTERM
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_stale_pid_still_removes_state_and_exits_0(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "serve.json"
            observatory.write_state_atomic(
                state_file,
                {"pid": 2**30, "port": 8799, "url": "http://127.0.0.1:8799/#token=t"},
            )
            code = stop_server(state_file)
            self.assertEqual(code, 0)
            self.assertFalse(state_file.exists())

    def test_reused_pid_is_not_signalled(self):
        """Hardening: if the recorded pid is alive but its command line no
        longer looks like a viewer (the classic pid-reuse race -- the viewer
        died and the OS handed that pid to an unrelated process), the
        production ``stop_server`` path must NOT send it a signal. The state
        file is still cleared (the recorded viewer is gone either way) and
        the call still exits 0.
        """
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_file = Path(tmp) / "serve.json"
                observatory.write_state_atomic(
                    state_file,
                    {
                        "pid": proc.pid,
                        "port": 8799,
                        "url": "http://127.0.0.1:8799/#token=t",
                    },
                )
                code = stop_server(state_file)
                self.assertEqual(code, 0)
                self.assertFalse(state_file.exists())
                # Give a would-be signal a moment to land, then confirm the
                # process is still alive -- it was never signalled.
                time.sleep(0.2)
                self.assertIsNone(proc.poll())
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_pid_looks_like_viewer_true_for_marked_process(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(5)  # amplifier_fast_decisions"]
        )
        try:
            time.sleep(0.2)
            self.assertTrue(observatory.pid_looks_like_viewer(proc.pid))
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_pid_looks_like_viewer_false_for_unrelated_process(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            time.sleep(0.2)
            self.assertFalse(observatory.pid_looks_like_viewer(proc.pid))
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_pid_looks_like_viewer_true_when_pid_gone(self):
        # Can't verify identity of a pid that no longer exists -- fail open
        # (this pid is essentially guaranteed never to exist; see
        # test_stale_pid_still_removes_state_and_exits_0 above).
        self.assertTrue(observatory.pid_looks_like_viewer(2**30))


class ServePortFallbackTests(unittest.TestCase):
    """`afast serve` binds a real OS socket, so the busy-port scenario is
    exercised via a real subprocess against a real pre-bound socket, not a
    mock -- the same style already used by StopServerTests above."""

    @staticmethod
    def _wait_for_state(state_file: Path, timeout: float = 5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = observatory.read_state(state_file)
            if state is not None:
                return state
            time.sleep(0.05)
        return None

    def test_falls_back_to_free_port_when_requested_port_is_busy(self):
        busy_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        busy_socket.bind(("127.0.0.1", 0))
        busy_socket.listen()
        busy_port = busy_socket.getsockname()[1]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                events_dir = Path(tmp) / "events"
                events_dir.mkdir()
                state_file = Path(tmp) / "serve.json"
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "amplifier_fast_decisions",
                        "serve",
                        "--events",
                        str(events_dir),
                        "--port",
                        str(busy_port),
                        "--state-file",
                        str(state_file),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    state = self._wait_for_state(state_file)
                    self.assertIsNotNone(state, "server never wrote a state file")
                    assert state is not None  # narrows for the type checker
                    self.assertNotEqual(state["port"], busy_port)
                    self.assertIn(str(state["port"]), state["url"])
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{state['port']}/", method="GET"
                    )
                    with urllib.request.urlopen(request, timeout=2) as response:
                        self.assertEqual(response.status, 200)
                finally:
                    code = stop_server(state_file)
                    self.assertEqual(code, 0)
                    proc.wait(timeout=5)
        finally:
            busy_socket.close()

    def test_no_fallback_exits_nonzero_with_message(self):
        busy_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        busy_socket.bind(("127.0.0.1", 0))
        busy_socket.listen()
        busy_port = busy_socket.getsockname()[1]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                events_dir = Path(tmp) / "events"
                events_dir.mkdir()
                state_file = Path(tmp) / "serve.json"
                proc = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "amplifier_fast_decisions",
                        "serve",
                        "--events",
                        str(events_dir),
                        "--port",
                        str(busy_port),
                        "--state-file",
                        str(state_file),
                        "--no-fallback",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("afast:", proc.stderr)
                self.assertFalse(state_file.exists())
        finally:
            busy_socket.close()


# ---------------------------------------------------------------------------
# observer.py: session:start wiring
# ---------------------------------------------------------------------------


def _continue():
    return SimpleNamespace(action="continue")


@unittest.skipUnless(HAS_CORE, "amplifier_core not installed (real-kernel lane only)")
class SessionStartWiringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tty_patches = [
            mock.patch("sys.stdin.isatty", return_value=True),
            mock.patch("sys.stdout.isatty", return_value=True),
        ]
        for patch in self._tty_patches:
            patch.start()
        self.addCleanup(self._stop_tty_patches)

    def _stop_tty_patches(self):
        for patch in self._tty_patches:
            patch.stop()

    async def _mount_and_start(self, obs_config=None, *, data=None):
        from amplifier_fast_decisions import observer

        coordinator = DemoCoordinator()
        obs_events: list[dict] = []

        async def record(event, payload):
            obs_events.append(dict(payload.get("data", {})))
            return _continue()

        coordinator.hooks.register("fast_decisions:observatory", record)

        with tempfile.TemporaryDirectory() as events_dir:
            cleanup = await observer.mount(
                coordinator,
                {
                    "mode": "shadow",
                    "backend": "unavailable",
                    "events_dir": events_dir,
                    "observatory": obs_config or {},
                },
            )
            try:
                await coordinator.hooks.emit("session:start", data or {})
                await asyncio.sleep(0.05)
            finally:
                await cleanup()
        return obs_events

    async def test_skips_child_session(self):
        with (
            mock.patch("amplifier_fast_decisions.observer.ensure_viewer") as ensure,
            mock.patch("amplifier_fast_decisions.observer.open_page") as opener,
        ):
            events = await self._mount_and_start(data={"parent_id": "parent-1"})
        ensure.assert_not_called()
        opener.assert_not_called()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["action"], "skipped")
        self.assertEqual(events[0]["reason"], "child_session")

    async def test_skips_when_afast_observatory_off(self):
        with (
            mock.patch.dict("os.environ", {"AFAST_OBSERVATORY": "off"}),
            mock.patch("amplifier_fast_decisions.observer.ensure_viewer") as ensure,
        ):
            events = await self._mount_and_start()
        ensure.assert_not_called()
        self.assertEqual(events[0]["action"], "skipped")
        self.assertEqual(events[0]["reason"], "env_disabled")

    async def test_skips_when_amplifier_no_browser(self):
        with (
            mock.patch.dict("os.environ", {"AMPLIFIER_NO_BROWSER": "1"}),
            mock.patch("amplifier_fast_decisions.observer.ensure_viewer") as ensure,
        ):
            events = await self._mount_and_start()
        ensure.assert_not_called()
        self.assertEqual(events[0]["action"], "skipped")
        self.assertEqual(events[0]["reason"], "env_disabled")

    async def test_skips_when_non_tty(self):
        self._stop_tty_patches()  # revert the setUp TTY patches for this test
        with (
            mock.patch("sys.stdin.isatty", return_value=False),
            mock.patch("sys.stdout.isatty", return_value=False),
            mock.patch("amplifier_fast_decisions.observer.ensure_viewer") as ensure,
        ):
            events = await self._mount_and_start()
        ensure.assert_not_called()
        self.assertEqual(events[0]["action"], "skipped")
        self.assertEqual(events[0]["reason"], "non_tty")
        # Re-apply so asyncSetUp's cleanup doesn't double-stop.
        self._tty_patches = [
            mock.patch("sys.stdin.isatty", return_value=True),
            mock.patch("sys.stdout.isatty", return_value=True),
        ]
        for patch in self._tty_patches:
            patch.start()

    async def test_proceeds_for_top_level_tty_session(self):
        with (
            mock.patch(
                "amplifier_fast_decisions.observer.ensure_viewer",
                return_value="http://127.0.0.1:8765/#token=abc",
            ) as ensure,
            mock.patch("amplifier_fast_decisions.observer.open_page") as opener,
            mock.patch(
                "amplifier_fast_decisions.observer.read_state", return_value=None
            ),
        ):
            events = await self._mount_and_start()
        ensure.assert_called_once()
        self.assertEqual(events[0]["action"], "started")
        self.assertEqual(events[0]["reason"], "ok")
        opener.assert_called_once_with("http://127.0.0.1:8765/#token=abc")

    async def test_open_browser_never_suppresses_open(self):
        with (
            mock.patch(
                "amplifier_fast_decisions.observer.ensure_viewer",
                return_value="http://127.0.0.1:8765/#token=abc",
            ),
            mock.patch("amplifier_fast_decisions.observer.open_page") as opener,
            mock.patch(
                "amplifier_fast_decisions.observer.read_state", return_value=None
            ),
        ):
            events = await self._mount_and_start(obs_config={"open_browser": "never"})
        # The viewer was started (so an open WOULD have happened under
        # "always"); only the open itself is suppressed.
        self.assertEqual(events[0]["action"], "started")
        opener.assert_not_called()

    async def test_open_browser_first_only_opens_on_start_not_reuse(self):
        alive_state = {"pid": 1, "port": 8765}
        with (
            mock.patch(
                "amplifier_fast_decisions.observer.ensure_viewer",
                return_value="http://127.0.0.1:8765/#token=abc",
            ),
            mock.patch("amplifier_fast_decisions.observer.open_page") as opener,
            mock.patch(
                "amplifier_fast_decisions.observer.read_state",
                return_value=alive_state,
            ),
            mock.patch(
                "amplifier_fast_decisions.observer.viewer_is_alive", return_value=True
            ),
        ):
            events = await self._mount_and_start(obs_config={"open_browser": "first"})
        self.assertEqual(events[0]["action"], "reused")
        opener.assert_not_called()

    async def test_fires_once_per_session(self):
        from amplifier_fast_decisions import observer

        coordinator = DemoCoordinator()
        obs_events: list[dict] = []

        async def record(event, payload):
            obs_events.append(dict(payload.get("data", {})))
            return _continue()

        coordinator.hooks.register("fast_decisions:observatory", record)

        with (
            tempfile.TemporaryDirectory() as events_dir,
            mock.patch(
                "amplifier_fast_decisions.observer.ensure_viewer",
                return_value="http://127.0.0.1:8765/#token=abc",
            ),
            mock.patch("amplifier_fast_decisions.observer.open_page"),
            mock.patch(
                "amplifier_fast_decisions.observer.read_state", return_value=None
            ),
        ):
            cleanup = await observer.mount(
                coordinator,
                {"mode": "shadow", "backend": "unavailable", "events_dir": events_dir},
            )
            try:
                await coordinator.hooks.emit("session:start", {})
                await coordinator.hooks.emit("session:start", {})
                await asyncio.sleep(0.05)
            finally:
                await cleanup()
        self.assertEqual(len(obs_events), 1)

    async def test_handler_never_raises_when_ensure_viewer_blows_up(self):
        with mock.patch(
            "amplifier_fast_decisions.observer.ensure_viewer",
            side_effect=RuntimeError("boom"),
        ):
            events = await self._mount_and_start()
        self.assertEqual(events[0]["action"], "failed")
        self.assertEqual(events[0]["reason"], "unexpected_error")

    async def test_disabled_by_config(self):
        with mock.patch("amplifier_fast_decisions.observer.ensure_viewer") as ensure:
            events = await self._mount_and_start(obs_config={"enabled": False})
        ensure.assert_not_called()
        self.assertEqual(events, [])


# ---------------------------------------------------------------------------
# Real-kernel lane: the trigger fires through an actual StreamingOrchestrator
# turn, using amplifier_core's own MockCoordinator/hook registry -- not the
# offline DemoCoordinator above.
# ---------------------------------------------------------------------------


class _OneShotProvider:
    """A single ``provider:request``/``complete()`` call, plain text, no
    tool call -- the observatory trigger does not depend on any tool
    dispatch, so the simplest possible turn is enough to exercise it."""

    name = "fake"

    def get_info(self):
        return SimpleNamespace(
            id=self.name, display_name=self.name, context_window=32000
        )

    async def list_models(self):
        return []

    def parse_tool_calls(self, response):
        return response.tool_calls or []

    async def complete(self, request, **kwargs):
        from amplifier_core.message_models import ChatResponse, Usage

        return ChatResponse(
            content=[{"type": "text", "text": "done"}],
            tool_calls=[],
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
        )


@unittest.skipUnless(
    HAS_LOOP, "amplifier_module_loop_streaming not installed (real-kernel lane only)"
)
class RealKernelObservatoryTests(unittest.IsolatedAsyncioTestCase):
    """Mirrors tests/test_shadow_real_kernel.py's harness pattern (real
    MockCoordinator + real StreamingOrchestrator), scoped to just the
    auto-observatory trigger.

    The real Rust kernel emits ``session:start`` synchronously, before
    calling into the orchestrator (amplifier_core's
    ``_session_exec.run_orchestrator`` docstring), a step a direct
    ``StreamingOrchestrator.execute()`` call in a test harness does not
    reproduce on its own. Emit it explicitly here, exactly as
    ``amplifier_core.testing.ScriptedOrchestrator`` does for its own
    lifecycle-event tests -- this is the documented test convention for
    simulating that kernel behavior, not a deviation from it.
    """

    async def test_fires_once_through_real_streaming_orchestrator_turn(self):
        from amplifier_core.testing import MockContextManager, MockCoordinator
        from amplifier_module_loop_streaming import StreamingOrchestrator

        from amplifier_fast_decisions import observer

        coordinator = MockCoordinator()
        context = MockContextManager(messages=[])
        await coordinator.mount("context", context)

        obs_events: list[dict] = []

        async def record(event, data):
            from amplifier_core.models import HookResult

            obs_events.append(dict(data.get("data", data)))
            return HookResult(action="continue")

        coordinator.hooks.register("fast_decisions:observatory", record)

        with tempfile.TemporaryDirectory() as events_dir:
            cleanup = await observer.mount(
                coordinator,
                {
                    "mode": "shadow",
                    "backend": "unavailable",
                    "events_dir": events_dir,
                    "observatory": {"enabled": True, "open_browser": "never"},
                },
            )
            try:
                with (
                    mock.patch(
                        "amplifier_fast_decisions.observer.ensure_viewer",
                        return_value="http://127.0.0.1:8765/#token=abc",
                    ) as ensure,
                    mock.patch("amplifier_fast_decisions.observer.open_page") as opener,
                    mock.patch(
                        "amplifier_fast_decisions.observer.read_state",
                        return_value=None,
                    ),
                    mock.patch("sys.stdin.isatty", return_value=True),
                    mock.patch("sys.stdout.isatty", return_value=True),
                ):
                    # The real kernel emits session:start before handing off
                    # to the orchestrator -- see this class's docstring.
                    await coordinator.hooks.emit("session:start", {})
                    provider = _OneShotProvider()
                    orch = StreamingOrchestrator({})
                    result = await orch.execute(
                        "hello",
                        context,
                        {"fake": provider},
                        {},
                        coordinator.hooks,
                        coordinator=coordinator,
                    )
                    await asyncio.sleep(0.1)
            finally:
                await cleanup()

        self.assertEqual(result, "done")
        self.assertEqual(len(obs_events), 1)
        self.assertEqual(obs_events[0]["action"], "started")
        ensure.assert_called_once()
        opener.assert_not_called()  # open_browser: never


if __name__ == "__main__":
    unittest.main()
