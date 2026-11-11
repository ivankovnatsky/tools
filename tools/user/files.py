import hashlib
import os
import stat
from typing import Dict, List, NamedTuple, Optional, Tuple

from tools import privilege
from tools.fsutil import (
    FORCE_REMOVED,
    aside_refused,
    atomic_write,
    canonical_path,
    fingerprint,
    makedirs_secret,
    removal_blocked,
    respell,
    same_file_as,
    same_identity,
)
from tools.log import Color, debug, log
from tools.util import (
    format_diff_bytes,
    looks_like_secret,
    parse_on_change,
    run_hooks,
    settle_hooks,
)


def _file_hash(path: str) -> Optional[str]:
    """sha256 of a file on disk, or None if it cannot be read."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _tracked(target: str, record) -> bool:
    """Whether ``target`` is still the file ``record`` describes.

    Records without an inode predate identity tracking; for those the
    content hash stands in.
    """
    if not isinstance(record, dict):
        return False
    if "ino" in record:
        return same_identity(target, record)
    return bool(record.get("hash")) and _file_hash(target) == record["hash"]


def _record(target: str, file_hash: str) -> Dict:
    fp = fingerprint(target) or {}
    return {"hash": file_hash, **{k: fp[k] for k in ("dev", "ino") if k in fp}}


# Section move (secrets_flow.move_between_sections): every file-writing
# section defines these three.
def record_unmodified(target: str, record) -> bool:
    return (
        isinstance(record, dict)
        and bool(record.get("hash"))
        and (_file_hash(target) == record["hash"])
    )


def record_for(target: str, name: str, secret: bool) -> Dict:
    return _record(target, _file_hash(target))


def record_secret(record) -> bool:
    return False


def _removable(target: str, record) -> bool:
    """Only delete what still matches what we wrote."""
    record = record or {}
    if "ino" in record and not same_identity(target, record):
        return False
    stored_hash = record.get("hash")
    return not stored_hash or _file_hash(target) == stored_hash


def _is_real_dir(path: str) -> bool:
    # normpath: with a trailing slash, islink follows the link.
    path = os.path.normpath(path)
    return os.path.isdir(path) and not os.path.islink(path)


def _dir_record(target: str) -> Dict:
    fp = fingerprint(target) or {}
    return {"dir": True, **{k: fp[k] for k in ("dev", "ino") if k in fp}}


def _released_dir(target: str, conflicts) -> bool:
    keys = getattr(conflicts, "release_dirs", None)
    return bool(keys) and canonical_path(os.path.abspath(os.path.expanduser(target))) in keys


def _same_dir(target: str, record) -> bool:
    """Whether ``target`` is the directory ``record`` was taken from.

    A directory's mtime moves with its entries, so unlike files a changed
    device number (APFS remount) is accepted on the inode alone.
    """
    if not isinstance(record, dict) or not record.get("dir") or "ino" not in record:
        return False
    fp = fingerprint(os.path.normpath(target))
    return _is_real_dir(target) and fp is not None and fp["ino"] == record["ino"]


def _dir_kept(target: str, record) -> Optional[str]:
    """Why a tracked directory no longer declared stays; None to remove it."""
    if not _same_dir(target, record):
        return "not the directory tools created"
    try:
        if os.listdir(target):
            return "not empty"
    except OSError as e:
        return f"unreadable ({e})"
    return None


def _ensure_dir(
    target: str,
    mode: Optional[int],
    record,
    private_root: Optional[str] = None,
    created_dirs: Optional[list] = None,
) -> Tuple[Optional[bool], Optional[Dict]]:
    """Create ``target`` (and parents) if missing, then apply ``mode``.

    Returns (result, record): result as in `_copy_file`; record is set only
    for a directory tools created, so removal never touches one it did not.
    Directories under ``private_root`` (the secretsDir) are made like sops'
    and appended to ``created_dirs``.
    """
    if os.path.lexists(target) and not _is_real_dir(target):
        log(f"files: {target} exists and is not a directory", Color.RED)
        return False, None
    tracked = _same_dir(target, record)
    result = None
    try:
        if not os.path.lexists(target):
            makedirs_secret(target, private_root, created_dirs)
            if mode is not None:
                os.chmod(target, mode)
            log(f"Created directory: {target}", Color.GREEN)
            return True, _dir_record(target)
        if mode is not None and stat.S_IMODE(os.lstat(target).st_mode) != mode:
            os.chmod(target, mode, follow_symlinks=False)
            log(f"Chmod: {target} -> {oct(mode)}", Color.GREEN)
            result = True
    except OSError as e:
        log(f"Failed to create {target}: {e}", Color.RED)
        return False, record if tracked else None
    return result, _dir_record(target) if tracked else None


SKIP_DIRS = {".git", ".hg", ".svn", "__pycache__"}
SKIP_FILES = {".DS_Store", ".gitignore", ".gitkeep"}


def _parse_mode(value) -> Optional[int]:
    """Parse a mode value from config.

    Only quoted octal strings are accepted (``mode: "0644"``). Bare YAML
    ints are rejected because they are ambiguous: PyYAML (YAML 1.1)
    parses ``0644`` as octal into int 420, which is indistinguishable
    from a literal ``420`` — re-interpreting either as octal silently
    yields the wrong permission bits for one of them.

    Returns None when no mode was specified. Raises ValueError on a
    bad string and TypeError on any other type.
    """
    if value is None:
        return None
    if isinstance(value, str):
        mode = int(value, 8)
        if not 0 <= mode <= 0o7777:
            raise ValueError(f"files: mode {value!r} must be between 0000 and 7777")
        return mode
    raise TypeError(
        f'files: mode must be a quoted octal string (e.g. "0644"), got {value!r} '
        f"({type(value).__name__}); unquoted YAML ints are ambiguous"
    )


class FilePlan(NamedTuple):
    # create | unchanged | chmod | update | replace | error
    action: str
    reason: str = ""  # replace: why the existing file is moved aside; error: what failed
    existing: Optional[bytes] = None


def _replace(target: str, reason: str, existing: Optional[bytes] = None) -> FilePlan:
    refused = aside_refused(target)
    if refused:
        return FilePlan("error", refused)
    return FilePlan("replace", reason, existing)


def _uid(path: str) -> int:
    return os.lstat(path).st_uid


def plan_file(
    target: str, desired: bytes, desired_mode: int, record, owner: Optional[int] = None
) -> FilePlan:
    """What deploying ``desired`` to ``target`` does; shared by deploy and diff.

    With ``owner`` (0 for system entries), a file owned by anyone else is replaced
    whatever it holds: its owner could change it after any check."""
    if not os.path.lexists(target):
        return FilePlan("create")
    if os.path.islink(target):
        return _replace(target, "a symlink")
    if not os.path.isfile(target):
        return FilePlan("error", f"{target} exists and is not a regular file")
    try:
        with open(target, "rb") as f:
            existing = f.read()
    except OSError as e:
        return FilePlan("error", f"cannot read {target}: {e}")
    if owner is not None and _uid(target) != owner:
        return _replace(target, f"not owned by uid {owner}", existing)
    if existing == desired:
        if stat.S_IMODE(os.lstat(target).st_mode) == desired_mode:
            return FilePlan("unchanged", existing=existing)
        return FilePlan("chmod", existing=existing)
    if not _tracked(target, record):
        return _replace(target, "not tracked by tools", existing)
    if hashlib.sha256(existing).hexdigest() != record.get("hash"):
        return _replace(target, "modified since tools wrote it", existing)
    return FilePlan("update", existing=existing)


def _copy_file(
    source: str,
    target: str,
    mode: Optional[int],
    *,
    secrets: bool = False,
    record=None,
    owner: Optional[int] = None,
) -> Tuple[Optional[bool], str]:
    """Copy source to target atomically, applying mode if provided.

    ``owner``: see `plan_file`.

    Returns (result, source_hash) where result is:
    True if written, False on error, None if up to date.

    A target tools did not write, or that changed since, is moved aside first.

    When ``secrets`` is true — or either side (incoming or existing)
    trips the ``looks_like_secret`` heuristic — the inline diff body is
    suppressed so credentials do not bleed into deploy/activation logs.
    Both sides are checked because a rotation that *removes* a secret
    from the file would otherwise leak the old value.
    """
    try:
        with open(source, "rb") as f:
            desired = f.read()
    except OSError as e:
        log(f"Failed to read source {source}: {e}", Color.RED)
        return False, ""

    source_hash = hashlib.sha256(desired).hexdigest()
    desired_mode = mode if mode is not None else stat.S_IMODE(os.stat(source).st_mode)
    fp = plan_file(target, desired, desired_mode, record, owner)
    try:
        dirname = os.path.dirname(target)
        if fp.action == "create" and dirname:
            os.makedirs(dirname, exist_ok=True)
        if fp.action == "create":
            atomic_write(target, desired, desired_mode, exclusive=True)
            log(f"Created: {target}", Color.GREEN)
            return True, source_hash
    except FileExistsError:
        # Appeared since planning: judge it like any existing file.
        fp = plan_file(target, desired, desired_mode, record, owner)
    except OSError as e:
        log(f"Failed to write {target}: {e}", Color.RED)
        return False, source_hash

    if fp.action == "unchanged":
        return None, source_hash
    if fp.action == "error":
        log(f"files: {fp.reason}", Color.RED)
        return False, source_hash
    if fp.action == "chmod":
        try:
            os.chmod(target, desired_mode)
        except OSError as e:
            log(f"Failed to chmod {target}: {e}", Color.RED)
            return False, source_hash
        log(f"Chmod: {target} -> {oct(desired_mode)}", Color.GREEN)
        return True, source_hash
    if fp.action == "create":
        log(f"files: {target} keeps changing; not writing it", Color.RED)
        return False, source_hash

    diff_body = None
    if fp.action == "update":
        is_secret = secrets or looks_like_secret(desired) or looks_like_secret(fp.existing)
        if not is_secret:
            diff_body = format_diff_bytes(desired, fp.existing, target)
    # Content moved aside is never shown: it may hold hand-written secrets.

    try:
        moved = atomic_write(target, desired, desired_mode, backup=fp.action == "replace")
    except OSError as e:
        log(f"Failed to write {target}: {e}", Color.RED)
        return False, source_hash

    if moved:
        log(f"Moved aside ({fp.reason}): {target} -> {moved}", Color.YELLOW)
    log(f"{'Updated' if fp.action == 'update' else 'Replaced'}: {target}", Color.GREEN)
    if diff_body:
        print(diff_body)
    return True, source_hash


SYSTEM_HINT = "run sudo tools deploy --scope files"
SYSTEM_KEYS = {"source", "target", "mode", "secrets"}
SYSTEM_KEY_ERROR = "`system` is not needed: targets the user cannot write are system files"
_KEYS = {
    "dir",
    "source",
    "target",
    "directory",
    "mode",
    "secrets",
    "onChange",
    "onUpdate",
    "skip",
}


def _system_entry_error(entry: Dict) -> Optional[str]:
    """Why a system entry (target the user cannot write) is invalid: root writes
    only plain source/target files."""
    extra = sorted(set(entry) - SYSTEM_KEYS)
    if extra:
        return f"a system entry takes only {sorted(SYSTEM_KEYS)}, not {extra}: {entry!r}"
    if "source" not in entry:
        return f"a system entry needs `source` and `target`: {entry!r}"
    return None


def _user_mode(path: str, st: os.stat_result, inv) -> int:
    return privilege.user_mode(st, inv)


def _decision_dir(target: str, directory: bool, inv, lstat_fn=os.lstat, readlink_fn=os.readlink):
    """Walk down from `/` towards target (its parent, for a file), following symlinks
    by reading them, and stop at a missing component or a directory the invoking user
    cannot search. Root and the user stop at the same place. Returns the last directory
    reached, its lstat, and whether that is the target itself."""
    parts = [p for p in os.path.normpath(target).split(os.sep) if p]
    if not directory:
        parts = parts[:-1]
    current, st = "/", lstat_fn("/")
    links = 0
    while parts:
        if not _user_mode(current, st, inv) & 1:
            break
        part = parts.pop(0)
        if part == "..":
            current = os.path.dirname(current)
            st = lstat_fn(current)
            continue
        nxt = os.path.join(current, part)
        try:
            nst = lstat_fn(nxt)
        except OSError:
            parts.insert(0, part)
            break
        if stat.S_ISLNK(nst.st_mode):
            links += 1
            if links > 40:
                parts.insert(0, part)
                break
            link = readlink_fn(nxt)
            if os.path.isabs(link):
                current, st = "/", lstat_fn("/")
            parts[:0] = [p for p in link.split(os.sep) if p and p != "."]
            continue
        if not stat.S_ISDIR(nst.st_mode):
            parts.insert(0, part)
            break
        current, st = nxt, nst
    return current, st, directory and not parts


def is_system_entry(entry) -> bool:
    """A root-owned file: its target is an absolute path the invoking user cannot
    manage. See `_decision_dir`: an existing `directory` target reached by the walk is
    the user's when they own it; otherwise the user must be able to write the
    directory the walk stopped at."""
    if not isinstance(entry, dict) or "dir" in entry or "target" not in entry:
        return False
    target = os.path.normpath(os.path.expanduser(str(entry["target"])))
    if not os.path.isabs(target):
        return False
    inv = privilege.invoker()
    path, st, reached = _decision_dir(target, entry.get("directory") is True, inv)
    if reached:
        return st.st_uid != inv.uid
    return not _user_mode(path, st, inv) & 2


def has_system_work(entries, state: Dict) -> bool:
    """Root's `files` run: system entries declared, or files tracked in root's state."""
    declared = isinstance(entries, list) and any(is_system_entry(e) for e in entries)
    return declared or bool(state.get("files"))


def other_run_targets(entries, config_dir: str, system: bool) -> List[str]:
    """Declared targets the other run (root or user) deploys; never removed by this one."""
    resolved, _errors = _resolve_entries(entries, config_dir, not system)
    return [e.target for e in resolved]


def system_pending(entries, config_dir: str) -> Tuple[List[str], List[str]]:
    """As the user: system targets whose content or mode differ, and those the user
    cannot read to check (read-only)."""
    resolved, _errors = _resolve_entries(entries, config_dir, system=True)
    pending, unchecked = [], []
    for e in resolved:
        try:
            os.lstat(e.target)
        except PermissionError:
            unchecked.append(e.target)
            continue
        except OSError:
            pass
        if os.path.isfile(e.target) and not os.access(e.target, os.R_OK):
            unchecked.append(e.target)
            continue
        try:
            with open(e.source, "rb") as f:
                desired = f.read()
            mode = e.mode if e.mode is not None else stat.S_IMODE(os.stat(e.source).st_mode)
        except OSError:
            pending.append(e.target)
            continue
        if plan_file(e.target, desired, mode, None, 0).action != "unchanged":
            pending.append(e.target)
    return pending, unchecked


def note_system(entries, config_dir: str) -> None:
    """As the user (not under sudo): one note for system files deploy leaves to root."""
    if privilege.UNDER_SUDO:
        return
    pending, unchecked = system_pending(entries, config_dir)
    if pending:
        log(
            f"files: {len(pending)} system file(s) differ ({', '.join(pending)}); {SYSTEM_HINT}",
            Color.YELLOW,
        )
    if unchecked:
        log(
            f"files: cannot check {len(unchecked)} system file(s) ({', '.join(unchecked)})",
            Color.YELLOW,
        )


def root_target_error(target: str) -> Optional[str]:
    """As root, why `target` must not be written: it is the user's home or above it,
    its directory is under the home, or a non-root user can change that directory. A
    symlink at target is replaced, not followed."""
    parent = os.path.dirname(os.path.normpath(target))
    home = os.path.expanduser("~")
    if privilege.under_home(home, target):
        return "the user's home or a directory above it"
    if privilege.under_home(parent, home):
        return "under the user's home; root never writes in user space"
    return privilege.root_dir_error(parent)


class Entry(NamedTuple):
    target: str
    source: Optional[str]  # None for a directory entry
    mode: Optional[int]
    secrets: bool
    on_change: Optional[str] = None
    directory: bool = False
    on_update: Optional[str] = None
    skip: bool = False


def declared_hooks(entries, skipped: bool = False) -> set:
    """Every onChange in the raw config, including entries that fail to resolve.

    Entries with ``skip: true`` count only with ``skipped`` (and then only they do).
    """
    if not isinstance(entries, list):
        return set()
    return {
        e["onChange"]
        for e in entries
        if isinstance(e, dict)
        and isinstance(e.get("onChange"), str)
        and (e.get("skip") is True) == skipped
    }


def _ordered(resolved: List[Entry]) -> List[Entry]:
    """Directory entries first, parents before children, so a directory exists
    (and is tracked as created) before anything is written inside it."""
    return sorted(
        resolved,
        key=lambda e: (not e.directory, e.target.count(os.sep) if e.directory else 0),
    )


def _removal_order(targets, records: Dict) -> List[str]:
    """Files before directories, deepest directory first."""

    def is_dir(t):
        return isinstance(records.get(t), dict) and bool(records[t].get("dir"))

    return sorted(targets, key=lambda t: (is_dir(t), -t.count(os.sep) if is_dir(t) else 0))


def _entry_source(entry: Dict[str, object], config_dir: str) -> str:
    """The config-dir path a `dir` or `source` entry reads."""
    return os.path.join(config_dir, str(entry["dir"] if "dir" in entry else entry["source"]))


def source_paths(entries, config_dir: str) -> List[str]:
    """Every `dir` tree and `source` file the `files:` entries read, existing or not."""
    if not isinstance(entries, list):
        return []
    return [
        _entry_source(e, config_dir)
        for e in entries
        if isinstance(e, dict) and not e.get("directory") and ("dir" in e or "source" in e)
    ]


def _resolve_entries(
    entries: List[Dict[str, object]], config_dir: str, system: bool = False
) -> Tuple[List[Entry], List[str]]:
    """Expand a `files:` config into a flat list of `Entry`.

    Three entry shapes are supported and may be mixed in the same list:
      - dir entry:       {dir: <path>, mode?, secrets?, onChange?, onUpdate?}
      - file entry:      {source, target, mode?, secrets?, onChange?, onUpdate?}
      - directory entry: {target, directory: true, mode?, onChange?, onUpdate?}

    ``skip: true`` keeps an entry declared but unwritten (see `install_files`);
    it is rejected on a dir entry.

    The ``secrets`` flag, when true, suppresses inline content diffs in
    both ``tools deploy`` and ``tools diff`` for that entry. On a dir
    entry it propagates to every file walked under that tree.

    File and directory entries take precedence over dir entries on the same target.

    An entry whose target is an absolute path in a directory the invoking user
    cannot write (nearest existing ancestor) is a root-owned system file
    (``source``, ``target``, ``mode?``, ``secrets?``),
    deployed only by root (`sudo tools deploy`). ``system`` selects which kind is
    resolved; the other kind is still validated.

    Returns (resolved, errors). `resolved` is deduped by target, last-write-wins.
    `errors` contains human-readable messages about malformed entries.
    """
    dir_pairs: List[Entry] = []
    file_pairs: List[Entry] = []
    errors: List[str] = []
    home = os.path.expanduser("~")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        return [], [f"files must be a list of entries, got {type(entries).__name__}"]

    for entry in entries:
        if not isinstance(entry, dict):
            errors.append(f"entry must be a mapping: {entry!r}")
            continue
        if "force" in entry:
            errors.append(f"{FORCE_REMOVED}: {entry!r}")
            continue
        if "system" in entry:
            errors.append(f"{SYSTEM_KEY_ERROR}: {entry!r}")
            continue
        unknown = set(entry) - _KEYS
        if unknown:
            errors.append(f"unknown keys {sorted(unknown)} in entry {entry!r}")
            continue
        entry_system = is_system_entry(entry)
        if entry_system:
            err = _system_entry_error(entry)
            if err:
                errors.append(err)
                continue
        try:
            entry_mode = _parse_mode(entry.get("mode"))
        except (TypeError, ValueError) as e:
            errors.append(f"invalid mode in entry {entry!r}: {e}")
            continue
        try:
            on_change = parse_on_change(entry.get("onChange"), f"entry {entry!r}")
            on_update = parse_on_change(entry.get("onUpdate"), f"entry {entry!r}", "onUpdate")
        except ValueError as e:
            errors.append(str(e))
            continue

        secrets_raw = entry.get("secrets", False)
        if not isinstance(secrets_raw, bool):
            errors.append(
                f"`secrets` must be a bool, got {type(secrets_raw).__name__} in {entry!r}"
            )
            continue
        entry_secrets = secrets_raw
        skip = entry.get("skip", False)
        if not isinstance(skip, bool):
            errors.append(f"`skip` must be a bool, got {type(skip).__name__} in {entry!r}")
            continue
        if skip and "dir" in entry:
            errors.append(
                f"skip is supported on single files and templates, not dir: trees: {entry!r}"
            )
            continue
        if entry_system != system:
            continue

        directory = entry.get("directory", False)
        if not isinstance(directory, bool):
            errors.append(f"`directory` must be a bool in {entry!r}")
            continue
        if directory:
            if "target" not in entry or "source" in entry or "dir" in entry:
                errors.append(f"a directory entry takes `target` and no `source`/`dir`: {entry!r}")
                continue
            target = os.path.normpath(os.path.expanduser(str(entry["target"])))
            file_pairs.append(
                Entry(target, None, entry_mode, False, on_change, True, on_update, skip)
            )
            continue

        if "dir" in entry:
            source_dir = _entry_source(entry, config_dir)
            if not os.path.isdir(source_dir):
                errors.append(f"source directory not found: {entry['dir']}")
                continue
            for root, dirs, names in os.walk(source_dir):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for name in sorted(names):
                    if name in SKIP_FILES:
                        continue
                    source = os.path.join(root, name)
                    rel = os.path.relpath(source, source_dir)
                    target = os.path.join(home, rel)
                    dir_pairs.append(
                        Entry(
                            target,
                            source,
                            entry_mode,
                            entry_secrets,
                            on_change,
                            False,
                            on_update,
                            skip,
                        )
                    )
            continue

        if "source" in entry and "target" in entry:
            source = _entry_source(entry, config_dir)
            target = os.path.normpath(os.path.expanduser(str(entry["target"])))
            if not os.path.isfile(source):
                errors.append(f"source not found: {entry['source']}")
                continue
            file_pairs.append(
                Entry(target, source, entry_mode, entry_secrets, on_change, False, on_update, skip)
            )
            continue

        errors.append(f"entry must have either `dir` or both `source` and `target`: {entry!r}")

    # Dedupe by target, later entries win — but `secrets` is sticky across
    # every collision (dir/dir, dir/file, file/file): once any entry marks a
    # target as secret, the resolved entry stays secret. Otherwise a later
    # entry added just to override the mode (or two overlapping `dir:` walks)
    # would silently downgrade the suppression flag and re-expose content
    # from a secret source.
    resolved_map: Dict[str, Entry] = {}
    for e in dir_pairs + file_pairs:
        prev = resolved_map.get(e.target)
        resolved_map[e.target] = e._replace(secrets=e.secrets or bool(prev and prev.secrets))
    return list(resolved_map.values()), errors


def _conflict(target: str, conflicts: Optional[Dict[str, str]]) -> Optional[str]:
    if not conflicts:
        return None
    return conflicts.get(os.path.normpath(os.path.abspath(target)))


def install_files(
    entries: List[Dict[str, object]],
    config_dir: str,
    state: Dict,
    conflicts: Optional[Dict[str, str]] = None,
    private_root: Optional[str] = None,
    update: bool = False,
    system: bool = False,
) -> bool:
    """Deploy files declared in the `files:` config section.

    ``system`` (root under `sudo tools deploy`, with root's state) deploys only
    entries whose target directory the user cannot write; otherwise those are left alone
    with a note.

    With ``update``, each distinct ``onUpdate`` command runs once afterwards,
    changed or not, provided all its entries converged and onChange succeeded.
    Failures are not kept pending: the next update reruns them.

    ``private_root`` is the sops secretsDir: directory entries inside it are
    created with its mode and registered with the sops section's created dirs.

    See `_resolve_entries` for the supported entry shapes. A ``skip`` entry is
    neither written nor removed, and its state record is kept as is.

    Any legacy ``configFiles`` state from an earlier `tools` version is
    left untouched; nothing is auto-migrated, so previously deployed
    files are never deleted on upgrade. Users who want to retire an old
    target should re-declare it under ``files:`` then remove it.
    """
    success = True
    changed = False
    managed: Dict[str, Dict] = {}
    hooks: Dict[str, List[str]] = {}

    resolved, errors = _resolve_entries(entries, config_dir, system)
    # Classified before anything is written: a chmod below can change the answer.
    others = other_run_targets(entries, config_dir, system)
    for err in errors:
        log(f"files: {err}", Color.RED)
        success = False
    if not system:
        note_system(entries, config_dir)
    declared = [e.target for e in resolved]
    # State keys match declared targets by file identity, not spelling.
    state_files = respell(dict(state.get("files", {}) or {}), declared)

    held: Dict[str, Dict] = {}
    released = set()
    updates: Dict[str, List[str]] = {}
    unsettled = set()
    for e in _ordered(resolved):
        target = e.target
        reason = _conflict(target, conflicts) or (root_target_error(target) if system else None)
        if reason:
            log(f"files: refusing {target}: {reason}", Color.RED)
            success = False
            if target in state_files:
                held[target] = state_files[target]
            if e.on_update:
                unsettled.add(e.on_update)
            continue
        if e.skip:
            log(f"files: skipped {target} (skip: true)", Color.YELLOW)
            if target in state_files:
                held[target] = state_files[target]
            continue
        if e.directory:
            created = state.setdefault("sops", {}).setdefault("createdDirs", [])
            result, record = _ensure_dir(
                target, e.mode, state_files.get(target), private_root, created
            )
            if record is not None:
                managed[target] = record
            elif result is not False:
                released.add(target)
        else:
            record = state_files.get(target)
            result, source_hash = _copy_file(
                e.source,
                target,
                e.mode,
                secrets=e.secrets,
                record=record,
                owner=0 if system else None,
            )
            if source_hash and result is not False:
                managed[target] = _record(target, source_hash)
        if e.on_update:
            if result is False:
                unsettled.add(e.on_update)
            else:
                updates.setdefault(e.on_update, []).append(target)
        if result is False:
            success = False
            if target in state_files:
                held[target] = state_files[target]
        elif result is True:
            changed = True
            if e.on_change:
                hooks.setdefault(e.on_change, []).append(target)

    # Only config errors hold removals: an unparsed entry looks undesired.
    if not errors:
        kept = dict(held)
        for target in _removal_order(state_files, state_files):
            if target not in managed and target not in held and target not in released:
                record = state_files.get(target)
                if same_file_as(target, others):
                    kept[target] = record
                    continue
                blocked = removal_blocked(conflicts, target)
                twin = same_file_as(target, declared)
                if twin:
                    blocked = f"the same file as declared {twin}"
                if blocked:
                    log(
                        f"files: not removing {target}: it is {blocked}; still tracked",
                        Color.YELLOW,
                    )
                    kept[target] = record
                    continue
                if not os.path.exists(target):
                    continue
                unsafe = root_target_error(target) if system else None
                if unsafe:
                    log(f"files: not removing {target}: {unsafe}; still tracked", Color.RED)
                    kept[target] = record
                    success = False
                    continue
                is_dir = isinstance(record, dict) and record.get("dir")
                if is_dir and _released_dir(target, conflicts):
                    log(
                        f"files: releasing {target}: another section manages a path inside",
                        Color.YELLOW,
                    )
                    continue
                if is_dir:
                    why = _dir_kept(target, record)
                    if why:
                        log(f"Keeping {target}: {why}.", Color.YELLOW)
                        kept[target] = record
                        continue
                # A file edited since then holds work this tool never authored.
                elif not _removable(target, record):
                    log(
                        f"Keeping {target}: modified since we wrote it. "
                        "Delete it by hand if it is no longer wanted.",
                        Color.YELLOW,
                    )
                    kept[target] = record
                    continue
                try:
                    if is_dir:
                        os.rmdir(os.path.normpath(target))
                    else:
                        os.remove(target)
                    log(f"Removed: {target}", Color.RED)
                    changed = True
                except OSError as e:
                    log(f"Failed to remove {target}: {e}", Color.RED)
                    success = False
                    # Keep tracking it so the next run retries the cleanup.
                    kept[target] = record

        state["files"] = dict(managed)
        state["files"].update(kept)
    else:
        merged = dict(state_files)
        merged.update(managed)
        state["files"] = merged

    settled = settle_hooks(
        state, "files", hooks, declared_hooks(entries), declared_hooks(entries, skipped=True)
    )
    success &= settled

    if update:
        # An entry that failed to resolve may declare the same command.
        skipped = unsettled if settled and not errors else unsettled | set(updates)
        for command in sorted(skipped):
            log(
                f"files: onUpdate skipped (files, config or onChange failed): {command}",
                Color.YELLOW,
            )
        runnable = {c: t for c, t in updates.items() if c not in skipped}
        if run_hooks("files", runnable, "onUpdate", "rerun on the next deploy --update"):
            success = False

    if not changed and entries:
        debug("All files up to date", Color.BLUE)

    return success
