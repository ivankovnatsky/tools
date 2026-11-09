"""fetch: download files from https URLs, the nix ``fetchurl`` equivalent.

Config shape::

    fetch:
      ~/.config/nvim/spell/uk.utf-8.spl: https://ftp.nluug.nl/vim/runtime/spell/uk.utf-8.spl
      ~/.local/bin/tool:
        url: https://example.com/tool
        sha256: <hex>     # optional: downloads are verified; changing it re-downloads
        mode: "0755"      # default 0644

A missing target is downloaded. A file tools wrote is left alone unless
config changed it (``sha256`` for pinned entries, ``url`` for unpinned ones)
or ``deploy --update`` runs (unpinned only); a pinned file that already
matches is kept even when its url changes. A file tools did not write, or
edited since, is moved aside and downloaded again.
``tools diff`` never touches the network. Records match ``files:`` (content
hash, device, inode) plus the url.
"""

import hashlib
import http.client
import os
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from tools import fsutil
from tools.log import Color, debug, log
from tools.user.files import _file_hash, _record, _removable, _tracked

DEFAULT_MODE = 0o644
TIMEOUT = 60
_KEYS = {"url", "sha256", "mode"}
_HEX = set("0123456789abcdef")


@dataclass(frozen=True)
class Entry:
    target: str
    url: str
    sha256: Optional[str] = None
    mode: int = DEFAULT_MODE


def _https(url) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port  # raises on a malformed port
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.hostname)


def parse(raw) -> Tuple[List[Entry], List[str]]:
    if raw is None:
        return [], []
    if not isinstance(raw, Mapping):
        return [], [f"fetch must be a mapping of target -> url, got {type(raw).__name__}"]
    entries: Dict[str, Entry] = {}
    errors: List[str] = []
    for key, spec in raw.items():
        where = f"fetch.{key}"
        target = os.path.normpath(os.path.expanduser(str(key)))
        if not os.path.isabs(target):
            errors.append(f"{where}: target must be an absolute path")
            continue
        if isinstance(spec, str):
            spec = {"url": spec}
        if not isinstance(spec, Mapping):
            errors.append(f"{where}: must be a url or a mapping with url")
            continue
        if "force" in spec:
            errors.append(f"{where}: {fsutil.FORCE_REMOVED}")
            continue
        unknown = set(spec) - _KEYS
        if unknown:
            errors.append(f"{where}: unknown keys {', '.join(sorted(map(str, unknown)))}")
            continue
        url = spec.get("url")
        if not _https(url):
            errors.append(f"{where}: url must be an https:// URL, got {url!r}")
            continue
        sha = spec.get("sha256")
        if sha is not None:
            if not isinstance(sha, str) or len(sha) != 64 or not set(sha.lower()) <= _HEX:
                errors.append(f"{where}: sha256 must be 64 hex characters")
                continue
            sha = sha.lower()
        try:
            mode = fsutil.parse_mode(spec.get("mode"), where)
        except (TypeError, ValueError) as e:
            errors.append(str(e))
            continue
        canon = fsutil.canonical_path(target)
        if canon in entries:
            errors.append(f"{where}: {target} is declared twice")
            continue
        entries[canon] = Entry(target, url, sha, DEFAULT_MODE if mode is None else mode)
    return list(entries.values()), errors


# --------------------------------------------------------------------------
# Plan: shared by deploy and diff, no network
# --------------------------------------------------------------------------


@dataclass
class Change:
    entry: Entry
    action: str  # fetch | update | replace | chmod | adopt | unchanged | error
    reason: str = ""  # replace: why the existing file is moved aside first


@dataclass
class Plan:
    errors: List[str]
    changes: List[Change] = field(default_factory=list)
    remove: List[str] = field(default_factory=list)
    forget: List[Tuple[str, str]] = field(default_factory=list)
    keep: List[Tuple[str, str]] = field(default_factory=list)  # still tracked


def _why_refetch(e: Entry, record: Dict, update: bool) -> Optional[str]:
    # Compared against what tools wrote, not the file on disk: a hand edit is
    # left alone (like files:), only a config change triggers a download.
    if e.sha256:
        return "sha256 changed" if record.get("hash") != e.sha256 else None
    if not record.get("url"):
        return "new url"  # record moved in from files/sops/templates
    if record["url"] != e.url:
        return "url changed"
    return "--update" if update else None


def plan(raw, installed: Mapping[str, Dict], conflicts=None, update: bool = False) -> Plan:
    entries, errors = parse(raw)
    installed = fsutil.respell(dict(installed), [e.target for e in entries])
    p = Plan(errors)
    for e in entries:
        reason = conflicts.get(e.target) if conflicts else None
        if reason:
            p.changes.append(Change(e, "error", f"refusing {e.target}: {reason}"))
            continue
        record = installed.get(e.target) or {}
        if not os.path.lexists(e.target):
            p.changes.append(Change(e, "fetch"))
            continue
        if os.path.islink(e.target):
            p.changes.append(Change(e, "replace", "a symlink"))
            continue
        if not os.path.isfile(e.target):
            p.changes.append(Change(e, "error", f"{e.target} exists and is not a regular file"))
            continue
        current = _file_hash(e.target)
        if current is None:
            p.changes.append(Change(e, "error", f"cannot read {e.target}"))
            continue
        if not _tracked(e.target, record):
            if e.sha256 and current == e.sha256:
                p.changes.append(Change(e, "adopt"))
            else:
                p.changes.append(Change(e, "replace", "not tracked by tools"))
            continue
        modified = record.get("hash") != current
        why = _why_refetch(e, record, update)
        if e.sha256 and current == e.sha256 and record.get("hash") != e.sha256:
            p.changes.append(Change(e, "adopt"))  # already holds the new pin
        elif modified:
            p.changes.append(Change(e, "replace", "modified since tools wrote it"))
        elif why:
            p.changes.append(Change(e, "update", why))
        elif not modified and stat.S_IMODE(os.stat(e.target).st_mode) != e.mode:
            p.changes.append(Change(e, "chmod"))
        else:
            p.changes.append(Change(e, "unchanged"))

    for ch in p.changes:
        refused = ch.action == "replace" and fsutil.aside_refused(ch.entry.target)
        if refused:
            ch.action, ch.reason = "error", refused
    declared = [e.target for e in entries]
    for target in sorted(set(installed) - set(declared)):
        record = installed[target]
        blocked = fsutil.removal_blocked(conflicts, target)
        twin = fsutil.same_file_as(target, declared)
        if twin:
            blocked = f"the same file as declared {twin}"
        if blocked:
            p.keep.append((target, f"is {blocked}"))
        elif not os.path.lexists(target):
            p.forget.append((target, "already gone"))
        elif _removable(target, record):
            p.remove.append(target)
        else:
            p.keep.append((target, "modified since tools wrote it"))
    return p


# --------------------------------------------------------------------------
# Deploy
# --------------------------------------------------------------------------


class FetchError(Exception):
    pass


class _HttpsRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _https(newurl):
            raise FetchError(f"refusing redirect to non-https {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, timeout: float):
    # Built per call: urllib reads proxy variables at build time, after `env:` ran.
    return urllib.request.build_opener(_HttpsRedirects()).open(url, timeout=timeout)


_ABSENT = object()


def download(
    e: Entry, opener: Callable, expect=None, on_aside: Optional[Callable[[str], None]] = None
) -> str:
    """Stream ``e.url`` to a temp file beside the target, verify, rename. Returns the sha256.

    ``expect`` guards the rename against changes made during the download:
    ``_ABSENT`` (target must still be missing), a hash (target must still hold
    what tools wrote), or None (replace: whatever is there is moved aside
    first, and ``on_aside`` gets the backup path).
    """
    dirname = os.path.dirname(e.target)
    os.makedirs(dirname, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dirname, prefix=".tools-")
    try:
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "wb") as f, opener(e.url, timeout=TIMEOUT) as resp:
            while chunk := resp.read(1 << 16):
                digest.update(chunk)
                f.write(chunk)
                size += len(chunk)
            os.fchmod(f.fileno(), e.mode)
            # read(amt) returns b"" on a dropped connection instead of raising.
            expected = getattr(resp, "headers", None) and resp.headers.get("Content-Length")
        if expected and expected.isdigit() and int(expected) != size:
            raise FetchError(f"incomplete download: got {size} of {expected} bytes")
        got = digest.hexdigest()
        if e.sha256 and got != e.sha256:
            raise FetchError(f"sha256 mismatch: expected {e.sha256}, got {got}")
        if expect is _ABSENT:
            try:
                fsutil._publish_exclusive(tmp, e.target)
            except FileExistsError:
                raise FetchError("target appeared during download; not replacing it") from None
            return got
        if isinstance(expect, str) and (os.path.islink(e.target) or _file_hash(e.target) != expect):
            raise FetchError("target changed during download; not replacing it")
        if expect is None:
            moved = fsutil.move_aside(e.target)
            if moved and on_aside:
                on_aside(moved)
        os.replace(tmp, e.target)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise
    return got


def _fetch_record(e: Entry, file_hash: str) -> Dict:
    return {**_record(e.target, file_hash), "url": e.url}


def install_fetch(
    raw, state: Dict, conflicts=None, update: bool = False, opener: Optional[Callable] = None
) -> bool:
    installed: Dict[str, Dict] = dict(state.get("fetch", {}).get("installed", {}) or {})
    p = plan(raw, installed, conflicts, update)
    fsutil.respell(installed, [ch.entry.target for ch in p.changes])
    # Shared with state up front so records survive an interrupted run.
    state.setdefault("fetch", {})["installed"] = installed
    opener = opener or _open
    success = not p.errors
    changed = False
    for err in p.errors:
        log(f"fetch: {err}", Color.RED)
    for ch in p.changes:
        e = ch.entry
        if ch.action == "error":
            log(f"fetch: {ch.reason}", Color.RED)
            success = False
        elif ch.action == "unchanged":
            if e.target in installed:
                fsutil.refresh_dev(e.target, installed[e.target])
                installed[e.target]["url"] = e.url
        elif ch.action in ("adopt", "chmod"):
            # Re-checked: an earlier download may have given time for an edit.
            expect = e.sha256 if ch.action == "adopt" else installed[e.target].get("hash")
            if _file_hash(e.target) != expect:
                log(f"fetch: {e.target} changed during this run; left as is", Color.YELLOW)
                success = success and ch.action == "chmod"
                continue
            try:
                fsutil.apply_attrs(e.target, e.mode)
            except OSError as err:
                log(f"fetch: failed to chmod {e.target}: {err}", Color.RED)
                success = False
                continue
            if ch.action == "adopt":
                installed[e.target] = _fetch_record(e, e.sha256)
            else:
                fsutil.refresh_dev(e.target, installed[e.target])
                installed[e.target]["url"] = e.url
                log(f"Chmod: {e.target} -> {oct(e.mode)}", Color.GREEN)
                changed = True
        else:
            try:
                expect = {"fetch": _ABSENT, "update": (installed.get(e.target) or {}).get("hash")}

                def aside(moved, e=e, why=ch.reason):
                    log(f"Moved aside ({why}): {e.target} -> {moved}", Color.YELLOW)

                file_hash = download(e, opener, expect.get(ch.action), aside)
            except (OSError, ValueError, http.client.HTTPException, FetchError) as err:
                log(f"fetch: {e.target} <- {e.url}: {err}", Color.RED)
                success = False
                continue
            installed[e.target] = _fetch_record(e, file_hash)
            verb = {"fetch": "Fetched", "update": "Updated", "replace": "Replaced"}
            suffix = f" ({ch.reason})" if ch.reason and ch.action != "replace" else ""
            log(f"{verb[ch.action]}: {e.target} <- {e.url}{suffix}", Color.GREEN)
            changed = True

    for target, reason in p.keep:
        log(f"fetch: not removing {target}: {reason}; still tracked", Color.YELLOW)
    # Only config errors hold removals: an unparsed entry looks undesired.
    if not p.errors:
        for target, _reason in p.forget:
            installed.pop(target, None)
        for target in p.remove:
            # Re-checked: downloads above may have taken long enough for an edit.
            if not _removable(target, installed[target]):
                log(
                    f"fetch: not removing {target}: modified since tools wrote it; still tracked",
                    Color.YELLOW,
                )
                continue
            try:
                os.remove(target)
            except OSError as err:
                log(f"fetch: failed to remove {target}: {err}", Color.RED)
                success = False
                continue
            installed.pop(target, None)
            log(f"Removed: {target}", Color.RED)
            changed = True

    if not changed:
        debug("All fetched files up to date", Color.BLUE)
    return success


# --------------------------------------------------------------------------
# Diff (read-only)
# --------------------------------------------------------------------------


def diff_fetch(raw, state: Dict, conflicts=None) -> List[str]:
    p = plan(raw, state.get("fetch", {}).get("installed", {}) or {}, conflicts)
    rows = [f"  ! {err}" for err in p.errors]
    for ch in p.changes:
        e = ch.entry
        if ch.action == "error":
            rows.append(f"  ! {ch.reason}")
        elif ch.action == "fetch":
            rows.append(f"  + fetch {e.target} <- {e.url}")
        elif ch.action == "update":
            rows.append(f"  ~ fetch {e.target} <- {e.url} ({ch.reason})")
        elif ch.action == "replace":
            note = fsutil.aside_note(e.target, ch.reason)
            rows.append(f"  ~ replace {e.target} <- {e.url} ({note})")
        elif ch.action == "chmod":
            rows.append(f"  ~ chmod {e.target} -> {oct(e.mode)}")
        elif ch.action == "adopt":
            rows.append(f"  ~ adopt {e.target} (matches sha256)")
    rows += [f"  ~ keep {target} ({reason}; not removed)" for target, reason in p.keep]
    return rows + fsutil.describe_removals(p.errors, p.remove, [])


def targets(raw) -> List[str]:
    return [e.target for e in parse(raw)[0]]


def has_work(raw, state: Dict) -> bool:
    if raw is not None and not isinstance(raw, Mapping):
        return True
    return bool(raw or state.get("fetch", {}).get("installed"))
