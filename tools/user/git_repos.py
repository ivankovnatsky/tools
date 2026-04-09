import os
import shutil
from typing import Dict

from tools.log import Color, log
from tools.util import run_command


def install_git_repos(repos: Dict[str, str], paths: Dict, state: Dict):
    """Clone or update git repositories to specified paths."""
    if not repos:
        return True

    installed = set(state.get("gitRepos", {}).get("installed", []))
    desired = set(repos.keys())
    to_install = desired - installed
    to_remove = installed - desired

    env = os.environ.copy()
    env["PATH"] = f"{paths['git']}:{env.get('PATH', '')}"

    state_changed = False

    for dest_path in to_remove:
        expanded_path = os.path.expanduser(dest_path)
        if os.path.exists(expanded_path):
            log(f"Removing git repo: {dest_path}", Color.RED)
            shutil.rmtree(expanded_path)
            state_changed = True
        installed.discard(dest_path)

    for dest_path in to_install:
        repo_url = repos[dest_path]
        expanded_path = os.path.expanduser(dest_path)

        if os.path.exists(expanded_path):
            log(f"Updating git repo: {dest_path}", Color.BLUE)
            cmd = [f"{paths['git']}/git", "-C", expanded_path, "pull", "--ff-only"]
            returncode, stdout, stderr = run_command(cmd, env)
            if returncode != 0:
                log(f"Failed to update {dest_path}: {stderr}", Color.YELLOW)
        else:
            log(f"Cloning git repo: {repo_url} -> {dest_path}", Color.GREEN)
            parent_dir = os.path.dirname(expanded_path)
            os.makedirs(parent_dir, exist_ok=True)
            cmd = [f"{paths['git']}/git", "clone", repo_url, expanded_path]
            returncode, stdout, stderr = run_command(cmd, env)
            if returncode != 0:
                log(f"Failed to clone {repo_url}: {stderr}", Color.RED)
                continue

        installed.add(dest_path)
        state_changed = True

    if not to_install and not to_remove:
        log("All git repos already installed", Color.BLUE)

    if state_changed or installed != desired:
        state.setdefault("gitRepos", {})["installed"] = list(installed)

    return True
