"""Default browser: get or set it through the `defaultbrowser` CLI (macOS only).

The setting is per user, so this refuses to run as root. macOS asks the user to
confirm a change, so set returns before the switch is confirmed.
"""

from __future__ import annotations

import json
import os
import shutil
import sys

import click

from common import is_macos, is_root, run_cmd

SEARCH_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def defaultbrowser_cli() -> str | None:
    path = os.pathsep.join([os.environ.get("PATH", ""), *SEARCH_DIRS])
    return shutil.which("defaultbrowser", path=path)


def defaultbrowser_get(cli: str) -> dict:
    """{"defaultBrowser": current or None, "browsers": every name it accepts}."""
    rc, out, err = run_cmd([cli])
    if rc != 0:
        raise RuntimeError(f"defaultbrowser: {(err or out).strip() or rc}")
    current, names = None, []
    for line in out.splitlines():
        name = line.strip().lstrip("*").strip()
        if not name:
            continue
        names.append(name)
        if line.lstrip().startswith("*"):
            current = name
    return {"defaultBrowser": current, "browsers": names}


def register(cli):
    @cli.command()
    @click.argument("action", type=click.Choice(["get", "set"]))
    @click.argument("name", required=False)
    @click.option("--json", "as_json", is_flag=True, help="get: output as JSON")
    def defaultbrowser(action, name, as_json):
        """Get or set the default browser (macOS only; needs `defaultbrowser`)

        NAME is one of the names `get` lists, e.g. safari or firefox.
        """
        if not is_macos():
            print("Default browser only available on macOS", file=sys.stderr)
            sys.exit(1)
        if is_root():
            print(
                "Error: the default browser is per user; run without sudo",
                file=sys.stderr,
            )
            sys.exit(1)
        tool = defaultbrowser_cli()
        if not tool:
            print(
                "Error: `defaultbrowser` not found (brew install defaultbrowser)", file=sys.stderr
            )
            sys.exit(1)
        try:
            info = defaultbrowser_get(tool)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        if action == "get":
            if as_json:
                print(json.dumps(info))
            else:
                print(f"Default browser: {info['defaultBrowser'] or '(unknown)'}")
                print(f"  available: {', '.join(info['browsers']) or 'none'}")
            return

        if not name:
            print("Error: specify a browser name", file=sys.stderr)
            sys.exit(1)
        if name not in info["browsers"]:
            available = ", ".join(info["browsers"]) or "none"
            print(
                f"Error: {name!r} is not an installed browser (have {available})", file=sys.stderr
            )
            sys.exit(1)
        if info["defaultBrowser"] == name:
            print(f"Default browser: already {name}")
            return
        rc, out, err = run_cmd([tool, name])
        if rc != 0:
            print(f"Error: defaultbrowser {name}: {(err or out).strip() or rc}", file=sys.stderr)
            sys.exit(1)
        print(f"Default browser: {name} (confirm in the macOS dialog)")
