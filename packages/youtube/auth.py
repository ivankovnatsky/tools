"""OAuth and secret-reading helpers for youtube."""

import http.server
import json
import os
import secrets
import time
import urllib.parse
import webbrowser

import click
import requests
from constants import AUTH_URI, CONSENT_TIMEOUT, TOKEN_URI, USER_AGENT


def read_secret_file(path: str) -> str | None:
    """Read and strip a secret file's contents, or None if missing.

    Distinguishes FileNotFoundError (silent None) from other OSError
    (re-raise) so a permissions error isn't masked as "not configured".
    """
    try:
        return open(os.path.expanduser(path)).read().strip()
    except FileNotFoundError:
        return None


def client_credentials(client_secret_file: str) -> tuple[str, str]:
    """Return (client_id, client_secret) from a Google OAuth client JSON."""
    raw = read_secret_file(client_secret_file)
    if not raw:
        raise click.ClickException(
            f"missing OAuth client (expected file at {client_secret_file}).\n"
            "Create an OAuth client of type 'Desktop app' in Google Cloud "
            "(APIs & Services > Credentials), enable the YouTube Data API v3, "
            "and store the downloaded JSON in sops as youtube/clientSecret."
        )
    data = json.loads(raw)
    node = data.get("installed") or data.get("web") or data
    return node["client_id"], node["client_secret"]


def _post_token(payload: dict[str, str]) -> dict:
    response = requests.post(
        TOKEN_URI, data=payload, headers={"User-Agent": USER_AGENT}, timeout=30
    )
    if response.status_code != 200:
        raise click.ClickException(
            f"token request failed ({response.status_code}): {response.text}"
        )
    return response.json()


def consent_flow(client_secret_file: str, scope: str, token_path: str) -> dict:
    """Run the loopback OAuth flow: open a browser, catch the redirect, save the token."""
    client_id, client_secret = client_credentials(client_secret_file)
    captured: dict[str, str] = {}
    state = secrets.token_urlsafe(16)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            captured.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            ok = "code" in captured and captured.get("state") == state
            self.wfile.write(
                b"Authorized. You can close this tab."
                if ok
                else b"Authorization failed. Check the terminal."
            )

        def log_message(self, *args: object) -> None:
            pass

    # Desktop-app clients may redirect to loopback on any port.
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}"

    url = f"{AUTH_URI}?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    click.echo(f"Opening browser for consent:\n{url}\n", err=True)
    webbrowser.open(url)
    # Stray requests (favicon, prefetch) must not consume the callback.
    server.timeout = CONSENT_TIMEOUT
    deadline = time.monotonic() + CONSENT_TIMEOUT
    while "code" not in captured and "error" not in captured:
        if time.monotonic() >= deadline:
            server.server_close()
            raise click.ClickException(
                f"no OAuth redirect within {CONSENT_TIMEOUT}s; aborting."
            )
        server.handle_request()
    server.server_close()

    if captured.get("state") != state:
        raise click.ClickException("OAuth state mismatch; aborting.")
    if "code" not in captured:
        raise click.ClickException(
            f"no authorization code returned: {captured.get('error', 'unknown error')}"
        )

    token = _post_token(
        {
            "code": captured["code"],
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    )
    os.makedirs(os.path.dirname(token_path), exist_ok=True)
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(token, indent=2) + "\n")
    return token


def access_token(client_secret_file: str, scope: str, token_path: str) -> str:
    """Exchange the saved refresh token for an access token, running consent if absent."""
    if not os.path.exists(token_path):
        return consent_flow(client_secret_file, scope, token_path)["access_token"]

    client_id, client_secret = client_credentials(client_secret_file)
    with open(token_path) as f:
        saved = json.load(f)
    if "refresh_token" not in saved:
        raise click.ClickException(
            f"{token_path} has no refresh_token; run `youtube auth login` to replace it."
        )
    try:
        refreshed = _post_token(
            {
                "refresh_token": saved["refresh_token"],
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
            }
        )
    except click.ClickException as e:
        if "invalid_grant" in e.message:
            raise click.ClickException(
                f"refresh token in {token_path} was revoked or expired; "
                "run `youtube auth login` to replace it."
            )
        raise
    return refreshed["access_token"]
