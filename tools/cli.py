import os
import sys
import time

import click

from tools.config import ConfigError, deep_merge, load_config, load_config_dir, resolve_config
from tools.diff import ALL_SECTIONS, show_diff
from tools.log import Color, log, set_verbose
from tools.services import has_work as services_has_work
from tools.services import install_services, release_services
from tools.services.keys import key_path as services_key_path
from tools.state import load_json, migrate_state_file, migrate_state_schema, save_json
from tools.user import secrets_flow
from tools.user import sops as sops_section
from tools.user import templates as templates_section
from tools.user.brew import install_brew_packages
from tools.user.bun import install_bun_packages
from tools.user.curl_shell import install_curl_shell_scripts
from tools.user.defaults import has_work as defaults_has_work
from tools.user.defaults import install_defaults, run_restarts
from tools.user.dock import has_work as dock_has_work
from tools.user.dock import install_dock
from tools.user.files import install_files
from tools.user.flatpak import install_flatpak_packages
from tools.user.git_repos import install_git_repos
from tools.user.go import install_go_packages
from tools.user.launchd import has_work as launchd_has_work
from tools.user.launchd import install_launchd, status_rows
from tools.user.links import has_work as links_has_work
from tools.user.links import install_links
from tools.user.mcp import install_mcp_servers
from tools.user.npm import install_npm_packages
from tools.user.ollama_models import install_ollama_models
from tools.user.pi import install_pi_packages
from tools.user.skills import has_work as skills_has_work
from tools.user.skills import install_skills
from tools.user.uv import install_uv_packages
from tools.watch import is_excluded, log_files, run_in_tmux, snapshot, watch_loop, wlog


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


def _load_merged_config(config_paths: list[str]) -> dict:
    """Load and merge config from one or more paths."""
    config: dict = {}
    for config_path in config_paths:
        try:
            if os.path.isdir(config_path):
                loaded = load_config_dir(config_path)
            else:
                loaded = load_config(config_path)
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
        return resolve_config(config, _resolve_config_dir(config_paths))
    except ConfigError as exc:
        log(f"Config error: {exc}", Color.RED)
        sys.exit(1)


def _deploy(config: dict, config_dir: str, scope: tuple[str, ...] = ()) -> bool:
    """Apply config to bring system to desired state. Returns True on success."""
    # `or`, not a default arg: an explicit `stateFile:` (YAML null) must not
    # crash expanduser — and it must resolve identically to show_diff.
    state_file = os.path.expanduser(config.get("stateFile") or "~/.local/state/tools/state.json")
    migrate_state_file(state_file)
    state = migrate_state_schema(load_json(state_file))

    active = set(scope) if scope else set(ALL_SECTIONS)

    success = True
    paths = config.get("paths", {})

    # The reconcilers mutate `state` in memory as they install/remove; the
    # try/finally guarantees those mutations reach disk even when a later
    # section crashes. Without it, packages installed earlier in the same run
    # become permanently unowned.
    try:
        success = _run_sections(
            config,
            config_dir,
            state,
            paths,
            active,
            lambda: save_json(state_file, state),
            state_file,
        )
    finally:
        save_json(state_file, state)
    return success


def _run_sections(
    config: dict,
    config_dir: str,
    state: dict,
    paths: dict,
    active: set,
    persist=lambda: None,
    state_file: str = "",
) -> bool:
    success = True

    moves = secrets_flow.move_between_sections(
        state,
        files_entries=config.get("files"),
        sops_raw=config.get("sops"),
        templates_raw=config.get("templates"),
        config_dir=config_dir,
    )
    for move in moves:
        log(f"Moved: {move}", Color.GREEN)
    if moves:
        persist()

    def registry():
        return secrets_flow.ownership_conflicts(
            files_entries=config.get("files"),
            sops_raw=config.get("sops"),
            templates_raw=config.get("templates"),
            links_raw=config.get("links"),
            skills_raw=config.get("skills"),
            state=state,
            config_dir=config_dir,
            git_repos_raw=config.get("gitRepos"),
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

    success &= secrets(conflicts, secrets_flow.EARLY if inside else None)

    if "gitRepos" in active and (
        config.get("gitRepos") or state.get("gitRepos", {}).get("installed")
    ):
        success &= install_git_repos(config.get("gitRepos") or {}, state)
        # A fresh checkout can bring in symlinks that make paths collide.
        conflicts = registry()

    if inside:
        success &= secrets(conflicts, secrets_flow.LATE)
    if run_templates:
        success &= secrets_flow.run_template_hooks(config.get("templates"), state)
    for line in secrets_flow.format_notify(report):
        log(line, Color.YELLOW)

    # bun/npm index paths['bun'] and paths['nodejs'] directly, so calling them
    # with nothing configured raises KeyError on a machine show_diff calls
    # clean. Gate them like uv/go: run only when there is work or state.
    if "bun" in active:
        bun_config = config.get("bun", {})
        bun_packages = bun_config.get("packages", {})
        bun_only_config = {"configFile": bun_config.get("configFile")}
        if bun_packages or bun_config.get("configFile") or state.get("bun", {}).get("packages"):
            success &= install_bun_packages(bun_packages, paths, state, bun_only_config)

    if "npm" in active:
        npm_config = config.get("npm", {})
        npm_packages = npm_config.get("packages", {})
        npm_only_config = {"configFile": npm_config.get("configFile")}
        if npm_packages or npm_config.get("configFile") or state.get("npm", {}).get("packages"):
            success &= install_npm_packages(npm_packages, paths, state, npm_only_config)

    if "uv" in active:
        uv_packages = config.get("uv", {}).get("packages", {})
        if uv_packages or state.get("uv", {}).get("packages"):
            success &= install_uv_packages(uv_packages, paths, state)

    if "go" in active:
        go_packages = config.get("go", {}).get("packages", {})
        go_state = state.get("go", {})
        if go_packages or go_state.get("packages"):
            success &= install_go_packages(go_packages, paths, state)

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

    # Gate on state as well as config, or dropping the last entry means the
    # reconciler never runs again and its state is orphaned forever.
    if "curlShell" in active and (
        config.get("curlShell") or state.get("curlShell", {}).get("installed")
    ):
        success &= install_curl_shell_scripts(config.get("curlShell") or {}, state)

    if "files" in active:
        success &= install_files(
            config.get("files", []) or [],
            config_dir,
            state,
            conflicts,
            sops_section.parse_sops(config.get("sops")).secrets_dir()[0],
        )

    # Gated on state too, like gitRepos: dropping the whole section must
    # still reach the reconciler so it can remove what it installed.
    if "skills" in active and skills_has_work(config.get("skills") or {}, state):
        success &= install_skills(config.get("skills") or {}, config_dir, state)

    # After skills, so links into freshly installed skill dirs find their source.
    if "links" in active and links_has_work(config.get("links") or [], state):
        success &= install_links(config.get("links") or [], state)

    if "brew" in active:
        success &= install_brew_packages(config.get("brew", {}), state)

    if "ollamaModels" in active:
        success &= install_ollama_models(config.get("ollamaModels", {}) or {}, state)

    if "flatpak" in active:
        success &= install_flatpak_packages(config.get("flatpak", {}) or {}, state)

    # After files, so restartOn hashes see files written in this run.
    if "launchd" in active and launchd_has_work(config.get("launchd"), state):
        success &= install_launchd(
            config.get("launchd"),
            state,
            **sops_section.secrets_dir_kwargs(config.get("sops")),
        )

    # Talks to service APIs; gated so an absent section makes no network calls.
    if "services" in active and services_has_work(config.get("services")):
        success &= install_services(
            config.get("services"),
            config_dir,
            state,
            services_key_path(state_file) if state_file else None,
            **sops_section.secrets_dir_kwargs(config.get("sops")),
        )
    elif "services" in active:
        release_services(config.get("services"), state)

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
        run_restarts(restarts)
    return success


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Show debug output.")
def main(verbose):
    """Declarative configuration manager."""
    set_verbose(verbose)


@main.command()
@click.option("--config", multiple=True, default=["."])
@click.option(
    "--scope", multiple=True, type=click.Choice(ALL_SECTIONS), help="Run only these sections."
)
@click.option("--approve", is_flag=True, help="Skip confirmation prompt.")
def deploy(config, scope, approve):
    """Apply config to bring system to desired state."""
    config_paths = list(config)
    merged = _load_merged_config(config_paths)
    config_dir = _resolve_config_dir(config_paths)

    if not approve:
        has_changes = not show_diff(merged, config_dir, scope)
        if has_changes:
            answer = click.prompt("\nType 'yes' to deploy", default="no")
            if answer != "yes":
                log("Aborted.", Color.YELLOW)
                sys.exit(1)

    # Deploy runs even when the preview was clean: non-destructive
    # maintenance (pulling tracked git checkouts) has no diff line of its
    # own, and skipping it here would make it exclusive to --approve.
    # A clean diff means there is nothing approval-worthy, not nothing to do.
    if not _deploy(merged, config_dir, scope):
        sys.exit(1)


@main.command(hidden=True)
@click.option("--config", multiple=True, default=["."])
@click.option("--scope", multiple=True, type=click.Choice(ALL_SECTIONS))
@click.option("--approve", is_flag=True)
def apply(config, scope, approve):
    """Alias for deploy."""
    deploy.callback(config, scope, approve)


@main.command()
@click.option("--config", multiple=True, default=["."])
@click.option(
    "--scope", multiple=True, type=click.Choice(ALL_SECTIONS), help="Diff only these sections."
)
def diff(config, scope):
    """Show what would change (dry-run)."""
    config_paths = list(config)
    merged = _load_merged_config(config_paths)
    config_dir = _resolve_config_dir(config_paths)
    if not show_diff(merged, config_dir, scope):
        sys.exit(1)


@main.command(hidden=True)
@click.option("--config", multiple=True, default=["."])
@click.option("--scope", multiple=True, type=click.Choice(ALL_SECTIONS))
def plan(config, scope):
    """Alias for diff."""
    diff.callback(config, scope)


@main.command()
@click.option("--config", multiple=True, default=["."])
def status(config):
    """Show live status of managed launchd jobs (read-only)."""
    from tools.state import find_state_file

    merged = _load_merged_config(list(config))
    state_file = os.path.expanduser(merged.get("stateFile") or "~/.local/state/tools/state.json")
    state = migrate_state_schema(load_json(find_state_file(state_file)))
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
    return {state_file, os.path.realpath(services_key_path(state_file))}


def _watch_run(
    config_paths: list[str],
    scope: tuple[str, ...],
    approve: bool,
    files,
    excluded: set | None = None,
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
    try:
        merged = _load_merged_config(config_paths)
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
    if approve:
        ok = _deploy(merged, config_dir, scope)
    else:
        ok = show_diff(merged, config_dir, scope)
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
def watch(config, scope, approve, debounce, poll, tmux):
    """Watch the config paths and redeploy when they change."""
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

    def run(files):
        _watch_run(config_paths, scope, approve, files, excluded)

    try:
        watch_loop(
            lambda: snapshot(config_paths),
            run,
            poll=poll,
            debounce=debounce,
            ignore=lambda path: is_excluded(os.path.realpath(path), excluded),
        )
    except KeyboardInterrupt:
        wlog("Received interrupt, stopping...")


@main.command()
def reconcile():
    """Continuously watch and apply config changes."""
    log("reconcile is not implemented yet", Color.YELLOW)
    sys.exit(1)


if __name__ == "__main__":
    main()
