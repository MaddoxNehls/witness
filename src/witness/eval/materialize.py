"""Turn a manifest case into a local git repo with ``base`` and ``head`` commits.

Upstream repos are mirrored once into a cache. Each case gets a ``--shared`` clone (no
object copying) where the synthetic base/head commits are written with git plumbing
through a private index, so nothing is ever checked out. Author, committer and dates
are fixed, so the same case always yields the same commit hashes.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from witness import git
from witness.eval.manifest import Case

DEFAULT_CACHE = Path(".cache") / "benchmark"
_IDENTITY = {
    "GIT_AUTHOR_NAME": "witness-eval",
    "GIT_AUTHOR_EMAIL": "eval@witness.invalid",
    "GIT_COMMITTER_NAME": "witness-eval",
    "GIT_COMMITTER_EMAIL": "eval@witness.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
}
_mirror_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)


@dataclass(frozen=True)
class Materialized:
    path: Path
    base: str
    head: str


def run_git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, **(env or {})},
        check=False,
    )
    if proc.returncode != 0:
        raise git.GitError(f"git {' '.join(args[:3])} failed: {proc.stderr.strip()[:500]}")
    return proc.stdout


def mirror(repo: str, cache: Path) -> Path:
    """A bare mirror of ``repo`` (URL or path) in ``cache``, cloned on first use."""
    name = repo.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
    path = (cache / f"{name}.git").resolve()
    with _mirror_locks[str(path)]:
        if not path.exists():
            cache.mkdir(parents=True, exist_ok=True)
            run_git(cache, "clone", "--quiet", "--mirror", repo, str(path))
    return path


def materialize(case: Case, cache: Path, dest: Path) -> Materialized:
    source = mirror(case.repo, cache)
    run_git(dest.parent, "clone", "--quiet", "--shared", "--no-checkout", str(source), str(dest))
    run_git(dest, "config", "core.autocrlf", "false")
    if case.kind == "clean":
        return Materialized(dest, case.parent, case.commit)
    base = commit_with(
        dest,
        case.commit,
        {path: case.parent for path in case.test_files},
        message="witness-eval: the fix, with its tests held back",
    )
    head = commit_with(
        dest,
        base,
        {path: case.parent for path in case.source_files},
        message="witness-eval: reintroduce the bug",
    )
    return Materialized(dest, base, head)


def commit_with(repo: Path, start: str, overrides: dict[str, str], *, message: str) -> str:
    """A commit on top of ``start`` whose tree takes each listed path from another revision.

    A path absent in that revision is deleted.
    """
    with tempfile.TemporaryDirectory(prefix="witness-index-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index"), **_IDENTITY}
        run_git(repo, "read-tree", start, env=env)
        for path, revision in overrides.items():
            entry = run_git(repo, "ls-tree", revision, "--", path).strip()
            if entry:
                mode, _, rest = entry.split(" ", 2)
                blob = rest.split("\t", 1)[0]
                run_git(
                    repo, "update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}", env=env
                )
            else:
                run_git(repo, "update-index", "--force-remove", "--", path, env=env)
        tree = run_git(repo, "write-tree", env=env).strip()
        return run_git(repo, "commit-tree", tree, "-p", start, "-m", message, env=env).strip()


def export_with_tests(repo: Path, rev: str, case: Case, dest: Path) -> Path:
    """The tree at ``rev`` plus the case's hidden tests as of the fix commit."""
    git.export_tree(repo, rev, dest)
    for path in case.hidden_tests:
        content = run_git(repo, "show", f"{case.commit}:{path}")
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return dest
