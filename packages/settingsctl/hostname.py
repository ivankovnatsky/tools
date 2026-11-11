"""Hostname: get or set HostName, LocalHostName and ComputerName (macOS only)."""

from __future__ import annotations

import json
import re
import sys

import click

from common import is_macos, require_root, run_cmd

SCUTIL = "/usr/sbin/scutil"

# JSON key -> (scutil name, --option)
NAMES = {
    "hostName": ("HostName", "host_name"),
    "localHostName": ("LocalHostName", "local_host_name"),
    "computerName": ("ComputerName", "computer_name"),
}

_HOST_NAME = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_LOCAL_HOST_NAME = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def hostname_validate(key: str, value: str) -> str | None:
    """Why value is not a valid name for key, or None."""
    if key == "hostName" and not _HOST_NAME.fullmatch(value):
        return "HostName must be a DNS name (letters, digits, '-', '.')"
    if key == "localHostName" and not _LOCAL_HOST_NAME.fullmatch(value):
        return "LocalHostName must be letters, digits and '-' (at most 63)"
    if key == "computerName" and (not value.strip() or "\n" in value):
        return "ComputerName must be a non-empty single line"
    return None


def hostname_read(scutil_name: str) -> str | None:
    """Current value, None when unset; RuntimeError when scutil fails otherwise."""
    rc, out, err = run_cmd([SCUTIL, "--get", scutil_name])
    if rc == 0:
        return out.strip()
    if "not set" in f"{out}{err}":
        return None
    raise RuntimeError(f"scutil --get {scutil_name}: {(err or out).strip() or rc}")


def hostname_get() -> dict:
    return {key: hostname_read(name) for key, (name, _) in NAMES.items()}


def register(cli):
    @cli.command()
    @click.argument("action", type=click.Choice(["get", "set"]))
    @click.option("--json", "as_json", is_flag=True, help="get: output as JSON")
    @click.option("--host-name", help="set: HostName")
    @click.option("--local-host-name", help="set: LocalHostName (Bonjour)")
    @click.option("--computer-name", help="set: ComputerName")
    def hostname(action, as_json, host_name, local_host_name, computer_name):
        """Get or set host names (macOS only; set needs root)

        Only names that differ are written.
        """
        if not is_macos():
            print("Hostname settings only available on macOS", file=sys.stderr)
            sys.exit(1)
        try:
            current = hostname_get()
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        if action == "get":
            if as_json:
                print(json.dumps(current))
            else:
                for key, (name, _) in NAMES.items():
                    print(f"{name}: {current[key] if current[key] is not None else '(not set)'}")
            return

        given = {"hostName": host_name, "localHostName": local_host_name}
        given["computerName"] = computer_name
        wanted = {k: v for k, v in given.items() if v is not None}
        if not wanted:
            print(
                "Error: give --host-name, --local-host-name or --computer-name",
                file=sys.stderr,
            )
            sys.exit(1)
        for key, value in wanted.items():
            why = hostname_validate(key, value)
            if why:
                print(f"Error: {why}", file=sys.stderr)
                sys.exit(1)
        todo = {k: v for k, v in wanted.items() if current[k] != v}
        if not todo:
            print("Host names: already set")
            return
        require_root("setting host names")
        for key, value in todo.items():
            name = NAMES[key][0]
            rc, out, err = run_cmd([SCUTIL, "--set", name, value])
            if rc != 0 or hostname_read(name) != value:
                print(f"Error: could not set {name}: {(err or out).strip()}", file=sys.stderr)
                sys.exit(1)
            print(f"{name}: {value}")
