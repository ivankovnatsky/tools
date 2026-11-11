"""Remote Login: get or set the macOS SSH server (com.openssh.sshd).

Uses launchctl like nix-darwin's services.openssh, so no Full Disk Access is
needed (unlike `systemsetup -setremotelogin`). On means enabled and loaded; off
means neither, so sshd does not come back after a reboot.
"""

from __future__ import annotations

import json
import re
import sys

import click

from common import is_macos, require_root, run_cmd

LAUNCHCTL = "/bin/launchctl"
SSHD = "com.openssh.sshd"
SSHD_PLIST = "/System/Library/LaunchDaemons/ssh.plist"

_DISABLED_LINE = re.compile(r'^\s*"([^"]+)"\s*=>\s*(\S+)\s*$')


def remotelogin_get() -> dict:
    rc, out, err = run_cmd([LAUNCHCTL, "print-disabled", "system"])
    if rc != 0:
        raise RuntimeError(f"launchctl print-disabled: {(err or out).strip() or rc}")
    # Absent means ssh.plist's own Disabled key, which is true.
    enabled = False
    for line in out.splitlines():
        m = _DISABLED_LINE.match(line)
        if m and m.group(1) == SSHD:
            enabled = m.group(2) in ("enabled", "false")
    rc, _, _ = run_cmd([LAUNCHCTL, "print", f"system/{SSHD}"])
    loaded = rc == 0
    on = enabled and loaded
    return {"remoteLogin": on, "enabled": enabled, "loaded": loaded}


def remotelogin_is(state: dict, want: bool) -> bool:
    if want:
        return state["enabled"] and state["loaded"]
    return not state["enabled"] and not state["loaded"]


def register(cli):
    @cli.command()
    @click.argument("action", type=click.Choice(["get", "set"]))
    @click.argument("mode", required=False, type=click.Choice(["on", "off"]))
    @click.option("--json", "as_json", is_flag=True, help="get: output as JSON")
    def remotelogin(action, mode, as_json):
        """Get or set Remote Login (SSH server) (macOS only; set needs root)"""
        if not is_macos():
            print("Remote Login only available on macOS", file=sys.stderr)
            sys.exit(1)
        try:
            state = remotelogin_get()
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        if action == "get":
            if as_json:
                print(json.dumps(state))
            else:
                print(f"Remote Login: {'on' if state['remoteLogin'] else 'off'}")
                print(f"  enabled: {state['enabled']}, loaded: {state['loaded']}")
            return

        if mode is None:
            print("Error: specify on or off", file=sys.stderr)
            sys.exit(1)
        want = mode == "on"
        if remotelogin_is(state, want):
            print(f"Remote Login: already {mode}")
            return
        require_root("setting Remote Login")
        if want:
            steps = []
            if not state["enabled"]:
                steps.append([LAUNCHCTL, "enable", f"system/{SSHD}"])
            if not state["loaded"]:
                steps.append([LAUNCHCTL, "bootstrap", "system", SSHD_PLIST])
        else:
            steps = []
            if state["loaded"]:
                steps.append([LAUNCHCTL, "bootout", f"system/{SSHD}"])
            if state["enabled"]:
                steps.append([LAUNCHCTL, "disable", f"system/{SSHD}"])
        for cmd in steps:
            rc, out, err = run_cmd(cmd)
            if rc != 0:
                msg = (err or out).strip() or f"exit {rc}"
                print(f"Error: {' '.join(cmd[1:3])}: {msg}", file=sys.stderr)
                sys.exit(1)
        if not remotelogin_is(remotelogin_get(), want):
            print(f"Error: Remote Login is still not {mode}", file=sys.stderr)
            sys.exit(1)
        print(f"Remote Login: {mode}")
