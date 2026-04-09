import json
import os
import shutil
from typing import Dict

from tools.log import Color, log

LEGACY_STATE_DIRS = [
    "manual-packages",  # Original name, renamed to "tools" in 2026-01
]


def load_json(path: str) -> Dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)


def save_json(path: str, data: Dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def migrate_state_file(new_state_file: str):
    """Migrate state file from legacy locations to current location."""
    if os.path.exists(new_state_file):
        return

    # Get the base directory pattern: ~/.config/home-manager/<name>/state.json
    # We replace the current dir name with each legacy name to check
    state_dir = os.path.dirname(new_state_file)
    parent_dir = os.path.dirname(state_dir)
    state_filename = os.path.basename(new_state_file)

    for legacy_dir in LEGACY_STATE_DIRS:
        old_state_file = os.path.join(parent_dir, legacy_dir, state_filename)
        if os.path.exists(old_state_file):
            log(
                f"Migrating state file from {old_state_file} to {new_state_file}",
                Color.YELLOW,
            )
            os.makedirs(os.path.dirname(new_state_file), exist_ok=True)
            shutil.copy2(old_state_file, new_state_file)
            log("State file migrated successfully", Color.GREEN)
            return
