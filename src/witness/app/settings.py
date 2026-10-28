"""Configuration for the webhook service and worker, read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATABASE_URL = "postgresql://witness:witness@localhost:5449/witness"


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class AppSettings:
    app_id: int
    private_key: str
    webhook_secret: str
    database_url: str = DEFAULT_DATABASE_URL
    backend: str = "anthropic"
    model: str | None = None
    check_name: str = "Witness"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> AppSettings:
        env = dict(os.environ if env is None else env)

        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise SettingsError(f"{name} is not set")
            return value

        key = env.get("WITNESS_GITHUB_PRIVATE_KEY", "").strip()
        if not key:
            key = Path(required("WITNESS_GITHUB_PRIVATE_KEY_PATH")).read_text(encoding="utf-8")
        return cls(
            app_id=int(required("WITNESS_GITHUB_APP_ID")),
            private_key=key,
            webhook_secret=required("WITNESS_GITHUB_WEBHOOK_SECRET"),
            database_url=env.get("WITNESS_DATABASE_URL", DEFAULT_DATABASE_URL),
            backend=env.get("WITNESS_BACKEND", "anthropic"),
            model=env.get("WITNESS_MODEL") or None,
        )
