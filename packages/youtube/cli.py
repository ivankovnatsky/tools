"""Argument parsing and CLI entry point for youtube."""

import os

import click
from client import Credentials
from commands import (
    cmd_api,
    cmd_auth_login,
    cmd_auth_status,
    cmd_categories,
    cmd_channel,
    cmd_likes_clear,
    cmd_likes_export,
    cmd_playlist,
    cmd_search,
    cmd_subscriptions_clear,
    cmd_subscriptions_export,
    cmd_trending,
    cmd_video,
)
from constants import (
    API_KEY_NAME,
    CATEGORIES,
    CATEGORY_ALIASES,
    CATEGORY_DOCS_URL,
    CHART_LIMIT,
    CLIENT_SECRET_NAME,
    SECRETS_DIR_ENV,
    default_token_dir,
)


def _output_format(*formats):
    return click.option(
        "--output-format",
        type=click.Choice(list(formats)),
        default=formats[0],
        show_default=True,
        help="Output format",
    )


_output_dir = click.option(
    "--output-dir",
    type=click.Path(file_okay=False),
    default=".",
    show_default=True,
    help="Directory for exported files and logs",
)

_dry_run = click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would be changed without making changes",
)

_yes = click.option(
    "-y", "--yes", is_flag=True, default=False, help="Skip the confirmation prompt"
)

_region = click.option(
    "--region",
    default="UA",
    show_default=True,
    callback=lambda ctx, param, value: value.upper() if value else value,
    help="ISO 3166-1 alpha-2 region code",
)


def _resolve_category(ctx, param, value):
    """Map a category name, alias or numeric ID to its numeric ID."""
    if not value:
        return value
    key = value.strip().lower()
    if key.isdigit():
        return key
    key = CATEGORY_ALIASES.get(key, key)
    if key in CATEGORIES:
        return CATEGORIES[key]
    raise click.BadParameter(
        f"unknown category {value!r}; use one of {', '.join(CATEGORIES)} "
        f"or a numeric ID ({CATEGORY_DOCS_URL})"
    )


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--secrets-dir",
    envvar=SECRETS_DIR_ENV,
    default=None,
    help=f"Directory holding {CLIENT_SECRET_NAME} and {API_KEY_NAME} "
    f"(env: {SECRETS_DIR_ENV})",
)
@click.option(
    "--client-secret-file",
    default=None,
    help=f"Google OAuth client JSON (Desktop app) (default: <secrets-dir>/{CLIENT_SECRET_NAME})",
)
@click.option(
    "--api-key-file",
    default=None,
    help="API key for public data; $YOUTUBE_API_KEY wins when set "
    f"(default: <secrets-dir>/{API_KEY_NAME})",
)
@click.option(
    "--token-dir",
    default=default_token_dir,
    show_default="$XDG_CONFIG_HOME/youtube",
    help="Where OAuth refresh tokens are kept",
)
@click.pass_context
def main(ctx, secrets_dir, client_secret_file, api_key_file, token_dir):
    """YouTube Data API v3 tool.

    Public commands (trending, search, video, channel, playlist, categories)
    use the API key when one is configured and fall back to the read-only
    OAuth token otherwise. Account commands always use OAuth.
    """
    if secrets_dir:
        client_secret_file = client_secret_file or os.path.join(
            secrets_dir, CLIENT_SECRET_NAME
        )
        api_key_file = api_key_file or os.path.join(secrets_dir, API_KEY_NAME)
    ctx.obj = Credentials(client_secret_file, api_key_file, token_dir)


@main.group("auth")
def auth_group():
    """Manage OAuth tokens."""


@auth_group.command("login")
@click.option(
    "--write",
    is_flag=True,
    default=False,
    help="Request the read/write scope (needed for `clear`)",
)
@click.pass_obj
def auth_login(creds, write):
    """Run the browser consent flow and save a refresh token."""
    cmd_auth_login(creds, write)


@auth_group.command("status")
@click.pass_obj
def auth_status(creds):
    """Show which credentials are present."""
    cmd_auth_status(creds)


@main.command("trending")
@_region
@click.option(
    "--category",
    default=None,
    callback=_resolve_category,
    help=f"Category name or ID: {', '.join(CATEGORIES)} (see `youtube categories`)",
)
@click.option(
    "--limit",
    type=click.IntRange(1, CHART_LIMIT),
    default=50,
    show_default=True,
    help="Number of videos",
)
@click.option(
    "--save-dir",
    type=click.Path(file_okay=False),
    default=None,
    help="Also write <DIR>/<REGION>/<date>.md and .json snapshots",
)
@_output_format("table", "md", "json")
@click.pass_obj
def trending(creds, region, category, limit, save_dir, output_format):
    """Most popular videos in a region (the chart behind the old Trending page)."""
    cmd_trending(creds, region, category, limit, save_dir, output_format)


@main.command("categories")
@_region
@click.option(
    "--lang", default="en", show_default=True, help="Language for category titles"
)
@_output_format("table", "json")
@click.pass_obj
def categories(creds, region, lang, output_format):
    """List video category IDs and the names `--category` accepts.

    IDs come from videoCategories.list:
    https://developers.google.com/youtube/v3/docs/videoCategories/list
    """
    cmd_categories(creds, region, lang, output_format)


@main.command("search")
@click.argument("query")
@click.option(
    "--type",
    "kind",
    type=click.Choice(["video", "channel", "playlist"]),
    default="video",
    show_default=True,
    help="Resource type to search",
)
@click.option(
    "--region",
    default=None,
    callback=lambda ctx, param, value: value.upper() if value else value,
    help="Bias results to a region",
)
@click.option(
    "--order",
    type=click.Choice(["relevance", "date", "viewCount", "rating"]),
    default="relevance",
    show_default=True,
    help="Result ordering",
)
@click.option(
    "--limit",
    type=click.IntRange(1, 50),
    default=20,
    show_default=True,
    help="Number of results",
)
@_output_format("table", "md", "json")
@click.pass_obj
def search(creds, query, kind, region, order, limit, output_format):
    """Search YouTube (costs 100 quota units per call)."""
    cmd_search(creds, query, kind, region, order, limit, output_format)


@main.command("video")
@click.argument("ids", nargs=-1, required=True)
@_output_format("table", "md", "json")
@click.pass_obj
def video(creds, ids, output_format):
    """Show details for one or more video IDs."""
    cmd_video(creds, ids, output_format)


@main.command("channel")
@click.argument("ref")
@_output_format("table", "json")
@click.pass_obj
def channel(creds, ref, output_format):
    """Show a channel by ID (UC…) or @handle."""
    cmd_channel(creds, ref, output_format)


@main.command("playlist")
@click.argument("playlist_id")
@click.option(
    "--limit", type=click.IntRange(min=1), default=None, help="Stop after N videos"
)
@_output_format("table", "md", "json")
@click.pass_obj
def playlist(creds, playlist_id, limit, output_format):
    """List videos in a playlist (a channel's uploads playlist works too)."""
    cmd_playlist(creds, playlist_id, limit, output_format)


@main.command("api")
@click.argument("endpoint")
@click.argument("params", nargs=-1)
@click.option(
    "--account",
    is_flag=True,
    default=False,
    help="Use the OAuth token even when an API key is set",
)
@click.pass_obj
def api(creds, endpoint, params, account):
    """GET any Data API endpoint, e.g. `youtube api videos part=snippet id=dQw4w9WgXcQ`."""
    cmd_api(creds, endpoint, params, account)


@main.group("subscriptions")
def subscriptions_group():
    """Your subscriptions."""


@subscriptions_group.command("export")
@_output_dir
@click.pass_obj
def subscriptions_export(creds, output_dir):
    """Write Subscriptions.md and Subscriptions.json."""
    cmd_subscriptions_export(creds, output_dir)


@subscriptions_group.command("clear")
@_output_dir
@_dry_run
@_yes
@click.pass_obj
def subscriptions_clear(creds, output_dir, dry_run, yes):
    """Unsubscribe from every channel, logging to Unsubscribed.md."""
    cmd_subscriptions_clear(creds, output_dir, dry_run, yes)


@main.group("likes")
def likes_group():
    """Your liked videos."""


@likes_group.command("export")
@_output_dir
@click.pass_obj
def likes_export(creds, output_dir):
    """Write Likes.md and Likes.json."""
    cmd_likes_export(creds, output_dir)


@likes_group.command("clear")
@_output_dir
@_dry_run
@_yes
@click.pass_obj
def likes_clear(creds, output_dir, dry_run, yes):
    """Remove every like, logging to Unliked.md (needs Likes.json in --output-dir)."""
    cmd_likes_clear(creds, output_dir, dry_run, yes)
