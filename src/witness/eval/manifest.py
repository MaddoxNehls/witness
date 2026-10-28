"""The benchmark manifest: cases as commit references, never as committed repos.

A **bug case** comes from a real bug-fix commit F (parent P) and becomes a PR that
reintroduces the bug:

* base = F's tree, with F's test changes held back (the tests are the hidden truth)
* head = base with F's source files restored to P: the PR diff is exactly "un-fix"

A **clean case** is a refactor or feature commit C with no known bug: base = P, head = C.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class Hunk(BaseModel):
    file: str
    start: int  # HEAD line numbers, inclusive
    end: int


class Case(BaseModel):
    id: str
    kind: Literal["bug", "clean"]
    repo: str = Field(description="Clone URL or local path.")
    license: str
    language: Literal["python", "typescript"]
    commit: str = Field(description="The fix commit (bug) or the change itself (clean).")
    parent: str
    message: str
    source_files: list[str]
    test_files: list[str]
    hidden_tests: list[str] = Field(
        default_factory=list, description="Bug cases: the fix's test files, at the fix commit."
    )
    hunks: list[Hunk] = Field(
        default_factory=list, description="Bug cases: the reintroduced code, in HEAD lines."
    )


class Manifest(BaseModel):
    version: int = 1
    description: str = ""
    cases: list[Case]

    @classmethod
    def load(cls, path: Path) -> Manifest:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        path.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")
