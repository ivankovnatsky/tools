"""YouTube Data API v3 client."""

import os

import click
import requests
from auth import access_token, read_secret_file
from constants import (
    API_BASE,
    READ_SCOPE,
    READ_TOKEN_NAME,
    USER_AGENT,
    WRITE_SCOPE,
    WRITE_TOKEN_NAME,
)


class QuotaExceeded(Exception):
    pass


class YoutubeClient:
    """Bearer token for account data; API key (when given) for public data."""

    def __init__(
        self, token: str | None = None, api_key: str | None = None, timeout: int = 60
    ):
        self.api_key = api_key
        self.timeout = timeout
        self.headers = {"User-Agent": USER_AGENT}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def _api_call(self, method: str, endpoint: str, params: dict | None = None):
        """Make API request with error handling."""
        params = dict(params or {})
        if self.api_key:
            params["key"] = self.api_key
        try:
            response = requests.request(
                method,
                f"{API_BASE}/{endpoint.lstrip('/')}",
                params=params,
                headers=self.headers,
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException as e:
            raise click.ClickException(f"network error: {e}")

        if response.status_code == 403 and "quotaExceeded" in response.text:
            raise QuotaExceeded()
        if not response.ok:
            raise click.ClickException(
                f"{method} {endpoint} failed ({response.status_code}): {response.text}"
            )
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    def get(self, endpoint: str, **params):
        return self._api_call("GET", endpoint, params)

    def post(self, endpoint: str, **params):
        return self._api_call("POST", endpoint, params)

    def delete(self, endpoint: str, **params):
        return self._api_call("DELETE", endpoint, params)

    def paginate(self, endpoint: str, limit: int | None = None, **params):
        """Yield items across pages, stopping after `limit` items when given."""
        page = None
        seen = 0
        while True:
            extra = {"pageToken": page} if page else {}
            data = self.get(endpoint, **params, **extra)
            for item in data.get("items", []):
                yield item
                seen += 1
                if limit is not None and seen >= limit:
                    return
            page = data.get("nextPageToken")
            if not page:
                return

    def videos(self, ids: list[str]) -> dict[str, dict]:
        """Fetch full video resources by ID, 50 per request."""
        found: dict[str, dict] = {}
        for start in range(0, len(ids), 50):
            for video in self.get(
                "videos",
                part="snippet,contentDetails,statistics",
                id=",".join(ids[start : start + 50]),
                maxResults=50,
            ).get("items", []):
                found[video["id"]] = video
        return found


class Credentials:
    """Credential locations shared by every command, resolved lazily."""

    def __init__(
        self, client_secret_file: str | None, api_key_file: str | None, token_dir: str
    ):
        self.client_secret_file = client_secret_file
        self.api_key_file = api_key_file
        self.token_dir = os.path.expanduser(token_dir)

    @property
    def read_token(self) -> str:
        return os.path.join(self.token_dir, READ_TOKEN_NAME)

    @property
    def write_token(self) -> str:
        return os.path.join(self.token_dir, WRITE_TOKEN_NAME)

    def api_key(self) -> str | None:
        return os.environ.get("YOUTUBE_API_KEY") or read_secret_file(self.api_key_file)

    def public(self) -> YoutubeClient:
        """API key when configured, else the read-only OAuth token."""
        key = self.api_key()
        if key:
            return YoutubeClient(api_key=key)
        return self.account()

    def account(self, write: bool = False) -> YoutubeClient:
        scope, path = (
            (WRITE_SCOPE, self.write_token) if write else (READ_SCOPE, self.read_token)
        )
        return YoutubeClient(token=access_token(self.client_secret_file, scope, path))
