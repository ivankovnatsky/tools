"""Dry-run diff: show what deploy would change without modifying anything."""

import os
import stat
from pathlib import Path
from typing import Dict, List, Optional

from tools import privilege, selection
from tools.config import SECTIONS
from tools.log import Color, log
from tools.mgmt import diff_mgmt
from tools.mgmt import has_work as mgmt_has_work
from tools.mgmt import released_rows as mgmt_released_rows
from tools.mgmt.keys import key_path as mgmt_key_path
from tools.user import secrets_flow
from tools.user import sops as sops_section
from tools.user import templates as templates_section
from tools.user.brew import diff_brew
from tools.user.cargo import CrateRecordError, _crate_name, _resolve_cargo_bin
from tools.user.curl_shell import diff_curl_shell
from tools.user.defaults import diff_defaults
from tools.user.defaults import has_work as defaults_has_work
from tools.user.dock import diff_dock
from tools.user.dock import has_work as dock_has_work
from tools.user.env import diff_env
from tools.user.env import has_work as env_has_work
from tools.user.fetch import diff_fetch
from tools.user.fetch import has_work as fetch_has_work
from tools.user.files import has_system_work as files_has_system_work
from tools.user.flatpak import diff_flatpak
from tools.user.git_repos import is_checkout
from tools.user.go import _installed_path as _go_installed_path
from tools.user.go import _resolve_go_bin
from tools.user.launchd import diff_launchd
from tools.user.launchd import has_work as launchd_has_work
from tools.user.links import diff_links
from tools.user.links import has_work as links_has_work
from tools.user.npm import binary_missing
from tools.user.ollama_models import diff_ollama_models
from tools.user.plist import diff_plist
from tools.user.plist import has_work as plist_has_work
from tools.user.settingsctl import diff_settingsctl
from tools.user.settingsctl import has_root_work as settingsctl_has_root_work
from tools.user.skills import diff_skills
from tools.user.skills import has_work as skills_has_work
from tools.user.users import diff_users
from tools.user.users import has_work as users_has_work
from tools.util import (
    SKIPPED_ROW,
    format_diff_bytes,
    get_pkg_post_install,
    get_pkg_source,
    get_pkg_subpackages,
    looks_like_secret,
    package_entries,
    pkg_install_spec,
    pkg_unpinned,
    skipped_row,
    version_changed,
)

ALL_SECTIONS = SECTIONS


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


def _notify_rows(pkg_info) -> List[str]:
    units = pkg_info.get("restartUnits") if isinstance(pkg_info, dict) else None
    if not isinstance(units, list):
        return []
    return [f"    notify: {u}" for u in units if isinstance(u, str)]


def _bad_packages(section: str, packages) -> List[str]:
    """Config error rows for `<section>.packages`; deploy then runs nothing in the section."""
    return [f"  ! {err}" for err in package_entries(section, packages)[1]]


def _units_error(section: str, pkg: str, pkg_info) -> Optional[str]:
    """Why deploy skips (re)installing ``pkg``: its restartUnits do not parse."""
    try:
        secrets_flow.parse_restart_units(pkg_info.get("restartUnits"), f"{section}.packages.{pkg}")
    except ValueError as e:
        return str(e)
    return None


def _released(key: str, old, new, tracked, desired) -> List[str]:
    """Rows for a changed install location: deploy forgets what it tracked there."""
    rows = [f"  ~ {key} changed ({old} -> {new}); releasing tracked packages"]
    rows += [f"  ~ forget {pkg} (left in the old location)" for pkg in sorted(tracked - desired)]
    return rows


def _diff_js(section: str, packages: Dict, state: Dict, prefix_key: str, paths: Dict):
    """bun/npm removal rows and the tracked names deploy still acts on."""
    changes: List[str] = []
    desired = set(packages.keys())
    state_packages = set(state.get(section, {}).get("packages", {}).keys())
    old = state.get(section, {}).get("prefix", paths.get(prefix_key))
    if old != paths.get(prefix_key):
        if state_packages:
            changes += _released(
                f"{section} prefix", old, paths.get(prefix_key), state_packages, desired
            )
        state_packages = set()

    for pkg in sorted(state_packages - desired):
        changes.append(f"  - remove {pkg}")
    return changes, state_packages


def _diff_bun(packages: Dict, paths: Dict, state: Dict, bun_config: Dict, update: bool = False):
    changes, state_packages = _diff_js("bun", packages, state, "bunBin", paths)

    for pkg, pkg_info in packages.items():
        row = None
        if pkg not in state_packages:
            row = f"  + install {pkg}"
        elif version_changed(pkg, pkg_info, state, "bun"):
            row = f"  ~ update {pkg}"
        elif update and pkg_unpinned(pkg_info):
            row = f"  ~ upgrade {pkg}"
        if row:
            err = _units_error("bun", pkg, pkg_info)
            changes += [f"  ! {err}"] if err else [row] + _notify_rows(pkg_info)

    bunfig_content = bun_config.get("configFile")
    if bunfig_content and not state.get("bun", {}).get("bunfig_created"):
        if not os.path.exists(os.path.expanduser("~/.bunfig.toml")):
            changes.append("  + create ~/.bunfig.toml")

    return changes


def _diff_npm(packages: Dict, paths: Dict, state: Dict, npm_config: Dict, update: bool = False):
    changes: List[str] = []
    npmrc_content = npm_config.get("configFile")
    if npmrc_content and not state.get("npm", {}).get("npmrc_created"):
        if not os.path.exists(os.path.expanduser("~/.npmrc")):
            changes.append("  + create ~/.npmrc")

    rows, state_packages = _diff_js("npm", packages, state, "npmBin", paths)
    changes += rows
    stored = state.get("npm", {}).get("packages", {}) if state_packages else {}

    npm_bin = paths.get("npmBin")
    npm_lib = Path(npm_bin).parent / "lib" / "node_modules" if npm_bin else None
    for pkg, pkg_info in packages.items():
        row = None
        if pkg not in state_packages:
            row = f"  + install {pkg}"
        elif version_changed(pkg, pkg_info, state, "npm"):
            row = f"  ~ update {pkg}"
        elif npm_bin and binary_missing(pkg_info, npm_bin):
            row = f"  ~ reinstall {pkg} (binary {pkg_info['binary']} missing)"
        elif update and pkg_unpinned(pkg_info):
            row = f"  ~ upgrade {pkg}"
        installing = False
        if row:
            err = _units_error("npm", pkg, pkg_info)
            installing = not err
            changes += [f"  ! {err}"] if err else [row] + _notify_rows(pkg_info)
        if npm_lib is None:
            continue
        pkg_dir = npm_lib / pkg
        present = installing or pkg_dir.exists()
        entry = stored.get(pkg) if isinstance(stored.get(pkg), dict) else {}
        post_install = get_pkg_post_install(pkg_info)
        rerun = installing or post_install != entry.get("postInstall", "")
        if post_install and present and rerun:
            changes.append(f"  ~ run postInstall for {pkg}: {post_install}")
        subpkgs = get_pkg_subpackages(pkg_info)
        if not subpkgs or not present:
            continue
        stored_subs = entry.get("subpackages", {})
        if not isinstance(stored_subs, dict):
            stored_subs = {}
        specs = []
        for sp_name, sp_info in subpkgs.items():
            sp_version = sp_info.get("version", "latest")
            sp_stored = stored_subs.get(sp_name)
            stored_version = (
                sp_stored.get("version", "latest") if isinstance(sp_stored, dict) else None
            )
            if (
                installing
                or not (pkg_dir / "node_modules" / sp_name).exists()
                or sp_version != stored_version
            ):
                specs.append(pkg_install_spec(sp_name, sp_version))
        if specs:
            changes.append(f"  + install subpackages for {pkg}: {', '.join(specs)}")

    return changes


def _diff_uv(packages: Dict, paths: Dict, state: Dict, update: bool = False):
    changes = []
    desired = set(packages.keys())
    state_packages = set(state.get("uv", {}).get("packages", {}).keys())
    if paths.get("uvBin") and paths.get("uvToolDir"):
        context = f"{paths['uvBin']}|{paths['uvToolDir']}"
        old = state.get("uv", {}).get("context", context)
        if old != context:
            if state_packages:
                changes += _released("uv tool dir", old, context, state_packages, desired)
            state_packages = set()

    for pkg, pkg_info in packages.items():
        row = None
        if pkg not in state_packages:
            source = get_pkg_source(pkg_info)
            spec = source if source else pkg
            row = f"  + install {spec}"
        elif version_changed(pkg, pkg_info, state, "uv"):
            row = f"  ~ update {pkg}"
        elif paths.get("uvBin") and binary_missing(pkg_info, paths["uvBin"]):
            row = f"  ~ reinstall {pkg} (binary {pkg_info['binary']} missing)"
        elif update and pkg_unpinned(pkg_info):
            row = f"  ~ upgrade {pkg}"
        if row:
            err = _units_error("uv", pkg, pkg_info)
            changes += [f"  ! {err}"] if err else [row] + _notify_rows(pkg_info)

    for pkg in sorted(state_packages - desired):
        changes.append(f"  - remove {pkg}")

    return changes


def _diff_go(packages: Dict, paths: Dict, state: Dict, update: bool = False):
    changes = []
    desired = set(packages.keys())
    state_pkgs = state.get("go", {}).get("packages", {})
    state_packages = set(state_pkgs.keys())
    context = f"{paths.get('goBin') or ''}|{paths.get('goPath') or ''}"
    context_changed = state.get("go", {}).get("context", context) != context
    go_bin = None
    if any(pkg_info.get("binary") for pkg_info in packages.values()):
        go_bin = _resolve_go_bin(paths)

    for pkg, pkg_info in packages.items():
        row = None
        if pkg not in state_packages:
            source = get_pkg_source(pkg_info) or pkg
            row = f"  + install {source}"
        elif context_changed:
            row = f"  ~ reinstall {pkg} (go install dir changed)"
        elif version_changed(pkg, pkg_info, state, "go"):
            row = f"  ~ update {pkg}"
        elif (
            go_bin
            and pkg_info.get("binary")
            and not os.path.exists(os.path.join(go_bin, pkg_info["binary"]))
        ):
            row = f"  ~ reinstall {pkg} (binary {pkg_info['binary']} missing)"
        elif update and pkg_unpinned(pkg_info):
            row = f"  ~ upgrade {pkg}"
        if row:
            err = _units_error("go", pkg, pkg_info)
            changes += [f"  ! {err}"] if err else [row] + _notify_rows(pkg_info)

    # Same rules as install_go_packages: without a known path, or while another
    # package still uses the binary, the entry is only forgotten.
    legacy_bin = None
    if not context_changed and (paths.get("goBin") or paths.get("goPath")):
        legacy_bin = _resolve_go_bin(paths)
    kept_paths = {
        _go_installed_path(pkg, entry, legacy_bin)
        for pkg, entry in state_pkgs.items()
        if pkg in desired and isinstance(entry, dict)
    }
    for pkg in sorted(state_packages - desired):
        entry = state_pkgs[pkg] if isinstance(state_pkgs[pkg], dict) else {}
        path = _go_installed_path(pkg, entry, legacy_bin)
        if path and path in kept_paths:
            changes.append(f"  ~ forget {pkg} (binary {path} still in use)")
        elif not path:
            changes.append(f"  ~ forget {pkg} (install path unknown, binary not removed)")
        else:
            changes.append(f"  - remove {pkg}")

    return changes


def _diff_cargo(packages: Dict, paths: Dict, state: Dict, update: bool = False):
    changes = []
    desired = set(packages.keys())
    state_pkgs = state.get("cargo", {}).get("packages", {})
    state_packages = set(state_pkgs.keys())
    context = f"{paths.get('cargoRoot') or ''}|{paths.get('cargoHome') or ''}"
    root = os.path.dirname(_resolve_cargo_bin(paths))
    context_changed = (
        state.get("cargo", {}).get("context", context) != context
        or state.get("cargo", {}).get("root", root) != root
    )

    for pkg, pkg_info in packages.items():
        row = None
        if pkg not in state_packages:
            source = get_pkg_source(pkg_info) or pkg
            row = f"  + install {source}"
        elif context_changed:
            row = f"  ~ reinstall {pkg} (cargo install dir changed)"
        elif version_changed(pkg, pkg_info, state, "cargo"):
            row = f"  ~ update {pkg}"
        elif update and pkg_unpinned(pkg_info):
            row = f"  ~ upgrade {pkg}"
        if row:
            err = _units_error("cargo", pkg, pkg_info)
            changes += [f"  ! {err}"] if err else [row] + _notify_rows(pkg_info)

    for pkg in sorted(state_packages - desired):
        if context_changed:
            changes.append(f"  ~ forget {pkg} (cargo install dir changed; binary left behind)")
            continue
        entry = state_pkgs[pkg] if isinstance(state_pkgs[pkg], dict) else {}
        try:
            name = _crate_name(pkg, entry, paths, (state_packages | desired) - {pkg})
        except CrateRecordError as e:
            changes.append(f"  ! cannot remove {pkg}: {e}")
            continue
        if not name:
            changes.append(f"  ~ forget {pkg} (not in cargo's install record)")
        else:
            changes.append(f"  - remove {pkg}")

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
        config_scope,
        foreign_servers,
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
    foreign = foreign_servers(servers, state, current)
    for name in sorted(foreign):
        changes.append(
            f"{SKIPPED_ROW}{name} (registered outside tools, not in scope {foreign[name]}; "
            "left alone)"
        )
    for name in sorted(desired - current - set(foreign)):
        changes.append(f"  + install {name}")
    for name in sorted(desired & managed & current):
        if server_fingerprint(servers[name]) != server_fingerprint(tracked_cfg[name]):
            changes.append(f"  ~ re-register {name}")
    for name in sorted((desired & current) - managed - set(foreign)):
        changes.append(
            f"  ~ re-register {name} (registered outside tools; adopted if it is in scope "
            f"{config_scope(servers[name])}, otherwise left alone)"
        )
    for name in sorted((managed & current) - desired):
        changes.append(f"  - remove {name}")
    for name in sorted(managed - current - desired):
        changes.append(f"  ~ forget {name}")

    return changes


def _git_local_work(path: str) -> bool:
    """Read-only git queries deploy runs before an rmtree; any failure keeps the checkout."""
    from tools.system_paths import system_bin, system_dir
    from tools.user.git_repos import _has_local_work

    try:
        git_bin = system_bin("git")
    except (KeyError, FileNotFoundError):
        return True
    env = os.environ.copy()
    env["PATH"] = f"{system_dir('git')}:{env.get('PATH', '')}"
    return _has_local_work(git_bin, path, env)


def _diff_git_repos(git_repos: Dict, state: Dict, update: bool = False):
    changes = []
    installed = set(state.get("gitRepos", {}).get("installed", []))
    for dest, _url in git_repos.items():
        expanded = os.path.expanduser(dest)
        if not os.path.exists(expanded):
            changes.append(f"  + clone {dest}")
        elif not is_checkout(expanded):
            changes.append(f"  ! not a git repo: {dest} (deploy skips it)")
        elif dest not in installed:
            changes.append(f"  + track {dest}")
        elif update:
            changes.append(f"  ~ pull {dest}")
    # Removal is an rmtree of the whole checkout, so it must never reach the
    # user as a surprise — this is the one section where a missing preview
    # line costs data rather than a confusing diff.
    for dest in sorted(installed - set(git_repos)):
        expanded = os.path.expanduser(dest)
        if os.path.exists(expanded) and not is_checkout(expanded):
            changes.append(f"  ~ release {dest} (not a git checkout, left in place)")
        elif os.path.exists(expanded) and _git_local_work(expanded):
            changes.append(
                f"  ~ keep {dest} (uncommitted or unpushed work, or git cannot read it; "
                "not removed)"
            )
        else:
            changes.append(f"  - remove {dest} (deletes the checkout)")
    return changes


def _diff_files(
    entries: List[Dict[str, object]],
    config_dir: str,
    state: Dict,
    conflicts=None,
    system: bool = False,
    update: bool = False,
):
    """Dry-run for the unified `files:` section.

    Resolves entries via `_resolve_entries`, compares each desired entry
    against the live filesystem, and accounts for cleanup against state.
    ``system`` (root) covers only entries whose target directory the user cannot write;
    as the user they get a note instead.
    """
    from tools.fsutil import aside_note, removal_blocked, respell, same_file_as
    from tools.user.files import (
        _conflict,
        _dir_kept,
        _is_real_dir,
        _ordered,
        _released_dir,
        _removable,
        _removal_order,
        _resolve_entries,
        declared_hooks,
        note_system,
        other_run_targets,
        plan_file,
        root_target_error,
    )
    from tools.util import pending_hooks

    changes: List[str] = []
    hooks: Dict[str, None] = {}

    resolved, errors = _resolve_entries(entries, config_dir, system)
    others = other_run_targets(entries, config_dir, system)
    for err in errors:
        changes.append(f"  ! {err}")
    if not system:
        note_system(entries, config_dir)
    declared = [e.target for e in resolved]
    state_files = respell(dict(state.get("files", {}) or {}), declared)

    managed_targets = set()
    on_update: Dict[str, None] = {}
    held_updates = set()
    for e in _ordered(resolved):
        target, source, desired_mode, secrets = e.target, e.source, e.mode, e.secrets
        managed_targets.add(target)
        reason = _conflict(target, conflicts) or (root_target_error(target) if system else None)
        if reason:
            changes.append(f"  ! refusing {target}: {reason}")
            if e.on_update:
                held_updates.add(e.on_update)
            continue
        if e.skip:
            changes.append(skipped_row(target))
            continue
        if update and e.on_update and not errors:
            on_update[e.on_update] = None
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
        else:
            try:
                with open(source, "rb") as f:
                    src_bytes = f.read()
            except OSError:
                changes.append(f"  ? cannot read source {source}")
                continue
            source_mode = stat.S_IMODE(os.stat(source).st_mode)
            effective_mode = desired_mode if desired_mode is not None else source_mode
            fp = plan_file(
                target,
                src_bytes,
                effective_mode,
                state_files.get(target),
                0 if system else None,
                e.backup,
            )
            if fp.action == "create":
                changes.append(f"  + create {target}")
            elif fp.action == "error":
                changes.append(f"  ! {fp.reason}")
                continue
            elif fp.action == "replace":
                note = (
                    aside_note(target, fp.reason)
                    if e.backup
                    else f"{fp.reason}; overwritten, backup: false"
                )
                changes.append(f"  ~ replace {target} ({note}; content not shown)")
            elif fp.action == "update":
                is_secret = (
                    secrets or looks_like_secret(src_bytes) or looks_like_secret(fp.existing)
                )
                if is_secret:
                    changes.append(f"  ~ update {target} (secret, diff suppressed)")
                else:
                    changes.append(f"  ~ update {target}")
                    diff_text = format_diff_bytes(src_bytes, fp.existing, target)
                    if diff_text:
                        changes.append(diff_text)
            elif fp.action == "chmod":
                changes.append(f"  ~ chmod {target} -> {oct(effective_mode)}")

        if len(changes) > before:
            changes += [f"    notify: {u}" for u in e.restart_units]
            if e.on_change:
                hooks[e.on_change] = None

    # Deploy holds removals on config errors; a refused target stays tracked.
    removed = set()
    for target in _removal_order(state_files, state_files) if not errors else ():
        if target not in managed_targets:
            record = state_files.get(target)
            if same_file_as(target, others):
                continue
            blocked = removal_blocked(conflicts, target)
            twin = same_file_as(target, declared)
            if twin:
                blocked = f"the same file as declared {twin}"
            if blocked:
                changes.append(f"  ~ keep {target} (is {blocked}; not removed)")
                continue
            if os.path.exists(target):
                unsafe = root_target_error(target) if system else None
                if unsafe:
                    changes.append(f"  ! not removing {target}: {unsafe}")
                    continue
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

    retried = pending_hooks(state, "files", declared_hooks(entries))
    for command in retried:
        hooks[command] = None
    for command in hooks:
        changes.append(f"  ~ run onChange: {command}")
        if command in retried:
            changes += _retried_notify_rows(entries, command)
    changes += [f"  ~ run onUpdate: {c}" for c in on_update if c not in held_updates]
    return changes


def _retried_notify_rows(entries, command: str) -> List[str]:
    """restartUnits a retried onChange owes once it succeeds, as `files._owe_restarts`."""
    units: List[str] = []
    for raw in entries if isinstance(entries, list) else []:
        if not isinstance(raw, dict) or raw.get("skip") is True or raw.get("onChange") != command:
            continue
        try:
            units += secrets_flow.parse_restart_units(raw.get("restartUnits"), "files")
        except ValueError:
            continue
    return _notify_rows({"restartUnits": list(dict.fromkeys(units))})


def _empty_after(directory: str, removed) -> bool:
    """Whether ``directory`` holds nothing but entries this diff removes."""
    try:
        names = os.listdir(directory)
    except OSError:
        return False
    return all(os.path.join(directory, n) in removed for n in names)


def show_diff(
    config: dict,
    config_dir: str,
    scope: tuple[str, ...] = (),
    root: bool = False,
    only: "selection.Only | None" = None,
    update: bool = False,
) -> bool:
    """Show what deploy would change. Returns True if no changes needed.

    As root (``root``) only system sections are diffed, against root's records in the user's
    state file.
    With ``only``, narrowed sections are diffed exactly as deploy narrows them.
    With ``update``, also what `deploy --update` upgrades, pulls, refetches and reruns.
    """
    from tools.state import (
        find_state_file,
        load_json,
        migrate_state_schema,
        root_records,
        state_file_path,
    )

    view = selection.view(only or {}, config, config_dir)
    if root:
        state = root_records(
            migrate_state_schema(load_json(find_state_file(state_file_path(config))))
        )
        if view and not _hide(view, state):
            return False
        return _print_sections(
            _root_sections(view.config if view else config, config_dir, scope, state)
        )

    state = {}
    state_file = os.path.expanduser(config.get("stateFile") or "~/.local/state/tools/state.json")
    # Deploy keeps the key beside the migrated state file, not a legacy one.
    mgmt_key = mgmt_key_path(state_file)
    # Read the same state deploy would act on — including a not-yet-migrated
    # legacy location — but without copying anything: diff is a dry run and
    # must not modify the filesystem.
    state_file = find_state_file(state_file)
    if os.path.exists(state_file):
        state = migrate_state_schema(load_json(state_file))
    full, full_state = config, state
    if view:
        if not _hide(view, state):
            return False
        config, full_state = view.config, view.merged(state)

    active = set(scope) if scope else set(ALL_SECTIONS)
    paths = config.get("paths", {})

    sections = []

    # Section gates must mirror _deploy exactly: any extra condition here
    # (e.g. requiring a paths key) makes diff report "clean" for work deploy
    # would still attempt.
    if "bun" in active:
        bun_config = config.get("bun", {})
        bun_packages = bun_config.get("packages", {})
        if bun_packages or bun_config.get("configFile") or state.get("bun", {}).get("packages"):
            changes = _bad_packages("bun", bun_packages) or _diff_bun(
                package_entries("bun", bun_packages)[0],
                paths,
                state,
                {"configFile": bun_config.get("configFile")},
                update,
            )
            if changes:
                sections.append(("bun", changes))

    if "npm" in active:
        npm_config = config.get("npm", {})
        npm_packages = npm_config.get("packages", {})
        if npm_packages or npm_config.get("configFile") or state.get("npm", {}).get("packages"):
            changes = _bad_packages("npm", npm_packages) or _diff_npm(
                package_entries("npm", npm_packages)[0],
                paths,
                state,
                {"configFile": npm_config.get("configFile")},
                update,
            )
            if changes:
                sections.append(("npm", changes))

    if "uv" in active:
        if config.get("uv", {}).get("packages") or state.get("uv", {}).get("packages"):
            uv_packages = config.get("uv", {}).get("packages", {})
            changes = _bad_packages("uv", uv_packages) or _diff_uv(
                package_entries("uv", uv_packages)[0], paths, state, update
            )
            if changes:
                sections.append(("uv", changes))

    if "go" in active:
        go_state = state.get("go", {})
        if config.get("go", {}).get("packages") or go_state.get("packages"):
            go_packages = config.get("go", {}).get("packages", {})
            changes = _bad_packages("go", go_packages) or _diff_go(
                package_entries("go", go_packages)[0], paths, state, update
            )
            if changes:
                sections.append(("go", changes))

    if "cargo" in active:
        cargo_state = state.get("cargo", {})
        if config.get("cargo", {}).get("packages") or cargo_state.get("packages"):
            cargo_packages = config.get("cargo", {}).get("packages", {})
            changes = _bad_packages("cargo", cargo_packages) or _diff_cargo(
                package_entries("cargo", cargo_packages)[0], paths, state, update
            )
            if changes:
                sections.append(("cargo", changes))

    if "pi" in active:
        if config.get("pi", {}).get("packages") or state.get("pi", {}).get("packages"):
            pi_packages = config.get("pi", {}).get("packages", {})
            changes = _bad_packages("pi", pi_packages) or _diff_pi(
                package_entries("pi", pi_packages)[0], paths, state
            )
            if changes:
                sections.append(("pi", changes))

    if "mcp" in active:
        changes = _diff_mcp(config.get("mcp", {}).get("servers", {}), paths, state)
        if changes:
            sections.append(("mcp", changes))

    if "curlShell" in active and (
        config.get("curlShell") or state.get("curlShell", {}).get("installed")
    ):
        changes = diff_curl_shell(config.get("curlShell"), state)
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
    # In memory only: diff never saves state. A narrowed deploy makes no moves.
    moves = (
        []
        if view
        else secrets_flow.move_between_sections(
            state,
            files_entries=config.get("files"),
            sops_raw=config.get("sops"),
            templates_raw=config.get("templates"),
            config_dir=config_dir,
            fetch_raw=config.get("fetch"),
        )
    )
    if moves:
        sections.append(("move", [f"~ move {m}" for m in moves]))
    conflicts = secrets_flow.ownership_conflicts(
        files_entries=full.get("files"),
        sops_raw=full.get("sops"),
        templates_raw=full.get("templates"),
        links_raw=full.get("links"),
        skills_raw=full.get("skills"),
        state=full_state,
        config_dir=config_dir,
        git_repos_raw=full.get("gitRepos"),
        clone_pending="gitRepos" in active,
        env_raw=full.get("env"),
        fetch_raw=full.get("fetch"),
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
        changes = _diff_git_repos(config.get("gitRepos") or {}, state, update)
        if changes:
            sections.append(("gitRepos", changes))

    if inside:
        secrets(secrets_flow.LATE, " (after gitRepos)")

    if "files" in active:
        changes = _diff_files(
            config.get("files", []) or [], config_dir, state, conflicts, update=update
        )
        if changes:
            sections.append(("files", changes))

    if "fetch" in active and fetch_has_work(config.get("fetch"), state):
        changes = diff_fetch(config.get("fetch"), state, conflicts, update)
        if changes:
            sections.append(("fetch", changes))

    if "env" in active and env_has_work(config.get("env"), state):
        changes = diff_env(config.get("env"), state, conflicts)
        if changes:
            sections.append(("env", changes))

    if "skills" in active and skills_has_work(config.get("skills") or {}, state):
        changes = diff_skills(config.get("skills") or {}, config_dir, state, update)
        if changes:
            sections.append(("skills", changes))

    if "links" in active and links_has_work(config.get("links") or [], state):
        changes = diff_links(config.get("links") or [], state)
        if changes:
            sections.append(("links", changes))

    if "brew" in active:
        brew_subsections = diff_brew(config.get("brew"), state, update)
        if brew_subsections:
            sections.append(("brew", brew_subsections))

    if "ollamaModels" in active:
        changes = diff_ollama_models(config.get("ollamaModels", {}) or {}, state, update)
        if changes:
            sections.append(("ollamaModels", changes))

    if "flatpak" in active:
        changes = diff_flatpak(config.get("flatpak", {}) or {}, state, update)
        if changes:
            sections.append(("flatpak", changes))

    if "settingsctl" in active and config.get("settingsctl") is not None:
        changes = diff_settingsctl(config.get("settingsctl"), config.get("paths"))
        if changes:
            sections.append(("settingsctl", changes))

    if "users" in active and users_has_work(config.get("users")):
        changes = diff_users(config.get("users"))
        if changes:
            sections.append(("users", changes))

    if "plist" in active and plist_has_work(config.get("plist")):
        changes = diff_plist(config.get("plist"))
        if changes:
            sections.append(("plist", changes))

    units = secrets_flow.pending_restarts(state)
    if "launchd" in active and (launchd_has_work(config.get("launchd"), state) or units):
        changes = diff_launchd(
            config.get("launchd"),
            state,
            **sops_section.secrets_dir_kwargs(config.get("sops")),
            units=units,
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

    if "mgmt" in active:
        if mgmt_has_work(config.get("mgmt")):
            subsections = diff_mgmt(
                config.get("mgmt"),
                config_dir,
                state,
                mgmt_key,
                **sops_section.secrets_dir_kwargs(config.get("sops")),
            )
        else:
            subsections = mgmt_released_rows(config.get("mgmt"), state)
        if subsections:
            sections.append(("mgmt", subsections))

    return _print_sections(sections)


def _hide(view, state: dict) -> bool:
    """Set aside unselected records; a failure aborts, never reads as pending changes."""
    try:
        view.hide(state)
    except selection.SelectionError as exc:
        log(str(exc), Color.RED)
        raise SystemExit(1) from exc
    return True


def _root_sections(config: dict, config_dir: str, scope, state: dict) -> list:
    """Under sudo: system files, launchd daemons, system-scope defaults and settingsctl host
    settings; nothing in user space."""
    active = set(scope) if scope else set(privilege.ROOT_SECTIONS)
    sections = []
    if "files" in active and files_has_system_work(config.get("files"), state):
        changes = _diff_files(config.get("files") or [], config_dir, state, system=True)
        if changes:
            sections.append(("files", changes))
    if "users" in active and users_has_work(config.get("users"), state):
        changes = diff_users(config.get("users"), state)
        if changes:
            sections.append(("users", changes))
    if "settingsctl" in active and settingsctl_has_root_work(config.get("settingsctl")):
        changes = diff_settingsctl(config.get("settingsctl"), config.get("paths"))
        if changes:
            sections.append(("settingsctl", changes))
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
    if restarts:
        sections.append(("restart", [f"  ~ restart {proc}" for proc in restarts]))
    return sections


def _is_change(item) -> bool:
    return not (isinstance(item, str) and item.startswith(SKIPPED_ROW))


def _print_sections(sections: list) -> bool:
    """Print every row; only rows other than `skip: true` notes count as changes."""
    has_changes = any(_is_change(item) for _, items in sections for item in items)
    if sections:
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
