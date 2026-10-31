"""Accessibility: Manage accessibility permissions (macOS only)."""

from __future__ import annotations

import sys

import click

from accessibility_state import (
    accessibility_enable,
    accessibility_set,
)
from accessibility_ui import (
    accessibility_add,
    accessibility_list,
    accessibility_open,
    accessibility_remove,
    accessibility_toggle,
)
from common import is_macos
from privacy_pane import parse_app_specs, resolve_app_path


def register(cli):
    @cli.command()
    @click.argument(
        "action",
        required=False,
        type=click.Choice(["list", "add", "remove", "toggle", "open", "enable", "set"]),
    )
    @click.argument("app", required=False)
    @click.option(
        "--add-missing",
        is_flag=True,
        help="With enable/set: add unlisted apps via + (needs you to authenticate).",
    )
    def accessibility(action, app, add_missing):
        """Manage accessibility permissions (macOS only)

        Actions:
          list                    Show all entries and their enabled state
          add APP                 Add an app entry (UI automation; may prompt)
          remove APP              Remove an app entry
          toggle APP              Toggle an app's enabled state
          enable "APP1,APP2,..."  Enable apps (idempotent, additive)
          set "APP1,APP2,..."     Declaratively sync to list; removes previously
                                  managed apps no longer in the list
                                  APP may be NAME=/path; --add-missing adds
                                  unlisted apps from that path (default: NAME.app
                                  in /Applications, /System/Applications, ~/Applications)
          open                    Open the Accessibility pane in System Settings
        """
        if not is_macos():
            print("Accessibility settings only available on macOS", file=sys.stderr)
            sys.exit(1)

        if action == "set":
            if app is None:
                print(
                    "Error: specify app list (use empty string for none)",
                    file=sys.stderr,
                )
                sys.exit(1)
            accessibility_set(parse_app_specs(app), add_missing)
            return

        if action == "enable":
            if not app:
                print("Error: specify app list", file=sys.stderr)
                sys.exit(1)
            apps = parse_app_specs(app)
            if not apps:
                print("Error: specify app list", file=sys.stderr)
                sys.exit(1)
            accessibility_enable(apps, add_missing)
            return

        if action == "open":
            accessibility_open()
            return

        if action == "list":
            items = accessibility_list()
            if not items:
                print("No accessibility items found (or could not read)")
            else:
                for item in items:
                    status = "enabled" if item["enabled"] else "disabled"
                    print(f"  {item['name']}: {status}")
            return

        if action == "add":
            if not app:
                print(
                    "Error: specify app path (e.g. /Applications/Amethyst.app)",
                    file=sys.stderr,
                )
                sys.exit(1)
            app_path = resolve_app_path(app, app if app.startswith("/") else None)
            if not app_path:
                print(f"Error: no app found for {app}", file=sys.stderr)
                sys.exit(1)
            if accessibility_add(app_path):
                print(f"Added {app_path} to Accessibility")
            else:
                print(
                    f"Could not add {app_path} (may need manual approval)",
                    file=sys.stderr,
                )
                sys.exit(1)
            return

        if action == "remove":
            if not app:
                print("Error: specify app name to remove", file=sys.stderr)
                sys.exit(1)
            if accessibility_remove(app):
                print(f"Removed {app} from Accessibility")
            else:
                print(f"Could not remove {app} (not found or failed)", file=sys.stderr)
                sys.exit(1)
            return

        if action == "toggle":
            if not app:
                print("Error: specify app name to toggle", file=sys.stderr)
                sys.exit(1)
            result = accessibility_toggle(app)
            if result is not None:
                status = "enabled" if result else "disabled"
                print(f"{app}: {status}")
            else:
                print(f"Could not toggle {app} (not found)", file=sys.stderr)
                sys.exit(1)
            return

        click.echo(click.get_current_context().get_help())
