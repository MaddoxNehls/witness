"""Structured calls on top of any backend: tracing, validation, one repair retry, usage."""

from __future__ import annotations

import json

from pydantic import BaseModel, ValidationError

from witness import tracing
from witness.llm.base import Backend, BackendError, Completion, CompletionRequest, Message
from witness.llm.schema import strict_schema
from witness.models import Usage


class LLM:
    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self.usage = Usage()

    def structured[T: BaseModel](
        self, tag: str, system: str, messages: list[Message], output: type[T]
    ) -> tuple[T, str]:
        """Ask for a reply matching ``output``. Returns the parsed model and the raw JSON.

        A reply that fails validation gets one retry with the error shown to the model;
        a second failure raises ``BackendError``.
        """
        schema = strict_schema(output)
        conversation = list(messages)
        for attempt in (1, 2):
            request = CompletionRequest(
                tag=tag if attempt == 1 else f"{tag}/repair",
                system=system,
                messages=tuple(conversation),
                schema=schema,
                schema_name=output.__name__,
            )
            completion = self._complete(request)
            try:
                return output.model_validate_json(_extract_json(completion.text)), completion.text
            except (ValidationError, ValueError) as exc:
                error = exc
            conversation += [
                Message("assistant", completion.text),
                Message(
                    "user",
                    f"That reply was not valid against the schema:\n{error}\n"
                    "Reply again with only the corrected JSON object.",
                ),
            ]
        raise BackendError(f"{tag}: model reply failed validation twice: {error}")

    def _complete(self, request: CompletionRequest) -> Completion:
        attributes = {
            "gen_ai.provider.name": self.backend.name,
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": self.backend.model,
            "witness.call": request.tag,
        }
        with tracing.span(
            f"chat {self.backend.model}",
            kind="llm",
            input={"system": request.system, "messages": [m.__dict__ for m in request.messages]},
            attributes=attributes,
        ) as span:
            completion = self.backend.complete(request)
            span.set_attributes(
                {
                    "gen_ai.response.model": completion.model,
                    "gen_ai.usage.input_tokens": completion.input_tokens,
                    "gen_ai.usage.output_tokens": completion.output_tokens,
                }
            )
            span.set_output(completion.text)
        self.usage.calls += 1
        self.usage.input_tokens += completion.input_tokens
        self.usage.output_tokens += completion.output_tokens
        self.usage.cost_usd += completion.cost_usd or 0.0
        return completion


def _extract_json(text: str) -> str:
    """Accept a bare JSON object, or one wrapped in prose or a code fence."""
    text = text.strip()
    if text.startswith("{"):
        return text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("reply contains no JSON object")
    candidate = text[start : end + 1]
    json.loads(candidate)  # raise ValueError early with a clear position
    return candidate
