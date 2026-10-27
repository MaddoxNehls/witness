"""The prove loop, driven by the replay backend and a scripted sandbox (no Docker)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from helpers import IMPORT_ERROR, PASS, FakeSandbox, assertion, exception

from witness.classify import ExecResult
from witness.context import ReviewContext
from witness.git import FileChange
from witness.llm import LLM, ReplayBackend
from witness.models import Candidate, FileEdit, OutcomeKind, Severity, Status
from witness.project import Project
from witness.prove import EditError, Prover, Trees, apply_edits

CANDIDATE = Candidate(
    id="C1",
    claim="prorate drops the last day",
    file="src/invoicing/dates.py",
    line_start=15,
    line_end=18,
    trigger="a full month",
    severity=Severity.HIGH,
    confidence=0.9,
)
GOOD_TEST = "def test_full_month():\n    assert prorate(...) == Decimal('31.00')\n"


def repro(test_code: str = GOOD_TEST, **expected: Any) -> dict[str, Any]:
    return {
        "reasoning": "because",
        "concede": False,
        "test_code": test_code,
        "expected_failure": expected or {"kind": "assertion", "exception_type": None},
    }


CONCEDE = {
    "reasoning": "the test passed; the code is fine",
    "concede": True,
    "test_code": "",
    "expected_failure": {"kind": "assertion", "exception_type": None},
}
FIX = {
    "explanation": "inclusive end",
    "edits": [{"file": "src/invoicing/dates.py", "find": "day < end", "replace": "day <= end"}],
}


@pytest.fixture
def trees(tmp_path: Path) -> Trees:
    for name in ("head", "base"):
        source = tmp_path / name / "src" / "invoicing" / "dates.py"
        source.parent.mkdir(parents=True)
        source.write_text("while day < end:\n" if name == "head" else "while day <= end:\n")
    return Trees(head=tmp_path / "head", base=tmp_path / "base")


def context() -> ReviewContext:
    return ReviewContext(
        project=Project("python", "pytest", None, "detected"),
        changes=[FileChange("src/invoicing/dates.py", "M")],
        symbols=[],
        rendered="## Diff\n...",
        tokens=3,
    )


def prove(
    responses: dict[str, Any],
    behaviour: Any,
    trees: Trees,
    *,
    propose_fixes: bool = False,
) -> tuple[Any, FakeSandbox, ReplayBackend]:
    backend = ReplayBackend({"responses": responses})
    sandbox = FakeSandbox(behaviour)
    prover = Prover(LLM(backend), sandbox, context(), trees, propose_fixes=propose_fixes)
    return prover.prove(CANDIDATE), sandbox, backend


def by_tree(head: ExecResult, base: ExecResult = PASS, fix: ExecResult = PASS) -> Any:
    return lambda label, _code: {"head": head, "base": base, "fix": fix}[label]


def test_regression_is_verified_and_fix_is_checked(trees: Trees) -> None:
    finding, sandbox, _ = prove(
        {"prove/C1/attempt-1": repro(), "fix/C1": FIX},
        by_tree(head=assertion()),
        trees,
        propose_fixes=True,
    )
    assert finding.status is Status.VERIFIED
    assert finding.regression is True
    assert [label for label, _ in sandbox.calls] == ["head", "base", "fix"]
    assert finding.fix is not None
    assert finding.fix.verified
    # The patch went to a scratch copy; the HEAD snapshot is untouched.
    assert (trees.head / "src/invoicing/dates.py").read_text() == "while day < end:\n"


def test_fix_that_does_not_make_the_repro_pass_is_not_verified(trees: Trees) -> None:
    finding, _, _ = prove(
        {"prove/C1/attempt-1": repro(), "fix/C1": FIX},
        by_tree(head=assertion(), fix=assertion()),
        trees,
        propose_fixes=True,
    )
    assert finding.status is Status.VERIFIED
    assert finding.fix is not None
    assert not finding.fix.verified


def test_broken_test_gets_feedback_and_a_retry(trees: Trees) -> None:
    def behaviour(label: str, code: str) -> ExecResult:
        if label == "head":
            return IMPORT_ERROR if "pricing" in code else assertion()
        return PASS

    finding, _, backend = prove(
        {
            "prove/C1/attempt-1": repro("from invoicing.pricing import x\n"),
            "prove/C1/attempt-2": repro(),
        },
        behaviour,
        trees,
    )
    assert finding.status is Status.VERIFIED
    assert [a.head.kind for a in finding.attempts] == [
        OutcomeKind.TEST_ERROR,
        OutcomeKind.ASSERTION,
    ]
    assert backend.served == ["prove/C1/attempt-1", "prove/C1/attempt-2"]


def test_feedback_reaches_the_model(trees: Trees) -> None:
    seen: list[str] = []

    class Spy(ReplayBackend):
        def complete(self, request: Any) -> Any:
            seen.append(request.messages[-1].content)
            return super().complete(request)

    backend = Spy({"responses": {"prove/C1/attempt-1": repro(), "prove/C1/attempt-2": CONCEDE}})
    Prover(LLM(backend), FakeSandbox(by_tree(head=PASS)), context(), trees).prove(CANDIDATE)
    assert "the test passed on HEAD" in seen[1]
    assert "Classified outcome on HEAD: passed" in seen[1]


def test_passing_repro_then_concession_is_rejected(trees: Trees) -> None:
    finding, sandbox, _ = prove(
        {"prove/C1/attempt-1": repro(), "prove/C1/attempt-2": CONCEDE},
        by_tree(head=PASS),
        trees,
    )
    assert finding.status is Status.REJECTED
    assert finding.concession == "the test passed; the code is fine"
    assert [label for label, _ in sandbox.calls] == ["head"]  # BASE is never consulted


def test_same_failure_on_base_is_pre_existing(trees: Trees) -> None:
    finding, _, _ = prove(
        {"prove/C1/attempt-1": repro()}, by_tree(head=assertion(), base=assertion()), trees
    )
    assert finding.status is Status.PRE_EXISTING
    assert finding.regression is False


def test_new_code_that_base_cannot_run_is_still_verified(trees: Trees) -> None:
    finding, _, _ = prove(
        {"prove/C1/attempt-1": repro()}, by_tree(head=assertion(), base=IMPORT_ERROR), trees
    )
    assert finding.status is Status.VERIFIED
    assert finding.regression is None


def test_wrong_failure_reason_never_verifies(trees: Trees) -> None:
    # The test declares a ZeroDivisionError but the code raises KeyError every time.
    declared = {"kind": "exception", "exception_type": "ZeroDivisionError"}
    responses = {f"prove/C1/attempt-{n}": repro(**declared) for n in (1, 2, 3)}
    finding, sandbox, _ = prove(
        responses, by_tree(head=exception("KeyError", "src/invoicing/dates.py")), trees
    )
    assert finding.status is Status.UNVERIFIED
    assert len(finding.attempts) == 3
    assert "declared ZeroDivisionError but the code raised KeyError" in finding.reason
    assert all(label == "head" for label, _ in sandbox.calls)


def test_declared_exception_from_changed_code_verifies(trees: Trees) -> None:
    declared = {"kind": "exception", "exception_type": "ValueError"}
    finding, _, _ = prove(
        {"prove/C1/attempt-1": repro(**declared)},
        by_tree(head=exception("ValueError", "src/invoicing/dates.py")),
        trees,
    )
    assert finding.status is Status.VERIFIED


def test_llm_failure_degrades_to_unverified(trees: Trees) -> None:
    finding, _, _ = prove({}, by_tree(head=PASS), trees)
    assert finding.status is Status.UNVERIFIED
    assert finding.reason.startswith("LLM error")


def test_spans_follow_candidate_attempt_structure(trees: Trees) -> None:
    tracewright = pytest.importorskip("tracewright")
    from tracewright.testing import InMemoryExporter

    exporter = InMemoryExporter()
    tracewright.init(exporter=exporter)
    try:
        prove(
            {"prove/C1/attempt-1": repro(), "prove/C1/attempt-2": CONCEDE},
            by_tree(head=PASS),
            trees,
        )
    finally:
        tracewright.shutdown()

    [candidate] = exporter.by_name("candidate C1")
    attempts = exporter.children_of(candidate["span_id"])
    assert [a["name"] for a in attempts] == ["attempt 1", "attempt 2"]
    assert [c["kind"] for c in exporter.children_of(attempts[0]["span_id"])] == ["llm"]
    assert candidate["attributes"]["witness.status"] == "rejected"


class TestApplyEdits:
    def test_rejects_paths_outside_the_tree(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x = 1\n")
        with pytest.raises(EditError, match="not a file"):
            apply_edits(
                tmp_path, [FileEdit(file="../a.py", find="x", replace="y")], protected="t.py"
            )

    def test_requires_a_unique_snippet(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x = 1\nx = 1\n")
        with pytest.raises(EditError, match="occurs 2 times"):
            apply_edits(
                tmp_path, [FileEdit(file="a.py", find="x = 1", replace="")], protected="t.py"
            )

    def test_may_not_touch_the_repro(self, tmp_path: Path) -> None:
        (tmp_path / "t.py").write_text("assert False\n")
        with pytest.raises(EditError, match="repro"):
            apply_edits(
                tmp_path, [FileEdit(file="t.py", find="False", replace="True")], protected="t.py"
            )
