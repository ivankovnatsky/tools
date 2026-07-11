import os
import shutil
from pathlib import Path
from typing import Dict, Optional, Set

from tools.log import Color, debug, log
from tools.util import (
    get_pkg_binary,
    get_pkg_commit,
    get_pkg_source,
    get_pkg_version,
    run_command,
    version_changed,
)


def _default_binary(pkg: str, pkg_info: Dict) -> str:
    """Best-effort guess of the binary name `go install` will produce.

    `go install` writes `$GOBIN/<last-path-component-of-package>`. We
    fall back to the source's last path component, then to the package
    key. Users with nested package paths (e.g. `cobra-cli` whose source
    is `github.com/spf13/cobra-cli/cobra-cli`) should set `binary:`
    explicitly when the package key diverges from the binary name.
    """
    explicit = get_pkg_binary(pkg_info)
    if explicit:
        return explicit
    source = get_pkg_source(pkg_info)
    if source:
        return source.rsplit("/", 1)[-1]
    return pkg


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


def get_installed_go_packages(go_bin: str, packages: Dict[str, Dict]) -> Set[str]:
    installed = set()
    for pkg, info in packages.items():
        binary = _default_binary(pkg, info)
        if (Path(go_bin) / binary).exists():
            installed.add(pkg)
    return installed


def _force_writable(root: Path) -> None:
    """Make a tree removable.

    Go writes `$GOPATH/pkg/mod` read-only (dirs `0555`), so unlinking their
    contents fails with EACCES until the directory bit is restored.
    """
    for parent, dirs, _ in os.walk(root):
        for entry in [Path(parent), *(Path(parent) / d for d in dirs)]:
            if not entry.is_symlink():
                entry.chmod(0o700)


def _cleanup_managed_gopath(paths: Dict) -> bool:
    """Remove caches after the final managed Go binary is removed."""
    configured_go_path = paths.get("goPath")
    if not configured_go_path:
        return True

    go_path = Path(os.path.expanduser(configured_go_path)).resolve()
    # Only $GOPATH/bin gates the cache: binaries installed to an explicit
    # `goBin` elsewhere are managed here and already removed by now.
    go_path_bin = go_path / "bin"

    try:
        if go_path_bin.exists() and any(go_path_bin.iterdir()):
            log(
                f"Keeping Go dependencies because {go_path_bin} is not empty",
                Color.YELLOW,
            )
            return True

        if go_path_bin.exists():
            go_path_bin.rmdir()

        pkg_path = go_path / "pkg"
        if pkg_path.is_symlink():
            log(f"Refusing to remove symlinked Go package cache: {pkg_path}", Color.RED)
            return False
        if pkg_path.exists():
            _force_writable(pkg_path)
            shutil.rmtree(pkg_path)
            log(f"Removed Go package cache: {pkg_path}", Color.RED)

        if go_path.exists() and not any(go_path.iterdir()):
            go_path.rmdir()
            log(f"Removed empty GOPATH: {go_path}", Color.RED)
    except OSError as e:
        log(f"Failed to clean managed GOPATH {go_path}: {e}", Color.RED)
        return False

    return True


def install_go_packages(packages: Dict, paths: Dict, state: Dict):
    desired = set(packages.keys())
    go_state_before = state.get("go", {})
    state_packages = set(go_state_before.get("packages", {}).keys())
    cleanup_requested = not desired and (
        bool(state_packages) or go_state_before.get("cleanupPending", False)
    )

    go_bin = _resolve_go_bin(paths)
    if not go_bin:
        log("Failed to resolve Go install dir (no goBin, no go env GOPATH)", Color.RED)
        return False
    go_env = _go_env(paths)
    # When goBin isn't pinned in config, propagate the resolved value
    # so `go install` agrees with our existence checks.
    go_env.setdefault("GOBIN", go_bin)

    current = get_installed_go_packages(go_bin, packages)

    all_tracked = {}
    for pkg, pkg_data in state.get("go", {}).get("packages", {}).items():
        all_tracked[pkg] = pkg_data.get("binary", pkg)
    for pkg, pkg_info in packages.items():
        if pkg not in all_tracked:
            all_tracked[pkg] = _default_binary(pkg, pkg_info)

    to_remove = []
    for pkg, binary in all_tracked.items():
        if pkg not in desired and (Path(go_bin) / binary).exists():
            to_remove.append((pkg, binary))
            continue
        # If the desired binary name moved (source change, explicit
        # `binary:` flip, etc.), remove the old binary so the new
        # install doesn't leave it orphaned in $GOBIN.
        if pkg in desired:
            new_binary = _default_binary(pkg, packages[pkg])
            if new_binary != binary and (Path(go_bin) / binary).exists():
                to_remove.append((pkg, binary))

    state_changed = False
    # Removals that fail mid-loop must stay in state; otherwise the next
    # run forgets the orphaned binary and never retries cleanup.
    failed_removals: Dict[str, Dict] = {}
    success = True

    if to_remove:
        log(f"Removing Go packages: {', '.join(p for p, _ in to_remove)}", Color.RED)
        for pkg, binary in to_remove:
            target = Path(go_bin) / binary
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                log(f"Failed to remove Go package {pkg}: {e}", Color.RED)
                state_entry = state.get("go", {}).get("packages", {}).get(pkg)
                if state_entry is not None:
                    failed_removals[pkg] = state_entry
                success = False
                continue
            log(f"Removed: {pkg}", Color.GREEN)
            state_changed = True

    cleanup_succeeded = True
    if cleanup_requested and not failed_removals:
        cleanup_succeeded = _cleanup_managed_gopath(paths)
        success &= cleanup_succeeded

    to_install = []
    for pkg, pkg_info in packages.items():
        if pkg not in current or version_changed(pkg, pkg_info, state, "go"):
            to_install.append(pkg)

    if to_install:
        log(f"Installing Go packages: {', '.join(to_install)}", Color.GREEN)
        for pkg in to_install:
            spec = _install_spec(pkg, packages[pkg])
            cmd = ["go", "install", spec]
            returncode, stdout, stderr = run_command(cmd, env=go_env)
            if returncode != 0:
                log(f"Failed to install Go package {spec}: {stderr}", Color.RED)
                return False
            log(f"Installed: {spec}", Color.GREEN)
            state_changed = True
    elif not to_remove:
        debug("All Go packages already installed", Color.BLUE)

    if state_changed or state_packages != desired or cleanup_requested:
        go_state = dict(failed_removals)
        for pkg, pkg_info in packages.items():
            entry = {
                "installed": True,
                "binary": _default_binary(pkg, pkg_info),
                "version": get_pkg_version(pkg_info),
                "source": get_pkg_source(pkg_info),
            }
            commit = get_pkg_commit(pkg_info)
            if commit:
                entry["commit"] = commit
            go_state[pkg] = entry
        state["go"] = {"packages": go_state}
        if cleanup_requested and (failed_removals or not cleanup_succeeded):
            state["go"]["cleanupPending"] = True

    return success


def _install_spec(pkg: str, pkg_info: Dict) -> str:
    source = get_pkg_source(pkg_info) or pkg
    commit = get_pkg_commit(pkg_info)
    if commit:
        return f"{source}@{commit}"
    version = get_pkg_version(pkg_info)
    return f"{source}@{version or 'latest'}"
