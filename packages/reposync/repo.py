"""Repository init, sync, and status operations."""

import os

import click

from alerting import alert
from bundle import (
    CONFLICT,
    ERROR,
    MISSING,
    READY,
    bundle_state,
    bundle_tip,
    publish_bundle,
)
from git_ops import commit_count, git_dir_of, has_git_lock, run_git


def _configure_local_branch(path, branch, remote, name, webhook_url):
    """Configure the local branch ref + upstream tracking from an existing
    remote-tracking ref, using only plumbing so the working tree is never
    touched. If HEAD is currently unborn, also point HEAD at the new branch
    and populate the index; if HEAD is already born on another branch, leave
    HEAD and the index alone so an existing checkout is not disturbed.

    Returns True on success, False (with alert) on failure.
    """
    click.echo(
        f"{name}: creating branch {branch} tracking {remote}/{branch}",
        err=True,
    )

    # Detect unborn HEAD. `rev-parse --verify HEAD` fails when HEAD points at
    # a ref that does not yet exist (the state immediately after `git init`).
    head_check = run_git("rev-parse", "--verify", "HEAD", cwd=path, check=False)
    head_is_unborn = head_check.returncode != 0

    # Create the local branch ref pointing at the remote tip. Always safe —
    # this does not touch HEAD, the index, or the working tree.
    result = run_git(
        "update-ref",
        f"refs/heads/{branch}",
        f"refs/remotes/{remote}/{branch}",
        cwd=path,
        check=False,
    )
    if result.returncode != 0:
        alert(
            webhook_url,
            f"`{name}`: failed to create branch {branch} — {result.stderr.strip()}",
        )
        return False

    # Set upstream tracking. Idempotent and harmless when already configured.
    result = run_git(
        "branch", "-u", f"{remote}/{branch}", branch, cwd=path, check=False
    )
    if result.returncode != 0:
        alert(
            webhook_url,
            f"`{name}`: failed to set upstream tracking for {branch} — {result.stderr.strip()}",
        )
        return False

    if not head_is_unborn:
        # An existing checkout is on a real branch — do NOT move HEAD or
        # touch the index. The user (or a later cycle, once they switch
        # branches) can integrate the new branch when ready.
        click.echo(
            f"{name}: branch {branch} created; HEAD left on existing checkout",
            err=True,
        )
        return True

    # HEAD is unborn (e.g. fresh `git init`). Point HEAD at the new branch
    # and populate the index from HEAD so `git status` reports working-tree
    # paths against the configured ref instead of listing all tracked paths
    # as "deleted". None of these calls write to the working tree.
    result = run_git(
        "symbolic-ref", "HEAD", f"refs/heads/{branch}", cwd=path, check=False
    )
    if result.returncode != 0:
        alert(
            webhook_url,
            f"`{name}`: failed to point HEAD at {branch} — {result.stderr.strip()}",
        )
        return False
    # Populate the index from HEAD. This overwrites the index — safe here
    # because we only reach this branch when HEAD was unborn (just after
    # `git init`), so the index was empty. The working tree is not touched.
    result = run_git("read-tree", "HEAD", cwd=path, check=False)
    if result.returncode != 0:
        alert(
            webhook_url,
            f"`{name}`: failed to populate index from HEAD — {result.stderr.strip()}",
        )
        return False
    # Refresh stat info so files whose working-tree content matches HEAD show
    # as clean. Non-zero exit just means some files differ — working-tree
    # state is the user's domain; we never alert on dirtiness.
    run_git("update-index", "--refresh", cwd=path, check=False)
    return True


def _ignore_file(repo):
    """`<path>/.gitignore`, or `<gitDir>/info/exclude` for a gitDir repo,
    whose work tree must stay free of git files."""
    if repo.get("gitDir"):
        return os.path.join(repo["gitDir"], "info", "exclude")
    return os.path.join(repo["path"], ".gitignore")


def _ensure_gitignore(gitignore_path, lines, name, webhook_url):
    """Enforce the content of the repo's ignore file. Rewritten whenever it
    drifts, so the config stays the single source of truth — the common case
    being `["*"]` over a directory like $HOME, where tracking is opt-in via
    `git add -f`.

    Returns True on success, False (with alert) on failure.
    """
    desired = "".join(f"{line}\n" for line in lines)
    try:
        with open(gitignore_path) as f:
            if f.read() == desired:
                return True
    except FileNotFoundError:
        pass
    except OSError as exc:
        alert(webhook_url, f"`{name}`: could not read {gitignore_path} — {exc}")
        return False

    try:
        os.makedirs(os.path.dirname(gitignore_path), exist_ok=True)
        with open(gitignore_path, "w") as f:
            f.write(desired)
    except OSError as exc:
        alert(webhook_url, f"`{name}`: could not write {gitignore_path} — {exc}")
        return False
    click.echo(f"{name}: wrote {gitignore_path}", err=True)
    return True


def _ensure_identity(path, identity, name, webhook_url):
    """Enforce repo-local committer identity. Repo-local config outranks
    every global/include rule, so commits made in this repo use the
    configured identity no matter who commits or what the global git
    config resolves to.

    Returns True on success, False (with alert) on failure.
    """
    settings = {
        "user.name": identity.get("name"),
        "user.email": identity.get("email"),
        "user.signingKey": identity.get("signingKey"),
    }
    for key, value in settings.items():
        if not value:
            continue
        current = run_git("config", "--local", "--get", key, cwd=path, check=False)
        if current.returncode == 0 and current.stdout.strip() == value:
            continue
        result = run_git("config", "--local", key, value, cwd=path, check=False)
        if result.returncode != 0:
            alert(
                webhook_url,
                f"`{name}`: failed to set {key} — {result.stderr.strip()}",
            )
            return False
        click.echo(f"{name}: set {key}", err=True)
    return True


def _worktree_ok(path):
    current = run_git(
        "config", "--local", "--get", "core.worktree", cwd=path, check=False
    )
    return current.returncode == 0 and current.stdout.strip() == path


def _ensure_worktree(path, name, webhook_url):
    """Enforce core.worktree for a repo with an outside `gitDir`, so plain
    `git --git-dir <gitDir>` by hand finds the work tree. reposync itself
    always passes GIT_WORK_TREE and does not depend on it.

    Returns True on success, False (with alert) on failure.
    """
    if _worktree_ok(path):
        return True
    result = run_git("config", "--local", "core.worktree", path, cwd=path, check=False)
    if result.returncode != 0:
        alert(
            webhook_url,
            f"`{name}`: failed to set core.worktree — {result.stderr.strip()}",
        )
        return False
    click.echo(f"{name}: set core.worktree", err=True)
    return True


def init_repo(repo, webhook_url=None):
    path = repo["path"]
    remote = repo["remote"]
    remote_url = repo.get("remoteUrl")
    bundle_file = repo.get("bundleFile")
    branch = repo["branch"]
    prune = repo.get("prune", False)
    gitignore = repo.get("gitignore")
    display = repo.get("name") or os.path.basename(path)
    name = f"{display} ({remote}/{branch})"

    if not os.path.isdir(path) and repo.get("gitDir"):
        # A gitDir work tree sits in a file-sync folder that delivers it;
        # creating it first could leave iCloud a rival empty copy.
        click.echo(f"{name}: skip (work tree {path} not there yet)", err=True)
        return False

    if not os.path.isdir(path) and remote_url is None:
        # Local-only or bundle repo: there is nothing to clone (the bundle
        # may not exist yet), so the directory is ours to create before
        # `git init` can run in it.
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            alert(webhook_url, f"`{name}`: could not create {path} — {exc}")
            return False
        click.echo(f"{name}: created directory {path}", err=True)

    if not os.path.isdir(path):
        parent = os.path.dirname(path.rstrip("/"))
        if not os.path.isdir(parent):
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as exc:
                alert(
                    webhook_url,
                    f"`{name}`: could not create parent directory {parent} — {exc}",
                )
                return False
            click.echo(f"{name}: created parent directory {parent}", err=True)
        click.echo(f"{name}: cloning {remote_url} into {path}", err=True)
        result = run_git(
            "clone",
            "--origin",
            remote,
            "--branch",
            branch,
            remote_url,
            path,
            check=False,
        )
        if result.returncode != 0:
            # Fallback for empty/branchless remotes: clone without --branch.
            result = run_git(
                "clone",
                "--origin",
                remote,
                remote_url,
                path,
                check=False,
            )
            if result.returncode != 0:
                alert(
                    webhook_url, f"`{name}`: git clone failed — {result.stderr.strip()}"
                )
                return False

    git_dir = git_dir_of(path)
    if not os.path.isdir(git_dir):
        if repo.get("gitDir"):
            # The git dir lives outside the work tree; its parent is ours to
            # create, and `git init` (run with GIT_DIR/GIT_WORK_TREE set)
            # records the work tree as core.worktree.
            try:
                os.makedirs(os.path.dirname(git_dir.rstrip("/")), exist_ok=True)
            except OSError as exc:
                alert(webhook_url, f"`{name}`: could not create {git_dir} — {exc}")
                return False
        click.echo(f"{name}: initializing git repo at {path}", err=True)
        result = run_git("init", "-b", branch, cwd=path)
        if result.returncode != 0:
            alert(webhook_url, f"`{name}`: git init failed")
            return False

    if repo.get("gitDir") and not _ensure_worktree(path, name, webhook_url):
        return False

    if gitignore is not None and not _ensure_gitignore(
        _ignore_file(repo), gitignore, name, webhook_url
    ):
        return False

    identity = repo.get("identity") or {}
    if identity and not _ensure_identity(path, identity, name, webhook_url):
        return False

    if remote_url is None and bundle_file is None:
        # No remote to configure, fetch from, or track a branch against.
        # HEAD stays unborn until the first commit, which is the user's call.
        click.echo(f"{name}: init complete (local-only, no remote)", err=True)
        return True

    # A bundle file is the remote: git fetches from it like any URL.
    remote_url = remote_url or bundle_file

    # Ensure remote exists with correct URL
    result = run_git("remote", "get-url", remote, cwd=path, check=False)
    if result.returncode != 0:
        click.echo(f"{name}: adding remote {remote} -> {remote_url}", err=True)
        run_git("remote", "add", remote, remote_url, cwd=path)
    elif result.stdout.strip() != remote_url:
        click.echo(f"{name}: updating remote {remote} -> {remote_url}", err=True)
        run_git("remote", "set-url", remote, remote_url, cwd=path)
    else:
        click.echo(f"{name}: remote {remote} OK", err=True)

    if bundle_file is not None:
        # Fetching and branch setup happen in sync_repo, which knows how to
        # treat a missing or evicted bundle.
        click.echo(f"{name}: init complete (bundle remote)", err=True)
        return True

    # Fetch from remote
    fetch_args = ["fetch"] + (["--prune"] if prune else []) + [remote]
    result = run_git(*fetch_args, cwd=path, check=False)
    if result.returncode != 0:
        alert(webhook_url, f"`{name}`: fetch failed — {result.stderr.strip()}")
        return False

    # Ensure branch exists and tracks remote
    result = run_git("rev-parse", "--verify", branch, cwd=path, check=False)
    if result.returncode != 0:
        # Local branch doesn't exist — check if remote branch does
        result = run_git(
            "rev-parse", "--verify", f"{remote}/{branch}", cwd=path, check=False
        )
        if result.returncode == 0:
            if not _configure_local_branch(path, branch, remote, name, webhook_url):
                return False
        else:
            click.echo(
                f"{name}: branch {branch} not on remote yet (will be created on first push)",
                err=True,
            )
    else:
        # Set upstream tracking
        run_git("branch", "-u", f"{remote}/{branch}", branch, cwd=path, check=False)

    click.echo(f"{name}: init complete", err=True)
    return True


def needs_init(repo):
    """Check if a repo needs initialization."""
    path = repo["path"]
    remote = repo["remote"]
    remote_url = repo.get("remoteUrl") or repo.get("bundleFile")
    branch = repo["branch"]
    gitignore = repo.get("gitignore")

    if not os.path.isdir(path):
        return True
    if not os.path.isdir(git_dir_of(path)):
        return True
    if repo.get("gitDir") and not _worktree_ok(path):
        return True

    # Drifted or missing .gitignore is init's job to rewrite.
    if gitignore is not None:
        desired = "".join(f"{line}\n" for line in gitignore)
        try:
            with open(_ignore_file(repo)) as f:
                if f.read() != desired:
                    return True
        except OSError:
            return True

    # A local-only repo has no remote or tracking branch to verify.
    if remote_url is None:
        return False

    result = run_git("remote", "get-url", remote, cwd=path, check=False)
    if result.returncode != 0:
        return True
    if result.stdout.strip() != remote_url:
        return True

    # Local branch missing while remote branch exists — init must
    # create the tracking branch so sync_repo has something to pull/push.
    local = run_git("rev-parse", "--verify", branch, cwd=path, check=False)
    if local.returncode != 0:
        remote_ref = run_git(
            "rev-parse", "--verify", f"{remote}/{branch}", cwd=path, check=False
        )
        if remote_ref.returncode == 0:
            return True

    return False


def _stage_local_changes(path, branch, name, webhook_url):
    """Run `git add --all` so untracked or modified files don't block the
    next ff-only pull (incoming commits that touch the same paths would
    otherwise refuse to merge with "untracked working tree files would
    be overwritten"). Only acts when HEAD is on the configured branch —
    staging onto an unrelated branch the user is working on would
    interfere with their in-progress index.

    Returns (acted, ok): acted is True if files were staged, ok is False
    only on a hard failure (alerted).
    """
    head_ref = run_git("symbolic-ref", "--short", "HEAD", cwd=path, check=False)
    current_branch = head_ref.stdout.strip() if head_ref.returncode == 0 else None
    if current_branch != branch:
        click.echo(
            f"{name}: skip stage (HEAD is on {current_branch!r}, not {branch!r})",
            err=True,
        )
        return False, True

    status = run_git("status", "--porcelain", cwd=path, check=False)
    if status.returncode != 0:
        alert(
            webhook_url,
            f"`{name}`: stage status check failed — {status.stderr.strip()}",
        )
        return False, False
    if not status.stdout.strip():
        return False, True

    add_result = run_git("add", "--all", cwd=path, check=False)
    if add_result.returncode != 0:
        alert(
            webhook_url,
            f"`{name}`: `git add --all` failed — {add_result.stderr.strip()}",
        )
        return False, False

    return True, True


def sync_repo(repo, webhook_url=None):
    path = repo["path"]
    remote = repo["remote"]
    remote_url = repo.get("remoteUrl")
    bundle_file = repo.get("bundleFile")
    branch = repo["branch"]
    sync_mode = repo.get("syncMode", "pull-push")
    prune = repo.get("prune", False)
    auto_stage = repo.get("autoStage", False)
    display = repo.get("name") or os.path.basename(path)
    name = f"{display} ({remote}/{branch})"

    if not os.path.isdir(path):
        # init_repo runs before sync_repo; reaching here means init was
        # supposed to handle this and didn't. Alert so the user investigates.
        alert(
            webhook_url,
            f"`{name}`: cannot sync — {path} does not exist (init did not run or failed)",
        )
        return False

    if not os.path.isdir(git_dir_of(path)):
        alert(
            webhook_url,
            f"`{name}`: cannot sync — {path} is not a git repo (init did not configure it)",
        )
        return False

    if has_git_lock(path):
        # Lock files are transient (another git process is running). This is
        # benign — sync next cycle. Don't alert; just log.
        click.echo(f"{name}: skip (git lock file exists)", err=True)
        return True

    ok = True
    actions = []

    identity = repo.get("identity") or {}
    if identity and not _ensure_identity(path, identity, name, webhook_url):
        return False

    if auto_stage:
        acted, stage_ok = _stage_local_changes(path, branch, name, webhook_url)
        if not stage_ok:
            return False
        if acted:
            actions.append("staged local changes")

    if remote_url is None and bundle_file is None:
        # Local-only: no fetch, pull or push exists for this repo. init_repo
        # has already enforced identity and .gitignore, and committing is the
        # user's call, so there is nothing left for a sync cycle to do.
        summary = ", ".join(actions) if actions else "local-only, nothing to sync"
        click.echo(f"{name}: OK ({summary})", err=True)
        return ok

    fetch = True
    if bundle_file is not None:
        state = bundle_state(bundle_file, name, webhook_url)
        if state in (CONFLICT, ERROR):
            return False
        if state == MISSING:
            # Nobody has published yet. Write the bundle if there is a local
            # branch to publish; otherwise wait for the other host.
            local = run_git("rev-parse", "--verify", branch, cwd=path, check=False)
            if local.returncode != 0:
                click.echo(f"{name}: OK (no bundle and no local branch yet)", err=True)
                return ok
            if sync_mode == "pull-only":
                click.echo(f"{name}: OK (no bundle yet, pull-only)", err=True)
                return ok
            # What the bundle last carried, if this host fetched it before
            # it went missing: publish_bundle must not regress past it.
            known = run_git(
                "rev-parse", "--verify", f"{remote}/{branch}", cwd=path, check=False
            )
            pub_ok, action = publish_bundle(
                path,
                branch,
                remote,
                bundle_file,
                local.stdout.strip(),
                known.stdout.strip() if known.returncode == 0 else None,
                name,
                webhook_url,
                missing=True,
            )
            if pub_ok:
                if action:
                    actions.append(action)
                summary = (
                    ", ".join(actions)
                    if actions
                    else "bundle missing, waiting for the host with the newest commits"
                )
                click.echo(f"{name}: OK ({summary})", err=True)
            return pub_ok and ok
        if state != READY:
            click.echo(f"{name}: skip (bundle {state})", err=True)
            return ok
        # Fetching unpacks the whole bundle; skip it when the tip has not
        # moved since the last fetch.
        tip = bundle_tip(bundle_file, branch)
        if tip is None:
            alert(webhook_url, f"`{name}`: bundle unreadable or missing {branch}")
            return False
        known = run_git(
            "rev-parse", "--verify", f"{remote}/{branch}", cwd=path, check=False
        )
        fetch = known.returncode != 0 or known.stdout.strip() != tip

    # Fetch
    fetch_args = ["fetch"] + (["--prune"] if prune else []) + [remote]
    result = run_git(*fetch_args, cwd=path, check=False) if fetch else None
    if result is not None and result.returncode != 0:
        alert(webhook_url, f"`{name}`: fetch failed — {result.stderr.strip()}")
        return False

    # Check if remote branch exists
    result = run_git(
        "rev-parse", "--verify", f"{remote}/{branch}", cwd=path, check=False
    )
    remote_exists = result.returncode == 0

    # Check if local branch exists
    result = run_git("rev-parse", "--verify", branch, cwd=path, check=False)
    local_exists = result.returncode == 0

    # Upstream tracking, so a by-hand `git pull` works. init sets it only
    # when the remote branch already existed; a bundle remote can appear
    # later than the local branch.
    if local_exists and remote_exists:
        upstream = run_git(
            "config", "--get", f"branch.{branch}.remote", cwd=path, check=False
        )
        if upstream.returncode != 0:
            run_git("branch", "-u", f"{remote}/{branch}", branch, cwd=path, check=False)
            actions.append("set upstream")

    # Pull (ff-only) — only if HEAD is on the target branch and this repo allows pulls.
    if sync_mode == "push-only":
        click.echo(f"{name}: skip pull (push-only mode)", err=True)
    elif remote_exists and local_exists:
        head_ref = run_git("symbolic-ref", "--short", "HEAD", cwd=path, check=False)
        current_branch = head_ref.stdout.strip() if head_ref.returncode == 0 else None

        if current_branch != branch:
            # User is on a different branch (e.g. a feature branch). Skipping
            # pull is the right call — don't alert; this is a legitimate
            # workflow state, not a failure. Log how far behind the target
            # branch is so silent drift is at least visible in the journal —
            # matters most for pull-only repos, where push is also skipped
            # and the repo can otherwise fall behind without any signal.
            behind_note = ""
            n = commit_count(path, branch, f"{remote}/{branch}")
            if n not in ("0", "?"):
                behind_note = f", behind {n} commit(s)"
            click.echo(
                f"{name}: skip pull (HEAD is on {current_branch!r}, not {branch!r}){behind_note}",
                err=True,
            )
        else:
            local_before = run_git(
                "rev-parse", branch, cwd=path, check=False
            ).stdout.strip()
            # Pin the merge target so a concurrent fetch advancing the
            # remote-tracking ref between rev-parse and merge can't cause a
            # post-hoc mismatch.
            target_sha = run_git(
                "rev-parse", f"{remote}/{branch}", cwd=path, check=False
            ).stdout.strip()
            # Only guard against clobbering when the merge could actually
            # fast-forward; otherwise let git report the real reason.
            # --no-overwrite-ignore: git protects tracked and untracked
            # files on its own but would silently replace *ignored* ones,
            # which over a `*`-ignored $HOME is every file not force-added.
            result = run_git(
                "merge",
                "--ff-only",
                "--no-overwrite-ignore",
                target_sha,
                cwd=path,
                check=False,
            )
            if result.returncode != 0:
                reason = [
                    line.strip()
                    for line in result.stderr.splitlines()
                    if line.strip()
                    and not line.startswith(("hint:", "Please", "Aborting"))
                ]
                detail = "; ".join(reason[:8]) if reason else "not fast-forward"
                alert(
                    webhook_url,
                    f"`{name}`: pull failed — {detail} — resolve manually",
                )
                ok = False
            else:
                local_after = run_git(
                    "rev-parse", branch, cwd=path, check=False
                ).stdout.strip()
                # Tripwire: ff-only succeeded, so target_sha must be an
                # ancestor of local (behind: local advanced to it; equal:
                # no-op; ahead: already up to date). If not, a silent break
                # left local short of the merge target — surface it.
                is_ancestor = run_git(
                    "merge-base",
                    "--is-ancestor",
                    target_sha,
                    local_after,
                    cwd=path,
                    check=False,
                )
                if is_ancestor.returncode != 0:
                    alert(
                        webhook_url,
                        f"`{name}`: merge --ff-only returned OK but {branch}@{local_after[:8]} is not descended from target {target_sha[:8]} — investigate",
                    )
                    ok = False
                if local_before != local_after:
                    n = commit_count(path, local_before, local_after)
                    actions.append(f"pulled {n} commit(s)")

    # Push — skip for pull-only repos.
    if sync_mode == "pull-only":
        click.echo(f"{name}: skip push (pull-only mode)", err=True)
    elif local_exists:
        local_sha = run_git("rev-parse", branch, cwd=path, check=False).stdout.strip()
        remote_sha = (
            run_git(
                "rev-parse", f"{remote}/{branch}", cwd=path, check=False
            ).stdout.strip()
            if remote_exists
            else None
        )
        if bundle_file is not None:
            # A failed pull already alerted and means the bundle is ahead or
            # diverged, so there is nothing to publish this cycle.
            if ok:
                pub_ok, action = publish_bundle(
                    path,
                    branch,
                    remote,
                    bundle_file,
                    local_sha,
                    remote_sha,
                    name,
                    webhook_url,
                )
                if not pub_ok:
                    ok = False
                elif action:
                    actions.append(action)
        elif (
            result := run_git("push", remote, branch, cwd=path, check=False)
        ).returncode != 0:
            stderr = result.stderr.strip()
            if "non-fast-forward" in stderr or "rejected" in stderr:
                alert(
                    webhook_url,
                    f"`{name}`: push rejected (non-fast-forward) — resolve manually",
                )
            else:
                alert(webhook_url, f"`{name}`: push failed — {stderr}")
            ok = False
        elif remote_sha and local_sha != remote_sha:
            n = commit_count(path, remote_sha, local_sha)
            actions.append(f"pushed {n} commit(s)")
    elif not remote_exists:
        click.echo(f"{name}: skip (no local or remote branch yet)", err=True)
    else:
        # Remote branch is now visible (commonly: it appeared between this
        # cycle's `needs_init` check and the fetch above). Configure the
        # local branch via the same plumbing init uses — this is not an
        # error condition.
        if not _configure_local_branch(path, branch, remote, name, webhook_url):
            ok = False
        else:
            actions.append("configured local branch")

    if ok:
        summary = ", ".join(actions) if actions else "up to date"
        click.echo(f"{name}: OK ({summary})", err=True)

    return ok


def status_repo(repo):
    path = repo["path"]
    remote = repo["remote"]
    remote_url = repo.get("remoteUrl") or repo.get("bundleFile")
    branch = repo["branch"]
    prune = repo.get("prune", False)
    display = repo.get("name") or os.path.basename(path)
    name = f"{display} ({remote}/{branch})"

    if not os.path.isdir(path) or not os.path.isdir(git_dir_of(path)):
        click.echo(f"{name}: not a repo")
        return

    if remote_url is None:
        head = run_git("rev-parse", "--verify", "HEAD", cwd=path, check=False)
        state = "no commits yet" if head.returncode != 0 else "local-only"
        click.echo(f"{name}: {state}")
        return

    bundle_file = repo.get("bundleFile")
    if bundle_file is not None:
        state = bundle_state(bundle_file, name, None, act=False)
        if state != READY:
            click.echo(f"{name}: bundle {state}")
            return

    # Fetch silently
    fetch_args = ["fetch"] + (["--prune"] if prune else []) + [remote]
    run_git(*fetch_args, cwd=path, check=False)

    local = run_git("rev-parse", branch, cwd=path, check=False)
    remote_ref = run_git("rev-parse", f"{remote}/{branch}", cwd=path, check=False)

    if local.returncode != 0:
        click.echo(f"{name}: no local branch")
        return
    if remote_ref.returncode != 0:
        click.echo(f"{name}: no remote branch (local only)")
        return

    local_sha = local.stdout.strip()
    remote_sha = remote_ref.stdout.strip()

    if local_sha == remote_sha:
        click.echo(f"{name}: up to date")
        return

    ahead = run_git(
        "rev-list", "--count", f"{remote}/{branch}..{branch}", cwd=path, check=False
    )
    behind = run_git(
        "rev-list", "--count", f"{branch}..{remote}/{branch}", cwd=path, check=False
    )
    a = int(ahead.stdout.strip()) if ahead.returncode == 0 else 0
    b = int(behind.stdout.strip()) if behind.returncode == 0 else 0

    if a > 0 and b > 0:
        click.echo(f"{name}: DIVERGED (ahead {a}, behind {b})")
    elif a > 0:
        click.echo(f"{name}: ahead {a}")
    elif b > 0:
        click.echo(f"{name}: behind {b}")


def _in_reflog(path, branch, sha):
    """True if `branch` once pointed at `sha` on this host."""
    reflog = run_git("reflog", "show", "--format=%H", branch, cwd=path, check=False)
    return reflog.returncode == 0 and sha in reflog.stdout.split()


def push_repo(repo, webhook_url=None):
    """Force the remote to the local branch, for when local was rewound or
    rewritten on purpose and sync would otherwise pull the dropped commits
    back. Refuses unless the remote tip is one this host's branch once had,
    so commits published by another host are never dropped. Other hosts
    still holding the dropped commits push them back on their next sync
    unless they are reset too. Returns True on success."""
    path = repo["path"]
    remote = repo["remote"]
    remote_url = repo.get("remoteUrl")
    bundle_file = repo.get("bundleFile")
    branch = repo["branch"]
    display = repo.get("name") or os.path.basename(path)
    name = f"{display} ({remote}/{branch})"

    if remote_url is None and bundle_file is None:
        click.echo(f"{name}: local-only, nothing to push", err=True)
        return False
    if repo.get("syncMode", "pull-push") == "pull-only":
        click.echo(f"{name}: pull-only, not pushing", err=True)
        return False
    if has_git_lock(path):
        click.echo(f"{name}: git lock file exists, retry later", err=True)
        return False

    local = run_git("rev-parse", "--verify", branch, cwd=path, check=False)
    if local.returncode != 0:
        click.echo(f"{name}: no local branch {branch}", err=True)
        return False
    local_sha = local.stdout.strip()

    # The remote tip as it is now, not as last fetched: the sync timer
    # fetches in the background, so the tracking ref may hold commits
    # this host never had.
    missing = False
    if bundle_file is not None:
        state = bundle_state(bundle_file, name, webhook_url)
        if state == MISSING:
            # Check against the tip last fetched, so a bundle that briefly
            # vanished is not replaced by an older one.
            missing = True
            known = run_git(
                "rev-parse", "--verify", f"{remote}/{branch}", cwd=path, check=False
            )
            remote_sha = known.stdout.strip() if known.returncode == 0 else None
        elif state != READY:
            click.echo(f"{name}: bundle {state}, retry later", err=True)
            return False
        else:
            remote_sha = bundle_tip(bundle_file, branch)
            if remote_sha is None:
                click.echo(f"{name}: bundle unreadable or missing {branch}", err=True)
                return False
    else:
        result = run_git(
            "ls-remote",
            "--heads",
            remote,
            f"refs/heads/{branch}",
            cwd=path,
            check=False,
        )
        if result.returncode != 0:
            click.echo(f"{name}: ls-remote failed — {result.stderr.strip()}", err=True)
            return False
        remote_sha = result.stdout.split()[0] if result.stdout.strip() else None

    if remote_sha == local_sha and not missing:
        click.echo(f"{name}: OK (already at {local_sha[:8]})", err=True)
        return True
    if remote_sha is not None and not _in_reflog(path, branch, remote_sha):
        click.echo(
            f"{name}: remote tip {remote_sha[:8]} was never local here, "
            "pull it first or resolve manually",
            err=True,
        )
        return False

    if bundle_file is not None:
        pub_ok, _ = publish_bundle(
            path,
            branch,
            remote,
            bundle_file,
            local_sha,
            remote_sha,
            name,
            webhook_url,
            missing=missing,
            force=True,
        )
        if not pub_ok:
            return False
    else:
        lease = f"--force-with-lease={branch}:{remote_sha or ''}"
        result = run_git("push", lease, remote, branch, cwd=path, check=False)
        if result.returncode != 0:
            click.echo(f"{name}: push failed — {result.stderr.strip()}", err=True)
            return False

    old = remote_sha[:8] if remote_sha else "nothing"
    click.echo(f"{name}: OK (forced {old} -> {local_sha[:8]})", err=True)
    return True
