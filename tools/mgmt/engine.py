"""Run context shared by service reconcilers.

Everything a reconciler touches outside its own logic is injected here: the
HTTP opener, the file/secret reader, sleep, subprocess runner and the state
dict. Reconcilers receive already-validated specs and never see the config
layer.
"""

import hashlib
import hmac
import os
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from tools.log import Color, log
from tools.mgmt.http import ApiClient, InvalidSpec, ServiceError, norm_url
from tools.util import run_command as _run_command

MAX_LINE = 400


def read_file(path: str) -> str:
    with open(os.path.expanduser(path), "r") as f:
        return f.read()


@dataclass
class Endpoint:
    """Where a service lives and how hard to try reaching it.

    ``url`` may contain ``@NAME@`` placeholders filled from ``url_files`` at run
    time, so a secret domain never has to appear in desired state.
    """

    url: str
    url_files: Dict[str, str] = field(default_factory=dict)
    timeout: float = 30
    retries: int = 5
    retry_delay: float = 2.0


class Context:
    def __init__(
        self,
        service: str,
        *,
        apply: bool,
        state: Dict,
        opener: Callable = urllib.request.urlopen,
        reader: Callable[[str], str] = read_file,
        sleep: Callable[[float], None] = time.sleep,
        runner: Callable = _run_command,
        fingerprint_key: Optional[bytes] = None,
    ):
        self.service = service
        self.fingerprint_key = fingerprint_key
        self.apply = apply
        self.state = state
        self.opener = opener
        self.reader = reader
        self.sleep = sleep
        self.runner = runner
        self.changes: List[str] = []
        self.ok = True
        self.applied = 0
        self._redact: set = set()
        # Inputs are resolved before the first write and then reused, so a
        # file that changes or vanishes mid-run cannot half-apply a service.
        self._files: Dict[str, str] = {}
        self._header_done = False

    # -- output -----------------------------------------------------------

    def redact(self, text: str) -> str:
        for value in sorted(self._redact, key=len, reverse=True):
            text = text.replace(value, "***")
        return text

    def _emit(self, line: str, color: str) -> None:
        self.changes.append(line)
        if self.apply:
            if not self._header_done:
                log(f"{self.service}:", Color.BLUE)
                self._header_done = True
            log(line, color)

    def _clean(self, text: str) -> str:
        # Redact first, then truncate: the other order can leave a secret prefix.
        line = self.redact(text)
        return line if len(line) <= MAX_LINE else line[: MAX_LINE - 3] + "..."

    def change(self, text: str) -> None:
        """Record one planned change (printed live during deploy)."""
        self._emit(f"  {self._clean(text)}", Color.YELLOW)

    def fail(self, text: str) -> None:
        self.ok = False
        self._emit(f"  ! {self._clean(text)}", Color.RED)

    def do(self, what: str, fn: Callable[[], object]) -> bool:
        """Run a write only when applying. Failures fail the service, not the run."""
        if not self.apply:
            return False
        try:
            fn()
        except ServiceError as e:
            self.fail(f"{what}: {e}")
            return False
        self.applied += 1
        return True

    # -- inputs -----------------------------------------------------------

    def _read(self, path: str) -> str:
        if path not in self._files:
            self._files[path] = self.reader(path)
        return self._files[path]

    def secret(self, path: str, what: str) -> str:
        """Read a secret file; its value is redacted from all later output."""
        try:
            value = self._read(path).strip()
        except OSError as e:
            raise InvalidSpec(f"cannot read {what} file {path}: {e.strerror or e}") from None
        if not value:
            raise InvalidSpec(f"{what} file {path} is empty")
        self._redact.add(value)
        return value

    def sensitive(self, value: str) -> None:
        """Redact a derived secret (e.g. a reformatted webhook) from output."""
        if value:
            self._redact.add(value)

    def optional_secret(self, path: str, what: str) -> Optional[str]:
        """Like secret(), but an absent or empty file is None (not an error)."""
        try:
            value = self._read(path).strip()
        except FileNotFoundError:
            return None
        except OSError as e:
            raise InvalidSpec(f"cannot read {what} file {path}: {e.strerror or e}") from None
        if not value:
            return None
        self._redact.add(value)
        return value

    def check_prune(self, kind: str, desired: int, remote: int, prune: bool) -> None:
        """Refuse to delete every remote object of a kind unless `prune: true`.

        An empty desired list is far more often a config mistake (children
        commented out) than an intent to wipe the remote side.
        """
        if desired == 0 and remote > 0 and not prune:
            raise InvalidSpec(
                f"refusing to remove all {remote} {kind}; set prune: true to allow it"
            )

    def kept(self, kind: str, label: str, items: Iterable[Tuple[str, str]], why: str) -> None:
        """Show items that left the config but stay remote because `kind` is never deleted.

        `items` are (resolved identity, printable name) pairs. State keeps an
        HMAC of each identity and its printable name, never a resolved value,
        so a name moved into a file is still the same item. Each dropped item
        is shown once rather than kept silently.
        """
        if self.fingerprint_key is None:
            return  # no key yet (diff before first deploy): nothing recorded
        now: Dict[str, str] = {}
        for identity, name in items:
            msg = f"managed\0{kind}\0{identity}".encode()
            now[hmac.new(self.fingerprint_key, msg, hashlib.sha256).hexdigest()] = name
        before = self.state.get("managed", {}).get(kind, {})
        if not isinstance(before, dict):
            before = {}
        for digest in sorted(before.keys() - now.keys(), key=lambda d: str(before[d])):
            self.change(f"= {label} {before[digest]} removed from config, kept ({why})")
        if self.apply:
            managed = self.state.setdefault("managed", {})
            if now:
                managed[kind] = now
            else:
                managed.pop(kind, None)
            if not managed:
                self.state.pop("managed")

    def value(self, ref: Dict[str, str], what: str) -> str:
        """Resolve a `{"value": ...}` / `{"file": ...}` reference from spec.one_of."""
        if "file" in ref:
            return self.secret(ref["file"], what)
        return ref["value"]

    def text(self, path: str, what: str) -> str:
        """Read a non-secret input file."""
        try:
            return self._read(path)
        except OSError as e:
            raise InvalidSpec(f"cannot read {what} file {path}: {e.strerror or e}") from None

    def client(self, endpoint: Endpoint, headers: Optional[Dict[str, str]] = None) -> ApiClient:
        url = endpoint.url
        for name, path in endpoint.url_files.items():
            # Newlines dropped like `tr -d '\n'`: a trailing one would split the URL.
            value = self.secret(path, f"@{name}@").replace("\n", "")
            self.sensitive(value)
            url = url.replace(f"@{name}@", value)
        return ApiClient(
            url,
            label=self.service,
            headers=headers,
            timeout=endpoint.timeout,
            # diff must answer promptly even when the service is down.
            retries=endpoint.retries if self.apply else 0,
            retry_delay=endpoint.retry_delay,
            opener=self.opener,
            sleep=self.sleep,
        )

    # -- write-only secrets ----------------------------------------------
    #
    # APIs mask secrets on read ("********"), so "did it change" cannot be
    # asked of the server. An HMAC of the last value we wrote, keyed by a
    # per-host key kept outside the state file and indexed by resource
    # identity, answers it locally. Never the value itself.

    def _digest(self, endpoint: str, identity: str, value: str) -> str:
        # The endpoint is part of the digest (not the state key), so the same
        # resource on another instance reads as changed and is pushed once.
        msg = f"{norm_url(endpoint)}\0{identity}\0{value}".encode()
        return hmac.new(self.fingerprint_key, msg, hashlib.sha256).hexdigest()

    def secret_changed(self, endpoint: str, identity: str, value: str) -> bool:
        if self.fingerprint_key is None:
            return True  # no key yet (diff before first deploy): nothing recorded
        stored = self.state.get("fingerprints", {}).get(identity)
        digest = self._digest(endpoint, identity, value)
        return stored is None or not hmac.compare_digest(stored, digest)

    def record_secret(self, endpoint: str, identity: str, value: str) -> None:
        if self.apply and self.fingerprint_key is not None:
            self.state.pop("salt", None)  # pre-HMAC scheme
            digest = self._digest(endpoint, identity, value)
            self.state.setdefault("fingerprints", {})[identity] = digest
