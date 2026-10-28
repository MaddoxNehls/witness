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
        self._usage: dict[str, Any] = dict(data.get("usage", {}))
        self.served: list[str] = []

    def complete(self, request: CompletionRequest) -> Completion:
        if request.tag not in self._responses:
            known = ", ".join(sorted(self._responses)) or "none"
            raise BackendError(f"no recorded response for {request.tag!r} (have: {known})")
        self.served.append(request.tag)
        return _replayed(self._responses[request.tag], self._usage.get(request.tag), self.model)


class RecordingBackend:
    """Cache-through wrapper: serve replies already saved at ``path``, call the live
    backend for the rest, and save each new reply (with its usage) immediately.

    The file is a replay fixture, so a recorded run can be replayed offline, and an
    interrupted run resumes without paying for any call twice.
    """

    def __init__(self, inner: Backend, path: Path) -> None:
        self._inner = inner
        self._path = path
        self.name = inner.name
        self.model = inner.model
        cached = _load(path) if path.exists() else {}
        self._responses: dict[str, Any] = dict(cached.get("responses", {}))
        self._usage: dict[str, Any] = dict(cached.get("usage", {}))
        self.live_calls = 0

    def complete(self, request: CompletionRequest) -> Completion:
        if request.tag in self._responses:
            return _replayed(self._responses[request.tag], self._usage.get(request.tag), self.model)
        completion = self._inner.complete(request)
        self.live_calls += 1
        try:
            self._responses[request.tag] = json.loads(completion.text)
        except json.JSONDecodeError:
            self._responses[request.tag] = completion.text
        self._usage[request.tag] = {
            "input_tokens": completion.input_tokens,
            "output_tokens": completion.output_tokens,
            "cost_usd": completion.cost_usd,
        }
        document = {"model": completion.model, "responses": self._responses, "usage": self._usage}
        self._path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        return completion


def _replayed(value: Any, usage: dict[str, Any] | None, model: str) -> Completion:
    usage = usage or {}
    return Completion(
        text=value if isinstance(value, str) else json.dumps(value),
        model=model,
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cost_usd=usage.get("cost_usd"),
    )


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackendError(f"cannot read replay fixture {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BackendError(f"replay fixture {path} must be a JSON object")
    return data
