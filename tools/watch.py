"""Polling file watcher behind `tools watch`."""

import fnmatch
import json
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
IGNORE_FILE = ".tools-watch.json"
BUILTIN_SKIP_DIRS = {".git"}
TEMP_PREFIX = ".tools-"

_ignore_cache: dict = {}


def wlog(message: str, color: str = "") -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log(f"{stamp} {LOG_PREFIX} {message}", color)


def load_ignores(root: str) -> list[str]:
    """Return `ignore_patterns` from `<root>/.tools-watch.json`, re-read when it changes.

    A missing file means no patterns; an unreadable or invalid one is warned
    about once per version and also means none.
    """
    path = os.path.join(root, IGNORE_FILE)
    try:
        st = os.stat(path)
    except OSError:
        _ignore_cache.pop(path, None)
        return []
    key = (st.st_mtime_ns, st.st_size, st.st_ino)
    cached = _ignore_cache.get(path)
    if cached and cached[0] == key:
        return cached[1]
    try:
        with open(path) as f:
            raw = json.load(f).get("ignore_patterns", [])
        if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
            raise ValueError("ignore_patterns must be a list of strings")
        patterns = [p.rstrip("/") for p in raw if p.rstrip("/")]
    except (OSError, ValueError, AttributeError) as exc:
        wlog(f"Ignoring invalid {path}: {exc}", Color.YELLOW)
        patterns = []
    _ignore_cache[path] = (key, patterns)
    return patterns


def is_ignored(rel: str, patterns) -> bool:
    """Match a root-relative path the way nix-config's rebuild watch does.

    A plain name matches any path component; a pattern with `*` is a glob
    matched against the relative path and against every trailing sub-path.
    """
    parts = rel.split(os.sep)
    for pattern in patterns:
        if "*" in pattern:
            if any(fnmatch.fnmatchcase("/".join(parts[i:]), pattern) for i in range(len(parts))):
                return True
        elif pattern in parts:
            return True
    return False


def snapshot(paths) -> dict:
    """Map every file under `paths` to its identity; missing paths are absent.

    Symlinked directories are followed, as the config loader follows them;
    each directory is walked once, so link loops end. `.git`, tools' own
    `.tools-*` temp files, and paths matching the root's `.tools-watch.json`
    `ignore_patterns` are left out.
    """
    result: dict = {}
    for root in paths:
        root = os.path.realpath(root)
        if not os.path.isdir(root):
            _stat_into(result, root)
            continue
        patterns = load_ignores(root)
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
            reldir = os.path.relpath(dirpath, root)
            reldir = "" if reldir == "." else reldir

            def rel(name, reldir=reldir):
                return os.path.join(reldir, name) if reldir else name

            dirnames[:] = sorted(
                d
                for d in dirnames
                if d not in BUILTIN_SKIP_DIRS and not is_ignored(rel(d), patterns)
            )
            for name in filenames:
                if not name.startswith(TEMP_PREFIX) and not is_ignored(rel(name), patterns):
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
