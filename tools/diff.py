"""Dry-run diff: show what deploy would change without modifying anything."""

import hashlib
import os
import stat
import sys
from typing import Dict, List

from tools.log import Color, log
from tools.user.brew import _normalize_tap
from tools.user.flatpak import diff_flatpak
from tools.user.ollama_models import diff_ollama_models
from tools.util import (
    format_diff_bytes,
    get_pkg_source,
    looks_like_secret,
    version_changed,
)

ALL_SECTIONS = (
    "bun",
    "npm",
    "uv",
    "go",
    "mcp",
    "curlShell",
    "gitRepos",
    "files",
    "brew",
    "ollamaModels",
    "flatpak",
)


def _emit_change(item: str) -> None:
    """Print one change row.

    Single-line items get the standard yellow change-row treatment.
    Multi-line items (e.g. inline file content diffs from
    `format_diff_bytes`) carry their own ANSI from `delta` or are
    pre-padded for the difflib fallback, so we print them as-is to
    avoid the outer yellow wrapper clobbering inner colors.
    """
    if "\n" in item:
        print(item)
    else:
        log(item, Color.YELLOW)


def _diff_bun(packages: Dict, paths: Dict, state: Dict, bun_config: Dict):
    changes = []
    desired = set(packages.keys())
    state_packages = set(state.get("bun", {}).get("packages", {}).keys())

    for pkg in sorted(state_packages - desired):
        changes.append(f"  - remove {pkg}")

    for pkg, pkg_info in packages.items():
        if pkg not in state_packages:
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
    desired = set(packages.keys())
    state_packages = set(state.get("npm", {}).get("packages", {}).keys())

    for pkg in sorted(state_packages - desired):
        changes.append(f"  - remove {pkg}")

    for pkg, pkg_info in packages.items():
        if pkg not in state_packages:
            changes.append(f"  + install {pkg}")
        elif version_changed(pkg, pkg_info, state, "npm"):
            changes.append(f"  ~ update {pkg}")

    return changes


def _diff_uv(packages: Dict, paths: Dict, state: Dict):
    changes = []
    desired = set(packages.keys())
    state_packages = set(state.get("uv", {}).get("packages", {}).keys())

    for pkg, pkg_info in packages.items():
        if pkg not in state_packages:
            source = get_pkg_source(pkg_info)
            spec = source if source else pkg
            changes.append(f"  + install {spec}")
        elif version_changed(pkg, pkg_info, state, "uv"):
            changes.append(f"  ~ update {pkg}")

    for pkg in sorted(state_packages - desired):
        changes.append(f"  - remove {pkg}")

    return changes


def _diff_go(packages: Dict, paths: Dict, state: Dict):
    changes = []
    desired = set(packages.keys())
    state_packages = set(state.get("go", {}).get("packages", {}).keys())

    for pkg, pkg_info in packages.items():
        if pkg not in state_packages:
            source = get_pkg_source(pkg_info) or pkg
            changes.append(f"  + install {source}")
        elif version_changed(pkg, pkg_info, state, "go"):
            changes.append(f"  ~ update {pkg}")

    # Go has no uninstall; dropping a package leaves its binary in $GOBIN.
    for pkg in sorted(state_packages - desired):
        changes.append(f"  - drop {pkg} (binary remains in $GOBIN)")

    return changes


def _diff_mcp(servers: Dict, paths: Dict, state: Dict):
    from tools.user.mcp import (
        build_mcp_env,
        get_installed_mcp_servers,
        resolve_claude_cli,
        server_fingerprint,
    )

    changes = []
    desired = set(servers.keys())
    managed = set(state.get("mcp", {}).get("servers", {}).keys())
    # Nothing desired and nothing tracked: skip the `claude mcp list` shell-out
    # entirely, so machines without claude do not fail every diff.
    if not desired and not managed:
        return changes

    claude = resolve_claude_cli(paths)
    if not claude:
        return ["  ? claude CLI not found, cannot diff MCP servers"]

    # Same resolver and parser as deploy, or the two disagree about what is
    # installed and the diff never matches what gets applied.
    current = get_installed_mcp_servers(claude, build_mcp_env(paths))
    tracked_cfg = state.get("mcp", {}).get("servers", {})
    for name in sorted(desired - current):
        changes.append(f"  + install {name}")
    for name in sorted(desired & managed & current):
        if server_fingerprint(servers[name]) != server_fingerprint(tracked_cfg[name]):
            changes.append(f"  ~ re-register {name}")
    for name in sorted((managed & current) - desired):
        changes.append(f"  - remove {name}")
    for name in sorted(managed - current - desired):
        changes.append(f"  ~ forget {name}")

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
    # Removal is an rmtree of the whole checkout, so it must never reach the
    # user as a surprise — this is the one section where a missing preview
    # line costs data rather than a confusing diff.
    for dest in sorted(installed - set(git_repos)):
        changes.append(f"  - remove {dest} (deletes the checkout)")
    return changes


def _diff_files(entries: List[Dict[str, object]], config_dir: str, state: Dict):
    """Dry-run for the unified `files:` section.

    Resolves entries via `_resolve_entries`, compares each desired
    (target, source, mode, secrets) against the live filesystem, and
    accounts for cleanup against state.
    """
    from tools.user.files import _resolve_entries

    state_files = state.get("files", {})
    changes: List[str] = []

    resolved, errors = _resolve_entries(entries, config_dir)
    for err in errors:
        changes.append(f"  ! {err}")

    managed_targets = set()
    for target, source, desired_mode, secrets in resolved:
        managed_targets.add(target)

        if not os.path.exists(target):
            changes.append(f"  + create {target}")
            continue

        try:
            with open(source, "rb") as f:
                src_bytes = f.read()
        except OSError:
            changes.append(f"  ? cannot read source {source}")
            continue
        try:
            with open(target, "rb") as f:
                tgt_bytes = f.read()
        except OSError:
            changes.append(f"  ? cannot read {target}")
            continue

        src_hash = hashlib.sha256(src_bytes).hexdigest()
        tgt_hash = hashlib.sha256(tgt_bytes).hexdigest()

        target_mode = stat.S_IMODE(os.stat(target).st_mode)
        source_mode = stat.S_IMODE(os.stat(source).st_mode)
        effective_mode = desired_mode if desired_mode is not None else source_mode

        if src_hash != tgt_hash:
            is_secret = secrets or looks_like_secret(src_bytes) or looks_like_secret(tgt_bytes)
            if is_secret:
                changes.append(f"  ~ update {target} (secret, diff suppressed)")
            else:
                changes.append(f"  ~ update {target}")
                diff_text = format_diff_bytes(src_bytes, tgt_bytes, target)
                if diff_text:
                    changes.append(diff_text)
        elif effective_mode != target_mode:
            changes.append(f"  ~ chmod {target} -> {oct(effective_mode)}")

    for target in state_files:
        if target not in managed_targets:
            if os.path.exists(target):
                changes.append(f"  - remove {target}")

    return changes


def _diff_brew(brew_config: Dict, state: Dict):
    if sys.platform != "darwin":
        return []

    desired_brews = set(brew_config.get("brews", []))
    desired_casks = set(brew_config.get("casks", []))
    desired_taps = {_normalize_tap(t) for t in brew_config.get("taps", [])}
    desired_mas = brew_config.get("masApps", {}) or {}

    # State-only, matching install_brew_packages: reconcile desired against what
    # we recorded installing, never live `brew list`.
    prev = state.get("brew", {})
    prev_brews = set(prev.get("brews", []))
    prev_casks = set(prev.get("casks", []))
    prev_taps = {_normalize_tap(t) for t in prev.get("taps", [])}
    prev_mas = prev.get("masApps", {}) or {}

    tap_changes = [f"    + tap {t}" for t in sorted(desired_taps - prev_taps)]
    formula_changes = [f"    + install {f}" for f in sorted(desired_brews - prev_brews)]
    cask_changes = [f"    + install {c}" for c in sorted(desired_casks - prev_casks)]

    formula_changes += [f"    - remove {f}" for f in sorted(prev_brews - desired_brews)]
    cask_changes += [f"    - remove {c}" for c in sorted(prev_casks - desired_casks)]
    tap_changes += [f"    - untap {t}" for t in sorted(prev_taps - desired_taps)]

    # Mac App Store apps are reconciled by deploy, so they have to appear here
    # too — otherwise a MAS-only difference short-circuits and its uninstalls
    # never show up in the approval preview.
    prev_ids = {str(v) for v in prev_mas.values()}
    desired_ids = {str(v) for v in desired_mas.values()}
    mas_changes = [
        f"    + install {name} ({app_id})"
        for name, app_id in sorted(desired_mas.items())
        if str(app_id) not in prev_ids
    ]
    mas_changes += [
        f"    - remove {name} ({app_id})"
        for name, app_id in sorted(prev_mas.items())
        if str(app_id) not in desired_ids
    ]

    subsections = []
    if tap_changes:
        subsections.append(("taps", tap_changes))
    if formula_changes:
        subsections.append(("brews", formula_changes))
    if cask_changes:
        subsections.append(("casks", cask_changes))
    if mas_changes:
        subsections.append(("masApps", mas_changes))

    return subsections


def show_diff(config: dict, config_dir: str, scope: tuple[str, ...] = ()) -> bool:
    """Show what deploy would change. Returns True if no changes needed."""
    from tools.state import load_json, migrate_state_file, migrate_state_schema

    state = {}
    state_file = os.path.expanduser(config.get("stateFile") or "~/.local/state/tools/state.json")
    # deploy relocates legacy state before reading it; without the same step
    # here a not-yet-migrated machine previews everything as a fresh install.
    migrate_state_file(state_file)
    if os.path.exists(state_file):
        # Preview against the same view deploy will act on, or the two disagree
        # on every legacy entry.
        state = migrate_state_schema(load_json(state_file))

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

    if "go" in active:
        go_state = state.get("go", {})
        if config.get("go", {}).get("packages") or go_state.get("packages"):
            changes = _diff_go(config.get("go", {}).get("packages", {}), paths, state)
            if changes:
                sections.append(("go", changes))

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
        brew_subsections = _diff_brew(config.get("brew", {}), state)
        if brew_subsections:
            sections.append(("brew", brew_subsections))

    if "ollamaModels" in active:
        changes = diff_ollama_models(config.get("ollamaModels", {}) or {}, state)
        if changes:
            sections.append(("ollamaModels", changes))

    if "flatpak" in active:
        changes = diff_flatpak(config.get("flatpak", {}) or {}, state)
        if changes:
            sections.append(("flatpak", changes))

    if sections:
        has_changes = True
        for i, (title, items) in enumerate(sections):
            prefix = "\n" if i > 0 else ""
            log(f"{prefix}{title}:", Color.BLUE)
            if items and isinstance(items[0], tuple):
                for sub_title, sub_items in items:
                    log(f"  {sub_title}:", Color.BLUE)
                    for item in sub_items:
                        _emit_change(item)
            else:
                for item in items:
                    _emit_change(item)

    if not has_changes:
        log("No changes needed — system is up to date.", Color.GREEN)

    return not has_changes
