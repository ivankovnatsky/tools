"""Git subprocess helpers."""

import os
import subprocess

import click

GIT_TIMEOUT = 60 * 5

# Work-tree path -> git dir, for repos whose git dir lives outside the work
# tree (`gitDir`). Every repo call passes `cwd=path`, so run_git keys on it.
_GIT_DIRS = {}

# Environment variables that point git at a repository other than the one
# found from cwd.
_REPO_ENV = {
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
}


def register_git_dir(path, git_dir):
    _GIT_DIRS[os.path.normpath(path)] = git_dir


def git_dir_of(path):
    return _GIT_DIRS.get(os.path.normpath(path)) or os.path.join(path, ".git")


def run_git(*args, cwd=None, check=True, timeout=GIT_TIMEOUT):
    # Each call targets the repo at cwd only. Drop repo-locating vars inherited
    # from the caller (e.g. an exported GIT_DIR), or every repo's commands
    # would run against that one repo.
    env = {k: v for k, v in os.environ.items() if k not in _REPO_ENV}
    git_dir = _GIT_DIRS.get(os.path.normpath(cwd)) if cwd else None
    if git_dir:
        # Explicit GIT_DIR turns off discovery, so git never walks up from
        # the work tree into an enclosing repo (e.g. one over $HOME).
        env.update(GIT_DIR=git_dir, GIT_WORK_TREE=cwd)
    try:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args, 1, "", f"git {args[0]} timed out after {timeout}s"
        )
    if check and result.returncode != 0:
        click.echo(f"Error: {result.stderr.strip()}", err=True)
    return result


def commit_count(path, old, new):
    """Number of commits in old..new as a string, "?" if git cannot tell."""
    count = run_git("rev-list", "--count", f"{old}..{new}", cwd=path, check=False)
    return count.stdout.strip() if count.returncode == 0 else "?"


def has_git_lock(path):
    git_dir = git_dir_of(path)
    for lock in ("index.lock", "HEAD.lock", "config.lock"):
        if os.path.exists(os.path.join(git_dir, lock)):
            return True
    return False
