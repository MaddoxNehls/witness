"""Mine benchmark cases from real repositories.

Selection, cheap to expensive:

1. **Message.** Bug cases: the first line says it fixes something (``fix:``, "Fix ...",
   "Fixes #12") and is not about typos, docs, CI, typing, lint or dependencies. Clean
   cases: refactor/feature/perf messages with no bug words.
2. **Shape.** 1-3 source files and at least one test file changed; the source diff is
   small. Clean cases must also not have their files touched by a bug fix in the next
   50 commits ("no known bug").
3. **Execution (the real filter).** Bug: the fix's own tests must fail on the PR head
   with an assertion or exception (not an import error) and pass on the fixed base.
   Clean: the commit's tests must pass on its head. Anything else is discarded, so every
   kept case is runnable in the Witness sandbox and has executable ground truth.

Linked-issue labels would be a better bug signal than commit messages but need the
GitHub API; messages plus the execution filter are what this builder uses.
"""

from __future__ import annotations

import logging
import re
import tempfile
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from witness import git
from witness.classify import classify_run
from witness.eval.manifest import Case, Hunk
from witness.eval.materialize import export_with_tests, materialize, mirror, run_git
from witness.models import OutcomeKind
from witness.project import detect_project
from witness.sandbox import DockerSandbox, Limits, runner_for

logger = logging.getLogger(__name__)

BUG_MESSAGE = re.compile(
    r"^(fix(\([^)]*\))?!?:|fix(es|ed)?\b|bug ?fix|resolve[sd]? #\d+|correct(s|ed)?\b)", re.I
)
NOT_A_BUG = re.compile(
    r"\b(typos?|lint|linter|ci|docs?|documentation|readme|changelog|typing|types?|"
    r"type hints?|annotations?|mypy|pyright|flake8|ruff|eslint|prettier|format(ting)?|"
    r"tests?|deps?|dependenc(y|ies)|bump|release|build|warnings?|deprecat\w*|coverage)\b",
    re.I,
)
CLEAN_MESSAGE = re.compile(
    r"^(refactor|perf|feat|style)(\([^)]*\))?!?:|^(refactor|simplify|add|improve|support|"
    r"use|move|rename|clean ?up|optimi[sz]e|allow|expose|implement)\b",
    re.I,
)
BUG_WORDS = re.compile(r"\b(fix\w*|bug\w*|regression|crash\w*|broken|wrong|incorrect)\b", re.I)
TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__)/|(^|/)test_[^/]*\.py$|_test\.py$|\.(test|spec)\.[cm]?tsx?$"
)

MAX_SOURCE_FILES = 3
MAX_SOURCE_DIFF_LINES = 120
CLEAN_LOOKAHEAD = 50


@dataclass(frozen=True)
class RepoSpec:
    slug: str
    url: str
    license: str
    language: str  # "python" | "typescript"
    source_roots: tuple[str, ...]  # path prefixes that count as source
    max_bug: int = 6
    max_clean: int = 6
    max_commits: int = 2500


@dataclass
class Candidate:
    commit: str
    parent: str
    message: str
    source_files: list[str]
    test_files: list[str]
    kind: str
    diff_lines: int = 0


@dataclass
class MiningStats:
    scanned: int = 0
    candidates: dict[str, int] = field(default_factory=lambda: {"bug": 0, "clean": 0})
    kept: dict[str, int] = field(default_factory=lambda: {"bug": 0, "clean": 0})
    rejected: dict[str, int] = field(default_factory=dict)


def is_test(path: str) -> bool:
    return bool(TEST_PATH.search(path))


def is_source(path: str, spec: RepoSpec) -> bool:
    suffix = PurePosixPath(path).suffix
    wanted = {".py"} if spec.language == "python" else {".ts", ".mts", ".tsx"}
    return (
        suffix in wanted
        and not path.endswith(".d.ts")
        and not is_test(path)
        and path.startswith(spec.source_roots)
    )


def classify_message(message: str) -> str | None:
    if BUG_MESSAGE.search(message) and not NOT_A_BUG.search(message):
        return "bug"
    if CLEAN_MESSAGE.search(message) and not BUG_WORDS.search(message):
        return "clean"
    return None


def candidates(spec: RepoSpec, repo: Path) -> Iterator[Candidate]:
    """Commits that pass the message and shape filters, newest first."""
    log = run_git(
        repo, "log", "--no-merges", f"-n{spec.max_commits}", "--format=%x00%H %P%x09%s",
        "--name-only", "HEAD",
    )  # fmt: skip
    commits: list[list[str]] = []
    touched: list[set[str]] = []
    for record in log.split("\x00")[1:]:
        header, _, names = record.partition("\n")
        commits.append(header.split("\t", 1))
        touched.append({name for name in names.splitlines() if name})

    for index, (shas, message) in enumerate(commits):
        sha, *parents = shas.split()
        kind = classify_message(message)
        if kind is None or len(parents) != 1:
            continue
        files = sorted(touched[index])
        sources = [f for f in files if is_source(f, spec)]
        tests = [f for f in files if is_test(f)]
        if not 1 <= len(sources) <= MAX_SOURCE_FILES or not tests:
            continue
        numstat = run_git(repo, "diff", "--numstat", parents[0], sha, "--", *sources)
        diff_lines = sum(
            int(a) + int(d)
            for a, d, *_ in (r.split("\t") for r in numstat.splitlines())
            if a != "-"
        )
        if not 0 < diff_lines <= MAX_SOURCE_DIFF_LINES:
            continue
        if kind == "clean":
            # commits[index - k] are *newer* commits (log is newest first).
            later = touched[max(0, index - CLEAN_LOOKAHEAD) : index]
            later_msgs = commits[max(0, index - CLEAN_LOOKAHEAD) : index]
            if any(
                classify_message(m) == "bug" and set(sources) & t
                for t, (_, m) in zip(later, later_msgs, strict=True)
            ):
                continue
        yield Candidate(sha, parents[0], message, sources, tests, kind, diff_lines)


def validate(
    spec: RepoSpec, candidate: Candidate, cache: Path, *, timeout_s: float = 300
) -> Case | str:
    """Run the ground-truth tests; return the case, or the reason it was discarded."""
    case = Case(
        id=f"{spec.slug}-{candidate.kind}-{candidate.commit[:8]}",
        kind="bug" if candidate.kind == "bug" else "clean",
        repo=spec.url,
        license=spec.license,
        language="python" if spec.language == "python" else "typescript",
        commit=candidate.commit,
        parent=candidate.parent,
        message=candidate.message,
        source_files=candidate.source_files,
        test_files=candidate.test_files,
        hidden_tests=[t for t in candidate.test_files] if candidate.kind == "bug" else [],
    )
    with tempfile.TemporaryDirectory(prefix="witness-mine-") as tmp:
        root = Path(tmp)
        mat = materialize(case, cache, root / "repo")
        if case.kind == "bug":
            # Hidden tests deleted by the fix cannot be run; keep the ones that exist at F.
            present = run_git(
                mat.path, "ls-tree", "-r", "--name-only", case.commit, "--", *case.test_files
            )
            case.hidden_tests = present.split()
            if not case.hidden_tests:
                return "no hidden tests at the fix commit"
            head_tree = export_with_tests(mat.path, mat.head, case, root / "head")
            fixed_tree = export_with_tests(mat.path, mat.base, case, root / "fixed")
        else:
            head_tree = git.export_tree(mat.path, mat.head, root / "head")
            fixed_tree = head_tree
        try:
            runner = runner_for(detect_project(head_tree))
            sandbox = DockerSandbox.create(runner, head_tree, limits=Limits(timeout_s=timeout_s))
        except Exception as exc:  # unsupported project, dependency build failure, ...
            return f"environment: {type(exc).__name__}: {str(exc)[:120]}"

        targets = case.hidden_tests if case.kind == "bug" else case.test_files
        targets = [t for t in targets if (fixed_tree / t).is_file()]
        if not targets:
            return "no runnable tests"
        fixed = classify_run(sandbox.run_tests(fixed_tree, targets, label="fixed"), targets[0])
        if fixed.kind is not OutcomeKind.PASSED:
            where = "fixed base" if case.kind == "bug" else "head"
            return f"tests do not pass on the {where}: {fixed.kind.value}"
        if case.kind == "bug":
            buggy = classify_run(sandbox.run_tests(head_tree, targets, label="buggy"), targets[0])
            if buggy.kind not in (OutcomeKind.ASSERTION, OutcomeKind.EXCEPTION):
                return f"hidden tests do not fail cleanly on the PR head: {buggy.kind.value}"
            case.hidden_tests = targets
            case.hunks = [
                Hunk(file=change.path, start=r.start, end=r.end)
                for change in git.changed_files(mat.path, mat.base, mat.head)
                if change.path in case.source_files
                for r in change.hunks
            ]
    return case


def mine_repo(
    spec: RepoSpec,
    cache: Path,
    *,
    workers: int = 4,
    progress: Callable[[str], None] = logger.info,
) -> tuple[list[Case], MiningStats]:
    repo = mirror(spec.url, cache)
    stats = MiningStats()
    pending = {"bug": [], "clean": []}  # type: dict[str, list[Candidate]]
    for candidate in candidates(spec, repo):
        stats.candidates[candidate.kind] += 1
        pending[candidate.kind].append(candidate)
    kept: list[Case] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for kind, cap in (("bug", spec.max_bug), ("clean", spec.max_clean)):
            queue = pending[kind]
            while queue and stats.kept[kind] < cap:
                batch, queue = queue[:workers], queue[workers:]
                for candidate, outcome in zip(
                    batch, pool.map(lambda c: _safe_validate(spec, c, cache), batch), strict=True
                ):
                    if isinstance(outcome, Case) and stats.kept[kind] < cap:
                        kept.append(outcome)
                        stats.kept[kind] += 1
                        progress(f"  kept {outcome.id}: {candidate.message[:70]}")
                    elif isinstance(outcome, str):
                        reason = outcome.split(":")[0]
                        stats.rejected[reason] = stats.rejected.get(reason, 0) + 1
                        progress(f"  skip {candidate.commit[:8]} ({outcome[:90]})")
    return kept, stats


def _safe_validate(spec: RepoSpec, candidate: Candidate, cache: Path) -> Case | str:
    try:
        return validate(spec, candidate, cache)
    except Exception as exc:
        return f"error: {type(exc).__name__}: {str(exc)[:120]}"
