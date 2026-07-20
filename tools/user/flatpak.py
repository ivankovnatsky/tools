"""Manage Flatpak remotes and applications declaratively.

Adds desired remotes, installs desired apps, and tracks both in state.
When `removeUntracked` is enabled, previously-managed remotes and apps
that fall out of the desired config are removed.
"""

import shutil
from typing import Dict, List, Optional, Set

from tools.log import Color, debug, log
from tools.util import run_command

SCOPE = "--user"


def _find_flatpak() -> Optional[str]:
    return shutil.which("flatpak")


def _desired_packages(config: Dict) -> Dict[str, str]:
    """Normalize `packages` to {app_id: remote}.

    Accepts a plain list of app IDs, or a dict mapping app ID to a
    per-app `{remote: ...}` override for when several remotes carry the
    same ID and flatpak would otherwise refuse to guess.
    """
    packages = config.get("packages", []) or []
    if isinstance(packages, dict):
        return {app: (info or {}).get("remote", "") for app, info in packages.items()}
    return {app: "" for app in packages}


def _list_remotes(flatpak: str) -> Optional[Set[str]]:
    rc, stdout, _ = run_command([flatpak, SCOPE, "remotes", "--columns=name"])
    if rc != 0:
        return None
    return {line.strip() for line in stdout.splitlines() if line.strip()}


def _list_installed(flatpak: str) -> Optional[Set[str]]:
    rc, stdout, _ = run_command([flatpak, SCOPE, "list", "--app", "--columns=application"])
    if rc != 0:
        return None
    return {line.strip() for line in stdout.splitlines() if line.strip()}


def diff_flatpak(config: Dict, state: Dict) -> List[str]:
    """Return human-readable changes the reconciler would apply."""
    desired_remotes = config.get("remotes", {}) or {}
    desired_apps = _desired_packages(config)
    tracked = state.get("flatpak", {})
    if not desired_remotes and not desired_apps and not tracked:
        return []

    flatpak = _find_flatpak()
    if not flatpak:
        return ["  ? flatpak CLI not found"]

    remotes = _list_remotes(flatpak)
    installed = _list_installed(flatpak)
    if remotes is None or installed is None:
        return ["  ? flatpak not usable"]

    changes: List[str] = []
    for name in sorted(set(desired_remotes) - remotes):
        changes.append(f"  + remote-add {name}")

    for app in sorted(set(desired_apps) - installed):
        changes.append(f"  + install {app}")

    # Force deploy to run when a desired app is already present but
    # missing from state — otherwise show_diff reports "no changes",
    # deploy short-circuits, and it is never adopted.
    managed_apps = set(tracked.get("packages", []))
    for app in sorted((set(desired_apps) & installed) - managed_apps):
        changes.append(f"  ~ adopt {app}")

    if config.get("removeUntracked"):
        for app in sorted((managed_apps & installed) - set(desired_apps)):
            changes.append(f"  - remove {app}")
        managed_remotes = set(tracked.get("remotes", []))
        for name in sorted((managed_remotes & remotes) - set(desired_remotes)):
            changes.append(f"  - remote-delete {name}")

    return changes


def install_flatpak_packages(config: Dict, state: Dict) -> bool:
    """Reconcile flatpak remotes and installed apps toward the desired config."""
    desired_remotes = config.get("remotes", {}) or {}
    desired_apps = _desired_packages(config)
    tracked = state.get("flatpak", {})
    if not desired_remotes and not desired_apps and not tracked:
        return True

    flatpak = _find_flatpak()
    if not flatpak:
        log("flatpak CLI not found, skipping flatpak management", Color.YELLOW)
        return True

    remotes = _list_remotes(flatpak)
    installed = _list_installed(flatpak)
    if remotes is None or installed is None:
        log("flatpak not usable, skipping flatpak management", Color.YELLOW)
        return True

    success = True
    changed = False

    for name in sorted(set(desired_remotes) - remotes):
        url = desired_remotes[name]
        log(f"Adding flatpak remote {name}", Color.GREEN)
        rc, _, stderr = run_command([flatpak, SCOPE, "remote-add", "--if-not-exists", name, url])
        if rc != 0:
            log(f"Failed to add remote {name}: {stderr.strip()}", Color.RED)
            success = False
            continue
        remotes.add(name)
        changed = True

    for app in sorted(set(desired_apps) - installed):
        remote = desired_apps[app]
        log(f"Installing {app} ...", Color.GREEN)
        cmd = [flatpak, SCOPE, "install", "--noninteractive"]
        if remote:
            cmd.append(remote)
        cmd.append(app)
        rc, _, stderr = run_command(cmd)
        if rc != 0:
            log(f"Failed to install {app}: {stderr.strip()}", Color.RED)
            success = False
            continue
        installed.add(app)
        changed = True

    managed_apps = set(desired_apps) | (set(tracked.get("packages", [])) & installed)
    managed_remotes = set(desired_remotes) | (set(tracked.get("remotes", [])) & remotes)

    if config.get("removeUntracked"):
        for app in sorted((managed_apps & installed) - set(desired_apps)):
            log(f"Removing {app} ...", Color.RED)
            rc, _, stderr = run_command([flatpak, SCOPE, "uninstall", "--noninteractive", app])
            if rc != 0:
                log(f"Failed to remove {app}: {stderr.strip()}", Color.RED)
                success = False
                continue
            managed_apps.discard(app)
            installed.discard(app)
            changed = True

        for name in sorted((managed_remotes & remotes) - set(desired_remotes)):
            log(f"Removing flatpak remote {name}", Color.RED)
            rc, _, stderr = run_command([flatpak, SCOPE, "remote-delete", name])
            if rc != 0:
                log(f"Failed to remove remote {name}: {stderr.strip()}", Color.RED)
                success = False
                continue
            managed_remotes.discard(name)
            changed = True

        # Drop entries that vanished out-of-band so state converges.
        managed_apps &= installed
        managed_remotes &= remotes

    if not changed:
        debug("All flatpak packages in sync", Color.BLUE)

    state["flatpak"] = {
        "packages": sorted(managed_apps),
        "remotes": sorted(managed_remotes),
    }
    return success
