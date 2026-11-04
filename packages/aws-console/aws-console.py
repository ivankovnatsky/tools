#!/usr/bin/env python3

"""Open an AWS SSO console for a profile in the default browser.

Reads ~/.aws/config for SSO profiles (anything whose credential_process line
contains an iam::<acct>:role/<role> ARN) and opens the console through
`aws-sso console`, so the session length follows ConsoleDuration in
~/.aws-sso/config.yaml.

The console opens in --region, else DefaultRegion from ~/.aws-sso/config.yaml.
Without either, AWS picks the region (the account's default Region setting or
the last one used).

With no flags it shows an fzf picker. --account / --role narrow the candidates;
when they leave exactly one match the picker is skipped (non-interactive use).

--destination signs in and lands on a console page instead of the home page:
it swaps the Destination of the federation URL aws-sso prints.
"""

import configparser
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

import click

AWS_CONFIG = Path.home() / ".aws" / "config"
AWS_SSO_CONFIG = Path.home() / ".aws-sso" / "config.yaml"
ARN_RE = re.compile(r"arn:aws:iam::(\d+):role/(\S+)")
DEFAULT_REGION_RE = re.compile(r"^\s*DefaultRegion:\s*(\S+)", re.MULTILINE)


def load_region(region: str | None) -> str | None:
    if region:
        return region
    if AWS_SSO_CONFIG.is_file():
        m = DEFAULT_REGION_RE.search(AWS_SSO_CONFIG.read_text())
        if m:
            return m.group(1)
    return None


def load_profiles() -> list[tuple[str, str, str]]:
    if not AWS_CONFIG.is_file():
        raise click.ClickException(f"{AWS_CONFIG} not found")

    parser = configparser.RawConfigParser()
    parser.read(AWS_CONFIG)

    entries: list[tuple[str, str, str]] = []
    for section in parser.sections():
        if not section.startswith("profile "):
            continue
        name = section[len("profile ") :]
        cred = parser[section].get("credential_process", "")
        m = ARN_RE.search(cred)
        if not m:
            continue
        entries.append((name, m.group(1), m.group(2)))
    return entries


def pick(entries: list[tuple[str, str, str]]) -> tuple[str, str, str]:
    width = max(len(e[0]) for e in entries)
    lines = [f"{name:<{width}}  {acct}  {role}" for name, acct, role in entries]
    fzf = subprocess.run(
        ["fzf", "--height=60%", "--border", "--prompt=AWS account > "],
        input="\n".join(lines),
        text=True,
        capture_output=True,
    )
    if fzf.returncode in (1, 130):
        sys.exit(fzf.returncode)
    if fzf.returncode != 0:
        raise click.ClickException(f"fzf failed: {fzf.stderr.strip()}")
    idx = lines.index(fzf.stdout.rstrip("\n"))
    return entries[idx]


def deep_link(cmd: list[str], destination: str) -> str:
    """Return the aws-sso federation URL with Destination set to a page URL."""
    proc = subprocess.run(
        cmd + ["--url-action", "printurl"], capture_output=True, text=True
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise click.ClickException("aws-sso console failed")
    url = next(
        (w for w in (proc.stdout + proc.stderr).split() if "/federation?" in w),
        None,
    )
    if not url:
        raise click.ClickException("no federation URL in aws-sso output")
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query))
    query["Destination"] = destination
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def match(
    entries: list[tuple[str, str, str]], account: str | None, role: str | None
) -> list[tuple[str, str, str]]:
    """Filter entries by account (name or id) and role, case-insensitively."""
    out = entries
    if account:
        needle = account.lower()
        out = [e for e in out if needle in e[0].lower() or needle == e[1]]
    if role:
        needle = role.lower()
        out = [e for e in out if needle == e[2].lower()]
    return out


@click.command()
@click.option(
    "-a", "--account", help="Account name (substring) or 12-digit account id."
)
@click.option("-r", "--role", help="Exact SSO role name.")
@click.option(
    "-R",
    "--region",
    help="Console region (default: DefaultRegion in ~/.aws-sso/config.yaml).",
)
@click.option(
    "-p",
    "--print",
    "print_url",
    is_flag=True,
    help="Print the URL instead of opening it.",
)
@click.option(
    "-d",
    "--destination",
    metavar="URL",
    help="Console page to open after sign-in (e.g. a CloudWatch or Billing URL).",
)
def main(
    account: str | None,
    role: str | None,
    region: str | None,
    print_url: bool,
    destination: str | None,
) -> None:
    """Open the AWS SSO console for a selected profile."""
    entries = load_profiles()
    if not entries:
        raise click.ClickException(f"no SSO role profiles found in {AWS_CONFIG}")

    candidates = match(entries, account, role)
    if not candidates:
        raise click.ClickException(
            f"no profile matches account={account!r} role={role!r}"
        )
    if len(candidates) == 1:
        _, account_id, role_name = candidates[0]
    else:
        _, account_id, role_name = pick(candidates)

    cmd = ["aws-sso", "console", "--account", account_id, "--role", role_name]
    console_region = load_region(region)
    if console_region:
        cmd += ["--region", console_region]
    if destination:
        if not destination.startswith("https://"):
            raise click.ClickException("--destination must be an https:// URL")
        url = deep_link(cmd, destination)
        if print_url:
            click.echo(url)
        elif click.launch(url) != 0:
            raise click.ClickException("failed to open the browser")
        return
    cmd += ["--url-action", "print" if print_url else "open"]
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
