"""The backend interface: one structured completion per call, nothing provider-specific."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol


class BackendError(RuntimeError):
    """The LLM backend failed or returned something Witness cannot use."""


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class CompletionRequest:
    tag: str  # stable name of the call site, e.g. "prove/C2/attempt-1"; keys replay fixtures
    system: str
    messages: tuple[Message, ...]
    schema: dict[str, Any]  # JSON schema the reply must satisfy
    schema_name: str = "response"
    max_tokens: int = 16_000


@dataclass(frozen=True)
class Completion:
    text: str  # the JSON reply, still unvalidated
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None


class Backend(Protocol):
    """One LLM provider."""

    name: str
    model: str

    def complete(self, request: CompletionRequest) -> Completion: ...
