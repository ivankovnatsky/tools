"""CLI argument parsing and subcommand dispatch."""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import click

from alerting import clear_alerts_for_repo, configure_alerts
from config import default_config_path, get_discord_webhook, load_config
from git_ops import run_git
from repo import init_repo, needs_init, push_repo, status_repo, sync_repo

BOOT_DEBOUNCE_SECONDS = 2 * 60


def get_repo_full_name(repo):
    """Return the display name used for alerts and logs."""
    display = repo.get("name") or os.path.basename(repo["path"])
    return f"{display} ({repo['remote']}/{repo['branch']})"


config_file_option = click.option(
    "--config-file",
    default=default_config_path,
    show_default="$XDG_CONFIG_HOME/reposync/config.json",
)


@click.group()
def main():
    """Sync local git repos with remotes."""


@main.command()
@config_file_option
def init(config_file):
    config = load_config(config_file)
    configure_alerts(webhook_file=config.get("discordWebhookFile"))
    webhook_url = get_discord_webhook(config)
    all_ok = True
    for repo in config.get("repositories", []):
        if not init_repo(repo, webhook_url):
            all_ok = False
        else:
            clear_alerts_for_repo(get_repo_full_name(repo))
    sys.exit(0 if all_ok else 1)


def get_system_uptime():
    """Return system uptime in seconds on supported platforms."""
    if sys.platform.startswith("linux"):
        proc_uptime = Path("/proc/uptime")
        try:
            return float(proc_uptime.read_text().split()[0])
        except (IndexError, OSError, ValueError):
            return None

    if sys.platform != "darwin":
        return None

    result = subprocess.run(
        ["sysctl", "-n", "kern.boottime"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None

    match = re.search(r"sec = (\d+)", result.stdout)
    if not match:
        return None

    return max(0, time.time() - int(match.group(1)))


def debounce_after_boot():
    if not os.environ.get("REPOSYNC_TIMER"):
        return  # run by hand, not by the timer
    uptime = get_system_uptime()
    if uptime is None or uptime >= BOOT_DEBOUNCE_SECONDS:
        return

    delay = BOOT_DEBOUNCE_SECONDS - uptime
    click.echo(
        f"System booted {uptime:.0f}s ago; delaying reposync for {delay:.0f}s.",
        err=True,
    )
    time.sleep(delay)


@main.command()
@config_file_option
def sync(config_file):
    config = load_config(config_file)
    configure_alerts(webhook_file=config.get("discordWebhookFile"))
    webhook_url = get_discord_webhook(config)

    debounce_after_boot()

    # Only init repos that need it
    failed_paths = set()
    for repo in config.get("repositories", []):
        if needs_init(repo):
            if not init_repo(repo, webhook_url):
                failed_paths.add(repo["path"])

    # Then sync (skip repos whose init failed)
    all_ok = not failed_paths
    for repo in config.get("repositories", []):
        if repo["path"] in failed_paths:
            continue
        if not sync_repo(repo, webhook_url):
            all_ok = False
        else:
            clear_alerts_for_repo(get_repo_full_name(repo))
    sys.exit(0 if all_ok else 1)


@main.command()
@config_file_option
def status(config_file):
    config = load_config(config_file)
    for repo in config.get("repositories", []):
        status_repo(repo)
    sys.exit(0)


def _gitdir_repo_at(path, repos):
    """The gitDir repo whose work tree holds `path`, if any. Such a work
    tree has no .git for git to discover, and discovery would land in an
    enclosing repo, so walk up by hand. samefile copes with symlinks and
    case-insensitive paths; a .git on the way up is a nearer repo."""
    candidates = [r for r in repos if r.get("gitDir") and os.path.isdir(r["path"])]
    d = os.path.abspath(path)
    while True:
        for repo in candidates:
            if os.path.samefile(d, repo["path"]):
                return repo
        parent = os.path.dirname(d)
        if os.path.exists(os.path.join(d, ".git")) or parent == d:
            return None
        d = parent


@main.command()
@config_file_option
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite the remote even if its tip was never local here; "
    "drops commits only another host has.",
)
@click.argument("path", default=".", type=click.Path(exists=True, file_okay=False))
def push(config_file, force, path):
    """Force the remote to match the local branch of the repo at PATH.

    Use after rewinding or rewriting the branch on purpose; `sync` would
    pull the dropped commits back.
    """
    config = load_config(config_file)
    configure_alerts(webhook_file=config.get("discordWebhookFile"))
    webhook_url = get_discord_webhook(config)
    repos = config.get("repositories", [])
    repo = _gitdir_repo_at(path, repos)
    if repo is None:
        toplevel = run_git("rev-parse", "--show-toplevel", cwd=path, check=False)
        if toplevel.returncode != 0:
            raise click.ClickException(f"{os.path.abspath(path)} is not in a git repo")
        target = os.path.realpath(toplevel.stdout.strip())
        repo = next((r for r in repos if os.path.realpath(r["path"]) == target), None)
        if repo is None:
            raise click.ClickException(f"{target} is not a reposync repo")
    if not push_repo(repo, webhook_url, force=force):
        sys.exit(1)
    clear_alerts_for_repo(get_repo_full_name(repo))
    sys.exit(0)
