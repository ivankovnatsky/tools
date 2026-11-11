"""Timezone: get or set the system time zone (macOS only).

Two names linked to one tzdata file are the
same zone: automatic time zone may pick either, so neither counts as a change.
"""

from __future__ import annotations

import json
import os
import re
import sys

import click

from common import is_macos, require_root, run_cmd

SYSTEMSETUP = "/usr/sbin/systemsetup"
LOCALTIME = "/etc/localtime"
ZONEINFO = "/usr/share/zoneinfo"

_ZONE = re.compile(r"[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*")


def timezone_known(zone: str) -> bool:
    return bool(_ZONE.fullmatch(zone)) and os.path.isfile(os.path.join(ZONEINFO, zone))


def timezone_current() -> str | None:
    """The zone /etc/localtime points at, None when it is not a zoneinfo link."""
    try:
        target = os.readlink(LOCALTIME)
    except OSError as e:
        raise RuntimeError(f"cannot read {LOCALTIME}: {e}") from e
    _, sep, zone = target.partition("/zoneinfo/")
    return zone if sep else None


def _zone_bytes(zone: str) -> bytes:
    with open(os.path.join(ZONEINFO, zone), "rb") as f:
        return f.read()


def timezone_same(a: str | None, b: str) -> bool:
    if not a:
        return False
    if a == b:
        return True
    if not timezone_known(a) or not timezone_known(b):
        return False
    try:
        return _zone_bytes(a) == _zone_bytes(b)
    except OSError:
        return False


def register(cli):
    @cli.command()
    @click.argument("action", type=click.Choice(["get", "set"]))
    @click.argument("zone", required=False)
    @click.option("--json", "as_json", is_flag=True, help="get: output as JSON")
    def timezone(action, zone, as_json):
        """Get or set the time zone (macOS only; set needs root)

        `get ZONE` also reports whether the current zone matches ZONE, counting
        linked names as one zone.
        """
        if not is_macos():
            print("Time zone settings only available on macOS", file=sys.stderr)
            sys.exit(1)
        if zone is not None and not timezone_known(zone):
            print(f"Error: unknown time zone {zone!r} (not in {ZONEINFO})", file=sys.stderr)
            sys.exit(1)
        try:
            current = timezone_current()
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        if action == "get":
            info = {"timeZone": current}
            if zone is not None:
                info["matches"] = timezone_same(current, zone)
            if as_json:
                print(json.dumps(info))
            else:
                print(f"Time zone: {current or '(unknown)'}")
                if zone is not None:
                    print(f"Matches {zone}: {'yes' if info['matches'] else 'no'}")
            return

        if zone is None:
            print("Error: specify a zone, e.g. Europe/Uzhgorod", file=sys.stderr)
            sys.exit(1)
        if timezone_same(current, zone):
            print(f"Time zone: already {current}")
            return
        require_root("setting the time zone")
        rc, out, err = run_cmd([SYSTEMSETUP, "-settimezone", zone])
        if rc != 0 or not timezone_same(timezone_current(), zone):
            msg = (err or out).strip() or "time zone did not change"
            print(f"Error: could not set time zone: {msg}", file=sys.stderr)
            sys.exit(1)
        print(f"Time zone: {zone}")
