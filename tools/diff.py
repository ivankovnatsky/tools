"""Dry-run diff: show what deploy would change without modifying anything."""

import hashlib
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Dict, List

from tools.log import Color, log
from tools.user.brew import _normalize_tap
from tools.util import get_pkg_binary, get_pkg_source, run_command, version_changed

ALL_SECTIONS = ("bun", "npm", "uv", "mcp", "curlShell", "gitRepos", "files", "brew")


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
            line = line.strip()
            if ": " in line and ("http" in line or "stdio" in line or "npx" in line):
                name = line.split(":")[0].strip()
                if name:
                    current.add(name)

    desired = set(servers.keys())
    managed = set(state.get("mcp", {}).get("servers", {}).keys())
    for name in sorted(desired - current):
        changes.append(f"  + install {name}")
    for name in sorted(managed - desired):
        if name in current:
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


def _diff_files(entries: List[Dict[str, object]], config_dir: str, state: Dict):
    """Dry-run for the unified `files:` section.

    Resolves entries via `_resolve_entries`, compares each desired
    (target, source, mode) against the live filesystem, and accounts for
    cleanup against state.
    """
    from tools.user.files import _resolve_entries

    state_files = state.get("files", {})
    changes: List[str] = []

    resolved, errors = _resolve_entries(entries, config_dir)
    for err in errors:
        changes.append(f"  ! {err}")

    managed_targets = set()
    for target, source, desired_mode in resolved:
        managed_targets.add(target)

        if not os.path.exists(target):
            changes.append(f"  + create {target}")
            continue

        try:
            with open(source, "rb") as f:
                src_hash = hashlib.sha256(f.read()).hexdigest()
            with open(target, "rb") as f:
                tgt_hash = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            changes.append(f"  ? cannot read {target}")
            continue

        target_mode = stat.S_IMODE(os.stat(target).st_mode)
        source_mode = stat.S_IMODE(os.stat(source).st_mode)
        effective_mode = desired_mode if desired_mode is not None else source_mode

        if src_hash != tgt_hash:
            changes.append(f"  ~ update {target}")
        elif effective_mode != target_mode:
            changes.append(f"  ~ chmod {target} -> {oct(effective_mode)}")

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
    for key, value in brew_config.get("environment", {}).items():
        env[key] = str(value)

    desired_brews = set(brew_config.get("brews", []))
    desired_casks = set(brew_config.get("casks", []))
    desired_taps = {_normalize_tap(t) for t in brew_config.get("taps", [])}

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

    tap_changes = []
    for tap in sorted(desired_taps - installed_taps):
        tap_changes.append(f"    + tap {tap}")

    formula_changes = []
    for formula in sorted(desired_brews - installed_formulas):
        formula_changes.append(f"    + install {formula}")

    cask_changes = []
    for cask in sorted(desired_casks - installed_casks):
        cask_changes.append(f"    + install {cask}")

    cleanup = brew_config.get("onActivation", {}).get("cleanup") == "zap"
    if cleanup:
        rc, stdout, _ = run_command([brew, "leaves"], env)
        leaves = (
            {line.strip() for line in stdout.splitlines() if line.strip()} if rc == 0 else set()
        )
        for formula in sorted(leaves - desired_brews):
            formula_changes.append(f"    - remove {formula}")
        for cask in sorted(installed_casks - desired_casks):
            cask_changes.append(f"    - remove {cask}")
        for tap in sorted(installed_taps - desired_taps):
            tap_changes.append(f"    - untap {tap}")

        rc, stdout, _ = run_command([brew, "autoremove", "--dry-run"], env)
        if rc == 0:
            for line in stdout.splitlines():
                line = line.strip()
                if line and not line.startswith("="):
                    formula_changes.append(f"    - autoremove {line}")

    subsections = []
    if tap_changes:
        subsections.append(("taps", tap_changes))
    if formula_changes:
        subsections.append(("brews", formula_changes))
    if cask_changes:
        subsections.append(("casks", cask_changes))

    return subsections


def show_diff(config: dict, config_dir: str, scope: tuple[str, ...] = ()) -> bool:
    """Show what deploy would change. Returns True if no changes needed."""
    from tools.state import load_json

    state = {}
    state_file = os.path.expanduser(config.get("stateFile") or "~/.local/state/tools/state.json")
    if os.path.exists(state_file):
        state = load_json(state_file)

    active = set(scope) if scope else set(ALL_SECTIONS)
    has_changes = False
    paths = config.get("paths", {})

    sections = []

    if "bun" in active:
        bun_config = config.get("bun", {})
        bun_packages = bun_config.get("packages", {})
        if (bun_packages or state.get("bun", {}).get("packages")) and paths.get("bunBin"):
            changes = _diff_bun(
                bun_packages, paths, state, {"configFile": bun_config.get("configFile")}
            )
            if changes:
                sections.append(("bun", changes))

    if "npm" in active:
        npm_config = config.get("npm", {})
        npm_packages = npm_config.get("packages", {})
        if (npm_packages or state.get("npm", {}).get("packages")) and paths.get("npmBin"):
            changes = _diff_npm(
                npm_packages, paths, state, {"configFile": npm_config.get("configFile")}
            )
            if changes:
                sections.append(("npm", changes))

    if "uv" in active:
        if (
            config.get("uv", {}).get("packages") or state.get("uv", {}).get("packages")
        ) and paths.get("uvBin"):
            changes = _diff_uv(config.get("uv", {}).get("packages", {}), paths, state)
            if changes:
                sections.append(("uv", changes))

    if "mcp" in active:
        changes = _diff_mcp(config.get("mcp", {}).get("servers", {}), paths, state)
        if changes:
            sections.append(("mcp", changes))

    if "curlShell" in active and config.get("curlShell"):
        changes = _diff_curl_shell(config["curlShell"], state)
        if changes:
            sections.append(("curlShell", changes))

    if "gitRepos" in active and config.get("gitRepos"):
        changes = _diff_git_repos(config["gitRepos"], state)
        if changes:
            sections.append(("gitRepos", changes))

    if "files" in active:
        changes = _diff_files(config.get("files", []) or [], config_dir, state)
        if changes:
            sections.append(("files", changes))

    if "brew" in active:
        brew_subsections = _diff_brew(config.get("brew", {}))
        if brew_subsections:
            sections.append(("brew", brew_subsections))

    if sections:
        has_changes = True
        for i, (title, items) in enumerate(sections):
            prefix = "\n" if i > 0 else ""
            log(f"{prefix}{title}:", Color.BLUE)
            if items and isinstance(items[0], tuple):
                for sub_title, sub_items in items:
                    log(f"  {sub_title}:", Color.BLUE)
                    for item in sub_items:
                        log(item, Color.YELLOW)
            else:
                for item in items:
                    log(item, Color.YELLOW)

    if not has_changes:
        log("No changes needed — system is up to date.", Color.GREEN)

    return not has_changes
