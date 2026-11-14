import os
import shlex
import shutil
from typing import Dict, Optional, Set

from tools.log import Color, debug, log
from tools.util import SecretSubstitutionError, run_command, substitute_secrets


def get_installed_mcp_servers(claude_cli: str, env: Dict = None) -> Optional[Set[str]]:
    """Servers currently registered, or None when the CLI cannot list them.

    A listing failure must not read as "nothing installed": reconciling
    against an empty set would forget every tracked server after one
    transient error.
    """
    if not os.path.exists(claude_cli):
        return set()

    returncode, stdout, stderr = run_command([claude_cli, "mcp", "list"], env)
    if returncode != 0:
        log(f"Failed to list MCP servers (exit {returncode}): {stderr}", Color.YELLOW)
        return None

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
    debug(f"Detected installed MCP servers: {servers}", Color.BLUE)
    return servers


def server_fingerprint(config: Dict) -> tuple:
    """The parts of a server definition that a re-register would change.

    args/headers/secretPaths are registration inputs too: leaving them out
    means editing them silently does nothing. Headers here are the *config
    templates* (`@VAR@` placeholders), never resolved secrets.
    """
    return (
        config.get("scope"),
        config.get("transport"),
        config.get("url"),
        config.get("command"),
        tuple(config.get("args") or ()),
        tuple(config.get("headers") or ()),
        tuple(sorted((config.get("secretPaths") or {}).items())),
    )


def resolve_claude_cli(paths: Dict) -> str | None:
    """Locate the claude CLI the same way for diff and deploy."""
    claude_cli = paths.get("claudeCli") or shutil.which("claude")
    if not claude_cli or not os.path.exists(claude_cli):
        return None
    return claude_cli


def build_mcp_env(paths: Dict) -> Dict[str, str]:
    """PATH augmentation shared by diff and deploy."""
    env = os.environ.copy()
    extra_paths = [
        paths.get("nodejs", ""),
        paths.get("npmBin", ""),
        paths.get("python", ""),
    ]
    extra = ":".join(p for p in extra_paths if p)
    if extra:
        env["PATH"] = f"{extra}:{env.get('PATH', '')}"
    return env


def config_scope(config: Dict) -> str:
    """The scope a configured server is registered in, and removed from."""
    return (config or {}).get("scope") or "user"


def foreign_records(servers: Dict, state: Dict, current: Set[str]) -> Dict[str, Dict]:
    """Configured names registered outside tools in a scope other than the configured
    one: name -> {"scope": configured scope, "listed": shown by `claude mcp list`}.
    Left alone until the configured scope changes; a listed one also until `claude mcp
    list` stops showing it. An unlisted one cannot be seen leaving."""
    recorded = state.get("mcp", {}).get("foreign") or {}
    if not isinstance(recorded, dict):
        return {}
    kept = {}
    for name, record in recorded.items():
        if isinstance(record, str):
            record = {"scope": record, "listed": True}
        if not isinstance(record, dict) or name not in servers:
            continue
        if record.get("scope") != config_scope(servers[name]):
            continue
        listed = record.get("listed") is not False
        if listed and name not in current:
            continue
        kept[name] = {"scope": record["scope"], "listed": listed}
    return kept


def foreign_servers(servers: Dict, state: Dict, current: Set[str]) -> Dict[str, str]:
    """`foreign_records` as name -> configured scope."""
    return {n: r["scope"] for n, r in foreign_records(servers, state, current).items()}


def not_in_scope(name: str, output: str) -> bool:
    """`claude mcp remove -s SCOPE NAME` failed because NAME is not registered there:
    `No MCP server named "NAME" in user scope` (`in .mcp.json` for project)."""
    return f'No MCP server named "{name}" in ' in output


def install_mcp_servers(servers: Dict, paths: Dict, state: Dict):
    claude_cli = resolve_claude_cli(paths)

    if not claude_cli:
        log("Claude CLI not found, skipping MCP server configuration", Color.YELLOW)
        return True

    env = build_mcp_env(paths)

    desired = set(servers.keys())
    current = get_installed_mcp_servers(claude_cli, env)
    if current is None:
        log("Cannot reconcile MCP servers without a server list, skipping", Color.RED)
        return False
    tracked_cfg = state.get("mcp", {}).get("servers", {})
    tracked = set(tracked_cfg.keys())
    # A server is identified by name, so a changed url/transport/scope/command
    # is invisible to `claude mcp list`. Re-register those, or editing config
    # silently does nothing.
    changed_cfg = {
        name
        for name in desired & tracked & current
        if server_fingerprint(servers[name]) != server_fingerprint(tracked_cfg[name])
    }
    # Registered outside tools under a configured name: tools owns it now. Its live
    # definition is never read, so it is re-registered from config once. Only one
    # registered in the configured scope is adopted: removing it there must succeed.
    foreign_recs = foreign_records(servers, state, current)
    foreign = {n: r["scope"] for n, r in foreign_recs.items()}
    adopted = (desired & current) - tracked - set(foreign)
    for name in sorted(adopted):
        log(f"Adopting {name}, registered outside tools", Color.YELLOW)
    changed_cfg |= adopted
    to_install = (desired - current) | changed_cfg
    # Only servers we installed are ours to remove. `current - desired` would
    # take anything registered by hand or by another tool.
    to_remove = ((tracked & current) - desired) | changed_cfg
    state_changed = False
    success = True
    failed: set = set()
    failed_removals: set = set()

    def leave_foreign(name: str, listed: bool) -> None:
        scope = config_scope(servers[name])
        log(
            f"{name} is registered outside tools, not in scope {scope}; left alone",
            Color.YELLOW,
        )
        foreign[name] = scope
        foreign_recs[name] = {"scope": scope, "listed": listed}

    if to_remove:
        log(f"Removing MCP servers: {', '.join(to_remove)}", Color.RED)
        for server_name in to_remove:
            # Remove from the scope the server was registered under — a
            # hardcoded `-s user` can never remove local/project servers.
            scope = config_scope(tracked_cfg.get(server_name) or servers.get(server_name))
            returncode, stdout, stderr = run_command(
                [claude_cli, "mcp", "remove", server_name, "-s", scope], env
            )
            if returncode != 0 and server_name in adopted:
                if not_in_scope(server_name, stderr + stdout):
                    leave_foreign(server_name, True)
                else:
                    # Not adopted this run: neither recorded nor added; retried next run.
                    log(f"Failed to adopt {server_name}: {stderr}", Color.RED)
                    success = False
                    failed.add(server_name)
            elif returncode != 0:
                log(f"Failed to remove {server_name}: {stderr}", Color.RED)
                success = False
                failed_removals.add(server_name)
            else:
                log(f"Removed {server_name}", Color.GREEN)
                state_changed = True

    if to_install:
        log(f"Installing MCP servers: {', '.join(to_install)}", Color.GREEN)
        for server_name in sorted(to_install):
            if server_name in foreign or server_name in failed:
                continue
            server_config = servers[server_name]

            if server_config.get("command"):
                # shlex, not str.split: a quoted argument (--flag "two words")
                # must survive as one argv entry.
                cmd = shlex.split(server_config["command"])
            else:
                scope = server_config.get("scope")
                transport = server_config.get("transport")
                if not scope or not transport:
                    log(
                        f"Skipping {server_name}: `scope` and `transport` are required",
                        Color.RED,
                    )
                    success = False
                    failed.add(server_name)
                    continue
                cmd = [
                    claude_cli,
                    "mcp",
                    "add",
                    "--scope",
                    scope,
                    "--transport",
                    transport,
                    server_name,
                ]
                # KNOWN LIMITATION: `claude mcp add` only accepts headers as
                # argv (-H), so a resolved secret is briefly visible in
                # /proc/<pid>/cmdline to other local users while the command
                # runs. The CLI has no stdin/file alternative today. Secrets
                # are never logged or persisted to state; keep secret files
                # user-readable only and treat multi-user hosts accordingly.
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
                    # Not registered: recording it as installed would hide the
                    # missing credential from every future run.
                    success = False
                    failed.add(server_name)
                    continue
                args = server_config.get("args", [])
                if args:
                    cmd.append("--")
                    cmd.extend(args)
                elif server_config.get("url"):
                    cmd.append(server_config["url"])

            returncode, _, stderr = run_command(cmd, env)
            if returncode != 0 and "already exists" in stderr:
                # Registered without showing in `mcp list`: adopt it the same way.
                log(f"Adopting {server_name}, registered outside tools", Color.YELLOW)
                scope = config_scope(server_config)
                rc, out, err = run_command(
                    [claude_cli, "mcp", "remove", server_name, "-s", scope], env
                )
                if rc != 0 and not_in_scope(server_name, err + out):
                    leave_foreign(server_name, server_name in current)
                    continue
                if rc != 0:
                    log(f"Failed to adopt {server_name}: {err}", Color.RED)
                    success = False
                    failed.add(server_name)
                    continue
                returncode, _, stderr = run_command(cmd, env)
            if returncode != 0:
                log(f"Failed to install {server_name}: {stderr}", Color.RED)
                success = False
                failed.add(server_name)
                continue
            else:
                log(f"Installed {server_name}", Color.GREEN)
                state_changed = True

    if not to_install and not to_remove:
        debug("All MCP servers already installed", Color.BLUE)

    if foreign_recs:
        state.setdefault("mcp", {})["foreign"] = foreign_recs
    else:
        state.get("mcp", {}).pop("foreign", None)
    tracked_now = set(state.get("mcp", {}).get("servers", {}).keys())
    if state_changed or tracked_now != desired - set(foreign):
        # A server whose `mcp add` failed is not installed; recording it as
        # such would hide the failure from the next run's diff.
        new_servers = {}
        for name, config in servers.items():
            if name in failed or name in foreign:
                continue
            if name in failed_removals and name in tracked_cfg:
                # Re-register whose removal failed: the old registration is
                # still live, so keep the old entry — the fingerprint keeps
                # mismatching and the next run retries.
                new_servers[name] = tracked_cfg[name]
                continue
            new_servers[name] = {
                "installed": True,
                "scope": config.get("scope"),
                "transport": config.get("transport"),
                "url": config.get("url"),
                "command": config.get("command"),
                "args": list(config.get("args") or []),
                # Header *templates* (@VAR@ placeholders), never resolved
                # secret values.
                "headers": list(config.get("headers") or []),
                "secretPaths": dict(config.get("secretPaths") or {}),
            }
        # A tracked server whose removal failed is still registered; dropping
        # it from state would orphan it — never removed, never diffed again.
        for name in failed_removals:
            if name not in new_servers and name in tracked_cfg:
                new_servers[name] = tracked_cfg[name]
        state.setdefault("mcp", {})["servers"] = new_servers

    return success
