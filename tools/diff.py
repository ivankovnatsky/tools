"""Dry-run diff: show what deploy would change without modifying anything."""

import hashlib
import os
import stat
from typing import Dict, List

from tools.log import Color, log
from tools.services import diff_services
from tools.services import has_work as services_has_work
from tools.services import released_rows as services_released_rows
from tools.services.keys import key_path as services_key_path
from tools.user import secrets_flow
from tools.user import sops as sops_section
from tools.user import templates as templates_section
from tools.user.brew import diff_brew
from tools.user.defaults import diff_defaults
from tools.user.defaults import has_work as defaults_has_work
from tools.user.dock import diff_dock
from tools.user.dock import has_work as dock_has_work
from tools.user.flatpak import diff_flatpak
from tools.user.git_repos import is_checkout
from tools.user.launchd import diff_launchd
from tools.user.launchd import has_work as launchd_has_work
from tools.user.links import diff_links
from tools.user.links import has_work as links_has_work
from tools.user.ollama_models import diff_ollama_models
from tools.user.skills import diff_skills
from tools.user.skills import has_work as skills_has_work
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
    "pi",
    "mcp",
    "curlShell",
    "gitRepos",
    "sops",
    "templates",
    "files",
    "skills",
    "links",
    "brew",
    "ollamaModels",
    "flatpak",
    "launchd",
    "defaults",
    "dock",
    "services",
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


def _diff_pi(packages: Dict, paths: Dict, state: Dict):
    changes = []
    desired = set(packages.keys())
    state_packages = set(state.get("pi", {}).get("packages", {}).keys())

    for pkg in sorted(state_packages - desired):
        changes.append(f"  - remove {pkg}")

    for pkg, pkg_info in packages.items():
        if pkg not in state_packages:
            source = get_pkg_source(pkg_info)
            spec = source if source else pkg
            changes.append(f"  + install {spec}")
        elif version_changed(pkg, pkg_info, state, "pi"):
            changes.append(f"  ~ update {pkg}")

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
    if current is None:
        return ["  ? failed to list MCP servers, cannot diff"]
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
    # Deploy releases dropped URLs from state (there is no uninstall); the
    # preview must show that or the interactive path never reconciles it.
    for name in sorted(installed - desired):
        changes.append(f"  ~ forget {name} (no uninstall; releases tracking)")
    return changes


def _diff_git_repos(git_repos: Dict, state: Dict):
    changes = []
    installed = set(state.get("gitRepos", {}).get("installed", []))
    # Pulls of already-tracked checkouts are deliberately not previewed:
    # they are non-destructive and would make every diff report changes.
    # They run on every deploy (the clean-diff path included).
    for dest, _url in git_repos.items():
        expanded = os.path.expanduser(dest)
        if not os.path.exists(expanded):
            changes.append(f"  + clone {dest}")
        elif not is_checkout(expanded):
            changes.append(f"  ! not a git repo: {dest} (deploy skips it)")
        elif dest not in installed:
            changes.append(f"  + track {dest}")
    # Removal is an rmtree of the whole checkout, so it must never reach the
    # user as a surprise — this is the one section where a missing preview
    # line costs data rather than a confusing diff.
    for dest in sorted(installed - set(git_repos)):
        changes.append(f"  - remove {dest} (deletes the checkout)")
    return changes


def _diff_files(entries: List[Dict[str, object]], config_dir: str, state: Dict, conflicts=None):
    """Dry-run for the unified `files:` section.

    Resolves entries via `_resolve_entries`, compares each desired entry
    against the live filesystem, and accounts for cleanup against state.
    """
    from tools.fsutil import removal_blocked, respell, same_file_as
    from tools.user.files import (
        _conflict,
        _dir_kept,
        _is_real_dir,
        _ordered,
        _released_dir,
        _removable,
        _removal_order,
        _resolve_entries,
        _tracked,
        declared_hooks,
    )
    from tools.util import pending_hooks

    changes: List[str] = []
    hooks: Dict[str, None] = {}

    resolved, errors = _resolve_entries(entries, config_dir)
    for err in errors:
        changes.append(f"  ! {err}")
    declared = [e.target for e in resolved]
    state_files = respell(dict(state.get("files", {}) or {}), declared)

    managed_targets = set()
    for e in _ordered(resolved):
        target, source, desired_mode, secrets = e.target, e.source, e.mode, e.secrets
        managed_targets.add(target)
        reason = _conflict(target, conflicts)
        if reason:
            changes.append(f"  ! refusing {target}: {reason}")
            continue
        before = len(changes)

        if e.directory:
            if not os.path.lexists(target):
                changes.append(f"  + create directory {target}")
            elif not _is_real_dir(target):
                changes.append(f"  ! {target} exists and is not a directory")
                continue
            elif desired_mode is not None and stat.S_IMODE(os.lstat(target).st_mode) != (
                desired_mode
            ):
                changes.append(f"  ~ chmod {target} -> {oct(desired_mode)}")
        elif not os.path.exists(target):
            changes.append(f"  + create {target}")
        else:
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
                if not _tracked(target, state_files.get(target)):
                    changes.append(
                        f"  ~ replace {target} (not tracked by tools; content not shown)"
                    )
                elif is_secret:
                    changes.append(f"  ~ update {target} (secret, diff suppressed)")
                else:
                    changes.append(f"  ~ update {target}")
                    diff_text = format_diff_bytes(src_bytes, tgt_bytes, target)
                    if diff_text:
                        changes.append(diff_text)
            elif effective_mode != target_mode:
                changes.append(f"  ~ chmod {target} -> {oct(effective_mode)}")

        if e.on_change and len(changes) > before:
            hooks[e.on_change] = None

    # Deploy holds removals on config errors; a refused target stays tracked.
    removed = set()
    for target in _removal_order(state_files, state_files) if not errors else ():
        if target not in managed_targets:
            record = state_files.get(target)
            blocked = removal_blocked(conflicts, target)
            twin = same_file_as(target, declared)
            if twin:
                blocked = f"the same file as declared {twin}"
            if blocked:
                changes.append(f"  ~ keep {target} (is {blocked}; not removed)")
                continue
            if os.path.exists(target):
                if isinstance(record, dict) and record.get("dir"):
                    if _released_dir(target, conflicts):
                        changes.append(
                            f"  ~ release {target} (another section manages a path inside)"
                        )
                        continue
                    why = _dir_kept(target, record)
                    if why == "not empty" and _empty_after(target, removed):
                        why = None
                    if why:
                        changes.append(f"  ~ keep {target} ({why}; not removed)")
                        continue
                elif not _removable(target, record):
                    changes.append(
                        f"  ~ keep {target} (modified since tools wrote it; not removed)"
                    )
                    continue
                changes.append(f"  - remove {target}")
                removed.add(target)

    for command in pending_hooks(state, "files", declared_hooks(entries)):
        hooks[command] = None
    changes += [f"  ~ run onChange: {command}" for command in hooks]
    return changes


def _empty_after(directory: str, removed) -> bool:
    """Whether ``directory`` holds nothing but entries this diff removes."""
    try:
        names = os.listdir(directory)
    except OSError:
        return False
    return all(os.path.join(directory, n) in removed for n in names)


def show_diff(config: dict, config_dir: str, scope: tuple[str, ...] = ()) -> bool:
    """Show what deploy would change. Returns True if no changes needed."""
    from tools.state import find_state_file, load_json, migrate_state_schema

    state = {}
    state_file = os.path.expanduser(config.get("stateFile") or "~/.local/state/tools/state.json")
    # Deploy keeps the key beside the migrated state file, not a legacy one.
    services_key = services_key_path(state_file)
    # Read the same state deploy would act on — including a not-yet-migrated
    # legacy location — but without copying anything: diff is a dry run and
    # must not modify the filesystem.
    state_file = find_state_file(state_file)
    if os.path.exists(state_file):
        state = migrate_state_schema(load_json(state_file))

    active = set(scope) if scope else set(ALL_SECTIONS)
    has_changes = False
    paths = config.get("paths", {})

    sections = []

    # Section gates must mirror _deploy exactly: any extra condition here
    # (e.g. requiring a paths key) makes diff report "clean" for work deploy
    # would still attempt.
    if "bun" in active:
        bun_config = config.get("bun", {})
        bun_packages = bun_config.get("packages", {})
        if bun_packages or bun_config.get("configFile") or state.get("bun", {}).get("packages"):
            changes = _diff_bun(
                bun_packages, paths, state, {"configFile": bun_config.get("configFile")}
            )
            if changes:
                sections.append(("bun", changes))

    if "npm" in active:
        npm_config = config.get("npm", {})
        npm_packages = npm_config.get("packages", {})
        if npm_packages or npm_config.get("configFile") or state.get("npm", {}).get("packages"):
            changes = _diff_npm(
                npm_packages, paths, state, {"configFile": npm_config.get("configFile")}
            )
            if changes:
                sections.append(("npm", changes))

    if "uv" in active:
        if config.get("uv", {}).get("packages") or state.get("uv", {}).get("packages"):
            changes = _diff_uv(config.get("uv", {}).get("packages", {}), paths, state)
            if changes:
                sections.append(("uv", changes))

    if "go" in active:
        go_state = state.get("go", {})
        if config.get("go", {}).get("packages") or go_state.get("packages"):
            changes = _diff_go(config.get("go", {}).get("packages", {}), paths, state)
            if changes:
                sections.append(("go", changes))

    if "pi" in active:
        if config.get("pi", {}).get("packages") or state.get("pi", {}).get("packages"):
            changes = _diff_pi(config.get("pi", {}).get("packages", {}), paths, state)
            if changes:
                sections.append(("pi", changes))

    if "mcp" in active:
        changes = _diff_mcp(config.get("mcp", {}).get("servers", {}), paths, state)
        if changes:
            sections.append(("mcp", changes))

    if "curlShell" in active and (
        config.get("curlShell") or state.get("curlShell", {}).get("installed")
    ):
        changes = _diff_curl_shell(config.get("curlShell") or {}, state)
        if changes:
            sections.append(("curlShell", changes))

    # One decryptor for both: each sops file is decrypted once, in memory.
    decryptor = None
    run_sops = "sops" in active and sops_section.has_work(config.get("sops"), state)
    run_templates = "templates" in active and templates_section.has_work(
        config.get("templates"), state
    )
    if run_sops or run_templates:
        decryptor = sops_section.Decryptor(sops_section.parse_sops(config.get("sops")).settings)
    conflicts = secrets_flow.ownership_conflicts(
        files_entries=config.get("files"),
        sops_raw=config.get("sops"),
        templates_raw=config.get("templates"),
        links_raw=config.get("links"),
        skills_raw=config.get("skills"),
        state=state,
        config_dir=config_dir,
        git_repos_raw=config.get("gitRepos"),
        clone_pending="gitRepos" in active,
    )
    # Same order as deploy: paths inside a gitRepos destination after the clone.
    inside = secrets_flow.paths_in_repos(
        config.get("gitRepos"), config.get("sops"), config.get("templates")
    )
    failed: tuple = ()

    def secrets(phase, suffix=""):
        nonlocal failed
        split = {"phase": phase, "inside": inside} if phase else {}
        if run_sops:
            sops_plan = secrets_flow.plan_sops(
                config.get("sops"), state, decryptor, conflicts, **split
            )
            failed += secrets_flow.failed_secrets(sops_plan)
            changes = sops_section.describe(sops_plan)
            if changes:
                sections.append(("sops" + suffix, changes))
        if run_templates:
            changes = secrets_flow.diff_templates(
                config.get("templates"),
                config.get("sops"),
                state,
                decryptor,
                conflicts,
                failed,
                **split,
            )
            if changes:
                sections.append(("templates" + suffix, changes))

    secrets(secrets_flow.EARLY if inside else None)

    # Gate on state too: dropping the whole gitRepos block is exactly the
    # case where deploy rmtrees every tracked checkout — that must never be
    # invisible in the preview.
    if "gitRepos" in active and (
        config.get("gitRepos") or state.get("gitRepos", {}).get("installed")
    ):
        changes = _diff_git_repos(config.get("gitRepos") or {}, state)
        if changes:
            sections.append(("gitRepos", changes))

    if inside:
        secrets(secrets_flow.LATE, " (after gitRepos)")

    if "files" in active:
        changes = _diff_files(config.get("files", []) or [], config_dir, state, conflicts)
        if changes:
            sections.append(("files", changes))

    if "skills" in active and skills_has_work(config.get("skills") or {}, state):
        changes = diff_skills(config.get("skills") or {}, config_dir, state)
        if changes:
            sections.append(("skills", changes))

    if "links" in active and links_has_work(config.get("links") or [], state):
        changes = diff_links(config.get("links") or [], state)
        if changes:
            sections.append(("links", changes))

    if "brew" in active:
        brew_subsections = diff_brew(config.get("brew"), state)
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

    if "launchd" in active and launchd_has_work(config.get("launchd"), state):
        changes = diff_launchd(
            config.get("launchd"),
            state,
            **sops_section.secrets_dir_kwargs(config.get("sops")),
        )
        if changes:
            sections.append(("launchd", changes))

    restarts: List[str] = []
    if "defaults" in active and defaults_has_work(config.get("defaults"), state):
        changes = diff_defaults(config.get("defaults"), state, restarts=restarts)
        if changes:
            sections.append(("defaults", changes))

    if "dock" in active and dock_has_work(config.get("dock"), state):
        changes = diff_dock(config.get("dock"), state, restarts=restarts)
        if changes:
            sections.append(("dock", changes))

    if restarts:
        sections.append(("restart", [f"  ~ restart {proc}" for proc in restarts]))

    if "services" in active:
        if services_has_work(config.get("services")):
            subsections = diff_services(
                config.get("services"),
                config_dir,
                state,
                services_key,
                **sops_section.secrets_dir_kwargs(config.get("sops")),
            )
        else:
            subsections = services_released_rows(config.get("services"), state)
        if subsections:
            sections.append(("services", subsections))

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
