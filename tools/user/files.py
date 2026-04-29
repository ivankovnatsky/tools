import hashlib
import os
import stat
import tempfile
from typing import Dict, List, Optional, Tuple

from tools.log import Color, debug, log


SKIP_DIRS = {".git", ".hg", ".svn", "__pycache__"}
SKIP_FILES = {".DS_Store", ".gitignore", ".gitkeep"}


def _parse_mode(value) -> Optional[int]:
    """Parse a mode value from config.

    Always interpreted as octal — both ``"0644"`` strings and unquoted
    YAML ``644`` ints map to ``0o644``. This avoids the trap where
    PyYAML loads ``mode: 644`` as decimal 644 (= ``0o1204``) and
    silently sets the wrong permission bits.

    Returns None when no mode was specified. Raises ValueError on a
    bad string and TypeError on any other type.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # Guard: bool is a subclass of int in Python.
        raise TypeError(f"files: mode must be an octal string or int, got bool")
    if isinstance(value, int):
        return int(str(value), 8)
    if isinstance(value, str):
        return int(value, 8)
    raise TypeError(
        f"files: mode must be an octal string or int, got {type(value).__name__}"
    )


def _copy_file(source: str, target: str, mode: Optional[int]) -> Tuple[Optional[bool], str]:
    """Copy source to target atomically, applying mode if provided.

    Returns (result, source_hash) where result is:
    True if written, False on error, None if up to date and mode matches.
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
        action = "Updated"
    else:
        action = "Created"

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
    return True, source_hash


def _resolve_entries(
    entries: List[Dict[str, object]], config_dir: str
) -> Tuple[List[Tuple[str, str, Optional[int]]], List[str]]:
    """Expand a `files:` config into a flat list of (target, source, mode) tuples.

    Two entry shapes are supported and may be mixed in the same list:
      - dir entry:  {dir: <path>, mode?: <octal>}        — walks tree, deploys under ~/
      - file entry: {source, target, mode?}              — explicit single file

    File entries take precedence over dir entries on the same target.

    Returns (resolved, errors). `resolved` is deduped by target, last-write-wins.
    `errors` contains human-readable messages about malformed entries.
    """
    dir_pairs: List[Tuple[str, str, Optional[int]]] = []
    file_pairs: List[Tuple[str, str, Optional[int]]] = []
    errors: List[str] = []
    home = os.path.expanduser("~")

    for entry in entries:
        try:
            entry_mode = _parse_mode(entry.get("mode"))
        except (TypeError, ValueError) as e:
            errors.append(f"invalid mode in entry {entry!r}: {e}")
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
                    dir_pairs.append((target, source, entry_mode))
            continue

        if "source" in entry and "target" in entry:
            source = os.path.join(config_dir, str(entry["source"]))
            target = os.path.expanduser(str(entry["target"]))
            if not os.path.isfile(source):
                errors.append(f"source not found: {entry['source']}")
                continue
            file_pairs.append((target, source, entry_mode))
            continue

        errors.append(
            f"entry must have either `dir` or both `source` and `target`: {entry!r}"
        )

    # Dedupe by target, file entries win.
    resolved_map: Dict[str, Tuple[str, Optional[int]]] = {}
    for target, source, mode in dir_pairs:
        resolved_map[target] = (source, mode)
    for target, source, mode in file_pairs:
        resolved_map[target] = (source, mode)

    resolved = [(target, source, mode) for target, (source, mode) in resolved_map.items()]
    return resolved, errors


def install_files(entries: List[Dict[str, object]], config_dir: str, state: Dict) -> bool:
    """Deploy files declared in the `files:` config section.

    See `_resolve_entries` for the supported entry shapes.

    Any legacy ``configFiles`` state from an earlier `tools` version is
    left untouched; nothing is auto-migrated, so previously deployed
    files are never deleted on upgrade. Users who want to retire an old
    target should re-declare it under ``files:`` then remove it.
    """
    state_files = state.get("files", {})
    success = True
    changed = False
    managed_targets: Dict[str, str] = {}

    resolved, errors = _resolve_entries(entries, config_dir)
    for err in errors:
        log(f"files: {err}", Color.RED)
        success = False

    for target, source, mode in resolved:
        result, source_hash = _copy_file(source, target, mode)
        if result is False:
            success = False
        elif result is True:
            changed = True

        if source_hash:
            managed_targets[target] = source_hash

    if success:
        for target in list(state_files.keys()):
            if target not in managed_targets:
                if os.path.exists(target):
                    try:
                        os.remove(target)
                        log(f"Removed: {target}", Color.RED)
                        changed = True
                    except OSError as e:
                        log(f"Failed to remove {target}: {e}", Color.RED)
                        success = False

        state["files"] = {
            target: {"hash": file_hash} for target, file_hash in managed_targets.items()
        }
    else:
        merged = dict(state_files)
        merged.update(
            {target: {"hash": file_hash} for target, file_hash in managed_targets.items()}
        )
        state["files"] = merged

    if not changed and entries:
        debug("All files up to date", Color.BLUE)

    return success
