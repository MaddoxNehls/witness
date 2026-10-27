"""Stage 2: try to prove each candidate with a reproducing test, in a bounded loop.

Per attempt the model writes a test and declares how it will fail; the sandbox runs
it on HEAD; ``classify`` decides whether it failed for the declared reason. A match is
then run on BASE to tell a regression from a pre-existing bug. Mismatches go back to
the model as feedback, and the model may concede. Only the verdict logic here, never
the model, decides a finding's status.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from witness import prompts, tracing
from witness.classify import ExecResult, classify_run, is_same_failure, matches_claim
from witness.context import ReviewContext
from witness.llm import LLM, BackendError, Message
from witness.models import (
    Attempt,
    Candidate,
    ExpectedFailure,
    FileEdit,
    Finding,
    OutcomeKind,
    Status,
    SuggestedFix,
    TestOutcome,
)

logger = logging.getLogger(__name__)


class Sandbox(Protocol):
    @property
    def repro_path(self) -> str: ...

    def run(self, tree: Path, test_code: str, *, label: str = "run") -> ExecResult: ...


class ReproReply(BaseModel):
    reasoning: str = Field(description="Why this test exposes the bug, or why you concede.")
    concede: bool = Field(description="True if the evidence shows the hypothesis is false.")
    test_code: str = Field(description="The complete test file. Empty when conceding.")
    expected_failure: ExpectedFailure


class FixReply(BaseModel):
    explanation: str
    edits: list[FileEdit]


class EditError(ValueError):
    pass


@dataclass(frozen=True)
class Trees:
    head: Path
    base: Path


class Prover:
    def __init__(
        self,
        llm: LLM,
        sandbox: Sandbox,
        context: ReviewContext,
        trees: Trees,
        *,
        max_attempts: int = 3,
        propose_fixes: bool = True,
    ) -> None:
        self._llm = llm
        self._sandbox = sandbox
        self._context = context
        self._trees = trees
        self._max_attempts = max_attempts
        self._propose_fixes = propose_fixes
        runner = context.project.runner
        self._system = prompts.PROVE_SYSTEM.format(
            framework=runner,
            repro_path=sandbox.repro_path,
            import_hint=prompts.IMPORT_HINTS.get(runner, ""),
        )

    def prove(self, candidate: Candidate) -> Finding:
        with tracing.span(
            f"candidate {candidate.id}", input=candidate.model_dump(mode="json")
        ) as span:
            try:
                finding = self._prove(candidate)
            except BackendError as exc:
                logger.warning("LLM failed while proving %s: %s", candidate.id, exc)
                finding = Finding(
                    candidate=candidate, status=Status.UNVERIFIED, reason=f"LLM error: {exc}"
                )
            span.set_attributes({"witness.status": finding.status.value})
            span.set_output({"status": finding.status.value, "reason": finding.reason})
            return finding

    def _prove(self, candidate: Candidate) -> Finding:
        messages = [
            Message(
                "user",
                prompts.PROVE_USER.format(
                    context=self._context.rendered,
                    candidate=candidate.model_dump_json(indent=2),
                ),
            )
        ]
        attempts: list[Attempt] = []
        concession: str | None = None
        for number in range(1, self._max_attempts + 1):
            with tracing.span(f"attempt {number}") as span:
                reply, raw = self._llm.structured(
                    f"prove/{candidate.id}/attempt-{number}", self._system, messages, ReproReply
                )
                if reply.concede or not reply.test_code.strip():
                    concession = reply.reasoning
                    span.set_output({"conceded": reply.reasoning})
                    break
                attempt = self._run_attempt(number, reply)
                attempts.append(attempt)
                span.set_output({"head": attempt.head.kind.value, "verdict": attempt.verdict})
            if attempt.reproduced:
                finding = judge_reproduction(candidate, attempts)
                if finding.status is Status.VERIFIED and self._propose_fixes:
                    finding.fix = self._propose_fix(candidate, attempt)
                return finding
            messages += [
                Message("assistant", raw),
                Message(
                    "user",
                    prompts.ATTEMPT_FEEDBACK.format(
                        number=number,
                        verdict=attempt.verdict,
                        kind=attempt.head.kind.value,
                        summary=attempt.head.summary,
                        output=attempt.head.output[-3000:] or "(none)",
                    ),
                ),
            ]
        return judge_no_reproduction(candidate, attempts, concession)

    def _run_attempt(self, number: int, reply: ReproReply) -> Attempt:
        repro = self._sandbox.repro_path
        head = classify_run(
            self._sandbox.run(self._trees.head, reply.test_code, label="head"), repro
        )
        if head.is_failure:
            ok, verdict = matches_claim(head, reply.expected_failure, self._context.changed_paths)
        else:
            ok, verdict = False, _describe_non_failure(head)
        base = None
        if ok:
            base = classify_run(
                self._sandbox.run(self._trees.base, reply.test_code, label="base"), repro
            )
        return Attempt(
            number=number,
            test_code=reply.test_code,
            expected=reply.expected_failure,
            reasoning=reply.reasoning,
            head=head,
            base=base,
            verdict=verdict,
        )

    def _propose_fix(self, candidate: Candidate, attempt: Attempt) -> SuggestedFix | None:
        with tracing.span("fix") as span:
            user = prompts.FIX_USER.format(
                context=self._context.rendered,
                candidate=candidate.model_dump_json(indent=2),
                repro_path=self._sandbox.repro_path,
                test_code=attempt.test_code,
                failure=attempt.head.details or attempt.head.summary,
            )
            try:
                reply, _ = self._llm.structured(
                    f"fix/{candidate.id}", prompts.FIX_SYSTEM, [Message("user", user)], FixReply
                )
            except BackendError as exc:
                logger.warning("no fix for %s: %s", candidate.id, exc)
                return None
            with tempfile.TemporaryDirectory(prefix="witness-fix-") as tmp:
                tree = Path(shutil.copytree(self._trees.head, Path(tmp) / "tree"))
                try:
                    apply_edits(tree, reply.edits, protected=self._sandbox.repro_path)
                except EditError as exc:
                    return SuggestedFix(
                        explanation=f"{reply.explanation} (could not apply: {exc})",
                        edits=reply.edits,
                        verified=False,
                    )
                result = self._sandbox.run(tree, attempt.test_code, label="fix")
                outcome = classify_run(result, self._sandbox.repro_path)
            span.set_output({"verified": outcome.kind is OutcomeKind.PASSED})
            return SuggestedFix(
                explanation=reply.explanation,
                edits=reply.edits,
                verified=outcome.kind is OutcomeKind.PASSED,
                outcome=outcome,
            )


def judge_reproduction(candidate: Candidate, attempts: list[Attempt]) -> Finding:
    """The last attempt failed on HEAD as declared; BASE decides what kind of finding it is."""
    attempt = attempts[-1]
    head, base = attempt.head, attempt.base
    assert base is not None
    if base.kind is OutcomeKind.PASSED:
        status, regression = Status.VERIFIED, True
        reason = f"{attempt.verdict}; the same test passes on BASE"
    elif is_same_failure(head, base):
        status, regression = Status.PRE_EXISTING, False
        reason = f"{attempt.verdict}, but BASE fails the same way, so this change did not cause it"
    else:
        status, regression = Status.VERIFIED, None
        reason = f"{attempt.verdict}; BASE cannot confirm a regression ({base.kind.value})"
    return Finding(
        candidate=candidate, status=status, reason=reason, regression=regression, attempts=attempts
    )


def judge_no_reproduction(
    candidate: Candidate, attempts: list[Attempt], concession: str | None
) -> Finding:
    passed = [a for a in attempts if a.head.kind is OutcomeKind.PASSED]
    if passed:
        status = Status.REJECTED
        reason = f"a well-formed repro passed on HEAD (attempt {passed[-1].number})"
        if concession:
            reason += "; the model conceded"
    elif attempts:
        status = Status.UNVERIFIED
        reason = f"no valid repro in {len(attempts)} attempt(s); last: {attempts[-1].verdict}"
    else:
        status, reason = Status.UNVERIFIED, "the model conceded before writing a test"
    return Finding(
        candidate=candidate,
        status=status,
        reason=reason,
        attempts=attempts,
        concession=concession,
    )


def apply_edits(root: Path, edits: list[FileEdit], *, protected: str) -> None:
    """Apply model-proposed exact-match edits inside ``root``. Paths are untrusted."""
    resolved_root = root.resolve()
    for edit in edits:
        target = (root / edit.file).resolve()
        if not target.is_relative_to(resolved_root) or not target.is_file():
            raise EditError(f"{edit.file} is not a file in the repository")
        if target == (root / protected).resolve():
            raise EditError("a fix may not edit the repro test")
        text = target.read_text(encoding="utf-8")
        count = text.count(edit.find)
        if count != 1:
            raise EditError(f"snippet occurs {count} times in {edit.file}, expected exactly once")
        target.write_text(text.replace(edit.find, edit.replace), encoding="utf-8")


def _describe_non_failure(outcome: TestOutcome) -> str:
    if outcome.kind is OutcomeKind.PASSED:
        return "the test passed on HEAD, so it does not demonstrate the bug"
    return f"inconclusive: {outcome.kind.value}: {outcome.summary}"
