"""Reporters turn a ``ReviewResult`` into output. A GitHub reporter will be one more."""

from __future__ import annotations

from typing import Protocol

from witness.models import ReviewResult


class Reporter(Protocol):
    def report(self, result: ReviewResult) -> None: ...
