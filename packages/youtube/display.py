"""Row shaping and table/markdown/json rendering."""

import json

import click


def cell(text: str | None) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def day(timestamp: str | None) -> str:
    return timestamp[:10] if timestamp else ""


def count(value) -> str:
    return "" if value is None else f"{int(value):,}"


def duration(iso: str | None) -> str:
    """PT1H2M3S -> 1:02:03."""
    if not iso or not iso.startswith("PT"):
        return ""
    parts = {"H": 0, "M": 0, "S": 0}
    number = ""
    for char in iso[2:]:
        if char.isdigit():
            number += char
        elif char in parts:
            parts[char] = int(number or 0)
            number = ""
    if parts["H"]:
        return f"{parts['H']}:{parts['M']:02d}:{parts['S']:02d}"
    return f"{parts['M']}:{parts['S']:02d}"


def truncate(text: str | None, width: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def video_row(video: dict) -> dict:
    info = video.get("snippet", {})
    stats = video.get("statistics", {})
    return {
        "video_id": video["id"],
        "title": info.get("title", ""),
        "channel": info.get("channelTitle", ""),
        "channel_id": info.get("channelId", ""),
        "published": day(info.get("publishedAt")),
        "duration": duration(video.get("contentDetails", {}).get("duration")),
        "views": stats.get("viewCount"),
        "likes": stats.get("likeCount"),
        "comments": stats.get("commentCount"),
        "category_id": info.get("categoryId", ""),
        "description": info.get("description", ""),
    }


def video_markdown(rows: list[dict], heading: str, summary: str) -> str:
    lines = [
        f"# {heading}",
        "",
        summary,
        "",
        "| # | Video | Channel | Published | Length | Views |",
        "|---:|---|---|---|---:|---:|",
    ]
    for rank, row in enumerate(rows, 1):
        video_url = f"https://www.youtube.com/watch?v={row['video_id']}"
        channel_url = f"https://www.youtube.com/channel/{row['channel_id']}"
        lines.append(
            f"| {rank} | [{cell(row['title'])}]({video_url}) "
            f"| [{cell(row['channel'])}]({channel_url}) | {row['published']} "
            f"| {row['duration']} | {count(row['views'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def video_table(rows: list[dict]) -> str:
    # Title goes last: wide glyphs (emoji, CJK) would misalign any column after it.
    header = (
        f"{'#':>3}  {'VIEWS':>13}  {'LENGTH':>8}  {'CHANNEL':<24}  {'URL':<28}  TITLE"
    )
    lines = [header]
    for rank, row in enumerate(rows, 1):
        url = f"https://youtu.be/{row['video_id']}"
        lines.append(
            f"{rank:>3}  {count(row['views']):>13}  {row['duration']:>8}  "
            f"{truncate(row['channel'], 24):<24}  {url:<28}  {truncate(row['title'], 80)}"
        )
    return "\n".join(lines)


def print_json(data) -> None:
    click.echo(json.dumps(data, indent=2, ensure_ascii=False))


def print_videos(
    rows: list[dict], output_format: str, heading: str, summary: str
) -> None:
    if output_format == "json":
        print_json(rows)
    elif output_format == "md":
        click.echo(video_markdown(rows, heading, summary))
    else:
        click.echo(video_table(rows))
