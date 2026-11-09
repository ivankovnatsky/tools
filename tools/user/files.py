import hashlib
import os
import stat
import tempfile
from typing import Dict, List, NamedTuple, Optional, Tuple

from tools.fsutil import (
    canonical_path,
    fingerprint,
    makedirs_secret,
    removal_blocked,
    respell,
    same_file_as,
    same_identity,
)
from tools.log import Color, debug, log
from tools.util import (
    format_diff_bytes,
    looks_like_secret,
    parse_on_change,
    run_hooks,
    settle_hooks,
)


def _file_hash(path: str) -> Optional[str]:
    """sha256 of a file on disk, or None if it cannot be read."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _tracked(target: str, record) -> bool:
    """Whether ``target`` is still the file ``record`` describes.

    Records without an inode predate identity tracking; for those the
    content hash stands in.
    """
    if not isinstance(record, dict):
        return False
    if "ino" in record:
        return same_identity(target, record)
    return bool(record.get("hash")) and _file_hash(target) == record["hash"]


def _record(target: str, file_hash: str) -> Dict:
    fp = fingerprint(target) or {}
    return {"hash": file_hash, **{k: fp[k] for k in ("dev", "ino") if k in fp}}


# Section move (secrets_flow.move_between_sections): every file-writing
# section defines these three.
def record_unmodified(target: str, record) -> bool:
    return (
        isinstance(record, dict)
        and bool(record.get("hash"))
        and (_file_hash(target) == record["hash"])
    )


def record_for(target: str, name: str, secret: bool) -> Dict:
    return _record(target, _file_hash(target))


def record_secret(record) -> bool:
    return False


def _removable(target: str, record) -> bool:
    """Only delete what still matches what we wrote."""
    record = record or {}
    if "ino" in record and not same_identity(target, record):
        return False
    stored_hash = record.get("hash")
    return not stored_hash or _file_hash(target) == stored_hash


def _is_real_dir(path: str) -> bool:
    # normpath: with a trailing slash, islink follows the link.
    path = os.path.normpath(path)
    return os.path.isdir(path) and not os.path.islink(path)


def _dir_record(target: str) -> Dict:
    fp = fingerprint(target) or {}
    return {"dir": True, **{k: fp[k] for k in ("dev", "ino") if k in fp}}


def _released_dir(target: str, conflicts) -> bool:
    keys = getattr(conflicts, "release_dirs", None)
    return bool(keys) and canonical_path(os.path.abspath(os.path.expanduser(target))) in keys


def _same_dir(target: str, record) -> bool:
    """Whether ``target`` is the directory ``record`` was taken from.

    A directory's mtime moves with its entries, so unlike files a changed
    device number (APFS remount) is accepted on the inode alone.
    """
    if not isinstance(record, dict) or not record.get("dir") or "ino" not in record:
        return False
    fp = fingerprint(os.path.normpath(target))
    return _is_real_dir(target) and fp is not None and fp["ino"] == record["ino"]


def _dir_kept(target: str, record) -> Optional[str]:
    """Why a tracked directory no longer declared stays; None to remove it."""
    if not _same_dir(target, record):
        return "not the directory tools created"
    try:
        if os.listdir(target):
            return "not empty"
    except OSError as e:
        return f"unreadable ({e})"
    return None


def _ensure_dir(
    target: str,
    mode: Optional[int],
    record,
    private_root: Optional[str] = None,
    created_dirs: Optional[list] = None,
) -> Tuple[Optional[bool], Optional[Dict]]:
    """Create ``target`` (and parents) if missing, then apply ``mode``.

    Returns (result, record): result as in `_copy_file`; record is set only
    for a directory tools created, so removal never touches one it did not.
    Directories under ``private_root`` (the secretsDir) are made like sops'
    and appended to ``created_dirs``.
    """
    if os.path.lexists(target) and not _is_real_dir(target):
        log(f"files: {target} exists and is not a directory", Color.RED)
        return False, None
    tracked = _same_dir(target, record)
    result = None
    try:
        if not os.path.lexists(target):
            makedirs_secret(target, private_root, created_dirs)
            if mode is not None:
                os.chmod(target, mode)
            log(f"Created directory: {target}", Color.GREEN)
            return True, _dir_record(target)
        if mode is not None and stat.S_IMODE(os.lstat(target).st_mode) != mode:
            os.chmod(target, mode, follow_symlinks=False)
            log(f"Chmod: {target} -> {oct(mode)}", Color.GREEN)
            result = True
    except OSError as e:
        log(f"Failed to create {target}: {e}", Color.RED)
        return False, record if tracked else None
    return result, _dir_record(target) if tracked else None


SKIP_DIRS = {".git", ".hg", ".svn", "__pycache__"}
SKIP_FILES = {".DS_Store", ".gitignore", ".gitkeep"}


def _parse_mode(value) -> Optional[int]:
    """Parse a mode value from config.

    Only quoted octal strings are accepted (``mode: "0644"``). Bare YAML
    ints are rejected because they are ambiguous: PyYAML (YAML 1.1)
    parses ``0644`` as octal into int 420, which is indistinguishable
    from a literal ``420`` — re-interpreting either as octal silently
    yields the wrong permission bits for one of them.

    Returns None when no mode was specified. Raises ValueError on a
    bad string and TypeError on any other type.
    """
    if value is None:
        return None
    if isinstance(value, str):
        mode = int(value, 8)
        if not 0 <= mode <= 0o7777:
            raise ValueError(f"files: mode {value!r} must be between 0000 and 7777")
        return mode
    raise TypeError(
        f'files: mode must be a quoted octal string (e.g. "0644"), got {value!r} '
        f"({type(value).__name__}); unquoted YAML ints are ambiguous"
    )


def _copy_file(
    source: str,
    target: str,
    mode: Optional[int],
    *,
    secrets: bool = False,
    tracked: bool = True,
) -> Tuple[Optional[bool], str]:
    """Copy source to target atomically, applying mode if provided.

    Returns (result, source_hash) where result is:
    True if written, False on error, None if up to date and mode matches.

    When ``secrets`` is true — or either side (incoming or existing)
    trips the ``looks_like_secret`` heuristic — the inline diff body is
    suppressed so credentials do not bleed into deploy/activation logs.
    Both sides are checked because a rotation that *removes* a secret
    from the file would otherwise leak the old value.
    """
    try:
        with open(source, "rb") as f:
            desired = f.read()
    except OSError as e:
        log(f"Failed to read source {source}: {e}", Color.RED)
        return False, ""

    source_hash = hashlib.sha256(desired).hexdigest()
    desired_mode = mode if mode is not None else stat.S_IMODE(os.stat(source).st_mode)

    if os.path.exists(target):
        try:
            with open(target, "rb") as f:
                existing = f.read()
        except OSError as e:
            log(f"Failed to read {target}: {e}", Color.RED)
            return False, source_hash

        existing_mode = stat.S_IMODE(os.stat(target).st_mode)
        if existing == desired and existing_mode == desired_mode:
            return None, source_hash
        action = "Updated" if tracked else "Replaced untracked"
        is_secret = secrets or looks_like_secret(desired) or looks_like_secret(existing)
        # Content tools never wrote is never shown: it may hold hand-written secrets.
        if is_secret or not tracked:
            diff_body = None
        else:
            diff_body = format_diff_bytes(desired, existing, target)
    else:
        action = "Created"
        diff_body = None

    try:
        dirname = os.path.dirname(target)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=dirname or ".")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(desired)
            os.chmod(tmp_path, desired_mode)
            os.replace(tmp_path, target)
        except BaseException:
            os.unlink(tmp_path)
            raise
    except OSError as e:
        log(f"Failed to write {target}: {e}", Color.RED)
        return False, source_hash

    log(f"{action}: {target}", Color.GREEN)
    if diff_body:
        print(diff_body)
    return True, source_hash


class Entry(NamedTuple):
    target: str
    source: Optional[str]  # None for a directory entry
    mode: Optional[int]
    secrets: bool
    on_change: Optional[str] = None
    directory: bool = False
    on_update: Optional[str] = None


def declared_hooks(entries) -> set:
    """Every onChange in the raw config, including entries that fail to resolve."""
    if not isinstance(entries, list):
        return set()
    return {
        e["onChange"] for e in entries if isinstance(e, dict) and isinstance(e.get("onChange"), str)
    }


def _ordered(resolved: List[Entry]) -> List[Entry]:
    """Directory entries first, parents before children, so a directory exists
    (and is tracked as created) before anything is written inside it."""
    return sorted(
        resolved,
        key=lambda e: (not e.directory, e.target.count(os.sep) if e.directory else 0),
    )


def _removal_order(targets, records: Dict) -> List[str]:
    """Files before directories, deepest directory first."""

    def is_dir(t):
        return isinstance(records.get(t), dict) and bool(records[t].get("dir"))

    return sorted(targets, key=lambda t: (is_dir(t), -t.count(os.sep) if is_dir(t) else 0))


def _resolve_entries(
    entries: List[Dict[str, object]], config_dir: str
) -> Tuple[List[Entry], List[str]]:
    """Expand a `files:` config into a flat list of `Entry`.

    Three entry shapes are supported and may be mixed in the same list:
      - dir entry:       {dir: <path>, mode?, secrets?, onChange?, onUpdate?}
      - file entry:      {source, target, mode?, secrets?, onChange?, onUpdate?}
      - directory entry: {target, directory: true, mode?, onChange?, onUpdate?}

    The ``secrets`` flag, when true, suppresses inline content diffs in
    both ``tools deploy`` and ``tools diff`` for that entry. On a dir
    entry it propagates to every file walked under that tree.

    File and directory entries take precedence over dir entries on the same target.

    Returns (resolved, errors). `resolved` is deduped by target, last-write-wins.
    `errors` contains human-readable messages about malformed entries.
    """
    dir_pairs: List[Entry] = []
    file_pairs: List[Entry] = []
    errors: List[str] = []
    home = os.path.expanduser("~")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        return [], [f"files must be a list of entries, got {type(entries).__name__}"]

    for entry in entries:
        if not isinstance(entry, dict):
            errors.append(f"entry must be a mapping: {entry!r}")
            continue
        try:
            entry_mode = _parse_mode(entry.get("mode"))
        except (TypeError, ValueError) as e:
            errors.append(f"invalid mode in entry {entry!r}: {e}")
            continue
        try:
            on_change = parse_on_change(entry.get("onChange"), f"entry {entry!r}")
            on_update = parse_on_change(entry.get("onUpdate"), f"entry {entry!r}", "onUpdate")
        except ValueError as e:
            errors.append(str(e))
            continue

        secrets_raw = entry.get("secrets", False)
        if not isinstance(secrets_raw, bool):
            errors.append(
                f"`secrets` must be a bool, got {type(secrets_raw).__name__} in {entry!r}"
            )
            continue
        entry_secrets = secrets_raw

        directory = entry.get("directory", False)
        if not isinstance(directory, bool):
            errors.append(f"`directory` must be a bool in {entry!r}")
            continue
        if directory:
            if "target" not in entry or "source" in entry or "dir" in entry:
                errors.append(f"a directory entry takes `target` and no `source`/`dir`: {entry!r}")
                continue
            target = os.path.normpath(os.path.expanduser(str(entry["target"])))
            file_pairs.append(Entry(target, None, entry_mode, False, on_change, True, on_update))
            continue

        if "dir" in entry:
            source_dir = os.path.join(config_dir, str(entry["dir"]))
            if not os.path.isdir(source_dir):
                errors.append(f"source directory not found: {entry['dir']}")
                continue
            for root, dirs, names in os.walk(source_dir):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for name in sorted(names):
                    if name in SKIP_FILES:
                        continue
                    source = os.path.join(root, name)
                    rel = os.path.relpath(source, source_dir)
                    target = os.path.join(home, rel)
                    dir_pairs.append(
                        Entry(
                            target, source, entry_mode, entry_secrets, on_change, False, on_update
                        )
                    )
            continue

        if "source" in entry and "target" in entry:
            source = os.path.join(config_dir, str(entry["source"]))
            target = os.path.normpath(os.path.expanduser(str(entry["target"])))
            if not os.path.isfile(source):
                errors.append(f"source not found: {entry['source']}")
                continue
            file_pairs.append(
                Entry(target, source, entry_mode, entry_secrets, on_change, False, on_update)
            )
            continue

        errors.append(f"entry must have either `dir` or both `source` and `target`: {entry!r}")

    # Dedupe by target, later entries win — but `secrets` is sticky across
    # every collision (dir/dir, dir/file, file/file): once any entry marks a
    # target as secret, the resolved entry stays secret. Otherwise a later
    # entry added just to override the mode (or two overlapping `dir:` walks)
    # would silently downgrade the suppression flag and re-expose content
    # from a secret source.
    resolved_map: Dict[str, Entry] = {}
    for e in dir_pairs + file_pairs:
        prev = resolved_map.get(e.target)
        resolved_map[e.target] = e._replace(secrets=e.secrets or bool(prev and prev.secrets))
    return list(resolved_map.values()), errors


def _conflict(target: str, conflicts: Optional[Dict[str, str]]) -> Optional[str]:
    if not conflicts:
        return None
    return conflicts.get(os.path.normpath(os.path.abspath(target)))


def install_files(
    entries: List[Dict[str, object]],
    config_dir: str,
    state: Dict,
    conflicts: Optional[Dict[str, str]] = None,
    private_root: Optional[str] = None,
    update: bool = False,
) -> bool:
    """Deploy files declared in the `files:` config section.

    With ``update``, each distinct ``onUpdate`` command runs once afterwards,
    changed or not, provided all its entries converged and onChange succeeded.
    Failures are not kept pending: the next update reruns them.

    ``private_root`` is the sops secretsDir: directory entries inside it are
    created with its mode and registered with the sops section's created dirs.

    See `_resolve_entries` for the supported entry shapes.

    Any legacy ``configFiles`` state from an earlier `tools` version is
    left untouched; nothing is auto-migrated, so previously deployed
    files are never deleted on upgrade. Users who want to retire an old
    target should re-declare it under ``files:`` then remove it.
    """
    success = True
    changed = False
    managed: Dict[str, Dict] = {}
    hooks: Dict[str, List[str]] = {}

    resolved, errors = _resolve_entries(entries, config_dir)
    for err in errors:
        log(f"files: {err}", Color.RED)
        success = False
    declared = [e.target for e in resolved]
    # State keys match declared targets by file identity, not spelling.
    state_files = respell(dict(state.get("files", {}) or {}), declared)

    held: Dict[str, Dict] = {}
    released = set()
    updates: Dict[str, List[str]] = {}
    unsettled = set()
    for e in _ordered(resolved):
        target = e.target
        reason = _conflict(target, conflicts)
        if reason:
            log(f"files: refusing {target}: {reason}", Color.RED)
            success = False
            if target in state_files:
                held[target] = state_files[target]
            if e.on_update:
                unsettled.add(e.on_update)
            continue
        if e.directory:
            created = state.setdefault("sops", {}).setdefault("createdDirs", [])
            result, record = _ensure_dir(
                target, e.mode, state_files.get(target), private_root, created
            )
            if record is not None:
                managed[target] = record
            elif result is not False:
                released.add(target)
        else:
            result, source_hash = _copy_file(
                e.source,
                target,
                e.mode,
                secrets=e.secrets,
                tracked=_tracked(target, state_files.get(target)),
            )
            if source_hash and result is not False:
                managed[target] = _record(target, source_hash)
        if e.on_update:
            if result is False:
                unsettled.add(e.on_update)
            else:
                updates.setdefault(e.on_update, []).append(target)
        if result is False:
            success = False
            if target in state_files:
                held[target] = state_files[target]
        elif result is True:
            changed = True
            if e.on_change:
                hooks.setdefault(e.on_change, []).append(target)

    # Only config errors hold removals: an unparsed entry looks undesired.
    if not errors:
        kept = dict(held)
        for target in _removal_order(state_files, state_files):
            if target not in managed and target not in held and target not in released:
                record = state_files.get(target)
                blocked = removal_blocked(conflicts, target)
                twin = same_file_as(target, declared)
                if twin:
                    blocked = f"the same file as declared {twin}"
                if blocked:
                    log(
                        f"files: not removing {target}: it is {blocked}; still tracked",
                        Color.YELLOW,
                    )
                    kept[target] = record
                    continue
                if not os.path.exists(target):
                    continue
                is_dir = isinstance(record, dict) and record.get("dir")
                if is_dir and _released_dir(target, conflicts):
                    log(
                        f"files: releasing {target}: another section manages a path inside",
                        Color.YELLOW,
                    )
                    continue
                if is_dir:
                    why = _dir_kept(target, record)
                    if why:
                        log(f"Keeping {target}: {why}.", Color.YELLOW)
                        kept[target] = record
                        continue
                # A file edited since then holds work this tool never authored.
                elif not _removable(target, record):
                    log(
                        f"Keeping {target}: modified since we wrote it. "
                        "Delete it by hand if it is no longer wanted.",
                        Color.YELLOW,
                    )
                    kept[target] = record
                    continue
                try:
                    if is_dir:
                        os.rmdir(os.path.normpath(target))
                    else:
                        os.remove(target)
                    log(f"Removed: {target}", Color.RED)
                    changed = True
                except OSError as e:
                    log(f"Failed to remove {target}: {e}", Color.RED)
                    success = False
                    # Keep tracking it so the next run retries the cleanup.
                    kept[target] = record

        state["files"] = dict(managed)
        state["files"].update(kept)
    else:
        merged = dict(state_files)
        merged.update(managed)
        state["files"] = merged

    settled = settle_hooks(state, "files", hooks, declared_hooks(entries))
    success &= settled

    if update:
        skipped = unsettled if settled else unsettled | set(updates)
        for command in sorted(skipped):
            log(f"files: onUpdate skipped (files or onChange failed): {command}", Color.YELLOW)
        runnable = {c: t for c, t in updates.items() if c not in skipped}
        if run_hooks("files", runnable, "onUpdate", "rerun on the next deploy --update"):
            success = False

    if not changed and entries:
        debug("All files up to date", Color.BLUE)

    return success
