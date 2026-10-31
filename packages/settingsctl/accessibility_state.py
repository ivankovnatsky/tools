"""Accessibility state-file persistence and declarative enable/set orchestrators."""

from __future__ import annotations

import sys
from pathlib import Path

from accessibility_ui import (
    ACCESSIBILITY_URL,
    accessibility_list,
    accessibility_remove,
)
from common import _get_state_dir, quit_app
from privacy_pane import ensure_enabled


def accessibility_state_file() -> Path:
    return _get_state_dir() / "accessibility-enabled"


def accessibility_state_matches(enable_apps: list[str]) -> bool:
    """Check if state file matches the requested app list."""
    state = accessibility_state_file()
    if not state.exists():
        return False
    return state.read_text().strip() == ",".join(sorted(enable_apps))


def accessibility_write_state(enable_apps: list[str]) -> None:
    state = accessibility_state_file()
    state.write_text(",".join(sorted(enable_apps)))


def accessibility_previous_state() -> list[str]:
    state = accessibility_state_file()
    if not state.exists():
        return []
    raw = state.read_text().strip()
    if not raw:
        return []
    return [a.strip() for a in raw.split(",") if a.strip()]


def accessibility_enable(
    apps: dict[str, str | None], add_missing: bool = False
) -> None:
    """Ensure apps are enabled in Accessibility; add_missing adds unlisted ones."""
    names = list(apps)
    if not add_missing and accessibility_state_matches(names):
        print("Skipping accessibility enable (already configured)")
        return

    if ensure_enabled(ACCESSIBILITY_URL, "accessibility", apps, add_missing):
        try:
            accessibility_write_state(names)
        except Exception as e:
            print(f"Warning: could not write state: {e}", file=sys.stderr)


def accessibility_set(apps: dict[str, str | None], add_missing: bool = False) -> None:
    """Sync accessibility permissions to exactly target_apps (declarative).

    Removes apps previously managed by us (per state file) that are no longer
    in target_apps, then enables target_apps. User-added entries are preserved.
    """
    target_apps = list(apps)
    if not add_missing and accessibility_state_matches(target_apps):
        print("Skipping accessibility set (already configured)")
        return

    target = set(target_apps)
    previously_managed = set(accessibility_previous_state())
    to_remove = previously_managed - target

    if to_remove:
        current = {item["name"] for item in accessibility_list()}
        for app in sorted(to_remove):
            if app not in current:
                continue
            if accessibility_remove(app):
                print(f"Removed accessibility entry: {app}")
                if quit_app(app):
                    print(f"Quit running app: {app}")
            else:
                print(
                    f"Could not remove accessibility entry: {app}",
                    file=sys.stderr,
                )

    accessibility_enable(apps, add_missing)
