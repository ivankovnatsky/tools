"""Polling file watcher behind `tools watch`."""

import os
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time
from datetime import datetime

from tools.log import Color, log

LOG_PREFIX = "[tools watch]"
TMUX_WINDOW = "tools-watch"
MAX_LISTED = 10


def wlog(message: str, color: str = "") -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log(f"{stamp} {LOG_PREFIX} {message}", color)


def snapshot(paths) -> dict:
    """Map every file under `paths` to its identity; missing paths are absent.

    Symlinked directories are followed, as the config loader follows them;
    each directory is walked once, so link loops end. Hidden files and
    directories under a directory root are skipped, as the loader skips them.
    """
    result: dict = {}
    for root in paths:
        root = os.path.realpath(root)
        if not os.path.isdir(root):
            _stat_into(result, root)
            continue
        seen: set = set()
        for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
            try:
                st = os.stat(dirpath)
            except OSError:
                dirnames[:] = []
                continue
            if (st.st_dev, st.st_ino) in seen:
                dirnames[:] = []
                continue
            seen.add((st.st_dev, st.st_ino))
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in filenames:
                if not name.startswith("."):
                    _stat_into(result, os.path.join(dirpath, name))
    return result


def _stat_into(result: dict, path: str) -> None:
    try:
        lst = os.lstat(path)
    except OSError:
        return
    target = None
    if stat.S_ISLNK(lst.st_mode):
        try:
            target = os.readlink(path)
        except OSError:
            return
    try:
        st = os.stat(path)
    except OSError:
        result[path] = (None, None, None, None, None, target)
        return
    result[path] = (st.st_mode, st.st_mtime_ns, st.st_size, st.st_dev, st.st_ino, target)


def is_excluded(path: str, exclude) -> bool:
    """True for a path in `exclude` or one starting with an excluded path plus "."."""
    return any(path == e or path.startswith(e + ".") for e in exclude)


def changed_paths(old: dict, new: dict) -> list[str]:
    return sorted(p for p in old.keys() | new.keys() if old.get(p) != new.get(p))


def watch_loop(
    scan,
    run,
    *,
    poll: float,
    debounce: float,
    clock=time.monotonic,
    sleep=time.sleep,
    should_stop=lambda: False,
    initial_run: bool = True,
    ignore=lambda path: False,
) -> None:
    """Call `run(files)` once changes reported by `scan` settle for `debounce` seconds.

    With `initial_run`, `run([])` is called first. Every baseline is taken
    before the run it precedes, so edits made while a run is in progress
    trigger a follow-up run. Paths for which `ignore` is true (checked on
    every comparison, so it may change between runs) never trigger a run. An
    exception from `run` is logged and watching continues.
    """
    baseline = scan()
    if initial_run:
        safe_run(run, [])
    pending: set[str] = set()
    deadline = 0.0
    while not should_stop():
        sleep(poll)
        current = scan()
        raw = changed_paths(baseline, current)
        baseline = current
        changes = [p for p in raw if not ignore(p)]
        if changes:
            pending.update(changes)
            deadline = clock() + debounce
            wlog(f"Change detected, waiting {debounce:g}s for more changes...", Color.BLUE)
            continue
        if pending and clock() >= deadline:
            files = sorted(pending)
            pending.clear()
            safe_run(run, files)


def safe_run(run, files: list[str]) -> None:
    try:
        run(files)
    except Exception as exc:
        wlog(f"Run crashed: {exc!r}; still watching", Color.RED)


def log_files(files: list[str]) -> None:
    for f in files[:MAX_LISTED]:
        wlog(f"  - {f}")
    if len(files) > MAX_LISTED:
        wlog(f"  ... and {len(files) - MAX_LISTED} more")


def tmux_target(session: str) -> str:
    return f"={session}:={TMUX_WINDOW}"


def run_in_tmux(args: list[str]) -> None:
    """Run `tools <args>` in tmux window `tools-watch` of session <hostname>."""
    tmux = shutil.which("tmux")
    if not tmux:
        wlog("tmux not found in PATH", Color.RED)
        sys.exit(1)

    session = socket.gethostname().split(".")[0].replace(":", "_")
    target = tmux_target(session)
    command = shlex.join([shutil.which("tools") or "tools", *args])
    cwd = os.getcwd()

    def tmux_ok(*tmux_args):
        return subprocess.run([tmux, *tmux_args], capture_output=True).returncode == 0

    if not tmux_ok("has-session", "-t", f"={session}"):
        subprocess.run(
            [tmux, "new-session", "-d", "-s", session, "-n", TMUX_WINDOW, "-c", cwd, command],
            check=True,
        )
    elif tmux_ok("list-panes", "-t", target):
        wlog(f"Already running in tmux {session}:{TMUX_WINDOW}", Color.YELLOW)
    else:
        subprocess.run(
            [tmux, "new-window", "-d", "-t", f"={session}:", "-n", TMUX_WINDOW, "-c", cwd, command],
            check=True,
        )

    if os.environ.get("TMUX"):
        subprocess.run([tmux, "switch-client", "-t", target], check=False)
    else:
        os.execvp(tmux, [tmux, "attach-session", "-t", target])
