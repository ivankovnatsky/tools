"""Accessibility: Manage accessibility permissions (macOS only)."""

from __future__ import annotations

import sys

import click

from accessibility_state import (
    accessibility_enable,
    accessibility_revoke,
    accessibility_set,
)
from accessibility_ui import (
    ACCESSIBILITY_URL,
    accessibility_add,
    accessibility_open,
    accessibility_remove,
    accessibility_toggle,
)
from common import is_macos, state_disabled
from privacy_pane import (
    NO_STATE_SET,
    list_apps,
    parse_app_specs,
    print_list,
    resolve_app_path,
)


def register(cli):
    @cli.command()
    @click.argument(
        "action",
        required=False,
        type=click.Choice(["list", "add", "remove", "toggle", "open", "enable", "set", "revoke"]),
    )
    @click.argument("app", required=False)
    @click.option(
        "--add-missing",
        is_flag=True,
        help="With enable/set: add unlisted apps via + (needs you to authenticate).",
    )
    @click.option("--json", "as_json", is_flag=True, help="With list: print JSON.")
    def accessibility(action, app, add_missing, as_json):
        """Manage accessibility permissions (macOS only)

        Actions:
          list [--json]           Show all entries and their enabled state
          add APP                 Add an app entry (UI automation; may prompt)
          remove APP              Remove an app entry
          toggle APP              Toggle an app's enabled state
          enable "APP1,APP2,..."  Enable apps (idempotent, additive)
          set "APP1,APP2,..."     Declaratively sync to list; removes previously
                                  managed apps no longer in the list
                                  APP may be NAME=/path; --add-missing adds
                                  unlisted apps from that path (default: NAME.app
                                  in /Applications, /System/Applications, ~/Applications)
          revoke "APP1,APP2,..."  Remove these apps and forget them; others untouched
          open                    Open the Accessibility pane in System Settings
        """
        if not is_macos():
            print("Accessibility settings only available on macOS", file=sys.stderr)
            sys.exit(1)

        if action == "set":
            if state_disabled():
                print(NO_STATE_SET, file=sys.stderr)
                sys.exit(2)
            if app is None:
                print(
                    "Error: specify app list (use empty string for none)",
                    file=sys.stderr,
                )
                sys.exit(1)
            if not accessibility_set(parse_app_specs(app), add_missing):
                sys.exit(1)
            return

        if action == "enable":
            if not app:
                print("Error: specify app list", file=sys.stderr)
                sys.exit(1)
            apps = parse_app_specs(app)
            if not apps:
                print("Error: specify app list", file=sys.stderr)
                sys.exit(1)
            if not accessibility_enable(apps, add_missing):
                sys.exit(1)
            return

        if action == "revoke":
            names = list(parse_app_specs(app or ""))
            if not names:
                print("Error: specify app list", file=sys.stderr)
                sys.exit(1)
            if not accessibility_revoke(names):
                sys.exit(1)
            return

        if action == "open":
            accessibility_open()
            return

        if action == "list":
            if not print_list(list_apps(ACCESSIBILITY_URL), "accessibility", as_json):
                sys.exit(1)
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
