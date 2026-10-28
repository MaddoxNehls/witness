"""Thin, read-only wrappers over the git CLI."""

from __future__ import annotations

import re
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path


class GitError(RuntimeError):
    pass


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def resolve(repo: Path, ref: str) -> str:
    """Resolve a ref to a full commit hash."""
    if ref.startswith("-"):  # would be parsed as an option
        raise GitError(f"invalid ref {ref!r}")
    try:
        return _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()
    except GitError:
        raise GitError(f"{ref!r} is not a commit in {repo}") from None


def diff(repo: Path, base: str, head: str, *, context_lines: int = 3) -> str:
    return _git(repo, "diff", "--no-color", "-M", f"-U{context_lines}", base, head, "--")


@dataclass(frozen=True)
class LineRange:
    start: int
    end: int

    def overlaps(self, start: int, end: int) -> bool:
        return self.start <= end and start <= self.end


@dataclass(frozen=True)
class FileChange:
    path: str
    status: str  # A, M, D, R (git --name-status letters)
    old_path: str | None = None
    hunks: tuple[LineRange, ...] = field(default=())


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_files(repo: Path, base: str, head: str) -> list[FileChange]:
    """Changed files with the HEAD-side line ranges each hunk touches."""
    entries: list[tuple[str, str, str | None]] = []
    for line in _git(repo, "diff", "--name-status", "-M", base, head, "--").splitlines():
        parts = line.split("\t")
        status = parts[0][0]
        if status == "R":
            entries.append((status, parts[2], parts[1]))
        else:
            entries.append((status, parts[1], None))

    hunks = parse_hunks(_git(repo, "diff", "--no-color", "-M", "-U0", base, head, "--"))
    return [
        FileChange(path, status, old, tuple(hunks.get(path, ()))) for status, path, old in entries
    ]


def parse_hunks(unified_zero_diff: str) -> dict[str, list[LineRange]]:
    """Map each file to the HEAD line ranges its hunks touch, from a ``-U0`` diff.

    A pure deletion has no HEAD lines; it is recorded as the single line it sat
    next to so the enclosing function is still found.
    """
    result: dict[str, list[LineRange]] = {}
    current: str | None = None
    for line in unified_zero_diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            current = None if target == "/dev/null" else target.removeprefix("b/")
        elif current is not None and (match := _HUNK.match(line)):
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            end = start + count - 1 if count else start
            result.setdefault(current, []).append(LineRange(max(start, 1), max(end, 1)))
    return result


def visible_lines(repo: Path, base: str, head: str) -> dict[str, list[LineRange]]:
    """HEAD line ranges a PR diff displays (hunks plus GitHub's 3 lines of context).

    Review comments can only be attached to these lines.
    """
    return parse_hunks(diff(repo, base, head, context_lines=3))


def show_file(repo: Path, rev: str, path: str) -> str | None:
    try:
        return _git(repo, "show", f"{rev}:{path}")
    except GitError:
        return None


def list_files(repo: Path, rev: str) -> list[str]:
    return _git(repo, "ls-tree", "-r", "--name-only", rev).splitlines()


def export_tree(repo: Path, rev: str, dest: Path) -> Path:
    """Write the tree at ``rev`` into ``dest`` (no .git, no untracked files)."""
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        ["git", "-C", str(repo), "archive", "--format=tar", rev],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    with tarfile.open(fileobj=proc.stdout, mode="r|") as archive:
        # The "data" filter refuses absolute paths, ".." escapes, device files and
        # links pointing outside dest: repo content is untrusted.
        archive.extractall(dest, filter="data")
    _, stderr = proc.communicate()
    if proc.returncode != 0:
        raise GitError(f"git archive {rev} failed: {stderr.decode(errors='replace').strip()}")
    return dest
