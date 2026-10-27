"""Builders for test-report XML and a scripted sandbox, shared by the unit tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from witness.classify import ExecResult

REPRO = "__witness__/test_witness_repro.py"
DEMO = Path(__file__).resolve().parents[1] / "fixtures" / "demo-repo"


def junit(*cases: str) -> str:
    return f'<testsuites><testsuite name="t">{"".join(cases)}</testsuite></testsuites>'


def passed_case(name: str = "test_it") -> str:
    return f'<testcase classname="repro" name="{name}" time="0.01" />'


def skipped_case() -> str:
    return '<testcase classname="repro" name="test_it"><skipped message="nope" /></testcase>'


def failed_case(text: str, message: str = "", exc_type: str | None = None) -> str:
    type_attr = f' type="{exc_type}"' if exc_type else ""
    return (
        f'<testcase classname="repro" name="test_it"><failure message="{message}"{type_attr}>'
        f"{text}</failure></testcase>"
    )


def error_case(text: str, message: str = "collection failure") -> str:
    error = f'<error message="{message}">{text}</error>'
    return f'<testcase classname="" name="test_it">{error}</testcase>'


def run_result(junit_xml: str | None, exit_code: int = 1, output: str = "") -> ExecResult:
    return ExecResult(exit_code=exit_code, output=output, junit_xml=junit_xml)


PASS = run_result(junit(passed_case()), exit_code=0)


def assertion(actual: str = "Decimal('30.00')", expected: str = "Decimal('31.00')") -> ExecResult:
    """A pytest assertion failure, shaped like real ``--junitxml`` output."""
    text = f"&gt;       assert {actual} == {expected}\n\n{REPRO}:8: AssertionError"
    return run_result(junit(failed_case(text, f"assert {actual} == {expected}")))


def exception(exc_type: str, raised_in: str, message: str = "boom") -> ExecResult:
    """A pytest failure whose traceback ends in ``raised_in``."""
    text = (
        f"{REPRO}:6: \n_ _ _ _\n\n    def f():\n&gt;       raise\n"
        f"E       {exc_type}: {message}\n\n{raised_in}:12: {exc_type}"
    )
    return run_result(junit(failed_case(text, f"{exc_type}: {message}")))


IMPORT_ERROR = run_result(
    junit(
        error_case(
            f"{REPRO}:3: in &lt;module&gt;\n"
            "    from invoicing.pricing import apply_discount\n"
            "E   ModuleNotFoundError: No module named 'invoicing.pricing'"
        )
    ),
    exit_code=2,
)

Behaviour = Callable[[str, str], ExecResult]


class FakeSandbox:
    """Answers each run from a function of (run label, test code); records every call."""

    repro_path = REPRO

    def __init__(self, behaviour: Behaviour) -> None:
        self._behaviour = behaviour
        self.calls: list[tuple[str, str]] = []

    def run(self, tree: Path, test_code: str, *, label: str = "run") -> ExecResult:
        self.calls.append((label, test_code))
        return self._behaviour(label, test_code)
