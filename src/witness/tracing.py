"""Optional Tracewright tracing. Without the ``tracing`` extra every span is a no-op.

Span tree of a review::

    witness review            (agent, the run)
      hypothesize             (custom)
        chat <model>          (llm)
      candidate C1            (custom)
        attempt 1             (custom)
          chat <model>        (llm)
          sandbox exec head   (tool)
          sandbox exec base   (tool)
        fix                   (custom)
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from types import ModuleType
from typing import Any, Literal, Protocol

tracewright: ModuleType | None
try:
    import tracewright
except ImportError:  # the "tracing" extra is not installed
    tracewright = None

SpanKind = Literal["agent", "llm", "tool", "custom"]


class SpanHandle(Protocol):
    def set_attributes(self, attributes: Mapping[str, Any]) -> None: ...
    def set_output(self, value: Any) -> None: ...


class _NullSpan:
    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        pass

    def set_output(self, value: Any) -> None:
        pass


def enabled() -> bool:
    return tracewright is not None


def init() -> None:
    """Configure the exporter from TRACEWRIGHT_API_KEY / TRACEWRIGHT_ENDPOINT, if installed."""
    if tracewright is not None:
        tracewright.init()


def flush() -> None:
    if tracewright is not None:
        tracewright.flush()


@contextmanager
def run(name: str, *, input: Any = None, tags: list[str] | None = None) -> Iterator[SpanHandle]:
    if tracewright is None:
        yield _NullSpan()
        return
    with tracewright.run(name, input=input, tags=tags) as current:
        yield current


@contextmanager
def span(
    name: str,
    *,
    kind: SpanKind = "custom",
    input: Any = None,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[SpanHandle]:
    if tracewright is None:
        yield _NullSpan()
        return
    with tracewright.span(name, kind=kind, input=input, attributes=attributes) as current:
        yield current
