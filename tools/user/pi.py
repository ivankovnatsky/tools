import os
import shutil
from typing import Dict, Optional

from tools.log import Color, debug, log
from tools.util import (
    get_pkg_source,
    get_pkg_version,
    pkg_install_spec,
    pkg_state_entry,
    run_command,
    version_changed,
)


def resolve_pi_cli(paths: Dict) -> Optional[str]:
    """Locate the pi CLI for diff and deploy."""
    if paths.get("piCli") and os.path.isfile(os.path.expanduser(paths["piCli"])):
        return os.path.expanduser(paths["piCli"])
    if paths.get("pi"):
        candidate = os.path.expanduser(paths["pi"])
        if os.path.isfile(candidate):
            return candidate
        if os.path.isdir(candidate) and os.path.isfile(os.path.join(candidate, "pi")):
            return os.path.join(candidate, "pi")
    if paths.get("npmBin"):
        npm_bin_candidate = os.path.join(os.path.expanduser(paths["npmBin"]), "pi")
        if os.path.isfile(npm_bin_candidate):
            return npm_bin_candidate
    return shutil.which("pi")


def build_pi_env(paths: Dict) -> Dict[str, str]:
    env = os.environ.copy()
    extra_paths = [os.path.expanduser(p) for p in (paths.get("npmBin"), paths.get("nodejs")) if p]
    if extra_paths:
        env["PATH"] = f"{':'.join(extra_paths)}:{env.get('PATH', '')}"
    return env


def install_pi_packages(packages: Dict, paths: Dict, state: Dict) -> bool:
    """Declarative pi package management.

    Ensures all declared packages are installed via `pi install`.
    """
    desired = set(packages.keys())
    state_pkgs = state.get("pi", {}).get("packages", {})
    state_packages = set(state_pkgs.keys())

    if not desired and not state_packages:
        return True

    pi_cli = resolve_pi_cli(paths)
    if not pi_cli:
        log("pi: CLI not found, cannot reconcile", Color.RED)
        return False

    env = build_pi_env(paths)
    tracked = dict(state_pkgs)
    success = True

    # 1. CLEANUP: Remove packages no longer in config
    to_remove = sorted(pkg for pkg in state_packages if pkg not in desired)
    if to_remove:
        log(f"Removing Pi packages: {', '.join(to_remove)}", Color.RED)
        for pkg in to_remove:
            cmd = [pi_cli, "remove", pkg]
            returncode, _, stderr = run_command(cmd, env)
            if returncode != 0:
                log(f"Failed to remove Pi package {pkg}: {stderr}", Color.RED)
                success = False
            else:
                log(f"Removed: {pkg}", Color.GREEN)
                tracked.pop(pkg, None)

    # 2. INSTALL: Ensure all declared packages exist at correct version/source
    to_install = [
        pkg
        for pkg, pkg_info in packages.items()
        if pkg not in state_packages or version_changed(pkg, pkg_info, state, "pi")
    ]
    if to_install:
        log(f"Installing Pi packages: {', '.join(to_install)}", Color.GREEN)
        for pkg in to_install:
            pkg_info = packages[pkg]
            source = get_pkg_source(pkg_info)
            spec = pkg_install_spec(pkg, get_pkg_version(pkg_info), source)
            cmd = [pi_cli, "install", spec]
            returncode, _, stderr = run_command(cmd, env)
            if returncode != 0:
                log(f"Failed to install Pi package {spec}: {stderr}", Color.RED)
                success = False
                continue
            log(f"Installed: {spec}", Color.GREEN)
            tracked[pkg] = pkg_state_entry(pkg_info)
    elif not to_remove:
        debug("All Pi packages already installed", Color.BLUE)

    # 3. Refresh metadata for unchanged desired packages
    for pkg, pkg_info in packages.items():
        if pkg in tracked and pkg not in to_install:
            tracked[pkg] = pkg_state_entry(pkg_info)

    if tracked != state_pkgs:
        state.setdefault("pi", {})["packages"] = tracked

    return success
