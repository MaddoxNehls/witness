"""The App-manifest registration flow, against a mocked GitHub."""

from __future__ import annotations

import html
import json
from pathlib import Path

import respx
from helpers import DEMO

from witness.app.registration import exchange_code, form_page, load_manifest, write_credentials
from witness.app.settings import AppSettings

MANIFEST = DEMO.parents[1] / "github-app-manifest.json"


def test_manifest_requests_least_privilege() -> None:
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["default_permissions"] == {
        "checks": "write",
        "contents": "read",
        "metadata": "read",
        "pull_requests": "write",
    }
    assert manifest["default_events"] == ["pull_request"]


def test_form_posts_the_manifest_with_state() -> None:
    manifest = load_manifest(
        MANIFEST,
        webhook_url="https://w.example/webhooks/github",
        redirect_url="http://localhost:1/cb",
    )
    page = form_page(manifest, state="xyz", org="acme")
    assert 'action="https://github.com/organizations/acme/settings/apps/new?state=xyz"' in page
    value = page.split('name="manifest" value="', 1)[1].split('"', 1)[0]
    assert (
        json.loads(html.unescape(value))["hook_attributes"]["url"]
        == "https://w.example/webhooks/github"
    )


@respx.mock
def test_code_exchange_writes_settings_the_app_can_load(tmp_path: Path) -> None:
    respx.post("https://api.github.com/app-manifests/c0de/conversions").respond(
        201,
        json={
            "id": 987,
            "slug": "witness-reviewer",
            "pem": "-----BEGIN-----\n",
            "webhook_secret": "whs",
        },
    )
    env_file = write_credentials(exchange_code("c0de"), tmp_path)
    env = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
    settings = AppSettings.from_env(env)
    assert (settings.app_id, settings.webhook_secret) == (987, "whs")
    assert settings.private_key == "-----BEGIN-----\n"
