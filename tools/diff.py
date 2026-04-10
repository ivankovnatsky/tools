"""Dry-run diff: show what deploy would change without modifying anything."""

import hashlib
import os
import shutil
import sys
from pathlib import Path
from typing import Dict, List

from tools.log import Color, log
from tools.util import get_pkg_binary, get_pkg_source, run_command, version_changed


def _section(title: str):
    log(f"\n=== {title} ===", Color.BLUE)


def _diff_bun(packages: Dict, paths: Dict, state: Dict, bun_config: Dict):
    changes = []
    bun_bin = Path(paths["bunBin"])
    desired = set(packages.keys())

    all_tracked = {}
    for pkg, pkg_data in state.get("bun", {}).get("packages", {}).items():
        all_tracked[pkg] = pkg_data.get("binary", pkg.split("/")[-1])
    for pkg, pkg_info in packages.items():
        all_tracked[pkg] = get_pkg_binary(pkg_info)

    for pkg, binary in all_tracked.items():
        if pkg not in desired and (bun_bin / binary).exists():
            changes.append(f"  - remove {pkg}")

    for pkg, pkg_info in packages.items():
        binary = get_pkg_binary(pkg_info)
        if not (bun_bin / binary).exists():
            changes.append(f"  + install {pkg}")
        elif version_changed(pkg, pkg_info, state, "bun"):
            changes.append(f"  ~ update {pkg}")

    bunfig_content = bun_config.get("configFile")
    if bunfig_content and not state.get("bun", {}).get("bunfig_created"):
        if not os.path.exists(os.path.expanduser("~/.bunfig.toml")):
            changes.append("  + create ~/.bunfig.toml")

    return changes


def _diff_npm(packages: Dict, paths: Dict, state: Dict, npm_config: Dict):
    changes = []
    npm_bin = Path(paths["npmBin"])
    desired = set(packages.keys())

    all_tracked = {}
    for pkg, pkg_data in state.get("npm", {}).get("packages", {}).items():
        all_tracked[pkg] = pkg_data.get("binary", pkg)
    for pkg, pkg_info in packages.items():
        all_tracked[pkg] = get_pkg_binary(pkg_info)

    for pkg, binary in all_tracked.items():
        if pkg not in desired and (npm_bin / binary).exists():
            changes.append(f"  - remove {pkg}")

    for pkg, pkg_info in packages.items():
        binary = get_pkg_binary(pkg_info)
        if not (npm_bin / binary).exists():
            changes.append(f"  + install {pkg}")
        elif version_changed(pkg, pkg_info, state, "npm"):
            changes.append(f"  ~ update {pkg}")

    return changes


def _diff_uv(packages: Dict, paths: Dict, state: Dict):
    changes = []
    desired = set(packages.keys())

    binary_map = {pkg: get_pkg_binary(info) for pkg, info in packages.items()}
    for pkg, binary in binary_map.items():
        if not (Path(paths["uvBin"]) / binary).exists():
            source = get_pkg_source(packages[pkg])
            spec = source if source else pkg
            changes.append(f"  + install {spec}")
        elif version_changed(pkg, packages[pkg], state, "uv"):
            changes.append(f"  ~ update {pkg}")

    all_tracked = {}
    for pkg, pkg_data in state.get("uv", {}).get("packages", {}).items():
        all_tracked[pkg] = pkg_data.get("binary", pkg)
    for pkg, binary in all_tracked.items():
        if pkg not in desired and (Path(paths["uvBin"]) / binary).exists():
            changes.append(f"  - remove {pkg}")

    return changes


def _diff_mcp(servers: Dict, paths: Dict, state: Dict):
    changes = []
    claude = shutil.which("claude")
    if not claude:
        return ["  ? claude CLI not found, cannot diff MCP servers"]

    rc, stdout, _ = run_command([claude, "mcp", "list"], cwd=os.path.expanduser("~"))
    current = set()
    if rc == 0:
        for line in stdout.splitlines():
            parts = line.strip().split(":")
            if parts:
                current.add(parts[0].strip())

    desired = set(servers.keys())
    for name in sorted(desired - current):
        changes.append(f"  + install {name}")
    for name in sorted(current - desired):
        changes.append(f"  - remove {name}")

    return changes


def _diff_curl_shell(curl_shell: Dict, state: Dict):
    changes = []
    installed = set(state.get("curlShell", {}).get("installed", []))
    desired = set(curl_shell.keys())
    for name in sorted(desired - installed):
        changes.append(f"  + install {name}")
    return changes


def _diff_git_repos(git_repos: Dict, state: Dict):
    changes = []
    installed = set(state.get("gitRepos", {}).get("installed", []))
    for dest, _url in git_repos.items():
        expanded = os.path.expanduser(dest)
        if not os.path.isdir(expanded):
            changes.append(f"  + clone {dest}")
        elif dest not in installed:
            changes.append(f"  + track {dest}")
    return changes


def _diff_config_files(config_files: List[Dict[str, str]], config_dir: str, state: Dict):
    changes = []
    state_files = state.get("configFiles", {})
    managed_targets = set()

    skip_dirs = {".git", ".hg", ".svn", "__pycache__"}
    skip_files = {".DS_Store", ".gitignore", ".gitkeep"}

    for entry in config_files:
        source_dir = os.path.join(config_dir, entry["dir"])
        file_type = entry.get("type", "dotfiles")

        if not os.path.isdir(source_dir):
            changes.append(f"  ! source directory not found: {entry['dir']}")
            continue

        if file_type == "dotfiles":
            target_base = os.path.expanduser("~")
        else:
            continue

        for root, dirs, files in os.walk(source_dir):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for name in sorted(files):
                if name in skip_files:
                    continue
                source = os.path.join(root, name)
                rel_path = os.path.relpath(source, source_dir)
                target = os.path.join(target_base, rel_path)
                managed_targets.add(target)

                if not os.path.exists(target):
                    changes.append(f"  + create {target}")
                else:
                    try:
                        with open(source, "rb") as f:
                            src_hash = hashlib.sha256(f.read()).hexdigest()
                        with open(target, "rb") as f:
                            tgt_hash = hashlib.sha256(f.read()).hexdigest()
                        if src_hash != tgt_hash:
                            changes.append(f"  ~ update {target}")
                    except OSError:
                        changes.append(f"  ? cannot read {target}")

    for target in state_files:
        if target not in managed_targets:
            if os.path.exists(target):
                changes.append(f"  - remove {target}")

    return changes


def _diff_brew(brew_config: Dict):
    changes = []
    if sys.platform != "darwin":
        return changes

    brew = shutil.which("brew")
    if not brew:
        return ["  ? brew not found"]

    env = os.environ.copy()
    env["PATH"] = f"{os.path.dirname(brew)}:{env.get('PATH', '')}"

    desired_brews = set(brew_config.get("brews", []))
    desired_casks = set(brew_config.get("casks", []))
    desired_taps = set(brew_config.get("taps", []))

    rc, stdout, _ = run_command([brew, "list", "--formula", "-1"], env)
    installed_formulas = (
        {line.strip() for line in stdout.splitlines() if line.strip()} if rc == 0 else set()
    )

    rc, stdout, _ = run_command([brew, "list", "--cask", "-1"], env)
    installed_casks = (
        {line.strip() for line in stdout.splitlines() if line.strip()} if rc == 0 else set()
    )

    rc, stdout, _ = run_command([brew, "tap"], env)
    installed_taps = (
        {line.strip() for line in stdout.splitlines() if line.strip()} if rc == 0 else set()
    )

    for tap in sorted(desired_taps - installed_taps):
        changes.append(f"  + tap {tap}")
    for formula in sorted(desired_brews - installed_formulas):
        changes.append(f"  + install {formula}")
    for cask in sorted(desired_casks - installed_casks):
        changes.append(f"  + install --cask {cask}")

    cleanup = brew_config.get("onActivation", {}).get("cleanup") == "zap"
    if cleanup:
        rc, stdout, _ = run_command([brew, "leaves"], env)
        leaves = (
            {line.strip() for line in stdout.splitlines() if line.strip()} if rc == 0 else set()
        )
        for formula in sorted(leaves - desired_brews):
            changes.append(f"  - remove {formula}")
        for cask in sorted(installed_casks - desired_casks):
            changes.append(f"  - remove --cask {cask}")
        for tap in sorted(installed_taps - desired_taps):
            changes.append(f"  - untap {tap}")

    return changes


def show_diff(config: dict, config_dir: str) -> bool:
    """Show what deploy would change. Returns True if no changes needed."""
    state_file = os.path.expanduser(config["stateFile"])
    state = {}
    if os.path.exists(state_file):
        from tools.state import load_json

        state = load_json(state_file)

    has_changes = False

    sections = []

    bun_config = config.get("bun", {})
    bun_packages = bun_config.get("packages", {})
    if bun_packages or state.get("bun", {}).get("packages"):
        changes = _diff_bun(
            bun_packages, config["paths"], state, {"configFile": bun_config.get("configFile")}
        )
        if changes:
            sections.append(("bun", changes))

    npm_config = config.get("npm", {})
    npm_packages = npm_config.get("packages", {})
    if npm_packages or state.get("npm", {}).get("packages"):
        changes = _diff_npm(
            npm_packages, config["paths"], state, {"configFile": npm_config.get("configFile")}
        )
        if changes:
            sections.append(("npm", changes))

    if config.get("uv", {}).get("packages") or state.get("uv", {}).get("packages"):
        changes = _diff_uv(config.get("uv", {}).get("packages", {}), config["paths"], state)
        if changes:
            sections.append(("uv", changes))

    changes = _diff_mcp(config.get("mcp", {}).get("servers", {}), config["paths"], state)
    if changes:
        sections.append(("mcp", changes))

    if config.get("curlShell"):
        changes = _diff_curl_shell(config["curlShell"], state)
        if changes:
            sections.append(("curlShell", changes))

    if config.get("gitRepos"):
        changes = _diff_git_repos(config["gitRepos"], state)
        if changes:
            sections.append(("gitRepos", changes))

    changes = _diff_config_files(config.get("configFiles", []), config_dir, state)
    if changes:
        sections.append(("configFiles", changes))

    changes = _diff_brew(config.get("brew", {}))
    if changes:
        sections.append(("brew", changes))

    if sections:
        has_changes = True
        for title, items in sections:
            _section(title)
            for item in items:
                log(item, Color.YELLOW)

    if not has_changes:
        log("No changes needed — system is up to date.", Color.GREEN)

    return not has_changes
