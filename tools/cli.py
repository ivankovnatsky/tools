import os
import sys

import click

from tools.config import deep_merge, load_config, load_config_dir
from tools.diff import show_diff
from tools.log import Color, log
from tools.state import load_json, migrate_state_file, save_json
from tools.user.brew import install_brew_packages
from tools.user.bun import install_bun_packages
from tools.user.config_files import install_config_files
from tools.user.curl_shell import install_curl_shell_scripts
from tools.user.git_repos import install_git_repos
from tools.user.mcp import install_mcp_servers
from tools.user.npm import install_npm_packages
from tools.user.uv import install_uv_packages


def _resolve_config_dir(config_paths: list[str]) -> str:
    """Resolve the config directory from the first --config path."""
    first = config_paths[0]
    if os.path.isdir(first):
        return os.path.abspath(first)
    return os.path.abspath(os.path.dirname(first))


def _load_merged_config(config_paths: list[str]) -> dict:
    """Load and merge config from one or more paths."""
    config: dict = {}
    for config_path in config_paths:
        if os.path.isdir(config_path):
            loaded = load_config_dir(config_path)
        else:
            loaded = load_config(config_path)
        config = deep_merge(config, loaded)
    return config


def _deploy(config: dict, config_dir: str) -> bool:
    """Apply config to bring system to desired state. Returns True on success."""
    state_file = os.path.expanduser(
        config.get("stateFile", "~/.local/state/tools/state.json")
    )
    migrate_state_file(state_file)
    state = load_json(state_file)

    success = True
    paths = config.get("paths", {})

    bun_config = config.get("bun", {})
    npm_config = config.get("npm", {})

    bun_packages = bun_config.get("packages", {})
    bun_only_config = {"configFile": bun_config.get("configFile")}
    success &= install_bun_packages(bun_packages, paths, state, bun_only_config)

    npm_packages = npm_config.get("packages", {})
    npm_only_config = {"configFile": npm_config.get("configFile")}
    success &= install_npm_packages(npm_packages, paths, state, npm_only_config)

    if config.get("uv", {}).get("packages"):
        success &= install_uv_packages(config["uv"]["packages"], paths, state)

    success &= install_mcp_servers(config.get("mcp", {}).get("servers", {}), paths, state)

    if config.get("curlShell"):
        success &= install_curl_shell_scripts(config["curlShell"], state)

    if config.get("gitRepos"):
        success &= install_git_repos(config["gitRepos"], state)

    success &= install_config_files(config.get("configFiles", []), config_dir, state)

    success &= install_brew_packages(config.get("brew", {}), state)

    save_json(state_file, state)
    return success


@click.group()
def main():
    """Declarative configuration manager."""


@main.command()
@click.option("--config", multiple=True, default=["."])
def deploy(config):
    """Apply config to bring system to desired state."""
    config_paths = list(config)
    merged = _load_merged_config(config_paths)
    config_dir = _resolve_config_dir(config_paths)
    if not _deploy(merged, config_dir):
        sys.exit(1)


@main.command(hidden=True)
@click.option("--config", multiple=True, default=["."])
def apply(config):
    """Alias for deploy."""
    deploy.callback(config)


@main.command()
@click.option("--config", multiple=True, default=["."])
def diff(config):
    """Show what would change (dry-run)."""
    config_paths = list(config)
    merged = _load_merged_config(config_paths)
    config_dir = _resolve_config_dir(config_paths)
    if not show_diff(merged, config_dir):
        sys.exit(1)


@main.command(hidden=True)
@click.option("--config", multiple=True, default=["."])
def plan(config):
    """Alias for diff."""
    diff.callback(config)


@main.command()
def reconcile():
    """Continuously watch and apply config changes."""
    log("reconcile is not implemented yet", Color.YELLOW)
    sys.exit(1)


if __name__ == "__main__":
    main()
