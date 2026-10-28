"""The worker: claim a job, fetch the PR, review it, and report back to GitHub.

Per job: start an ``in_progress`` check run, fetch exactly the base and head commits
with an installation token, run the review, then (unless a newer push superseded the
job meanwhile) post one PR review with inline comments for verified findings, mark
earlier Witness reviews on the PR as superseded, and complete the check run.
"""

from __future__ import annotations

import base64
import logging
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from witness import git
from witness.app.render import CheckOutput, check_run_output, review_payload
from witness.app.store import Job, Store
from witness.models import ReviewResult, Status

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3

ReviewFn = Callable[[Path, str, str], ReviewResult]
FetchFn = Callable[[Job, str, Path], Path]


class Gateway(Protocol):
    def clone_token(self) -> str: ...
    def start_check_run(self, repo: str, head_sha: str, name: str) -> int: ...
    def complete_check_run(self, repo: str, check_run_id: int, output: CheckOutput) -> None: ...
    def post_review(
        self, repo: str, pr_number: int, payload: dict[str, object]
    ) -> tuple[int, list[str]]: ...
    def supersede_review(
        self, repo: str, pr_number: int, review_id: int, comment_node_ids: list[str], new_head: str
    ) -> None: ...


def fetch_pr(job: Job, token: str, dest: Path) -> Path:
    """Fetch only the two commits under review, authenticating with an HTTP header.

    The token never lands in the remote URL or ``.git/config``. GitHub serves fork PR
    heads from the base repository, so one remote suffices.
    """
    url = f"https://github.com/{job.repo}.git"
    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    subprocess.run(["git", "init", "--quiet", str(dest)], check=True)
    subprocess.run(
        [
            "git", "-C", str(dest), "-c", f"http.extraHeader=Authorization: Basic {auth}",
            "fetch", "--quiet", "--no-tags", "--depth=1", url, job.base_sha, job.head_sha,
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return dest


class Worker:
    def __init__(
        self,
        store: Store,
        gateway_for: Callable[[int], Gateway],
        review: ReviewFn,
        *,
        fetch: FetchFn = fetch_pr,
        check_name: str = "Witness",
    ) -> None:
        self._store = store
        self._gateway_for = gateway_for
        self._review = review
        self._fetch = fetch
        self._check_name = check_name

    def run_forever(self, poll_seconds: float = 2.0) -> None:
        while True:
            if not self.run_once():
                time.sleep(poll_seconds)

    def run_once(self) -> bool:
        """Process one job if there is one. Returns whether a job was claimed."""
        job = self._store.claim_next()
        if job is None:
            return False
        if job.attempts > MAX_ATTEMPTS:
            self._store.finish(job.id, "failed", f"gave up after {MAX_ATTEMPTS} attempts")
            return True
        logger.info("job %s: %s#%s at %s", job.id, job.repo, job.pr_number, job.head_sha[:8])
        self._process(job, self._gateway_for(job.installation_id))
        return True

    def _process(self, job: Job, gateway: Gateway) -> None:
        check_run_id = gateway.start_check_run(job.repo, job.head_sha, self._check_name)
        try:
            with tempfile.TemporaryDirectory(prefix="witness-pr-") as tmp:
                repo = self._fetch(job, gateway.clone_token(), Path(tmp) / "repo")
                result = self._review(repo, job.base_sha, job.head_sha)
                if self._store.is_superseded(job):
                    gateway.complete_check_run(
                        job.repo,
                        check_run_id,
                        CheckOutput("neutral", "Superseded by a newer push", "Not posted.", ""),
                    )
                    self._store.finish(job.id, "superseded")
                    return
                self._publish(job, gateway, repo, result, check_run_id)
        except Exception as exc:
            logger.exception("job %s failed", job.id)
            message = f"{type(exc).__name__}: {exc}"[:1000]
            gateway.complete_check_run(
                job.repo,
                check_run_id,
                CheckOutput("neutral", "Witness could not finish the review", message, ""),
            )
            self._store.finish(job.id, "failed", message)

    def _publish(
        self, job: Job, gateway: Gateway, repo: Path, result: ReviewResult, check_run_id: int
    ) -> None:
        payload = review_payload(
            result,
            commentable=git.visible_lines(repo, job.base_sha, job.head_sha),
            read_file=lambda path: git.show_file(repo, job.head_sha, path),
        )
        review_id, node_ids = (
            gateway.post_review(job.repo, job.pr_number, payload) if payload else (None, [])
        )
        previous = self._store.active_reviews(job.repo, job.pr_number)
        for earlier in previous:
            if earlier.review_id is not None:
                gateway.supersede_review(
                    job.repo,
                    job.pr_number,
                    earlier.review_id,
                    earlier.comment_node_ids,
                    job.head_sha,
                )
        gateway.complete_check_run(job.repo, check_run_id, check_run_output(result))
        self._store.save_review(
            job,
            check_run_id=check_run_id,
            review_id=review_id,
            comment_node_ids=node_ids,
            verified=len(result.with_status(Status.VERIFIED)),
            hypothesized=len(result.findings),
            result=result.model_dump(mode="json"),
            supersedes=[earlier.id for earlier in previous],
        )
        self._store.finish(job.id, "done")
