"""Git bundle as a remote, for repos shared through a file-sync folder.

iCloud Drive syncs plain files but corrupts `.git` directories, so a bundle
stands in for a git server: it is the repo's remote, fetch and ff-only pull
work unchanged, and "push" rewrites the file (only when the bundle tip is an
ancestor of the local branch). The file is written under the cache dir and
renamed into place, so the folder never syncs a half-written bundle.
"""

import errno
import glob
import os
import shutil
import subprocess
import tempfile

import click

from alerting import alert
from git_ops import commit_count, run_git

# sys/stat.h SF_DATALESS: file content lives only in iCloud
# (`find -flags +dataless`). Not in Python's stat module.
SF_DATALESS = 0x40000000

# Returned by bundle_state().
READY = "ready"
MISSING = "missing"
PLACEHOLDER = "placeholder"
CONFLICT = "conflict"
ERROR = "error"
NODIR = "dir missing"


def _conflict_copies(bundle_file):
    """iCloud resolves concurrent writes by keeping both files, the loser
    renamed `<stem> 2<ext>`. Git never sees those, so surface them."""
    stem, ext = os.path.splitext(bundle_file)
    return sorted(glob.glob(f"{glob.escape(stem)} [0-9]*{glob.escape(ext)}"))


def bundle_state(bundle_file, name, webhook_url, act=True):
    """Classify the bundle file on disk. With `act`, CONFLICT and ERROR
    alert and PLACEHOLDER (evicted by iCloud) requests a download."""
    copies = _conflict_copies(bundle_file)
    if copies:
        if act:
            alert(
                webhook_url,
                f"`{name}`: conflict copies next to bundle, merge or delete them — "
                + ", ".join(os.path.basename(c) for c in copies),
            )
        return CONFLICT

    try:
        st = os.stat(bundle_file)
    except FileNotFoundError:
        bundle_dir = os.path.dirname(bundle_file)
        if not os.path.isdir(bundle_dir):
            if not act:
                return NODIR
            try:
                os.makedirs(bundle_dir, exist_ok=True)
            except OSError as exc:
                alert(webhook_url, f"`{name}`: could not create {bundle_dir} — {exc}")
                return ERROR
        return MISSING
    except OSError as exc:
        if act:
            alert(webhook_url, f"`{name}`: cannot stat bundle — {exc}")
        return ERROR
    if getattr(st, "st_flags", 0) & SF_DATALESS:
        # Evicted by "Optimize Mac Storage": the path exists but reading it
        # blocks on a download. Ask for it back and retry next cycle.
        brctl = shutil.which("brctl")
        if act and brctl:
            click.echo(f"{name}: bundle evicted, requesting download", err=True)
            subprocess.run([brctl, "download", bundle_file], check=False)
        return PLACEHOLDER
    return READY


def bundle_tip(bundle_file, branch):
    """SHA of `branch` in the bundle, or None if the bundle is unreadable or
    does not carry that branch."""
    result = run_git(
        "bundle", "list-heads", bundle_file, f"refs/heads/{branch}", check=False
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        sha, _, ref = line.partition(" ")
        if ref == f"refs/heads/{branch}":
            return sha
    return None


def write_bundle(path, branch, bundle_file, name, webhook_url):
    """Rewrite the bundle from the local branch. Returns True on success."""
    cache_root = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    cache_dir = os.path.join(cache_root, "reposync")
    try:
        os.makedirs(cache_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=cache_dir, prefix=f"{os.path.basename(bundle_file)}.", suffix=".tmp"
        )
        os.close(fd)
    except OSError as exc:
        alert(webhook_url, f"`{name}`: could not prepare bundle temp file — {exc}")
        return False

    try:
        result = run_git(
            "bundle", "create", tmp, f"refs/heads/{branch}", cwd=path, check=False
        )
        if result.returncode != 0:
            alert(
                webhook_url,
                f"`{name}`: bundle create failed — {result.stderr.strip()}",
            )
            return False
        try:
            os.replace(tmp, bundle_file)
        except OSError as exc:
            hint = (
                " (bundle dir and cache dir must be on one volume)"
                if exc.errno == errno.EXDEV
                else ""
            )
            alert(
                webhook_url, f"`{name}`: could not move bundle into place{hint} — {exc}"
            )
            return False
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return True


def publish_bundle(
    path,
    branch,
    remote,
    bundle_file,
    local_sha,
    remote_sha,
    name,
    webhook_url,
    missing=False,
    force=False,
):
    """The push step for a bundle remote. `remote_sha` is the bundle tip
    (None when unknown). `missing` means the file is gone from disk and must
    be rewritten even when nothing new is to publish. `force` publishes even
    when local is behind or diverged from the bundle.

    Returns (ok, action) where action describes what happened, or None.
    """
    if remote_sha == local_sha and not missing:
        return True, None
    if not force and remote_sha is not None and remote_sha != local_sha:
        is_ancestor = run_git(
            "merge-base", "--is-ancestor", remote_sha, local_sha, cwd=path, check=False
        )
        if is_ancestor.returncode != 0:
            # Bundle has commits local lacks. Either local is simply behind
            # (the pull step already said why it did not apply) or the two
            # diverged; only the latter is news here.
            behind = run_git(
                "merge-base",
                "--is-ancestor",
                local_sha,
                remote_sha,
                cwd=path,
                check=False,
            )
            if behind.returncode == 0:
                click.echo(
                    f"{name}: skip publish ({branch} is behind the bundle)", err=True
                )
                return True, None
            alert(
                webhook_url,
                f"`{name}`: {branch} and bundle diverged — resolve manually",
            )
            return False, None

    if not write_bundle(path, branch, bundle_file, name, webhook_url):
        return False, None

    # Mirror what `git push` does: advance the tracking ref, so the next
    # cycle sees the bundle tip as already known and skips the fetch.
    tracked = run_git(
        "update-ref",
        f"refs/remotes/{remote}/{branch}",
        local_sha,
        cwd=path,
        check=False,
    )
    if tracked.returncode != 0:
        click.echo(
            f"{name}: tracking ref not updated — {tracked.stderr.strip()}", err=True
        )

    if remote_sha is None:
        return True, "wrote bundle"
    if remote_sha == local_sha:
        return True, "rewrote missing bundle"
    if force:
        return True, "forced bundle"
    return True, f"wrote bundle ({commit_count(path, remote_sha, local_sha)} commit(s))"
