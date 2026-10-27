"""LLM backends. Provider SDKs are imported lazily, so replay needs neither."""

from __future__ import annotations

from pathlib import Path

from witness.llm.base import Backend, BackendError, Completion, CompletionRequest, Message
from witness.llm.client import LLM
from witness.llm.replay import RecordingBackend, ReplayBackend

BACKENDS = ("anthropic", "openrouter", "claude-cli", "replay")


def create_backend(name: str, *, model: str | None = None, fixtures: Path | None = None) -> Backend:
    if name == "replay":
        if fixtures is None:
            raise ValueError("the replay backend needs --fixtures PATH")
        return ReplayBackend(fixtures)
    if name == "anthropic":
        from witness.llm.anthropic_backend import DEFAULT_MODEL, AnthropicBackend

        return AnthropicBackend(model or DEFAULT_MODEL)
    if name == "openrouter":
        from witness.llm.openrouter_backend import DEFAULT_MODEL as OPENROUTER_MODEL
        from witness.llm.openrouter_backend import OpenRouterBackend

        return OpenRouterBackend(model or OPENROUTER_MODEL)
    if name == "claude-cli":
        from witness.llm.claude_cli import ClaudeCliBackend

        return ClaudeCliBackend(model)
    raise ValueError(f"unknown backend {name!r}; choose from {', '.join(BACKENDS)}")


__all__ = [
    "BACKENDS",
    "LLM",
    "Backend",
    "BackendError",
    "Completion",
    "CompletionRequest",
    "Message",
    "RecordingBackend",
    "ReplayBackend",
    "create_backend",
]
