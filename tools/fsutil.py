"""Filesystem helpers shared by the file-producing providers (sops, templates)."""

import errno
import grp
import hashlib
import os
import pwd
import stat
import tempfile
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, Union

_NO_LINK_ERRNOS = {
    errno.EPERM,
    errno.ENOTSUP,
    errno.EOPNOTSUPP,
    errno.EXDEV,
    errno.EMLINK,
    errno.ENOSYS,
}


def parse_mode(value, section: str) -> Optional[int]:
    """Quoted octal string -> int; None stays None. See files._parse_mode for why."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            mode = int(value, 8)
        except ValueError:
            raise ValueError(f"{section}: invalid mode {value!r}") from None
        if not 0 <= mode <= 0o7777:
            raise ValueError(f"{section}: mode {value!r} must be between 0000 and 7777")
        return mode
    raise TypeError(
        f'{section}: mode must be a quoted octal string (e.g. "0600"), got {value!r} '
        f"({type(value).__name__}); unquoted YAML ints are ambiguous"
    )


def check_owner(value, label: str) -> None:
    """None, a non-empty name, or a non-negative int id; bools are rejected."""
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{label} must be a user/group name or a non-negative id, got {value!r}")
    if isinstance(value, int) and value < 0 or isinstance(value, str) and not value:
        raise ValueError(f"{label} must be a user/group name or a non-negative id, got {value!r}")


def resolve_uid(owner: Union[str, int, None]) -> int:
    if owner is None:
        return -1
    check_owner(owner, "owner")
    if isinstance(owner, int) or owner.isdigit():
        return int(owner)
    return pwd.getpwnam(owner).pw_uid


def resolve_gid(group: Union[str, int, None]) -> int:
    if group is None:
        return -1
    check_owner(group, "group")
    if isinstance(group, int) or group.isdigit():
        return int(group)
    return grp.getgrnam(group).gr_gid


@dataclass(frozen=True)
class Current:
    """What is on disk at a path right now."""

    exists: bool
    content: Optional[bytes] = None
    mode: Optional[int] = None
    uid: Optional[int] = None
    gid: Optional[int] = None
    error: Optional[str] = None
    symlink: bool = False


_NOFOLLOW = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK

SYMLINK_REFUSED = "is a symlink; refusing to follow it (tools writes regular files only)"


def _open_regular(path: str) -> int:
    """fd of the regular file at ``path``, never following a final symlink."""
    try:
        fd = os.open(path, _NOFOLLOW)
    except OSError as e:
        if os.path.islink(path):
            raise OSError(errno.ELOOP, SYMLINK_REFUSED, path) from None
        raise e
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(errno.EINVAL, "not a regular file", path)
    return fd


def read_current(path: str) -> Current:
    if not os.path.lexists(path):
        return Current(exists=False)
    if os.path.islink(path):
        return Current(exists=True, error=SYMLINK_REFUSED, symlink=True)
    try:
        fd = _open_regular(path)
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            content = f.read()
    except OSError as e:
        return Current(exists=True, error=e.strerror or str(e))
    return Current(True, content, stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid)


def attrs_match(current: Current, mode: int, uid: int, gid: int) -> bool:
    return (
        current.mode == mode
        and (uid == -1 or current.uid == uid)
        and (gid == -1 or current.gid == gid)
    )


def stat_mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def fingerprint(path: str) -> Optional[Dict[str, int]]:
    """Identity of the file we wrote, without hashing its content.

    Hashes of low-entropy secrets (a domain, a short password) are
    brute-forceable, so ownership records for secret-bearing files keep
    inode + mtime instead.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return {"dev": st.st_dev, "ino": st.st_ino, "mtime_ns": st.st_mtime_ns}


def _content_hash(path: str) -> Optional[str]:
    try:
        fd = _open_regular(path)
        with os.fdopen(fd, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def same_identity(path: str, record) -> bool:
    """Whether ``path`` is still the file ``record`` was taken from (not, say,
    another file behind a repointed parent symlink).

    The inode must match. Device numbers are not stable across mounts
    (APFS assigns them at mount time), so a different ``dev`` is accepted
    when the content is provably unchanged: same mtime, or the recorded
    ``hash`` where the record keeps one.
    """
    if not isinstance(record, dict) or "ino" not in record:
        return False
    fp = fingerprint(path)
    if fp is None or record["ino"] != fp["ino"]:
        return False
    if "dev" not in record or record["dev"] == fp["dev"]:
        return True
    if record.get("mtime_ns") == fp["mtime_ns"]:
        return True
    return bool(record.get("hash")) and _content_hash(path) == record["hash"]


def refresh_dev(path: str, record: Dict) -> None:
    """Record the current device number of a file ``same_identity`` accepted,
    including records written before ``dev`` was tracked."""
    fp = fingerprint(path)
    if fp is not None and record.get("ino") == fp["ino"] and record.get("dev") != fp["dev"]:
        record["dev"] = fp["dev"]


def matches_fingerprint(path: str, record: Dict) -> bool:
    fp = fingerprint(path)
    if fp is None or "mtime_ns" not in record:
        return False
    return same_identity(path, record) and record["mtime_ns"] == fp["mtime_ns"]


_CASE_INSENSITIVE: Dict[int, bool] = {}


def _case_insensitive(existing_dir: str) -> bool:
    """Whether the volume holding ``existing_dir``'s entries folds case.

    Probed by stat-ing a case-swapped sibling name; cached per device.
    """
    try:
        dev = os.stat(existing_dir).st_dev
    except OSError:
        return False
    if dev in _CASE_INSENSITIVE:
        return _CASE_INSENSITIVE[dev]
    try:
        names = os.listdir(existing_dir)[:64]
    except OSError:
        names = []
    probes = [os.path.join(existing_dir, n) for n in names] + [existing_dir]
    result = False
    for probe in probes:
        base = os.path.basename(probe)
        if not base or base.swapcase() == base:
            continue
        try:
            st = os.lstat(probe)
        except OSError:
            continue
        try:
            other = os.lstat(os.path.join(os.path.dirname(probe), base.swapcase()))
        except OSError:
            break
        result = (other.st_ino, other.st_dev) == (st.st_ino, st.st_dev)
        break
    _CASE_INSENSITIVE[dev] = result
    return result


def _fold(path: str, start: str) -> str:
    existing = start
    while not os.path.isdir(existing):
        parent = os.path.dirname(existing)
        if parent == existing:
            break
        existing = parent
    return path.casefold() if _case_insensitive(existing) else path


def canonical_path(path: str) -> str:
    """One spelling per file: parent fully resolved, the last component not
    followed, case folded on case-insensitive volumes."""
    n = os.path.normpath(os.path.abspath(os.path.expanduser(path)))
    parent = os.path.realpath(os.path.dirname(n))
    return _fold(os.path.join(parent, os.path.basename(n)), parent)


def canonical_real(path: str) -> str:
    """Like ``canonical_path`` but the last component is resolved too."""
    real = os.path.realpath(os.path.normpath(os.path.abspath(os.path.expanduser(path))))
    return _fold(real, os.path.dirname(real))


def respell(records: Dict, paths) -> Dict:
    """Rename state keys that name the same file as a declared path (alias,
    case variant) to the declared spelling, merging duplicates. In place."""
    wanted = {}
    for path in paths:
        wanted.setdefault(canonical_path(path), path)
    for key in list(records):
        if not isinstance(key, str):
            continue
        spelling = wanted.get(canonical_path(key))
        if spelling is not None and spelling != key:
            record = records.pop(key)
            records.setdefault(spelling, record)
    return records


def file_id(path: str) -> Optional[tuple]:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def same_file_as(path: str, paths) -> Optional[str]:
    """The first of ``paths`` that is the same file as ``path``, if any."""
    canon = canonical_path(path)
    ident = file_id(path)
    for other in paths:
        if canonical_path(other) == canon or (ident is not None and file_id(other) == ident):
            return other
    return None


def is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


SECRETS_DIR_MODE = 0o751  # as sops-nix's /run/secrets


def fix_private_dirs(dirs) -> None:
    """Re-assert SECRETS_DIR_MODE on directories tools created (never through a symlink)."""
    for d in list(dirs):
        try:
            st = os.lstat(d)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode) and stat.S_IMODE(st.st_mode) != SECRETS_DIR_MODE:
            os.chmod(d, SECRETS_DIR_MODE)


def makedirs_secret(path: str, secrets_dir: Optional[str], created: Optional[list] = None) -> None:
    """Create missing directories one level at a time.

    Those inside ``secrets_dir`` (and it itself) get SECRETS_DIR_MODE and are
    appended to ``created``; any outside it are made with the normal umask.
    """
    missing = []
    current = os.path.abspath(path)
    while not os.path.isdir(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    root = canonical_real(secrets_dir) if secrets_dir else None
    for d in reversed(missing):
        if root is not None and is_within(canonical_real(d), root):
            os.mkdir(d, SECRETS_DIR_MODE)
            # mkdir honours umask; force the bits.
            os.chmod(d, SECRETS_DIR_MODE)
            if created is not None and d not in created:
                created.append(d)
        else:
            os.mkdir(d)


def _publish_exclusive(tmp: str, path: str) -> None:
    """Move ``tmp`` to ``path`` only if ``path`` does not exist yet."""
    try:
        os.link(tmp, path)
    except FileExistsError:
        raise
    except OSError as e:
        # No hard links here (exFAT, some network mounts): check, then rename.
        if e.errno not in _NO_LINK_ERRNOS:
            raise
        if os.path.lexists(path):
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), path) from None
        os.replace(tmp, path)
        return
    os.unlink(tmp)


FORCE_REMOVED = "force is no longer supported: tools now always moves the old file aside"

BACKUP_SUFFIX = ".tools"


def backup_path(path: str) -> str:
    return path + BACKUP_SUFFIX


def earlier_backup(path: str) -> Optional[str]:
    """``backup_path(path)`` if something is already there."""
    backup = backup_path(path)
    return backup if os.path.lexists(backup) else None


def aside_refused(path: str) -> Optional[str]:
    """Why ``path`` must not be moved aside again: an earlier backup exists."""
    earlier = earlier_backup(path)
    if earlier is None:
        return None
    return f"{path} changed again; an earlier backup {earlier} exists; remove it"


def move_aside(path: str) -> Optional[str]:
    """Rename whatever is at ``path`` (a symlink itself, never its target) to
    ``backup_path(path)``; the rename keeps its mode. None if nothing is there."""
    if not os.path.lexists(path):
        return None
    dest = backup_path(path)
    # A target is moved aside at most once: link (which never replaces) rather
    # than rename, so a backup created meanwhile is not overwritten.
    try:
        os.link(path, dest, follow_symlinks=False)
    except FileExistsError:
        raise FileExistsError(errno.EEXIST, "an earlier backup exists", dest) from None
    except OSError as e:
        if e.errno not in _NO_LINK_ERRNOS:
            raise
        raise OSError(
            e.errno, f"cannot move {path} aside: hard links unsupported here ({e.strerror})"
        ) from None
    os.unlink(path)
    return dest


def aside_note(path: str, reason: str) -> str:
    """Diff wording for a write that first moves the existing file aside."""
    return f"{reason}; old file moved aside to {backup_path(path)}"


def atomic_write(
    path: str,
    data: bytes,
    mode: int,
    uid: int = -1,
    gid: int = -1,
    *,
    exclusive: bool = False,
    backup: bool = False,
) -> Optional[str]:
    """Write via a temp file in the same dir. Mode/owner land before content.

    ``exclusive`` refuses to replace an existing path (hard-link publish;
    check-then-rename where hard links are unsupported), so a value that
    must only ever be created once cannot be clobbered.

    ``backup`` moves the existing path aside (``move_aside``) once the new
    content is on disk, just before publishing; returns the backup path.
    """
    dirname = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=dirname, prefix=".tools-")
    moved = None
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            if uid != -1 or gid != -1:
                os.fchown(f.fileno(), uid, gid)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        moved = move_aside(path) if backup else None
        if exclusive:
            _publish_exclusive(tmp, path)
        else:
            os.replace(tmp, path)
    except BaseException as e:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        if moved and isinstance(e, OSError):
            raise OSError(e.errno, f"{e.strerror or e}; old file moved aside to {moved}") from e
        raise
    return moved


def create_or_replace(
    path: str, data: bytes, mode: int, uid: int = -1, gid: int = -1
) -> Tuple[Optional[str], Optional[str]]:
    """Create ``path`` only while it is missing. One that appeared since
    planning is judged like an untracked file: the same bytes are rewritten,
    anything else is moved aside once (``aside_refused``).

    Returns (backup path, why it was moved aside), both None for a plain create.
    """
    try:
        atomic_write(path, data, mode, uid, gid, exclusive=True)
        return None, None
    except FileExistsError:
        pass
    current = read_current(path)
    if current.error and not current.symlink:
        raise OSError(errno.EEXIST, f"{path} appeared during this run ({current.error})")
    same = not current.symlink and current.content == data
    reason = None if same else ("a symlink" if current.symlink else "not tracked by tools")
    if reason:
        refused = aside_refused(path)
        if refused:
            raise OSError(errno.EEXIST, refused)
    return atomic_write(path, data, mode, uid, gid, backup=reason is not None), reason


def describe_removals(errors, remove, forget) -> list:
    """Diff rows for removals; with config errors deploy holds them, and says so."""
    if errors:
        held = "held until config errors are fixed"
        return [f"  ~ hold {path} (would remove; {held})" for path in remove] + [
            f"  ~ hold {path} (would forget: {reason}; {held})" for path, reason in forget
        ]
    return [f"  - remove {path}" for path in remove] + [
        f"  ~ forget {path} ({reason})" for path, reason in forget
    ]


def removal_blocked(conflicts, path: str) -> Optional[str]:
    """Reason a tracked ``path`` must not be removed (it is reached through,
    or lies inside, something `links`/`skills` manage), else None."""
    check = getattr(conflicts, "managed_reason", None)
    return check(path) if check is not None else None


def apply_attrs(path: str, mode: int, uid: int = -1, gid: int = -1) -> None:
    """chown/chmod the regular file at ``path``; a symlink there is refused."""
    fd = _open_regular(path)
    try:
        if uid != -1 or gid != -1:
            os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)
