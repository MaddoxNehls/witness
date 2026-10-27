"""The offline demo, end to end: replayed LLM responses, real git, real Docker."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from helpers import DEMO
from rich.console import Console

from witness.cli import main
from witness.llm import LLM, ReplayBackend
from witness.models import OutcomeKind, ReviewResult, Status
from witness.report import JsonReporter, TerminalReporter
from witness.review import review

pytestmark = pytest.mark.integration

FIXTURES = DEMO / "replay.json"


@pytest.fixture(scope="module")
def traced_result(demo_repo: Path, docker_client: Any) -> tuple[ReviewResult, list[dict[str, Any]]]:
    tracewright = pytest.importorskip("tracewright")
    from tracewright.testing import InMemoryExporter

    exporter = InMemoryExporter()
    tracewright.init(exporter=exporter)
    try:
        backend = ReplayBackend(FIXTURES)
        result = review(demo_repo, "main", "float-discounts", LLM(backend))
    finally:
        tracewright.shutdown()
    assert sorted(backend.served) == sorted(json.loads(FIXTURES.read_text())["responses"])
    return result, exporter.spans


def test_two_verified_bugs_and_one_rejected_hypothesis(
    traced_result: tuple[ReviewResult, list[dict[str, Any]]],
) -> None:
    result, _ = traced_result
    statuses = {f.candidate.id: f.status for f in result.findings}
    assert statuses == {"C1": Status.VERIFIED, "C2": Status.VERIFIED, "C3": Status.REJECTED}

    dates, money, subtotal = result.findings
    assert dates.candidate.file == "src/invoicing/dates.py"
    assert dates.regression is True
    assert money.regression is True
    # C2's first test imported a module that does not exist: classified, fed back, retried.
    assert [a.head.kind for a in money.attempts] == [OutcomeKind.TEST_ERROR, OutcomeKind.ASSERTION]
    assert all(f.fix is not None and f.fix.verified for f in (dates, money))
    # C3's repro passed on HEAD, so the claim is dropped, not reported.
    assert subtotal.attempts[0].head.kind is OutcomeKind.PASSED
    assert subtotal.concession is not None


def test_trace_tree(traced_result: tuple[ReviewResult, list[dict[str, Any]]]) -> None:
    _, spans = traced_result
    by_id = {s["span_id"]: s for s in spans}

    def path(span: dict[str, Any]) -> list[str]:
        names = []
        while span is not None:
            names.append(span["name"])
            span = by_id.get(span["parent_span_id"])  # type: ignore[assignment]
        return names[::-1]

    paths = {" > ".join(path(s)) for s in spans}
    assert "witness review > hypothesize > chat claude-opus-5-5" in paths
    assert "witness review > candidate C1 > attempt 1 > sandbox exec head" in paths
    assert "witness review > candidate C1 > attempt 1 > sandbox exec base" in paths
    assert "witness review > candidate C2 > fix > sandbox exec fix" in paths
    assert "witness review > candidate C3 > attempt 2 > chat claude-opus-5-5" in paths


def test_reporters(
    traced_result: tuple[ReviewResult, list[dict[str, Any]]], tmp_path: Path
) -> None:
    result, _ = traced_result
    console = Console(record=True, width=100)
    TerminalReporter(console).report(result)
    text = console.export_text()
    assert "2 verified" in text
    assert "1 rejected" in text
    assert "C1 VERIFIED" in text
    assert "C2 VERIFIED" in text
    assert "Not reported" in text

    out = tmp_path / "result.json"
    JsonReporter(out).report(result)
    assert ReviewResult.model_validate_json(out.read_text()) == result


def test_cli(demo_repo: Path, docker_client: Any, tmp_path: Path) -> None:
    out = tmp_path / "cli.json"
    argv = ["review", "--repo", str(demo_repo), "--base", "main", "--head", "float-discounts"]
    argv += ["--backend", "replay", "--fixtures", str(FIXTURES), "--json", str(out), "--no-fix"]
    assert main(argv) == 0
    data = json.loads(out.read_text())
    assert [f["status"] for f in data["findings"]] == ["verified", "verified", "rejected"]
    assert all(f["fix"] is None for f in data["findings"])


def test_cli_reports_bad_refs(demo_repo: Path) -> None:
    argv = ["review", "--repo", str(demo_repo), "--base", "main", "--head", "nope"]
    assert main([*argv, "--backend", "replay", "--fixtures", str(FIXTURES)]) == 2
