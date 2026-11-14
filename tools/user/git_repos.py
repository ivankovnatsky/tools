import os
import shutil
from typing import Dict

from tools.log import Color, debug, log
from tools.system_paths import system_bin, system_dir
from tools.util import run_command


def is_checkout(path: str) -> bool:
    """A ``.git`` directory, or a ``.git`` file pointing at one (linked
    worktrees, submodules, ``--separate-git-dir``)."""
    dot_git = os.path.join(os.path.expanduser(path), ".git")
    if os.path.isdir(dot_git):
        return True
    try:
        with open(dot_git, "rb") as f:
            return f.read(8) == b"gitdir: "
    except OSError:
        return False


def _has_local_work(git_bin: str, path: str, env: Dict[str, str]) -> bool:
    """True if the worktree has uncommitted changes or unpushed commits.

    Removal is an rmtree, so anything not on a remote is gone for good.
    A repo we cannot interrogate counts as having work — refusing to
    delete costs a stale directory, deleting costs the work itself.
    """
    rc, stdout, _ = run_command([git_bin, "-C", path, "status", "--porcelain"], env)
    if rc != 0:
        return True
    if stdout.strip():
        return True
    # Stash entries are neither status output nor branch commits; rmtree
    # would delete them silently.
    rc, stdout, _ = run_command([git_bin, "-C", path, "stash", "list"], env)
    if rc != 0 or stdout.strip():
        return True
    # --all (all local refs plus HEAD) rather than --branches: a clean
    # detached-HEAD commit or a local-tag-only commit is local work too.
    rc, stdout, _ = run_command(
        [git_bin, "-C", path, "log", "--all", "--not", "--remotes", "--oneline"], env
    )
    if rc != 0:
        return True
    return bool(stdout.strip())


def install_git_repos(repos: Dict[str, str], state: Dict, update: bool = False):
    """Clone missing repositories; with ``update``, also pull existing ones."""
    installed = set(state.get("gitRepos", {}).get("installed", []))
    if not repos and not installed:
        return True
    desired = set(repos.keys())
    to_install = desired - installed
    to_remove = installed - desired
    to_update = (
        {d for d in desired & installed if os.path.isdir(os.path.expanduser(d))}
        if update
        else set()
    )

    if not to_install and not to_remove and not to_update:
        debug("All git repos already installed", Color.BLUE)
        if installed != desired:
            state.setdefault("gitRepos", {})["installed"] = sorted(installed)
        return True

    git_bin = system_bin("git")

    env = os.environ.copy()
    env["PATH"] = f"{system_dir('git')}:{env.get('PATH', '')}"

    state_changed = False
    success = True

    for dest_path in to_remove:
        expanded_path = os.path.expanduser(dest_path)
        if os.path.exists(expanded_path):
            # git -C on a dir that lost its .git interrogates the enclosing
            # repo, which can read clean (e.g. a `**` .gitignore).
            if not is_checkout(expanded_path):
                log(
                    f"Releasing {dest_path}: no longer a git checkout, left in place.",
                    Color.YELLOW,
                )
                installed.discard(dest_path)
                state_changed = True
                continue
            if _has_local_work(git_bin, expanded_path, env):
                log(
                    f"Keeping {dest_path}: uncommitted or unpushed work. "
                    "Remove it by hand once the work is safe.",
                    Color.YELLOW,
                )
                continue
            log(f"Removing git repo: {dest_path}", Color.RED)
            try:
                shutil.rmtree(expanded_path)
            except OSError as e:
                # Keep it tracked so the next run retries, and don't let one
                # stubborn checkout abort the whole deploy (unsaved state).
                log(f"Failed to remove {dest_path}: {e}", Color.RED)
                success = False
                continue
            state_changed = True
        installed.discard(dest_path)

    for dest_path in sorted(to_install | to_update):
        repo_url = repos[dest_path]
        expanded_path = os.path.expanduser(dest_path)

        if os.path.exists(expanded_path):
            # Only a real checkout gets pulled and recorded. An arbitrary
            # directory at that path is not a repo we manage, and marking it
            # installed would make it an rmtree candidate later.
            if not is_checkout(expanded_path):
                log(f"Not a git repo, skipping {dest_path}", Color.YELLOW)
                success = False
                continue
            if update:
                log(f"Updating git repo: {dest_path}", Color.BLUE)
                cmd = [git_bin, "-C", expanded_path, "pull", "--ff-only"]
                returncode, stdout, stderr = run_command(cmd, env)
                if returncode != 0:
                    log(f"Failed to update {dest_path}: {stderr}", Color.YELLOW)
                    success = False
            else:
                log(f"Tracking existing git repo: {dest_path}", Color.BLUE)
        else:
            log(f"Cloning git repo: {repo_url} -> {dest_path}", Color.GREEN)
            parent_dir = os.path.dirname(expanded_path)
            os.makedirs(parent_dir, exist_ok=True)
            cmd = [git_bin, "clone", repo_url, expanded_path]
            returncode, stdout, stderr = run_command(cmd, env)
            if returncode != 0:
                log(f"Failed to clone {repo_url}: {stderr}", Color.RED)
                success = False
                continue

        installed.add(dest_path)
        state_changed = True

    if state_changed or installed != desired:
        state.setdefault("gitRepos", {})["installed"] = sorted(installed)

    return success
