"""Manage Flatpak remotes and applications declaratively.

Adds desired remotes, installs desired apps, and tracks both in state.
Previously-managed remotes and apps that fall out of the desired config
are removed; anything installed out-of-band is never touched.
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


def _remote_urls(flatpak: str) -> dict:
    """{name: url} for configured remotes.

    A remote is trusted by name alone everywhere else, so a name pointing at
    a different URL than the config declares is where installs would come
    from — worth surfacing rather than silently accepting.
    """
    rc, stdout, _ = run_command([flatpak, SCOPE, "remotes", "--columns=name,url"])
    if rc != 0:
        return {}
    urls = {}
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            urls[parts[0].strip()] = parts[1].strip()
    return urls


def _list_installed(flatpak: str) -> Optional[Set[str]]:
    rc, stdout, _ = run_command([flatpak, SCOPE, "list", "--app", "--columns=application"])
    if rc != 0:
        return None
    return {line.strip() for line in stdout.splitlines() if line.strip()}


def _remotes_in_use(flatpak: str) -> Set[str]:
    """Remotes that still have refs installed from them."""
    rc, stdout, _ = run_command([flatpak, SCOPE, "list", "--columns=origin"])
    if rc != 0:
        return set()
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

    live_urls = _remote_urls(flatpak)
    for name in sorted(set(desired_remotes) & remotes):
        live = live_urls.get(name)
        if live and live != desired_remotes[name]:
            changes.append(f"  ! remote {name} points at {live}, config declares its own URL")

    for app in sorted(set(desired_apps) - installed):
        changes.append(f"  + install {app}")

    managed_apps = set(tracked.get("packages", []))
    managed_remotes = set(tracked.get("remotes", []))

    for app in sorted((managed_apps & installed) - set(desired_apps)):
        changes.append(f"  - remove {app}")
    remotes_in_use = _remotes_in_use(flatpak)
    for name in sorted((managed_remotes & remotes) - set(desired_remotes) - remotes_in_use):
        changes.append(f"  - remote-delete {name}")

    # Tracked entries that vanished out-of-band produce no install/remove
    # work, so without their own diff line deploy short-circuits and state
    # keeps claiming them as managed — a later manual reinstall would then
    # be mistaken for ours and deleted.
    for app in sorted(managed_apps - installed - set(desired_apps)):
        changes.append(f"  ~ forget {app}")
    for name in sorted(managed_remotes - remotes - set(desired_remotes)):
        changes.append(f"  ~ forget remote {name}")

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
    # Only what this run actually installs joins the tracked set — anything
    # already present was put there by hand and is never ours to remove.
    newly_installed: Set[str] = set()
    newly_added_remotes: Set[str] = set()

    # Installs resolve through whatever URL the remote name currently points
    # at, so a mismatch against the declared URL decides where packages come
    # from. Surface it instead of installing from an unexpected source.
    live_urls = _remote_urls(flatpak)
    for name in sorted(set(desired_remotes) & remotes):
        live = live_urls.get(name)
        if live and live != desired_remotes[name]:
            log(
                f"Remote {name} points at {live}, not the URL in config. "
                "Delete the remote by hand if this is unexpected.",
                Color.RED,
            )
            success = False

    for name in sorted(set(desired_remotes) - remotes):
        url = desired_remotes[name]
        log(f"Adding flatpak remote {name}", Color.GREEN)
        rc, _, stderr = run_command([flatpak, SCOPE, "remote-add", "--if-not-exists", name, url])
        if rc != 0:
            log(f"Failed to add remote {name}: {stderr.strip()}", Color.RED)
            success = False
            continue
        remotes.add(name)
        newly_added_remotes.add(name)
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
        newly_installed.add(app)
        changed = True

    managed_apps = (set(tracked.get("packages", [])) | newly_installed) & installed
    managed_remotes = (set(tracked.get("remotes", [])) | newly_added_remotes) & remotes

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

    remotes_in_use = _remotes_in_use(flatpak)
    for name in sorted((managed_remotes & remotes) - set(desired_remotes)):
        # flatpak refuses to delete a remote that still has refs installed,
        # and those refs can be hand-installed apps we must not touch. Leave
        # it tracked and retry on a later run instead of failing forever.
        if name in remotes_in_use:
            debug(f"Remote {name} still has installed refs, leaving it", Color.YELLOW)
            continue
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
