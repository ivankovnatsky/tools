import os
from typing import Dict

from tools.log import Color, debug, log
from tools.util import (
    get_pkg_version,
    pkg_install_spec,
    run_command,
    version_changed,
)


def install_bun_packages(packages: Dict, paths: Dict, state: Dict, bun_config: Dict):
    """Fully declarative bun package management.

    Ensures all declared packages exist in ~/.bun/bin at the declared version.
    """
    # Handle .bunfig.toml creation (only if bun.configFile is set)
    bunfig_content = bun_config.get("configFile")
    if bunfig_content and not state.get("bun", {}).get("bunfig_created"):
        bunfig_path = os.path.expanduser("~/.bunfig.toml")
        if not os.path.exists(bunfig_path):
            log("Creating .bunfig.toml file", Color.GREEN)
            with open(bunfig_path, "w") as f:
                f.write(bunfig_content)
            state.setdefault("bun", {})["bunfig_created"] = True
        else:
            log(".bunfig.toml already exists, skipping creation", Color.BLUE)
            state.setdefault("bun", {})["bunfig_created"] = True

    desired = set(packages.keys())
    state_packages = set(state.get("bun", {}).get("packages", {}).keys())

    env = os.environ.copy()
    env["PATH"] = f"{paths['bun']}:{paths['nodejs']}:{env.get('PATH', '')}"

    state_changed = False
    success = True

    # 1. CLEANUP: Remove packages no longer in config (state is the source of
    # truth; bun remove is keyed by package name, not a binary path). Keep
    # failed removals in state so the next run retries them.
    state_pkgs = state.get("bun", {}).get("packages", {})
    to_remove = sorted(pkg for pkg in state_packages if pkg not in desired)
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

    # 2. INSTALL: Ensure all declared packages exist at correct version
    to_install = []
    for pkg, pkg_info in packages.items():
        if pkg not in state_packages or version_changed(pkg, pkg_info, state, "bun"):
            to_install.append(pkg_install_spec(pkg, get_pkg_version(pkg_info)))
    if to_install:
        log(f"Installing bun packages: {', '.join(to_install)}", Color.GREEN)
        cmd = [f"{paths['bun']}/bun", "install", "-g"] + to_install
        returncode, stdout, stderr = run_command(cmd, env)
        if returncode != 0:
            log(f"Failed to install bun packages: {stderr}", Color.RED)
            return False
        state_changed = True

    if not to_remove and not to_install:
        debug("All bun packages in sync", Color.BLUE)

    # Update state. Packages whose removal failed are kept so cleanup retries.
    # Compare rebuilt entries against what is stored so a pure metadata change
    # (e.g. shedding a legacy "binary" field) is persisted even on a no-op sync.
    bun_state = dict(failed_removals)
    for pkg, pkg_info in packages.items():
        bun_state[pkg] = {
            "installed": True,
            "version": get_pkg_version(pkg_info),
        }
    if state_changed or bun_state != state.get("bun", {}).get("packages", {}):
        state.setdefault("bun", {})["packages"] = bun_state

    return success
