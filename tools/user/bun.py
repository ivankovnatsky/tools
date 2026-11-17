import os
from typing import Dict

from tools.log import Color, debug, log
from tools.user import secrets_flow
from tools.user.npm import CONFIG_FILE_REMOVED
from tools.util import (
    checked_packages,
    pkg_spec_full,
    pkg_state_entry,
    pkg_unpinned,
    run_command,
    version_changed,
)


def _prefix(paths: Dict, state: Dict):
    """((old prefix, packages tracked there) when the prefix moved, live tracked names)."""
    state_packages = set(state.get("bun", {}).get("packages", {}).keys())
    old = state.get("bun", {}).get("prefix", paths.get("bunBin"))
    if old != paths.get("bunBin"):
        return (old, state_packages), set()
    return None, state_packages


def plan_removals(packages: Dict, paths: Dict, state: Dict) -> Dict:
    released, state_packages = _prefix(paths, state)
    return {
        "released": released,
        "state_packages": state_packages,
        "remove": sorted(state_packages - set(packages)),
    }


def plan_installs(packages: Dict, paths: Dict, state: Dict, update: bool = False) -> Dict:
    _, state_packages = _prefix(paths, state)
    actions = []
    for pkg, pkg_info in packages.items():
        if pkg not in state_packages:
            actions.append((pkg, "install"))
        elif version_changed(pkg, pkg_info, state, "bun"):
            actions.append((pkg, "update"))
        elif update and pkg_unpinned(pkg_info):
            actions.append((pkg, "upgrade"))
    units, unit_errors = secrets_flow.parse_package_units("bun", packages)
    return {"actions": actions, "units": units, "unit_errors": unit_errors}


def plan(packages: Dict, paths: Dict, state: Dict, update: bool = False) -> Dict:
    """What deploy does with bun packages; the preview renders the same plan."""
    return {
        **plan_removals(packages, paths, state),
        **plan_installs(packages, paths, state, update),
    }


def install_bun_packages(
    packages: Dict, paths: Dict, state: Dict, bun_config: Dict, update: bool = False
):
    """Fully declarative bun package management.

    Ensures all declared packages exist in ~/.bun/bin at the declared version.
    """
    packages, ok = checked_packages("bun", packages)
    if not ok:
        return False
    if bun_config.get("configFile") is not None:
        log(f"bun: {CONFIG_FILE_REMOVED}", Color.RED)
        return False

    # See npm: ownership is only meaningful against the prefix it was recorded
    # for, so a changed prefix releases the old entries rather than acting on
    # whatever now sits there under the same names.
    p = plan_removals(packages, paths, state)
    if p["released"]:
        log("bun prefix changed, releasing previously tracked packages", Color.YELLOW)
        state.setdefault("bun", {})["packages"] = {}
    state_packages = set(state.get("bun", {}).get("packages", {}).keys())

    # Fail with a clear message instead of a KeyError traceback when the
    # paths a reconcile would need are not configured.
    if packages or state_packages:
        missing = [k for k in ("bun", "nodejs") if not paths.get(k)]
        if missing:
            log(
                "bun: required paths missing "
                f"({', '.join('paths.' + k for k in missing)}), cannot reconcile",
                Color.RED,
            )
            return False

    env = os.environ.copy()
    env["PATH"] = f"{paths.get('bun', '')}:{paths.get('nodejs', '')}:{env.get('PATH', '')}"

    state_changed = False
    success = True

    # 1. CLEANUP: Remove packages no longer in config (state is the source of
    # truth; bun remove is keyed by package name, not a binary path). Keep
    # failed removals in state so the next run retries them.
    state_pkgs = state.get("bun", {}).get("packages", {})
    to_remove = p["remove"]
    failed_removals: Dict[str, Dict] = {}

    if to_remove:
        log(f"Removing bun packages: {', '.join(to_remove)}", Color.RED)
        cmd = [f"{paths['bun']}/bun", "remove", "-g"] + to_remove
        returncode, _, stderr = run_command(cmd, env)
        if returncode != 0:
            log(f"Failed to remove bun packages: {stderr}", Color.RED)
            failed_removals = {pkg: state_pkgs[pkg] for pkg in to_remove}
            success = False
        state_changed = True

    p = plan_installs(packages, paths, state, update)
    units = p["units"]
    for err in p["unit_errors"].values():
        log(err, Color.RED)
        success = False

    # 2. INSTALL: Ensure all declared packages exist at correct version
    to_install = []
    to_install_names = []
    for pkg, _ in p["actions"]:
        if pkg in units:
            to_install.append(pkg_spec_full(pkg, packages[pkg]))
            to_install_names.append(pkg)
    # Entries with invalid restartUnits are not (re)installed: keep their old state.
    install_failed = {pkg for pkg in packages if pkg not in units}
    if to_install:
        log(f"Installing bun packages: {', '.join(to_install)}", Color.GREEN)
        cmd = [f"{paths['bun']}/bun", "install", "-g"] + to_install
        returncode, stdout, stderr = run_command(cmd, env)
        if returncode != 0:
            log(f"Failed to install bun packages: {stderr}", Color.RED)
            # No early return: the removals above already mutated the system,
            # so state must still be rewritten below or the next run retries
            # `bun remove -g` on packages that are already gone — forever.
            success = False
            install_failed |= set(to_install_names)
        else:
            for pkg in to_install_names:
                secrets_flow.owe_restarts(state, units[pkg], f"bun/{pkg}")
        state_changed = True

    if not to_remove and not to_install:
        debug("All bun packages in sync", Color.BLUE)

    # Update state. Packages whose removal failed are kept so cleanup retries.
    # Compare rebuilt entries against what is stored so a pure metadata change
    # (e.g. shedding a legacy "binary" field) is persisted even on a no-op sync.
    bun_state = dict(failed_removals)
    for pkg, pkg_info in packages.items():
        if pkg in install_failed:
            # Not (re)installed: keep the old entry (or none) so the next run
            # still sees the mismatch and retries.
            if pkg in state_pkgs:
                bun_state[pkg] = state_pkgs[pkg]
            continue
        bun_state[pkg] = pkg_state_entry(pkg_info)
    if state_changed or bun_state != state.get("bun", {}).get("packages", {}):
        state.setdefault("bun", {})["packages"] = bun_state
    state.setdefault("bun", {})["prefix"] = paths.get("bunBin")

    return success
