"""Webhook intake: signatures, routing, dedupe and the Postgres job queue."""

from __future__ import annotations

import json
import threading
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from witness.app.signature import sign, verify_signature
from witness.app.store import JobSpec, Store
from witness.app.webhook import create_app, job_for

SECRET = "s3cret"


def pr_event(action: str = "opened", number: int = 7, head: str = "h" * 40) -> dict[str, Any]:
    return {
        "action": action,
        "installation": {"id": 42},
        "repository": {"full_name": "acme/billing"},
        "pull_request": {
            "number": number,
            "state": "open",
            "base": {"sha": "b" * 40},
            "head": {"sha": head},
        },
    }


class TestSignature:
    BODY = b'{"zen": "Design for failure."}'

    def test_valid(self) -> None:
        assert verify_signature(b"k", self.BODY, sign(b"k", self.BODY))

    @pytest.mark.parametrize(
        "header",
        [
            None,
            "",
            "sha256=" + "0" * 64,
            "sha1=deadbeef",
            sign(b"other-key", BODY),
            sign(b"k", BODY + b" "),
        ],
    )
    def test_rejected(self, header: str | None) -> None:
        assert not verify_signature(b"k", self.BODY, header)

    def test_empty_secret_never_verifies(self) -> None:
        assert not verify_signature(b"", self.BODY, sign(b"", self.BODY))


class TestJobFor:
    def test_reviewed_actions(self) -> None:
        for action in ("opened", "synchronize", "reopened"):
            job = job_for("pull_request", pr_event(action))
            assert job == JobSpec(42, "acme/billing", 7, "b" * 40, "h" * 40)

    def test_everything_else_is_ignored(self) -> None:
        assert job_for("pull_request", pr_event("closed")) is None
        assert job_for("pull_request", pr_event("labeled")) is None
        assert job_for("issues", pr_event()) is None
        closed = pr_event("synchronize")
        closed["pull_request"]["state"] = "closed"
        assert job_for("pull_request", closed) is None


class RecordingSink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, JobSpec | None]] = []

    def accept_delivery(self, delivery_id: str, event: str, job: JobSpec | None) -> Any:
        self.calls.append((delivery_id, event, job))
        return "queued" if job else "ignored"


def post(client: TestClient, payload: dict[str, Any], *, delivery: str = "d-1", **kw: Any) -> Any:
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": kw.get("event", "pull_request"),
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": kw.get("signature", sign(SECRET.encode(), body)),
        "Content-Type": "application/json",
    }
    return client.post("/webhooks/github", content=body, headers=headers)


class TestEndpoint:
    def test_bad_signature_is_rejected_before_anything_is_recorded(self) -> None:
        sink = RecordingSink()
        response = post(TestClient(create_app(SECRET, sink)), pr_event(), signature="sha256=00")
        assert response.status_code == 401
        assert sink.calls == []

    def test_pull_request_is_enqueued(self) -> None:
        sink = RecordingSink()
        response = post(TestClient(create_app(SECRET, sink)), pr_event())
        assert (response.status_code, response.json()) == (202, {"status": "queued"})
        assert sink.calls == [
            ("d-1", "pull_request", JobSpec(42, "acme/billing", 7, "b" * 40, "h" * 40))
        ]

    def test_malformed_payload(self) -> None:
        sink = RecordingSink()
        response = post(TestClient(create_app(SECRET, sink)), {"action": "opened"})
        assert response.status_code == 400


@pytest.mark.postgres
class TestStore:
    def test_dedupe_by_delivery_id(self, pg_dsn: str) -> None:
        store = Store(pg_dsn)
        store.migrate()
        client = TestClient(create_app(SECRET, store))
        first = post(client, pr_event(), delivery="abc")
        again = post(client, pr_event(), delivery="abc")
        assert (first.status_code, first.json()["status"]) == (202, "queued")
        assert (again.status_code, again.json()["status"]) == (200, "duplicate")
        assert post(client, {"zen": "hi"}, delivery="ping-1", event="ping").json() == {
            "status": "ignored"
        }
        assert _count(pg_dsn, "jobs") == 1

    def test_job_lifecycle(self, pg_dsn: str) -> None:
        store = Store(pg_dsn)
        store.migrate()
        spec = JobSpec(42, "acme/billing", 7, "b" * 40, "h" * 40)
        assert store.accept_delivery("d1", "pull_request", spec) == "queued"

        job = store.claim_next()
        assert job is not None
        assert (job.repo, job.pr_number, job.attempts) == ("acme/billing", 7, 1)
        assert store.job_status(job.id) == "running"
        assert store.claim_next() is None  # running jobs are not handed out twice

        store.finish(job.id, "done")
        assert store.job_status(job.id) == "done"
        assert store.claim_next() is None

    def test_newer_push_supersedes_queued_and_running_jobs(self, pg_dsn: str) -> None:
        store = Store(pg_dsn)
        store.migrate()
        old = JobSpec(42, "acme/billing", 7, "b" * 40, "1" * 40)
        store.accept_delivery("d1", "pull_request", old)
        running = store.claim_next()
        assert running is not None
        store.accept_delivery(
            "d2", "pull_request", JobSpec(42, "acme/billing", 7, "b" * 40, "2" * 40)
        )
        store.accept_delivery(
            "d3", "pull_request", JobSpec(42, "acme/billing", 7, "b" * 40, "3" * 40)
        )

        assert store.is_superseded(running)  # the worker will not post its stale result
        newest = store.claim_next()
        assert newest is not None
        assert newest.head_sha == "3" * 40  # the queued "2" job was superseded at enqueue
        assert store.job_status(newest.id - 1) == "superseded"
        assert store.claim_next() is None

    def test_expired_lease_is_reclaimed(self, pg_dsn: str) -> None:
        store = Store(pg_dsn, lease=timedelta(seconds=0))
        store.migrate()
        store.accept_delivery("d1", "pull_request", JobSpec(1, "a/b", 1, "b" * 40, "h" * 40))
        first = store.claim_next()
        second = store.claim_next()  # the first worker "died": its lease has already expired
        assert first is not None
        assert second is not None
        assert (second.id, second.attempts) == (first.id, 2)

    def test_skip_locked_hands_each_job_to_one_worker(self, pg_dsn: str) -> None:
        store = Store(pg_dsn)
        store.migrate()
        for n in range(20):
            spec = JobSpec(1, "acme/repo", n, "b" * 40, f"{n:040d}")
            store.accept_delivery(f"d{n}", "pull_request", spec)
        claimed: list[int] = []
        lock = threading.Lock()

        def drain() -> None:
            while (job := store.claim_next()) is not None:
                with lock:
                    claimed.append(job.id)

        threads = [threading.Thread(target=drain) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(claimed) == sorted(set(claimed))
        assert len(claimed) == 20


def _count(dsn: str, table: str) -> int:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])
