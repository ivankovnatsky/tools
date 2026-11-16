"""Single keys inside plist files an app owns, such as its config.plist.

Config shape::

    plist:
      ~/Library/Application Support/com.nuebling.mac-mouse-fix/config.plist:
        General:showMenuBarItem: false   # `:` separates nested dict keys

Only the declared keys change; the rest of the file, its format (XML or binary)
and its mode are kept. A file that does not exist yet is skipped: the app
creates it on first launch, and a later deploy sets the keys. Values are typed
as in `defaults`: `true` is not `1`.

Ownership, as in `defaults`: the first write of a key records its prior value
in state (keyed by path and key path). A key that leaves config while it still
holds the value tools wrote gets its prior value back, or is deleted if it did
not exist before tools; a key changed by hand, or whose file is gone, is left
as is and forgotten.

For preferences domains under ~/Library/Preferences use `defaults`, which goes
through cfprefsd; this section writes the file directly, as the user.
"""

import base64
import copy
import os
import plistlib
import stat
from typing import Callable, Dict, List, Optional, Tuple

from tools import fsutil
from tools.log import Color, log
from tools.user.defaults import (
    MISSING,
    _removal,
    fmt_value,
    from_xml,
    same,
    to_xml,
    validate_value,
    xml_error,
)
from tools.util import SKIPPED_ROW


def parse(raw) -> Tuple[Dict[str, List[Tuple[List[str], object]]], List[str]]:
    """Validate `plist`. Returns ({absolute path: [(key path, value)]}, errors); any error
    holds the whole section."""
    if raw is None:
        return {}, []
    if not isinstance(raw, dict):
        return {}, [f"plist must be a mapping, got {type(raw).__name__}"]
    spec: Dict[str, List[Tuple[List[str], object]]] = {}
    errors: List[str] = []
    for raw_path, keys in raw.items():
        if not isinstance(raw_path, str) or not raw_path.strip():
            errors.append(f"path must be a non-empty string, got {raw_path!r}")
            continue
        path = os.path.normpath(os.path.expanduser(raw_path.strip()))
        if not os.path.isabs(path):
            errors.append(f"{raw_path}: must be an absolute or ~ path")
            continue
        if path in spec:
            errors.append(f"{raw_path}: declared more than once")
            continue
        if not isinstance(keys, dict) or not keys:
            errors.append(f"{raw_path}: must map key path -> value")
            continue
        entries = []
        for key, value in keys.items():
            where = f"{raw_path} {key}"
            parts = key.split(":") if isinstance(key, str) else []
            if not parts or not all(parts):
                errors.append(f"{where}: key path must be non-empty names joined by ':'")
                continue
            err = validate_value(value, where) or xml_error(value, where)
            if err:
                errors.append(err)
                continue
            overlap = next(
                (p for p, _ in entries if p[: len(parts)] == parts or parts[: len(p)] == p),
                None,
            )
            if overlap is not None:
                errors.append(f"{where}: overlaps {':'.join(overlap)}; declare one of them")
                continue
            entries.append((parts, value))
        spec[path] = entries
    return ({} if errors else spec), errors


def _get(data, parts: List[str]):
    for part in parts:
        if not isinstance(data, dict) or part not in data:
            return MISSING
        data = data[part]
    return data


def _parent_error(data, parts: List[str]) -> Optional[str]:
    for i, part in enumerate(parts[:-1]):
        if part not in data:
            return None
        data = data[part]
        if not isinstance(data, dict):
            return f"{':'.join(parts[: i + 1])} is not a dict"
    return None


def _set(data: dict, parts: List[str], value) -> Optional[str]:
    """Set parts in data, creating missing dicts; why not, when a parent is not a dict."""
    for i, part in enumerate(parts[:-1]):
        child = data.setdefault(part, {})
        if not isinstance(child, dict):
            return f"{':'.join(parts[: i + 1])} is not a dict"
        data = child
    data[parts[-1]] = value
    return None


def _delete(data: dict, parts: List[str]) -> None:
    parent = _get(data, parts[:-1]) if parts[:-1] else data
    if isinstance(parent, dict):
        parent.pop(parts[-1], None)


def _remove(data: dict, removals: List[Tuple[List[str], object]]) -> Optional[str]:
    """Restore each key to its prior value (delete it when MISSING); why not, if one failed."""
    for parts, prior in removals:
        if prior is MISSING:
            _delete(data, parts)
            continue
        why = _set(data, parts, prior)
        if why:
            return why
    return None


_BINARY = "bplist-base64:"


def _encode(value) -> str:
    """to_xml, or base64 binary for values XML cannot hold (UID, None)."""
    try:
        return to_xml(value)
    except (TypeError, ValueError, OverflowError):
        return _BINARY + base64.b64encode(plistlib.dumps(value, fmt=plistlib.FMT_BINARY)).decode()


def _decode(text: Optional[str]):
    if text is not None and text.startswith(_BINARY):
        return plistlib.loads(base64.b64decode(text[len(_BINARY) :]))
    return from_xml(text)


def _load(path: str) -> Tuple[dict, plistlib.PlistFormat]:
    with open(path, "rb") as f:
        raw = f.read()
    fmt = plistlib.FMT_BINARY if raw.startswith(b"bplist") else plistlib.FMT_XML
    try:
        data = plistlib.loads(raw)
    except Exception as e:
        raise ValueError(f"not a plist: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("top level is not a dict")
    return data, fmt


def _managed(state: Dict) -> Dict:
    return (state.get("plist") or {}).get("managed") or {}


def plan(spec: Dict, state: Optional[Dict] = None) -> Dict[str, list]:
    """Read-only. "set": {path: [(key path, current, value)]}; "restore": {path: [(key
    path, prior)]} for owned keys that left config (prior MISSING deletes); "forget":
    [(path, key path, reason)]; plus "missing" files and read "errors"."""
    managed = _managed(state or {})
    result: Dict = {
        "set": {},
        "refresh": [],  # (path, key, value): already equal, state is stale
        "restore": {},
        "forget": [],
        "missing": [],
        "errors": [],
    }
    cache: Dict[str, object] = {}

    def load(path: str):
        if path not in cache:
            if not os.path.lexists(path):
                cache[path] = None
            else:
                try:
                    cache[path] = _load(path)[0]
                except (OSError, ValueError, plistlib.InvalidFileException) as e:
                    cache[path] = e
                    result["errors"].append(f"cannot read {path}: {e}")
        return cache[path]

    desired = {(path, ":".join(parts)) for path, entries in spec.items() for parts, _ in entries}
    for path, keys in sorted(managed.items()):
        for key, entry in sorted((keys or {}).items()):
            if (path, key) in desired:
                continue
            data = load(path)
            if isinstance(data, Exception):
                continue
            parts = key.split(":")
            cur = MISSING if data is None else _get(data, parts)
            prior = _decode((entry or {}).get("prior"))
            ours = [from_xml((entry or {}).get(k)) for k in ("written", "confirmed")]
            reason = "file is gone; left as is" if data is None else _removal(cur, prior, ours)
            if reason:
                result["forget"].append((path, parts, reason))
            else:
                result["restore"].setdefault(path, []).append((parts, prior))
    for path, entries in spec.items():
        data = load(path)
        if data is None:
            result["missing"].append(path)
            continue
        if isinstance(data, Exception):
            continue
        if path in result["restore"]:
            data = copy.deepcopy(data)
            _remove(data, result["restore"][path])
        for parts, value in entries:
            why = _parent_error(data, parts)
            if why:
                result["errors"].append(f"{path} {':'.join(parts)}: {why}")
                continue
            cur = _get(data, parts)
            if cur is MISSING or not same(cur, value):
                result["set"].setdefault(path, []).append((parts, cur, value))
                continue
            entry = (managed.get(path) or {}).get(":".join(parts))
            want = to_xml(value)
            if entry is not None and (
                entry.get("written") != want or entry.get("confirmed", want) != want
            ):
                result["refresh"].append((path, ":".join(parts), value))

    return result


def _row(path: str, parts: List[str], cur, value) -> str:
    key = ":".join(parts)
    if cur is MISSING:
        return f"  + write {path} {key} = {fmt_value(value)}"
    return f"  ~ write {path} {key}: {fmt_value(cur)} -> {fmt_value(value)}"


def _restore_row(path: str, parts: List[str], prior) -> str:
    key = ":".join(parts)
    if prior is MISSING:
        return f"  - delete {path} {key}"
    return f"  - restore {path} {key} -> {fmt_value(prior)}"


def diff_plist(raw, state: Optional[Dict] = None) -> List[str]:
    spec, errors = parse(raw)
    if errors:
        return [f"  ! {err}" for err in errors]
    p = plan(spec, state)
    rows = [f"  ? {err}" for err in p["errors"]]
    rows += [f"{SKIPPED_ROW}{path} (missing; set once the app creates it)" for path in p["missing"]]
    for path, changes in p["set"].items():
        rows += [_row(path, *change) for change in changes]
    for path, removals in p["restore"].items():
        rows += [_restore_row(path, *removal) for removal in removals]
    rows += [
        f"  ~ forget {path} {':'.join(parts)} ({reason})"
        for path, parts, reason in p["forget"]
        if reason != "already gone"
    ]
    return rows


def _slot(managed: Dict, path: str, key: str) -> Optional[Dict]:
    return (managed.get(path) or {}).get(key)


def _drop(managed: Dict, path: str, key: str) -> None:
    keys = managed.get(path) or {}
    keys.pop(key, None)
    if not keys:
        managed.pop(path, None)


def install_plist(
    raw, state: Optional[Dict] = None, persist: Callable[[], None] = lambda: None
) -> bool:
    """`persist` saves state; it runs before each file write so `written` is on disk first."""
    spec, errors = parse(raw)
    for err in errors:
        log(f"plist: {err}", Color.RED)
    if errors:
        return False
    state = {} if state is None else state
    p = plan(spec, state)
    success = not p["errors"]
    for err in p["errors"]:
        log(f"plist: {err}", Color.RED)
    for path in p["missing"]:
        log(f"plist: {path} does not exist yet; skipping", Color.YELLOW)
    managed = state.setdefault("plist", {}).setdefault("managed", {})

    for path in sorted(set(p["set"]) | set(p["restore"])):
        changes = p["set"].get(path, [])
        removals = p["restore"].get(path, [])
        saved = {}
        for parts, cur, value in changes:
            key = ":".join(parts)
            slot = _slot(managed, path, key)
            saved[key] = None if slot is None else dict(slot)
            if slot is None:
                slot = managed.setdefault(path, {})[key] = {
                    "prior": None if cur is MISSING else _encode(cur),
                    "written": None,
                }
            before = slot["written"]
            if before is not None and same(cur, from_xml(before)):
                slot["confirmed"] = before
            slot.setdefault("confirmed", before)
            slot["written"] = to_xml(value)
        if changes:
            persist()
        try:
            st = os.stat(path)
            data, fmt = _load(path)
            why = _remove(data, removals)
            if why:
                raise ValueError(why)
            for parts, _cur, value in changes:
                why = _set(data, parts, value)
                if why:
                    raise ValueError(why)
            fsutil.atomic_write(
                os.path.realpath(path), plistlib.dumps(data, fmt=fmt), stat.S_IMODE(st.st_mode)
            )
        except (OSError, ValueError, plistlib.InvalidFileException) as e:
            log(f"plist: {path}: {e}", Color.RED)
            success = False
            for key, old in saved.items():
                if old is None:
                    _drop(managed, path, key)
                else:
                    managed[path][key] = old
            continue
        for parts, _cur, value in changes:
            slot = managed[path][":".join(parts)]
            slot["confirmed"] = slot["written"]
            log(f"Wrote: {path} {':'.join(parts)} = {fmt_value(value)}", Color.GREEN)
        for parts, prior in removals:
            _drop(managed, path, ":".join(parts))
            if prior is MISSING:
                log(f"Deleted: {path} {':'.join(parts)}", Color.YELLOW)
            else:
                log(f"Restored: {path} {':'.join(parts)} = {fmt_value(prior)}", Color.YELLOW)

    for path, key, value in p["refresh"]:
        managed[path][key]["written"] = managed[path][key]["confirmed"] = to_xml(value)

    for path, parts, reason in p["forget"]:
        if reason != "already gone":
            log(f"plist: releasing {path} {':'.join(parts)}: {reason}", Color.YELLOW)
        _drop(managed, path, ":".join(parts))
    if not managed:
        state.pop("plist", None)
    return success


def has_work(raw, state: Optional[Dict] = None) -> bool:
    return raw is not None or bool(_managed(state or {}))
