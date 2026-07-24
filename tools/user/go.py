import os
import shutil
from typing import Dict, Optional

from tools.log import Color, debug, log
from tools.util import (
    get_pkg_commit,
    get_pkg_source,
    get_pkg_version,
    run_command,
    version_changed,
)


def _resolve_go_bin(paths: Dict) -> Optional[str]:
    """Resolve where Go binaries are installed.

    Honors explicit `paths.goBin` first (matching the bunBin/uvBin/npmBin
    pattern: nix-config controls install locations). Falls back to
    `go env GOBIN` then `$GOPATH/bin` so standalone `tools` invocations
    outside nix still work.
    """
    explicit = paths.get("goBin")
    if explicit:
        return os.path.expanduser(explicit)
    gopath = paths.get("goPath")
    if gopath:
        return os.path.join(os.path.expanduser(gopath), "bin")
    # Shelling out to a missing binary raises FileNotFoundError; a machine
    # without go must degrade gracefully (this is also called for log hints).
    if not shutil.which("go"):
        return None
    rc, stdout, _ = run_command(["go", "env", "GOBIN"])
    if rc == 0:
        gobin = stdout.strip()
        if gobin:
            return gobin
    rc, stdout, _ = run_command(["go", "env", "GOPATH"])
    if rc != 0:
        return None
    gopath = stdout.strip().split(os.pathsep)[0]
    if not gopath:
        return None
    return os.path.join(gopath, "bin")


def _go_env(paths: Dict) -> Dict[str, str]:
    """Build env for `go install` so it lands where we tell it.

    Without this, `go install` honors `go env -w` defaults and writes
    binaries somewhere we don't track. We pin GOBIN/GOPATH from config
    when set so the install dir matches the dir we scan for state.
    """
    env = os.environ.copy()
    if paths.get("goBin"):
        env["GOBIN"] = os.path.expanduser(paths["goBin"])
    if paths.get("goPath"):
        env["GOPATH"] = os.path.expanduser(paths["goPath"])
    return env


def _go_entry(pkg_info: Dict) -> Dict:
    entry = {
        "installed": True,
        "version": get_pkg_version(pkg_info),
        "source": get_pkg_source(pkg_info),
    }
    commit = get_pkg_commit(pkg_info)
    if commit:
        entry["commit"] = commit
    return entry


def install_go_packages(packages: Dict, paths: Dict, state: Dict):
    desired = set(packages.keys())

    # Go has no uninstall, so a changed install dir cannot "release" anything
    # — but unchanged packages still need reinstalling into the new location,
    # or they are simply absent there while state claims them installed.
    context = f"{paths.get('goBin') or ''}|{paths.get('goPath') or ''}"
    context_changed = state.get("go", {}).get("context", context) != context
    if context_changed:
        log(
            "go install dir changed; reinstalling desired packages there "
            "(old binaries remain in the previous dir, remove manually if unwanted)",
            Color.YELLOW,
        )

    state_pkgs = state.get("go", {}).get("packages", {})
    state_packages = set(state_pkgs.keys())
    # tracked mirrors what is installed; persist each success so a later
    # failure keeps progress (state is the only record of installed tools).
    tracked = dict(state_pkgs)
    success = True

    # Go has no `go uninstall`; the only way to remove a tool is to delete its
    # binary from $GOBIN by hand. We deliberately don't do that, so dropping a
    # package from config just forgets it here and leaves the binary in place.
    # This path never needs $GOBIN resolved, so don't fail on it here.
    orphaned = sorted(state_packages - desired)
    if orphaned:
        go_bin_hint = _resolve_go_bin(paths) or "$GOBIN"
        log(
            f"Dropping Go packages from state (binaries remain in {go_bin_hint}, "
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
        or version_changed(pkg, pkg_info, state, "go")
    ]

    if to_install:
        # Only now do we need the install dir; resolving lazily means a pure
        # state-drop (nothing to install) doesn't fail when $GOBIN is unknown.
        go_bin = _resolve_go_bin(paths)
        if not go_bin:
            log("Failed to resolve Go install dir (no goBin, no go env GOPATH)", Color.RED)
            return False
        go_cmd = shutil.which("go")
        if not go_cmd:
            log("go binary not found on PATH, cannot install Go packages", Color.RED)
            return False
        go_env = _go_env(paths)
        # When goBin isn't pinned in config, propagate the resolved value
        # so `go install` lands where we expect.
        go_env.setdefault("GOBIN", go_bin)
        log(f"Installing Go packages: {', '.join(to_install)}", Color.GREEN)
        for pkg in to_install:
            spec = _install_spec(pkg, packages[pkg])
            cmd = [go_cmd, "install", spec]
            returncode, _, stderr = run_command(cmd, env=go_env)
            if returncode != 0:
                log(f"Failed to install Go package {spec}: {stderr}", Color.RED)
                success = False
                continue
            log(f"Installed: {spec}", Color.GREEN)
            tracked[pkg] = _go_entry(packages[pkg])
    elif not orphaned:
        debug("All Go packages already installed", Color.BLUE)

    # Refresh metadata for desired packages already installed and unchanged
    # (drops any legacy fields such as the old "binary").
    for pkg, pkg_info in packages.items():
        if pkg in tracked and pkg not in to_install:
            tracked[pkg] = _go_entry(pkg_info)

    if tracked != state_pkgs:
        state.setdefault("go", {})["packages"] = tracked
    state.setdefault("go", {})["context"] = context

    return success


def _install_spec(pkg: str, pkg_info: Dict) -> str:
    source = get_pkg_source(pkg_info) or pkg
    commit = get_pkg_commit(pkg_info)
    if commit:
        return f"{source}@{commit}"
    version = get_pkg_version(pkg_info)
    return f"{source}@{version or 'latest'}"
