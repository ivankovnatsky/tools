"""Who tools acts for: the login user, or root via `sudo tools deploy` on that user's behalf.

Under sudo, root does only system work (`files` system entries, launchd daemons,
`defaults` system scope), keeps its own state in `ROOT_STATE_FILE` and never writes in
the user's home; everything else runs in a forked child that has permanently dropped to
the user (`run_as_user`). tools itself never calls sudo.
"""

import os
import pwd
import stat
import sys
import tempfile
import traceback
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

ROOT_STATE_FILE = "/var/db/tools/state.json"
ROOT_SECTIONS = ("launchd", "defaults", "files")

SUDO_USER_ERROR = (
    "tools must not run as root directly; run it as your user, "
    "or through sudo (`sudo tools deploy`)"
)


class SudoUserError(Exception):
    """Running as root without a non-root user to act for."""


@dataclass(frozen=True)
class Invoker:
    root: bool
    uid: int
    home: str
    login: Optional[str]
    gid: Optional[int] = None
    shell: Optional[str] = None


def invoker(
    euid: Optional[int] = None,
    environ: Optional[Mapping[str, str]] = None,
    getpwnam: Optional[Callable] = None,
    getpwuid: Optional[Callable] = None,
) -> Invoker:
    """The user tools acts for. As root that is SUDO_USER, which must resolve to a non-root
    uid that matches SUDO_UID when set; otherwise SudoUserError."""
    euid = os.geteuid() if euid is None else euid
    environ = os.environ if environ is None else environ
    getpwnam = getpwnam or pwd.getpwnam
    getpwuid = getpwuid or pwd.getpwuid
    if euid != 0:
        uid = os.getuid()
        try:
            pw = getpwuid(uid)
            return Invoker(False, uid, pw.pw_dir, pw.pw_name)
        except KeyError:
            return Invoker(False, uid, os.path.expanduser("~"), None)
    name = environ.get("SUDO_USER")
    if not name or name == "root":
        raise SudoUserError(SUDO_USER_ERROR)
    try:
        pw = getpwnam(name)
    except KeyError:
        raise SudoUserError(f"{SUDO_USER_ERROR} (SUDO_USER {name!r} is not a known user)") from None
    if pw.pw_uid == 0:
        raise SudoUserError(SUDO_USER_ERROR)
    sudo_uid = environ.get("SUDO_UID")
    if sudo_uid is not None and sudo_uid != str(pw.pw_uid):
        raise SudoUserError(
            f"{SUDO_USER_ERROR} (SUDO_UID {sudo_uid} is not {name}'s uid {pw.pw_uid})"
        )
    return Invoker(True, pw.pw_uid, pw.pw_dir, pw.pw_name, pw.pw_gid, pw.pw_shell)


# User-controlled settings that would steer root's temp files or child processes.
UNSAFE_ENV = (
    "TMPDIR",
    "TEMP",
    "TMP",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "BASH_ENV",
    "ENV",
)
_UNSAFE_PREFIXES = ("DYLD_", "LD_")


def act_as(inv: Invoker) -> None:
    """Make `~`, `$HOME` and `${env:HOME}` resolve to the invoking user's, as without sudo,
    and drop user-controlled temp dir and loader settings from root's environment."""
    if not inv.root:
        return
    for name in list(os.environ):
        if name in UNSAFE_ENV or name.startswith(_UNSAFE_PREFIXES):
            del os.environ[name]
    tempfile.tempdir = None
    os.environ["HOME"] = inv.home
    if inv.login:
        os.environ["USER"] = inv.login
        os.environ["LOGNAME"] = inv.login


def under_home(path: str, home: Optional[str]) -> bool:
    """path is the user's home or inside it, lexically or after resolving symlinks.

    Case-insensitive, as macOS volumes usually are; on a case-sensitive one this
    errs toward refusing.
    """
    if not home or not path:
        return False
    homes = {os.path.normpath(home).casefold(), os.path.realpath(home).casefold()}
    paths = {os.path.normpath(path).casefold(), os.path.realpath(path).casefold()}
    for p in paths:
        for h in homes:
            if p == h or p.startswith(h.rstrip(os.sep) + os.sep):
                return True
    return False


StatFn = Callable[[str], os.stat_result]


def root_only(path: str, stat_fn: StatFn = os.stat, lstat_fn: StatFn = os.lstat) -> bool:
    """Only root can change what path runs: every component of the original path is
    root-owned (non-symlinks also not group/world-writable), and so is its canonical target."""
    if not os.path.isabs(path):
        return False
    current = "/"
    for part in [""] + [c for c in os.path.normpath(path).split(os.sep) if c]:
        current = os.path.join(current, part) if part else current
        try:
            st = lstat_fn(current)
        except OSError:
            return False
        if st.st_uid != 0:
            return False
        if not stat.S_ISLNK(st.st_mode) and st.st_mode & 0o022:
            return False
    current = os.path.realpath(path)
    while True:
        try:
            st = stat_fn(current)
        except OSError:
            return False
        if st.st_uid != 0 or st.st_mode & 0o022:
            return False
        parent = os.path.dirname(current)
        if parent == current:
            return True
        current = parent


def existing_ancestor(path: str) -> str:
    current = os.path.normpath(path)
    while not os.path.lexists(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return current


def root_dir_error(
    path: str, stat_fn: StatFn = os.stat, lstat_fn: StatFn = os.lstat
) -> Optional[str]:
    """Why root must not create or write inside dir `path`: its deepest existing component
    (and every component above it) must be root-owned and not group/world-writable."""
    existing = existing_ancestor(path)
    if not root_only(existing, stat_fn, lstat_fn):
        return f"{existing} is not root-only (root-owned, not group/world-writable)"
    return None


def root_file_error(
    path: str, stat_fn: StatFn = os.stat, lstat_fn: StatFn = os.lstat
) -> Optional[str]:
    """Why root must not write file `path`: it is a symlink, or a non-root user can change
    its directory (see `root_dir_error`)."""
    try:
        if stat.S_ISLNK(lstat_fn(path).st_mode):
            return f"{path} is a symlink"
    except OSError:
        pass
    return root_dir_error(os.path.dirname(os.path.normpath(path)), stat_fn, lstat_fn)


# Set in the child `run_as_user` forks: root already handled the system parts.
UNDER_SUDO = False


class PrivilegeError(Exception):
    """Dropping to the invoking user did not stick."""


# Prepended to the PATH sudo passed when missing from it (existing dirs only), so the
# user run finds brew, uv tools, ... as the user's shell would; `{home}` is the user's.
USER_PATH_DIRS = (
    "{home}/.local/bin",
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
)
BASE_PATH_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


def user_path(
    inv: Invoker, current: Optional[str], isdir: Callable[[str], bool] = os.path.isdir
) -> str:
    """The PATH sudo passed, with the user's usual tool dirs added in front and the
    base system dirs at the end, each only when missing."""
    have = [d for d in (current or "").split(os.pathsep) if d]
    extra = [d.format(home=inv.home) for d in USER_PATH_DIRS]
    front = [d for d in dict.fromkeys(extra) if d not in have and isdir(d)]
    back = [d for d in BASE_PATH_DIRS if d not in have]
    return os.pathsep.join(front + have + back)


def drop_privileges(inv: Invoker, ops=os) -> None:
    """Become inv's uid, gid and groups for good; PrivilegeError if root is still reachable."""
    if inv.gid is None:
        raise PrivilegeError(f"no primary group for uid {inv.uid}")
    if inv.login:
        ops.initgroups(inv.login, inv.gid)
    else:
        ops.setgroups([inv.gid])
    ops.setgid(inv.gid)
    ops.setuid(inv.uid)
    ids = (ops.getuid(), ops.geteuid(), ops.getgid(), ops.getegid())
    if ids != (inv.uid, inv.uid, inv.gid, inv.gid):
        raise PrivilegeError(f"still uid/euid/gid/egid {ids} after dropping to {inv.uid}")
    try:
        ops.setuid(0)
    except OSError:
        return
    raise PrivilegeError("setuid(0) succeeded after dropping privileges")


def user_environ(inv: Invoker, environ=None) -> None:
    """The environment a plain run as inv would see: their HOME/USER/LOGNAME/SHELL and
    PATH (see `user_path`), no sudo or temp-dir variables."""
    environ = os.environ if environ is None else environ
    for name in list(environ):
        if name in UNSAFE_ENV or name.startswith("SUDO_"):
            del environ[name]
    environ["HOME"] = inv.home
    environ["PATH"] = user_path(inv, environ.get("PATH"))
    if inv.login:
        environ["USER"] = environ["LOGNAME"] = inv.login
    if inv.shell:
        environ["SHELL"] = inv.shell
    tempfile.tempdir = None


def as_user(inv: Invoker, fn: Callable[[], int], ops=os) -> int:
    """Drop to inv and run fn; the exit code of that run. fn never runs if the drop fails."""
    global UNDER_SUDO
    try:
        drop_privileges(inv, ops)
    except (PrivilegeError, OSError) as e:
        print(f"Cannot drop privileges to {inv.login or inv.uid}: {e}", file=sys.stderr)
        return 1
    try:
        user_environ(inv)
        UNDER_SUDO = True
        return fn()
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except BaseException:
        traceback.print_exc()
    return 1


def run_as_user(inv: Invoker, fn: Callable[[], int]) -> int:
    """Run fn in a forked child that has permanently dropped to inv; its exit code."""
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            code = as_user(inv, fn)
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)
