import os
from typing import Dict, Optional

from tools.log import Color, debug, log
from tools.user import files as files_section
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


def _links_into(path: str, tool_dir: str) -> bool:
    if not os.path.islink(path):
        return False
    root = os.path.join(os.path.realpath(os.path.expanduser(tool_dir)), "")
    return os.path.realpath(path).startswith(root)


def _real_dir(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _old_dirs(paths: Dict, dirs) -> set:
    """``dirs`` minus empty ones and the current uvToolDir (a uvBin-only move)."""
    current = paths.get("uvToolDir")
    current = _real_dir(current) if current else None
    return {d for d in dirs if d and _real_dir(d) != current}


def _old_tool_dirs(paths: Dict, state: Dict, released) -> list:
    """Released tool dirs some entry point in uvBin still links into."""
    old_dirs = _old_dirs(paths, set(state.get("uv", {}).get("oldToolDirs") or []) | set(released))
    bin_dir = os.path.expanduser(paths["uvBin"])
    try:
        names = os.listdir(bin_dir)
    except OSError:
        return sorted(old_dirs)
    paths_in_bin = [os.path.join(bin_dir, n) for n in names]
    return sorted(d for d in old_dirs if any(_links_into(x, d) for x in paths_in_bin))


def _blocked(packages: Dict, paths: Dict, state: Dict, files_declared) -> Dict[str, Dict]:
    """Binaries in the way of a fresh install that are ours to replace.

    ``force``: package to the old tool dir its entry point links into, left by a
    released context (uv refuses to overwrite them); kept in state as
    ``oldToolDirs`` until those packages install. ``takeover``: an undeclared
    `files` copy, moved to the Trash first; None for ``files_declared`` (--only)
    skips it."""
    _, released, state_packages = _context(paths, state)
    old_dirs = list(state.get("uv", {}).get("oldToolDirs") or [])
    if released and isinstance(released[0], str):
        old_dirs.append(released[0].partition("|")[2])
    old_dirs = sorted(_old_dirs(paths, old_dirs))
    force, takeover = {}, {}
    for pkg, pkg_info in packages.items():
        binary = pkg_info.get("binary")
        if pkg in state_packages or not binary or not paths.get("uvBin"):
            continue
        path = os.path.join(os.path.expanduser(paths["uvBin"]), binary)
        old = next((d for d in old_dirs if _links_into(path, d)), None)
        if old:
            force[pkg] = old
        elif (
            files_declared is not None
            and os.path.lexists(path)
            and files_section.stale_copy(path, state, files_declared)
        ):
            takeover[pkg] = path
    return {"force": force, "takeover": takeover}


def plan_installs(
    packages: Dict,
    paths: Dict,
    state: Dict,
    update: bool = False,
    files_declared: Optional[files_section.Declared] = None,
) -> Dict:
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
    return {
        "actions": actions,
        "units": units,
        "unit_errors": unit_errors,
        **_blocked(packages, paths, state, files_declared),
    }


def plan(
    packages: Dict,
    paths: Dict,
    state: Dict,
    update: bool = False,
    files_declared: Optional[files_section.Declared] = None,
) -> Dict:
    """What deploy does with uv packages; the preview renders the same plan."""
    return {
        **plan_removals(packages, paths, state),
        **plan_installs(packages, paths, state, update, files_declared),
    }


def install_uv_packages(
    packages: Dict,
    paths: Dict,
    state: Dict,
    update: bool = False,
    files_declared: Optional[files_section.Declared] = None,
):
    """``files_declared``: `files.declared_targets` of the full config, or None
    (--only) to leave stale `files` copies alone."""
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
    p = plan_installs(packages, paths, state, update, files_declared)
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
            if pkg in p["takeover"]:
                path = p["takeover"][pkg]
                released = files_section.release_to(path, state, files_declared)
                if released is False:
                    log(f"Failed to install UV package {spec}: {path} is in the way", Color.RED)
                    success = False
                    continue
                if released:
                    log(f"Moved {path} to Trash: uv now installs it", Color.YELLOW)
            cmd = [f"{paths['uv']}/uv", "tool", "install", spec]
            # Force reinstall if already present (version changed or binary gone),
            # or over an entry point the old tool dir left behind.
            if pkg in state_packages or pkg in p["force"]:
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
    # A failed install still needs --force next run, after the context is saved.
    old_dirs = _old_tool_dirs(paths, state, p["force"].values())
    if old_dirs:
        state["uv"]["oldToolDirs"] = old_dirs
    else:
        state["uv"].pop("oldToolDirs", None)

    return success
