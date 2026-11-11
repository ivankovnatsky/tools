"""Single keys inside plist files an app owns, such as its config.plist.

Config shape::

    plist:
      ~/Library/Application Support/com.nuebling.mac-mouse-fix/config.plist:
        General:showMenuBarItem: false   # `:` separates nested dict keys

Only the declared keys change; the rest of the file, its format (XML or binary)
and its mode are kept. A file that does not exist yet is skipped: the app
creates it on first launch, and a later deploy sets the keys. A key that leaves
config is left as it is. Values are typed as in `defaults`: `true` is not `1`.

For preferences domains under ~/Library/Preferences use `defaults`, which goes
through cfprefsd; this section writes the file directly, as the user.
"""

import os
import plistlib
import stat
from typing import Dict, List, Optional, Tuple

from tools import fsutil
from tools.log import Color, log
from tools.user.defaults import fmt_value, same, validate_value, xml_error
from tools.util import SKIPPED_ROW

_MISSING = object()


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
            return _MISSING
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


def plan(spec: Dict) -> Tuple[Dict[str, list], List[str], List[str]]:
    """({path: [(key path, current, value)]} to write, missing files, errors); reads only."""
    todo: Dict[str, list] = {}
    missing: List[str] = []
    errors: List[str] = []
    for path, entries in spec.items():
        if not os.path.lexists(path):
            missing.append(path)
            continue
        try:
            data, _ = _load(path)
        except (OSError, ValueError, plistlib.InvalidFileException) as e:
            errors.append(f"cannot read {path}: {e}")
            continue
        for parts, value in entries:
            why = _parent_error(data, parts)
            if why:
                errors.append(f"{path} {':'.join(parts)}: {why}")
                continue
            cur = _get(data, parts)
            if cur is _MISSING or not same(cur, value):
                todo.setdefault(path, []).append((parts, cur, value))
    return todo, missing, errors


def _row(path: str, parts: List[str], cur, value) -> str:
    key = ":".join(parts)
    if cur is _MISSING:
        return f"  + write {path} {key} = {fmt_value(value)}"
    return f"  ~ write {path} {key}: {fmt_value(cur)} -> {fmt_value(value)}"


def diff_plist(raw) -> List[str]:
    spec, errors = parse(raw)
    if errors:
        return [f"  ! {err}" for err in errors]
    todo, missing, read_errors = plan(spec)
    rows = [f"  ? {err}" for err in read_errors]
    rows += [f"{SKIPPED_ROW}{path} (missing; set once the app creates it)" for path in missing]
    for path, changes in todo.items():
        rows += [_row(path, *change) for change in changes]
    return rows


def install_plist(raw) -> bool:
    spec, errors = parse(raw)
    for err in errors:
        log(f"plist: {err}", Color.RED)
    if errors:
        return False
    todo, missing, read_errors = plan(spec)
    success = not read_errors
    for err in read_errors:
        log(f"plist: {err}", Color.RED)
    for path in missing:
        log(f"plist: {path} does not exist yet; skipping", Color.YELLOW)
    for path, changes in todo.items():
        try:
            st = os.stat(path)
            data, fmt = _load(path)
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
            continue
        for parts, _cur, value in changes:
            log(f"Wrote: {path} {':'.join(parts)} = {fmt_value(value)}", Color.GREEN)
    return success


def has_work(raw) -> bool:
    return raw is not None
