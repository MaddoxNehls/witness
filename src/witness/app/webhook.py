"""The webhook endpoint: verify, dedupe, enqueue. All real work happens in the worker."""

from __future__ import annotations

import json
from typing import Any, Protocol

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from witness.app.signature import verify_signature
from witness.app.store import JobSpec, Outcome

REVIEWED_ACTIONS = frozenset({"opened", "synchronize", "reopened"})


class DeliverySink(Protocol):
    def accept_delivery(self, delivery_id: str, event: str, job: JobSpec | None) -> Outcome: ...


def job_for(event: str, payload: dict[str, Any]) -> JobSpec | None:
    """The review job a webhook asks for, or None for everything else."""
    if event != "pull_request" or payload.get("action") not in REVIEWED_ACTIONS:
        return None
    pr = payload["pull_request"]
    if pr.get("state") != "open":
        return None
    return JobSpec(
        installation_id=int(payload["installation"]["id"]),
        repo=str(payload["repository"]["full_name"]),
        pr_number=int(pr["number"]),
        base_sha=str(pr["base"]["sha"]),
        head_sha=str(pr["head"]["sha"]),
    )


def create_app(webhook_secret: str, sink: DeliverySink) -> FastAPI:
    app = FastAPI(title="Witness", docs_url=None, redoc_url=None)
    secret = webhook_secret.encode()

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/github", status_code=202)
    async def github_webhook(
        request: Request,
        x_github_event: str = Header(),
        x_github_delivery: str = Header(),
        x_hub_signature_256: str | None = Header(default=None),
    ) -> JSONResponse:
        body = await request.body()
        # Verify before parsing: nothing from an unsigned request is ever interpreted.
        if not verify_signature(secret, body, x_hub_signature_256):
            raise HTTPException(status_code=401, detail="invalid signature")
        try:
            payload = json.loads(body)
            job = job_for(x_github_event, payload)
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"malformed payload: {exc}") from exc
        outcome = await run_in_threadpool(
            sink.accept_delivery, x_github_delivery, x_github_event, job
        )
        return JSONResponse({"status": outcome}, status_code=200 if outcome == "duplicate" else 202)

    return app
