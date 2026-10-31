#!/usr/bin/env python3
"""
Kubernetes context switcher with one isolated kubeconfig per cluster.

Each cluster lives in its own ~/.kube.<account>.<cluster-name>/config, so a
shell only ever sees the single cluster its KUBECONFIG points at. The account
name keeps similar cluster names apart across accounts.

Under fish shell:
    eval (k8s-context)                       # interactive mode
    eval (k8s-context account.cluster-name)  # direct mode
    k8s-context generate                     # write configs for all discovered clusters
    k8s-context generate --prune             # also remove generated configs no longer found
    k8s-context generate -p some-profile     # only scan the given AWS profile(s)

generate discovers EKS clusters in every AWS account reachable through the
configured AWS profiles, using the first profile (by name) per account and
that profile's default region: the aws-sso DefaultRegion for the role, the
profile's configured region, AWS_REGION / AWS_DEFAULT_REGION, then the
aws-sso instance or global DefaultRegion. <account> is the aws-sso account
name when aws-sso knows it, otherwise the account ID. Both parts are
lowercased.
"""

import csv
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import click
import yaml

HOME = Path.home()
KUBE_DIR_PREFIX = ".kube."
# Each part of the ~/.kube.<account>.<cluster-name> path; no dots, so the
# account and cluster name stay unambiguous
NAME_PART_RE = re.compile(r"^[a-z0-9_-]+$")


@dataclass(frozen=True)
class Cluster:
    profile: str
    region: str
    name: str
    account: str

    @property
    def config_name(self) -> str:
        return f"{self.account}.{self.name.lower()}"


def log(msg: str) -> None:
    """Print message to stderr, keeping stdout eval-able."""
    click.echo(msg, err=True)


def fail(msg: str) -> None:
    log(f"Error: {msg}")
    sys.exit(1)


def kube_dir(name: str) -> Path:
    return HOME / f"{KUBE_DIR_PREFIX}{name}"


def existing_clusters() -> list[str]:
    """Find all config names with a ~/.kube.<name>/config file."""
    return sorted(
        path.name.removeprefix(KUBE_DIR_PREFIX)
        for path in HOME.glob(f"{KUBE_DIR_PREFIX}*")
        if (path / "config").is_file()
    )


def to_name_part(value: str) -> str:
    """Lowercase and replace anything outside [a-z0-9_-] with '-'."""
    return re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-")


def aws(profile: str | None, *args: str) -> subprocess.CompletedProcess:
    """Run an aws CLI call, never letting it see KUBECONFIG."""
    cmd = ["aws", *args, "--output", "text"]
    if profile:
        cmd += ["--profile", profile]
    env = {k: v for k, v in os.environ.items() if k != "KUBECONFIG"}
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def aws_profiles() -> list[str]:
    result = aws(None, "configure", "list-profiles")
    if result.returncode != 0:
        fail(f"cannot list AWS profiles: {result.stderr.strip()}")
    return sorted(set(result.stdout.split()))


@cache
def aws_sso_profiles() -> dict[str, tuple[str, str]]:
    """Map profile to its aws-sso (SSO instance, DefaultRegion)."""
    if not shutil.which("aws-sso"):
        return {}
    result = subprocess.run(
        ["aws-sso", "list", "--csv", "Profile", "SSO", "DefaultRegion"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {}
    return {
        row[0]: (row[1], row[2])
        for row in csv.reader(result.stdout.splitlines())
        if len(row) == 3
    }


@cache
def aws_sso_config() -> dict:
    path = Path(os.environ.get("AWS_SSO_CONFIG") or HOME / ".aws-sso" / "config.yaml")
    try:
        config = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return {}
    return config if isinstance(config, dict) else {}


@cache
def aws_sso_account_names() -> dict[str, str]:
    """Map account ID to its aws-sso name, empty when aws-sso is unavailable."""
    if not shutil.which("aws-sso"):
        return {}
    result = subprocess.run(
        ["aws-sso", "list", "--csv", "AccountId", "AccountName", "AccountAlias"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {}
    names = {}
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) != 3:
            continue
        account_id, configured_name, sso_name = row
        # AccountName is the user's local override, AccountAlias the SSO name
        if name := configured_name or sso_name:
            names[account_id.zfill(12)] = name
    return names


@cache
def aws_sso_login() -> bool:
    """Run `aws-sso login` at most once per invocation; True on success.

    Interactive (it opens a browser), so its output goes to stderr to keep
    stdout eval-able.
    """
    if not shutil.which("aws-sso"):
        return False
    log("aws-sso session expired, running aws-sso login")
    if subprocess.run(["aws-sso", "login"], stdout=sys.stderr).returncode != 0:
        return False
    # aws-sso list results may have been cached before the login.
    aws_sso_account_names.cache_clear()
    aws_sso_profiles.cache_clear()
    return True


@cache
def account_id(profile: str) -> str | None:
    result = aws(profile, "sts", "get-caller-identity", "--query", "Account")
    # aws-sso's credential_process says so when its token expired; log in and
    # retry once instead of failing every profile.
    if result.returncode != 0 and "aws-sso login" in result.stderr and aws_sso_login():
        result = aws(profile, "sts", "get-caller-identity", "--query", "Account")
    account = result.stdout.strip()
    return account if result.returncode == 0 and account else None


def account_name(account: str) -> str:
    """Stable, path-safe account name: the aws-sso name, else the account ID."""
    return to_name_part(aws_sso_account_names().get(account, account)) or account


@cache
def profile_region(profile: str) -> str | None:
    """The profile's default region, None if none is configured anywhere."""
    sso, sso_region = aws_sso_profiles().get(profile, ("", ""))
    sso_instance = aws_sso_config().get("SSOConfig", {}).get(sso) or {}
    candidates = [
        sso_region,
        aws(profile, "configure", "get", "region").stdout.strip(),
        os.environ.get("AWS_REGION"),
        os.environ.get("AWS_DEFAULT_REGION"),
        sso_instance.get("DefaultRegion") if sso else None,
        aws_sso_config().get("DefaultRegion") if sso else None,
    ]
    return next((region for region in candidates if region), None)


def list_clusters(profile: str, region: str) -> list[str] | None:
    """EKS cluster names in a region, None if they can't be listed."""
    result = aws(
        profile, "eks", "list-clusters", "--region", region, "--query", "clusters[]"
    )
    if result.returncode != 0:
        log(result.stderr.strip())
        return None
    return sorted(result.stdout.split())


def is_generated(config_name: str) -> bool:
    """Whether ~/.kube.<config-name> holds only a config written by generate.

    generate sets the context to the directory name and authenticates with
    'aws eks get-token', so hand-made configs and directories with anything
    besides the config file are never treated as generated.
    """
    target_dir = kube_dir(config_name)
    config = target_dir / "config"
    if target_dir.is_symlink() or config.is_symlink():
        return False
    if [entry.name for entry in target_dir.iterdir()] != ["config"]:
        return False
    lines = {line.strip() for line in config.read_text().splitlines()}
    return {
        f"current-context: {config_name}",
        "command: aws",
        "- eks",
        "- get-token",
    } <= lines


def write_config(cluster: Cluster) -> bool:
    """Write a fresh kubeconfig to a temp dir, then swap it in atomically.

    Nothing under ~/.kube.<config-name> is created or changed if aws fails.
    """
    target_dir = kube_dir(cluster.config_name)

    # Temp dir in HOME keeps the final rename on the same filesystem
    with tempfile.TemporaryDirectory(prefix=".k8s-context-generate.", dir=HOME) as tmp:
        tmp_config = Path(tmp) / "config"
        result = aws(
            cluster.profile,
            "eks",
            "update-kubeconfig",
            "--kubeconfig",
            str(tmp_config),
            "--region",
            cluster.region,
            "--name",
            cluster.name,
            "--alias",
            cluster.config_name,
        )
        if result.returncode != 0 or not tmp_config.is_file():
            log(result.stderr.strip())
            return False
        tmp_config.chmod(0o600)
        target_dir.mkdir(mode=0o700, exist_ok=True)
        target_dir.chmod(0o700)
        tmp_config.replace(target_dir / "config")
    return True


class DefaultGroup(click.Group):
    """Route anything that isn't a subcommand to the default 'use' command."""

    def parse_args(self, ctx, args):
        if not args or (args[0] not in self.commands and not args[0].startswith("-")):
            args = ["use", *args]
        return super().parse_args(ctx, args)


@click.group(cls=DefaultGroup)
def cli():
    """Switch between isolated per-cluster kubeconfigs."""


@cli.command(hidden=True)
@click.argument("cluster_name", required=False)
def use(cluster_name):
    """Print the KUBECONFIG export for a cluster (fzf when no name given)."""
    configs = existing_clusters()
    if not configs:
        fail("No Kubernetes configs found in ~/.kube.*/ directories")

    if cluster_name:
        if cluster_name not in configs:
            fail(f"Cluster '{cluster_name}' not found")
        selected = cluster_name
    else:
        if not shutil.which("fzf"):
            fail("fzf is not installed")
        result = subprocess.run(
            [
                "fzf",
                "--height",
                "40%",
                "--border",
                "--prompt=Select Kubernetes Cluster > ",
            ],
            input="\n".join(configs),
            stdout=subprocess.PIPE,
            text=True,
        )
        selected = result.stdout.strip()

    if selected:
        click.echo(f"export KUBECONFIG='{kube_dir(selected) / 'config'}'")


def discover_clusters(profiles: list[str]) -> tuple[list[Cluster], set[str], bool]:
    """Find clusters across all accounts reachable through the profiles.

    Returns the clusters, the names of every fully scanned account, and
    whether anything failed along the way.
    """
    failed = False
    accounts: dict[str, str] = {}  # account ID -> first profile reaching it
    for profile in profiles:
        account = account_id(profile)
        if account is None:
            log(f"failed  profile {profile} (cannot get caller identity)")
            failed = True
        elif account in accounts:
            log(f"skipped profile {profile} (same account as {accounts[account]})")
        else:
            accounts[account] = profile

    clusters: list[Cluster] = []
    scanned: set[str] = set()
    for account, profile in accounts.items():
        name = account_name(account)
        region = profile_region(profile)
        if region is None:
            log(f"failed  account {name} (no default region for profile {profile})")
            failed = True
            continue
        names = list_clusters(profile, region)
        if names is None:
            log(f"failed  account {name} region {region} (cannot list clusters)")
            failed = True
            continue
        scanned.add(name)
        clusters += [Cluster(profile, region, cluster, name) for cluster in names]

    return clusters, scanned, failed


@cli.command()
@click.option(
    "-p",
    "--profile",
    "profiles",
    multiple=True,
    help="AWS profile to scan, repeatable. Default: all configured profiles.",
)
@click.option(
    "--prune",
    is_flag=True,
    help="Remove generated configs of clusters no longer found.",
)
def generate(profiles, prune):
    """Write ~/.kube.<account>.<cluster-name>/config for every discovered cluster.

    Only ever writes those files: never touches ~/.kube/config and keeps the
    existing file on failure. Configs of clusters that are no longer found are
    left untouched unless --prune is given, and even then only ones generate
    wrote for scanned accounts are removed, and only when nothing failed.
    """
    if not shutil.which("aws"):
        fail("aws is not installed")

    all_profiles = aws_profiles()
    for profile in profiles:
        if profile not in all_profiles:
            fail(f"AWS profile '{profile}' not found")
    clusters, scanned, failed = discover_clusters(sorted(set(profiles)) or all_profiles)

    # Check every name before writing anything, so duplicates abort the run
    named: dict[str, Cluster] = {}
    for cluster in clusters:
        if not NAME_PART_RE.match(cluster.name.lower()):
            log(f"failed  {cluster.config_name} (unsupported cluster name)")
            failed = True
            continue
        if cluster.config_name in named:
            other = named[cluster.config_name]
            fail(
                f"duplicate cluster '{cluster.config_name}' "
                f"({other.region} and {cluster.region})"
            )
        named[cluster.config_name] = cluster

    if not named and not failed:
        log("No EKS clusters found")

    for config_name, cluster in named.items():
        if write_config(cluster):
            log(f"ok      {config_name} -> {kube_dir(config_name) / 'config'}")
        else:
            log(f"failed  {config_name} (existing config left unchanged)")
            failed = True

    for name in existing_clusters():
        if name in named:
            continue
        if not prune:
            log(f"skipped {name} (not found, left untouched)")
        elif failed:
            log(f"skipped {name} (not pruned, something failed)")
        elif name.split(".", 1)[0] not in scanned:
            log(f"skipped {name} (account not scanned, left untouched)")
        elif not is_generated(name):
            log(
                f"skipped {name} (not generated by k8s-context or holds other files, left untouched)"
            )
        else:
            (kube_dir(name) / "config").unlink()
            kube_dir(name).rmdir()
            log(f"pruned  {name}")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    cli(prog_name="k8s-context")
