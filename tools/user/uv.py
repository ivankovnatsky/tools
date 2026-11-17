import os
from typing import Dict

from tools.log import Color, debug, log
from tools.user import secrets_flow
from tools.util import (
    checked_packages,
    get_pkg_commit,
    get_pkg_source,
    get_pkg_version,
    pkg_install_spec,
    pkg_unpinned,
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


def _uv_commit_spec(source: str, commit: str) -> str:
    """Pin `source` to `commit`: the ref goes before any `#fragment`."""
    base, sep, fragment = source.partition("#")
    return f"{base}@{commit}{sep}{fragment}"


def binary_missing(pkg_info: Dict, bin_dir: str) -> bool:
    binary = pkg_info.get("binary")
    return bool(binary) and not os.path.exists(os.path.join(os.path.expanduser(bin_dir), binary))


def _context(paths: Dict, state: Dict):
    """(context, (old context, packages tracked there) when it moved, live tracked names)."""
    state_packages = set(state.get("uv", {}).get("packages", {}).keys())
    if not (paths.get("uvBin") and paths.get("uvToolDir")):
        return None, None, state_packages
    context = f"{paths['uvBin']}|{paths['uvToolDir']}"
    old = state.get("uv", {}).get("context", context)
    if old != context:
        return context, (old, state_packages), set()
    return context, None, state_packages


def plan_removals(packages: Dict, paths: Dict, state: Dict) -> Dict:
    context, released, state_packages = _context(paths, state)
    return {
        "context": context,
        "released": released,
        "remove": sorted(state_packages - set(packages)),
    }


def plan_installs(packages: Dict, paths: Dict, state: Dict, update: bool = False) -> Dict:
    """Run after removals: a removal can take a binary another package declares."""
    _, _, state_packages = _context(paths, state)
    actions = []
    for pkg, pkg_info in packages.items():
        if pkg not in state_packages:
            actions.append((pkg, "install"))
        elif version_changed(pkg, pkg_info, state, "uv"):
            actions.append((pkg, "update"))
        elif paths.get("uvBin") and binary_missing(pkg_info, paths["uvBin"]):
            actions.append((pkg, "reinstall"))
        elif update and pkg_unpinned(pkg_info):
            actions.append((pkg, "upgrade"))
    units, unit_errors = secrets_flow.parse_package_units("uv", packages)
    return {"actions": actions, "units": units, "unit_errors": unit_errors}


def plan(packages: Dict, paths: Dict, state: Dict, update: bool = False) -> Dict:
    """What deploy does with uv packages; the preview renders the same plan."""
    return {
        **plan_removals(packages, paths, state),
        **plan_installs(packages, paths, state, update),
    }


def install_uv_packages(packages: Dict, paths: Dict, state: Dict, update: bool = False):
    packages, ok = checked_packages("uv", packages)
    if not ok:
        return False

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
    p = plan_removals(packages, paths, state)
    context = p["context"]
    if p["released"]:
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
    to_remove = p["remove"]
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

    # Install missing packages, reinstall on version change or when a declared
    # binary is gone. Persist each success so a later failure does not discard
    # progress.
    p = plan_installs(packages, paths, state, update)
    to_install = [pkg for pkg, kind in p["actions"] if kind != "upgrade"]
    units = p["units"]
    for err in p["unit_errors"].values():
        log(err, Color.RED)
        success = False
    if to_install:
        log(f"Installing UV packages: {', '.join(to_install)}", Color.GREEN)
        for pkg in to_install:
            if pkg not in units:
                continue
            pkg_info = packages[pkg]
            source = get_pkg_source(pkg_info)
            commit = get_pkg_commit(pkg_info)
            if source and commit:
                spec = _uv_commit_spec(source, commit)
            else:
                spec = pkg_install_spec(pkg, get_pkg_version(pkg_info), source)
            cmd = [f"{paths['uv']}/uv", "tool", "install", spec]
            # Force reinstall if already present (version changed or binary gone)
            if pkg in state_packages:
                cmd.append("--force")
            returncode, _, stderr = run_command(cmd, env)
            if returncode != 0:
                log(f"Failed to install UV package {spec}: {stderr}", Color.RED)
                success = False
                continue
            log(f"Installed: {spec}", Color.GREEN)
            tracked[pkg] = _uv_entry(pkg_info)
            secrets_flow.owe_restarts(state, units[pkg], f"uv/{pkg}")
    elif not to_remove:
        debug("All UV packages already installed", Color.BLUE)

    if update:
        for pkg in (pkg for pkg, kind in p["actions"] if kind == "upgrade"):
            if pkg in units:
                log(f"Upgrading UV package: {pkg}", Color.BLUE)
                cmd = [f"{paths['uv']}/uv", "tool", "upgrade", pkg]
                returncode, _, stderr = run_command(cmd, env)
                if returncode != 0:
                    log(f"Failed to upgrade UV package {pkg}: {stderr}", Color.RED)
                    success = False
                else:
                    secrets_flow.owe_restarts(state, units[pkg], f"uv/{pkg}")

    # Refresh metadata for desired packages already installed and unchanged
    # (drops any legacy fields such as the old "binary").
    for pkg, pkg_info in packages.items():
        if pkg in tracked and pkg not in to_install:
            tracked[pkg] = _uv_entry(pkg_info)

    if tracked != state_pkgs:
        state.setdefault("uv", {})["packages"] = tracked
    state.setdefault("uv", {})["context"] = context

    return success
