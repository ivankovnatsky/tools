import hashlib
import os
import stat
import tempfile
from typing import Dict, List, Optional, Tuple

from tools.fsutil import fingerprint, removal_blocked, respell, same_file_as, same_identity
from tools.log import Color, debug, log
from tools.util import format_diff_bytes, looks_like_secret


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


def _removable(target: str, record) -> bool:
    """Only delete what still matches what we wrote."""
    record = record or {}
    if "ino" in record and not same_identity(target, record):
        return False
    stored_hash = record.get("hash")
    return not stored_hash or _file_hash(target) == stored_hash


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
        return int(value, 8)
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


def _resolve_entries(
    entries: List[Dict[str, object]], config_dir: str
) -> Tuple[List[Tuple[str, str, Optional[int], bool]], List[str]]:
    """Expand a `files:` config into a flat list of (target, source, mode, secrets) tuples.

    Two entry shapes are supported and may be mixed in the same list:
      - dir entry:  {dir: <path>, mode?: <octal>, secrets?: <bool>}
      - file entry: {source, target, mode?, secrets?}

    The ``secrets`` flag, when true, suppresses inline content diffs in
    both ``tools deploy`` and ``tools diff`` for that entry. On a dir
    entry it propagates to every file walked under that tree.

    File entries take precedence over dir entries on the same target.

    Returns (resolved, errors). `resolved` is deduped by target, last-write-wins.
    `errors` contains human-readable messages about malformed entries.
    """
    dir_pairs: List[Tuple[str, str, Optional[int], bool]] = []
    file_pairs: List[Tuple[str, str, Optional[int], bool]] = []
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

        secrets_raw = entry.get("secrets", False)
        if not isinstance(secrets_raw, bool):
            errors.append(
                f"`secrets` must be a bool, got {type(secrets_raw).__name__} in {entry!r}"
            )
            continue
        entry_secrets = secrets_raw

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
                    dir_pairs.append((target, source, entry_mode, entry_secrets))
            continue

        if "source" in entry and "target" in entry:
            source = os.path.join(config_dir, str(entry["source"]))
            target = os.path.expanduser(str(entry["target"]))
            if not os.path.isfile(source):
                errors.append(f"source not found: {entry['source']}")
                continue
            file_pairs.append((target, source, entry_mode, entry_secrets))
            continue

        errors.append(f"entry must have either `dir` or both `source` and `target`: {entry!r}")

    # Dedupe by target, later entries win on (source, mode) — but
    # `secrets` is sticky across every collision (dir/dir, dir/file,
    # file/file): once any entry marks a target as secret, the
    # resolved entry stays secret. Otherwise a later entry added just
    # to override the mode (or two overlapping `dir:` walks) would
    # silently downgrade the suppression flag and re-expose content
    # from a secret source.
    resolved_map: Dict[str, Tuple[str, Optional[int], bool]] = {}
    for target, source, mode, secrets in dir_pairs + file_pairs:
        prev_secrets = resolved_map.get(target, (None, None, False))[2]
        resolved_map[target] = (source, mode, secrets or prev_secrets)

    resolved = [
        (target, source, mode, secrets) for target, (source, mode, secrets) in resolved_map.items()
    ]
    return resolved, errors


def _conflict(target: str, conflicts: Optional[Dict[str, str]]) -> Optional[str]:
    if not conflicts:
        return None
    return conflicts.get(os.path.normpath(os.path.abspath(target)))


def install_files(
    entries: List[Dict[str, object]],
    config_dir: str,
    state: Dict,
    conflicts: Optional[Dict[str, str]] = None,
) -> bool:
    """Deploy files declared in the `files:` config section.

    See `_resolve_entries` for the supported entry shapes.

    Any legacy ``configFiles`` state from an earlier `tools` version is
    left untouched; nothing is auto-migrated, so previously deployed
    files are never deleted on upgrade. Users who want to retire an old
    target should re-declare it under ``files:`` then remove it.
    """
    success = True
    changed = False
    managed_targets: Dict[str, str] = {}

    resolved, errors = _resolve_entries(entries, config_dir)
    for err in errors:
        log(f"files: {err}", Color.RED)
        success = False
    declared = [target for target, _source, _mode, _secrets in resolved]
    # State keys match declared targets by file identity, not spelling.
    state_files = respell(dict(state.get("files", {}) or {}), declared)

    held: Dict[str, Dict] = {}
    for target, source, mode, secrets in resolved:
        reason = _conflict(target, conflicts)
        if reason:
            log(f"files: refusing {target}: {reason}", Color.RED)
            success = False
            if target in state_files:
                held[target] = state_files[target]
            continue
        result, source_hash = _copy_file(
            source, target, mode, secrets=secrets, tracked=_tracked(target, state_files.get(target))
        )
        if result is False:
            success = False
            if target in state_files:
                held[target] = state_files[target]
        elif result is True:
            changed = True

        if source_hash and result is not False:
            managed_targets[target] = source_hash

    # Only config errors hold removals: an unparsed entry looks undesired.
    if not errors:
        kept = dict(held)
        for target in list(state_files.keys()):
            if target not in managed_targets and target not in held:
                blocked = removal_blocked(conflicts, target)
                twin = same_file_as(target, declared)
                if twin:
                    blocked = f"the same file as declared {twin}"
                if blocked:
                    log(
                        f"files: not removing {target}: it is {blocked}; still tracked",
                        Color.YELLOW,
                    )
                    kept[target] = state_files[target]
                    continue
                if os.path.exists(target):
                    # A file edited since then holds work this tool never authored.
                    if not _removable(target, state_files.get(target)):
                        log(
                            f"Keeping {target}: modified since we wrote it. "
                            "Delete it by hand if it is no longer wanted.",
                            Color.YELLOW,
                        )
                        kept[target] = state_files[target]
                        continue
                    try:
                        os.remove(target)
                        log(f"Removed: {target}", Color.RED)
                        changed = True
                    except OSError as e:
                        log(f"Failed to remove {target}: {e}", Color.RED)
                        success = False
                        # Keep tracking it so the next run retries the cleanup.
                        kept[target] = state_files[target]

        state["files"] = {
            target: _record(target, file_hash) for target, file_hash in managed_targets.items()
        }
        state["files"].update(kept)
    else:
        merged = dict(state_files)
        merged.update(
            {target: _record(target, file_hash) for target, file_hash in managed_targets.items()}
        )
        state["files"] = merged

    if not changed and entries:
        debug("All files up to date", Color.BLUE)

    return success
