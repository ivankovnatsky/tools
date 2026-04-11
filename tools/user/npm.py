import os
from pathlib import Path
from typing import Dict

from tools.log import Color, debug, log
from tools.util import (
    get_pkg_binary,
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
            with open(npmrc_path, "w") as f:
                f.write(npmrc_content)
            state.setdefault("npm", {})["npmrc_created"] = True
        else:
            log(".npmrc already exists, skipping creation", Color.BLUE)
            state.setdefault("npm", {})["npmrc_created"] = True

    npm_bin = Path(paths["npmBin"])
    desired = set(packages.keys())
    state_packages = set(state.get("npm", {}).get("packages", {}).keys())

    env = os.environ.copy()
    env["PATH"] = f"{paths['nodejs']}:{env.get('PATH', '')}"

    state_changed = False

    # Build binary mapping for tracked packages
    all_tracked = {}
    for pkg, pkg_data in state.get("npm", {}).get("packages", {}).items():
        all_tracked[pkg] = pkg_data.get("binary", pkg.split("/")[-1])
    for pkg, pkg_info in packages.items():
        all_tracked[pkg] = get_pkg_binary(pkg_info)

    # 1. CLEANUP: Remove packages no longer in config
    to_remove = {
        pkg: binary
        for pkg, binary in all_tracked.items()
        if pkg not in desired and (npm_bin / binary).exists()
    }

    if to_remove:
        log(f"Removing npm packages: {', '.join(to_remove.keys())}", Color.RED)
        cmd = [f"{paths['nodejs']}/npm", "uninstall", "-g"] + list(to_remove.keys())
        run_command(cmd, env)
        state_changed = True

    # 2. INSTALL: Ensure all declared packages exist at correct version
    to_install = []
    for pkg, pkg_info in packages.items():
        binary = get_pkg_binary(pkg_info)
        if not (npm_bin / binary).exists() or version_changed(pkg, pkg_info, state, "npm"):
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

    # Update state (always save progress, even on partial failure)
    if state_changed or state_packages != desired:
        state.setdefault("npm", {})["packages"] = {
            pkg: {
                "installed": True,
                "binary": get_pkg_binary(pkg_info),
                "version": get_pkg_version(pkg_info),
                "subpackages": get_pkg_subpackages(pkg_info),
                "postInstall": get_pkg_post_install(pkg_info),
            }
            for pkg, pkg_info in packages.items()
        }

    return not subpkg_failed
