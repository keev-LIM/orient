"""The process boundary.

Everything in orient that touches the clock, the filesystem or another process
goes through this module, and nothing here interprets what it finds. The clock
and the subprocess runner are constructor arguments so tests can inject fakes;
the filesystem is used directly because tests always run under a temporary root.
"""

from __future__ import annotations

import datetime as _dt
import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "Completed",
    "Env",
    "GIT_ALLOWED",
    "OrientError",
    "PRUNE_DIRS",
    "Runner",
]


@dataclass(frozen=True)
class Completed:
    """Result of a subprocess. Never raises; failure is data."""

    code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out


class OrientError(Exception):
    """Any failure orient chooses to surface. Carries its own exit code."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


Runner = Callable[[Sequence[str], "str | None", float, Path], Completed]

#: Read-only git subcommands. Env.git refuses anything not in this set.
GIT_ALLOWED: frozenset[str] = frozenset({"log", "ls-files", "rev-parse", "shortlog", "blame"})

#: Directory names never walked into.
PRUNE_DIRS: frozenset[str] = frozenset(
    {
        ".git", ".hg", ".svn", ".orient", ".venv", "venv", "node_modules",
        "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build",
        ".tox", ".idea", "vendor", "third_party",
    }
)

#: Dot-directories that are walked anyway, because entry points hide in them.
KEEP_DOT_DIRS: frozenset[str] = frozenset({".github", ".gitlab", ".circleci", ".config"})


def _default_runner(
    argv: Sequence[str], stdin: str | None, timeout: float, cwd: Path
) -> Completed:
    try:
        done = subprocess.run(  # noqa: S603 - argv is built by orient, never by a user string
            list(argv),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Completed(124, "", f"{argv[0]}: timed out after {timeout:g}s", True)
    except FileNotFoundError:
        return Completed(127, "", f"{argv[0]}: not found")
    except OSError as exc:
        return Completed(127, "", f"{argv[0]}: {exc}")
    return Completed(done.returncode, done.stdout or "", done.stderr or "")


class Env:
    """Performs side effects on request and reports what happened."""

    def __init__(
        self,
        root: Path,
        *,
        clock: Callable[[], _dt.datetime] | None = None,
        runner: Runner | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self._clock = clock or (lambda: _dt.datetime.now(_dt.UTC))
        self._runner = runner or _default_runner

    # -- time ---------------------------------------------------------------

    def now(self) -> _dt.datetime:
        """UTC, timezone-aware. The only source of time in the program."""
        return self._clock()

    # -- filesystem ---------------------------------------------------------

    def walk(self, *, max_files: int) -> tuple[list[str], bool]:
        """Repo-relative POSIX paths of regular files, sorted, PRUNE_DIRS pruned."""
        found: list[str] = []
        truncated = False
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(
                d
                for d in dirnames
                if d not in PRUNE_DIRS and (not d.startswith(".") or d in KEEP_DOT_DIRS)
            )
            here = Path(dirpath)
            for name in sorted(filenames):
                full = here / name
                if full.is_symlink() or not full.is_file():
                    continue
                found.append(full.relative_to(self.root).as_posix())
                if len(found) >= max_files:
                    truncated = True
                    break
            if truncated:
                break
        found.sort()
        return found, truncated

    def read_text(self, rel: str, *, max_bytes: int = 2_000_000) -> str:
        """Decoded UTF-8 with errors='replace'. '' if unreadable, binary, or oversized."""
        path = self.root / rel
        try:
            if path.stat().st_size > max_bytes:
                return ""
            data = path.read_bytes()
        except OSError:
            return ""
        if b"\x00" in data[:8192]:
            return ""
        return data.decode("utf-8", errors="replace")

    def size(self, rel: str) -> int:
        """Byte size, 0 if the file has vanished."""
        try:
            return (self.root / rel).stat().st_size
        except OSError:
            return 0

    def write_text(self, path: Path, text: str) -> int:
        """mkdir -p the parent, write UTF-8, return bytes written."""
        path = Path(path)
        data = text.encode("utf-8")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as exc:
            raise OrientError(f"cannot write {path}: {exc.strerror or exc}", 1) from exc
        return len(data)

    # -- processes ----------------------------------------------------------

    def run(
        self, argv: Sequence[str], *, stdin: str | None = None, timeout: float = 60.0
    ) -> Completed:
        """Run argv with cwd=self.root. Missing binary -> 127, timeout -> 124."""
        return self._runner(list(argv), stdin, timeout, self.root)

    def git(self, args: Sequence[str], *, timeout: float = 90.0) -> Completed:
        args = list(args)
        if not args or args[0] not in GIT_ALLOWED:
            raise OrientError(f"refusing git subcommand {args[0] if args else '<none>'!r}", 1)
        return self.run(["git", "-C", str(self.root), *args], timeout=timeout)

    def claude(self, prompt: str, *, timeout: float = 90.0) -> Completed:
        return self.run(
            ["claude", "-p", "--output-format", "text"], stdin=prompt, timeout=timeout
        )

    def which(self, name: str) -> str | None:
        """shutil.which, so doctor can report a missing dependency without running it."""
        return shutil.which(name)
