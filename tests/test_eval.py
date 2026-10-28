"""Eval harness: mining filters, scoring rules, case selection, and an offline end-to-end run."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from helpers import DEMO

from witness import git
from witness.eval.manifest import Case, Hunk, Manifest
from witness.eval.materialize import materialize
from witness.eval.mine import RepoSpec, classify_message, is_source, is_test
from witness.eval.report import metrics_table
from witness.eval.runner import EvalOptions, run_eval, select
from witness.eval.score import aggregate, location_match, score_case
from witness.llm import LLM, ReplayBackend
from witness.models import Candidate, Finding, ReviewResult, Severity, Status, Usage

EVAL_DEMO = DEMO.parent / "eval-demo"


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("fix: honour a falsey validator in bucket (#1308)", "bug"),
        ("fix(withBase): prevent false prefix matches (#313)", "bug"),
        ("Fix `intword()` rounding carry for very large numbers (#346)", "bug"),
        ("Fixes #12: crash on empty input", "bug"),
        ("Fix typo in docstring", None),
        ("fix: types for parseQuery", None),
        ("fix(deps): update dependency vitest to v3", None),
        ("Fix tests on Windows", None),
        ("refactor: simplify the tokenizer", "clean"),
        ("Add running_mean recipe", "clean"),
        ("feat: pass namespace to custom merger (#25)", "clean"),
        ("Add a fix for the broken parser", None),  # feature verb, but bug words
        ("Bump version to 4.2", None),
    ],
)
def test_classify_message(message: str, kind: str | None) -> None:
    assert classify_message(message) == kind


def test_path_classification() -> None:
    spec = RepoSpec("x", "u", "MIT", "typescript", ("src/",))
    assert is_test("test/utils.test.ts")
    assert is_test("tests/test_more.py")
    assert is_test("src/__tests__/parse.ts")
    assert not is_test("src/testing.ts")
    assert is_source("src/utils.ts", spec)
    assert not is_source("src/types.d.ts", spec)
    assert not is_source("scripts/release.ts", spec)


def case(kind: str = "bug", repo: str = "r", cid: str = "c") -> Case:
    return Case(
        id=cid,
        kind=kind,  # type: ignore[arg-type]
        repo=repo,
        license="MIT",
        language="python",
        commit="f" * 40,
        parent="p" * 40,
        message="m",
        source_files=["a.py"],
        test_files=["tests/test_a.py"],
        hunks=[Hunk(file="a.py", start=40, end=42)] if kind == "bug" else [],
    )


def finding(
    status: Status, lines: tuple[int, int] = (41, 41), *, regression: bool | None = True
) -> Finding:
    return Finding(
        candidate=Candidate(
            id="C1",
            claim="c",
            file="a.py",
            line_start=lines[0],
            line_end=lines[1],
            trigger="t",
            severity=Severity.HIGH,
            confidence=0.5,
        ),
        status=status,
        reason="r",
        regression=regression,
    )


def result(*findings: Finding, cost: float = 0.5, duration: float = 10.0) -> ReviewResult:
    return ReviewResult(
        repo="r",
        base="b",
        head="h",
        backend="replay",
        model="m",
        language="python",
        test_runner="pytest",
        findings=list(findings),
        usage=Usage(calls=2, cost_usd=cost),
        duration_s=duration,
        context_tokens=1,
    )


class TestScoring:
    @pytest.mark.parametrize(
        ("lines", "expected"),
        [
            ((41, 41), True),
            ((36, 37), True),
            ((45, 50), True),
            ((20, 30), False),
            ((46, 60), False),
        ],
    )
    def test_location_tolerance(self, lines: tuple[int, int], expected: bool) -> None:
        assert location_match(finding(Status.VERIFIED, lines), case()) is expected

    def test_behavioural_match_needs_a_regression_on_a_bug_case(self) -> None:
        far_away = (200, 210)
        verified = score_case(case(), result(finding(Status.VERIFIED, far_away)), verify=True)
        assert verified.findings[0].true_positive  # repro fails on head, passes on the fix
        no_regression = finding(Status.VERIFIED, far_away, regression=None)
        assert not score_case(case(), result(no_regression), verify=True).findings[0].true_positive

    def test_reported_depends_on_mode(self) -> None:
        verify = score_case(case(), result(finding(Status.REJECTED)), verify=True)
        assert verify.reported == []
        untested = score_case(case(), result(finding(Status.UNTESTED)), verify=False)
        assert [f.true_positive for f in untested.reported] == [True]

    def test_aggregate_and_paired_ablation(self) -> None:
        scores = [
            score_case(case(cid="b1"), result(finding(Status.VERIFIED)), verify=True),
            score_case(case(cid="b2"), result(finding(Status.REJECTED, (1, 2))), verify=True),
            score_case(case("clean", cid="c1"), result(finding(Status.REJECTED)), verify=True),
            score_case(case("clean", cid="c2"), result(), verify=True),
        ]
        verified = aggregate(scores, "verify")
        assert (verified.reported, verified.true_positives, verified.precision) == (1, 1, 1.0)
        assert (verified.recall, verified.false_alarm_rate) == (0.5, 0.0)
        assert verified.cost_per_pr_usd == 0.5
        paired = aggregate(scores, "paired", all_hypotheses=True)
        assert (paired.reported, paired.true_positives) == (3, 1)
        assert (paired.clean_flagged, paired.false_alarm_rate) == (1, 0.5)
        assert "| verify | 2 / 2 | 1 | 100% (1/1) | 50% | 0 / 2 |" in metrics_table([verified])


def test_select_spreads_across_repos() -> None:
    manifest = Manifest(
        cases=[
            case(repo="a", cid="a1"),
            case(repo="a", cid="a2"),
            case(repo="b", cid="b1"),
            case("clean", repo="a", cid="ca1"),
            case("clean", repo="b", cid="cb1"),
        ]
    )
    assert [c.id for c in select(manifest, limit=2)] == ["a1", "b1", "ca1", "cb1"]
    assert [c.id for c in select(manifest, limit=None, case_ids=("cb1",))] == ["cb1"]
    with pytest.raises(ValueError, match="unknown case ids"):
        select(manifest, limit=None, case_ids=("nope",))


@pytest.fixture(scope="module")
def eval_demo(tmp_path_factory: pytest.TempPathFactory, docker_client: Any) -> Manifest:
    spec = importlib.util.spec_from_file_location(
        "build_eval_demo", EVAL_DEMO / "build_eval_demo.py"
    )
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return Manifest.load(module.build(tmp_path_factory.mktemp("eval-demo")))


@pytest.mark.integration
class TestOfflineEval:
    def test_miner_finds_and_validates_the_cases(self, eval_demo: Manifest) -> None:
        assert {(c.id, c.kind) for c in eval_demo.cases} == {
            ("eval-demo-bug-ad81546a", "bug"),
            ("eval-demo-bug-c41ee6bb", "bug"),
            ("eval-demo-clean-22ebbea1", "clean"),
        }
        dates = next(c for c in eval_demo.cases if c.id == "eval-demo-bug-ad81546a")
        assert dates.hunks == [Hunk(file="src/invoicing/dates.py", start=16, end=16)]
        assert dates.hidden_tests == ["tests/test_dates.py"]

    def test_bug_pr_is_exactly_the_revert(self, eval_demo: Manifest, tmp_path: Path) -> None:
        dates = next(c for c in eval_demo.cases if c.kind == "bug" and "ad81546a" in c.id)
        mat = materialize(dates, Path(dates.repo).parent / "cache", tmp_path / "repo")
        changes = git.changed_files(mat.path, mat.base, mat.head)
        assert [c.path for c in changes] == ["src/invoicing/dates.py"]  # tests are held back
        assert "-    while day <= end:\n+    while day < end:" in git.diff(
            mat.path, mat.base, mat.head
        )

    @pytest.mark.parametrize("verify", [True, False])
    def test_replay_run(self, eval_demo: Manifest, verify: bool) -> None:
        def make_llm(c: Case) -> LLM:
            return LLM(ReplayBackend(EVAL_DEMO / "replay" / f"{c.id}.json"))

        cache = Path(eval_demo.cases[0].repo).parent / "cache"
        run = run_eval(
            eval_demo, make_llm, EvalOptions(verify=verify, cache=cache, concurrency=3), suite="t"
        )
        headline = run.metrics[0]
        assert (headline.bug_cases, headline.clean_cases, headline.errors) == (2, 1, 0)
        assert headline.recall == 1.0
        if verify:
            assert (headline.precision, headline.false_alarm_rate) == (1.0, 0.0)
            assert headline.fixes_resolving_hidden_tests == 2
            paired = run.metrics[1]
            assert (paired.reported, paired.true_positives, paired.clean_flagged) == (3, 2, 1)
        else:
            assert (headline.reported, headline.true_positives, headline.clean_flagged) == (3, 2, 1)
