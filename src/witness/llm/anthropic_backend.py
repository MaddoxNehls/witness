"""Claude via the Anthropic SDK, with JSON-schema structured output."""

from __future__ import annotations

from typing import Any

from witness.llm.base import BackendError, Completion, CompletionRequest

DEFAULT_MODEL = "claude-opus-5-5"
# Server-side refusal fallback: if a safety classifier declines (code review can brush
# against the cyber category), the API retries on a suitable model inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicBackend:
    name = "anthropic"

    def __init__(
        self, model: str = DEFAULT_MODEL, *, client: Any = None, effort: str = "high"
    ) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client = client
        self.model = model
        self._effort = effort

    def complete(self, request: CompletionRequest) -> Completion:
        response = self._client.beta.messages.create(
            model=self.model,
            max_tokens=request.max_tokens,
            system=request.system,
            messages=[{"role": m.role, "content": m.content} for m in request.messages],
            output_config={
                "effort": self._effort,
                "format": {"type": "json_schema", "schema": request.schema},
            },
            # Every prove conversation opens with the same repo context; caching it makes
            # the attempt loop cheap after the first call.
            cache_control={"type": "ephemeral"},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise BackendError("the model declined to answer (stop_reason=refusal)")
        if response.stop_reason == "max_tokens":
            raise BackendError("response hit max_tokens before finishing")
        text = "".join(block.text for block in response.content if block.type == "text")
        usage = response.usage
        return Completion(
            text=text,
            model=str(response.model),
            input_tokens=int(usage.input_tokens)
            + int(usage.cache_read_input_tokens or 0)
            + int(usage.cache_creation_input_tokens or 0),
            output_tokens=int(usage.output_tokens),
        )
