"""Dry-run diff: show what deploy would change without modifying anything."""

import os
import stat
from pathlib import Path
from typing import Dict, List, Optional

from tools import privilege, selection
from tools.config import SECTIONS, null_section_errors
from tools.fsutil import respell
from tools.log import Color, log
from tools.mgmt import diff_mgmt
from tools.mgmt import has_work as mgmt_has_work
from tools.mgmt import released_rows as mgmt_released_rows
from tools.mgmt.keys import key_path as mgmt_key_path
from tools.user import bun as bun_section
from tools.user import cargo as cargo_section
from tools.user import git_repos as git_repos_section
from tools.user import go as go_section
from tools.user import npm as npm_section
from tools.user import pi as pi_section
from tools.user import secrets_flow
from tools.user import sops as sops_section
from tools.user import templates as templates_section
from tools.user import uv as uv_section
from tools.user.brew import diff_brew
from tools.user.curl_shell import diff_curl_shell
from tools.user.defaults import diff_defaults
from tools.user.defaults import has_work as defaults_has_work
from tools.user.dock import diff_dock
from tools.user.dock import has_work as dock_has_work
from tools.user.env import diff_env
from tools.user.env import has_work as env_has_work
from tools.user.fetch import diff_fetch
from tools.user.fetch import has_work as fetch_has_work
from tools.user.files import declared_targets as files_declared_targets
from tools.user.files import has_system_work as files_has_system_work
from tools.user.flatpak import diff_flatpak
from tools.user.launchd import diff_launchd
from tools.user.launchd import has_work as launchd_has_work
from tools.user.links import diff_links
from tools.user.links import has_work as links_has_work
from tools.user.nix_darwin import diff_nix_darwin
from tools.user.nix_darwin import has_work as nix_darwin_has_work
from tools.user.npm import CONFIG_FILE_REMOVED
from tools.user.ollama_models import diff_ollama_models
from tools.user.plist import diff_plist
from tools.user.plist import has_work as plist_has_work
from tools.user.settingsctl import diff_settingsctl
from tools.user.settingsctl import has_root_work as settingsctl_has_root_work
from tools.user.settingsctl import has_work as settingsctl_has_work
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
    package_section_errors,
    pkg_install_spec,
    skipped_row,
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


def _bad_section(section: str, config: Dict, sections: List) -> bool:
    """Rows for errors in `<section>` itself; deploy then runs nothing in the section."""
    rows = [f"  ! {err}" for err in package_section_errors(section, config.get(section, {}))]
    if rows:
        sections.append((section, rows))
    return bool(rows)


def _held_sections(config: Dict, active: set, sections: List) -> set:
    """Rows for each active section's null config errors; deploy runs nothing in it."""
    held = {s: errors for s, errors in null_section_errors(config).items() if s in active}
    for section, errors in held.items():
        sections.append((section, [f"  ! {err}" for err in errors]))
    return set(held)


def _released(key: str, old, new, tracked, desired) -> List[str]:
    """Rows for a changed install location: deploy forgets what it tracked there."""
    rows = [f"  ~ {key} changed ({old} -> {new}); releasing tracked packages"]
    rows += [f"  ~ forget {pkg} (left in the old location)" for pkg in sorted(tracked - desired)]
    return rows


def _action_rows(section: str, p: Dict, packages: Dict, pkg: str, kind: str, by_source: bool):
    """Row for one planned install/update, or the restartUnits error that keeps
    deploy from acting on that package."""
    pkg_info = packages[pkg]
    err = p["unit_errors"].get(pkg)
    if err:
        return [f"  ! {err}"]
    if kind == "install":
        row = f"  + install {(get_pkg_source(pkg_info) if by_source else None) or pkg}"
    elif kind == "update":
        row = f"  ~ update {pkg}"
    elif kind == "reinstall":
        row = f"  ~ reinstall {pkg} (binary {pkg_info['binary']} missing)"
    elif kind == "relocate":
        row = f"  ~ reinstall {pkg} ({section} install dir changed)"
    else:
        row = f"  ~ upgrade {pkg}"
    return [row] + _notify_rows(pkg_info)


def _package_rows(section: str, p: Dict, packages: Dict, by_source: bool = True) -> List[str]:
    changes: List[str] = []
    for pkg, kind in p["actions"]:
        changes += _action_rows(section, p, packages, pkg, kind, by_source)
    return changes


def _diff_js(section: str, p: Dict, packages: Dict, paths: Dict, prefix_key: str):
    """bun/npm release and removal rows."""
    changes: List[str] = []
    if p["released"]:
        old, tracked = p["released"]
        if tracked:
            changes += _released(
                f"{section} prefix", old, paths.get(prefix_key), tracked, set(packages)
            )
    changes += [f"  - remove {pkg}" for pkg in p["remove"]]
    return changes


def _diff_bun(packages: Dict, paths: Dict, state: Dict, bun_config: Dict, update: bool = False):
    if bun_config.get("configFile") is not None:
        return [f"  ! {CONFIG_FILE_REMOVED}"]
    p = bun_section.plan(packages, paths, state, update)
    changes = _diff_js("bun", p, packages, paths, "bunBin")
    return changes + _package_rows("bun", p, packages, by_source=False)


def _diff_npm(packages: Dict, paths: Dict, state: Dict, npm_config: Dict, update: bool = False):
    if npm_config.get("configFile") is not None:
        return [f"  ! {CONFIG_FILE_REMOVED}"]
    p = npm_section.plan(packages, paths, state, update)
    changes = _diff_js("npm", p, packages, paths, "npmBin")
    stored = state.get("npm", {}).get("packages", {}) if p["state_packages"] else {}
    actions = dict(p["actions"])

    npm_bin = paths.get("npmBin")
    npm_lib = Path(npm_bin).parent / "lib" / "node_modules" if npm_bin else None
    for pkg, pkg_info in packages.items():
        installing = False
        if pkg in actions:
            installing = pkg not in p["unit_errors"]
            changes += _action_rows("npm", p, packages, pkg, actions[pkg], False)
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


def _diff_uv(
    packages: Dict,
    paths: Dict,
    state: Dict,
    update: bool = False,
    files_declared=None,
    taken_over: Optional[set] = None,
):
    """``taken_over`` collects the `files` copies uv takes over, for `_diff_files`."""
    p = uv_section.plan(packages, paths, state, update, files_declared)
    if taken_over is not None:
        taken_over.update(p["takeover"].values())
    changes: List[str] = []
    if p["released"]:
        old, tracked = p["released"]
        if tracked:
            changes += _released("uv tool dir", old, p["context"], tracked, set(packages))
    changes += [
        f"  ~ take over {path} from files (old copy to Trash)" for path in p["takeover"].values()
    ]
    changes += _package_rows("uv", p, packages)
    changes += [f"  - remove {pkg}" for pkg in p["remove"]]
    return changes


def _diff_go(packages: Dict, paths: Dict, state: Dict, update: bool = False):
    p = go_section.plan(packages, paths, state, update)
    changes = _package_rows("go", p, packages)
    for pkg, kind, path in p["removals"]:
        if kind == "shared":
            changes.append(f"  ~ forget {pkg} (binary {path} still in use)")
        elif kind == "unknown":
            changes.append(f"  ~ forget {pkg} (install path unknown, binary not removed)")
        else:
            changes.append(f"  - remove {pkg}")
    return changes


def _diff_cargo(packages: Dict, paths: Dict, state: Dict, update: bool = False):
    p = cargo_section.plan(packages, paths, state, update)
    changes = _package_rows("cargo", p, packages)
    for pkg in p["orphaned"]:
        if p["context_changed"]:
            changes.append(f"  ~ forget {pkg} (cargo install dir changed; binary left behind)")
            continue
        kind, detail = cargo_section.removal(pkg, packages, paths, state)
        if kind == "error":
            changes.append(f"  ! cannot remove {pkg}: {detail}")
        elif kind == "forget":
            changes.append(f"  ~ forget {pkg} (not in cargo's install record)")
        else:
            changes.append(f"  - remove {pkg}")
    return changes


def _diff_pi(packages: Dict, paths: Dict, state: Dict):
    p = pi_section.plan(packages, state)
    changes = [f"  - remove {pkg}" for pkg in p["remove"]]
    for pkg, kind in p["actions"]:
        if kind == "install":
            changes.append(f"  + install {get_pkg_source(packages[pkg]) or pkg}")
        else:
            changes.append(f"  ~ update {pkg}")
    return changes


def _diff_mcp(servers: Dict, paths: Dict, state: Dict):
    from tools.user import mcp as mcp_section

    changes = []
    desired = set(servers.keys())
    managed = set(state.get("mcp", {}).get("servers", {}).keys())
    # Nothing desired and nothing tracked: skip the `claude mcp list` shell-out
    # entirely, so machines without claude do not fail every diff.
    if not desired and not managed:
        return changes

    claude = mcp_section.resolve_claude_cli(paths)
    if not claude:
        return ["  ? claude CLI not found, cannot diff MCP servers"]

    # Same resolver and parser as deploy, or the two disagree about what is
    # installed and the diff never matches what gets applied.
    current = mcp_section.get_installed_mcp_servers(claude, mcp_section.build_mcp_env(paths))
    if current is None:
        return ["  ? failed to list MCP servers, cannot diff"]
    p = mcp_section.plan(servers, state, current)
    foreign = p["foreign"]
    for name in sorted(foreign):
        changes.append(
            f"{SKIPPED_ROW}{name} (registered outside tools, not in scope {foreign[name]}; "
            "left alone)"
        )
    for name in sorted(desired - current - set(foreign)):
        changes.append(f"  + install {name}")
    for name in sorted(p["changed"]):
        changes.append(f"  ~ re-register {name}")
    for name in sorted(p["adopted"]):
        changes.append(
            f"  ~ re-register {name} (registered outside tools; adopted if it is in scope "
            f"{mcp_section.config_scope(servers[name])}, otherwise left alone)"
        )
    for name in sorted((managed & current) - desired):
        changes.append(f"  - remove {name}")
    for name in sorted(p["forget"]):
        changes.append(f"  ~ forget {name}")

    return changes


def _git_local_work(path: str) -> bool:
    """Read-only git queries deploy runs before trashing a checkout; any failure keeps it."""
    from tools.system_paths import system_bin, system_dir

    try:
        git_bin = system_bin("git")
    except (KeyError, FileNotFoundError):
        return True
    env = os.environ.copy()
    env["PATH"] = f"{system_dir('git')}:{env.get('PATH', '')}"
    return git_repos_section._has_local_work(git_bin, path, env)


def _diff_git_repos(git_repos: Dict, state: Dict, update: bool = False):
    changes = []
    p = git_repos_section.plan(git_repos, state, update)
    installed = p["installed"]
    for dest in git_repos:
        action = git_repos_section.checkout_action(dest, installed, update)
        if action == "clone":
            changes.append(f"  + clone {dest}")
        elif action == "not_repo":
            changes.append(f"  ! not a git repo: {dest} (deploy skips it)")
        elif action == "track":
            changes.append(f"  + track {dest}")
        elif action == "pull":
            changes.append(f"  ~ pull {dest}")
    # Removal trashes the whole checkout, so it must never reach the
    # user as a surprise — this is the one section where a missing preview
    # line costs data rather than a confusing diff.
    for dest in sorted(p["to_remove"]):
        action = git_repos_section.removal_action(dest, _git_local_work)
        if action == "release":
            changes.append(f"  ~ release {dest} (not a git checkout, left in place)")
        elif action == "keep":
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
    taken_over=(),
):
    """Dry-run for the unified `files:` section.

    Resolves entries via `_resolve_entries`, compares each desired entry
    against the live filesystem, and accounts for cleanup against state.
    ``system`` (root) covers only entries whose target directory the user cannot write;
    as the user they get a note instead. ``taken_over``: tracked copies another
    section (uv) removes first, so not listed again.
    """
    from tools.fsutil import (
        aside_note,
        canonical_path,
        removal_blocked,
        same_file_as,
        unmounted_volume,
    )
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
        restorable_backup,
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
        if e.unmounted:
            changes.append(
                f"{SKIPPED_ROW}{target} (volume not mounted: {e.unmounted}; not written)"
            )
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
                if e.on_update:
                    held_updates.add(e.on_update)
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
                if e.on_update:
                    held_updates.add(e.on_update)
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
                if e.on_update:
                    held_updates.add(e.on_update)
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
    tracked = declared + list(state_files)
    taken = {canonical_path(t) for t in taken_over}
    for target in _removal_order(state_files, state_files) if not errors else ():
        if target not in managed_targets and canonical_path(target) not in taken:
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
            volume = unmounted_volume(target)
            if volume:
                changes.append(f"  ~ keep {target} (volume not mounted: {volume}; not removed)")
                continue
            if not os.path.lexists(target) and isinstance(record, dict) and record.get("restore"):
                backup = restorable_backup(target, tracked, conflicts)
                unsafe = root_target_error(target) if system else None
                if backup and unsafe:
                    changes.append(f"  ! not restoring {target}: {unsafe}")
                elif backup:
                    changes.append(f"  ~ restore {backup} -> {target}")
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
                elif system and not (isinstance(record, dict) and record.get("hash")):
                    changes.append(f"  ~ release {target} (no recorded hash; left in place)")
                    continue
                elif not _removable(target, record):
                    changes.append(
                        f"  ~ keep {target} (modified since tools wrote it; not removed)"
                    )
                    continue
                changes.append(f"  - remove {target}")
                removed.add(target)
                backup = (
                    None
                    if isinstance(record, dict) and record.get("dir")
                    else restorable_backup(target, tracked, conflicts)
                )
                if backup:
                    changes.append(f"  ~ restore {backup} -> {target}")

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
            _root_sections(
                view.config if view else config,
                config_dir,
                scope,
                state,
                full_state=view.merged(state) if view else None,
            )
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
    active -= _held_sections(config, active, sections)
    # Null sections in or out of scope: their records must not be read as empty.
    null = set(null_section_errors(full))
    taken_over: set = set()

    def registry():
        return secrets_flow.ownership_conflicts(
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

    # Section gates must mirror _deploy exactly: any extra condition here
    # (e.g. requiring a paths key) makes diff report "clean" for work deploy
    # would still attempt.
    if "bun" in active and not _bad_section("bun", config, sections):
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

    if "npm" in active and not _bad_section("npm", config, sections):
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

    if "uv" in active and not _bad_section("uv", config, sections):
        if config.get("uv", {}).get("packages") or state.get("uv", {}).get("packages"):
            uv_packages = config.get("uv", {}).get("packages", {})
            changes = _bad_packages("uv", uv_packages) or _diff_uv(
                package_entries("uv", uv_packages)[0],
                paths,
                state,
                update,
                None
                if view or "files" in null
                else files_declared_targets(full.get("files"), config_dir, registry()),
                taken_over,
            )
            if changes:
                sections.append(("uv", changes))

    if "go" in active and not _bad_section("go", config, sections):
        go_state = state.get("go", {})
        if config.get("go", {}).get("packages") or go_state.get("packages"):
            go_packages = config.get("go", {}).get("packages", {})
            changes = _bad_packages("go", go_packages) or _diff_go(
                package_entries("go", go_packages)[0], paths, state, update
            )
            if changes:
                sections.append(("go", changes))

    if "cargo" in active and not _bad_section("cargo", config, sections):
        cargo_state = state.get("cargo", {})
        if config.get("cargo", {}).get("packages") or cargo_state.get("packages"):
            cargo_packages = config.get("cargo", {}).get("packages", {})
            changes = _bad_packages("cargo", cargo_packages) or _diff_cargo(
                package_entries("cargo", cargo_packages)[0], paths, state, update
            )
            if changes:
                sections.append(("cargo", changes))

    if "pi" in active and not _bad_section("pi", config, sections):
        if config.get("pi", {}).get("packages") or state.get("pi", {}).get("packages"):
            pi_packages = config.get("pi", {}).get("packages", {})
            changes = _bad_packages("pi", pi_packages) or _diff_pi(
                package_entries("pi", pi_packages)[0], paths, state
            )
            if changes:
                sections.append(("pi", changes))

    if "mcp" in active:
        from tools.user.mcp import config_errors as mcp_config_errors

        changes = [f"  ! {err}" for err in mcp_config_errors(config.get("mcp", {}))] or _diff_mcp(
            config.get("mcp", {}).get("servers", {}), paths, state
        )
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
    sops_settings = sops_section.parse_sops(config.get("sops")).settings
    if run_sops or run_templates:
        decryptor = sops_section.Decryptor(sops_settings)
    # Deploy creates it before decrypting; shown in the first sops rows.
    new_key = sops_section.ssh_key_to_generate(sops_settings) if "sops" in active else None
    key_rows = [f"  + generate ssh key {new_key}"] if new_key else []
    # In memory only: diff never saves state. A narrowed deploy makes no moves.
    moves = (
        []
        if view or null & secrets_flow.MOVE_SECTIONS
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
    conflicts = registry()
    # Same order as deploy: paths inside a gitRepos destination after the clone.
    inside = secrets_flow.paths_in_repos(
        config.get("gitRepos"), config.get("sops"), config.get("templates")
    )
    failed: tuple = ()

    def secrets(phase, suffix=""):
        nonlocal failed, key_rows
        split = {"phase": phase, "inside": inside} if phase else {}
        if run_sops:
            sops_plan = secrets_flow.plan_sops(
                config.get("sops"), state, decryptor, conflicts, **split
            )
            failed += secrets_flow.failed_secrets(sops_plan)
            changes = key_rows + sops_section.describe(sops_plan)
            key_rows = []
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
    # case where deploy trashes every tracked checkout — that must never be
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
            config.get("files", []) or [],
            config_dir,
            state,
            conflicts,
            update=update,
            taken_over=taken_over,
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

    if "settingsctl" in active and settingsctl_has_work(config.get("settingsctl"), state):
        changes = diff_settingsctl(config.get("settingsctl"), config.get("paths"), state=state)
        if changes:
            sections.append(("settingsctl", changes))

    if "users" in active and users_has_work(config.get("users")):
        changes = diff_users(config.get("users"))
        if changes:
            sections.append(("users", changes))

    if "plist" in active and plist_has_work(config.get("plist"), state):
        changes = diff_plist(config.get("plist"), state)
        if changes:
            sections.append(("plist", changes))

    if "nixDarwin" in active and nix_darwin_has_work(config.get("nixDarwin")):
        changes = diff_nix_darwin(config.get("nixDarwin"), full_state, root=False)
        if changes:
            sections.append(("nixDarwin", changes))

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


def _root_sections(
    config: dict, config_dir: str, scope, state: dict, full_state: dict | None = None
) -> list:
    """Under sudo: system files, launchd daemons, system-scope defaults and settingsctl host
    settings; nothing in user space."""
    active = set(scope) if scope else set(privilege.ROOT_SECTIONS)
    sections = []
    active -= _held_sections(config, active & set(privilege.ROOT_SECTIONS), sections)
    if "nixDarwin" in active and nix_darwin_has_work(config.get("nixDarwin")):
        owned = full_state if full_state is not None else state
        changes = diff_nix_darwin(config.get("nixDarwin"), owned, root=True)
        if changes:
            sections.append(("nixDarwin", changes))
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
