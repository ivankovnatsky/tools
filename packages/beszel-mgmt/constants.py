"""Constants for beszel-mgmt."""

import os


TOOLS_SECRETS_DIR = "~/.local/state/tools/secrets"
SOPS_NIX_SECRETS_DIR = "~/.config/sops-nix/secrets"


def secret_path(name: str) -> str:
    """The tools-rendered secret ``name`` when present, else the sops-nix one."""
    tools_path = f"{TOOLS_SECRETS_DIR}/{name}"
    if os.path.exists(os.path.expanduser(tools_path)):
        return tools_path
    return f"{SOPS_NIX_SECRETS_DIR}/{name}"


DEFAULT_EMAIL_PATH = secret_path("beszel-email")
DEFAULT_PASSWORD_PATH = secret_path("beszel-password")
DEFAULT_DISCORD_WEBHOOK_PATH = secret_path("discord-webhook-beszel")
