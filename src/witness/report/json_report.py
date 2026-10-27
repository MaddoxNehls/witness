"""Machine-readable output: the full ``ReviewResult``, including every attempt."""

from __future__ import annotations

from pathlib import Path

from witness.models import ReviewResult


class JsonReporter:
    def __init__(self, path: Path) -> None:
        self.path = path

    def report(self, result: ReviewResult) -> None:
        self.path.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
