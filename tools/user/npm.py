import os
from pathlib import Path
from typing import Dict

from tools.log import Color, debug, log
from tools.user import secrets_flow
from tools.util import (
    checked_packages,
    get_pkg_post_install,
    get_pkg_subpackages,
    pkg_install_spec,
    pkg_spec_full,
    pkg_state_entry,
    pkg_unpinned,
    run_command,
    version_changed,
)

CONFIG_FILE_REMOVED = "configFile is no longer supported; manage the dotfile with files"


def binary_missing(pkg_info: Dict, bin_dir: str) -> bool:
    binary = pkg_info.get("binary")
    return bool(binary) and not os.path.exists(os.path.join(os.path.expanduser(bin_dir), binary))


def _prefix(paths: Dict, state: Dict):
    """((old prefix, packages tracked there) when the prefix moved, live tracked names)."""
    state_packages = set(state.get("npm", {}).get("packages", {}).keys())
    old = state.get("npm", {}).get("prefix", paths.get("npmBin"))
    if old != paths.get("npmBin"):
        return (old, state_packages), set()
    return None, state_packages


def plan_removals(packages: Dict, paths: Dict, state: Dict) -> Dict:
    released, state_packages = _prefix(paths, state)
    return {
        "released": released,
        "state_packages": state_packages,
        "remove": sorted(state_packages - set(packages)),
    }


def plan_installs(packages: Dict, paths: Dict, state: Dict, update: bool = False) -> Dict:
    """Run after removals: a removal can take a binary another package declares."""
    _, state_packages = _prefix(paths, state)
    actions = []
    for pkg, pkg_info in packages.items():
        if pkg not in state_packages:
            actions.append((pkg, "install"))
        elif version_changed(pkg, pkg_info, state, "npm"):
            actions.append((pkg, "update"))
        elif paths.get("npmBin") and binary_missing(pkg_info, paths["npmBin"]):
            actions.append((pkg, "reinstall"))
        elif update and pkg_unpinned(pkg_info):
            actions.append((pkg, "upgrade"))
    units, unit_errors = secrets_flow.parse_package_units("npm", packages)
    return {"actions": actions, "units": units, "unit_errors": unit_errors}


def plan(packages: Dict, paths: Dict, state: Dict, update: bool = False) -> Dict:
    """What deploy does with npm packages; the preview renders the same plan."""
    return {
        **plan_removals(packages, paths, state),
        **plan_installs(packages, paths, state, update),
    }


def install_npm_packages(
    packages: Dict, paths: Dict, state: Dict, npm_config: Dict, update: bool = False
):
    """Declarative npm package management.

    Ensures all declared packages exist in ~/.npm/bin at the declared version.
    """
    packages, ok = checked_packages("npm", packages)
    if not ok:
        return False
    if npm_config.get("configFile") is not None:
        log(f"npm: {CONFIG_FILE_REMOVED}", Color.RED)
        return False

    # Packages recorded against a different global prefix live somewhere we no
    # longer manage; uninstalling by name would hit whatever occupies the new
    # prefix under the same name.
    p = plan_removals(packages, paths, state)
    if p["released"]:
        log("npm prefix changed, releasing previously tracked packages", Color.YELLOW)
        state.setdefault("npm", {})["packages"] = {}
    state_packages = set(state.get("npm", {}).get("packages", {}).keys())

    # Fail with a clear message instead of a KeyError traceback when the
    # paths a reconcile would need are not configured.
    if packages or state_packages:
        missing = [k for k in ("nodejs", "npmBin") if not paths.get(k)]
        if missing:
            log(
                "npm: required paths missing "
                f"({', '.join('paths.' + k for k in missing)}), cannot reconcile",
                Color.RED,
            )
            return False

    env = os.environ.copy()
    env["PATH"] = f"{paths.get('nodejs', '')}:{env.get('PATH', '')}"

    state_changed = False
    success = True

    # 1. CLEANUP: Remove packages no longer in config (state is the source of
    # truth; npm uninstall is keyed by package name, not a binary path).
    # Keep failed removals in state so the next run retries them instead of
    # forgetting a package that is still installed.
    state_pkgs = state.get("npm", {}).get("packages", {})
    to_remove = p["remove"]
    failed_removals: Dict[str, Dict] = {}

    if to_remove:
        log(f"Removing npm packages: {', '.join(to_remove)}", Color.RED)
        cmd = [f"{paths['nodejs']}/npm", "uninstall", "-g"] + to_remove
        returncode, _, stderr = run_command(cmd, env)
        if returncode != 0:
            log(f"Failed to remove npm packages: {stderr}", Color.RED)
            failed_removals = {pkg: state_pkgs[pkg] for pkg in to_remove}
            success = False
        state_changed = True

    p = plan_installs(packages, paths, state, update)
    units = p["units"]
    for err in p["unit_errors"].values():
        log(err, Color.RED)
        success = False

    # 2. INSTALL: Ensure all declared packages exist at correct version
    to_install = []
    to_install_names = []
    for pkg, _ in p["actions"]:
        if pkg in units:
            to_install.append(pkg_spec_full(pkg, packages[pkg]))
            to_install_names.append(pkg)
    # Entries with invalid restartUnits are not (re)installed: keep their old state.
    install_failed = {pkg for pkg in packages if pkg not in units}
    if to_install:
        log(f"Installing npm packages: {', '.join(to_install)}", Color.GREEN)
        cmd = [f"{paths['nodejs']}/npm", "install", "-g"] + to_install
        returncode, stdout, stderr = run_command(cmd, env)
        if returncode != 0:
            log(f"Failed to install npm packages: {stderr}", Color.RED)
            # No early return: the removals above already mutated the system,
            # so state must still be rewritten below or the next run retries
            # `npm uninstall -g` on packages that are already gone — forever.
            success = False
            install_failed |= set(to_install_names)
        else:
            for pkg in to_install_names:
                secrets_flow.owe_restarts(state, units[pkg], f"npm/{pkg}")
        state_changed = True

    if not to_remove and not to_install:
        debug("All npm packages in sync", Color.BLUE)

    # 3. POST-INSTALL: Run postInstall commands for packages that need them
    # npm global prefix layout: <prefix>/lib/node_modules/<pkg>
    post_install_failed: set = set()
    npm_lib = Path(paths.get("npmBin") or "").parent / "lib" / "node_modules"
    for pkg, pkg_info in packages.items():
        post_install = get_pkg_post_install(pkg_info)
        if not post_install:
            continue
        pkg_dir = npm_lib / pkg
        if not pkg_dir.exists():
            continue
        stored_post_install = (
            state.get("npm", {}).get("packages", {}).get(pkg, {}).get("postInstall", "")
        )
        # Run if: package was just installed, or postInstall command changed,
        # or never ran before
        just_installed = pkg in to_install_names and pkg not in install_failed
        if just_installed or post_install != stored_post_install:
            log(f"Running postInstall for {pkg}: {post_install}", Color.GREEN)
            returncode, stdout, stderr = run_command(
                ["bash", "-c", post_install], env, cwd=str(pkg_dir)
            )
            if returncode != 0:
                log(f"postInstall failed for {pkg}: {stderr}", Color.RED)
                # Nothing on disk reveals that the hook did not run, so if the
                # command is still recorded as current it is never retried.
                post_install_failed.add(pkg)
            else:
                log(f"postInstall completed for {pkg}", Color.GREEN)
                state_changed = True

    # 4. SUBPACKAGES: Install additional deps inside package's node_modules
    # pkg -> its previously recorded subpackages, kept when an install fails so
    # the next run still sees the mismatch and retries.
    subpkg_failed: Dict[str, Dict] = {}
    for pkg, pkg_info in packages.items():
        subpkgs = get_pkg_subpackages(pkg_info)
        if not subpkgs:
            continue
        pkg_dir = npm_lib / pkg
        if not pkg_dir.exists():
            log(f"Skipping subpackages for {pkg}: package dir not found", Color.YELLOW)
            continue
        # Compare declared subpackages with state to detect version changes
        stored_subpkgs = (
            state.get("npm", {}).get("packages", {}).get(pkg, {}).get("subpackages", {})
        )
        # Migrate from old list format to dict
        if isinstance(stored_subpkgs, list):
            stored_subpkgs = {}
        to_install_sub = []
        for sp_name, sp_info in subpkgs.items():
            sp_version = sp_info.get("version", "latest")
            # Normalize both sides to "latest": a stored entry without an
            # explicit version must not read as None and reinstall forever.
            stored = stored_subpkgs.get(sp_name)
            stored_version = stored.get("version", "latest") if stored is not None else None
            missing = not (pkg_dir / "node_modules" / sp_name).exists()
            version_diff = sp_version != stored_version
            if missing or version_diff:
                to_install_sub.append(pkg_install_spec(sp_name, sp_version))
        if not to_install_sub:
            continue
        log(
            f"Installing subpackages for {pkg}: {', '.join(to_install_sub)}",
            Color.GREEN,
        )
        cmd = [f"{paths['nodejs']}/npm", "install", "--save=false"] + to_install_sub
        returncode, stdout, stderr = run_command(cmd, env, cwd=str(pkg_dir))
        if returncode != 0:
            log(f"Failed to install subpackages for {pkg}: {stderr}", Color.RED)
            subpkg_failed[pkg] = stored_subpkgs
        else:
            log(f"Installed subpackages for {pkg}", Color.GREEN)
            state_changed = True

    # Update state (always save progress, even on partial failure). Packages
    # whose uninstall failed are kept so cleanup is retried next run. Compare
    # the rebuilt entries against what is stored so a pure metadata change
    # (e.g. shedding a legacy "binary" field) is persisted even on a no-op sync.
    stored_pkgs = state.get("npm", {}).get("packages", {})
    npm_state = dict(failed_removals)
    for pkg, pkg_info in packages.items():
        if pkg in install_failed:
            # Not (re)installed: keep the old entry (or none) so the next run
            # still sees the mismatch and retries.
            if pkg in stored_pkgs:
                npm_state[pkg] = stored_pkgs[pkg]
            continue
        npm_state[pkg] = {
            **pkg_state_entry(pkg_info),
            "subpackages": subpkg_failed.get(pkg, get_pkg_subpackages(pkg_info)),
            # A failed hook records the empty "never ran" marker, so the next
            # run still sees a mismatch and retries it — keeping the old
            # command would mask a failure of an *unchanged* hook after a
            # version update.
            "postInstall": ("" if pkg in post_install_failed else get_pkg_post_install(pkg_info)),
        }
    if state_changed or npm_state != stored_pkgs:
        state.setdefault("npm", {})["packages"] = npm_state
    state.setdefault("npm", {})["prefix"] = paths.get("npmBin")

    return success and not subpkg_failed and not post_install_failed
