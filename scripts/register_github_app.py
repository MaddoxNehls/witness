"""Register the Witness GitHub App from github-app-manifest.json (one-click manifest flow).

    uv run python scripts/register_github_app.py --webhook-url https://<public-host>/webhooks/github
    uv run python scripts/register_github_app.py --webhook-url ... --org my-org

Opens a browser at a local page that submits the manifest to GitHub. After you confirm,
GitHub redirects back here; the script exchanges the code for the App's credentials and
writes .env.github-app plus the private key (both gitignored). Nothing is registered
until you click "Create GitHub App" on github.com.
"""

from __future__ import annotations

import argparse
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from witness.app.registration import exchange_code, form_page, load_manifest, write_credentials

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--webhook-url", required=True, help="public URL of /webhooks/github")
    parser.add_argument("--org", help="register under this organization instead of your user")
    parser.add_argument("--port", type=int, default=3555)
    args = parser.parse_args()

    state = secrets.token_urlsafe(16)
    redirect = f"http://localhost:{args.port}/callback"
    manifest = load_manifest(
        ROOT / "github-app-manifest.json", webhook_url=args.webhook_url, redirect_url=redirect
    )
    page = form_page(manifest, state, args.org).encode()
    done: list[Path] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if url.path == "/":
                self._reply(200, page)
            elif url.path == "/callback" and query.get("state") == [state] and "code" in query:
                env = write_credentials(exchange_code(query["code"][0]), ROOT)
                done.append(env)
                self._reply(200, f"Registered. Credentials written to {env}.".encode())
            else:
                self._reply(400, b"Unexpected request or state mismatch.")

        def _reply(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", args.port), Handler)
    webbrowser.open(f"http://localhost:{args.port}/")
    print(f"Waiting for GitHub at {redirect} (Ctrl+C to abort)...")
    while not done:
        server.handle_request()
    print(f"Done. Load the settings with: set -a; . {done[0]}; set +a")


if __name__ == "__main__":
    main()
