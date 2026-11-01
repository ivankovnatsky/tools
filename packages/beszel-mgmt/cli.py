"""Argument parsing and CLI entry point for beszel-mgmt."""

import json
import os
import sys

import click

from auth import read_secret_file
from client import BeszelClient
from commands import cmd_list, cmd_get, cmd_create, cmd_update, cmd_delete, cmd_sync
from constants import (
    DISCORD_WEBHOOK_SECRET,
    EMAIL_SECRET,
    PASSWORD_SECRET,
    SECRETS_DIR_ENV,
)


def _require_secret(path: str, label: str) -> str:
    value = read_secret_file(path)
    if not value:
        click.echo(
            f"Error: missing {label} (expected file at {path})",
            err=True,
        )
        sys.exit(1)
    return value


@click.group()
def main():
    """Beszel systems management tool."""
    pass


@main.command("list")
@click.option("--base-url", required=True, help="Beszel base URL")
@click.option("--email", required=True, help="User email")
@click.option("--password", required=True, help="User password")
@click.option(
    "--output-format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format",
)
def list_cmd(base_url, email, password, output_format):
    """List all systems."""
    client = BeszelClient(base_url, email, password)
    cmd_list(output_format, client)


@main.command("get")
@click.option("--base-url", required=True, help="Beszel base URL")
@click.option("--email", required=True, help="User email")
@click.option("--password", required=True, help="User password")
@click.option("--system-id", required=True, help="System ID")
def get_cmd(base_url, email, password, system_id):
    """Get system details."""
    client = BeszelClient(base_url, email, password)
    cmd_get(system_id, client)


@main.command("create")
@click.option("--base-url", required=True, help="Beszel base URL")
@click.option("--email", required=True, help="User email")
@click.option("--password", required=True, help="User password")
@click.option("--name", required=True, help="System name")
@click.option("--host", required=True, help="System host/IP")
@click.option("--port", default="45876", help="System port (default: 45876)")
def create_cmd(base_url, email, password, name, host, port):
    """Create a new system."""
    client = BeszelClient(base_url, email, password)
    cmd_create(name, host, port, client)


@main.command("update")
@click.option("--base-url", required=True, help="Beszel base URL")
@click.option("--email", required=True, help="User email")
@click.option("--password", required=True, help="User password")
@click.option("--system-id", required=True, help="System ID")
@click.option("--name", default=None, help="New system name")
@click.option("--host", default=None, help="New system host/IP")
@click.option("--port", default=None, help="New system port")
def update_cmd(base_url, email, password, system_id, name, host, port):
    """Update a system."""
    client = BeszelClient(base_url, email, password)
    cmd_update(system_id, name, host, port, client)


@main.command("delete")
@click.option("--base-url", required=True, help="Beszel base URL")
@click.option("--email", required=True, help="User email")
@click.option("--password", required=True, help="User password")
@click.option("--system-id", required=True, help="System ID to delete")
def delete_cmd(base_url, email, password, system_id):
    """Delete a system."""
    client = BeszelClient(base_url, email, password)
    cmd_delete(system_id, client)


@main.command("sync")
@click.option("--config-file", required=True, help="JSON configuration file")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would be changed without making changes",
)
@click.option(
    "--secrets-dir",
    envvar=SECRETS_DIR_ENV,
    required=True,
    help=f"Directory holding {EMAIL_SECRET}, {PASSWORD_SECRET} and "
    f"{DISCORD_WEBHOOK_SECRET} (env: {SECRETS_DIR_ENV})",
)
def sync_cmd(config_file, dry_run, secrets_dir):
    """Sync systems from configuration file.

    Reads credentials from files under --secrets-dir. base_url is read from
    the config JSON.
    """
    with open(config_file) as f:
        config = json.load(f)
    base_url = config.get("base_url")
    if not base_url:
        click.echo("Error: config file missing 'base_url' field", err=True)
        sys.exit(1)

    email = _require_secret(os.path.join(secrets_dir, EMAIL_SECRET), "email")
    password = _require_secret(os.path.join(secrets_dir, PASSWORD_SECRET), "password")
    discord_webhook = read_secret_file(
        os.path.join(secrets_dir, DISCORD_WEBHOOK_SECRET)
    )

    client = BeszelClient(base_url, email, password)
    cmd_sync(config_file, dry_run, discord_webhook, client)
