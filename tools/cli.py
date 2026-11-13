import json
import os
import sys
import threading
import time

import click

from tools import privilege, selection
from tools.config import (
    ConfigError,
    deep_merge,
    load_config,
    load_config_dir,
    pin_hostname,
    resolve_config,
)
from tools.diff import ALL_SECTIONS, show_diff
from tools.log import Color, log, set_verbose
from tools.mgmt import has_work as mgmt_has_work
from tools.mgmt import install_mgmt, release_mgmt
from tools.mgmt import spec as mgmt_spec
from tools.mgmt.keys import key_path as mgmt_key_path
from tools.state import (
    ROOT_KEY,
    load_json,
    lock_path,
    migrate_state_file,
    migrate_state_schema,
    root_records,
    save_json,
    state_file_path,
    state_lock,
)
from tools.user import brew as brew_section
from tools.user import secrets_flow
from tools.user import sops as sops_section
from tools.user import templates as templates_section
from tools.user.brew import install_brew_packages
from tools.user.bun import install_bun_packages
from tools.user.cargo import install_cargo_packages
from tools.user.curl_shell import install_curl_shell_scripts
from tools.user.defaults import has_work as defaults_has_work
from tools.user.defaults import install_defaults, process_name_error, run_restarts
from tools.user.dock import has_work as dock_has_work
from tools.user.dock import install_dock
from tools.user.env import export_process as env_export_process
from tools.user.env import has_work as env_has_work
from tools.user.env import install_env
from tools.user.fetch import has_work as fetch_has_work
from tools.user.fetch import install_fetch
from tools.user.files import has_system_work as files_has_system_work
from tools.user.files import install_files
from tools.user.files import source_paths as files_source_paths
from tools.user.flatpak import install_flatpak_packages
from tools.user.git_repos import install_git_repos
from tools.user.go import install_go_packages
from tools.user.launchd import has_work as launchd_has_work
from tools.user.launchd import (
    install_launchd,
    restart_daemons,
    stale_units,
    status_rows,
    valid_label,
)
from tools.user.links import has_work as links_has_work
from tools.user.links import install_links
from tools.user.links import resolve as links_resolve
from tools.user.mcp import install_mcp_servers
from tools.user.npm import install_npm_packages
from tools.user.ollama_models import install_ollama_models
from tools.user.pi import install_pi_packages
from tools.user.plist import has_work as plist_has_work
from tools.user.plist import install_plist
from tools.user.settingsctl import has_root_work as settingsctl_has_root_work
from tools.user.settingsctl import install_settingsctl
from tools.user.skills import has_work as skills_has_work
from tools.user.skills import install_skills
from tools.user.skills import source_root as skills_source_root
from tools.user.users import has_work as users_has_work
from tools.user.users import install_users
from tools.user.uv import install_uv_packages
from tools.watch import is_excluded, is_watched, log_files, run_in_tmux, snapshot, watch_loop, wlog


def _resolve_config_dir(config_paths: list[str]) -> str:
    """Resolve the config directory from the first --config path.

    Relative `files:` sources in *every* merged config resolve against this
    one directory, even entries contributed by a second --config argument.
    Keep file-carrying sections in the first config path.
    """
    first = config_paths[0]
    if os.path.isdir(first):
        return os.path.abspath(first)
    return os.path.abspath(os.path.dirname(first))


def _load_merged_config(config_paths: list[str], sources: list | None = None) -> dict:
    """Load and merge config from one or more paths.

    Every config file and template file read is appended to `sources`.
    """
    config: dict = {}
    for config_path in config_paths:
        try:
            if os.path.isdir(config_path):
                loaded = load_config_dir(config_path, sources)
            else:
                loaded = load_config(config_path, loaded=sources)
        except (
            ConfigError,
            FileNotFoundError,
            NotADirectoryError,
            TypeError,
            ValueError,
        ) as exc:
            log(f"Config error in {config_path}: {exc}", Color.RED)
            sys.exit(1)
        if not isinstance(loaded, dict):
            # deep_merge on a list/str would die with a raw AttributeError.
            log(f"Config error in {config_path}: top level must be a mapping", Color.RED)
            sys.exit(1)
        config = deep_merge(config, loaded)
    try:
        return resolve_config(config, _resolve_config_dir(config_paths), sources=sources)
    except ConfigError as exc:
        log(f"Config error: {exc}", Color.RED)
        sys.exit(1)


def _invoker() -> privilege.Invoker:
    """Who this run acts for; as root, also points `~` and $HOME at SUDO_USER's home."""
    try:
        inv = privilege.invoker()
    except privilege.SudoUserError as exc:
        log(str(exc), Color.RED)
        sys.exit(1)
    privilege.act_as(inv)
    return inv


def _selection(scope, only) -> tuple[tuple[str, ...], selection.Only]:
    """(sections to run, --only items); exits on a malformed or conflicting --only."""
    try:
        parsed = selection.parse(only)
        return selection.combine(scope, parsed), parsed
    except selection.SelectionError as exc:
        log(str(exc), Color.RED)
        sys.exit(1)


def _read_state(config: dict, root: bool) -> dict:
    """The state a run would act on, read without migrating anything."""
    from tools.state import find_state_file

    state = migrate_state_schema(load_json(find_state_file(state_file_path(config))))
    return root_records(state) if root else state


def _check_only(config: dict, config_paths: list[str], only, root: bool = False) -> None:
    """Exit before anything runs when --only can't: config errors in a narrowed section,
    a name that matches nothing, or state whose other records can't be set aside."""
    if not only:
        return
    config_dir = _resolve_config_dir(config_paths)
    errors = selection.unmatched(only, config, config_dir)
    if not errors:
        errors = selection.state_errors(only, config, config_dir, _read_state(config, root))
    for err in errors:
        log(err, Color.RED)
    if errors:
        sys.exit(1)


def _user_check(config_paths: list[str], only) -> int:
    _check_only(_load_merged_config(config_paths), config_paths, only)
    return 0


USER_CHANGES = 3  # exit code of the user diff child: changes pending


def _system_scope(scope: tuple[str, ...]) -> tuple[str, ...]:
    """The root-capable sections of scope (all of them for an empty scope)."""
    return tuple(s for s in (scope or privilege.ROOT_SECTIONS) if s in privilege.ROOT_SECTIONS)


def _user_diff(config_paths: list[str], scope: tuple[str, ...], only=None) -> int:
    """`tools diff` as the user: 0 when clean, USER_CHANGES when changes are pending."""
    merged = _load_merged_config(config_paths)
    _check_only(merged, config_paths, only)
    clean = show_diff(merged, _resolve_config_dir(config_paths), scope, only=only)
    return 0 if clean else USER_CHANGES


def _user_deploy(
    config_paths: list[str], scope: tuple[str, ...], update: bool, restarts_fd: int, only=None
) -> int:
    """`tools deploy --approve` as the user; its process restarts and the daemons its
    restartUnits owe go to restarts_fd as JSON for the root parent to run instead."""
    restarts: list = []
    daemons: list = []
    try:
        merged = _load_merged_config(config_paths)
        _check_only(merged, config_paths, only)
        ok = _deploy(
            merged,
            _resolve_config_dir(config_paths),
            scope,
            update,
            restarts=restarts,
            only=only,
            daemons=daemons,
        )
        return 0 if ok else 1
    finally:
        data = json.dumps({"processes": restarts, "daemons": daemons}).encode()
        while data:
            data = data[os.write(restarts_fd, data) :]


MAX_RESTARTS_PAYLOAD = 64 * 1024


def _read_restarts(fd: int, out: list, daemons: list | None = None) -> None:
    """The user child's restart queues from fd: processes into out, launchd daemon labels
    into daemons. Drains to EOF so the child never blocks on a full pipe; an oversized or
    malformed payload, a name killall must not see, or an invalid label yields nothing
    from the child."""
    data = bytearray()
    oversized = False
    while chunk := os.read(fd, 65536):
        if not oversized:
            data += chunk
            oversized = len(data) > MAX_RESTARTS_PAYLOAD
    if oversized:
        log("User restart queue too large; ignoring it", Color.YELLOW)
        return
    try:
        payload = json.loads(bytes(data) or b"{}")
    except ValueError:
        log("User restart queue is malformed; ignoring it", Color.YELLOW)
        return
    if not isinstance(payload, dict):
        log("User restart queue is malformed; ignoring it", Color.YELLOW)
        return
    restarts = payload.get("processes", [])
    labels = payload.get("daemons", [])
    if not isinstance(restarts, list) or any(process_name_error(p) for p in restarts):
        log("User restart queue has invalid process names; ignoring it", Color.YELLOW)
        return
    if not isinstance(labels, list) or not all(
        isinstance(item, list) and len(item) == 2 and valid_label(item[0]) and type(item[1]) is int
        for item in labels
    ):
        log("User restart queue has invalid launchd labels; ignoring it", Color.YELLOW)
        return
    out += restarts
    if daemons is not None:
        daemons += labels


def _sudo_diff(inv: privilege.Invoker, config_paths: list[str], scope: tuple, only=None) -> bool:
    """Both diffs under sudo: system sections as root, the rest in a child dropped to the
    user. True when there are changes; exits when the user diff fails."""
    pin_hostname()
    changes = False
    system = _system_scope(scope)
    if system:
        log("As root:", Color.BLUE)
        merged = _load_merged_config(config_paths)
        _check_only(merged, config_paths, only, root=True)
        changes = not show_diff(
            merged, _resolve_config_dir(config_paths), system, root=True, only=only
        )
    log(f"As {inv.login}:", Color.BLUE)
    rc = privilege.run_as_user(inv, lambda: _user_diff(config_paths, scope, only))
    if rc not in (0, USER_CHANGES):
        log(f"Diff as {inv.login} failed; nothing deployed", Color.RED)
        sys.exit(1)
    return changes or rc == USER_CHANGES


def _sudo_deploy(
    inv: privilege.Invoker,
    config_paths: list[str],
    scope: tuple,
    approve: bool,
    update: bool,
    only=None,
) -> None:
    """User sections in a child dropped to the user, then system sections as root
    (daemons and host settings run what the user phase installs), then `mgmt` in
    another user child (its services may be daemons root just started). One prompt
    covers all; a failed phase stops the ones after it. Process restarts from every
    phase run once, at the end."""
    pin_hostname()
    if only:
        # Both phases' --only checks before either runs anything.
        _check_only(_load_merged_config(config_paths), config_paths, only, root=True)
        if privilege.run_as_user(inv, lambda: _user_check(config_paths, only)) != 0:
            log(f"--only check as {inv.login} failed; nothing deployed", Color.RED)
            sys.exit(1)
    if not approve and _sudo_diff(inv, config_paths, scope, only):
        _confirm()
    system = _system_scope(scope)
    user, mgmt = scope, ()
    if system and (not scope or "mgmt" in scope):
        if mgmt_has_work(_load_merged_config(config_paths).get("mgmt")):
            user = tuple(s for s in (scope or ALL_SECTIONS) if s != "mgmt")
            mgmt = ("mgmt",)
    restarts: list = []
    daemons: list = []
    try:
        if user or not scope:
            rc = _user_phase(inv, config_paths, user, update, only, restarts, daemons)
            if rc != 0:
                if system or mgmt:
                    log(f"Deploy as {inv.login} failed; not running the later phases", Color.RED)
                sys.exit(1)
        if system:
            merged = _load_merged_config(config_paths)
            config_dir = _resolve_config_dir(config_paths)
            if not _deploy(merged, config_dir, system, update, True, restarts, only, inv=inv):
                if mgmt:
                    log("System sections failed; not running mgmt", Color.RED)
                sys.exit(1)
        if mgmt and _user_phase(inv, config_paths, mgmt, update, only, restarts, daemons) != 0:
            sys.exit(1)
    finally:
        run_restarts(list(dict.fromkeys(restarts)))
        if daemons and not _restart_daemons(inv, config_paths, daemons):
            sys.exit(1)


def _user_phase(
    inv: privilege.Invoker,
    config_paths: list[str],
    scope: tuple,
    update: bool,
    only,
    restarts: list,
    daemons: list,
) -> int:
    """`_user_deploy` in a child dropped to the user; its restart queues land in
    restarts and daemons. Returns the child's exit code."""
    r, w = os.pipe()
    child_restarts: list = []
    reader = threading.Thread(target=_read_restarts, args=(r, child_restarts, daemons), daemon=True)
    reader.start()
    try:
        return privilege.run_as_user(
            inv, lambda: _user_deploy(config_paths, scope, update, w, only)
        )
    finally:
        os.close(w)
        reader.join()
        os.close(r)
        restarts += child_restarts


def _restart_daemons(inv: privilege.Invoker, config_paths: list[str], queued: list) -> bool:
    """Kickstart, as root, the daemons whose restartUnits sources the user phase rewrote
    (`queued`: [label, seq] pairs), then clear those it handled from the user's state,
    unless re-owed since (seq changed); the rest stay owed."""
    seqs = {label: seq for label, seq in queued}
    merged = _load_merged_config(config_paths)
    state_file = state_file_path(merged)
    with state_lock(state_file):
        state = root_records(migrate_state_schema(load_json(state_file)))
    done: set = set()
    ok = restart_daemons(
        merged.get("launchd"),
        list(seqs),
        state,
        **sops_section.secrets_dir_kwargs(merged.get("sops")),
        done=done,
    )
    settled = {label: seqs[label] for label in done}
    if settled and privilege.run_as_user(inv, lambda: _settle_restarts(config_paths, settled)) != 0:
        log(f"Could not record daemon restarts in {inv.login}'s state", Color.RED)
        ok = False
    return ok


def _settle_restarts(config_paths: list[str], done: dict) -> int:
    """As the user: drop restartUnits the root parent carried out from the user's state."""
    merged = _load_merged_config(config_paths)
    state_file = os.path.expanduser(merged.get("stateFile") or "~/.local/state/tools/state.json")
    with state_lock(state_file):
        state = migrate_state_schema(load_json(state_file))
        secrets_flow.settle_restarts(state, done)
        save_json(state_file, state)
    return 0


def _confirm() -> None:
    answer = click.prompt("\nType 'yes' to deploy", default="no")
    if answer != "yes":
        log("Aborted.", Color.YELLOW)
        sys.exit(1)


def _refuse_root(command: str) -> None:
    if os.geteuid() == 0:
        log(f"{command} does not run as root; run it as your normal user", Color.RED)
        sys.exit(1)


def _deploy(
    config: dict,
    config_dir: str,
    scope: tuple[str, ...] = (),
    update: bool = False,
    root: bool = False,
    restarts: list | None = None,
    only: selection.Only | None = None,
    daemons: list | None = None,
    inv: privilege.Invoker | None = None,
) -> bool:
    """Apply config to bring system to desired state. Returns True on success.

    Already-installed items are only upgraded (pulled, refetched) with ``update``.
    As root (``root``) only system sections run, against root's records in the user's
    state file, which is written by a child dropped to the user.
    With ``restarts``, queued process restarts are added there instead of run.
    With ``daemons``, launchd daemons owed a restartUnits restart are added there for the
    root parent; without it they stay pending.
    """
    state_file = state_file_path(config)
    if root:
        _ensure_user_owned(inv, lock_path(state_file))
    # Overlapping runs (watch, scheduled and manual deploys) would otherwise
    # each save their own copy and drop the other's records.
    with state_lock(state_file):
        return _deploy_locked(
            config, config_dir, state_file, scope, update, root, restarts, only, daemons, inv
        )


def _deploy_locked(
    config: dict,
    config_dir: str,
    state_file: str,
    scope: tuple[str, ...],
    update: bool,
    root: bool,
    restarts: list | None = None,
    only: selection.Only | None = None,
    daemons: list | None = None,
    inv: privilege.Invoker | None = None,
) -> bool:
    if root:
        full = migrate_state_schema(load_json(state_file))
        state = root_records(full)

        def save(records: dict) -> None:
            _save_as_user(inv, state_file, {**full, ROOT_KEY: records})

    else:
        migrate_state_file(state_file)
        state = migrate_state_schema(load_json(state_file))

        def save(records: dict) -> None:
            save_json(state_file, records)

    active = set(scope) if scope else set(ALL_SECTIONS)

    success = True
    paths = config.get("paths", {})
    view = selection.view(only or {}, config, config_dir)

    # The reconcilers mutate `state` in memory as they install/remove; the
    # try/finally guarantees those mutations reach disk even when a later
    # section crashes. Without it, packages installed earlier in the same run
    # become permanently unowned.
    try:
        # Mid-run saves must carry the records set aside for --only too.
        persist = lambda: save(view.merged(state) if view else state)  # noqa: E731
        if view:
            try:
                view.hide(state)
            except selection.SelectionError as exc:
                log(str(exc), Color.RED)
                return False
        try:
            if root:
                success = _run_root_sections(
                    view.config if view else config,
                    config_dir,
                    state,
                    active,
                    persist,
                    restarts,
                )
            else:
                success = _run_sections(
                    view.config if view else config,
                    config_dir,
                    state,
                    paths,
                    active,
                    persist,
                    state_file,
                    update,
                    restarts,
                    full_config=config,
                    view=view,
                    daemons=daemons,
                )
        finally:
            if view:
                view.restore(state)
    finally:
        save(state)
    return success


def _ensure_user_owned(inv: privilege.Invoker | None, path: str) -> None:
    """As root: create path (and its parents) as the user, so root never creates files in
    the user's home."""
    if os.path.exists(path) or not (inv and inv.root):
        return

    def create() -> int:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.close(os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600))
        return 0

    if privilege.run_as_user(inv, create) != 0:
        raise RuntimeError(f"could not create {path} as {inv.login}")


def _save_as_user(inv: privilege.Invoker | None, state_file: str, state: dict) -> None:
    """Save the user's state file; as root, from a child dropped to the user."""
    if not (inv and inv.root):
        save_json(state_file, state)
        return

    def save() -> int:
        save_json(state_file, state)
        return 0

    if privilege.run_as_user(inv, save) != 0:
        raise RuntimeError(f"could not save {state_file} as {inv.login}")


def _finish_restarts(restarts: list, deferred: list | None) -> None:
    if deferred is None:
        run_restarts(restarts)
    else:
        deferred.extend(restarts)


def _run_root_sections(
    config: dict,
    config_dir: str,
    state: dict,
    active: set,
    persist=lambda: None,
    deferred: list | None = None,
) -> bool:
    """Under sudo: system files, launchd daemons, system-scope defaults and settingsctl host
    settings; nothing in user space."""
    success = True
    # Before daemons, whose restartOn may name these files.
    if "files" in active and files_has_system_work(config.get("files"), state):
        success &= install_files(config.get("files") or [], config_dir, state, system=True)
    # After files, which may write /etc/shells.
    if "users" in active and users_has_work(config.get("users"), state):
        success &= install_users(config.get("users"), state)
    if "settingsctl" in active and settingsctl_has_root_work(config.get("settingsctl")):
        success &= install_settingsctl(config.get("settingsctl"), config.get("paths"))
    if "launchd" in active and launchd_has_work(config.get("launchd"), state):
        success &= install_launchd(
            config.get("launchd"),
            state,
            **sops_section.secrets_dir_kwargs(config.get("sops")),
        )
    restarts: list = []
    try:
        if "defaults" in active and defaults_has_work(config.get("defaults"), state):
            success &= install_defaults(
                config.get("defaults"), state, restarts=restarts, persist=persist
            )
    finally:
        _finish_restarts(restarts, deferred)
    return success


def _run_sections(
    config: dict,
    config_dir: str,
    state: dict,
    paths: dict,
    active: set,
    persist=lambda: None,
    state_file: str = "",
    update: bool = False,
    deferred: list | None = None,
    full_config: dict | None = None,
    view: selection.View | None = None,
    daemons: list | None = None,
) -> bool:
    """With --only, ``config`` is the narrowed config and ``view`` holds the state records
    set aside; ownership is still judged on ``full_config`` and the whole state."""
    success = True
    full = full_config if full_config is not None else config

    # Moves rewrite records of any section, so a narrowed run leaves them to a full one.
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
    for move in moves:
        log(f"Moved: {move}", Color.GREEN)
    if moves:
        persist()

    def registry():
        return secrets_flow.ownership_conflicts(
            files_entries=full.get("files"),
            sops_raw=full.get("sops"),
            templates_raw=full.get("templates"),
            links_raw=full.get("links"),
            skills_raw=full.get("skills"),
            state=view.merged(state) if view else state,
            config_dir=config_dir,
            git_repos_raw=full.get("gitRepos"),
            env_raw=full.get("env"),
            fetch_raw=full.get("fetch"),
        )

    # Before anything installs: uv/npm/... must see the session env (e.g.
    # UV_PYTHON_INSTALL_DIR) even under a --scope that excludes env.
    env_export_process(config.get("env"))

    # Installs before everything that runs them: sops, git, node, uv, go, rust.
    if "brew" in active:
        success &= install_brew_packages(
            config.get("brew", {}), state, update, phase=brew_section.INSTALL
        )

    # One registry for every file-writing section, taken before any of them runs.
    conflicts = registry()
    # sops and templates first: later sections (mcp secretPaths, ${sopsPath:}
    # consumers, private gitRepos clones) read the files they write; a failed
    # secret blocks its templates. Paths inside a gitRepos destination wait
    # for the clone, which would otherwise find the directory and skip it.
    inside = secrets_flow.paths_in_repos(
        config.get("gitRepos"), config.get("sops"), config.get("templates")
    )
    report = secrets_flow.RunReport()
    # One decryptor for both, so templates render the values sops just wrote.
    decryptor = sops_section.Decryptor(sops_section.parse_sops(config.get("sops")).settings)
    run_sops = "sops" in active and sops_section.has_work(config.get("sops"), state)
    run_templates = "templates" in active and templates_section.has_work(
        config.get("templates"), state
    )

    def secrets(owners, phase=None):
        ok = True
        split = {"phase": phase, "inside": inside} if phase else {}
        if run_sops:
            ok &= secrets_flow.deploy_sops(
                config.get("sops"), state, report, decryptor, conflicts=owners, **split
            )
        if run_templates:
            ok &= secrets_flow.deploy_templates(
                config.get("templates"),
                config.get("sops"),
                state,
                report,
                owners,
                decryptor,
                **split,
            )
        return ok

    rendered = secrets(conflicts, secrets_flow.EARLY if inside else None)
    success &= rendered

    if "gitRepos" in active and (
        config.get("gitRepos") or state.get("gitRepos", {}).get("installed")
    ):
        success &= install_git_repos(config.get("gitRepos") or {}, state, update)
        # A fresh checkout can bring in symlinks that make paths collide.
        conflicts = registry()

    if inside:
        late = secrets(conflicts, secrets_flow.LATE)
        rendered &= late
        success &= late
    # Only a full sops (and templates) run proves every secret is in place.
    full_run = (
        not view and "sops" in active and ("templates" in active or not config.get("templates"))
    )
    if rendered and (
        full_run
        or (
            "launchd" in active
            and secrets_flow.rendered_in_state(full.get("sops"), full.get("templates"), state)
        )
    ):
        success &= secrets_flow.mark_rendered(full.get("sops"))
    if run_templates:
        success &= secrets_flow.run_template_hooks(config.get("templates"), state)
    for line in secrets_flow.format_notify(report):
        log(line, Color.YELLOW)

    # Before bun/npm/mcp: install scripts can bring runtimes and CLIs (claude).
    # Gate on state as well as config, or dropping the last entry means the
    # reconciler never runs again and its state is orphaned forever.
    if "curlShell" in active and (
        config.get("curlShell") or state.get("curlShell", {}).get("installed")
    ):
        success &= install_curl_shell_scripts(config.get("curlShell") or {}, state)

    # bun/npm index paths['bun'] and paths['nodejs'] directly, so calling them
    # with nothing configured raises KeyError on a machine show_diff calls
    # clean. Gate them like uv/go: run only when there is work or state.
    if "bun" in active:
        bun_config = config.get("bun", {})
        bun_packages = bun_config.get("packages", {})
        bun_only_config = {"configFile": bun_config.get("configFile")}
        if bun_packages or bun_config.get("configFile") or state.get("bun", {}).get("packages"):
            success &= install_bun_packages(bun_packages, paths, state, bun_only_config, update)

    if "npm" in active:
        npm_config = config.get("npm", {})
        npm_packages = npm_config.get("packages", {})
        npm_only_config = {"configFile": npm_config.get("configFile")}
        if npm_packages or npm_config.get("configFile") or state.get("npm", {}).get("packages"):
            success &= install_npm_packages(npm_packages, paths, state, npm_only_config, update)

    if "uv" in active:
        uv_packages = config.get("uv", {}).get("packages", {})
        if uv_packages or state.get("uv", {}).get("packages"):
            success &= install_uv_packages(uv_packages, paths, state, update)

    if "go" in active:
        go_packages = config.get("go", {}).get("packages", {})
        go_state = state.get("go", {})
        if go_packages or go_state.get("packages"):
            success &= install_go_packages(go_packages, paths, state, update)

    if "cargo" in active:
        cargo_packages = config.get("cargo", {}).get("packages", {})
        cargo_state = state.get("cargo", {})
        if cargo_packages or cargo_state.get("packages"):
            success &= install_cargo_packages(cargo_packages, paths, state, update)

    if "pi" in active:
        pi_packages = config.get("pi", {}).get("packages", {})
        if pi_packages or state.get("pi", {}).get("packages"):
            success &= install_pi_packages(pi_packages, paths, state)

    if "mcp" in active:
        mcp_servers = config.get("mcp", {}).get("servers", {})
        # Skip the `claude mcp list` shell-out when there is nothing to
        # reconcile, matching _diff_mcp.
        if mcp_servers or state.get("mcp", {}).get("servers"):
            success &= install_mcp_servers(mcp_servers, paths, state)

    # Removals after the package managers, which may still need a runtime brew drops.
    if "brew" in active:
        success &= install_brew_packages(
            config.get("brew", {}), state, update, phase=brew_section.REMOVE
        )

    if "files" in active:
        success &= install_files(
            config.get("files", []) or [],
            config_dir,
            state,
            conflicts,
            sops_section.parse_sops(config.get("sops")).secrets_dir()[0],
            update,
        )

    if "fetch" in active and fetch_has_work(config.get("fetch"), state):
        success &= install_fetch(config.get("fetch"), state, conflicts, update)

    if "env" in active and env_has_work(config.get("env"), state):
        success &= install_env(config.get("env"), state, conflicts)

    # Gated on state too, like gitRepos: dropping the whole section must
    # still reach the reconciler so it can remove what it installed.
    if "skills" in active and skills_has_work(config.get("skills") or {}, state):
        success &= install_skills(config.get("skills") or {}, config_dir, state, update)

    # After skills, so links into freshly installed skill dirs find their source.
    if "links" in active and links_has_work(config.get("links") or [], state):
        success &= install_links(config.get("links") or [], state)

    if "ollamaModels" in active:
        success &= install_ollama_models(config.get("ollamaModels", {}) or {}, state, update)

    if "flatpak" in active:
        success &= install_flatpak_packages(config.get("flatpak", {}) or {}, state, update)

    # After uv (installs settingsctl) and brew (installs the apps it grants).
    if "settingsctl" in active and config.get("settingsctl") is not None:
        success &= install_settingsctl(config.get("settingsctl"), config.get("paths"))

    if "users" in active and users_has_work(config.get("users")):
        success &= install_users(config.get("users"))

    # After brew, which installs the apps whose files these are.
    if "plist" in active and plist_has_work(config.get("plist")):
        success &= install_plist(config.get("plist"))

    # After files, so restartOn hashes see files written in this run.
    units = secrets_flow.pending_restarts(state)
    if "launchd" in active and (launchd_has_work(config.get("launchd"), state) or units):
        done: set = set()
        queued: list = []
        success &= install_launchd(
            config.get("launchd"),
            state,
            **sops_section.secrets_dir_kwargs(config.get("sops")),
            units=units,
            done=done,
            daemons=None if daemons is None else queued,
        )
        seqs = secrets_flow.pending_seqs(state)
        if daemons is not None:
            daemons += [[label, seqs[label]] for label in queued if label in seqs]
        if not view:
            done |= stale_units(
                config.get("launchd"),
                {k: v for k, v in units.items() if k not in done},
                **sops_section.secrets_dir_kwargs(config.get("sops")),
            )
        secrets_flow.settle_restarts(state, done)

    # Talks to service APIs; gated so an absent section makes no network calls.
    if "mgmt" in active and mgmt_has_work(config.get("mgmt")):
        success &= install_mgmt(
            config.get("mgmt"),
            config_dir,
            state,
            mgmt_key_path(state_file) if state_file else None,
            **sops_section.secrets_dir_kwargs(config.get("sops")),
        )
    elif "mgmt" in active:
        release_mgmt(config.get("mgmt"), state)

    # Last: dock entries point at apps brew casks may have just installed.
    # Restarts wait for both: a Dock killed mid-deploy can drop a later
    # persistent-apps write.
    restarts: list = []
    try:
        if "defaults" in active and defaults_has_work(config.get("defaults"), state):
            success &= install_defaults(
                config.get("defaults"), state, restarts=restarts, persist=persist
            )

        if "dock" in active and dock_has_work(config.get("dock"), state):
            success &= install_dock(config.get("dock"), state, restarts=restarts, persist=persist)
    finally:
        _finish_restarts(restarts, deferred)
    return success


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Show debug output.")
def main(verbose):
    """Declarative configuration manager."""
    set_verbose(verbose)
    _invoker()


@main.command()
@click.option("--config", multiple=True, default=["."])
@click.option(
    "--scope", multiple=True, type=click.Choice(ALL_SECTIONS), help="Run only these sections."
)
@click.option(
    "--only",
    multiple=True,
    metavar="SECTION:NAME",
    help=f"Run only this item of a section (repeatable; {', '.join(selection.SUPPORTED)}).",
)
@click.option("--approve", is_flag=True, help="Skip confirmation prompt.")
@click.option(
    "--update",
    is_flag=True,
    help=(
        "Also upgrade what is already installed "
        "(packages, git checkouts, skill sources, fetched files) "
        "and run files onUpdate hooks."
    ),
)
def deploy(config, scope, only, approve, update):
    """Apply config to bring system to desired state.

    Under `sudo`, everything but system work runs as the invoking user first, then system
    work (system files, launchd daemons, system defaults, settingsctl host settings) as root,
    then mgmt as the user.
    """
    inv = _invoker()
    scope, only = _selection(scope, only)
    config_paths = list(config)
    if inv.root:
        _sudo_deploy(inv, config_paths, scope, approve, update, only)
        return
    merged = _load_merged_config(config_paths)
    _check_only(merged, config_paths, only)
    config_dir = _resolve_config_dir(config_paths)

    if not approve and not show_diff(merged, config_dir, scope, only=only):
        _confirm()

    # Deploy runs even when the preview was clean: --update upgrades and
    # state housekeeping have no diff line of their own.
    if not _deploy(merged, config_dir, scope, update, only=only):
        sys.exit(1)


@main.command(hidden=True)
@click.option("--config", multiple=True, default=["."])
@click.option("--scope", multiple=True, type=click.Choice(ALL_SECTIONS))
@click.option("--only", multiple=True)
@click.option("--approve", is_flag=True)
@click.option("--update", is_flag=True)
def apply(config, scope, only, approve, update):
    """Alias for deploy."""
    deploy.callback(config, scope, only, approve, update)


@main.command()
@click.option("--config", multiple=True, default=["."])
@click.option(
    "--scope", multiple=True, type=click.Choice(ALL_SECTIONS), help="Diff only these sections."
)
@click.option(
    "--only",
    multiple=True,
    metavar="SECTION:NAME",
    help=f"Run only this item of a section (repeatable; {', '.join(selection.SUPPORTED)}).",
)
def diff(config, scope, only):
    """Show what would change (dry-run)."""
    inv = _invoker()
    scope, only = _selection(scope, only)
    config_paths = list(config)
    if inv.root:
        if _sudo_diff(inv, config_paths, scope, only):
            sys.exit(1)
        return
    merged = _load_merged_config(config_paths)
    _check_only(merged, config_paths, only)
    config_dir = _resolve_config_dir(config_paths)
    if not show_diff(merged, config_dir, scope, only=only):
        sys.exit(1)


@main.command(hidden=True)
@click.option("--config", multiple=True, default=["."])
@click.option("--scope", multiple=True, type=click.Choice(ALL_SECTIONS))
@click.option("--only", multiple=True)
def plan(config, scope, only):
    """Alias for diff."""
    diff.callback(config, scope, only)


@main.command()
@click.option("--config", multiple=True, default=["."])
def status(config):
    """Show live status of managed launchd jobs (read-only)."""
    from tools.state import find_state_file

    inv = _invoker()
    merged = _load_merged_config(list(config))
    state = migrate_state_schema(load_json(find_state_file(state_file_path(merged))))
    if inv.root:
        state = root_records(state)
    try:
        rows = status_rows(merged.get("launchd"), state)
    except PermissionError as e:
        log(f"launchd: {e}", Color.RED)
        sys.exit(1)
    if not rows:
        log("No launchd jobs configured or tracked.", Color.BLUE)
        return
    for label, domain, st in rows:
        if not st.loaded:
            log(f"  {domain}/{label}: not loaded", Color.YELLOW)
            continue
        pid = st.pid if st.pid is not None else "-"
        color = (
            Color.GREEN if st.pid or st.last_exit in (None, "0", "(never exited)") else Color.RED
        )
        log(
            f"  {domain}/{label}: {st.state or 'loaded'} pid={pid} last exit={st.last_exit or '-'}",
            color,
        )


def _written_paths(config: dict) -> set[str]:
    """Paths every deploy rewrites, which a watch must not react to."""
    state_file = os.path.expanduser(config.get("stateFile") or "~/.local/state/tools/state.json")
    state_file = os.path.realpath(state_file)
    return {state_file, os.path.realpath(mgmt_key_path(state_file))}


# mgmt path keys besides `*File`; forgejo's workPath and forgejoBin are not sources.
_MGMT_PATH_KEYS = {"configDir", "profileIdDir"}
_MGMT_PATH_MAPS = {"baseUrlFiles", "varsFiles"}


def _source_paths(config: dict, config_dir: str) -> list[str]:
    """Paths the sections read their sources from: files, sops files, local skills, mgmt files."""
    paths = files_source_paths(config.get("files"), config_dir)
    paths += [
        s.sops_file for s in sops_section.parse_sops(config.get("sops")).secrets if s.sops_file
    ]
    skills = config.get("skills")
    skill_sources = skills.get("sources") if isinstance(skills, dict) else None
    if isinstance(skill_sources, dict):
        paths += [
            skills_source_root(name, src, "", config_dir)
            for name, src in skill_sources.items()
            if isinstance(src, dict) and src.get("path") is not None
        ]

    links_desired, _ = links_resolve(config.get("links") or [])
    paths += links_desired.values()

    def mgmt_paths(value):
        if isinstance(value, list):
            for v in value:
                yield from mgmt_paths(v)
        if not isinstance(value, dict):
            return
        for key, v in value.items():
            if not isinstance(key, str):
                continue
            if isinstance(v, str) and (key.endswith("File") or key in _MGMT_PATH_KEYS):
                yield v
            elif key in _MGMT_PATH_MAPS and isinstance(v, dict):
                yield from (p for p in v.values() if isinstance(p, str))
            else:
                yield from mgmt_paths(v)

    for value in mgmt_paths(config.get("mgmt")):
        path = mgmt_spec.path({"p": value}, "p", "mgmt", config_dir)
        if path:
            paths.append(path)
    return paths


def _watch_run(
    config_paths: list[str],
    scope: tuple[str, ...],
    approve: bool,
    files,
    excluded: set | None = None,
    watched: set | None = None,
    only: selection.Only | None = None,
) -> None:
    action = "deploy" if approve else "diff"
    wlog("=" * 60)
    if files:
        wlog(f"Running {action} after {len(files)} file change(s):")
        log_files(files)
    else:
        wlog(f"Running initial {action} on startup")
    wlog("=" * 60)
    started = time.monotonic()
    sources: list = []
    if watched is not None:
        # Until a config loads, every change under the config paths counts.
        watched.clear()
    try:
        merged = _load_merged_config(config_paths, sources)
        _check_only(merged, config_paths, only)
    except SystemExit:
        wlog("Config failed to load; still watching", Color.RED)
        return
    except Exception as exc:
        wlog(f"Config failed to load: {exc}; still watching", Color.RED)
        return
    if excluded is not None:
        excluded.clear()
        excluded.update(_written_paths(merged))
    config_dir = _resolve_config_dir(config_paths)
    if watched is not None:
        for p in sources + _source_paths(merged, config_dir):
            # snapshot() keys a symlink by its own name, so watch the link as well as its target.
            watched.add(os.path.join(os.path.realpath(os.path.dirname(p)), os.path.basename(p)))
            watched.add(os.path.realpath(p))
    narrowed = {"only": only} if only else {}
    if approve:
        ok = _deploy(merged, config_dir, scope, **narrowed)
    else:
        ok = show_diff(merged, config_dir, scope, **narrowed)
    elapsed = f"{time.monotonic() - started:.1f}s"
    if approve and ok:
        wlog(f"Deploy successful ({elapsed})", Color.GREEN)
    elif approve:
        wlog(f"Deploy failed ({elapsed}); still watching", Color.RED)
    elif ok:
        wlog(f"No changes pending ({elapsed})", Color.GREEN)
    else:
        wlog("Changes pending; not deploying without --approve", Color.YELLOW)


@main.command()
@click.option(
    "--config", multiple=True, required=True, help="Config file or directory to watch (repeatable)."
)
@click.option(
    "--scope", multiple=True, type=click.Choice(ALL_SECTIONS), help="Run only these sections."
)
@click.option(
    "--only",
    multiple=True,
    metavar="SECTION:NAME",
    help=f"Run only this item of a section (repeatable; {', '.join(selection.SUPPORTED)}).",
)
@click.option(
    "--approve",
    is_flag=True,
    help="Deploy on every change without prompting. Without it, only the diff is shown.",
)
@click.option(
    "--debounce",
    type=click.FloatRange(min=0),
    default=5.0,
    show_default=True,
    help="Seconds to wait after the last change before running.",
)
@click.option(
    "--poll",
    type=click.FloatRange(min=0.1),
    default=1.0,
    show_default=True,
    help="Seconds between scans of the config paths.",
)
@click.option("--tmux", is_flag=True, help="Run in tmux: session <hostname>, window tools-watch.")
def watch(config, scope, only, approve, debounce, poll, tmux):
    """Watch the config paths and redeploy when they change."""
    _refuse_root("watch")
    scope, only = _selection(scope, only)
    config_paths = list(config)
    missing = [p for p in config_paths if not os.path.exists(p)]
    if missing:
        wlog(f"Config path does not exist: {', '.join(missing)}", Color.RED)
        sys.exit(1)
    if tmux:
        run_in_tmux([a for a in sys.argv[1:] if a != "--tmux"])
        return

    mode = "deploy (--approve)" if approve else "diff only; pass --approve to deploy"
    wlog(f"Watching {', '.join(os.path.abspath(p) for p in config_paths)}")
    wlog(f"Mode: {mode}; debounce {debounce:g}s, poll {poll:g}s")

    excluded = _written_paths({})
    watched: set = set()

    def run(files):
        _watch_run(config_paths, scope, approve, files, excluded, watched, only)

    try:
        watch_loop(
            lambda: snapshot(config_paths),
            run,
            poll=poll,
            debounce=debounce,
            ignore=lambda path: (
                is_excluded(os.path.realpath(path), excluded)
                or bool(watched)
                and not is_watched(path, watched)
            ),
        )
    except KeyboardInterrupt:
        wlog("Received interrupt, stopping...")


@main.command()
def reconcile():
    """Continuously watch and apply config changes."""
    _refuse_root("reconcile")
    log("reconcile is not implemented yet", Color.YELLOW)
    sys.exit(1)


if __name__ == "__main__":
    main()
