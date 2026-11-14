"""fetch: download files from https URLs, the nix ``fetchurl`` equivalent.

Config shape::

    fetch:
      ~/.config/nvim/spell/uk.utf-8.spl: https://ftp.nluug.nl/vim/runtime/spell/uk.utf-8.spl
      ~/.local/bin/tool:
        url: https://example.com/tool
        sha256: <hex>     # optional: downloads are verified; changing it re-downloads
        mode: "0755"      # default 0644
        restartUnits: [launchd:org.example.tool]
      /Applications/Tool.app:
        url: https://example.com/tool.zip
        sha256: <hex>     # of the download
        install: |        # sh -e; $src download, $tmp scratch dir (cwd), $out result
          ditto -x -k "$src" .
          mv Tool.app "$out"

With ``install``, the verified download is handed to the script, which must
create ``$out`` (a file or directory); that result is moved into place. Only
its inputs (url, sha256, script) and its type are recorded: the result is
never hashed, and a plan only lstats it. ``mode`` then applies only to a file
result and only when given. A changed input re-runs the script.

A missing target is downloaded. A file tools wrote is left alone unless
config changed it (``sha256`` for pinned entries, ``url`` for unpinned ones)
or ``deploy --update`` runs (unpinned only); a pinned file that already
matches is kept even when its url changes. A file tools did not write, or
edited since, is moved aside and downloaded again.
``tools diff`` never touches the network or runs scripts. Plain records match
``files:`` (content hash, device, inode) plus the url.
"""

import errno
import hashlib
import http.client
import os
import posixpath
import shutil
import stat
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from tools import fsutil
from tools.log import Color, debug, log
from tools.user import files, secrets_flow
from tools.user.files import _file_hash, _is_real_dir, _record, _removable, _tracked

DEFAULT_MODE = 0o644
TIMEOUT = 60
_KEYS = {"url", "sha256", "mode", "install", "restartUnits"}
_HEX = set("0123456789abcdef")
_OUTPUT_TAIL = 20


@dataclass(frozen=True)
class Entry:
    target: str
    url: str
    sha256: Optional[str] = None
    mode: Optional[int] = DEFAULT_MODE  # None: install result keeps its own mode
    install: Optional[str] = None
    restart_units: Tuple[str, ...] = ()


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
        install = spec.get("install")
        if install is not None and (not isinstance(install, str) or not install.strip()):
            errors.append(f"{where}: install must be a non-empty shell script")
            continue
        units = spec.get("restartUnits")
        units = [] if units is None else units
        if not isinstance(units, list) or not all(isinstance(u, str) for u in units):
            errors.append(f"{where}: restartUnits must be a list of strings")
            continue
        if mode is None and not install:
            mode = DEFAULT_MODE
        canon = fsutil.canonical_path(target)
        if canon in entries:
            errors.append(f"{where}: {target} is declared twice")
            continue
        entries[canon] = Entry(target, url, sha, mode, install, tuple(units))
    return list(entries.values()), errors


def _kind(path: str) -> Optional[str]:
    """``file``, ``dir``, ``symlink`` or ``other`` from lstat; None if absent."""
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return None
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "dir"
    return "symlink" if stat.S_ISLNK(mode) else "other"


def _is_install_record(record) -> bool:
    return isinstance(record, dict) and "type" in record


def _birthtime(path: str) -> Optional[float]:
    try:
        return getattr(os.lstat(path), "st_birthtime", None)
    except OSError:
        return None


def _same_result(target: str, record) -> bool:
    """Whether ``target`` is the install result tools moved into place.

    A directory's dev moves with remounts, so it is matched on inode plus
    birth time, or mtime where the platform has no birth time: a freed
    inode number is reused (ext4), so the inode alone is not identity.
    Records without an inode predate identity tracking and pass.
    """
    if "ino" not in record:
        return True
    if record["type"] == "dir":
        fp = fsutil.fingerprint(target)
        if fp is None or fp["ino"] != record["ino"]:
            return False
        if "birthtime" in record:
            return _birthtime(target) == record["birthtime"]
        return fp["mtime_ns"] == record.get("mtime_ns")
    return fsutil.same_identity(target, record)


def _intact(target: str, record) -> bool:
    """Whether ``target`` is still what tools put there: the recorded type and
    inode for an install result (never read), the recorded content for a
    plain file."""
    if _is_install_record(record):
        return _kind(target) == record["type"] and _same_result(target, record)
    return not os.path.islink(target) and _file_hash(target) == (record or {}).get("hash")


def _why_kept(target: str, record) -> Optional[str]:
    """Why a dropped entry's target is not removed, or None to remove it."""
    if _is_install_record(record):
        intact = _intact(target, record)
        return None if intact else f"no longer the {record['type']} tools installed"
    return None if _removable(target, record) else "modified since tools wrote it"


# Section move (secrets_flow.move_between_sections).
def record_unmodified(target: str, record) -> bool:
    if _is_install_record(record):
        return record["type"] == "file" and _intact(target, record)
    return files.record_unmodified(target, record)


record_for = files.record_for
record_secret = files.record_secret


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
    # Compared against what tools recorded, not the target on disk: a hand
    # edit is left alone, only a config change triggers a download.
    if record.get("install") != e.install:
        return "install changed"
    if e.sha256:
        pinned = record.get("sha256") if e.install else record.get("hash")
        return "sha256 changed" if pinned != e.sha256 else None
    if not record.get("url"):
        return "new url"  # record moved in from files/sops/templates
    if record["url"] != e.url:
        return "url changed"
    return "--update" if update else None


def _plan_install(e: Entry, kind: str, record: Dict, update: bool) -> Change:
    """An install result, or a target whose record is one: only inputs and
    the lstat type are compared, the result is never read."""
    if kind not in ("file", "dir"):
        return Change(e, "error", f"{e.target} exists and is not a regular file or directory")
    if not record:
        return Change(e, "replace", "not tracked by tools")
    if not _is_install_record(record) and not (
        _tracked(e.target, record) and _intact(e.target, record)
    ):
        return Change(e, "replace", "modified since tools wrote it")
    recorded = record.get("type", "file")
    if kind != recorded or (_is_install_record(record) and not _same_result(e.target, record)):
        return Change(e, "replace", f"no longer the {recorded} tools installed")
    why = _why_refetch(e, record, update)
    if why:
        return Change(e, "update", why)
    if e.mode is not None and kind == "file" and stat.S_IMODE(os.lstat(e.target).st_mode) != e.mode:
        return Change(e, "chmod")
    return Change(e, "unchanged")


def _plan_file(e: Entry, record: Dict, update: bool) -> Change:
    if not os.path.isfile(e.target):
        return Change(e, "error", f"{e.target} exists and is not a regular file")
    current = _file_hash(e.target)
    if current is None:
        return Change(e, "error", f"cannot read {e.target}")
    if not _tracked(e.target, record):
        if e.sha256 and current == e.sha256:
            return Change(e, "adopt")
        return Change(e, "replace", "not tracked by tools")
    why = _why_refetch(e, record, update)
    if e.sha256 and current == e.sha256 and record.get("hash") != e.sha256:
        return Change(e, "adopt")  # already holds the new pin
    if record.get("hash") != current:
        return Change(e, "replace", "modified since tools wrote it")
    if why:
        return Change(e, "update", why)
    if stat.S_IMODE(os.stat(e.target).st_mode) != e.mode:
        return Change(e, "chmod")
    return Change(e, "unchanged")


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
        kind = _kind(e.target)
        if kind is None:
            p.changes.append(Change(e, "fetch"))
        elif kind == "symlink":
            p.changes.append(Change(e, "replace", "a symlink"))
        elif e.install or _is_install_record(record):
            p.changes.append(_plan_install(e, kind, record, update))
        else:
            p.changes.append(_plan_file(e, record, update))

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
        else:
            kept = _why_kept(target, record)
            if kept:
                p.keep.append((target, kept))
            else:
                p.remove.append(target)
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


def _src_name(url: str) -> str:
    name = posixpath.basename(urllib.parse.urlsplit(url).path)
    return name if name not in ("", ".", "..") and "\0" not in name else "download"


def _run_install(e: Entry, src: str, tmp: str, out: str) -> None:
    env = {**os.environ, "src": src, "tmp": tmp, "out": out}
    try:
        proc = subprocess.run(
            ["/bin/sh", "-e", "-c", e.install],
            cwd=tmp,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    except OSError as err:
        raise FetchError(f"install script could not run: {err}") from None
    output = proc.stdout.decode(errors="replace").rstrip("\n")
    if proc.returncode != 0:
        tail = "\n".join(output.splitlines()[-_OUTPUT_TAIL:])
        raise FetchError(
            f"install script failed (exit {proc.returncode})" + (f":\n{tail}" if tail else "")
        )
    if output:
        debug(output)
    if not os.path.lexists(out):
        raise FetchError("install script did not create $out")
    if os.path.islink(out) or not (os.path.isfile(out) or os.path.isdir(out)):
        raise FetchError("install script's $out must be a regular file or a directory")


def _move_aside(path: str) -> Optional[str]:
    if not _is_real_dir(path):
        return fsutil.move_aside(path)
    dest = fsutil.backup_path(path)
    if os.path.lexists(dest):
        raise FileExistsError(errno.EEXIST, "an earlier backup exists", dest)
    os.rename(path, dest)
    return dest


def _force_rmtree(path: str) -> None:
    """``shutil.rmtree``, first making read-only directories in the tree writable."""
    os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) | stat.S_IRWXU)
    for root, dirs, _files in os.walk(path):
        for d in dirs:
            p = os.path.join(root, d)
            if not os.path.islink(p):
                os.chmod(p, stat.S_IMODE(os.lstat(p).st_mode) | stat.S_IRWXU)
    shutil.rmtree(path)


def _rmtree(path: str) -> None:
    """Remove a scratch tree, including read-only directories an install script left."""
    try:
        _force_rmtree(path)
    except OSError as err:
        log(f"fetch: could not clean up {path}: {err}", Color.YELLOW)


def download(
    e: Entry, opener: Callable, expect=None, on_aside: Optional[Callable[[str], None]] = None
) -> str:
    """Stream ``e.url`` into a scratch directory beside the target, verify, install.

    Returns the sha256 of the download.

    ``expect`` guards the rename against changes made during the download:
    ``_ABSENT`` (target must still be missing), a record (target must still
    be what tools put there, see ``_intact``), or None (replace: whatever is
    there is moved aside first, and ``on_aside`` gets the backup path).
    """
    dirname = os.path.dirname(e.target)
    os.makedirs(dirname, exist_ok=True)
    work = tempfile.mkdtemp(dir=dirname, prefix=".tools-")
    try:
        if e.install:
            os.mkdir(os.path.join(work, "src"))
            src = os.path.join(work, "src", _src_name(e.url))
        else:
            src = os.path.join(work, "out")
        digest = hashlib.sha256()
        size = 0
        with open(src, "xb") as f, opener(e.url, timeout=TIMEOUT) as resp:
            while chunk := resp.read(1 << 16):
                digest.update(chunk)
                f.write(chunk)
                size += len(chunk)
            if not e.install:
                os.fchmod(f.fileno(), e.mode)
            # read(amt) returns b"" on a dropped connection instead of raising.
            expected = getattr(resp, "headers", None) and resp.headers.get("Content-Length")
        if expected and expected.isdigit() and int(expected) != size:
            raise FetchError(f"incomplete download: got {size} of {expected} bytes")
        got = digest.hexdigest()
        if e.sha256 and got != e.sha256:
            raise FetchError(f"sha256 mismatch: expected {e.sha256}, got {got}")
        staged = src
        if e.install:
            tmp, staged = os.path.join(work, "tmp"), os.path.join(work, "out")
            os.mkdir(tmp)
            _run_install(e, src, tmp, staged)
            if os.path.isfile(staged) and e.mode is not None:
                os.chmod(staged, e.mode)
        if expect is _ABSENT:
            try:
                if os.path.isdir(staged):
                    if os.path.lexists(e.target):
                        raise FileExistsError(errno.EEXIST, "exists", e.target)
                    os.rename(staged, e.target)
                else:
                    fsutil._publish_exclusive(staged, e.target)
            except FileExistsError:
                raise FetchError("target appeared during download; not replacing it") from None
            return got
        if isinstance(expect, dict) and not _intact(e.target, expect):
            raise FetchError("target changed during download; not replacing it")
        if expect is None:
            moved = _move_aside(e.target)
            if moved and on_aside:
                on_aside(moved)
        elif os.path.lexists(e.target) and (os.path.isdir(staged) or _is_real_dir(e.target)):
            # os.replace cannot put a directory over a file or a non-empty directory.
            old = os.path.join(work, "old")
            os.rename(e.target, old)
            try:
                os.replace(staged, e.target)
            except BaseException:
                os.rename(old, e.target)
                raise
            return got
        os.replace(staged, e.target)
    finally:
        _rmtree(work)
    return got


def _fetch_record(e: Entry, sha: str) -> Dict:
    if e.install:
        fp = fsutil.fingerprint(e.target) or {}
        return {
            "url": e.url,
            "sha256": sha,
            "install": e.install,
            "type": _kind(e.target),
            **{k: fp[k] for k in ("dev", "ino", "mtime_ns") if k in fp},
            **({"birthtime": birth} if (birth := _birthtime(e.target)) is not None else {}),
        }
    return {**_record(e.target, sha), "url": e.url}


def _owe_restarts(state: Dict, e: Entry) -> None:
    secrets_flow.owe_restarts(state, e.restart_units, f"fetch/{e.target}")


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
            if (
                _file_hash(e.target) != e.sha256
                if ch.action == "adopt"
                else not _intact(e.target, installed[e.target])
            ):
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
                expect = {"fetch": _ABSENT, "update": installed.get(e.target) or {}}

                def aside(moved, e=e, why=ch.reason):
                    log(f"Moved aside ({why}): {e.target} -> {moved}", Color.YELLOW)

                sha = download(e, opener, expect.get(ch.action), aside)
            except (OSError, ValueError, http.client.HTTPException, FetchError) as err:
                log(f"fetch: {e.target} <- {e.url}: {err}", Color.RED)
                success = False
                continue
            installed[e.target] = _fetch_record(e, sha)
            _owe_restarts(state, e)
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
            kept = _why_kept(target, installed[target])
            if kept:
                log(f"fetch: not removing {target}: {kept}; still tracked", Color.YELLOW)
                continue
            try:
                if _is_real_dir(target):
                    _force_rmtree(target)
                else:
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
            rows.append(f"  + fetch {e.target} <- {e.url}" + (" (install)" if e.install else ""))
        elif ch.action == "update":
            rows.append(f"  ~ fetch {e.target} <- {e.url} ({ch.reason})")
        elif ch.action == "replace":
            note = fsutil.aside_note(e.target, ch.reason)
            rows.append(f"  ~ replace {e.target} <- {e.url} ({note})")
        elif ch.action == "chmod":
            rows.append(f"  ~ chmod {e.target} -> {oct(e.mode)}")
        elif ch.action == "adopt":
            rows.append(f"  ~ adopt {e.target} (matches sha256)")
        if ch.action in ("fetch", "update", "replace"):
            rows += [f"    notify: {u}" for u in e.restart_units]
    rows += [f"  ~ keep {target} ({reason}; not removed)" for target, reason in p.keep]
    return rows + fsutil.describe_removals(p.errors, p.remove, [])


def targets(raw) -> List[str]:
    return [e.target for e in parse(raw)[0]]


def has_work(raw, state: Dict) -> bool:
    if raw is not None and not isinstance(raw, Mapping):
        return True
    return bool(raw or state.get("fetch", {}).get("installed"))
