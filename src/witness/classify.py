"""Turn raw sandbox runs into classified outcomes, and outcomes into verdicts.

Everything here is pure: no Docker, no LLM. This is the code that decides whether a
bug is proven, so it is deliberately small, strict and exhaustively tested.

The rule for a proof: the repro must fail on HEAD *for the reason it declared up
front* (an assertion, or a named exception raised from changed code), not because the
test itself is broken. A test that imports something missing, has a syntax error, is
skipped, or raises from its own body proves nothing.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import PurePosixPath

from witness.models import ExpectedFailure, OutcomeKind, TestOutcome

# Exceptions that mean "the test could not even run as written".
TEST_ERROR_TYPES = frozenset(
    {"ImportError", "ModuleNotFoundError", "SyntaxError", "IndentationError", "NameError"}
)

# pytest --tb=long ends each failure with "path/to/file.py:12: ExceptionType".
_PYTEST_TRAILER = re.compile(r"^(?P<file>[^\s:][^:]*\.py):\d+: (?P<type>[A-Za-z_][\w.]*)\s*$", re.M)
# vitest stack frames, marked with U+276F: "> fn src/mod.ts:1:40" or "> x.test.ts:3:38".
_VITEST_FRAME = re.compile(r"\u276f (?:\S+ )?(?P<file>[^\s:]+\.[cm]?[jt]sx?):\d+:\d+")
_LEADING_TYPE = re.compile(r"^(?P<type>[A-Z][\w.]*(?:Error|Exception|Exit|Interrupt)):")
# pytest collection errors have no trailer, only "E   ModuleNotFoundError: ..." lines.
_PYTEST_E_LINE = re.compile(r"^E\s+(?P<type>[A-Z][\w.]*(?:Error|Exception|Exit)):", re.M)


@dataclass(frozen=True)
class ExecResult:
    """What came back from one sandboxed test run, before interpretation."""

    exit_code: int | None
    output: str
    junit_xml: str | None
    timed_out: bool = False
    duration_s: float = 0.0
    error: str | None = None  # the sandbox itself failed (image build, daemon, ...)


@dataclass(frozen=True)
class _Case:
    name: str
    status: str  # "passed", "failure", "error", "skipped"
    message: str
    type: str | None
    text: str


def classify_run(result: ExecResult, repro_path: str) -> TestOutcome:
    """Classify a sandbox run of the repro test file at ``repro_path`` (repo-relative)."""

    def outcome(kind: OutcomeKind, summary: str, case: _Case | None = None) -> TestOutcome:
        exc_type, raised_in = _locate(case) if case else (None, None)
        return TestOutcome(
            kind=kind,
            summary=summary,
            exception_type=exc_type,
            raised_in=raised_in,
            details=case.text[-4000:] if case else "",
            output=result.output[-4000:],
            exit_code=result.exit_code,
            duration_s=result.duration_s,
        )

    if result.error is not None:
        return outcome(OutcomeKind.INFRA_ERROR, result.error)
    if result.timed_out:
        return outcome(OutcomeKind.TIMEOUT, "test run exceeded the time limit")
    cases = _parse_junit(result.junit_xml) if result.junit_xml else None
    if cases is None:
        return outcome(OutcomeKind.INFRA_ERROR, "runner produced no test report")
    if not cases:
        return outcome(OutcomeKind.TEST_ERROR, "no tests were collected")

    failed = next((c for c in cases if c.status in ("failure", "error")), None)
    if failed is None:
        if all(c.status == "skipped" for c in cases):
            return outcome(OutcomeKind.TEST_ERROR, "all tests were skipped")
        return outcome(OutcomeKind.PASSED, "all tests passed")
    kind, summary = _failure_kind(failed, repro_path)
    return outcome(kind, summary, failed)


def _failure_kind(case: _Case, repro_path: str) -> tuple[OutcomeKind, str]:
    exc_type, raised_in = _locate(case)
    # The first line names the failure; pytest's introspection lines stay in ``details``.
    message = case.message.strip().split("\n", 1)[0]
    if exc_type and not message.startswith(exc_type):
        message = f"{exc_type}: {message}"
    label = message[:300] or (exc_type or "error")
    if case.status == "error":  # pytest: collection, setup or teardown error
        return OutcomeKind.TEST_ERROR, f"test errored outside its body: {label}"
    short = _short(exc_type)
    if short == "AssertionError":  # pytest and chai (vitest) alike
        return OutcomeKind.ASSERTION, label
    if short in TEST_ERROR_TYPES:
        return OutcomeKind.TEST_ERROR, f"test is broken: {label}"
    if raised_in is not None and _same_file(raised_in, repro_path):
        return OutcomeKind.TEST_ERROR, f"exception raised by the test itself: {label}"
    return OutcomeKind.EXCEPTION, label


def _locate(case: _Case) -> tuple[str | None, str | None]:
    """Exception type and the file it surfaced in, from pytest or vitest failure text."""
    trailers = list(_PYTEST_TRAILER.finditer(case.text))
    if trailers:
        last = trailers[-1]
        return last.group("type"), last.group("file")
    frame = _VITEST_FRAME.search(case.text)
    exc_type = case.type
    if exc_type is None and (match := _LEADING_TYPE.match(case.message)):
        exc_type = match.group("type")
    if exc_type is None and (e_lines := _PYTEST_E_LINE.findall(case.text)):
        exc_type = e_lines[-1]
    return exc_type, frame.group("file") if frame else None


def _parse_junit(xml_text: str) -> list[_Case] | None:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    cases: list[_Case] = []
    for testcase in root.iter("testcase"):
        status, message, exc_type, text = "passed", "", None, ""
        for tag in ("failure", "error", "skipped"):
            element = testcase.find(tag)
            if element is not None:
                status = tag
                message = element.get("message", "")
                exc_type = element.get("type") or None
                text = (element.text or "").strip()
                break
        cases.append(_Case(testcase.get("name", ""), status, message, exc_type, text))
    return cases


def _short(name: str | None) -> str | None:
    return name.rsplit(".", 1)[-1] if name else None


def _same_file(a: str, b: str) -> bool:
    pa, pb = PurePosixPath(a.replace("\\", "/")), PurePosixPath(b.replace("\\", "/"))
    return (
        pa == pb or pa.parts[-len(pb.parts) :] == pb.parts or pb.parts[-len(pa.parts) :] == pa.parts
    )


def matches_claim(
    outcome: TestOutcome, expected: ExpectedFailure, changed_files: list[str]
) -> tuple[bool, str]:
    """Did the test fail for the reason it declared? Returns (ok, explanation)."""
    if expected.kind == "assertion":
        if outcome.kind is OutcomeKind.ASSERTION:
            return True, "failed on the declared assertion"
        return (
            False,
            f"declared an assertion failure but got {outcome.kind.value}: {outcome.summary}",
        )

    if outcome.kind is not OutcomeKind.EXCEPTION:
        return False, f"declared an exception but got {outcome.kind.value}: {outcome.summary}"
    wanted, got = _short(expected.exception_type), _short(outcome.exception_type)
    if wanted != got:
        return False, f"declared {wanted} but the code raised {got}"
    # The exception must come out of code this change touched, not some unrelated module.
    trace = f"{outcome.raised_in or ''}\n{outcome.details}"
    if not any(path in trace for path in changed_files):
        return False, f"{got} was raised, but no changed file appears in its traceback"
    return True, f"raised the declared {got} from changed code"


def is_same_failure(head: TestOutcome, base: TestOutcome) -> bool:
    """BASE failing the same way as HEAD means the bug predates the change."""
    if not base.is_failure or base.kind is not head.kind:
        return False
    return head.kind is OutcomeKind.ASSERTION or _short(head.exception_type) == _short(
        base.exception_type
    )
