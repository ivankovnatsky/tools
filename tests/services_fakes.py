"""In-process fakes for service tests: no sockets, no real files, no sleeping."""

import io
import json
import urllib.error
import urllib.parse

from tools.services import run_services
from tools.services.engine import Context


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeServer:
    """Routes (method, path) to canned JSON or a handler(body, query).

    A handler returns either a body or (status, body). Unrouted requests 404.
    """

    def __init__(self, base="http://svc.example.com"):
        self.base = base
        self.routes = {}
        self.calls = []
        self.urls = []
        self.down = False

    def on(self, method, path, response=None, status=200):
        if callable(response):
            self.routes[(method, path)] = response
        else:
            self.routes[(method, path)] = lambda body, query: (status, response)
        return self

    def __call__(self, req, timeout=None):
        self.urls.append(req.full_url)
        url = urllib.parse.urlsplit(req.full_url)
        base_path = urllib.parse.urlsplit(self.base).path.rstrip("/")
        path = url.path[len(base_path) :] if url.path.startswith(base_path) else url.path
        query = urllib.parse.parse_qs(url.query)
        body = json.loads(req.data) if req.data else None
        self.calls.append((req.get_method(), path, query, body, dict(req.header_items())))
        if self.down:
            raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
        handler = self.routes.get((req.get_method(), path))
        if handler is None:
            status, payload = 404, {"message": "not found"}
        else:
            result = handler(body, query)
            status, payload = result if isinstance(result, tuple) else (200, result)
        raw = b"" if payload is None else json.dumps(payload).encode()
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(raw))
        return FakeResponse(raw)

    def writes(self):
        return [(m, p) for m, p, *_ in self.calls if m not in ("GET", "HEAD")]

    def made(self, method, path):
        return [c for c in self.calls if c[0] == method and c[1] == path]


class Harness:
    def __init__(self, files=None, runner=None):
        self.files = dict(files or {})
        self.sleeps = []
        self.runner = runner or (lambda cmd, env=None, cwd=None: (0, "", ""))
        self.server = FakeServer()
        self.key = b"test-key-" + b"0" * 32

    def reader(self, path):
        if path in self.files:
            return self.files[path]
        raise FileNotFoundError(2, "No such file or directory", path)

    def factory(self, name, *, apply, state, fingerprint_key=None):
        return Context(
            name,
            apply=apply,
            state=state,
            opener=self.server,
            reader=self.reader,
            sleep=self.sleeps.append,
            runner=self.runner,
            fingerprint_key=fingerprint_key or self.key,
        )

    def managed(self, kind, *items):
        """State for Context.kept(): items are names or (identity, name) pairs."""
        ctx = self.factory("x", apply=True, state={})
        pairs = [i if isinstance(i, tuple) else (i, i) for i in items]
        ctx.kept(kind, "", pairs, "")
        return ctx.state["managed"][kind]

    def run(self, config, state=None, apply=True, **secrets):
        state = {} if state is None else state
        secrets = secrets or {"secrets_dir": "/secrets"}
        results, ok = run_services(
            config, state, apply=apply, context_factory=self.factory, **secrets
        )
        rows = [row for _, rows in results for row in rows]
        return rows, ok, state
