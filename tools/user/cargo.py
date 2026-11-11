import os
import shutil
from typing import Dict, List, Optional

from tools.log import Color, debug, log
from tools.util import (
    get_pkg_commit,
    get_pkg_source,
    get_pkg_version,
    pkg_unpinned,
    run_command,
    version_changed,
)


def _resolve_cargo_bin(paths: Dict) -> Optional[str]:
    """Resolve where `cargo install` puts binaries (`<install root>/bin`).

    Honors explicit `paths.cargoRoot` first, then `paths.cargoHome`, then
    $CARGO_INSTALL_ROOT, $CARGO_HOME and cargo's default `~/.cargo`.
    """
    for root in (
        paths.get("cargoRoot"),
        paths.get("cargoHome"),
        os.environ.get("CARGO_INSTALL_ROOT"),
        os.environ.get("CARGO_HOME"),
        "~/.cargo",
    ):
        if root:
            return os.path.join(os.path.expanduser(root), "bin")
    return None


def _cargo_env(paths: Dict) -> Dict[str, str]:
    """Build env for `cargo install` so it lands where we tell it.

    CARGO_HOME also holds the registry and git caches.
    """
    env = os.environ.copy()
    if paths.get("cargoHome"):
        env["CARGO_HOME"] = os.path.expanduser(paths["cargoHome"])
    if paths.get("cargoRoot"):
        env["CARGO_INSTALL_ROOT"] = os.path.expanduser(paths["cargoRoot"])
    return env


def _cargo_entry(pkg_info: Dict) -> Dict:
    entry = {
        "installed": True,
        "version": get_pkg_version(pkg_info),
        "source": get_pkg_source(pkg_info),
    }
    commit = get_pkg_commit(pkg_info)
    if commit:
        entry["commit"] = commit
    return entry


def install_cargo_packages(packages: Dict, paths: Dict, state: Dict, update: bool = False):
    desired = set(packages.keys())

    # Packages are only forgotten, never uninstalled (as with go), so a changed
    # install dir cannot "release" anything; unchanged packages still need
    # reinstalling into the new location.
    context = f"{paths.get('cargoRoot') or ''}|{paths.get('cargoHome') or ''}"
    context_changed = state.get("cargo", {}).get("context", context) != context
    if context_changed:
        log(
            "cargo install dir changed; reinstalling desired packages there "
            "(old binaries remain in the previous dir, remove manually if unwanted)",
            Color.YELLOW,
        )

    state_pkgs = state.get("cargo", {}).get("packages", {})
    state_packages = set(state_pkgs.keys())
    # tracked mirrors what is installed; persist each success so a later
    # failure keeps progress (state is the only record of installed tools).
    tracked = dict(state_pkgs)
    success = True

    # Dropping a package from config just forgets it here and leaves the
    # binary in place.
    orphaned = sorted(state_packages - desired)
    if orphaned:
        cargo_bin_hint = _resolve_cargo_bin(paths) or "$CARGO_HOME/bin"
        log(
            f"Dropping cargo packages from state (binaries remain in {cargo_bin_hint}, "
            f"remove manually if unwanted): {', '.join(orphaned)}",
            Color.YELLOW,
        )
        for pkg in orphaned:
            tracked.pop(pkg, None)

    to_install = [
        pkg
        for pkg, pkg_info in packages.items()
        if pkg not in state_packages
        or context_changed
        or version_changed(pkg, pkg_info, state, "cargo")
        or (update and pkg_unpinned(pkg_info))
    ]

    if to_install:
        cargo_cmd = shutil.which("cargo")
        if not cargo_cmd:
            log("cargo binary not found on PATH, cannot install cargo packages", Color.RED)
            return False
        cargo_env = _cargo_env(paths)
        log(f"Installing cargo packages: {', '.join(to_install)}", Color.GREEN)
        for pkg in to_install:
            args = _install_args(pkg, packages[pkg])
            cmd = [cargo_cmd, "install", *args]
            returncode, _, stderr = run_command(cmd, env=cargo_env)
            if returncode != 0:
                log(f"Failed to install cargo package {' '.join(args)}: {stderr}", Color.RED)
                success = False
                continue
            log(f"Installed: {' '.join(args)}", Color.GREEN)
            tracked[pkg] = _cargo_entry(packages[pkg])
    elif not orphaned:
        debug("All cargo packages already installed", Color.BLUE)

    # Refresh metadata for desired packages already installed and unchanged.
    for pkg, pkg_info in packages.items():
        if pkg in tracked and pkg not in to_install:
            tracked[pkg] = _cargo_entry(pkg_info)

    if tracked != state_pkgs:
        state.setdefault("cargo", {})["packages"] = tracked
    state.setdefault("cargo", {})["context"] = context

    return success


def _install_args(pkg: str, pkg_info: Dict) -> List[str]:
    source = get_pkg_source(pkg_info) or pkg
    commit = get_pkg_commit(pkg_info)
    if commit:
        return ["--git", source, "--rev", commit]
    version = get_pkg_version(pkg_info)
    if version and version != "latest":
        return [source, "--version", version]
    return [source]
