"""Who tools acts for: the login user, or root via `sudo tools deploy` on that user's behalf.

Root mode only does system work (launchd daemons, `defaults` system scope),
keeps its own state in `ROOT_STATE_FILE`, and never writes in the user's home.
tools itself never calls sudo.
"""

import os
import pwd
import tempfile
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

ROOT_STATE_FILE = "/var/db/tools/state.json"
ROOT_SECTIONS = ("launchd", "defaults")

SUDO_USER_ERROR = (
    "tools must not run as root directly; run it as your user, "
    "or `sudo tools deploy` for system work"
)


class RootModeError(Exception):
    """Running as root without a non-root user to act for."""


@dataclass(frozen=True)
class Invoker:
    root: bool
    uid: int
    home: str
    login: Optional[str]


def invoker(
    euid: Optional[int] = None,
    environ: Optional[Mapping[str, str]] = None,
    getpwnam: Optional[Callable] = None,
    getpwuid: Optional[Callable] = None,
) -> Invoker:
    """The user tools acts for. As root that is SUDO_USER, which must resolve to a non-root
    uid that matches SUDO_UID when set; otherwise RootModeError."""
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
        raise RootModeError(SUDO_USER_ERROR)
    try:
        pw = getpwnam(name)
    except KeyError:
        raise RootModeError(f"{SUDO_USER_ERROR} (SUDO_USER {name!r} is not a known user)") from None
    if pw.pw_uid == 0:
        raise RootModeError(SUDO_USER_ERROR)
    sudo_uid = environ.get("SUDO_UID")
    if sudo_uid is not None and sudo_uid != str(pw.pw_uid):
        raise RootModeError(
            f"{SUDO_USER_ERROR} (SUDO_UID {sudo_uid} is not {name}'s uid {pw.pw_uid})"
        )
    return Invoker(True, pw.pw_uid, pw.pw_dir, pw.pw_name)


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
