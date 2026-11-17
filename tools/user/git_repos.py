import os
from typing import Callable, Dict, Optional

from tools import fsutil
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
    """True if the worktree has uncommitted changes, ignored files or unpushed commits.

    Removal trashes the whole checkout; anything not on a remote then lives
    only in the Trash.
    A repo we cannot interrogate counts as having work — refusing to
    delete costs a stale directory, deleting costs the work itself.
    """
    rc, stdout, _ = run_command([git_bin, "-C", path, "status", "--porcelain", "--ignored"], env)
    if rc != 0:
        return True
    if stdout.strip():
        return True
    # Stash entries are neither status output nor branch commits; removal
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


def _is_empty_dir(path: str) -> bool:
    try:
        return os.path.isdir(path) and not os.path.islink(path) and not os.listdir(path)
    except OSError:
        return False


def plan(repos: Dict[str, str], state: Dict, update: bool = False) -> Dict:
    """What deploy does with git repos; the preview renders the same plan.

    Per-path checks that the filesystem can change mid-deploy (an earlier
    clone, a removal) are `checkout_action` and `removal_action`, which
    deploy calls right before acting on each path.
    """
    stored = set(state.get("gitRepos", {}).get("installed", []))
    # State keys match declared paths by file identity, not spelling.
    installed = set(fsutil.respell(dict.fromkeys(stored), repos.keys()))
    desired = set(repos.keys())
    # A tracked checkout deleted (or emptied) by hand is cloned again.
    to_install = {
        d
        for d in desired
        if d not in installed
        or not os.path.exists(os.path.expanduser(d))
        or _is_empty_dir(os.path.expanduser(d))
    }
    to_update = (
        {d for d in desired & installed if os.path.isdir(os.path.expanduser(d))}
        if update
        else set()
    )
    return {
        "stored": stored,
        "installed": installed,
        "to_install": to_install,
        "to_remove": installed - desired,
        "to_update": to_update,
    }


def checkout_action(dest: str, installed, update: bool) -> Optional[str]:
    """ "clone", "not_repo", "track", "pull", or None when nothing is due."""
    expanded = os.path.expanduser(dest)
    if not os.path.exists(expanded) or _is_empty_dir(expanded):
        return "clone"
    # Only a real checkout gets pulled and recorded. An arbitrary directory at
    # that path is not a repo we manage, and marking it installed would make
    # it a removal candidate later.
    if not is_checkout(expanded):
        return "not_repo"
    if dest not in installed:
        return "track"
    return "pull" if update else None


def removal_action(dest: str, has_local_work: Callable[[str], bool]) -> str:
    """ "release" (not a checkout), "keep" (local work), or "remove"."""
    expanded = os.path.expanduser(dest)
    # git -C on a dir that lost its .git interrogates the enclosing repo,
    # which can read clean (e.g. a `**` .gitignore).
    if os.path.exists(expanded) and not is_checkout(expanded):
        return "release"
    if os.path.exists(expanded) and has_local_work(expanded):
        return "keep"
    return "remove"


def install_git_repos(repos: Dict[str, str], state: Dict, update: bool = False):
    """Clone missing repositories; with ``update``, also pull existing ones."""
    p = plan(repos, state, update)
    stored = p["stored"]
    if not repos and not stored:
        return True
    installed = p["installed"]
    to_install, to_remove, to_update = p["to_install"], p["to_remove"], p["to_update"]

    if not to_install and not to_remove and not to_update:
        debug("All git repos already installed", Color.BLUE)
        if installed != stored:
            state.setdefault("gitRepos", {})["installed"] = sorted(installed)
        return True

    git_bin = system_bin("git")

    env = os.environ.copy()
    env["PATH"] = f"{system_dir('git')}:{env.get('PATH', '')}"

    state_changed = False
    success = True

    for dest_path in to_remove:
        expanded_path = os.path.expanduser(dest_path)
        action = removal_action(dest_path, lambda path: _has_local_work(git_bin, path, env))
        if action == "release":
            log(
                f"Releasing {dest_path}: no longer a git checkout, left in place.",
                Color.YELLOW,
            )
            installed.discard(dest_path)
            state_changed = True
            continue
        if action == "keep":
            log(
                f"Keeping {dest_path}: uncommitted or unpushed work. "
                "Remove it by hand once the work is safe.",
                Color.YELLOW,
            )
            continue
        if os.path.exists(expanded_path):
            log(f"Removing git repo: {dest_path}", Color.RED)
            try:
                fsutil.trash(expanded_path)
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
        action = checkout_action(dest_path, installed, update)

        if action == "not_repo":
            log(f"Not a git repo, skipping {dest_path}", Color.YELLOW)
            success = False
            continue
        if action != "clone" and update:
            log(f"Updating git repo: {dest_path}", Color.BLUE)
            cmd = [git_bin, "-C", expanded_path, "pull", "--ff-only"]
            returncode, stdout, stderr = run_command(cmd, env)
            if returncode != 0:
                log(f"Failed to update {dest_path}: {stderr}", Color.YELLOW)
                success = False
        elif action != "clone":
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

    if state_changed or installed != stored:
        state.setdefault("gitRepos", {})["installed"] = sorted(installed)

    return success
