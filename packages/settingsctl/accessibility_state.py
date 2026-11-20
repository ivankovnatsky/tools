"""Accessibility state-file persistence and declarative enable/set orchestrators."""

from __future__ import annotations

import sys
from pathlib import Path

from accessibility_ui import ACCESSIBILITY_URL, accessibility_remove
from common import _get_state_dir, quit_app, state_disabled
from privacy_pane import enable_apps, ensure_enabled, list_apps, partial_state


def accessibility_state_file() -> Path:
    return _get_state_dir() / "accessibility-enabled"


def accessibility_state_matches(enable_apps: list[str]) -> bool:
    """Check if state file matches the requested app list."""
    if state_disabled():
        return False
    state = accessibility_state_file()
    if not state.exists():
        return False
    return state.read_text().strip() == ",".join(sorted(enable_apps))


def accessibility_write_state(enable_apps: list[str]) -> None:
    state = accessibility_state_file()
    state.write_text(",".join(sorted(enable_apps)))


def accessibility_previous_state() -> list[str]:
    if state_disabled():
        return []
    state = accessibility_state_file()
    if not state.exists():
        return []
    raw = state.read_text().strip()
    if not raw:
        return []
    return [a.strip() for a in raw.split(",") if a.strip()]


def _record(names: list[str]) -> None:
    if state_disabled():
        return
    try:
        accessibility_write_state(names)
    except Exception as e:
        print(f"Warning: could not write state: {e}", file=sys.stderr)


def _remove_confirmed(apps: list[str]) -> dict[str, bool]:
    """Remove apps from the pane: {app: gone}.

    accessibility_remove is True only when the row is gone after the click (it
    re-checks the rows in the same System Settings session), so a click that
    did not remove the row never drops a grant from state.
    """
    gone = {}
    for app in sorted(apps):
        gone[app] = accessibility_remove(app)
        if not gone[app]:
            print(f"Could not remove accessibility entry: {app}", file=sys.stderr)
            continue
        print(f"Removed accessibility entry: {app}")
        if quit_app(app):
            print(f"Quit running app: {app}")
    return gone


def accessibility_enable(
    apps: dict[str, str | None], add_missing: bool = False
) -> bool:
    """Ensure apps are enabled in Accessibility; add_missing adds unlisted ones.

    Returns False when the pane could not be read or an app is not enabled.
    """
    names = list(apps)
    if not add_missing and accessibility_state_matches(names):
        print("Skipping accessibility enable (already configured)")
        return True
    if not ensure_enabled(ACCESSIBILITY_URL, "accessibility", apps, add_missing):
        return False
    _record(names)
    return True


def accessibility_set(apps: dict[str, str | None], add_missing: bool = False) -> bool:
    """Sync accessibility permissions to exactly target_apps (declarative).

    Removes apps previously managed by us (per state file) that are no longer
    in target_apps, then enables target_apps. User-added entries are preserved.
    On partial failure the state keeps every app still granted by us and leaves
    out the ones that failed, so the next run retries.
    """
    target_apps = list(apps)
    if not add_missing and accessibility_state_matches(target_apps):
        print("Skipping accessibility set (already configured)")
        return True

    ok = True
    previous = accessibility_previous_state()
    to_remove = set(previous) - set(target_apps)
    gone: dict[str, bool] = {}
    if to_remove:
        listed = list_apps(ACCESSIBILITY_URL)
        if listed is None:
            print("Could not read the accessibility list", file=sys.stderr)
            ok = False
        else:
            current = {item["name"] for item in listed}
            gone = {app: app not in current for app in to_remove}
        removed = _remove_confirmed([a for a in to_remove if gone.get(a) is False])
        gone.update(removed)
        ok = ok and all(removed.values())

    on_ok, on = enable_apps(ACCESSIBILITY_URL, "accessibility", apps, add_missing)
    ok = on_ok and ok
    if ok:
        _record(target_apps)
    elif (kept := partial_state(previous, gone, on)) != sorted(previous):
        _record(kept)
    return ok


def accessibility_revoke(names: list[str]) -> bool:
    """Remove these apps from the pane and the state file; apps not listed are done."""
    listed = list_apps(ACCESSIBILITY_URL)
    if listed is None:
        print("Could not read the accessibility list", file=sys.stderr)
        return False
    current = {item["name"] for item in listed}
    done = {name for name in names if name not in current}
    removed = _remove_confirmed(sorted(set(names) - done))
    done |= {app for app, gone in removed.items() if gone}
    ok = all(removed.values())
    if not state_disabled():
        previous = accessibility_previous_state()
        kept = [a for a in previous if a not in done]
        if kept != previous:
            _record(kept)
    return ok
