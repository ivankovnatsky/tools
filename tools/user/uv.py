import os
from typing import Dict

from tools.log import Color, debug, log
from tools.util import (
    get_pkg_commit,
    get_pkg_source,
    get_pkg_version,
    pkg_install_spec,
    run_command,
    version_changed,
)


def _uv_entry(pkg_info: Dict) -> Dict:
    entry = {
        "installed": True,
        "version": get_pkg_version(pkg_info),
        "source": get_pkg_source(pkg_info),
    }
    commit = get_pkg_commit(pkg_info)
    if commit:
        entry["commit"] = commit
    return entry


def install_uv_packages(packages: Dict, paths: Dict, state: Dict):
    desired = set(packages.keys())

    # Fail with a clear message instead of a KeyError traceback when the
    # paths a reconcile would need are not configured.
    missing = [k for k in ("uv", "uvBin", "uvToolDir") if not paths.get(k)]
    if missing:
        log(
            "uv: required paths missing "
            f"({', '.join('paths.' + k for k in missing)}), cannot reconcile",
            Color.RED,
        )
        return False

    # Ownership is only meaningful against the tool dir it was recorded for
    # (see bun/npm prefix): after a relocation, uninstalling by name would hit
    # whatever occupies the new location, and "already installed" entries
    # would never materialize there.
    context = f"{paths['uvBin']}|{paths['uvToolDir']}"
    if state.get("uv", {}).get("context", context) != context:
        log("uv tool dir changed, releasing previously tracked packages", Color.YELLOW)
        state.setdefault("uv", {})["packages"] = {}

    state_pkgs = state.get("uv", {}).get("packages", {})
    state_packages = set(state_pkgs.keys())
    # tracked mirrors what is actually installed; we mutate it as removes and
    # installs succeed, then persist it. This keeps failed removals and any
    # already-installed packages when a later step fails (state is the only
    # record of installed tools now).
    tracked = dict(state_pkgs)
    success = True

    env = os.environ.copy()
    env["PATH"] = f"{paths['uv']}:{env.get('PATH', '')}"
    env["UV_TOOL_BIN_DIR"] = paths["uvBin"]
    env["UV_TOOL_DIR"] = paths["uvToolDir"]
    env["UV_LINK_MODE"] = "copy"

    # CLEANUP: uv tool uninstall is keyed by package name. Keep failed removals
    # tracked so the next run retries them.
    to_remove = sorted(pkg for pkg in state_packages if pkg not in desired)
    if to_remove:
        log(f"Removing UV packages: {', '.join(to_remove)}", Color.RED)
        for pkg in to_remove:
            cmd = [f"{paths['uv']}/uv", "tool", "uninstall", pkg]
            returncode, _, stderr = run_command(cmd, env)
            if returncode != 0:
                log(f"Failed to remove UV package {pkg}: {stderr}", Color.RED)
                success = False
            else:
                log(f"Removed: {pkg}", Color.GREEN)
                tracked.pop(pkg, None)

    # Install missing packages or reinstall on version change. Persist each
    # success so a later failure does not discard progress.
    to_install = [
        pkg
        for pkg, pkg_info in packages.items()
        if pkg not in state_packages or version_changed(pkg, pkg_info, state, "uv")
    ]
    if to_install:
        log(f"Installing UV packages: {', '.join(to_install)}", Color.GREEN)
        for pkg in to_install:
            pkg_info = packages[pkg]
            source = get_pkg_source(pkg_info)
            commit = get_pkg_commit(pkg_info)
            if source and commit:
                spec = f"{source}@{commit}"
            else:
                spec = pkg_install_spec(pkg, get_pkg_version(pkg_info), source)
            cmd = [f"{paths['uv']}/uv", "tool", "install", spec]
            # Force reinstall if already present but version changed
            if pkg in state_packages:
                cmd.append("--force")
            returncode, _, stderr = run_command(cmd, env)
            if returncode != 0:
                log(f"Failed to install UV package {spec}: {stderr}", Color.RED)
                success = False
                continue
            log(f"Installed: {spec}", Color.GREEN)
            tracked[pkg] = _uv_entry(pkg_info)
    elif not to_remove:
        debug("All UV packages already installed", Color.BLUE)

    # Refresh metadata for desired packages already installed and unchanged
    # (drops any legacy fields such as the old "binary").
    for pkg, pkg_info in packages.items():
        if pkg in tracked and pkg not in to_install:
            tracked[pkg] = _uv_entry(pkg_info)

    if tracked != state_pkgs:
        state.setdefault("uv", {})["packages"] = tracked
    state.setdefault("uv", {})["context"] = context

    return success
