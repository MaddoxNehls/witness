"""Assemble what the model sees about a change, within a token budget.

Truncation strategy
-------------------
The context is a list of sections in priority order:

1. the project summary and the repo file list (capped at ``MAX_TREE_PATHS``),
2. the diff, one section per changed file,
3. the enclosing function/class of every changed hunk, with line numbers,
4. the full HEAD content of each changed file.

Sections are added greedily in that order. The first section that does not fit is cut
at a line boundary with an explicit ``[truncated]`` marker; every section after it is
dropped and named in an "Omitted" footer, so the model knows what it did not see.
Higher layers are summaries of lower ones, so whatever survives is the most useful
view of the change. Tokens are estimated at four characters each, which is close
enough for budgeting and avoids a tokenizer round-trip.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from witness import git
from witness.project import Project
from witness.symbols import Symbol, enclosing_symbols, grammar_for

DEFAULT_BUDGET_TOKENS = 60_000
MAX_TREE_PATHS = 300
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


@dataclass(frozen=True)
class Section:
    title: str
    body: str

    def render(self) -> str:
        return f"## {self.title}\n{self.body.rstrip()}\n"


@dataclass(frozen=True)
class ReviewContext:
    project: Project
    changes: list[git.FileChange]
    symbols: list[Symbol]
    rendered: str
    tokens: int
    omitted: list[str] = field(default_factory=list)

    @property
    def changed_paths(self) -> list[str]:
        return [c.path for c in self.changes if c.status != "D"]


def build_context(
    repo: Path,
    base: str,
    head: str,
    head_tree: Path,
    project: Project,
    *,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
) -> ReviewContext:
    changes = git.changed_files(repo, base, head)
    file_diffs = split_diff(git.diff(repo, base, head))

    symbols: list[Symbol] = []
    sources: dict[str, str] = {}
    for change in changes:
        path = head_tree / change.path
        if change.status == "D" or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        sources[change.path] = text
        symbols += enclosing_symbols(change.path, text, change.hunks)

    sections = [
        Section("Project", _project_summary(project, changes)),
        Section("Repository files", _file_tree(git.list_files(repo, head))),
        *(Section(f"Diff: {path}", body) for path, body in file_diffs.items()),
        *(Section(f"Changed code: {s.path} {s.name}", s.render()) for s in symbols),
        *(
            Section(f"Full file at HEAD: {path}", number_lines(text))
            for path, text in sources.items()
            if grammar_for(path) is not None
        ),
    ]
    rendered, omitted = fit_sections(sections, budget_tokens)
    return ReviewContext(project, changes, symbols, rendered, estimate_tokens(rendered), omitted)


def fit_sections(sections: list[Section], budget_tokens: int) -> tuple[str, list[str]]:
    """Render sections in order until the budget runs out. See the module docstring.

    The "Omitted" footer is paid for out of the same budget, so the result stays within
    ``budget_tokens`` unless the footer alone is larger than what is left.
    """
    parts: list[str] = []
    remaining = budget_tokens
    for index, section in enumerate(sections):
        text = section.render()
        cost = estimate_tokens(text) + 1  # +1 for the joining newline
        if cost <= remaining:
            parts.append(text)
            remaining -= cost
            continue
        later = [s.title for s in sections[index + 1 :]]
        cut_titles = [f"{section.title} (truncated)", *later]
        truncated = _truncate(text, remaining - estimate_tokens(_omitted_footer(cut_titles)) - 1)
        if truncated:
            parts.append(truncated)
            omitted = cut_titles
        else:
            omitted = [section.title, *later]
        parts.append(_omitted_footer(omitted))
        return "\n".join(parts), omitted
    return "\n".join(parts), []


def _omitted_footer(titles: list[str]) -> str:
    return "## Omitted (over the context budget)\n" + "\n".join(f"- {t}" for t in titles)


def _truncate(text: str, budget_tokens: int) -> str:
    marker = "[truncated: {n} more lines]\n"
    allowance = budget_tokens * CHARS_PER_TOKEN - len(marker) - 8
    if allowance <= 0:
        return ""
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) > allowance:
            break
        kept.append(line)
        used += len(line)
    if len(kept) <= 1:  # a heading alone is noise
        return ""
    return "".join(kept) + marker.format(n=len(lines) - len(kept))


def split_diff(diff_text: str) -> dict[str, str]:
    """Split a unified diff into per-file chunks keyed by the HEAD path."""
    chunks: dict[str, str] = {}
    current: list[str] = []
    path = ""
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git ") and current:
            chunks[path] = "".join(current)
            current = []
        if line.startswith("diff --git "):
            path = line.rsplit(" b/", 1)[-1].strip()
        current.append(line)
    if current:
        chunks[path] = "".join(current)
    return chunks


def number_lines(text: str) -> str:
    lines = text.splitlines()
    width = len(str(len(lines)))
    return "\n".join(f"{i:>{width}} | {line}" for i, line in enumerate(lines, start=1))


def _project_summary(project: Project, changes: list[git.FileChange]) -> str:
    counts: dict[str, int] = {}
    for change in changes:
        counts[change.status] = counts.get(change.status, 0) + 1
    changed = ", ".join(f"{n} {status}" for status, n in sorted(counts.items()))
    return (
        f"language: {project.language}\n"
        f"test runner: {project.runner} ({project.source})\n"
        f"changed files: {changed or 'none'} (A=added, M=modified, D=deleted, R=renamed)"
    )


def _file_tree(paths: list[str]) -> str:
    shown = paths[:MAX_TREE_PATHS]
    extra = len(paths) - len(shown)
    return "\n".join(shown) + (f"\n... and {extra} more files" if extra else "")
