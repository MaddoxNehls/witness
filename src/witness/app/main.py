"""Entry points: ``witness serve`` (webhooks) and ``witness worker`` (reviews)."""

from __future__ import annotations

from pathlib import Path

from witness.app.github import GitHubGateway
from witness.app.settings import AppSettings
from witness.app.store import Store
from witness.app.webhook import create_app
from witness.app.worker import Worker
from witness.llm import LLM, create_backend
from witness.models import ReviewResult
from witness.review import review


def serve(host: str, port: int) -> None:
    import uvicorn

    settings = AppSettings.from_env()
    store = Store(settings.database_url)
    store.migrate()
    uvicorn.run(create_app(settings.webhook_secret, store), host=host, port=port)


def work(poll_seconds: float = 2.0) -> None:
    settings = AppSettings.from_env()
    store = Store(settings.database_url)
    store.migrate()

    def run_review(repo: Path, base: str, head: str) -> ReviewResult:
        llm = LLM(create_backend(settings.backend, model=settings.model))
        return review(repo, base, head, llm)

    worker = Worker(
        store,
        lambda installation_id: GitHubGateway(
            settings.app_id, settings.private_key, installation_id
        ),
        run_review,
        check_name=settings.check_name,
    )
    worker.run_forever(poll_seconds)
