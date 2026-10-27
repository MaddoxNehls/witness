"""Any model on OpenRouter via the OpenAI SDK, with JSON-schema response format."""

from __future__ import annotations

import os
from typing import Any

from witness.llm.base import BackendError, Completion, CompletionRequest

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "anthropic/claude-opus-5.5"


class OpenRouterBackend:
    name = "openrouter"

    def __init__(self, model: str = DEFAULT_MODEL, *, client: Any = None) -> None:
        if client is None:
            import openai

            api_key = os.environ.get("OPENROUTER_API_KEY")
            if not api_key:
                raise BackendError("set OPENROUTER_API_KEY to use the openrouter backend")
            client = openai.OpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key)
        self._client = client
        self.model = model

    def complete(self, request: CompletionRequest) -> Completion:
        messages: list[dict[str, str]] = [{"role": "system", "content": request.system}]
        messages += [{"role": m.role, "content": m.content} for m in request.messages]
        response = self._client.chat.completions.create(
            model=self.model,
            max_tokens=request.max_tokens,
            messages=messages,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "strict": True,
                    "schema": request.schema,
                },
            },
        )
        if not response.choices:
            raise BackendError("OpenRouter returned no choices")
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise BackendError("response hit max_tokens before finishing")
        usage = response.usage
        return Completion(
            text=choice.message.content or "",
            model=str(response.model),
            input_tokens=int(usage.prompt_tokens) if usage else 0,
            output_tokens=int(usage.completion_tokens) if usage else 0,
        )
