import os
import re
import shutil
from typing import Dict, Optional

from tools.log import Color, debug, log
from tools.user import secrets_flow
from tools.util import (
    checked_packages,
    get_pkg_commit,
    get_pkg_source,
    get_pkg_version,
    pkg_unpinned,
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
    if paths.get("goPath"):
        env["GOPATH"] = os.path.expanduser(paths["goPath"])
    # An inherited GOBIN would otherwise win over the configured GOPATH.
    if paths.get("goBin") or paths.get("goPath"):
        env["GOBIN"] = _resolve_go_bin(paths)
    return env


def _go_binary(pkg: str, pkg_info: Dict) -> str:
    """Executable name: declared `binary`, else what `go install` names it.

    `go install` uses the last path element, skipping a `/vN` (N >= 2) major
    version suffix (github.com/TwiN/gatus/v5 -> gatus).
    """
    declared = pkg_info.get("binary")
    if declared:
        return declared
    parts = (get_pkg_source(pkg_info) or pkg).rstrip("/").split("/")
    if len(parts) > 1 and re.fullmatch(r"v([2-9]|[1-9][0-9]+)", parts[-1]):
        return parts[-2]
    return parts[-1]


def _go_entry(pkg_info: Dict, path: Optional[str]) -> Dict:
    entry = {
        "installed": True,
        "version": get_pkg_version(pkg_info),
        "source": get_pkg_source(pkg_info),
    }
    if path:
        entry["path"] = path
    commit = get_pkg_commit(pkg_info)
    if commit:
        entry["commit"] = commit
    return entry


def _installed_path(pkg: str, entry: Dict, go_bin: Optional[str]) -> Optional[str]:
    """Binary path to delete on removal, or None when it cannot be known."""
    if entry.get("path"):
        return entry["path"]
    # Records from before paths were stored: only trust a configured dir, an
    # env-derived one may have moved since the install.
    if go_bin:
        return os.path.join(go_bin, _go_binary(pkg, entry))
    return None


def install_go_packages(packages: Dict, paths: Dict, state: Dict, update: bool = False):
    packages, ok = checked_packages("go", packages)
    if not ok:
        return False
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

    # Go has no `go uninstall`: removing a tool means deleting the binary it
    # installed. Without a known path it is only forgotten.
    orphaned = sorted(state_packages - desired)
    if orphaned:
        legacy_bin = None
        if not context_changed and (paths.get("goBin") or paths.get("goPath")):
            legacy_bin = _resolve_go_bin(paths)
        kept_paths = {
            _installed_path(pkg, entry, legacy_bin)
            for pkg, entry in state_pkgs.items()
            if pkg in desired and isinstance(entry, dict)
        }
        for pkg in orphaned:
            entry = state_pkgs[pkg] if isinstance(state_pkgs[pkg], dict) else {}
            path = _installed_path(pkg, entry, legacy_bin)
            if path and path in kept_paths:
                log(
                    f"Dropping Go package {pkg} from state (binary {path} still in use)",
                    Color.YELLOW,
                )
                tracked.pop(pkg, None)
                continue
            if not path:
                log(
                    f"Dropping Go package {pkg} from state (install path unknown, "
                    "binary not removed)",
                    Color.YELLOW,
                )
                tracked.pop(pkg, None)
                continue
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError as e:
                # Keep it tracked so the next run retries the removal.
                log(f"Failed to remove Go package {pkg} ({path}): {e}", Color.RED)
                success = False
                continue
            log(f"Removed: {pkg} ({path})", Color.GREEN)
            tracked.pop(pkg, None)

    units: Dict[str, list] = {}
    for pkg, pkg_info in packages.items():
        try:
            units[pkg] = secrets_flow.parse_restart_units(
                pkg_info.get("restartUnits"), f"go.packages.{pkg}"
            )
        except ValueError as e:
            log(str(e), Color.RED)
            success = False

    # A declared binary that is gone from the install dir is reinstalled.
    missing_binary = set()
    if any(pkg_info.get("binary") for pkg_info in packages.values()):
        go_bin = _resolve_go_bin(paths)
        if go_bin:
            missing_binary = {
                pkg
                for pkg, pkg_info in packages.items()
                if pkg in state_packages
                and pkg_info.get("binary")
                and not os.path.exists(os.path.join(go_bin, pkg_info["binary"]))
            }

    to_install = [
        pkg
        for pkg, pkg_info in packages.items()
        if pkg not in state_packages
        or context_changed
        or pkg in missing_binary
        or version_changed(pkg, pkg_info, state, "go")
        or (update and pkg_unpinned(pkg_info))
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
            if pkg not in units:
                continue
            spec = _install_spec(pkg, packages[pkg])
            new_path = os.path.join(go_bin, _go_binary(pkg, packages[pkg]))
            old = tracked.get(pkg) if isinstance(tracked.get(pkg), dict) else {}
            old_path = old.get("path")
            # A changed source or binary name would leave the previous binary
            # behind; remove it first so a failure keeps the record to retry.
            if not context_changed and old_path and old_path != new_path:
                try:
                    os.remove(old_path)
                except FileNotFoundError:
                    pass
                except OSError as e:
                    log(f"Failed to remove old Go binary {old_path}: {e}", Color.RED)
                    success = False
                    continue
                # Gone now: if the install below fails, the next run retries it.
                tracked.pop(pkg, None)
            cmd = [go_cmd, "install", spec]
            returncode, _, stderr = run_command(cmd, env=go_env)
            if returncode != 0:
                log(f"Failed to install Go package {spec}: {stderr}", Color.RED)
                success = False
                # The old record belongs to the previous install dir; keeping
                # it would mark the package installed here and never retry.
                if context_changed:
                    tracked.pop(pkg, None)
                continue
            log(f"Installed: {spec}", Color.GREEN)
            tracked[pkg] = _go_entry(packages[pkg], new_path)
            secrets_flow.owe_restarts(state, units[pkg], f"go/{pkg}")
    elif not orphaned:
        debug("All Go packages already installed", Color.BLUE)

    # Refresh metadata for desired packages already installed and unchanged.
    for pkg, pkg_info in packages.items():
        if pkg in tracked and pkg not in to_install:
            old = tracked[pkg] if isinstance(tracked[pkg], dict) else {}
            tracked[pkg] = _go_entry(pkg_info, old.get("path"))

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
