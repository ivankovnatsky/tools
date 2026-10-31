"""Configuration loading."""

import json
import os

import click

from git_ops import register_git_dir

# Placeholders in the config and the config keys naming the file whose
# content replaces them. Substituted at load time so secrets never land in
# the nix store, yet the on-disk config is usable by hand.
PLACEHOLDERS = {
    "@domain@": "domainFile",
    "@username@": "usernameFile",
}


def default_config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "reposync", "config.json")


def load_config(config_file):
    try:
        with open(config_file) as f:
            text = f.read()
    except OSError as exc:
        raise click.ClickException(f"cannot read config: {exc}")
    config = json.loads(text)
    for placeholder, key in PLACEHOLDERS.items():
        secret_file = config.get(key)
        if not secret_file or placeholder not in text:
            continue
        try:
            with open(secret_file) as f:
                value = f.read().strip()
        except OSError as exc:
            raise click.ClickException(f"cannot read {key} for {placeholder}: {exc}")
        text = text.replace(placeholder, json.dumps(value)[1:-1])
    config = json.loads(text)
    for repo in config.get("repositories", []):
        if repo.get("gitDir"):
            register_git_dir(repo["path"], repo["gitDir"])
    return config


def get_discord_webhook(config):
    webhook_file = config.get("discordWebhookFile")
    if webhook_file:
        try:
            with open(webhook_file) as f:
                return f.read().strip()
        except FileNotFoundError:
            pass
    return None
