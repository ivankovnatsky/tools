import json
import os
import shutil
from typing import Dict, List, Optional

from tools.log import Color, debug, log
from tools.user import secrets_flow
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
    # An inherited CARGO_INSTALL_ROOT would otherwise win over cargoHome.
    if paths.get("cargoRoot") or paths.get("cargoHome"):
        env["CARGO_INSTALL_ROOT"] = os.path.dirname(_resolve_cargo_bin(paths))
    return env


class CrateRecordError(Exception):
    """cargo's install record exists but cannot be read."""


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

    # Unchanged packages still need reinstalling into a changed install dir.
    context = f"{paths.get('cargoRoot') or ''}|{paths.get('cargoHome') or ''}"
    context_changed = state.get("cargo", {}).get("context", context) != context
    # Without configured paths the root comes from the environment, which can
    # move between runs while the context above stays the same.
    root = os.path.dirname(_resolve_cargo_bin(paths))
    context_changed = context_changed or state.get("cargo", {}).get("root", root) != root
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

    # After an install dir change the binary sits in the old root, which
    # `cargo uninstall` no longer looks at, so it is only forgotten.
    orphaned = sorted(state_packages - desired)
    if orphaned and context_changed:
        log(
            "Dropping cargo packages from state (binaries remain in the previous "
            f"install dir, remove manually if unwanted): {', '.join(orphaned)}",
            Color.YELLOW,
        )
        for pkg in orphaned:
            tracked.pop(pkg, None)
    elif orphaned:
        log(f"Removing cargo packages: {', '.join(orphaned)}", Color.RED)
        cargo_cmd = shutil.which("cargo")
        if not cargo_cmd:
            # Kept in state so the next run with cargo available removes them.
            log(
                "cargo binary not found on PATH, keeping cargo packages to remove "
                f"next run: {', '.join(orphaned)}",
                Color.YELLOW,
            )
        else:
            cargo_env = _cargo_env(paths)
            for pkg in orphaned:
                entry = state_pkgs[pkg] if isinstance(state_pkgs[pkg], dict) else {}
                try:
                    name = _crate_name(pkg, entry, paths)
                except CrateRecordError as e:
                    log(f"Failed to remove cargo package {pkg}: {e}", Color.RED)
                    success = False
                    continue
                if not name:
                    log(
                        f"Dropping cargo package {pkg} from state (not in cargo's "
                        "install record, nothing to uninstall)",
                        Color.YELLOW,
                    )
                    tracked.pop(pkg, None)
                    continue
                returncode, _, stderr = run_command([cargo_cmd, "uninstall", name], env=cargo_env)
                # Already gone (removed by hand): nothing left to retry.
                if returncode != 0 and "did not match any packages" not in stderr:
                    log(f"Failed to remove cargo package {name}: {stderr}", Color.RED)
                    success = False
                    continue
                log(f"Removed: {name}", Color.GREEN)
                tracked.pop(pkg, None)

    units: Dict[str, list] = {}
    for pkg, pkg_info in packages.items():
        try:
            units[pkg] = secrets_flow.parse_restart_units(
                pkg_info.get("restartUnits"), f"cargo.packages.{pkg}"
            )
        except ValueError as e:
            log(str(e), Color.RED)
            success = False

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
            if pkg not in units:
                continue
            args = _install_args(pkg, packages[pkg])
            old = tracked.get(pkg) if isinstance(tracked.get(pkg), dict) else None
            # A different crate under the same key would leave the previous one
            # behind; remove it first so a failure keeps the record to retry.
            if old is not None and not context_changed:
                try:
                    old_name = _crate_name(pkg, old, paths)
                    new_name = _crate_name(pkg, packages[pkg], paths)
                except CrateRecordError as e:
                    log(f"Failed to install cargo package {pkg}: {e}", Color.RED)
                    success = False
                    continue
                same_repo = new_name is None and (get_pkg_source(old) or pkg) == (
                    get_pkg_source(packages[pkg]) or pkg
                )
                # A new revision of the same repo replaces its crate in place.
                if old_name and old_name != new_name and not same_repo:
                    rc, _, err = run_command([cargo_cmd, "uninstall", old_name], env=cargo_env)
                    if rc != 0 and "did not match any packages" not in err:
                        log(f"Failed to remove old cargo package {old_name}: {err}", Color.RED)
                        success = False
                        continue
                    # Gone now: if the install below fails, the next run retries it.
                    tracked.pop(pkg, None)
            cmd = [cargo_cmd, "install", *args]
            returncode, _, stderr = run_command(cmd, env=cargo_env)
            if returncode != 0:
                log(f"Failed to install cargo package {' '.join(args)}: {stderr}", Color.RED)
                success = False
                # The old record belongs to the previous install dir; keeping
                # it would mark the package installed here and never retry.
                if context_changed:
                    tracked.pop(pkg, None)
                continue
            log(f"Installed: {' '.join(args)}", Color.GREEN)
            tracked[pkg] = _cargo_entry(packages[pkg])
            secrets_flow.owe_restarts(state, units[pkg], f"cargo/{pkg}")
    elif not orphaned:
        debug("All cargo packages already installed", Color.BLUE)

    # Refresh metadata for desired packages already installed and unchanged.
    for pkg, pkg_info in packages.items():
        if pkg in tracked and pkg not in to_install:
            tracked[pkg] = _cargo_entry(pkg_info)

    if tracked != state_pkgs:
        state.setdefault("cargo", {})["packages"] = tracked
    state.setdefault("cargo", {})["context"] = context
    state.setdefault("cargo", {})["root"] = root

    return success


def _crate_name(pkg: str, pkg_info: Dict, paths: Dict) -> Optional[str]:
    """Package name `cargo uninstall` expects, matching `_install_args`.

    A `--git` install is named by the repo, not the config key, so it is
    looked up in cargo's install record. None when it is not recorded there;
    CrateRecordError when the record cannot be read.
    """
    source = get_pkg_source(pkg_info) or pkg
    if not get_pkg_commit(pkg_info):
        return source
    commit = get_pkg_commit(pkg_info)
    cargo_bin = _resolve_cargo_bin(paths)
    record = os.path.join(os.path.dirname(cargo_bin), ".crates2.json") if cargo_bin else ""
    try:
        with open(record) as f:
            installs = json.load(f).get("installs", {})
    except FileNotFoundError:
        return None
    except (OSError, ValueError, AttributeError) as e:
        raise CrateRecordError(f"cannot read {record}: {e}") from e
    # Keys look like "name 0.1.0 (git+https://host/repo.git?rev=abc#fullsha)".
    names = []
    for key in installs:
        name, _, rest = key.partition(" ")
        if f"(git+{source}?" in rest and f"rev={commit}#" in rest:
            names.append(name)
    # One repo and rev can hold several installed crates; the key names it.
    if pkg in names:
        return pkg
    return names[0] if len(names) == 1 else None


def _install_args(pkg: str, pkg_info: Dict) -> List[str]:
    source = get_pkg_source(pkg_info) or pkg
    commit = get_pkg_commit(pkg_info)
    if commit:
        args = ["--git", source, "--rev", commit]
        # A repo can hold several crates with binaries; name the one to build.
        if get_pkg_source(pkg_info):
            args.append(pkg)
        return args
    version = get_pkg_version(pkg_info)
    if version and version != "latest":
        return [source, "--version", version]
    return [source]
