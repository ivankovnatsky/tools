"""UI primitives for the Accessibility pane (macOS only)."""

from __future__ import annotations

import subprocess

from privacy_pane import add_apps, list_apps, remove_app, toggle_app

ACCESSIBILITY_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
)


def accessibility_list() -> list[dict]:
    """List apps in Accessibility with their enabled status."""
    return list_apps(ACCESSIBILITY_URL) or []


def accessibility_add(app_path: str) -> bool:
    """Add an app to Accessibility (click +, select app); True if it is listed after."""
    add_apps(ACCESSIBILITY_URL, [app_path])
    name = app_path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".app")
    return any(item["name"] == name for item in accessibility_list())


def accessibility_remove(app_name: str) -> bool:
    """Remove an app from Accessibility by selecting it and clicking minus."""
    return remove_app(ACCESSIBILITY_URL, app_name)


def accessibility_toggle(app_name: str) -> bool | None:
    """Toggle an app's accessibility permission on/off. Returns new state or None."""
    return toggle_app(ACCESSIBILITY_URL, app_name)


def accessibility_open() -> None:
    """Open the Accessibility pane in System Settings."""
    subprocess.run(["open", ACCESSIBILITY_URL], check=True)
