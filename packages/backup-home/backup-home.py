#!/usr/bin/env python3

"""
Primary backup script for Unix systems. Evolution journey:
1. Started with tar + rclone for cloud uploads
2. Explored standalone binaries for cross-platform support (Go, Rust, Python)
   - Go: Native but couldn't match scp speeds (5-38MB/s vs 109MB/s native)
   - Rust: librclone bindings lacked Windows support
   - Python: Too slow for large backups
3. Simplified to tar + ssh/scp with mini machine as single upload source

This bash version remains the best solution for Unix due to native scp speeds.
The Go version (github.com/ivankovnatsky/backup-home-go) exists for exploration
and Windows support, where a PowerShell script is used instead.

The purpose of this script is to only exclude directories to which user does
not have access in macOS and which contain some data that is not needed, like
VMs and so.

---

https://stackoverflow.com/a/984259

2026-01-09: Ported to Python for improved maintainability. Functionality unchanged.
2026-01-16: Removed scp support, miniserve is now the default upload method.
2026-01-16: Removed rclone support. Keep backup machine as single source of
            truth for sync with drive

---

Do not add file logging or redirect stderr. bsdtar on macOS writes both the -v
file listing and error messages (e.g. "Operation not permitted") to stderr.
Redirecting it to a log file hides errors from the caller and breaks failure
detection. Keep stderr going to the terminal for real-time visibility.
"""

import click
import os
import platform
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path


SOPS_SECRETS = {
    "MINISERVE_USER": "miniserve-username",
    "MINISERVE_PASS": "miniserve-password",
}


def _read_secret(name: str) -> str | None:
    """Read secret from sops-nix file."""
    sops_name = SOPS_SECRETS.get(name)
    if sops_name:
        sops_path = Path.home() / ".config/sops-nix/secrets" / sops_name
        try:
            return sops_path.read_text().strip()
        except OSError:
            pass
    return None


TAR_BACKENDS = ("bsdtar", "gnutar")

# Probed in order, each verified by --version: gnutar in PATH shadows Apple's
# tar, and bare "tar" is GNU on Linux, so neither name can be trusted.
BSDTAR_CANDIDATES = ("bsdtar", "/usr/bin/tar", "tar")
GNUTAR_CANDIDATES = ("gtar", "tar")

# Linux has no bsdtar; only macOS ships one worth defaulting to.
DEFAULT_TAR_BACKEND = "bsdtar" if platform.system() == "Darwin" else "gnutar"


def _tar_flavor(binary: str) -> str | None:
    """Return 'gnutar' or 'bsdtar' for a tar binary, None if it isn't one."""
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    version_output = result.stdout + result.stderr
    if "GNU tar" in version_output:
        return "gnutar"
    if "bsdtar" in version_output:
        return "bsdtar"
    return None


def resolve_tar(backend: str) -> str:
    """Find a tar binary matching the requested backend, or exit.

    2026-10-10, Ivans-MacBook-Air: bsdtar 3.5.3 (libarchive 3.7.4) abandons the
    traversal at the first unreadable file — one SIP-protected CoreSpeech plist
    was enough — and exits 1 reporting only "non-fatal errors". That produced
    514,839 entries / 19.5 GiB against GNU tar 1.35's 702,355 / 66 GiB on the
    same home, losing all of Pictures, Library/Messages, Mobile Documents,
    Keychains, .config and .gnupg. Excluding the offending file only moves the
    abort to the next unreadable one. Whole directories disappear without an
    error of their own because libarchive defers directory descent LIFO, so the
    directories listed first are still unvisited when it quits; their headers
    are already written from the listing pass. Hence the backend switch.
    """
    candidates = BSDTAR_CANDIDATES if backend == "bsdtar" else GNUTAR_CANDIDATES
    for candidate in candidates:
        if _tar_flavor(candidate) == backend:
            return candidate

    click.echo(
        f"Error: no {backend} binary found (tried: {', '.join(candidates)})", err=True
    )
    sys.exit(1)


# Defaults
DEFAULT_STORAGE_PATH = "/Volumes/Storage/Data"
DEFAULT_MINISERVE_URL = "http://192.168.50.6:8080"

# These globs are bsdtar-flavoured but behave identically under GNU tar, whose
# exclude defaults (--wildcards-match-slash, unanchored) make `**` and `*`
# equivalent, and every pattern here is anchored by a literal path component.
# Checked 2026-10-10 across both backends: same files excluded, no
# over-matching. GNU tar differs only in emitting an empty directory header for
# an excluded dir where bsdtar prunes it — no file content divergence.
EXCLUDE_PATTERNS: dict[str, list[str]] = {
    "runtime": [
        "**/.gnupg/S.*",
        "**/*.sock",
        "**/*.socket",
    ],
    "caches": [
        "**/.cache/huggingface/**",
        "**/.cache/nix/**",
        "**/.cache/uv/**",
        "**/.cache/whisper/**",
        "**/.cache/mozilla/**",
        "**/.cache/chromium/**",
        "**/.cache/go-build/**",
        "**/.cache/pypoetry/**",
        "**/Library/Caches/Google/Chrome/**",
        "**/Library/Caches/Firefox/**",
        "**/Library/Caches/Mozilla/**",
        "**/Library/Caches/Homebrew/**",
        "**/Library/Caches/bitwarden-updater/**",
        "**/Library/Caches/com.apple.Music/**",
        "**/Library/Caches/com.apple.callintelligenced/**",
        "**/Library/Caches/ms-playwright-go/**",
        "**/Library/Caches/go-build/**",
        "**/Library/Caches/pypoetry/**",
        "**/Library/Caches/typescript/**",
        "**/Library/Caches/Chromium/**",
        "**/Library/Caches/Vivaldi/**",
        "**/Library/Caches/Comet/**",
    ],
    "dev_deps": [
        "**/.cargo/registry/**",
        "**/.npm/**",
        "**/.npm-cache/**",
        "**/.bun/**",
        "**/.terraform.d/**",
        "**/.go/**",
        "**/go/**",
        "**/Library/Application Support/virtualenv/**",
        "**/Library/Biome/**",
        "**/Library/pnpm/**",
        "**/node_modules/**",
        "**/.terragrunt-cache/**",
    ],
    "large_blobs": [
        "**/.ollama/**",
        "**/.codeium/**",
        "**/OrbStack/**",
        "**/Group Containers/HUAQ24HBR6.dev.orbstack/**",
        "**/Library/Containers/com.utmapp.UTM/**",
        "**/.local/share/Steam/**",
        "**/.local/share/uv/**",
        "**/.local/share/baloo/**",
        "**/.local/share/flatpak/**",
        "**/.local/share/claude/**",
        "**/.local/share/lutris/**",
        "**/.velocidrone/**",
        "**/Library/Application Support/Claude/vm_bundles/**",
        "**/Library/Application Support/rancher-desktop/**",
        "**/Library/Application Support/Paradox Interactive/**",
        "**/Pictures/**",
    ],
    "app_data": [
        "**/Library/Application Support/Firefox/**",
        "**/Library/Application Support/Chromium/**",
        "**/Library/Application Support/Google/Chrome/**",
        "**/Library/Application Support/Vivaldi/**",
        "**/Library/Application Support/Cursor/**",
        "**/Library/Application Support/Code/**",
        "**/Library/Application Support/Windsurf/**",
        "**/Library/Application Support/Slack/**",
        "**/.cursor/extensions/**",
        "**/.vscode/**",
        "**/.vscode-oss/**",
    ],
    "os_system": [],
}

DEFAULT_EXCLUDE_CATEGORIES = [
    "runtime",
    "caches",
    "dev_deps",
    "large_blobs",
    "os_system",
]


def create_backup(
    archive_path: Path,
    user: str,
    tar_binary: str,
    backend: str,
    no_excludes: bool = False,
    exclude_categories: list[str] | None = None,
) -> bool:
    """Create tar.gz backup of home directory."""
    home_parent = Path.home().parent

    # Build tar command with exclusions
    cmd = [tar_binary]
    if not no_excludes:
        categories = (
            exclude_categories if exclude_categories else DEFAULT_EXCLUDE_CATEGORIES
        )
        for cat in categories:
            for pattern in EXCLUDE_PATTERNS[cat]:
                cmd.extend(["--exclude", pattern])
    cmd.append("--no-xattrs")
    if backend == "gnutar":
        # macOS always has files no user can read (SIP-protected Apple plists),
        # and GNU tar exits 2 on those. Without this every run would look like
        # a failure; with it, a non-zero exit again means something real.
        cmd.append("--ignore-failed-read")
    cmd.extend(["-cv", user])

    try:
        # Run tar and pipe to pigz (stderr=None lets tar -v output show on terminal)
        with open(archive_path, "wb") as archive_file:
            tar_proc = subprocess.Popen(
                cmd, cwd=home_parent, stdout=subprocess.PIPE, stderr=None
            )
            pigz_proc = subprocess.Popen(
                ["pigz"],
                stdin=tar_proc.stdout,
                stdout=archive_file,
                stderr=None,
            )

            # Allow tar to receive SIGPIPE if pigz exits
            if tar_proc.stdout:
                tar_proc.stdout.close()

            pigz_proc.wait()
            tar_proc.wait()

        if pigz_proc.returncode != 0:
            click.echo("Backup compression failed", err=True)
            return False

        # Exit codes mean different things per backend.
        #
        # bsdtar 1: it abandoned the traversal at the first unreadable file, so
        # the archive is TRUNCATED, not merely missing that file. Fatal — this
        # is the failure that cost 36% of a home on 2026-10-10 by being
        # reported as a warning and then uploaded.
        #
        # gnutar 1: "file changed as we read it" / "File shrank by N bytes".
        # Near-certain on a live home and not a truncation; unreadable files
        # are already absorbed by --ignore-failed-read. Warn and continue.
        if tar_proc.returncode != 0:
            if backend == "gnutar" and tar_proc.returncode == 1:
                click.echo(
                    "Warning: files changed while being read; the archive is a "
                    "slightly inconsistent snapshot of a live home",
                    err=True,
                )
            elif backend == "bsdtar" and tar_proc.returncode == 1:
                click.echo(
                    "Backup creation failed: bsdtar exited 1, which means it "
                    "stopped at the first unreadable file and the archive is "
                    "truncated. Re-run with --tar-backend gnutar.",
                    err=True,
                )
                return False
            else:
                click.echo("Backup creation failed", err=True)
                return False

        return True
    except Exception as e:
        click.echo(f"Error creating backup: {e}", err=True)
        return False


def upload_miniserve(
    archive_path: Path,
    url: str,
    hostname: str,
    home_parent_dir: str,
    user: str,
    password: str,
) -> bool:
    """Upload backup using curl to miniserve."""
    date_dir = datetime.now().strftime("%Y-%m-%d")
    upload_path = f"/backup/Machines/{hostname}/{home_parent_dir}/{date_dir}"

    click.echo(f"Creating directory: {upload_path}")

    # Create directories
    auth = f"{user}:{password}"
    for mkdir_path, mkdir_name in [
        ("/backup/Machines", hostname),
        (f"/backup/Machines/{hostname}", home_parent_dir),
        (f"/backup/Machines/{hostname}/{home_parent_dir}", date_dir),
    ]:
        subprocess.run(
            [
                "curl",
                "-s",
                "-u",
                auth,
                "-F",
                f"mkdir={mkdir_name}",
                f"{url}/upload?path={mkdir_path}",
            ],
            check=False,
        )

    click.echo(f"Uploading to miniserve: {url}{upload_path}")

    result = subprocess.run(
        [
            "curl",
            "-u",
            auth,
            "-F",
            f"path=@{archive_path}",
            "-o",
            "/dev/null",
            f"{url}/upload?path={upload_path}",
        ],
        check=False,
    )
    return result.returncode == 0


@click.command(
    name="backup-home",
    help="Backup home directory with automatic exclusions and remote upload.",
    epilog=(
        "Secrets (read from ~/.config/sops-nix/secrets/):\n\n"
        "  miniserve-username    Username for miniserve authentication\n\n"
        "  miniserve-password    Password for miniserve authentication\n\n"
        "Examples:\n\n"
        "  backup-home                              # Normal backup and upload via miniserve\n\n"
        "  backup-home --skip-upload                # Create backup locally only\n\n"
        "  backup-home --miniserve http://host:8080 # Upload via miniserve (custom URL)\n\n"
        "  backup-home --skip-backup --skip-upload  # Use existing backup, no upload"
    ),
)
@click.option(
    "--tar-backend",
    type=click.Choice(TAR_BACKENDS),
    default=DEFAULT_TAR_BACKEND,
    show_default=True,
    help="tar implementation to use (bsdtar truncates the archive at the "
    "first unreadable file on macOS)",
)
@click.option(
    "--no-excludes",
    is_flag=True,
    default=False,
    help="Disable all exclude patterns, backup everything",
)
@click.option(
    "--exclude-categories",
    type=str,
    default=None,
    help=(
        "Comma-separated list of exclude categories to apply "
        f"({', '.join(EXCLUDE_PATTERNS.keys())})"
    ),
)
@click.option(
    "--skip-backup",
    is_flag=True,
    default=False,
    help="Skip backup creation, use existing archive",
)
@click.option(
    "--skip-upload",
    is_flag=True,
    default=False,
    help="Skip upload to remote machine, keep backup local",
)
@click.option(
    "--delete-backup/--keep-backup",
    default=True,
    show_default=True,
    help="Delete local backup after successful upload",
)
@click.option(
    "--backup-path",
    type=str,
    default=None,
    help="Custom path for backup archive",
)
@click.option(
    "--miniserve",
    type=str,
    default=None,
    metavar="URL",
    help=f"Custom miniserve URL (default: {DEFAULT_MINISERVE_URL})",
)
def main(
    tar_backend,
    no_excludes,
    exclude_categories,
    skip_backup,
    skip_upload,
    delete_backup,
    backup_path,
    miniserve,
):
    tar_binary = resolve_tar(tar_backend)

    # Get environment info
    user = os.environ.get("USER", os.getlogin())
    hostname = socket.gethostname()
    home_parent_dir = Path.home().parent.name
    storage_path = os.environ.get("STORAGE_DATA_PATH", DEFAULT_STORAGE_PATH)

    # Determine archive path
    if backup_path:
        archive_path = Path(backup_path)
    elif Path(storage_path).is_dir() and os.access(storage_path, os.W_OK):
        temp_dir = Path(storage_path) / "Tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        archive_path = temp_dir / f"{user}.tar.gz"
    else:
        archive_path = Path("/tmp") / f"{user}.tar.gz"

    # Create or skip backup
    if skip_backup:
        if not archive_path.exists():
            click.echo(
                f"Error: --skip-backup specified but no backup file exists at {archive_path}",
                err=True,
            )
            sys.exit(1)
        click.echo(f"Skipping backup creation, using existing file: {archive_path}")
    else:
        exclude_cats = None
        if exclude_categories:
            exclude_cats = [c.strip() for c in exclude_categories.split(",")]
            invalid = [c for c in exclude_cats if c not in EXCLUDE_PATTERNS]
            if invalid:
                click.echo(
                    f"Error: unknown exclude categories: {', '.join(invalid)}. "
                    f"Valid: {', '.join(EXCLUDE_PATTERNS.keys())}",
                    err=True,
                )
                sys.exit(1)
        if not create_backup(
            archive_path, user, tar_binary, tar_backend, no_excludes, exclude_cats
        ):
            sys.exit(1)

    # Upload or skip
    if skip_upload:
        click.echo(f"Skipping upload, backup saved at: {archive_path}")
        sys.exit(0)

    miniserve_url = miniserve if miniserve else DEFAULT_MINISERVE_URL
    miniserve_user = _read_secret("MINISERVE_USER")
    miniserve_pass = _read_secret("MINISERVE_PASS")
    if not miniserve_user or not miniserve_pass:
        click.echo(
            "Error: miniserve secrets not found in ~/.config/sops-nix/secrets/",
            err=True,
        )
        sys.exit(1)
    success = upload_miniserve(
        archive_path,
        miniserve_url,
        hostname,
        home_parent_dir,
        miniserve_user,
        miniserve_pass,
    )

    if success:
        click.echo("Upload successful")
        if delete_backup:
            archive_path.unlink()
            click.echo("Local backup deleted")
        else:
            click.echo(f"Backup kept at: {archive_path}")
    else:
        click.echo(f"Upload failed, keeping local backup at: {archive_path}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main(prog_name="backup-home")
