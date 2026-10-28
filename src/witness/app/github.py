"""The few GitHub API calls the worker makes, as the App installation (githubkit auth)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from githubkit import AppAuthStrategy, AppInstallationAuthStrategy, GitHub

from witness.app.render import CheckOutput, superseded_body

MINIMIZE = """
mutation($id: ID!) {
  minimizeComment(input: {subjectId: $id, classifier: OUTDATED}) { clientMutationId }
}
"""


class GitHubGateway:
    """One installation's view of GitHub. ``base_url`` exists for tests and GHES.

    githubkit handles authentication (App JWT, then a cached installation token). The
    calls go through its generic ``request`` so each payload is sent exactly as built
    and tested, and only the few response fields used are read.
    """

    def __init__(
        self,
        app_id: int,
        private_key: str,
        installation_id: int,
        *,
        base_url: str | None = None,
    ) -> None:
        self._app = GitHub(AppAuthStrategy(app_id, private_key), base_url=base_url)
        self._gh = GitHub(
            AppInstallationAuthStrategy(app_id, private_key, installation_id), base_url=base_url
        )
        self._installation_id = installation_id

    def _call(self, method: str, url: str, payload: Any = None, *, as_app: bool = False) -> Any:
        client = self._app if as_app else self._gh
        return client.request(method, url, json=payload).json()

    def clone_token(self) -> str:
        """A short-lived installation token for ``git fetch``."""
        url = f"/app/installations/{self._installation_id}/access_tokens"
        return str(self._call("POST", url, as_app=True)["token"])

    def start_check_run(self, repo: str, head_sha: str, name: str) -> int:
        created = self._call(
            "POST",
            f"/repos/{repo}/check-runs",
            {
                "name": name,
                "head_sha": head_sha,
                "status": "in_progress",
                "started_at": _now(),
                "output": {
                    "title": "Reviewing",
                    "summary": "Hypothesizing bugs and trying to reproduce each one in a sandbox.",
                },
            },
        )
        return int(created["id"])

    def complete_check_run(self, repo: str, check_run_id: int, output: CheckOutput) -> None:
        self._call(
            "PATCH",
            f"/repos/{repo}/check-runs/{check_run_id}",
            {
                "status": "completed",
                "conclusion": output.conclusion,
                "completed_at": _now(),
                "output": {"title": output.title, "summary": output.summary, "text": output.text},
            },
        )

    def post_review(
        self, repo: str, pr_number: int, payload: dict[str, Any]
    ) -> tuple[int, list[str]]:
        """Create the review; return its id and the node ids of its inline comments."""
        review_id = int(
            self._call("POST", f"/repos/{repo}/pulls/{pr_number}/reviews", payload)["id"]
        )
        comments = self._call(
            "GET", f"/repos/{repo}/pulls/{pr_number}/reviews/{review_id}/comments?per_page=100"
        )
        return review_id, [str(comment["node_id"]) for comment in comments]

    def supersede_review(
        self, repo: str, pr_number: int, review_id: int, comment_node_ids: list[str], new_head: str
    ) -> None:
        """Mark an earlier review as superseded and collapse its comments as outdated."""
        self._call(
            "PUT",
            f"/repos/{repo}/pulls/{pr_number}/reviews/{review_id}",
            {"body": superseded_body(new_head)},
        )
        for node_id in comment_node_ids:
            self._gh.graphql.request(MINIMIZE, {"id": node_id})


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
