"""Outcome classification: the code that decides what counts as proof."""

from __future__ import annotations

import pytest
from helpers import (
    IMPORT_ERROR,
    PASS,
    REPRO,
    assertion,
    exception,
    failed_case,
    junit,
    passed_case,
    run_result,
    skipped_case,
)

from witness.classify import ExecResult, classify_run, is_same_failure, matches_claim
from witness.models import ExpectedFailure, OutcomeKind

CHANGED = ["src/invoicing/money.py", "src/invoicing/dates.py"]

# Verbatim shapes from real runs (pytest 8 and vitest 3 JUnit reporters).
VITEST_ASSERTION = junit(
    failed_case(
        "AssertionError: expected 2 to be 3 // Object.is equality\n\n"
        " \u276f __witness__/repro.test.ts:3:38",
        message="expected 2 to be 3 // Object.is equality",
        exc_type="AssertionError",
    )
)
VITEST_EXCEPTION = junit(
    failed_case(
        "RangeError: bad range\n"
        " \u276f boom src/mod.ts:1:40\n"
        " \u276f __witness__/repro.test.ts:4:21",
        message="bad range",
        exc_type="RangeError",
    )
)
VITEST_MISSING_IMPORT = junit(
    failed_case(
        "Error: Cannot find module './missing' imported from /work/__witness__/repro.test.ts\n"
        " \u276f __witness__/repro.test.ts:1:1",
        message="Cannot find module './missing'",
        exc_type="Error",
    )
)


def kind(result: ExecResult, repro: str = REPRO) -> OutcomeKind:
    return classify_run(result, repro).kind


class TestClassifyRun:
    def test_passing_test(self) -> None:
        assert kind(PASS) is OutcomeKind.PASSED

    def test_assertion_failure(self) -> None:
        outcome = classify_run(assertion(), REPRO)
        assert outcome.kind is OutcomeKind.ASSERTION
        assert outcome.exception_type == "AssertionError"
        assert "Decimal('30.00')" in outcome.summary

    def test_exception_from_code_under_test(self) -> None:
        outcome = classify_run(exception("ZeroDivisionError", "src/invoicing/money.py"), REPRO)
        assert outcome.kind is OutcomeKind.EXCEPTION
        assert outcome.exception_type == "ZeroDivisionError"
        assert outcome.raised_in == "src/invoicing/money.py"

    def test_collection_error_is_a_broken_test(self) -> None:
        outcome = classify_run(IMPORT_ERROR, REPRO)
        assert outcome.kind is OutcomeKind.TEST_ERROR
        assert outcome.exception_type == "ModuleNotFoundError"
        assert "collection failure" in outcome.summary

    def test_summary_is_the_first_line_of_the_message(self) -> None:
        message = "assert 0.57 == 0.58&#10; +  where 0.57 = apply_discount(1.15, 50)"
        result = run_result(junit(failed_case(f"{REPRO}:5: AssertionError", message)))
        outcome = classify_run(result, REPRO)
        assert outcome.summary == "AssertionError: assert 0.57 == 0.58"

    @pytest.mark.parametrize("exc_type", ["NameError", "ImportError", "SyntaxError"])
    def test_errors_that_mean_the_test_cannot_run(self, exc_type: str) -> None:
        assert kind(exception(exc_type, "src/invoicing/money.py")) is OutcomeKind.TEST_ERROR

    def test_exception_raised_by_the_test_itself(self) -> None:
        # e.g. a TypeError from calling the function with the wrong arguments
        assert kind(exception("TypeError", REPRO)) is OutcomeKind.TEST_ERROR

    def test_skipped_tests_prove_nothing(self) -> None:
        assert kind(run_result(junit(skipped_case()), exit_code=0)) is OutcomeKind.TEST_ERROR

    def test_no_tests_collected(self) -> None:
        assert kind(run_result(junit(), exit_code=5)) is OutcomeKind.TEST_ERROR

    def test_any_failure_wins_over_passes(self) -> None:
        mixed = junit(passed_case("a"), failed_case("x.py:1: AssertionError", "assert 1 == 2"))
        assert kind(run_result(mixed)) is OutcomeKind.ASSERTION

    def test_missing_report_is_an_infrastructure_error(self) -> None:
        assert kind(run_result(None, exit_code=137)) is OutcomeKind.INFRA_ERROR
        assert kind(run_result("<not xml", exit_code=1)) is OutcomeKind.INFRA_ERROR

    def test_timeout_and_sandbox_errors(self) -> None:
        timed_out = ExecResult(exit_code=None, output="", junit_xml=None, timed_out=True)
        broken = ExecResult(exit_code=None, output="", junit_xml=None, error="daemon gone")
        assert kind(timed_out) is OutcomeKind.TIMEOUT
        assert classify_run(broken, REPRO).summary == "daemon gone"

    def test_vitest_assertion(self) -> None:
        repro = "__witness__/repro.test.ts"
        assert kind(run_result(VITEST_ASSERTION), repro) is OutcomeKind.ASSERTION

    def test_vitest_exception_located_in_source(self) -> None:
        outcome = classify_run(run_result(VITEST_EXCEPTION), "__witness__/repro.test.ts")
        assert outcome.kind is OutcomeKind.EXCEPTION
        assert (outcome.exception_type, outcome.raised_in) == ("RangeError", "src/mod.ts")

    def test_vitest_failed_import_is_a_broken_test(self) -> None:
        repro = "__witness__/repro.test.ts"
        assert kind(run_result(VITEST_MISSING_IMPORT), repro) is OutcomeKind.TEST_ERROR


ASSERTION = ExpectedFailure(kind="assertion", exception_type=None)


class TestMatchesClaim:
    def test_declared_assertion(self) -> None:
        ok, _ = matches_claim(classify_run(assertion(), REPRO), ASSERTION, CHANGED)
        assert ok

    def test_declared_assertion_but_code_crashed(self) -> None:
        outcome = classify_run(exception("KeyError", "src/invoicing/money.py"), REPRO)
        ok, why = matches_claim(outcome, ASSERTION, CHANGED)
        assert not ok
        assert "declared an assertion" in why

    def test_declared_exception_matches_type_and_location(self) -> None:
        outcome = classify_run(
            exception("decimal.InvalidOperation", "src/invoicing/money.py"), REPRO
        )
        expected = ExpectedFailure(kind="exception", exception_type="InvalidOperation")
        assert matches_claim(outcome, expected, CHANGED)[0]

    def test_wrong_exception_type(self) -> None:
        outcome = classify_run(exception("KeyError", "src/invoicing/money.py"), REPRO)
        expected = ExpectedFailure(kind="exception", exception_type="ZeroDivisionError")
        ok, why = matches_claim(outcome, expected, CHANGED)
        assert not ok
        assert why == "declared ZeroDivisionError but the code raised KeyError"

    def test_exception_must_come_from_changed_code(self) -> None:
        outcome = classify_run(exception("ValueError", "lib/untouched.py"), REPRO)
        expected = ExpectedFailure(kind="exception", exception_type="ValueError")
        ok, why = matches_claim(outcome, expected, CHANGED)
        assert not ok
        assert "no changed file" in why


class TestSameFailure:
    def test_assertions_on_both_sides(self) -> None:
        head = classify_run(assertion(), REPRO)
        base = classify_run(assertion("Decimal('29.00')"), REPRO)
        assert is_same_failure(head, base)

    def test_base_passes(self) -> None:
        assert not is_same_failure(classify_run(assertion(), REPRO), classify_run(PASS, REPRO))

    def test_different_exceptions(self) -> None:
        head = classify_run(exception("KeyError", "src/a.py"), REPRO)
        base = classify_run(exception("ValueError", "src/a.py"), REPRO)
        assert not is_same_failure(head, base)
