import contextlib
import fcntl
import json
import os
import shutil
import tempfile
from typing import Dict, Iterator, List

from tools.log import Color, log

STATE_VERSION = 2

# Sections whose entries are deletion authority under the ownership model.
# Before v2 they recorded the *desired config* (brew, flatpak) or adopted
# preinstalled items (ollamaModels), so a legacy entry says nothing about
# who installed the package. Carrying it forward would let the first deploy
# after the upgrade uninstall something installed by hand.
_OWNERSHIP_SECTIONS = {
    "brew": ("brews", "casks", "taps", "masApps"),
    "flatpak": ("packages", "remotes"),
    "ollamaModels": ("installed",),
}

LEGACY_STATE_DIRS = [
    "manual-packages",  # Original name, renamed to "tools" in 2026-01
]

# Full legacy state file paths, tried in order. Used when the state
# file was relocated into a different parent directory (e.g. moving
# out of ~/.config/ into the XDG state dir).
# Each entry is expanded with os.path.expanduser at lookup time.
LEGACY_STATE_FILES: List[str] = [
    "~/.config/home-manager/tools/state.json",
    "~/.config/home-manager/manual-packages/state.json",
]


def _remove_if_dir(path: str):
    """Remove path if it is an empty directory (created by mistake)."""
    if os.path.isdir(path):
        log(f"Removing empty directory at {path} (should be a file)", Color.YELLOW)
        os.rmdir(path)


def load_json(path: str) -> Dict:
    # Read-only: a directory squatting where the file should be is repaired
    # by save_json/migrate_state_file, never by a read (diff must not write).
    if os.path.isdir(path):
        return {}
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)


def save_json(path: str, data: Dict):
    # Written atomically: this file is the only record of what we installed, and
    # a crash mid-dump would truncate it into a partial ownership set.
    _remove_if_dir(path)
    dirname = os.path.dirname(path)
    if dirname:
        # A bare relative filename has no parent to create.
        os.makedirs(dirname, exist_ok=True)
    # Unique per writer so concurrent saves never share a temp file; mkstemp
    # creates it 0600, and the state holds fingerprints of service secrets.
    fd, tmp_path = tempfile.mkstemp(
        dir=dirname or ".", prefix=f"{os.path.basename(path)}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_path)
        raise


DEFAULT_STATE_FILE = "~/.local/state/tools/state.json"

# Under sudo, root's records (system files, daemons, system defaults) live under this key
# of the user's state file.
ROOT_KEY = "root"


def state_file_path(config: Dict) -> str:
    # `or`, not a default arg: an explicit `stateFile:` (YAML null) must not crash expanduser.
    return os.path.expanduser(config.get("stateFile") or DEFAULT_STATE_FILE)


def root_records(state: Dict) -> Dict:
    """Root's records in a loaded user state, created empty when missing."""
    records = state.get(ROOT_KEY)
    if not isinstance(records, dict):
        records = state[ROOT_KEY] = {}
    return records


def lock_path(state_file: str) -> str:
    return f"{state_file}.lock"


@contextlib.contextmanager
def state_lock(state_file: str) -> Iterator[None]:
    """Hold an exclusive lock on state_file for a whole read-modify-write.

    Waits for another holder rather than failing; the kernel drops the lock
    when the process dies, so a crashed run never leaves it stuck.
    """
    path = lock_path(state_file)
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log(f"Waiting for another tools run to finish (lock {path})", Color.YELLOW)
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def migrate_state_schema(state: Dict) -> Dict:
    """Bring an on-disk state dict up to STATE_VERSION.

    v1 -> v2: ownership sections are cleared rather than reinterpreted.
    Packages stay installed; they are simply no longer removal candidates
    until this tool installs them itself.
    """
    found = state.get("version", 1)
    if found > STATE_VERSION:
        # Written by a newer tools; its sections may mean something else.
        # Silently downgrading would hand stale semantics deletion authority.
        raise RuntimeError(
            f"State file is version {found} but this tools understands {STATE_VERSION}. "
            "Upgrade tools, or remove the state file to start fresh."
        )
    if found == STATE_VERSION:
        return state

    dropped = []
    for section_name, keys in _OWNERSHIP_SECTIONS.items():
        section = state.get(section_name)
        if not isinstance(section, dict):
            continue
        for key in keys:
            value = section.get(key)
            if not value:
                continue
            dropped.append(f"{section_name}.{key}")
            section[key] = {} if isinstance(value, dict) else []

    if dropped:
        log(
            "State upgraded to ownership tracking; releasing prior entries so "
            f"nothing is removed on the strength of legacy data: {', '.join(sorted(dropped))}",
            Color.YELLOW,
        )
    state["version"] = STATE_VERSION
    return state


def find_state_file(new_state_file: str) -> str:
    """Return the path deploy would read after migration, without copying.

    Read-only counterpart to `migrate_state_file` for `tools diff`: a
    dry run must preview against the same state deploy will act on, but
    must not relocate anything on disk.
    """
    if os.path.isfile(new_state_file):
        return new_state_file

    state_dir = os.path.dirname(new_state_file)
    parent_dir = os.path.dirname(state_dir)
    state_filename = os.path.basename(new_state_file)

    for legacy_dir in LEGACY_STATE_DIRS:
        candidate = os.path.join(parent_dir, legacy_dir, state_filename)
        if os.path.isfile(candidate) and os.path.abspath(candidate) != os.path.abspath(
            new_state_file
        ):
            return candidate

    for legacy_template in LEGACY_STATE_FILES:
        candidate = os.path.expanduser(legacy_template)
        if os.path.isfile(candidate) and os.path.abspath(candidate) != os.path.abspath(
            new_state_file
        ):
            return candidate

    return new_state_file


def migrate_state_file(new_state_file: str):
    """Migrate state file from legacy locations to current location."""
    _remove_if_dir(new_state_file)
    if os.path.isfile(new_state_file):
        return

    # 1. Sibling-directory renames: same parent dir, different leaf name.
    state_dir = os.path.dirname(new_state_file)
    parent_dir = os.path.dirname(state_dir)
    state_filename = os.path.basename(new_state_file)

    for legacy_dir in LEGACY_STATE_DIRS:
        old_state_file = os.path.join(parent_dir, legacy_dir, state_filename)
        if os.path.exists(old_state_file) and os.path.abspath(old_state_file) != os.path.abspath(
            new_state_file
        ):
            _migrate(old_state_file, new_state_file)
            return

    # 2. Full-path relocations: file used to live somewhere else
    # entirely (e.g. ~/.config/home-manager/tools/state.json before the
    # move to ~/.local/state/tools/state.json).
    for legacy_template in LEGACY_STATE_FILES:
        old_state_file = os.path.expanduser(legacy_template)
        if os.path.exists(old_state_file) and os.path.abspath(old_state_file) != os.path.abspath(
            new_state_file
        ):
            _migrate(old_state_file, new_state_file)
            return


def _migrate(old_state_file: str, new_state_file: str):
    log(
        f"Migrating state file from {old_state_file} to {new_state_file}",
        Color.YELLOW,
    )
    os.makedirs(os.path.dirname(new_state_file), exist_ok=True)
    shutil.copy2(old_state_file, new_state_file)
    log("State file migrated successfully", Color.GREEN)
