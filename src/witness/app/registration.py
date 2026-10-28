"""GitHub's App-manifest flow: register the App from ``github-app-manifest.json`` in one click.

1. A local page auto-submits the manifest to github.com (``/settings/apps/new``).
2. The owner confirms; GitHub redirects back to ``redirect_url`` with a one-time code.
3. The code is exchanged at ``POST /app-manifests/{code}/conversions`` for the App id,
   private key and webhook secret, which are written to local files.

The ``state`` parameter ties the callback to the page this process served.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

import httpx

API = "https://api.github.com"


def load_manifest(path: Path, *, webhook_url: str, redirect_url: str) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    manifest["hook_attributes"]["url"] = webhook_url
    manifest["redirect_url"] = redirect_url
    return manifest


def form_page(manifest: dict[str, Any], state: str, org: str | None = None) -> str:
    """An HTML page that posts the manifest to GitHub as soon as it loads."""
    owner = f"organizations/{org}/" if org else ""
    action = f"https://github.com/{owner}settings/apps/new?state={html.escape(state)}"
    value = html.escape(json.dumps(manifest))
    return (
        "<!doctype html><title>Register Witness</title>"
        f'<form id="f" method="post" action="{action}">'
        f'<input type="hidden" name="manifest" value="{value}">'
        "<button>Create the GitHub App</button></form>"
        "<script>document.getElementById('f').submit()</script>"
    )


def exchange_code(code: str, client: httpx.Client | None = None) -> dict[str, Any]:
    """Trade the one-time code for the App's credentials (no auth needed; the code is it)."""
    client = client or httpx.Client(base_url=API, timeout=30)
    response = client.post(
        f"/app-manifests/{code}/conversions",
        headers={"Accept": "application/vnd.github+json"},
    )
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    return data


def write_credentials(conversion: dict[str, Any], directory: Path) -> Path:
    """Write the private key and an env file the service and worker read. Returns the env file."""
    key_path = directory / f"{conversion['slug']}.private-key.pem"
    key_path.write_text(conversion["pem"], encoding="utf-8")
    env_path = directory / ".env.github-app"
    env_path.write_text(
        f"WITNESS_GITHUB_APP_ID={conversion['id']}\n"
        f"WITNESS_GITHUB_PRIVATE_KEY_PATH={key_path.resolve()}\n"
        f"WITNESS_GITHUB_WEBHOOK_SECRET={conversion['webhook_secret']}\n",
        encoding="utf-8",
    )
    for path in (key_path, env_path):
        path.chmod(0o600)
    return env_path
