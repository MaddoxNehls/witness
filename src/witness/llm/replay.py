"""Serve recorded responses from a fixture file, keyed by call tag. No network.

Fixture format::

    {
      "model": "claude-opus-5-5",
      "responses": {
        "hypothesize": {...reply JSON...},
        "prove/C1/attempt-1": {...},
        ...
      }
    }

Keying by tag rather than by prompt hash keeps fixtures stable when prompt wording
changes. That suits demos and pipeline tests, which exercise Witness's own logic
(sandboxing, classification, verdicts), not the model's.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from witness.llm.base import Backend, BackendError, Completion, CompletionRequest


class ReplayBackend:
    name = "replay"

    def __init__(self, fixture: Path | dict[str, Any]) -> None:
        data = fixture if isinstance(fixture, dict) else _load(fixture)
        self.model = str(data.get("model", "replay"))
        self._responses: dict[str, Any] = dict(data.get("responses", {}))
        self.served: list[str] = []

    def complete(self, request: CompletionRequest) -> Completion:
        if request.tag not in self._responses:
            known = ", ".join(sorted(self._responses)) or "none"
            raise BackendError(f"no recorded response for {request.tag!r} (have: {known})")
        self.served.append(request.tag)
        value = self._responses[request.tag]
        text = value if isinstance(value, str) else json.dumps(value)
        return Completion(text=text, model=self.model)


class RecordingBackend:
    """Wrap a live backend and save every reply to ``path`` in replay-fixture format."""

    def __init__(self, inner: Backend, path: Path) -> None:
        self._inner = inner
        self._path = path
        self.name = inner.name
        self.model = inner.model
        self._responses: dict[str, Any] = {}

    def complete(self, request: CompletionRequest) -> Completion:
        completion = self._inner.complete(request)
        try:
            self._responses[request.tag] = json.loads(completion.text)
        except json.JSONDecodeError:
            self._responses[request.tag] = completion.text
        document = {"model": completion.model, "responses": self._responses}
        self._path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        return completion


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackendError(f"cannot read replay fixture {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BackendError(f"replay fixture {path} must be a JSON object")
    return data
