import os
import sys

from tools.config import load_config, load_config_dir
from tools.log import Color, log
from tools.state import load_json, migrate_state_file, save_json
from tools.user.bun import install_bun_packages
from tools.user.curl_shell import install_curl_shell_scripts
from tools.user.git_repos import install_git_repos
from tools.user.mcp import install_mcp_servers
from tools.user.npm import install_npm_packages
from tools.user.uv import install_uv_packages


def main():
    if len(sys.argv) < 3 or sys.argv[1] != "--config":
        log("Usage: tools --config <file-or-directory>", Color.RED)
        sys.exit(1)

    config_path = sys.argv[2]
    if os.path.isdir(config_path):
        config = load_config_dir(config_path)
    else:
        config = load_config(config_path)

    state_file = config["stateFile"]
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
        success &= install_curl_shell_scripts(config["curlShell"], config["paths"], state)

    if config.get("gitRepos"):
        success &= install_git_repos(config["gitRepos"], config["paths"], state)

    save_json(state_file, state)

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
