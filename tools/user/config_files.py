import hashlib
import os
import stat
import tempfile
from typing import Dict, List, Optional, Tuple

from tools.log import Color, debug, log


def _file_hash(path: str) -> str:
    """SHA-256 hash of file contents, or empty string if unreadable."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


def _copy_file(source: str, target: str) -> Tuple[Optional[bool], str]:
    """Copy source to target atomically.

    Returns (result, source_hash) where result is:
    True if written, False on error, None if up to date.
    """
    try:
        with open(source, "rb") as f:
            desired = f.read()
    except OSError as e:
        log(f"Failed to read source {source}: {e}", Color.RED)
        return False, ""

    source_hash = hashlib.sha256(desired).hexdigest()

    if os.path.exists(target):
        try:
            with open(target, "rb") as f:
                existing = f.read()
        except OSError as e:
            log(f"Failed to read {target}: {e}", Color.RED)
            return False, source_hash

        if existing == desired:
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
            # Preserve source file permissions
            src_mode = stat.S_IMODE(os.stat(source).st_mode)
            os.chmod(tmp_path, src_mode)
            os.replace(tmp_path, target)
        except BaseException:
            os.unlink(tmp_path)
            raise
    except OSError as e:
        log(f"Failed to write {target}: {e}", Color.RED)
        return False, source_hash

    log(f"{action}: {target}", Color.GREEN)
    return True, source_hash


def install_config_files(config_files: List[Dict[str, str]], config_dir: str, state: Dict) -> bool:
    """Copy config files from directories to their target locations.

    Config format:
        configFiles:
          - dir: dotfiles
            type: dotfiles  # copies to ~/
    """
    state_files = state.get("configFiles", {})
    success = True
    changed = False
    managed_targets: Dict[str, str] = {}

    for entry in config_files:
        source_dir = os.path.join(config_dir, entry["dir"])
        file_type = entry.get("type", "dotfiles")

        if not os.path.isdir(source_dir):
            log(f"Source directory not found: {entry['dir']}", Color.RED)
            success = False
            continue

        if file_type == "dotfiles":
            target_base = os.path.expanduser("~")
        else:
            log(f"Unknown config file type: {file_type}", Color.RED)
            success = False
            continue

        _skip_dirs = {".git", ".hg", ".svn", "__pycache__"}
        _skip_files = {".DS_Store", ".gitignore", ".gitkeep"}
        for root, dirs, files in os.walk(source_dir):
            dirs[:] = [d for d in dirs if d not in _skip_dirs]
            for name in sorted(files):
                if name in _skip_files:
                    continue
                source = os.path.join(root, name)
                rel_path = os.path.relpath(source, source_dir)
                target = os.path.join(target_base, rel_path)

                result, source_hash = _copy_file(source, target)
                if result is False:
                    success = False
                elif result is True:
                    changed = True

                if source_hash:
                    managed_targets[target] = source_hash

    # Remove files previously managed but no longer in config.
    # Only run cleanup if all entries were processed successfully —
    # partial failures could leave managed_targets incomplete and
    # cause deployed files to be incorrectly deleted.
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

        state["configFiles"] = {
            target: {"hash": file_hash} for target, file_hash in managed_targets.items()
        }
    else:
        # On failure, preserve existing state and add any successfully managed files
        merged = dict(state_files)
        merged.update(
            {target: {"hash": file_hash} for target, file_hash in managed_targets.items()}
        )
        state["configFiles"] = merged

    if not changed and config_files:
        debug("All config files up to date", Color.BLUE)

    return success
