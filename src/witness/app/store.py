"""Postgres: webhook deliveries, the job queue, and review history.

Why a Postgres table rather than arq/Redis: Postgres is already required for review
history, so the queue adds no infrastructure. Recording the delivery (dedupe),
superseding older jobs for the same PR, and enqueueing the new one happen in a
single transaction. ``FOR UPDATE SKIP LOCKED`` lets any number of workers claim jobs
without blocking each other, and a review takes minutes, so queue throughput never
matters.

Delivery is at least once: a worker that dies mid-job leaves it ``running``, and it
becomes claimable again once its lease expires.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id  text PRIMARY KEY,
    event        text NOT NULL,
    received_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS jobs (
    id               bigserial PRIMARY KEY,
    delivery_id      text NOT NULL REFERENCES deliveries,
    installation_id  bigint NOT NULL,
    repo             text NOT NULL,
    pr_number        integer NOT NULL,
    base_sha         text NOT NULL,
    head_sha         text NOT NULL,
    status           text NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'running', 'done', 'failed', 'superseded')),
    attempts         integer NOT NULL DEFAULT 0,
    error            text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    started_at       timestamptz,
    finished_at      timestamptz
);
CREATE INDEX IF NOT EXISTS jobs_claimable ON jobs (id) WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS jobs_by_pr ON jobs (repo, pr_number);

CREATE TABLE IF NOT EXISTS reviews (
    id                bigserial PRIMARY KEY,
    job_id            bigint NOT NULL REFERENCES jobs,
    repo              text NOT NULL,
    pr_number         integer NOT NULL,
    head_sha          text NOT NULL,
    check_run_id      bigint,
    review_id         bigint,
    comment_node_ids  text[] NOT NULL DEFAULT '{}',
    verified          integer NOT NULL,
    hypothesized      integer NOT NULL,
    result            jsonb NOT NULL,
    superseded_by     bigint REFERENCES reviews,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS reviews_by_pr ON reviews (repo, pr_number);
"""

Outcome = Literal["queued", "ignored", "duplicate"]


@dataclass(frozen=True)
class JobSpec:
    installation_id: int
    repo: str  # owner/name
    pr_number: int
    base_sha: str
    head_sha: str


@dataclass(frozen=True)
class Job(JobSpec):
    id: int
    attempts: int


@dataclass(frozen=True)
class PostedReview:
    id: int
    review_id: int | None
    comment_node_ids: list[str]


class Store:
    def __init__(self, dsn: str, *, lease: timedelta = timedelta(minutes=30)) -> None:
        self._dsn = dsn
        self._lease = lease

    @contextmanager
    def _tx(self) -> Iterator[psycopg.Cursor[dict[str, Any]]]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
            yield cur  # the connection context commits on success, rolls back on error

    def migrate(self) -> None:
        with self._tx() as cur:
            cur.execute(SCHEMA)

    def accept_delivery(self, delivery_id: str, event: str, job: JobSpec | None) -> Outcome:
        """Record a webhook delivery and, for a reviewable event, enqueue its job.

        Queued jobs for the same PR are superseded: only the newest push is reviewed.
        """
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO deliveries (delivery_id, event) VALUES (%s, %s)"
                " ON CONFLICT DO NOTHING RETURNING delivery_id",
                (delivery_id, event),
            )
            if cur.fetchone() is None:
                return "duplicate"
            if job is None:
                return "ignored"
            cur.execute(
                "UPDATE jobs SET status = 'superseded', finished_at = now()"
                " WHERE repo = %s AND pr_number = %s AND status = 'queued'",
                (job.repo, job.pr_number),
            )
            cur.execute(
                "INSERT INTO jobs"
                " (delivery_id, installation_id, repo, pr_number, base_sha, head_sha)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    delivery_id,
                    job.installation_id,
                    job.repo,
                    job.pr_number,
                    job.base_sha,
                    job.head_sha,
                ),
            )
            return "queued"

    def claim_next(self) -> Job | None:
        """Take the oldest queued job, or a running one whose lease expired."""
        with self._tx() as cur:
            cur.execute(
                """
                UPDATE jobs SET status = 'running', attempts = attempts + 1, started_at = now()
                WHERE id = (
                    SELECT id FROM jobs
                    WHERE status = 'queued'
                       OR (status = 'running' AND started_at < now() - %s)
                    ORDER BY id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING id, installation_id, repo, pr_number, base_sha, head_sha, attempts
                """,
                (self._lease,),
            )
            row = cur.fetchone()
        return Job(**row) if row else None

    def is_superseded(self, job: Job) -> bool:
        """A newer push to the same PR has been queued or reviewed since this job started."""
        with self._tx() as cur:
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM jobs WHERE repo = %s AND pr_number = %s"
                " AND id > %s AND status <> 'superseded') AS newer",
                (job.repo, job.pr_number, job.id),
            )
            row = cur.fetchone()
        return bool(row and row["newer"])

    def finish(self, job_id: int, status: str, error: str | None = None) -> None:
        with self._tx() as cur:
            cur.execute(
                "UPDATE jobs SET status = %s, error = %s, finished_at = now() WHERE id = %s",
                (status, error, job_id),
            )

    def job_status(self, job_id: int) -> str | None:
        with self._tx() as cur:
            cur.execute("SELECT status FROM jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()
        return str(row["status"]) if row else None

    def active_reviews(self, repo: str, pr_number: int) -> list[PostedReview]:
        """Posted reviews on this PR that have not been superseded yet."""
        with self._tx() as cur:
            cur.execute(
                "SELECT id, review_id, comment_node_ids FROM reviews"
                " WHERE repo = %s AND pr_number = %s AND superseded_by IS NULL ORDER BY id",
                (repo, pr_number),
            )
            return [PostedReview(**row) for row in cur.fetchall()]

    def save_review(
        self,
        job: Job,
        *,
        check_run_id: int | None,
        review_id: int | None,
        comment_node_ids: list[str],
        verified: int,
        hypothesized: int,
        result: dict[str, Any],
        supersedes: list[int],
    ) -> int:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO reviews (job_id, repo, pr_number, head_sha, check_run_id, review_id,"
                " comment_node_ids, verified, hypothesized, result)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    job.id,
                    job.repo,
                    job.pr_number,
                    job.head_sha,
                    check_run_id,
                    review_id,
                    comment_node_ids,
                    verified,
                    hypothesized,
                    Jsonb(result),
                ),
            )
            row = cur.fetchone()
            assert row is not None
            new_id = int(row["id"])
            if supersedes:
                cur.execute(
                    "UPDATE reviews SET superseded_by = %s WHERE id = ANY(%s)", (new_id, supersedes)
                )
            return new_id
