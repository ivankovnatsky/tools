import os
from pathlib import Path
from typing import Dict, Set

from tools.log import Color, log
from tools.util import (
    get_pkg_binary,
    get_pkg_source,
    get_pkg_version,
    pkg_install_spec,
    run_command,
    version_changed,
)


def get_installed_uv_packages(uv_bin: str, packages: Dict[str, str]) -> Set[str]:
    installed = set()
    for package, binary in packages.items():
        if (Path(uv_bin) / binary).exists():
            installed.add(package)
    return installed


def install_uv_packages(packages: Dict, paths: Dict, state: Dict):
    desired = set(packages.keys())
    state_packages = set(state.get("uv", {}).get("packages", {}).keys())

    # Build binary mapping for installed check
    binary_map = {pkg: get_pkg_binary(info) for pkg, info in packages.items()}
    current = get_installed_uv_packages(paths["uvBin"], binary_map)

    all_tracked = {}
    for pkg, pkg_data in state.get("uv", {}).get("packages", {}).items():
        all_tracked[pkg] = pkg_data.get("binary", pkg)
    for pkg, pkg_info in packages.items():
        if pkg not in all_tracked:
            all_tracked[pkg] = get_pkg_binary(pkg_info)

    to_remove = []
    for pkg, binary in all_tracked.items():
        if pkg not in desired and (Path(paths["uvBin"]) / binary).exists():
            to_remove.append(pkg)

    state_changed = False

    env = os.environ.copy()
    env["PATH"] = f"{paths['uv']}:{env.get('PATH', '')}"
    env["UV_TOOL_BIN_DIR"] = paths["uvBin"]
    env["UV_TOOL_DIR"] = paths["uvToolDir"]

    if to_remove:
        log(f"Removing UV packages: {', '.join(to_remove)}", Color.RED)

        for pkg in to_remove:
            cmd = [f"{paths['uv']}/uv", "tool", "uninstall", pkg]
            returncode, stdout, stderr = run_command(cmd, env)

            if returncode != 0:
                log(f"Failed to remove UV package {pkg}: {stderr}", Color.RED)
            else:
                log(f"Removed: {pkg}", Color.GREEN)
                state_changed = True

    # Install missing packages or reinstall on version change
    to_install = []
    for pkg, pkg_info in packages.items():
        if pkg not in current or version_changed(pkg, pkg_info, state, "uv"):
            to_install.append(pkg)

    if to_install:
        log(f"Installing UV packages: {', '.join(to_install)}", Color.GREEN)

        for pkg in to_install:
            pkg_info = packages[pkg]
            spec = pkg_install_spec(pkg, get_pkg_version(pkg_info), get_pkg_source(pkg_info))
            cmd = [f"{paths['uv']}/uv", "tool", "install", spec]
            # Force reinstall if already present but version changed
            if pkg in current:
                cmd.append("--force")
            returncode, stdout, stderr = run_command(cmd, env)

            if returncode != 0:
                log(f"Failed to install UV package {spec}: {stderr}", Color.RED)
                return False
            else:
                log(f"Installed: {spec}", Color.GREEN)
                state_changed = True
    elif not to_remove:
        log("All UV packages already installed", Color.BLUE)

    if state_changed or state_packages != desired:
        state.setdefault("uv", {})["packages"] = {
            pkg: {
                "installed": True,
                "binary": get_pkg_binary(pkg_info),
                "version": get_pkg_version(pkg_info),
                "source": get_pkg_source(pkg_info),
            }
            for pkg, pkg_info in packages.items()
        }

    return True
