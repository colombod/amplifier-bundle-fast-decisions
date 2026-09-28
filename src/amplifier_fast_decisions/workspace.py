"""A deliberately small read-only workspace tool for the initial active pilot.

No shell, network, writes, recursion, dotfiles, symlinks, or non-text reads.
The filesystem must not be adversarially mutated during a read; ordinary
workspace containment is provided, not an OS sandbox for hostile processes.

Two read-only additions serve the per-step prepared actions (step_actions.py):
``read`` with an optional ``line`` returns a numbered window around that line
(what a search hit points at), and ``git`` returns a fixed status summary
(branch, uncommitted changes, recent commits, commit count) from a fixed git
argv -- no shell, no user-supplied arguments, optional locks and fsmonitor
disabled, so it never writes the index or runs repository-configured programs.
"""
from __future__ import annotations
import asyncio
import codecs
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any

from .contracts import Candidate, digest
from .privacy import scrub

__amplifier_module_type__ = "tool"
_ALLOWED = {".md", ".txt", ".py", ".rs", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml", ".toml", ".html", ".css"}
_BLOCKED = {"credentials", "secrets", "secret", "id_rsa", "id_ed25519", "token", "tokens", "passwords"}
# Numbered window returned by ``read`` with ``line``: this many lines before
# the target line and this many from it.
WINDOW_BEFORE, WINDOW_AFTER = 15, 85
_GIT_TIMEOUT_S = 5.0
_GIT_BASE = ("git", "--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false",
             "-c", "core.untrackedCache=false", "-c", "color.ui=never")
_GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


class WorkspaceTool:
    name = "fast_workspace"
    # The published description and schema stay byte-identical to the
    # read/list-only tool: they are part of every request's cached prompt
    # prefix, so changing them makes every session miss the prompt cache
    # warmed by other sessions (measured: ~+$0.15 on a fresh session's first
    # call). The ``line`` and ``git`` shapes are only ever issued by prepared
    # actions (the synthetic tool call is not checked against this schema);
    # _read and validate_candidate accept them.
    description = "Read or list non-hidden text files within the configured workspace. No writes, shell, or network."
    input_schema = {
        "type": "object", "additionalProperties": False,
        "properties": {"operation": {"type": "string", "enum": ["read", "list"]},
                       "path": {"type": "string", "maxLength": 512}},
        "required": ["operation", "path"],
    }

    def __init__(self, root: str | Path = ".", max_bytes: int = 32768):
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Workspace root is not a directory")
        self.max_bytes = max(1024, min(int(max_bytes), 262144))

    def _path(self, relative: str, *, file: bool = False) -> Path:
        if not isinstance(relative, str) or len(relative) > 512 or "\x00" in relative:
            raise ValueError("Invalid path")
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError("Path must stay inside the workspace")
        current = self.root
        for part in rel.parts:
            if part in {"", "."}:
                continue
            if part.startswith(".") or Path(part).stem.lower() in _BLOCKED:
                raise ValueError("Hidden or sensitive file is excluded")
            current = current / part
            if current.is_symlink():
                raise ValueError("Symlinks are excluded")
        resolved = current.resolve(strict=True)
        resolved.relative_to(self.root)
        if file and (not resolved.is_file() or resolved.suffix.lower() not in _ALLOWED):
            raise ValueError("Only allowed text files may be read")
        return resolved

    def _revision(self, path: Path) -> str:
        st = path.stat()
        return digest([st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns])

    def relative(self, path: str) -> str | None:
        """``path`` (absolute inside the root, or already relative) as a
        root-relative path, or None when it lies outside. Never raises."""
        try:
            candidate = Path(path)
            if not candidate.is_absolute():
                return path
            real = Path(os.path.realpath(candidate))
            return str(real.relative_to(self.root))
        except (OSError, ValueError, TypeError):
            return None

    def read_identity(self, path: str, operation: str) -> tuple[str, str] | None:
        """``(normalized absolute path, current revision)`` for a read/list
        target inside the workspace, or ``None`` if it cannot be resolved.

        Accepts a path already relative to the workspace root (candidates
        always pass one), or an absolute path that resolves inside the root
        (the native ``read_file`` tool may pass one). Used by HC02a's
        completed-read ledger (contracts.candidate_read_identity and
        orchestrator.ObservedTool) so ledger keys and candidate identity
        share this one revision function.
        """
        try:
            candidate_path = Path(path)
            if candidate_path.is_absolute():
                try:
                    relative = str(candidate_path.relative_to(self.root))
                except ValueError:
                    return None
            else:
                relative = path
            resolved = self._path(relative, file=operation == "read")
            if operation == "list" and not resolved.is_dir():
                return None
            return str(resolved), self._revision(resolved)
        except (OSError, ValueError, TypeError):
            return None

    def candidate_for_path(self, relative: str, index: int, *, line: int | None = None,
                           origin: str = "explicit_user_path", source: str = "named by the user") -> Candidate | None:
        """A read candidate for ``relative``; with ``line``, a numbered window
        around that line (a different id, so both shapes can coexist)."""
        try:
            path = self._path(relative, file=True)
        except (OSError, ValueError):
            return None
        if line is None:
            return Candidate("read_" + digest(relative)[:12], f"Read requested file {index + 1}", self.name,
                {"operation": "read", "path": relative},
                rationale=f"Read the workspace text file explicitly {source}: {relative}",
                origin=origin, revision=self._revision(path))
        if not _positive_int(line):
            return None
        return Candidate("win_" + digest([relative, line])[:12], f"Read file window {index + 1}", self.name,
            {"operation": "read", "path": relative, "line": line},
            rationale=(f"Read lines {max(1, line - WINDOW_BEFORE)}-{line + WINDOW_AFTER - 1} of the workspace "
                       f"text file {source}: {relative} (around line {line})"),
            origin=origin, revision=self._revision(path))

    def estimate_chars(self, arguments: dict[str, Any]) -> int | None:
        """Approximate size of a read's result (what it adds to every later
        prompt), without reading more than the file's metadata for a whole
        read. None when unknown."""
        try:
            if arguments.get("operation") == "git":
                return 2_000
            path = self._path(arguments["path"], file=arguments.get("operation") == "read")
            if arguments.get("operation") != "read":
                return 2_000
            size = path.stat().st_size
            line = arguments.get("line")
            if not _positive_int(line):
                return min(size, self.max_bytes)
            lines = path.read_bytes()[:4_000_000].splitlines()
            start, end = max(1, line - WINDOW_BEFORE), min(len(lines), line + WINDOW_AFTER - 1)
            return sum(len(x) + 8 for x in lines[start - 1:end])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def is_git_repo(self) -> bool:
        return (self.root / ".git").exists()

    def git_candidate(self) -> Candidate | None:
        """The fixed git status summary, when the workspace root is a git checkout."""
        if not self.is_git_repo():
            return None
        return Candidate("git_status_summary", "Show git status summary", self.name,
            {"operation": "git", "path": "."},
            rationale=("Show the repository's current branch, uncommitted and untracked files, the 10 most "
                       "recent commits (hash and subject) and the commit count of the current branch"),
            origin="step_rule")

    def find_line(self, relative: str, identifiers: list[str]) -> int | None:
        """First line defining (``def``/``class``/assignment/annotation) one of
        ``identifiers`` in the file, else the first line mentioning one, else
        None. Reads at most 2 MB. Never raises."""
        if not identifiers:
            return None
        try:
            path = self._path(relative, file=True)
            with open(path, "rb") as handle:
                text = handle.read(2_000_000).decode("utf-8", errors="replace")
        except (OSError, ValueError):
            return None
        lines = text.splitlines()
        for ident in identifiers:
            pattern = re.compile(r"^\s*(?:async\s+def|def|class)\s+" + re.escape(ident) + r"\b"
                                 r"|^\s*" + re.escape(ident) + r"\s*[:=(]")
            for number, content in enumerate(lines, 1):
                if pattern.search(content):
                    return number
        for ident in identifiers:
            word = re.compile(r"\b" + re.escape(ident) + r"\b")
            for number, content in enumerate(lines, 1):
                if word.search(content):
                    return number
        return None

    def validate_candidate(self, candidate: Candidate) -> bool:
        args = candidate.arguments
        if args.get("operation") == "git":
            return set(args) == {"operation", "path"} and args.get("path") == "." and self.is_git_repo()
        keys = set(args)
        if keys == {"operation", "path", "line"}:
            if args.get("operation") != "read" or not _positive_int(args.get("line")):
                return False
        elif keys != {"operation", "path"} or args.get("operation") not in {"read", "list"}:
            return False
        try:
            path = self._path(args["path"], file=args["operation"] == "read")
            if args["operation"] == "list" and not path.is_dir():
                return False
            return not candidate.revision or candidate.revision == self._revision(path)
        except (OSError, ValueError, TypeError):
            return False

    def _git(self, *args: str) -> str:
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR")}
        env.update(_GIT_ENV)
        proc = subprocess.run([*_GIT_BASE, *args], cwd=self.root, env=env, capture_output=True,
                              text=True, errors="replace", timeout=_GIT_TIMEOUT_S, stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            tail = (proc.stderr.strip().splitlines() or [""])[-1][:200]
            raise ValueError(f"git {args[0]} failed: {tail}")
        return proc.stdout

    def _git_summary(self) -> dict[str, Any]:
        if not self.is_git_repo():
            raise ValueError("Workspace root is not a git checkout")
        status = self._git("status", "--porcelain=v1", "-uall", "--branch").splitlines()
        header = status[0] if status and status[0].startswith("## ") else ""
        changes = [line for line in status if not line.startswith("## ")]
        branch = self._git("branch", "--show-current").strip() or "(detached HEAD)"
        try:
            commits = self._git("log", "-10", "--format=%h %s").splitlines()
            count = int(self._git("rev-list", "--count", "HEAD").strip() or 0)
        except ValueError:
            commits, count = [], 0   # an unborn branch has no commits yet
        return {"branch": branch, "tracking": header[3:][:200], "uncommitted": changes[:200],
                "uncommitted_count": len(changes), "recent_commits": [scrub(c, 200) for c in commits],
                "commit_count": count}

    def _window(self, text: str, line: int) -> dict[str, Any]:
        lines = text.splitlines()
        start = max(1, min(line, len(lines)) - WINDOW_BEFORE)
        end = min(len(lines), line + WINDOW_AFTER - 1)
        numbered = "\n".join(f"{n:>6}\t{lines[n - 1]}" for n in range(start, end + 1))
        return {"text": scrub(numbered, self.max_bytes), "start_line": start, "end_line": end,
                "total_lines": len(lines)}

    def _read(self, args: dict[str, Any]) -> dict[str, Any]:
        operation = args.get("operation")
        if operation == "git":
            if set(args) != {"operation", "path"} or args.get("path") != ".":
                raise ValueError("git takes path '.' only")
            return self._git_summary()
        line = args.get("line")
        if set(args) == {"operation", "path", "line"}:
            if operation != "read" or not _positive_int(line):
                raise ValueError("line must be a positive integer on read")
        elif set(args) != {"operation", "path"}:
            raise ValueError("Unexpected input fields")
        if operation not in {"read", "list"}:
            raise ValueError("Unsupported operation")
        path = self._path(args["path"], file=operation == "read")
        if operation == "list":
            if not path.is_dir():
                raise ValueError("Path is not a directory")
            entries = []
            for entry in sorted(path.iterdir()):
                if entry.name.startswith(".") or entry.stem.lower() in _BLOCKED or entry.is_symlink():
                    continue
                entries.append({"name": entry.name, "type": "directory" if entry.is_dir() else "file"})
                if len(entries) >= 100:
                    break
            return {"entries": entries, "limit": 100}
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        limit = self.max_bytes if line is None else 4_000_000
        with os.fdopen(fd, "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise ValueError("Not a regular file")
            data = file.read(limit + 1)
        # Allow a truncated final UTF-8 codepoint, but still reject invalid bytes.
        decoder = codecs.getincrementaldecoder("utf-8")()
        text = decoder.decode(data[:limit], final=len(data) <= limit)
        if "\x00" in text:
            raise ValueError("Binary content excluded")
        if line is not None:
            return self._window(text, line)
        return {"text": scrub(text, self.max_bytes), "truncated": len(data) > self.max_bytes}

    async def execute(self, input: dict[str, Any], **kwargs):
        from amplifier_core.models import ToolResult
        try:
            output = await asyncio.to_thread(self._read, input)
            return ToolResult(success=True, output=output)
        except (ValueError, OSError, UnicodeError, subprocess.SubprocessError) as exc:
            return ToolResult(success=False, error={"message": str(exc), "type": type(exc).__name__})


async def mount(coordinator, config: dict):
    tool = WorkspaceTool(config.get("root", "."), config.get("max_bytes", 32768))
    await coordinator.mount("tools", tool, name=tool.name)
    return None
