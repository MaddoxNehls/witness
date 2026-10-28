"""PR rendering, the GitHub gateway (respx-mocked API) and the worker end to end."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from helpers import DEMO

from witness import git
from witness.app.github import GitHubGateway
from witness.app.render import MARKER, check_run_output, fenced, review_payload
from witness.app.store import Job, JobSpec, Store
from witness.app.worker import Worker
from witness.git import LineRange
from witness.models import (
    Attempt,
    Candidate,
    ExpectedFailure,
    FileEdit,
    Finding,
    OutcomeKind,
    ReviewResult,
    Severity,
    Status,
    SuggestedFix,
    TestOutcome,
    Usage,
)

HEAD_DATES = (DEMO / "head" / "src" / "invoicing" / "dates.py").read_text(encoding="utf-8")
TEST_CODE = "def test_full_month():\n    assert prorate(FEE, JAN_1, JAN_31) == FEE\n"
FAILURE = "AssertionError: assert Decimal('30.00') == Decimal('31.00')"
API = "https://api.github.com"


def finding(
    status: Status = Status.VERIFIED, *, cid: str = "C1", fix: SuggestedFix | None = None
) -> Finding:
    head = TestOutcome(
        kind=OutcomeKind.ASSERTION, summary=FAILURE, details="E   assert 30 == 31", exit_code=1
    )
    return Finding(
        candidate=Candidate(
            id=cid,
            claim="The end date is dropped.",
            file="src/invoicing/dates.py",
            line_start=15,
            line_end=18,
            trigger="a full month",
            severity=Severity.HIGH,
            confidence=0.9,
        ),
        status=status,
        reason="failed on the declared assertion; the same test passes on BASE",
        regression=True,
        attempts=[
            Attempt(
                number=1,
                test_code=TEST_CODE,
                expected=ExpectedFailure(kind="assertion", exception_type=None),
                reasoning="r",
                head=head,
                verdict="v",
            )
        ],
        fix=fix,
    )


FIX = SuggestedFix(
    explanation="The end is inclusive.",
    edits=[
        FileEdit(
            file="src/invoicing/dates.py",
            find="    while day < end:\n",
            replace="    while day <= end:\n",
        )
    ],
    verified=True,
)


def result(*findings: Finding, head: str = "a" * 40) -> ReviewResult:
    return ReviewResult(
        repo="r",
        base="b" * 40,
        head=head,
        backend="replay",
        model="m",
        language="python",
        test_runner="pytest",
        findings=list(findings),
        usage=Usage(),
        duration_s=1.0,
        context_tokens=10,
    )


def read_head(path: str) -> str | None:
    return HEAD_DATES if path == "src/invoicing/dates.py" else None


class TestRender:
    def test_verified_fix_becomes_a_suggestion_on_exactly_its_lines(self) -> None:
        payload = review_payload(
            result(finding(fix=FIX), finding(Status.REJECTED, cid="C2")),
            commentable={"src/invoicing/dates.py": [LineRange(8, 32)]},
            read_file=read_head,
        )
        expected_body = "\n\n".join(
            [
                "**Witness: verified bug** (high): The end date is dropped.",
                "**Trigger:** a full month\n**Evidence:** failed on the declared assertion; "
                "the same test passes on BASE. On this PR it fails with "
                f"`{FAILURE}`.",
                "<details><summary>Reproducing test</summary>\n\n"
                f"```python\n{TEST_CODE.rstrip()}\n```\n\n</details>",
                "<details><summary>Sandbox output</summary>\n\n"
                "```text\nE   assert 30 == 31\n```\n\n</details>",
                "**Suggested fix** (verified: the reproducing test passes with it applied). "
                "The end is inclusive.\n\n```suggestion\n    while day <= end:\n```",
            ]
        )
        assert payload == {
            "commit_id": "a" * 40,
            "event": "COMMENT",
            "body": f"{MARKER}\n**Witness verified 1 bug** in `aaaaaaaa`: each one comes with a "
            "test that fails on this PR and passes on the base branch.",
            "comments": [
                {
                    "path": "src/invoicing/dates.py",
                    "line": 16,
                    "side": "RIGHT",
                    "body": expected_body,
                }
            ],
        }

    def test_without_a_fix_the_comment_spans_the_claimed_lines(self) -> None:
        payload = review_payload(
            result(finding()),
            commentable={"src/invoicing/dates.py": [LineRange(10, 17)]},
            read_file=read_head,
        )
        assert payload is not None
        [comment] = payload["comments"]
        assert (comment["start_line"], comment["line"]) == (15, 17)  # clipped to the diff
        assert comment["start_side"] == comment["side"] == "RIGHT"

    def test_finding_outside_the_diff_goes_into_the_body(self) -> None:
        payload = review_payload(
            result(finding(fix=FIX)),
            commentable={"src/invoicing/dates.py": [LineRange(1, 5)]},
            read_file=read_head,
        )
        assert payload is not None
        assert payload["comments"] == []
        assert "### Findings outside the diff" in payload["body"]
        assert "```diff\n--- src/invoicing/dates.py" in payload["body"]

    def test_nothing_verified_means_no_review(self) -> None:
        assert (
            review_payload(result(finding(Status.REJECTED)), commentable={}, read_file=read_head)
            is None
        )

    def test_check_run_output(self) -> None:
        out = check_run_output(result(finding(), finding(Status.REJECTED, cid="C2")))
        assert (out.conclusion, out.title) == ("failure", "1 verified bug")
        assert "| Verified bugs (reported) | 1 |" in out.summary
        assert "| Rejected hypotheses (repro passed) | 1 |" in out.summary
        clean = check_run_output(result(finding(Status.REJECTED)))
        assert (clean.conclusion, clean.title) == ("success", "No verified bugs")

    def test_fence_outgrows_backticks_in_content(self) -> None:
        assert fenced("a ``` b", "text") == "````text\na ``` b\n````"


def mock_github(router: respx.Router) -> dict[str, respx.Route]:
    return {
        "token": router.post(f"{API}/app/installations/42/access_tokens").respond(
            201, json={"token": "ghs_test", "expires_at": "2099-01-01T00:00:00Z"}
        ),
        "check_create": router.post(f"{API}/repos/acme/billing/check-runs").respond(
            201, json={"id": 555}
        ),
        "check_update": router.patch(f"{API}/repos/acme/billing/check-runs/555").respond(
            200, json={"id": 555}
        ),
        "review": router.post(f"{API}/repos/acme/billing/pulls/7/reviews").mock(
            side_effect=lambda request: httpx.Response(
                200, json={"id": 900 + router.calls.call_count}
            )
        ),
        "review_comments": router.get(
            url__regex=rf"{API}/repos/acme/billing/pulls/7/reviews/\d+/comments.*"
        ).respond(200, json=[{"id": 1, "node_id": "PRRC_one"}]),
        "review_update": router.put(
            url__regex=rf"{API}/repos/acme/billing/pulls/7/reviews/\d+"
        ).respond(200, json={"id": 0}),
        "graphql": router.post(f"{API}/graphql").respond(
            200, json={"data": {"minimizeComment": {"clientMutationId": None}}}
        ),
    }


def body(route: respx.Route, index: int = -1) -> Any:
    return json.loads(route.calls[index].request.content)


@respx.mock(assert_all_mocked=True, assert_all_called=False)
def test_gateway_calls(respx_mock: respx.Router, rsa_private_key: str) -> None:
    routes = mock_github(respx_mock)
    gateway = GitHubGateway(123, rsa_private_key, 42)

    assert gateway.clone_token() == "ghs_test"
    assert gateway.start_check_run("acme/billing", "a" * 40, "Witness") == 555
    started = body(routes["check_create"])
    assert (started["head_sha"], started["status"], started["name"]) == (
        "a" * 40,
        "in_progress",
        "Witness",
    )

    payload = {"commit_id": "a" * 40, "event": "COMMENT", "body": "b", "comments": []}
    review_id, node_ids = gateway.post_review("acme/billing", 7, payload)
    assert body(routes["review"]) == payload
    assert node_ids == ["PRRC_one"]

    gateway.supersede_review("acme/billing", 7, review_id, ["PRRC_one"], "c" * 40)
    assert "Superseded by the review of `cccccccc`" in body(routes["review_update"])["body"]
    minimize = body(routes["graphql"])
    assert minimize["variables"] == {"id": "PRRC_one"}
    assert "classifier: OUTDATED" in minimize["query"]

    # Installation calls authenticate with the installation token, minted via the App JWT.
    assert routes["check_create"].calls[0].request.headers["authorization"] == "token ghs_test"
    assert routes["token"].calls[0].request.headers["authorization"].startswith("Bearer ey")


@pytest.mark.postgres
class TestWorker:
    def make(
        self, pg_dsn: str, demo_repo: Path, rsa_private_key: str, reviews: list[ReviewResult]
    ) -> tuple[Store, Worker]:
        store = Store(pg_dsn)
        store.migrate()

        def fetch(job: Job, token: str, dest: Path) -> Path:
            assert token == "ghs_test"
            subprocess.run(["git", "clone", "--quiet", str(demo_repo), str(dest)], check=True)
            return dest

        def review(repo: Path, base: str, head: str) -> ReviewResult:
            return reviews.pop(0).model_copy(update={"base": base, "head": head})

        worker = Worker(
            store, lambda iid: GitHubGateway(123, rsa_private_key, iid), review, fetch=fetch
        )
        return store, worker

    def enqueue(self, store: Store, demo_repo: Path, delivery: str) -> None:
        base, head = git.resolve(demo_repo, "main"), git.resolve(demo_repo, "float-discounts")
        store.accept_delivery(delivery, "pull_request", JobSpec(42, "acme/billing", 7, base, head))

    @respx.mock(assert_all_mocked=True, assert_all_called=False)
    def test_review_then_synchronize_supersedes(
        self, respx_mock: respx.Router, pg_dsn: str, demo_repo: Path, rsa_private_key: str
    ) -> None:
        routes = mock_github(respx_mock)
        store, worker = self.make(
            pg_dsn,
            demo_repo,
            rsa_private_key,
            [result(finding(fix=FIX)), result(finding(Status.REJECTED))],
        )

        self.enqueue(store, demo_repo, "d1")
        assert worker.run_once()
        review = body(routes["review"])
        [comment] = review["comments"]
        assert (comment["path"], comment["line"]) == ("src/invoicing/dates.py", 16)
        assert "```suggestion\n    while day <= end:\n```" in comment["body"]
        completed = body(routes["check_update"])
        assert (completed["status"], completed["conclusion"]) == ("completed", "failure")
        assert completed["output"]["title"] == "1 verified bug"

        # A new push: nothing verified this time, so no new review, but the old one is
        # marked superseded and its inline comment collapsed.
        self.enqueue(store, demo_repo, "d2")
        assert worker.run_once()
        assert routes["review"].call_count == 1
        assert body(routes["graphql"])["variables"] == {"id": "PRRC_one"}
        assert routes["review_update"].call_count == 1
        assert body(routes["check_update"])["conclusion"] == "success"
        assert store.active_reviews("acme/billing", 7)[0].review_id is None
        assert not worker.run_once()

    @respx.mock(assert_all_mocked=True, assert_all_called=False)
    def test_failure_completes_the_check_run_and_marks_the_job(
        self, respx_mock: respx.Router, pg_dsn: str, demo_repo: Path, rsa_private_key: str
    ) -> None:
        routes = mock_github(respx_mock)
        store, worker = self.make(pg_dsn, demo_repo, rsa_private_key, [])  # review() will fail
        self.enqueue(store, demo_repo, "d1")
        assert worker.run_once()
        completed = body(routes["check_update"])
        assert completed["conclusion"] == "neutral"
        assert completed["output"]["title"] == "Witness could not finish the review"
        assert store.job_status(1) == "failed"
