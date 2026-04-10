import json
import os
import shutil
from typing import Dict, List

from tools.log import Color, log

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
    """Remove path if it is a directory (created by mistake)."""
    if os.path.isdir(path):
        log(f"Removing directory at {path} (should be a file)", Color.YELLOW)
        shutil.rmtree(path)


def load_json(path: str) -> Dict:
    _remove_if_dir(path)
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)


def save_json(path: str, data: Dict):
    _remove_if_dir(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


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
