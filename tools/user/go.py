import os
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


def install_go_packages(packages: Dict, paths: Dict, state: Dict):
    desired = set(packages.keys())
    state_packages = set(state.get("go", {}).get("packages", {}).keys())

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

    if state_changed or state_packages != desired:
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
        state.setdefault("go", {})["packages"] = go_state

    return success


def _install_spec(pkg: str, pkg_info: Dict) -> str:
    source = get_pkg_source(pkg_info) or pkg
    commit = get_pkg_commit(pkg_info)
    if commit:
        return f"{source}@{commit}"
    version = get_pkg_version(pkg_info)
    return f"{source}@{version or 'latest'}"
