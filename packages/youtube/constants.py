"""Constants for youtube."""

import os

USER_AGENT = "youtube/1.0.0"


TOOLS_SECRETS_DIR = "~/.local/state/tools/secrets"
SOPS_NIX_SECRETS_DIR = "~/.config/sops-nix/secrets"


def secret_path(name: str) -> str:
    """The tools-rendered secret ``name`` when present, else the sops-nix one."""
    tools_path = f"{TOOLS_SECRETS_DIR}/{name}"
    if os.path.exists(os.path.expanduser(tools_path)):
        return tools_path
    return f"{SOPS_NIX_SECRETS_DIR}/{name}"


DEFAULT_CLIENT_SECRET_PATH = secret_path("youtube-client-secret")
DEFAULT_API_KEY_PATH = secret_path("youtube-api-key")


def default_token_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "youtube")


READ_TOKEN_NAME = "token.json"
WRITE_TOKEN_NAME = "token_write.json"

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/youtube/v3"

READ_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
WRITE_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"

CONSENT_TIMEOUT = 300

DAILY_QUOTA = 10_000
DELETE_COST = 50

# mostPopular stops paging after 200 results.
CHART_LIMIT = 200

# Category IDs are stable across regions; Google has no static table, only the API.
CATEGORY_DOCS_URL = "https://developers.google.com/youtube/v3/docs/videoCategories/list"
CATEGORIES = {
    "film": "1",
    "autos": "2",
    "music": "10",
    "pets": "15",
    "sports": "17",
    "travel": "19",
    "gaming": "20",
    "people": "22",
    "comedy": "23",
    "entertainment": "24",
    "news": "25",
    "howto": "26",
    "education": "27",
    "science": "28",
    "nonprofits": "29",
}
CATEGORY_ALIASES = {
    "blogs": "people",
    "politics": "news",
    "style": "howto",
    "tech": "science",
    "animation": "film",
    "vehicles": "autos",
    "animals": "pets",
    "activism": "nonprofits",
}
CATEGORY_NAMES = {v: k for k, v in CATEGORIES.items()}
