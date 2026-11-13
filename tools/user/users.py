"""Expected uid and login shell of existing macOS user accounts.

Config shape::

    users:
      ivan:
        uid: 502                        # checked only; a mismatch is reported
        shell: /opt/homebrew/bin/fish   # set with dscl when it differs

Root only (`sudo tools deploy`). tools never creates, deletes or renumbers
accounts. A user whose uid differs is reported and left alone, shell included.
The shell must be listed in /etc/shells and be executable by that user; a
shell not installed yet is reported and skipped, so the user phase can still
install it. Without sudo,
`deploy` and `diff` read the accounts and print a note. A user that leaves
config is left as is.
"""

import os
import pwd
import stat
import sys
from typing import Callable, Dict, List, Optional, Tuple

from tools import privilege
from tools.log import Color, log
from tools.util import run_command

KEYS = ("uid", "shell")
SHELLS_FILE = "/etc/shells"
DSCL = "/usr/bin/dscl"
ROOT_HINT = "run sudo tools deploy --scope users"


def _name_error(name) -> Optional[str]:
    if not isinstance(name, str) or not name.strip() or name != name.strip():
        return "must be a non-empty user name"
    if name.startswith("-") or any(c in name for c in "/:\n"):
        return "must not start with '-' or contain '/', ':' or a newline"
    return None


def _entry_errors(entry) -> List[str]:
    if not isinstance(entry, dict) or not entry:
        return [f"must be a mapping with {' and/or '.join(KEYS)}"]
    errors = []
    unknown = set(entry) - set(KEYS)
    if unknown:
        errors.append(f"unknown keys {sorted(unknown, key=str)} (expected {list(KEYS)})")
    uid = entry.get("uid")
    if "uid" in entry and (isinstance(uid, bool) or not isinstance(uid, int) or uid < 0):
        errors.append("uid: must be a non-negative integer")
    shell = entry.get("shell")
    if "shell" in entry and (
        not isinstance(shell, str) or not os.path.isabs(shell) or "\n" in shell
    ):
        errors.append("shell: must be an absolute path")
    return errors


def parse(raw) -> Tuple[Dict[str, Dict], List[str]]:
    """Validate `users`. Returns ({name: {uid?, shell?}}, errors); any error holds the
    whole section."""
    if raw is None:
        return {}, []
    if not isinstance(raw, dict):
        return {}, [f"users must be a mapping of user names, got {type(raw).__name__}"]
    errors = []
    for name, entry in raw.items():
        why = _name_error(name)
        if why:
            errors.append(f"{name!r}: {why}")
            continue
        errors += [f"{name}: {e}" for e in _entry_errors(entry)]
    return ({} if errors else dict(raw)), errors


def read_shells(path: str = SHELLS_FILE) -> List[str]:
    with open(path, encoding="utf-8") as f:
        return [s for s in (line.split("#", 1)[0].strip() for line in f) if s]


def _shell_error(path: str, pw) -> Optional[str]:
    """None when pw's user can run path; "missing" when it does not exist; else why not."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return "is not a file"
    except FileNotFoundError:
        return "missing"
    except OSError as e:
        return f"cannot be checked: {e.strerror}"
    inv = privilege.Invoker(False, pw.pw_uid, pw.pw_dir, pw.pw_name, pw.pw_gid)
    real = os.path.realpath(path)
    # Both the configured path's dirs and the resolved target's must be searchable.
    checks = [real, *_ancestors(real), *_ancestors(os.path.normpath(path))]
    for current in dict.fromkeys(checks):
        try:
            mode = privilege.user_mode(os.stat(current), inv)
        except OSError as e:
            return f"cannot be checked: {current}: {e.strerror}"
        if not mode & 1:
            return f"is not executable by {pw.pw_name} ({current})"
    return None


def _ancestors(path: str) -> List[str]:
    out = []
    while True:
        parent = os.path.dirname(path)
        if parent == path:
            return out
        out.append(parent)
        path = parent


def local_uid(name: str, run: Optional[Callable] = None) -> Optional[int]:
    """uid of name's record in the local directory node (the one `dscl .` writes), or None."""
    rc, stdout, _ = (run or run_command)([DSCL, ".", "-read", f"/Users/{name}", "UniqueID"])
    if rc != 0:
        return None
    value = stdout.partition(":")[2].strip()
    return int(value) if value.lstrip("-").isdigit() else None


def plan(
    spec: Dict[str, Dict],
    getpwnam: Callable = pwd.getpwnam,
    shells: Optional[Callable[[], List[str]]] = None,
    shell_error: Optional[Callable] = None,
    local: Optional[Callable[[str], Optional[int]]] = None,
) -> Tuple[List[Tuple[str, str, str]], List[str], List[str]]:
    """([(diff row, user, shell to set)], uid warnings, errors); reads only."""
    shells = shells or read_shells
    shell_error = shell_error or _shell_error
    local = local or local_uid
    todo: List[Tuple[str, str, str]] = []
    warnings: List[str] = []
    errors: List[str] = []
    allowed: Optional[List[str]] = None
    for name, entry in spec.items():
        try:
            pw = getpwnam(name)
        except KeyError:
            errors.append(f"{name}: no such user (tools does not create users)")
            continue
        if "uid" in entry and pw.pw_uid != entry["uid"]:
            warnings.append(
                f"{name}: uid is {pw.pw_uid}, config says {entry['uid']};"
                " tools never changes uids, skipping this user"
            )
            continue
        want = entry.get("shell")
        if want is None or pw.pw_shell == want:
            continue
        if allowed is None:
            try:
                allowed = shells()
            except OSError as e:
                errors.append(f"{name}: cannot read {SHELLS_FILE}: {e}")
                allowed = []
                continue
        if want not in allowed:
            errors.append(f"{name}: shell {want} is not listed in {SHELLS_FILE}")
            continue
        if local(name) != pw.pw_uid:
            errors.append(f"{name}: not a local account; only local accounts are changed")
            continue
        why = shell_error(want, pw)
        if why == "missing":
            warnings.append(f"{name}: shell {want} is not installed yet; leaving the shell as is")
            continue
        if why:
            errors.append(f"{name}: shell {want} {why}")
            continue
        todo.append((f"  ~ {name}: login shell {pw.pw_shell or '(none)'} -> {want}", name, want))
    return todo, warnings, errors


def has_work(raw) -> bool:
    return raw is not None


def _is_root() -> bool:
    return os.geteuid() == 0


def _note_as_user(spec: Dict[str, Dict], getpwnam: Callable) -> None:
    if privilege.UNDER_SUDO:
        return
    todo, warnings, errors = plan(spec, getpwnam)
    if todo:
        log(f"users: {len(todo)} login shell(s) differ; {ROOT_HINT}", Color.YELLOW)
    for msg in warnings + errors:
        log(f"users: {msg}", Color.YELLOW)


def diff_users(raw, root: Optional[bool] = None, getpwnam: Callable = pwd.getpwnam) -> List[str]:
    if sys.platform != "darwin":
        return []
    spec, errors = parse(raw)
    if errors:
        return [f"  ! {err}" for err in errors]
    root = _is_root() if root is None else root
    if not root:
        _note_as_user(spec, getpwnam)
        return []
    todo, warnings, read_errors = plan(spec, getpwnam)
    return (
        [f"  ! {e}" for e in read_errors]
        + [f"  ? {w}" for w in warnings]
        + [row for row, _, _ in todo]
    )


def install_users(
    raw,
    root: Optional[bool] = None,
    getpwnam: Callable = pwd.getpwnam,
    run: Optional[Callable] = None,
) -> bool:
    if sys.platform != "darwin":
        log("users: skipping, not macOS", Color.YELLOW)
        return True
    spec, errors = parse(raw)
    for err in errors:
        log(f"users: {err}", Color.RED)
    if errors:
        return False
    root = _is_root() if root is None else root
    if not root:
        _note_as_user(spec, getpwnam)
        return True
    todo, warnings, read_errors = plan(spec, getpwnam)
    for msg in warnings:
        log(f"users: {msg}", Color.YELLOW)
    for msg in read_errors:
        log(f"users: {msg}", Color.RED)
    success = not read_errors
    run = run or run_command
    for _row, name, shell in todo:
        rc, stdout, stderr = run([DSCL, ".", "-create", f"/Users/{name}", "UserShell", shell])
        if rc != 0:
            detail = (stderr or stdout).strip() or f"exit {rc}"
            log(f"users: setting {name}'s login shell failed: {detail}", Color.RED)
            success = False
        else:
            log(f"users: {name} login shell set to {shell}", Color.GREEN)
    return success
