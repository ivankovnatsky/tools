import os
from pathlib import Path
from typing import Dict

from tools.log import Color, log
from tools.util import (
    get_pkg_binary,
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

    bun_bin = Path(paths["bunBin"])
    desired = set(packages.keys())
    state_packages = set(state.get("bun", {}).get("packages", {}).keys())

    # Build binary mapping for tracked packages
    all_tracked = {}
    for pkg, pkg_data in state.get("bun", {}).get("packages", {}).items():
        all_tracked[pkg] = pkg_data.get("binary", pkg.split("/")[-1])
    for pkg, pkg_info in packages.items():
        all_tracked[pkg] = get_pkg_binary(pkg_info)

    env = os.environ.copy()
    env["PATH"] = f"{paths['bun']}:{paths['nodejs']}:{env.get('PATH', '')}"

    state_changed = False

    # 1. CLEANUP: Remove packages no longer in config
    to_remove = {
        pkg: binary
        for pkg, binary in all_tracked.items()
        if pkg not in desired and (bun_bin / binary).exists()
    }

    if to_remove:
        log(f"Removing bun packages: {', '.join(to_remove.keys())}", Color.RED)
        cmd = [f"{paths['bun']}/bun", "remove", "-g"] + list(to_remove.keys())
        run_command(cmd, env)
        state_changed = True

    # 2. INSTALL: Ensure all declared packages exist at correct version
    to_install = []
    for pkg, pkg_info in packages.items():
        binary = get_pkg_binary(pkg_info)
        if not (bun_bin / binary).exists() or version_changed(pkg, pkg_info, state, "bun"):
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
        log("All bun packages in sync", Color.BLUE)

    # Update state
    if state_changed or state_packages != desired:
        state.setdefault("bun", {})["packages"] = {
            pkg: {
                "installed": True,
                "binary": get_pkg_binary(pkg_info),
                "version": get_pkg_version(pkg_info),
            }
            for pkg, pkg_info in packages.items()
        }

    return True
