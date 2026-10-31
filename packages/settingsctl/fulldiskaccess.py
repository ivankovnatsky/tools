"""Full Disk Access: Manage Full Disk Access permissions (macOS only)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import click

from common import _get_state_dir, is_macos
from privacy_pane import ensure_enabled, list_apps, parse_app_specs, toggle_app

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


def fda_enable(apps: dict[str, str | None], add_missing: bool = False) -> None:
    """Ensure apps are enabled in Full Disk Access; add_missing adds unlisted ones."""
    names = list(apps)
    if not add_missing and fda_state_matches(names):
        print("Skipping Full Disk Access enable (already configured)")
        return

    if ensure_enabled(FDA_URL, "Full Disk Access", apps, add_missing):
        try:
            fda_write_state(names)
        except Exception as e:
            print(f"Warning: could not write state: {e}", file=sys.stderr)


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
        help="Add unlisted apps via + (needs you to authenticate).",
    )
    @click.argument(
        "action", required=False, type=click.Choice(["list", "toggle", "open"])
    )
    @click.argument("app", required=False)
    def fulldiskaccess(enable_apps, add_missing, action, app):
        """Manage Full Disk Access permissions (macOS only)"""
        if not is_macos():
            print("Full Disk Access settings only available on macOS", file=sys.stderr)
            sys.exit(1)

        if enable_apps is not None:
            apps = parse_app_specs(enable_apps)
            if not apps:
                print("Error: specify app list", file=sys.stderr)
                sys.exit(1)
            fda_enable(apps, add_missing)
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
