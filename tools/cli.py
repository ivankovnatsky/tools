import argparse
import os
import sys

from tools.config import deep_merge, load_config, load_config_dir
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
    state_file = os.path.expanduser(config["stateFile"])
    migrate_state_file(state_file)
    state = load_json(state_file)

    success = True

    bun_config = config.get("bun", {})
    npm_config = config.get("npm", {})

    bun_packages = bun_config.get("packages", {})
    bun_only_config = {"configFile": bun_config.get("configFile")}
    success &= install_bun_packages(bun_packages, config["paths"], state, bun_only_config)

    npm_packages = npm_config.get("packages", {})
    npm_only_config = {"configFile": npm_config.get("configFile")}
    success &= install_npm_packages(npm_packages, config["paths"], state, npm_only_config)

    if config.get("uv", {}).get("packages"):
        success &= install_uv_packages(config["uv"]["packages"], config["paths"], state)

    success &= install_mcp_servers(config.get("mcp", {}).get("servers", {}), config["paths"], state)

    if config.get("curlShell"):
        success &= install_curl_shell_scripts(config["curlShell"], state)

    if config.get("gitRepos"):
        success &= install_git_repos(config["gitRepos"], state)

    success &= install_config_files(config.get("configFiles", []), config_dir, state)

    success &= install_brew_packages(config.get("brew", {}), state)

    save_json(state_file, state)
    return success


def cmd_deploy(args):
    config = _load_merged_config(args.config)
    config_dir = _resolve_config_dir(args.config)
    if not _deploy(config, config_dir):
        sys.exit(1)


def cmd_diff(args):
    # Validate config loads successfully before reporting unimplemented
    _load_merged_config(args.config)
    log("diff is not implemented yet", Color.YELLOW)
    sys.exit(1)


def cmd_reconcile(args):
    log("reconcile is not implemented yet", Color.YELLOW)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(prog="tools", description="Declarative configuration manager")
    sub = parser.add_subparsers(dest="command")

    for name in ("deploy", "apply", "diff", "plan", "reconcile"):
        p = sub.add_parser(name)
        p.add_argument("--config", action="append")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if not args.config:
        args.config = ["."]

    # apply is alias for deploy, plan is alias for diff
    commands = {
        "deploy": cmd_deploy,
        "apply": cmd_deploy,
        "diff": cmd_diff,
        "plan": cmd_diff,
        "reconcile": cmd_reconcile,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
