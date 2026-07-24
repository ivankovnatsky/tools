import os
from pathlib import Path
from typing import Dict

from tools.log import Color, debug, log
from tools.util import (
    get_pkg_post_install,
    get_pkg_subpackages,
    get_pkg_version,
    pkg_install_spec,
    run_command,
    version_changed,
)


def install_npm_packages(packages: Dict, paths: Dict, state: Dict, npm_config: Dict):
    """Declarative npm package management.

    Ensures all declared packages exist in ~/.npm/bin at the declared version.
    """
    # Handle .npmrc creation
    npmrc_content = npm_config.get("configFile")
    if npmrc_content and not state.get("npm", {}).get("npmrc_created"):
        npmrc_path = os.path.expanduser("~/.npmrc")
        if not os.path.exists(npmrc_path):
            log("Creating .npmrc file", Color.GREEN)
            # .npmrc routinely holds registry auth tokens, so it must not
            # inherit a world-readable umask.
            fd = os.open(npmrc_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(npmrc_content)
            state.setdefault("npm", {})["npmrc_created"] = True
        else:
            log(".npmrc already exists, skipping creation", Color.BLUE)
            state.setdefault("npm", {})["npmrc_created"] = True

    desired = set(packages.keys())
    state_packages = set(state.get("npm", {}).get("packages", {}).keys())

    env = os.environ.copy()
    env["PATH"] = f"{paths['nodejs']}:{env.get('PATH', '')}"

    state_changed = False
    success = True

    # 1. CLEANUP: Remove packages no longer in config (state is the source of
    # truth; npm uninstall is keyed by package name, not a binary path).
    # Keep failed removals in state so the next run retries them instead of
    # forgetting a package that is still installed.
    state_pkgs = state.get("npm", {}).get("packages", {})
    to_remove = sorted(pkg for pkg in state_packages if pkg not in desired)
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

    # 2. INSTALL: Ensure all declared packages exist at correct version
    to_install = []
    for pkg, pkg_info in packages.items():
        if pkg not in state_packages or version_changed(pkg, pkg_info, state, "npm"):
            to_install.append(pkg_install_spec(pkg, get_pkg_version(pkg_info)))
    if to_install:
        log(f"Installing npm packages: {', '.join(to_install)}", Color.GREEN)
        cmd = [f"{paths['nodejs']}/npm", "install", "-g"] + to_install
        returncode, stdout, stderr = run_command(cmd, env)
        if returncode != 0:
            log(f"Failed to install npm packages: {stderr}", Color.RED)
            return False
        state_changed = True

    if not to_remove and not to_install:
        debug("All npm packages in sync", Color.BLUE)

    # 3. POST-INSTALL: Run postInstall commands for packages that need them
    # npm global prefix layout: <prefix>/lib/node_modules/<pkg>
    post_install_failed: set = set()
    npm_lib = Path(paths["npmBin"]).parent / "lib" / "node_modules"
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
        just_installed = pkg_install_spec(pkg, get_pkg_version(pkg_info)) in to_install
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
    subpkg_failed = False
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
            stored_version = stored_subpkgs.get(sp_name, {}).get("version")
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
            subpkg_failed = True
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
        npm_state[pkg] = {
            "installed": True,
            "version": get_pkg_version(pkg_info),
            "subpackages": get_pkg_subpackages(pkg_info),
            # A failed hook keeps its previously recorded command, so the next
            # run still sees a mismatch and retries it.
            "postInstall": (
                stored_pkgs.get(pkg, {}).get("postInstall", "")
                if pkg in post_install_failed
                else get_pkg_post_install(pkg_info)
            ),
        }
    if state_changed or npm_state != stored_pkgs:
        state.setdefault("npm", {})["packages"] = npm_state

    return success and not subpkg_failed and not post_install_failed
