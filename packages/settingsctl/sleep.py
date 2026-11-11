"""Sleep: get or set computer, display and disk sleep timers via pmset (macOS only)."""

from __future__ import annotations

import json
import sys

import click

from common import is_macos, require_root, run_cmd

PMSET = "/usr/bin/pmset"

# Our name -> pmset setting
SETTINGS = {"computer": "sleep", "display": "displaysleep", "disk": "disksleep"}


def sleep_parse_value(value: str) -> str | int:
    """`never` or minutes (1-180); ValueError otherwise."""
    if value == "never":
        return value
    if not value.isdigit() or not 1 <= int(value) <= 180:
        raise ValueError(f"must be minutes (1-180) or 'never', got {value!r}")
    return int(value)


def _from_pmset(value: str | None):
    if value is None:
        return None
    if value == "0":
        return "never"
    return int(value) if value.isdigit() else value


def sleep_parse_pmset(text: str) -> dict:
    """`pmset -g custom` into {power source: {computer, display, disk}}."""
    raw: dict[str, dict[str, str]] = {}
    current = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if not line[0].isspace() and line.rstrip().endswith(":"):
            current = raw.setdefault(line.strip()[:-1], {})
            continue
        if current is not None:
            parts = line.split()
            if len(parts) >= 2:
                current[" ".join(parts[:-1])] = parts[-1]
    return {
        source: {name: _from_pmset(vals.get(key)) for name, key in SETTINGS.items()}
        for source, vals in raw.items()
    }


def sleep_get() -> dict:
    rc, out, err = run_cmd([PMSET, "-g", "custom"])
    if rc != 0:
        raise RuntimeError(f"pmset -g custom: {(err or out).strip() or rc}")
    sources = sleep_parse_pmset(out)
    if not sources:
        raise RuntimeError("pmset -g custom: no power sources listed")
    return sources


def sleep_drift(sources: dict, wanted: dict) -> dict:
    """The wanted settings some power source does not have yet."""
    return {
        name: value
        for name, value in wanted.items()
        if any(vals.get(name) != value for vals in sources.values())
    }


def _value_option(name: str):
    return click.option(f"--{name}", help=f"set: {name} sleep in minutes (1-180) or never")


def register(cli):
    @cli.command("sleep")
    @click.argument("action", type=click.Choice(["get", "set"]))
    @click.option("--json", "as_json", is_flag=True, help="get: output as JSON")
    @_value_option("computer")
    @_value_option("display")
    @_value_option("disk")
    def sleep_cmd(action, as_json, computer, display, disk):
        """Get or set sleep timers for every power source (macOS only; set needs root)

        Only timers that differ are written.
        """
        if not is_macos():
            print("Sleep settings only available on macOS", file=sys.stderr)
            sys.exit(1)
        try:
            sources = sleep_get()
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        if action == "get":
            if as_json:
                print(json.dumps({"sources": sources}))
            else:
                for source, vals in sources.items():
                    print(f"{source}:")
                    for name, value in vals.items():
                        shown = "(not set)" if value is None else value
                        print(f"  {name}: {shown}")
            return

        given = {"computer": computer, "display": display, "disk": disk}
        wanted = {}
        for name, value in given.items():
            if value is None:
                continue
            try:
                wanted[name] = sleep_parse_value(value)
            except ValueError as e:
                print(f"Error: --{name} {e}", file=sys.stderr)
                sys.exit(1)
        if not wanted:
            print("Error: give --computer, --display or --disk", file=sys.stderr)
            sys.exit(1)
        todo = sleep_drift(sources, wanted)
        if not todo:
            print("Sleep: already set")
            return
        require_root("setting sleep timers")
        args = []
        for name, value in todo.items():
            args += [SETTINGS[name], "0" if value == "never" else str(value)]
        rc, out, err = run_cmd([PMSET, "-a", *args])
        if rc != 0 or sleep_drift(sleep_get(), todo):
            msg = (err or out).strip() or "timers did not change"
            print(f"Error: could not set sleep: {msg}", file=sys.stderr)
            sys.exit(1)
        for name, value in todo.items():
            print(f"Sleep {name}: {value}")
