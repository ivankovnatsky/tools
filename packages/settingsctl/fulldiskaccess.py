"""Full Disk Access: Manage Full Disk Access permissions (macOS only)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import click

from common import _get_state_dir, is_macos
from privacy_pane import (
    enable_apps,
    ensure_disabled,
    ensure_enabled,
    list_apps,
    parse_app_specs,
    partial_state,
    toggle_app,
)

FDA_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"


def fda_list() -> list[dict]:
    """List apps in Full Disk Access with their enabled status."""
    return list_apps(FDA_URL) or []


def fda_toggle(app_name: str) -> bool | None:
    """Toggle an app's Full Disk Access permission on/off. Returns new state or None."""
    return toggle_app(FDA_URL, app_name)


def fda_open() -> None:
    """Open the Full Disk Access pane in System Settings."""
    subprocess.run(["open", FDA_URL], check=True)


def fda_state_file() -> Path:
    return _get_state_dir() / "fda-enabled"


def fda_state_matches(enable_apps: list[str]) -> bool:
    state = fda_state_file()
    if not state.exists():
        return False
    return state.read_text().strip() == ",".join(sorted(enable_apps))


def fda_write_state(enable_apps: list[str]) -> None:
    state = fda_state_file()
    state.write_text(",".join(sorted(enable_apps)))


def fda_previous_state() -> list[str]:
    state = fda_state_file()
    if not state.exists():
        return []
    return [a.strip() for a in state.read_text().split(",") if a.strip()]


def _record(names: list[str]) -> None:
    try:
        fda_write_state(names)
    except Exception as e:
        print(f"Warning: could not write state: {e}", file=sys.stderr)


def fda_enable(apps: dict[str, str | None], add_missing: bool = False) -> bool:
    """Ensure apps are enabled in Full Disk Access; add_missing adds unlisted ones.

    Returns False when the pane could not be read or an app is not enabled.
    """
    names = list(apps)
    if not add_missing and fda_state_matches(names):
        print("Skipping Full Disk Access enable (already configured)")
        return True
    if not ensure_enabled(FDA_URL, "Full Disk Access", apps, add_missing):
        return False
    _record(names)
    return True


def fda_set(apps: dict[str, str | None], add_missing: bool = False) -> bool:
    """Sync Full Disk Access to exactly these apps (declarative).

    Turns off apps previously managed by us (per state file) that are no longer
    listed, then enables the listed ones. Entries added by hand are left alone.
    On partial failure the state keeps every app still granted by us and leaves
    out the ones that failed, so the next run retries.
    """
    names = list(apps)
    if not add_missing and fda_state_matches(names):
        print("Skipping Full Disk Access set (already configured)")
        return True
    previous = fda_previous_state()
    dropped = sorted(set(previous) - set(names))
    off_ok, off = ensure_disabled(FDA_URL, "Full Disk Access", dropped)
    on_ok, on = enable_apps(FDA_URL, "Full Disk Access", apps, add_missing)
    ok = off_ok and on_ok
    if ok:
        _record(names)
    elif (kept := partial_state(previous, off, on)) != sorted(previous):
        _record(kept)
    return ok


def fda_revoke(names: list[str]) -> bool:
    """Turn these apps off and drop them from the state file; apps not listed are off."""
    ok, off = ensure_disabled(FDA_URL, "Full Disk Access", names)
    previous = fda_previous_state()
    kept = [a for a in previous if not off.get(a)]
    if kept != previous:
        _record(kept)
    return ok


def register(cli):
    @cli.command()
    @click.option(
        "--enable",
        "enable_apps",
        help="Comma-separated apps to enable (idempotent); entries may be NAME=/path",
    )
    @click.option(
        "--add-missing",
        is_flag=True,
        help="With --enable/set: add unlisted apps via + (needs you to authenticate).",
    )
    @click.argument(
        "action", required=False, type=click.Choice(["list", "toggle", "open", "set", "revoke"]),
    )
    @click.argument("app", required=False)
    def fulldiskaccess(enable_apps, add_missing, action, app):
        """Manage Full Disk Access permissions (macOS only)

        \b
        Actions:
          list                Show all entries and their enabled state
          toggle APP          Toggle an app's enabled state
          set "APP1,APP2,..." Declaratively sync to list; turns off previously
                              managed apps no longer in the list
          revoke "APP1,..."   Turn these apps off and forget them; others untouched
          open                Open the Full Disk Access pane in System Settings
        """
        if not is_macos():
            print("Full Disk Access settings only available on macOS", file=sys.stderr)
            sys.exit(1)

        if enable_apps is not None:
            apps = parse_app_specs(enable_apps)
            if not apps:
                print("Error: specify app list", file=sys.stderr)
                sys.exit(1)
            if not fda_enable(apps, add_missing):
                sys.exit(1)
            return

        if action == "set":
            if app is None:
                print(
                    "Error: specify app list (use empty string for none)",
                    file=sys.stderr,
                )
                sys.exit(1)
            if not fda_set(parse_app_specs(app), add_missing):
                sys.exit(1)
            return

        if action == "revoke":
            names = list(parse_app_specs(app or ""))
            if not names:
                print("Error: specify app list", file=sys.stderr)
                sys.exit(1)
            if not fda_revoke(names):
                sys.exit(1)
            return

        if action == "open":
            fda_open()
            return

        if action == "list":
            items = fda_list()
            if not items:
                print("No Full Disk Access items found (or could not read)")
            else:
                for item in items:
                    status = "enabled" if item["enabled"] else "disabled"
                    print(f"  {item['name']}: {status}")
            return

        if action == "toggle":
            if not app:
                print("Error: specify app name to toggle", file=sys.stderr)
                sys.exit(1)
            result = fda_toggle(app)
            if result is not None:
                status = "enabled" if result else "disabled"
                print(f"{app}: {status}")
            else:
                print(f"Could not toggle {app} (not found)", file=sys.stderr)
                sys.exit(1)
            return

        click.echo(click.get_current_context().get_help())
