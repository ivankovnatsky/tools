import os
from typing import Dict, Set

from tools.log import Color, log
from tools.util import SecretSubstitutionError, run_command, substitute_secrets


def get_installed_mcp_servers(claude_cli: str, env: Dict = None) -> Set[str]:
    if not os.path.exists(claude_cli):
        return set()

    returncode, stdout, stderr = run_command([claude_cli, "mcp", "list"], env)
    if returncode != 0:
        log(f"Failed to list MCP servers (exit {returncode}): {stderr}", Color.YELLOW)
        return set()

    servers = set()
    for line in stdout.split("\n"):
        line = line.strip()
        if not line or ":" not in line:
            continue
        has_transport = "(SSE)" in line or "(HTTP)" in line or "(STDIO)" in line
        has_status = "✓" in line or "!" in line or "✗" in line
        if has_transport or has_status:
            server_name = line.split(":")[0].strip()
            if not server_name.startswith("claude.ai "):
                servers.add(server_name)
    log(f"Detected installed MCP servers: {servers}", Color.BLUE)
    return servers


def install_mcp_servers(servers: Dict, paths: Dict, state: Dict):
    claude_cli = paths["claudeCli"]

    if not os.path.exists(claude_cli):
        log("Claude CLI not found, skipping MCP server configuration", Color.YELLOW)
        return True

    env = os.environ.copy()
    env["PATH"] = f"{paths['nodejs']}:{paths['npmBin']}:{paths['python']}:{env.get('PATH', '')}"

    desired = set(servers.keys())
    current = get_installed_mcp_servers(claude_cli, env)
    to_install = desired - current
    to_remove = current - desired

    state_changed = False

    if to_remove:
        log(f"Removing MCP servers: {', '.join(to_remove)}", Color.RED)
        for server_name in to_remove:
            returncode, _, stderr = run_command(
                [claude_cli, "mcp", "remove", server_name, "-s", "user"], env
            )
            if returncode != 0:
                log(f"Failed to remove {server_name}: {stderr}", Color.RED)
            else:
                log(f"Removed {server_name}", Color.GREEN)
                state_changed = True

    if to_install:
        log(f"Installing MCP servers: {', '.join(to_install)}", Color.GREEN)
        for server_name in to_install:
            server_config = servers[server_name]

            if server_config.get("command"):
                cmd = server_config["command"].split()
            else:
                cmd = [
                    claude_cli,
                    "mcp",
                    "add",
                    "--scope",
                    server_config["scope"],
                    "--transport",
                    server_config["transport"],
                    server_name,
                ]
                secret_paths = server_config.get("secretPaths", {})
                header_failed = False
                for header in server_config.get("headers", []):
                    try:
                        processed_header = substitute_secrets(header, secret_paths)
                    except SecretSubstitutionError as e:
                        log(
                            f"Skipping {server_name}: {e}",
                            Color.RED,
                        )
                        header_failed = True
                        break
                    cmd.extend(["-H", processed_header])
                if header_failed:
                    continue
                args = server_config.get("args", [])
                if args:
                    cmd.append("--")
                    cmd.extend(args)
                elif server_config.get("url"):
                    cmd.append(server_config["url"])

            returncode, _, stderr = run_command(cmd, env)
            if returncode != 0:
                if "already exists" in stderr:
                    log(
                        f"{server_name} already exists, marking as installed",
                        Color.BLUE,
                    )
                else:
                    log(f"Failed to install {server_name}: {stderr}", Color.RED)
                    continue
            else:
                log(f"Installed {server_name}", Color.GREEN)
                state_changed = True

    if not to_install and not to_remove:
        log("All MCP servers already installed", Color.BLUE)

    if state_changed or set(state.get("mcp", {}).get("servers", {}).keys()) != desired:
        state.setdefault("mcp", {})["servers"] = {
            name: {
                "installed": True,
                "scope": config.get("scope"),
                "transport": config.get("transport"),
                "url": config.get("url"),
                "command": config.get("command"),
            }
            for name, config in servers.items()
        }

    return True
