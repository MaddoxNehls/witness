"""The data model shared by every stage of a review.

Two kinds of models live here: schemas the LLM must fill in (``Candidate``,
``ExpectedFailure``), validated with pydantic before anything acts on them, and
records Witness produces itself (``TestOutcome``, ``Finding``, ``ReviewResult``),
which reporters serialize. A future GitHub reporter consumes the same ``Finding``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Candidate(BaseModel):
    """A bug the model believes the diff introduced. Unproven until a test fails."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Short unique id such as C1.")
    claim: str = Field(description="One or two sentences: what is wrong and why it matters.")
    file: str = Field(description="Repo-relative path of the file containing the bug.")
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    trigger: str = Field(description="The concrete input or condition that exposes the bug.")
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)


class OutcomeKind(StrEnum):
    """How a single sandboxed test run ended."""

    PASSED = "passed"
    ASSERTION = "assertion"  # an assertion in the test failed
    EXCEPTION = "exception"  # the code under test raised
    TEST_ERROR = "test_error"  # the test itself is broken: import, syntax, fixture, skip
    TIMEOUT = "timeout"
    INFRA_ERROR = "infra_error"  # the sandbox produced no usable result


class TestOutcome(BaseModel):
    __test__ = False  # not a pytest test class

    kind: OutcomeKind
    summary: str = Field(description="One line explaining the classification.")
    exception_type: str | None = None
    raised_in: str | None = Field(default=None, description="File where the exception surfaced.")
    details: str = Field(default="", description="Failure text from the test report.")
    output: str = Field(default="", description="Tail of the runner's combined output.")
    exit_code: int | None = None
    duration_s: float = 0.0

    @property
    def is_failure(self) -> bool:
        return self.kind in (OutcomeKind.ASSERTION, OutcomeKind.EXCEPTION)


class ExpectedFailure(BaseModel):
    """What the model says its test will fail with, declared before the test runs."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["assertion", "exception"]
    exception_type: str | None = Field(
        description="For kind=exception, the exception class name, e.g. ZeroDivisionError."
    )


class Attempt(BaseModel):
    number: int
    test_code: str
    expected: ExpectedFailure
    reasoning: str
    head: TestOutcome
    base: TestOutcome | None = Field(
        default=None, description="Only run once HEAD failed for the declared reason."
    )
    verdict: str = Field(description="Why this attempt did or did not prove the claim.")

    @property
    def reproduced(self) -> bool:
        return self.base is not None


class Status(StrEnum):
    VERIFIED = "verified"  # fails on HEAD for the claimed reason, and not on BASE
    PRE_EXISTING = "pre_existing"  # reproduces, but fails the same way on BASE too
    REJECTED = "rejected"  # a well-formed repro passed on HEAD: evidence against the claim
    UNVERIFIED = "unverified"  # no valid repro within the attempt budget
    UNTESTED = "untested"  # verification disabled (--no-verify ablation): reported as-is


class FileEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str
    find: str = Field(description="Exact text to replace; must occur exactly once in the file.")
    replace: str


class SuggestedFix(BaseModel):
    explanation: str
    edits: list[FileEdit]
    verified: bool = Field(description="The repro test passes once the edits are applied.")
    outcome: TestOutcome | None = None


class Finding(BaseModel):
    candidate: Candidate
    status: Status
    reason: str
    regression: bool | None = Field(
        default=None, description="True if the repro passes on BASE; None if BASE cannot run it."
    )
    attempts: list[Attempt] = Field(default_factory=list)
    concession: str | None = Field(default=None, description="The model's reason for giving up.")
    fix: SuggestedFix | None = None

    @property
    def evidence(self) -> Attempt | None:
        """The decisive attempt: the last one made."""
        return self.attempts[-1] if self.attempts else None


class Usage(BaseModel):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class ReviewResult(BaseModel):
    repo: str
    base: str
    head: str
    backend: str
    model: str
    language: str
    test_runner: str
    findings: list[Finding]
    usage: Usage
    duration_s: float
    context_tokens: int
    context_omissions: list[str] = Field(default_factory=list)

    def with_status(self, status: Status) -> list[Finding]:
        return [f for f in self.findings if f.status is status]
