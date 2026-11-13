"""Expected uid and login shell of macOS user accounts; creates and deletes opted-in ones.

Config shape::

    users:
      ivan:
        uid: 502                        # checked only; a mismatch is reported
        shell: /opt/homebrew/bin/fish   # set with dscl when it differs
      guest:
        uid: 600                        # required with create, at least 501
        create: true                    # create the account when missing
        deleteHome: false               # on deletion, also remove its home

Root only (`sudo tools deploy`). tools never renumbers accounts. A user whose
uid differs is reported and left alone, shell included. The shell must be
listed in /etc/shells and be executable by that user; a shell not installed
yet is reported and skipped, so the user phase can still install it.

A missing `create: true` account is created with dscl, password disabled
(`*`), primary group staff, home /Users/NAME made by createhomedir. Creation
is refused when the name, uid or home path is taken. Created accounts are
recorded in state; once one leaves config it is deleted (`dscl . -delete`),
keeping its home unless `deleteHome: true` was deployed while it was still in
config. Unrecorded accounts, root, uids below 501, the current user and
SUDO_USER are never deleted. A user tools did not create that leaves config
is left as is. Without sudo, `deploy` and `diff` read the accounts and print
a note.
"""

import os
import pwd
import stat
import sys
from typing import Callable, Dict, List, Optional, Tuple

from tools import privilege
from tools.log import Color, log
from tools.util import run_command

KEYS = ("uid", "shell", "create", "deleteHome")
SHELLS_FILE = "/etc/shells"
DSCL = "/usr/bin/dscl"
CREATEHOMEDIR = "/usr/sbin/createhomedir"
RM = "/bin/rm"
MIN_UID = 501
STAFF_GID = 20
DEFAULT_SHELL = "/bin/zsh"
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
    for key in ("create", "deleteHome"):
        if key in entry and not isinstance(entry[key], bool):
            errors.append(f"{key}: must be true or false")
    if entry.get("create") is True:
        if not isinstance(uid, int) or isinstance(uid, bool) or uid < MIN_UID:
            errors.append(f"create: needs uid, at least {MIN_UID}")
    elif "deleteHome" in entry:
        errors.append("deleteHome: only applies with create: true")
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
    seen: Dict[int, str] = {}
    for name, entry in raw.items():
        if isinstance(entry, dict) and entry.get("create") is True and "uid" in entry:
            other = seen.setdefault(entry["uid"], name)
            if other != name:
                errors.append(f"{name}: uid {entry['uid']} is also {other}'s")
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
            if not entry.get("create"):
                errors.append(f"{name}: no such user (set create: true to create it)")
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


def _home(name: str) -> str:
    return f"/Users/{name}"


def created(state: Optional[Dict]) -> Dict[str, Dict]:
    """Accounts tools created, as recorded in state: {name: {uid, home, deleteHome}}."""
    section = (state or {}).get("users")
    records = section.get("created") if isinstance(section, dict) else None
    return records if isinstance(records, dict) else {}


def _protected(name: str, uid: int) -> Optional[str]:
    """Why name/uid must never be deleted, or None."""
    if name == "root" or uid == 0:
        return "is root"
    if uid < MIN_UID:
        return f"has uid {uid}, below {MIN_UID}"
    if uid == os.getuid():
        return "is the current user"
    try:
        inv = privilege.invoker()
    except privilege.SudoUserError:
        return "cannot be checked against SUDO_USER"
    if inv.login == name or inv.uid == uid:
        return "is the invoking user (SUDO_USER)"
    return None


def plan_create(
    spec: Dict[str, Dict],
    getpwnam: Callable = pwd.getpwnam,
    getpwuid: Callable = pwd.getpwuid,
    shells: Optional[Callable[[], List[str]]] = None,
    local: Optional[Callable[[str], Optional[int]]] = None,
    exists: Optional[Callable[[str], bool]] = None,
) -> Tuple[List[Tuple[str, str, int, str]], List[str]]:
    """([(diff row, user, uid, shell)], errors) for missing `create: true` users; reads only."""
    shells = shells or read_shells
    local = local or local_uid
    exists = exists or os.path.lexists
    todo: List[Tuple[str, str, int, str]] = []
    errors: List[str] = []
    allowed: Optional[List[str]] = None
    for name, entry in spec.items():
        if not entry.get("create"):
            continue
        try:
            getpwnam(name)
            continue
        except KeyError:
            pass
        uid = entry["uid"]
        try:
            other = getpwuid(uid)
            errors.append(f"{name}: uid {uid} is taken by {other.pw_name}; not creating")
            continue
        except KeyError:
            pass
        if local(name) is not None:
            errors.append(f"{name}: name is taken in the local directory; not creating")
            continue
        home = _home(name)
        if exists(home):
            errors.append(f"{name}: {home} already exists; not creating")
            continue
        shell = entry.get("shell", DEFAULT_SHELL)
        if allowed is None:
            try:
                allowed = shells()
            except OSError as e:
                errors.append(f"{name}: cannot read {SHELLS_FILE}: {e}")
                allowed = []
                continue
        if shell not in allowed:
            errors.append(f"{name}: shell {shell} is not listed in {SHELLS_FILE}")
            continue
        row = f"  + create account {name} (uid {uid}, shell {shell}, home {home}, no password)"
        todo.append((row, name, uid, shell))
    return todo, errors


def plan_delete(
    spec: Dict[str, Dict],
    state: Optional[Dict],
    getpwnam: Callable = pwd.getpwnam,
    local: Optional[Callable[[str], Optional[int]]] = None,
    protected: Callable[[str, int], Optional[str]] = _protected,
) -> Tuple[List[Tuple[str, str, Optional[Dict]]], List[str], List[str]]:
    """([(diff row, user, record or None to only forget it)], warnings, errors) for recorded
    accounts that left config; reads only."""
    local = local or local_uid
    todo: List[Tuple[str, str, Optional[Dict]]] = []
    warnings: List[str] = []
    errors: List[str] = []
    for name, rec in created(state).items():
        if name in spec:
            continue
        uid = rec.get("uid") if isinstance(rec, dict) else None
        if isinstance(uid, bool) or not isinstance(uid, int) or rec.get("home") != _home(name):
            errors.append(f"{name}: malformed state record; not deleting")
            continue
        why = protected(name, uid)
        if why:
            errors.append(f"{name}: {why}; never deleted")
            continue
        try:
            pw = getpwnam(name)
        except KeyError:
            todo.append((f"  - forget {name} (account already gone)", name, None))
            continue
        if pw.pw_uid != uid:
            warnings.append(
                f"{name}: uid is {pw.pw_uid}, tools created it with {uid}; not deleting"
            )
            continue
        if local(name) != uid:
            errors.append(f"{name}: not a local account; not deleting")
            continue
        home = "remove" if rec.get("deleteHome") is True else "keep"
        todo.append(
            (f"  - delete account {name} (uid {uid}), {home} home {rec['home']}", name, rec)
        )
    return todo, warnings, errors


def has_work(raw, state: Optional[Dict] = None) -> bool:
    return raw is not None or bool(created(state))


def _is_root() -> bool:
    return os.geteuid() == 0


def _note_as_user(spec: Dict[str, Dict], getpwnam: Callable) -> None:
    if privilege.UNDER_SUDO:
        return
    todo, warnings, errors = plan(spec, getpwnam)
    if todo:
        log(f"users: {len(todo)} login shell(s) differ; {ROOT_HINT}", Color.YELLOW)
    missing = [n for n, e in spec.items() if e.get("create") and not _exists(n, getpwnam)]
    if missing:
        log(f"users: {len(missing)} account(s) to create; {ROOT_HINT}", Color.YELLOW)
    for msg in warnings + errors:
        log(f"users: {msg}", Color.YELLOW)


def _exists(name: str, getpwnam: Callable) -> bool:
    try:
        getpwnam(name)
        return True
    except KeyError:
        return False


def diff_users(
    raw,
    state: Optional[Dict] = None,
    root: Optional[bool] = None,
    getpwnam: Callable = pwd.getpwnam,
    getpwuid: Callable = pwd.getpwuid,
) -> List[str]:
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
    deletes, del_warnings, del_errors = plan_delete(spec, state, getpwnam)
    creates, create_errors = plan_create(spec, getpwnam, getpwuid)
    return (
        [f"  ! {e}" for e in read_errors + del_errors + create_errors]
        + [f"  ? {w}" for w in warnings + del_warnings]
        + [row for row, _, _ in deletes]
        + [row for row, _, _, _ in creates]
        + [row for row, _, _ in todo]
    )


def _fail(run: Callable, cmd: List[str]) -> Optional[str]:
    rc, stdout, stderr = run(cmd)
    if rc == 0:
        return None
    return (stderr or stdout).strip() or f"exit {rc}"


def _create(
    name: str, uid: int, shell: str, delete_home: bool, records: Dict, run: Callable
) -> bool:
    record = f"/Users/{name}"
    home = _home(name)
    steps = [
        [DSCL, ".", "-create", record],
        [DSCL, ".", "-create", record, "Password", "*"],
        [DSCL, ".", "-create", record, "UniqueID", str(uid)],
        [DSCL, ".", "-create", record, "PrimaryGroupID", str(STAFF_GID)],
        [DSCL, ".", "-create", record, "UserShell", shell],
        [DSCL, ".", "-create", record, "NFSHomeDirectory", home],
        [CREATEHOMEDIR, "-c", "-u", name],
    ]
    for i, cmd in enumerate(steps):
        why = _fail(run, cmd)
        if why:
            log(f"users: creating {name} failed at {' '.join(cmd[:5])}: {why}", Color.RED)
            return False
        if i == 0:
            records[name] = {"uid": uid, "home": home, "deleteHome": delete_home}
    log(f"users: created {name} (uid {uid}, no password)", Color.GREEN)
    return True


def _remove_home(name: str, rec: Dict, run: Callable) -> bool:
    home = rec["home"]
    try:
        st = os.lstat(home)
    except FileNotFoundError:
        return True
    except OSError as e:
        log(f"users: {name}: cannot check {home}: {e.strerror}; keeping it", Color.RED)
        return False
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != rec["uid"]:
        log(
            f"users: {name}: {home} is not a directory owned by uid {rec['uid']}; keeping it",
            Color.RED,
        )
        return False
    why = _fail(run, [RM, "-rf", "--", home])
    if why:
        log(f"users: removing {home} failed: {why}", Color.RED)
        return False
    log(f"users: removed {home}", Color.GREEN)
    return True


def _delete(name: str, rec: Optional[Dict], records: Dict, run: Callable) -> bool:
    if rec is None:
        log(f"users: {name} is already gone; forgetting it", Color.YELLOW)
        records.pop(name, None)
        return True
    why = _fail(run, [DSCL, ".", "-delete", f"/Users/{name}"])
    if why:
        log(f"users: deleting {name} failed: {why}", Color.RED)
        return False
    records.pop(name, None)
    log(f"users: deleted {name}", Color.GREEN)
    if rec.get("deleteHome") is True:
        return _remove_home(name, rec, run)
    return True


def install_users(
    raw,
    state: Optional[Dict] = None,
    root: Optional[bool] = None,
    getpwnam: Callable = pwd.getpwnam,
    getpwuid: Callable = pwd.getpwuid,
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
    run = run or run_command
    success = True
    if state is None:
        creates, create_errors, deletes = [], [], []
        if any(e.get("create") for e in spec.values()):
            create_errors = ["no state to record created accounts in; not creating"]
    else:
        deletes, del_warnings, del_errors = plan_delete(spec, state, getpwnam)
        for msg in del_warnings:
            log(f"users: {msg}", Color.YELLOW)
        for msg in del_errors:
            log(f"users: {msg}", Color.RED)
        success &= not del_errors
        creates, create_errors = plan_create(spec, getpwnam, getpwuid)
    for msg in create_errors:
        log(f"users: {msg}", Color.RED)
    success &= not create_errors
    if state is not None:
        records = state.setdefault("users", {}).setdefault("created", {})
        for name, rec in records.items():
            if name in spec and isinstance(rec, dict):
                rec["deleteHome"] = spec[name].get("deleteHome") is True
        for _row, name, rec in deletes:
            success &= _delete(name, rec, records, run)
        for _row, name, uid, shell in creates:
            delete_home = spec[name].get("deleteHome") is True
            success &= _create(name, uid, shell, delete_home, records, run)
    todo, warnings, read_errors = plan(spec, getpwnam)
    for msg in warnings:
        log(f"users: {msg}", Color.YELLOW)
    for msg in read_errors:
        log(f"users: {msg}", Color.RED)
    success &= not read_errors
    for _row, name, shell in todo:
        why = _fail(run, [DSCL, ".", "-create", f"/Users/{name}", "UserShell", shell])
        if why:
            log(f"users: setting {name}'s login shell failed: {why}", Color.RED)
            success = False
        else:
            log(f"users: {name} login shell set to {shell}", Color.GREEN)
    return success
