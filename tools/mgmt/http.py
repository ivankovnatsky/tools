"""Minimal JSON-over-HTTP client for service APIs (stdlib only, no config deps)."""

import base64
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Iterable, List, Optional

USER_AGENT = "tools-mgmt"
_RETRY_STATUSES = {502, 503, 504}
_SAFE_METHODS = {"GET", "HEAD"}


class ServiceError(Exception):
    """Fails one service. Messages must never carry secret values."""


class InvalidSpec(ServiceError):
    """Desired state is malformed or a referenced input is unreadable."""


class Unreachable(ServiceError):
    """The service did not answer (not started, down, DNS, timeout)."""


class AuthFailed(ServiceError):
    """The service rejected our credentials."""


class BadResponse(ServiceError):
    """A read returned a shape we cannot reconcile against.

    Treating it as "nothing there" would turn into mass creates or deletes.
    """


class _Absent:
    def __repr__(self):
        return "ABSENT"

    def __bool__(self):
        return False


# Returned for a status listed in `allow`, so "not found" never looks like an
# empty 200 body.
ABSENT = _Absent()


def check_list(value: Any, what: str, keys: Iterable[str] = ()) -> List[Dict]:
    keys = tuple(keys)
    if not isinstance(value, list):
        raise BadResponse(f"{what}: expected a JSON list, got {type(value).__name__}")
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            raise BadResponse(f"{what}[{i}]: expected an object, got {type(item).__name__}")
        missing = [k for k in keys if k not in item]
        if missing:
            raise BadResponse(f"{what}[{i}]: missing {', '.join(missing)}")
    return value


def check_dict(value: Any, what: str) -> Dict:
    if not isinstance(value, dict):
        raise BadResponse(f"{what}: expected a JSON object, got {type(value).__name__}")
    return value


def list_field(obj: Dict, key: str, what: str, keys: Optional[Iterable[str]] = None) -> List:
    """A list the server may serialize as null or omit when empty.

    With `keys`, every item must be an object carrying them.
    """
    value = obj.get(key)
    if value is None:
        return []
    if keys is not None:
        return check_list(value, f"{what}.{key}", keys)
    if not isinstance(value, list):
        raise BadResponse(f"{what}.{key}: expected a list, got {type(value).__name__}")
    return value


def dict_field(obj: Dict, key: str, what: str) -> Dict:
    """A nested object we merge into and write back: absent or null aborts,
    since writing a stub would reset every server setting in it."""
    value = obj.get(key)
    if not isinstance(value, dict):
        raise BadResponse(f"{what}.{key}: expected an object, got {type(value).__name__}")
    return value


def norm_url(url: str) -> str:
    """Scheme and host are case-insensitive; a trailing slash is not significant."""
    parts = urllib.parse.urlsplit(url.strip())
    path = parts.path.rstrip("/")
    return urllib.parse.urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, parts.query, "")
    )


def norm_path(path: str) -> str:
    """APIs echo paths with or without a trailing slash; compare without."""
    return path.rstrip("/") or "/"


class ApiError(ServiceError):
    def __init__(self, method: str, path: str, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"{method} {path} failed (HTTP {status}): {detail}")


# Generous cap only against huge bodies; display truncation happens after
# redaction in Context, so a secret is never cut into a printable prefix.
_DETAIL_CAP = 4096


def _detail(raw: bytes) -> str:
    """Error summary from a response body; never the request payload."""
    try:
        data = json.loads(raw or b"null")
    except ValueError:
        return (raw or b"").decode("utf-8", "replace").strip()[:_DETAIL_CAP] or "no body"
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]
    if isinstance(data, dict):
        errors = data.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return str(errors[0].get("detail") or errors[0])[:_DETAIL_CAP]
        for key in ("message", "error", "errorMessage", "detail"):
            if data.get(key):
                return str(data[key])[:_DETAIL_CAP]
    return str(data)[:_DETAIL_CAP]


def _encode_params(params: Dict[str, Any]) -> str:
    def conv(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        return v

    flat = {k: [conv(x) for x in v] if isinstance(v, list) else conv(v) for k, v in params.items()}
    return urllib.parse.urlencode(flat, doseq=True)


class ApiClient:
    """One base URL, fixed headers, retries with exponential backoff.

    Only GET/HEAD (and calls marked ``idempotent``) are retried: a write that
    timed out may still have landed, and replaying it could duplicate objects.
    ``label`` is what errors show instead of the URL, which may embed a secret
    domain.
    """

    def __init__(
        self,
        base_url: str,
        *,
        label: str,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 30,
        retries: int = 0,
        retry_delay: float = 2.0,
        opener: Callable = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.label = label
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.retries = retries
        self.retry_delay = retry_delay
        self._opener = opener
        self._sleep = sleep

    def with_headers(self, **headers: str) -> "ApiClient":
        clone = ApiClient(
            self.base_url,
            label=self.label,
            headers={**self.headers, **headers},
            timeout=self.timeout,
            retries=self.retries,
            retry_delay=self.retry_delay,
            opener=self._opener,
            sleep=self._sleep,
        )
        return clone

    def with_basic_auth(self, username: str, password: str) -> "ApiClient":
        raw = base64.b64encode(f"{username}:{password}".encode()).decode()
        return self.with_headers(Authorization=f"Basic {raw}")

    def request(
        self,
        method: str,
        path: str,
        data: Any = None,
        *,
        params: Optional[Dict[str, Any]] = None,
        allow: Iterable[int] = (),
        idempotent: bool = False,
    ) -> Any:
        """Send one request and return the decoded JSON body (None when empty).

        A status listed in ``allow`` returns ``ABSENT`` instead of raising
        (e.g. 404 for existence checks).
        """
        url = self.base_url + path
        if params:
            url += ("&" if "?" in path else "?") + _encode_params(params)
        body = None
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **self.headers}
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        allowed = set(allow)
        attempts = 1 + (self.retries if (method in _SAFE_METHODS or idempotent) else 0)

        for attempt in range(attempts):
            last = attempt == attempts - 1
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with self._opener(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    status = getattr(resp, "status", 200)
                if not raw:
                    return None
                try:
                    decoded = json.loads(raw)
                except ValueError:
                    return raw.decode("utf-8", "replace")
                # NextDNS-style APIs can report a rejected write inside a 2xx.
                if isinstance(decoded, dict) and decoded.get("errors"):
                    raise ApiError(method, path, status, _detail(raw))
                return decoded
            except urllib.error.HTTPError as e:
                if e.code in allowed:
                    return ABSENT
                raw = e.read() if e.fp is not None else b""
                if e.code in (401, 403):
                    raise AuthFailed(
                        f"{self.label}: authentication failed (HTTP {e.code} on {method} {path})"
                    ) from None
                if e.code in _RETRY_STATUSES and not last:
                    self._sleep(self.retry_delay * (2**attempt))
                    continue
                raise ApiError(method, path, e.code, _detail(raw)) from None
            except (urllib.error.URLError, OSError, socket.timeout) as e:
                if not last:
                    self._sleep(self.retry_delay * (2**attempt))
                    continue
                reason = getattr(e, "reason", e)
                raise Unreachable(
                    f"{self.label} unreachable after {attempts} attempt(s): {reason}"
                ) from None
        raise AssertionError("unreachable")

    def get_list(self, path: str, keys: Iterable[str] = (), **kw) -> List[Dict]:
        """GET a JSON list of objects carrying `keys`, or raise BadResponse."""
        return check_list(self.request("GET", path, **kw), f"GET {path}", keys)

    def get_dict(self, path: str, **kw) -> Any:
        """GET a JSON object (or ABSENT for an allowed status), or raise BadResponse."""
        value = self.request("GET", path, **kw)
        if value is ABSENT:
            return value
        return check_dict(value, f"GET {path}")
