import os
import sys

from tools.config import deep_merge, load_config, load_config_dir
from tools.log import Color, log
from tools.state import load_json, migrate_state_file, save_json
from tools.user.brew import install_brew_packages
from tools.user.bun import install_bun_packages
from tools.user.curl_shell import install_curl_shell_scripts
from tools.user.git_repos import install_git_repos
from tools.user.mcp import install_mcp_servers
from tools.user.npm import install_npm_packages
from tools.user.uv import install_uv_packages


def _parse_config_args(argv: list[str]) -> list[str]:
    """Extract config paths from repeated --config arguments."""
    paths = []
    i = 1
    while i < len(argv):
        if argv[i] == "--config" and i + 1 < len(argv):
            paths.append(argv[i + 1])
            i += 2
        else:
            i += 1
    return paths


def main():
    config_paths = _parse_config_args(sys.argv)
    if not config_paths:
        log("Usage: tools --config <file-or-dir> [--config <file> ...]", Color.RED)
        sys.exit(1)

    config: dict = {}
    for config_path in config_paths:
        if os.path.isdir(config_path):
            loaded = load_config_dir(config_path)
        else:
            loaded = load_config(config_path)
        config = deep_merge(config, loaded)

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

    # Always call install_mcp_servers to handle both installation and removal
    # Even if servers is empty, we need to remove any existing servers
    success &= install_mcp_servers(config.get("mcp", {}).get("servers", {}), config["paths"], state)

    if config.get("curlShell"):
        success &= install_curl_shell_scripts(config["curlShell"], state)

    if config.get("gitRepos"):
        success &= install_git_repos(config["gitRepos"], state)

    # Always call install_brew_packages to handle both installation and removal
    # Even if brew section is empty, we need to remove any existing packages
    success &= install_brew_packages(config.get("brew", {}), state)

    save_json(state_file, state)

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
