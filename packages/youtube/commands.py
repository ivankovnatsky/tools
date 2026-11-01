"""Command handlers for youtube CLI."""

import datetime as dt
import json
import os

import click
from auth import consent_flow, read_secret_file
from client import QuotaExceeded
from constants import (
    CATEGORY_NAMES,
    DAILY_QUOTA,
    DELETE_COST,
    READ_SCOPE,
    WRITE_SCOPE,
)
from display import (
    cell,
    count,
    day,
    duration,
    print_json,
    print_videos,
    truncate,
    video_markdown,
    video_row,
)


def _write(path: str, text: str) -> None:
    with open(path, "w") as f:
        f.write(text)


def _quota_exhausted() -> click.ClickException:
    return click.ClickException(
        "daily API quota exhausted. Re-run once it resets (midnight Pacific)."
    )


def cmd_auth_login(creds, write):
    """Run consent and save a refresh token."""
    scope, path = (
        (WRITE_SCOPE, creds.write_token) if write else (READ_SCOPE, creds.read_token)
    )
    consent_flow(creds.client_secret_file, scope, path)
    click.echo(f"Saved {path}", err=True)


def cmd_auth_status(creds):
    """Show which credentials are present."""
    key_source = (
        "$YOUTUBE_API_KEY" if os.environ.get("YOUTUBE_API_KEY") else creds.api_key_file
    )
    entries = [
        (
            "OAuth client",
            creds.client_secret_file,
            read_secret_file(creds.client_secret_file) is not None,
        ),
        ("read token", creds.read_token, os.path.exists(creds.read_token)),
        ("write token", creds.write_token, os.path.exists(creds.write_token)),
        ("API key", key_source, creds.api_key() is not None),
    ]
    for label, path, present in entries:
        click.echo(f"{'✓' if present else '✗'} {label:<13} {path}")


def cmd_trending(creds, region, category, limit, save_dir, output_format):
    """Most popular videos in a region."""
    client = creds.public()
    params = {
        "part": "snippet,contentDetails,statistics",
        "chart": "mostPopular",
        "regionCode": region,
        "maxResults": min(limit, 50),
    }
    if category:
        params["videoCategoryId"] = category
    try:
        rows = [video_row(v) for v in client.paginate("videos", limit=limit, **params)]
    except QuotaExceeded:
        raise _quota_exhausted()
    if not rows:
        raise click.ClickException(f"no popular videos returned for {region}")

    today = dt.date.today().isoformat()
    name = CATEGORY_NAMES.get(category, category)
    scope = f"category {name}" if category else "all categories"
    heading = f"YouTube trending — {region}"
    summary = f"Fetched {today} — top **{len(rows)}** videos, {scope}."
    print_videos(rows, output_format, heading, summary)

    if save_dir:
        target = os.path.join(os.path.expanduser(save_dir), region)
        os.makedirs(target, exist_ok=True)
        stem = os.path.join(target, f"{today}-{name}" if category else today)
        _write(f"{stem}.md", video_markdown(rows, heading, summary))
        _write(f"{stem}.json", json.dumps(rows, indent=2, ensure_ascii=False) + "\n")
        click.echo(f"Saved {stem}.md and {stem}.json", err=True)


def cmd_categories(creds, region, lang, output_format):
    """List assignable video categories in a region."""
    try:
        items = creds.public().get(
            "videoCategories", part="snippet", regionCode=region, hl=lang
        )
    except QuotaExceeded:
        raise _quota_exhausted()
    rows = [
        {
            "id": item["id"],
            "name": CATEGORY_NAMES.get(item["id"], ""),
            "title": item["snippet"]["title"],
        }
        for item in items.get("items", [])
        if item["snippet"].get("assignable")
    ]
    if output_format == "json":
        print_json(rows)
        return
    click.echo(f"{'ID':>3}  {'NAME':<14}  TITLE")
    for row in rows:
        click.echo(f"{row['id']:>3}  {row['name']:<14}  {row['title']}")


def cmd_search(creds, query, kind, region, order, limit, output_format):
    """Search videos, channels or playlists."""
    client = creds.public()
    params = {
        "part": "snippet",
        "q": query,
        "type": kind,
        "order": order,
        "maxResults": limit,
    }
    if region:
        params["regionCode"] = region
    try:
        items = client.get("search", **params).get("items", [])
        if kind == "video":
            ids = [item["id"]["videoId"] for item in items]
            videos = client.videos(ids)
    except QuotaExceeded:
        raise _quota_exhausted()

    if kind == "video":
        rows = [video_row(videos[i]) for i in ids if i in videos]
        print_videos(
            rows,
            output_format,
            f"YouTube search — {query}",
            f"Top {len(rows)} results by {order}.",
        )
        return

    id_key = "channelId" if kind == "channel" else "playlistId"
    rows = [
        {
            "id": item["id"][id_key],
            "title": item["snippet"]["title"],
            "channel": item["snippet"]["channelTitle"],
        }
        for item in items
    ]
    if output_format == "json":
        print_json(rows)
        return
    for row in rows:
        click.echo(f"{row['id']}  {truncate(row['title'], 60):<60}  {row['channel']}")


def cmd_video(creds, ids, output_format):
    """Show details for video IDs."""
    try:
        videos = creds.public().videos(list(ids))
    except QuotaExceeded:
        raise _quota_exhausted()
    rows = [video_row(videos[i]) for i in ids if i in videos]
    if not rows:
        raise click.ClickException("no videos found")
    print_videos(rows, output_format, "YouTube videos", f"{len(rows)} videos.")


def cmd_channel(creds, ref, output_format):
    """Show a channel by ID or @handle."""
    params = {"part": "snippet,statistics,contentDetails"}
    params["forHandle" if ref.startswith("@") else "id"] = ref
    try:
        items = creds.public().get("channels", **params).get("items", [])
    except QuotaExceeded:
        raise _quota_exhausted()
    if not items:
        raise click.ClickException(f"no channel found for {ref}")
    item = items[0]
    if output_format == "json":
        print_json(item)
        return
    info, stats = item["snippet"], item.get("statistics", {})
    subscribers = (
        "hidden"
        if stats.get("hiddenSubscriberCount")
        else count(stats.get("subscriberCount"))
    )
    uploads = (
        item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")
    )
    click.echo(f"{info['title']}  {info.get('customUrl', '')}")
    click.echo(f"  URL:         https://www.youtube.com/channel/{item['id']}")
    click.echo(f"  Country:     {info.get('country', '')}")
    click.echo(f"  Created:     {day(info.get('publishedAt'))}")
    click.echo(f"  Subscribers: {subscribers}")
    click.echo(f"  Videos:      {count(stats.get('videoCount'))}")
    click.echo(f"  Views:       {count(stats.get('viewCount'))}")
    click.echo(f"  Uploads:     {uploads}")


def cmd_playlist(creds, playlist_id, limit, output_format):
    """List videos in a playlist."""
    client = creds.public()
    try:
        items = client.paginate(
            "playlistItems",
            limit=limit,
            part="contentDetails",
            playlistId=playlist_id,
            maxResults=50,
        )
        ids = [item["contentDetails"]["videoId"] for item in items]
        videos = client.videos(ids)
    except QuotaExceeded:
        raise _quota_exhausted()
    rows = [video_row(videos[i]) for i in ids if i in videos]
    print_videos(
        rows, output_format, f"YouTube playlist — {playlist_id}", f"{len(rows)} videos."
    )


def cmd_api(creds, endpoint, params, account):
    """GET any Data API endpoint and print the raw JSON."""
    pairs = {}
    for param in params:
        key, sep, value = param.partition("=")
        if not sep:
            raise click.BadParameter(f"expected key=value, got {param!r}")
        pairs[key] = value
    client = creds.account() if account else creds.public()
    try:
        print_json(client.get(endpoint, **pairs))
    except QuotaExceeded:
        raise _quota_exhausted()


def cmd_subscriptions_export(creds, output_dir):
    """Write Subscriptions.md and Subscriptions.json."""
    client = creds.account()
    try:
        subs = list(
            client.paginate(
                "subscriptions",
                part="snippet,contentDetails",
                mine="true",
                maxResults=50,
                order="alphabetical",
            )
        )
        if not subs:
            raise click.ClickException("no subscriptions returned")

        # Enrich with handle + public stats, which subscriptions.list does not carry.
        ids = [s["snippet"]["resourceId"]["channelId"] for s in subs]
        channels: dict[str, dict] = {}
        for start in range(0, len(ids), 50):
            for item in client.get(
                "channels",
                part="snippet,statistics",
                id=",".join(ids[start : start + 50]),
                maxResults=50,
            ).get("items", []):
                channels[item["id"]] = item
    except QuotaExceeded:
        raise _quota_exhausted()

    rows = []
    for sub in subs:
        snippet = sub["snippet"]
        channel_id = snippet["resourceId"]["channelId"]
        channel = channels.get(channel_id, {})
        stats = channel.get("statistics", {})
        rows.append(
            {
                "title": snippet["title"],
                "channel_id": channel_id,
                "handle": channel.get("snippet", {}).get("customUrl", ""),
                "subscribed": day(snippet.get("publishedAt")),
                "videos": stats.get("videoCount"),
                "subscribers": None
                if stats.get("hiddenSubscriberCount")
                else stats.get("subscriberCount"),
                "description": snippet.get("description", ""),
            }
        )

    lines = [
        "# YouTube subscriptions",
        "",
        f"Exported {dt.date.today().isoformat()} — **{len(rows)} channels**, ordered alphabetically.",
        "",
        "| Channel | Handle | Subscribed | Videos | Subscribers |",
        "|---|---|---|---:|---:|",
    ]
    for row in rows:
        url = f"https://www.youtube.com/channel/{row['channel_id']}"
        handle = (
            f"[{cell(row['handle'])}](https://www.youtube.com/{row['handle']})"
            if row["handle"]
            else ""
        )
        lines.append(
            f"| [{cell(row['title'])}]({url}) | {handle} | {row['subscribed']} "
            f"| {count(row['videos'])} | {count(row['subscribers'])} |"
        )
    lines.append("")

    os.makedirs(output_dir, exist_ok=True)
    _write(os.path.join(output_dir, "Subscriptions.md"), "\n".join(lines))
    _write(
        os.path.join(output_dir, "Subscriptions.json"),
        json.dumps(rows, indent=2, ensure_ascii=False) + "\n",
    )
    click.echo(
        f"Wrote {len(rows)} subscriptions to {output_dir}/Subscriptions.md and .json",
        err=True,
    )


def cmd_subscriptions_clear(creds, output_dir, dry_run, yes):
    """Unsubscribe from every channel, logging to Unsubscribed.md."""
    backup = os.path.join(output_dir, "Subscriptions.json")
    if not os.path.exists(backup):
        raise click.ClickException(
            f"{backup} missing — run `youtube subscriptions export` first; it is the only backup."
        )

    client = creds.account(write=True)
    try:
        subs = [
            {"id": item["id"], "title": item["snippet"]["title"]}
            for item in client.paginate(
                "subscriptions",
                part="snippet",
                mine="true",
                maxResults=50,
                order="alphabetical",
            )
        ]
    except QuotaExceeded:
        raise _quota_exhausted()

    if not subs:
        click.echo("No subscriptions left — nothing to do.", err=True)
        return

    budget = DAILY_QUOTA // DELETE_COST
    runs = -(-len(subs) // budget)
    click.echo(
        f"{len(subs)} subscriptions to remove; quota allows {budget} per day ({runs} run(s)).",
        err=True,
    )
    if dry_run:
        click.echo("Dry-run complete - no changes made.", err=True)
        return
    if not yes:
        click.confirm("Unsubscribe from all of them?", abort=True)

    os.makedirs(output_dir, exist_ok=True)
    log = os.path.join(output_dir, "Unsubscribed.md")
    if not os.path.exists(log):
        _write(log, "# Unsubscribed channels\n\n")

    removed = failed = 0
    with open(log, "a") as handle:
        for sub in subs:
            try:
                client.delete("subscriptions", id=sub["id"])
            except QuotaExceeded:
                click.echo(f"\nQuota exhausted after {removed} removals.", err=True)
                break
            except click.ClickException as e:
                failed += 1
                click.echo(f"Skipped {sub['title']}: {e.message}", err=True)
                continue
            removed += 1
            handle.write(f"- {dt.date.today().isoformat()} — {cell(sub['title'])}\n")
            handle.flush()
            click.echo(f"[{removed}/{len(subs)}] {sub['title']}")

    click.echo(
        f"Removed {removed}, failed {failed}, {len(subs) - removed} remaining. Logged to {log}.",
        err=True,
    )


def cmd_likes_export(creds, output_dir):
    """Write Likes.md and Likes.json."""
    client = creds.account()
    try:
        items = list(
            client.paginate(
                "playlistItems",
                part="snippet,contentDetails",
                playlistId="LL",
                maxResults=50,
            )
        )
        if not items:
            raise click.ClickException("no liked videos returned")
        videos = client.videos([item["contentDetails"]["videoId"] for item in items])
    except QuotaExceeded:
        raise _quota_exhausted()

    rows = []
    for item in items:
        snippet = item["snippet"]
        video_id = item["contentDetails"]["videoId"]
        video = videos.get(video_id, {})
        info = video.get("snippet", {})
        stats = video.get("statistics", {})
        rows.append(
            {
                "title": info.get("title") or snippet.get("title", ""),
                "video_id": video_id,
                "channel": info.get("channelTitle")
                or snippet.get("videoOwnerChannelTitle", ""),
                "channel_id": info.get("channelId")
                or snippet.get("videoOwnerChannelId", ""),
                "liked": day(snippet.get("publishedAt")),
                "published": day(info.get("publishedAt")),
                "duration": duration(video.get("contentDetails", {}).get("duration")),
                "views": stats.get("viewCount"),
                # Videos pulled private or deleted since you liked them have no
                # videos.list entry, only the stub title in the playlist item.
                "available": video_id in videos,
                "description": info.get("description", ""),
            }
        )

    gone = [row for row in rows if not row["available"]]
    lines = [
        "# YouTube liked videos",
        "",
        f"Exported {dt.date.today().isoformat()} — **{len(rows)} videos**, newest like first.",
        "",
    ]
    if gone:
        lines += [
            f"{len(gone)} are no longer available (deleted or private) and are marked ⚠️.",
            "",
        ]
    lines += [
        "| Video | Channel | Liked | Published | Length | Views |",
        "|---|---|---|---|---:|---:|",
    ]
    for row in rows:
        mark = "" if row["available"] else "⚠️ "
        video_url = f"https://www.youtube.com/watch?v={row['video_id']}"
        channel = (
            f"[{cell(row['channel'])}](https://www.youtube.com/channel/{row['channel_id']})"
            if row["channel_id"]
            else cell(row["channel"])
        )
        lines.append(
            f"| {mark}[{cell(row['title'])}]({video_url}) | {channel} | {row['liked']} "
            f"| {row['published']} | {row['duration']} | {count(row['views'])} |"
        )
    lines.append("")

    os.makedirs(output_dir, exist_ok=True)
    _write(os.path.join(output_dir, "Likes.md"), "\n".join(lines))
    _write(
        os.path.join(output_dir, "Likes.json"),
        json.dumps(rows, indent=2, ensure_ascii=False) + "\n",
    )
    click.echo(
        f"Wrote {len(rows)} liked videos ({len(gone)} unavailable) to {output_dir}/Likes.md and .json",
        err=True,
    )


def cmd_likes_clear(creds, output_dir, dry_run, yes):
    """Remove every like, logging to Unliked.md."""
    backup = os.path.join(output_dir, "Likes.json")
    if not os.path.exists(backup):
        raise click.ClickException(
            f"{backup} missing — run `youtube likes export` first; it is the only backup."
        )

    client = creds.account(write=True)
    try:
        liked = [
            {
                "id": item["contentDetails"]["videoId"],
                "title": item["snippet"].get("title", ""),
            }
            for item in client.paginate(
                "playlistItems",
                part="snippet,contentDetails",
                playlistId="LL",
                maxResults=50,
            )
        ]
    except QuotaExceeded:
        raise _quota_exhausted()

    if not liked:
        click.echo("No liked videos left — nothing to do.", err=True)
        return

    budget = DAILY_QUOTA // DELETE_COST
    runs = -(-len(liked) // budget)
    click.echo(
        f"{len(liked)} liked videos to unlike; quota allows {budget} per day ({runs} run(s)).",
        err=True,
    )
    if dry_run:
        click.echo("Dry-run complete - no changes made.", err=True)
        return
    if not yes:
        click.confirm("Unlike all of them?", abort=True)

    log = os.path.join(output_dir, "Unliked.md")
    if not os.path.exists(log):
        _write(log, "# Unliked videos\n\n")

    removed = failed = 0
    with open(log, "a") as handle:
        for video in liked:
            try:
                client.post("videos/rate", id=video["id"], rating="none")
            except QuotaExceeded:
                click.echo(f"\nQuota exhausted after {removed} unlikes.", err=True)
                break
            except click.ClickException as e:
                # Deleted/private videos stay in LL but can't be rated.
                failed += 1
                click.echo(f"Skipped {video['id']}: {e.message}", err=True)
                continue
            removed += 1
            handle.write(
                f"- {dt.date.today().isoformat()} — [{cell(video['title'])}]"
                f"(https://www.youtube.com/watch?v={video['id']})\n"
            )
            handle.flush()
            click.echo(f"[{removed}/{len(liked)}] {video['title']}")

    click.echo(
        f"Unliked {removed}, failed {failed}, {len(liked) - removed} remaining. Logged to {log}.",
        err=True,
    )
