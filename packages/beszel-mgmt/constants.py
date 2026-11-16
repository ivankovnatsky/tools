"""Constants for beszel-mgmt."""

import os


def default_secrets_dir() -> str:
    """tools-rendered secrets when present, else the sops-nix ones."""
    tools_dir = "~/.local/state/tools/secrets"
    if os.path.isdir(os.path.expanduser(tools_dir)):
        return tools_dir
    return "~/.config/sops-nix/secrets"


DEFAULT_SECRETS_PATH = default_secrets_dir()
DEFAULT_EMAIL_PATH = f"{DEFAULT_SECRETS_PATH}/beszel-email"
DEFAULT_PASSWORD_PATH = f"{DEFAULT_SECRETS_PATH}/beszel-password"
DEFAULT_DISCORD_WEBHOOK_PATH = f"{DEFAULT_SECRETS_PATH}/discord-webhook-beszel"
