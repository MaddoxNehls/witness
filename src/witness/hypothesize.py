"""Stage 1: one structured LLM call that proposes candidate bugs."""

from __future__ import annotations

import logging

from pydantic import BaseModel

from witness import prompts, tracing
from witness.context import ReviewContext
from witness.llm import LLM, Message
from witness.models import Candidate

logger = logging.getLogger(__name__)


class Hypotheses(BaseModel):
    candidates: list[Candidate]


def hypothesize(llm: LLM, context: ReviewContext, *, max_candidates: int = 5) -> list[Candidate]:
    with tracing.span("hypothesize", input={"context_tokens": context.tokens}) as span:
        user = prompts.HYPOTHESIZE_USER.format(
            context=context.rendered, max_candidates=max_candidates
        )
        reply, _ = llm.structured(
            "hypothesize", prompts.HYPOTHESIZE_SYSTEM, [Message("user", user)], Hypotheses
        )
        candidates = screen(reply.candidates, context.changed_paths, max_candidates)
        span.set_output([c.model_dump(mode="json") for c in candidates])
        return candidates


def screen(candidates: list[Candidate], changed: list[str], limit: int) -> list[Candidate]:
    """Keep the most confident candidates that point into this change.

    Ids are reassigned as C1..Cn in confidence order: they end up in trace names and
    fixture keys, so they should not be free text from the model.
    """
    kept: list[Candidate] = []
    for candidate in sorted(candidates, key=lambda c: c.confidence, reverse=True):
        if candidate.file not in changed:
            logger.info("dropping %s: %s is not a changed file", candidate.id, candidate.file)
            continue
        kept.append(
            candidate.model_copy(
                update={
                    "id": f"C{len(kept) + 1}",
                    "line_end": max(candidate.line_end, candidate.line_start),
                }
            )
        )
    return kept[:limit]
