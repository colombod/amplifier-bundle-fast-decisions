"""Auto-observatory bootstrap: discover-or-spawn a detached ``afast serve``.

Harness-agnostic (stdlib only; no ``amplifier_core``/``amplifier_foundation``
imports -- see ``tests/test_layering.py``'s AGNOSTIC set). This module knows
nothing about hooks, coordinators or sessions; it only knows how to read/write
a small JSON state file, health-check a pid+port, and spawn a detached
subprocess. The hook wiring that decides *whether* and *when* to call this
lives in ``observer.py``.

Never raises. Every public function either returns a value describing the
outcome or degrades to ``None``/no-op on any failure -- a bug here must never
affect a real session.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = [
    "ensure_viewer",
    "open_page",
    "pid_looks_like_viewer",
    "read_state",
    "remove_state",
    "viewer_is_alive",
    "write_state_atomic",
]

_POLL_INTERVAL_S = 0.1
_DEFAULT_LOG_DIR = Path.home() / ".amplifier" / "fast-decisions"


def read_state(state_file: str | Path) -> dict[str, Any] | None:
    """Best-effort read of the viewer state file. ``None`` on any failure."""
    try:
        return json.loads(Path(state_file).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_state_atomic(state_file: str | Path, data: dict[str, Any]) -> None:
    """Write ``data`` as JSON via tmp-file + rename, mode 0600.

    Raises on failure -- callers that must never raise (the hook path) call
    this only through :func:`ensure_viewer`'s own subprocess, never directly
    from the hook's async context.
    """
    path = Path(state_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def remove_state(state_file: str | Path) -> None:
    """Best-effort removal of the state file. Never raises."""
    try:
        Path(state_file).expanduser().unlink(missing_ok=True)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    except OSError:
        return False
    return True


def pid_looks_like_viewer(pid: int) -> bool:
    """Best-effort identity check before signalling a recorded pid.

    A viewer's state file can outlive the process it names -- if the viewer
    died and the OS recycled the pid for an unrelated process before someone
    runs ``afast serve --stop``, blindly signalling that pid would kill the
    wrong thing. This guards the common case by checking the pid's own
    command line (via ``ps``, no new dependency) for this package's module
    name, which every ``afast serve`` invocation carries
    (``python -m amplifier_fast_decisions serve`` or the installed console
    script, whose argv still resolves through this package).

    Returns ``True`` (i.e. "go ahead and signal it") whenever the check
    itself can't be performed -- missing ``ps``, permission denied, pid
    already gone, timeout -- so this stays a guard against an *identifiable*
    mismatch, not a hard gate that could leave a real viewer un-stoppable
    when ``ps`` is unavailable.
    """
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if result.returncode != 0:
        return True
    cmdline = result.stdout.strip()
    if not cmdline:
        return True
    return "amplifier_fast_decisions" in cmdline


def viewer_is_alive(state: dict[str, Any], *, timeout: float = 0.5) -> bool:
    """pid alive AND a GET to the viewer's index returns 200 within ``timeout``.

    The index (``/``) is static and unauthenticated (only ``/api/*`` needs
    the Bearer token -- see ``server.py``'s ``do_GET``), so this needs no
    token and works from any process.
    """
    pid = state.get("pid")
    port = state.get("port")
    if not isinstance(pid, int) or not isinstance(port, int):
        return False
    if not _pid_alive(pid):
        return False
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{port}/", method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return False


_BUILD_FILES = ("server.py", "savings.py", "efficiency.py", "operations.py", "static/app.js", "static/index.html", "static/style.css")


def build_id() -> str:
    """Short content hash of the viewer's own code. Recorded in the state
    file by ``afast serve``; a running viewer from a different build is
    replaced instead of reused, so upgrades reach the dashboard."""
    import hashlib

    digest = hashlib.sha256()
    root = Path(__file__).resolve().parent
    for name in _BUILD_FILES:
        try:
            digest.update(name.encode() + b"\0" + (root / name).read_bytes())
        except OSError:
            digest.update(name.encode() + b"\0missing")
    return digest.hexdigest()[:12]


def _stop_stale(state: dict[str, Any], state_path: Path, *, sleep: Callable[[float], None]) -> None:
    """Terminate an outdated viewer (best effort) and clear its state."""
    import signal

    pid = state.get("pid")
    if isinstance(pid, int) and pid > 1 and pid != os.getpid() and _pid_alive(pid):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
        for _ in range(10):
            if not _pid_alive(pid):
                break
            sleep(0.1)
    remove_state(state_path)


def _default_spawner(argv: list[str]) -> None:
    _DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = _DEFAULT_LOG_DIR / "serve.log"
    with open(log_path, "a", encoding="utf-8") as log_file:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )


def ensure_viewer(
    events_dir: str | Path,
    port: int,
    state_file: str | Path,
    *,
    spawner: Callable[[list[str]], None] | None = None,
    opener: Callable[[str], Any] | None = None,
    alive: Callable[[dict[str, Any]], bool] | None = None,
    sleep: Callable[[float], None] | None = None,
    deadline_s: float = 2.0,
) -> str | None:
    """Reuse a live viewer, or spawn a detached one and wait for it.

    Returns the token-bearing URL, or ``None`` if no viewer could be reached
    within ``deadline_s``. Never raises -- any failure (bad state file,
    spawn error, health check exception) collapses to ``None``.
    """
    spawner = spawner or _default_spawner
    is_alive = alive or viewer_is_alive
    sleep_fn = sleep or time.sleep
    try:
        state_path = Path(state_file).expanduser()
        existing = read_state(state_path)
        if existing is not None and is_alive(existing):
            if existing.get("build") == build_id():
                url = existing.get("url")
                return url if isinstance(url, str) else None
            # A viewer from an older (or newer) build: replace it so the
            # dashboard matches the installed code.
            _stop_stale(existing, state_path, sleep=sleep_fn)

        argv = [
            sys.executable,
            "-m",
            "amplifier_fast_decisions",
            "serve",
            "--events",
            str(events_dir),
            "--port",
            str(port),
            "--state-file",
            str(state_path),
        ]
        spawner(argv)

        attempts = max(1, int(deadline_s / _POLL_INTERVAL_S))
        for _ in range(attempts):
            sleep_fn(_POLL_INTERVAL_S)
            candidate = read_state(state_path)
            if candidate is not None and is_alive(candidate):
                url = candidate.get("url")
                return url if isinstance(url, str) else None
        return None
    except Exception:
        return None


def open_page(url: str, opener: Callable[[str], Any] | None = None) -> None:
    """Open ``url`` in a browser. Never raises."""
    try:
        (opener or webbrowser.open)(url)
    except Exception:
        pass
