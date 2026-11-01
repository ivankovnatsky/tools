"""sops: decrypt sops-encrypted values into individual files.

Input is plain, already-resolved data (see ``parse_sops``)::

    {
      "binary": None,                     # else `sops` from PATH
      "sshToAgeBinary": None,             # else `ssh-to-age` from PATH
      "age": {"keyFile": "...", "sshKeyPaths": ["..."]},
      "secretsDir": "~/.local/state/tools/secrets",
      "defaultSopsFile": "/abs/secrets.yaml",
      "defaultSopsFormat": "yaml",        # yaml | json | dotenv | ini | binary
      "secrets": {
        "<name>": {"key": "a/b/0", "sopsFile": "...", "format": "yaml", "path": "...",
                   "mode": "0400", "owner": ..., "group": ..., "restartUnits": [...]},
        "<name>": {"generate": {"length": 64, "charset": "alnum"}},
      },
    }

Option names and defaults follow sops-nix: ``key`` defaults to the
secret's name (``a/b`` nests), ``key: ""`` or ``format: binary`` is the
whole file, and a name may contain ``/``. Each secret is one resource
whose identity is its resolved ``path`` (default ``<secretsDir>/<name>``).
Values never reach logs, diff output or state: changes are reported as
path + action, and ownership records keep inode/mtime, not a content hash.
"""

import json
import os
import secrets as _secrets
import shutil
import stat
import string
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from tools import fsutil
from tools.log import Color, debug, log
from tools.placeholders import PlaceholderError

DEFAULT_SECRETS_DIR = "~/.local/state/tools/secrets"
DEFAULT_MODE = 0o400

CHARSETS = {
    "alnum": string.ascii_letters + string.digits,
    "alpha": string.ascii_letters,
    "hex": "0123456789abcdef",
    "urlsafe": string.ascii_letters + string.digits + "-_",
}

_SECRET_KEYS = {
    "key",
    "sopsFile",
    "format",
    "path",
    "mode",
    "owner",
    "group",
    "generate",
    "restartUnits",
    "force",
}
_TOP_KEYS = {
    "binary",
    "sshToAgeBinary",
    "age",
    "secretsDir",
    "defaultSopsFile",
    "defaultSopsFormat",
    "secrets",
}
FORMATS = ("yaml", "json", "dotenv", "ini", "binary")

# (cmd, env) -> (returncode, stdout bytes, stderr text)
Runner = Callable[[List[str], Mapping[str, str]], Tuple[int, bytes, str]]


def default_runner(cmd: List[str], env: Mapping[str, str]) -> Tuple[int, bytes, str]:
    result = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, env=dict(env))
    return result.returncode, result.stdout, result.stderr.decode("utf-8", "replace")


@dataclass(frozen=True)
class Generate:
    length: int
    chars: str


@dataclass(frozen=True)
class Secret:
    name: str
    path: str  # identity
    mode: int = DEFAULT_MODE
    owner: Optional[str] = None
    group: Optional[str] = None
    sops_file: Optional[str] = None
    key: Optional[Tuple[str, ...]] = None  # None: the whole decrypted file
    format: str = "yaml"
    generate: Optional[Generate] = None
    notify: Tuple[str, ...] = ()
    force: bool = False  # replace an existing file tools does not track


@dataclass(frozen=True)
class Settings:
    binary: Optional[str] = None
    ssh_to_age_binary: Optional[str] = None
    age_key_file: Optional[str] = None
    age_ssh_key_files: Tuple[str, ...] = ()
    secrets_dir: str = field(default_factory=lambda: os.path.expanduser(DEFAULT_SECRETS_DIR))


@dataclass
class Parsed:
    settings: Settings
    secrets: List[Secret]
    errors: List[str] = field(default_factory=list)
    settings_error: Optional[str] = None  # settings are defaults, not the user's

    def secrets_dir(self) -> Tuple[Optional[str], Optional[str]]:
        """(secretsDir, None), or (None, why) when the section is invalid."""
        if self.settings_error:
            return None, self.settings_error
        return self.settings.secrets_dir, None


def secrets_dir_kwargs(raw) -> Dict[str, Optional[str]]:
    """``secrets_dir``/``secrets_dir_error`` for sections that wait on or read
    secretsDir: None when the section is invalid or no secret lives inside it,
    since tools then never creates it."""
    parsed = parse_sops(raw)
    secrets_dir, why = parsed.secrets_dir()
    if secrets_dir and not parsed.secrets:
        secrets_dir, why = None, "no sops secrets are configured"
    elif secrets_dir and not any(
        fsutil.is_within(
            fsutil.canonical_real(os.path.dirname(s.path)), fsutil.canonical_real(secrets_dir)
        )
        for s in parsed.secrets
    ):
        secrets_dir, why = None, f"no sops secret has its path inside {secrets_dir}"
    return {"secrets_dir": secrets_dir, "secrets_dir_error": why}


def _expand(path: str) -> str:
    return os.path.normpath(os.path.expanduser(str(path)))


def _path_field(spec: Mapping, key: str, label: str) -> Optional[str]:
    """Absent or null -> None; anything but a non-empty string is an error."""
    value = spec.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}.{key} must be a non-empty path string, got {value!r}")
    return _expand(value)


def _parse_generate(raw, name: str) -> Generate:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{name}: generate must be a mapping")
    unknown = set(raw) - {"length", "charset", "chars"}
    if unknown:
        raise ValueError(f"{name}: unknown generate keys {sorted(unknown)}")
    length = raw.get("length", 64)
    if not isinstance(length, int) or isinstance(length, bool) or not 8 <= length <= 4096:
        raise ValueError(f"{name}: generate.length must be an int in 8..4096")
    if "chars" in raw:
        chars = raw["chars"]
        if not isinstance(chars, str) or len(set(chars)) < 2:
            raise ValueError(f"{name}: generate.chars needs at least two distinct characters")
    else:
        charset = raw.get("charset", "alnum")
        if charset not in CHARSETS:
            raise ValueError(f"{name}: generate.charset must be one of {sorted(CHARSETS)}")
        chars = CHARSETS[charset]
    return Generate(length, "".join(dict.fromkeys(chars)))


def parse_sops(raw: Optional[Mapping]) -> Parsed:
    """Normalize and validate the section into Settings + Secret resources."""
    raw = {} if raw is None else raw
    if not isinstance(raw, Mapping):
        err = f"sops must be a mapping, got {type(raw).__name__}"
        return Parsed(Settings(), [], [err], settings_error=err)
    errors: List[str] = []
    unknown = set(raw) - _TOP_KEYS
    if unknown:
        errors.append(f"unknown keys {sorted(unknown)}")

    age = raw.get("age")
    age = {} if age is None else age
    try:
        if not isinstance(age, Mapping) or set(age) - {"keyFile", "sshKeyPaths"}:
            raise ValueError("age must be a mapping with keyFile and/or sshKeyPaths")
        ssh_paths = age.get("sshKeyPaths")
        ssh_paths = [] if ssh_paths is None else ssh_paths
        if not isinstance(ssh_paths, list) or not all(isinstance(p, str) and p for p in ssh_paths):
            raise ValueError("sops.age.sshKeyPaths must be a list of non-empty path strings")
        ssh_keys = tuple(_path_field({"p": p}, "p", "sops.age.sshKeyPaths") for p in ssh_paths)
        default_format = raw.get("defaultSopsFormat")
        default_format = "yaml" if default_format is None else default_format
        if default_format not in FORMATS:
            raise ValueError(f"sops.defaultSopsFormat must be one of {', '.join(FORMATS)}")
        settings = Settings(
            binary=_path_field(raw, "binary", "sops"),
            ssh_to_age_binary=_path_field(raw, "sshToAgeBinary", "sops"),
            age_key_file=_path_field(age, "keyFile", "sops.age"),
            age_ssh_key_files=ssh_keys,
            secrets_dir=_path_field(raw, "secretsDir", "sops") or _expand(DEFAULT_SECRETS_DIR),
        )
        if not os.path.isabs(settings.secrets_dir):
            raise ValueError(
                f"sops.secretsDir must be an absolute path, got {settings.secrets_dir!r}"
            )
        default_file = _path_field(raw, "defaultSopsFile", "sops")
    except ValueError as e:
        # Without valid settings no secret can be placed or decrypted safely.
        return Parsed(Settings(), [], errors + [str(e)], settings_error=str(e))

    entries = raw.get("secrets")
    entries = {} if entries is None else entries
    if not isinstance(entries, Mapping):
        errors.append("secrets must be a mapping of name -> spec")
        entries = {}

    result: List[Secret] = []
    seen_paths: Dict[str, str] = {}
    for name, spec in entries.items():
        spec = {} if spec is None else spec
        try:
            if (
                not isinstance(name, str)
                or not name
                or any(part in ("", ".", "..") for part in name.split("/"))
            ):
                raise ValueError(f"invalid secret name {name!r}")
            if not isinstance(spec, Mapping):
                raise ValueError(f"{name}: spec must be a mapping")
            bad = set(spec) - _SECRET_KEYS
            if bad:
                raise ValueError(f"{name}: unknown keys {sorted(bad)}")
            path = _path_field(spec, "path", f"sops.secrets.{name}")
            path = path or os.path.join(settings.secrets_dir, name)
            if not os.path.isabs(path):
                raise ValueError(f"{name}: path must be absolute (or start with ~)")
            canon = fsutil.canonical_path(path)
            if canon in seen_paths:
                raise ValueError(f"{name}: path {path} already used by {seen_paths[canon]}")
            for other, other_name in seen_paths.items():
                if fsutil.is_within(canon, other) or fsutil.is_within(other, canon):
                    raise ValueError(f"{name}: path {path} nests with secret {other_name}")
            force = spec.get("force", False)
            if not isinstance(force, bool):
                raise ValueError(f"{name}: force must be a bool")
            notify = spec.get("restartUnits")
            notify = [] if notify is None else notify
            if not isinstance(notify, list) or not all(isinstance(n, str) for n in notify):
                raise ValueError(f"{name}: restartUnits must be a list of strings")
            mode = fsutil.parse_mode(spec.get("mode"), f"sops.secrets.{name}")
            fsutil.check_owner(spec.get("owner"), f"{name}: owner")
            fsutil.check_owner(spec.get("group"), f"{name}: group")
            common = dict(
                name=name,
                path=path,
                mode=DEFAULT_MODE if mode is None else mode,
                owner=spec.get("owner"),
                group=spec.get("group"),
                notify=tuple(notify),
                force=force,
            )
            if "generate" in spec:
                if {"key", "sopsFile", "format"} & set(spec):
                    raise ValueError(
                        f"{name}: generate cannot be combined with key/sopsFile/format"
                    )
                secret = Secret(**common, generate=_parse_generate(spec["generate"], name))
            else:
                sops_file = _path_field(spec, "sopsFile", f"sops.secrets.{name}") or default_file
                if not sops_file:
                    raise ValueError(f"{name}: no sopsFile and no defaultSopsFile")
                sops_file = _expand(sops_file)
                if not os.path.isabs(sops_file):
                    raise ValueError(f"{name}: sopsFile must be an absolute path")
                fmt = spec.get("format")
                fmt = default_format if fmt is None else fmt
                if fmt not in FORMATS:
                    raise ValueError(f"{name}: format must be one of {', '.join(FORMATS)}")
                key = spec.get("key")
                key = name if key is None else key
                if not isinstance(key, str) or any(not p for p in key.split("/") if key):
                    raise ValueError(f"{name}: key must be an 'a/b/0' path or \"\" (whole file)")
                if fmt == "binary" and "key" in spec and key:
                    raise ValueError(f"{name}: format binary is the whole file; drop key")
                key = None if fmt == "binary" or not key else tuple(key.split("/"))
                secret = Secret(**common, sops_file=sops_file, key=key, format=fmt)
        except (ValueError, TypeError) as e:
            errors.append(str(e))
            continue
        seen_paths[canon] = name
        result.append(secret)
    return Parsed(settings, result, errors)


def secret_paths(raw: Optional[Mapping]) -> Dict[str, str]:
    """name -> resolved path, for ${sopsPath:NAME} and other sections."""
    return {s.name: s.path for s in parse_sops(raw).secrets}


# --------------------------------------------------------------------------
# Decryption
# --------------------------------------------------------------------------


class DecryptError(Exception):
    pass


def _extract(doc, key: Tuple[str, ...], name: str) -> bytes:
    node = doc
    for i, part in enumerate(key):
        where = "/".join(key[: i + 1])
        if isinstance(node, list):
            if not part.isdigit() or int(part) >= len(node):
                raise DecryptError(f"{name}: no list index {where}")
            node = node[int(part)]
        elif isinstance(node, Mapping):
            if part not in node:
                raise DecryptError(f"{name}: key {where} not found")
            node = node[part]
        else:
            raise DecryptError(f"{name}: {where} is not a mapping or list")
    if isinstance(node, (Mapping, list)):
        raise DecryptError(f"{name}: key {'/'.join(key)} is a mapping/list, not a value")
    if not isinstance(node, str):
        # As sops-nix: a number would come back reformatted (1.10 -> 1.1).
        raise DecryptError(
            f"{name}: the value of key {'/'.join(key)} is not a string "
            f"(got {type(node).__name__}; quote it in the sops file)"
        )
    return node.encode("utf-8")


class Decryptor:
    """Runs sops once per (file, whole|structured) and caches the result."""

    def __init__(
        self,
        settings: Settings,
        *,
        runner: Runner = default_runner,
        which: Callable[[str], Optional[str]] = shutil.which,
        env: Optional[Mapping[str, str]] = None,
    ):
        self._settings = settings
        self._runner = runner
        self._which = which
        self._env = dict(os.environ if env is None else env)
        self._cache: Dict[Tuple[str, bool, str], object] = {}
        self._ssh_age_keys: Optional[List[str]] = None
        self._usable: Dict[str, bool] = {}

    def _binary(self) -> str:
        binary = self._settings.binary or self._which("sops")
        if not binary:
            raise DecryptError("sops binary not found on PATH (set sops.binary)")
        return binary

    def _convert_ssh_keys(self) -> List[str]:
        """age identities from ``sshKeyPaths`` via ``ssh-to-age``, as sops-nix
        converts them; kept in memory and handed to sops as SOPS_AGE_KEY.

        Like sops-nix, a key that cannot be read or converted is skipped.
        """
        paths = [p for p in self._settings.age_ssh_key_files if self._usable_ssh_key(p)]
        if not paths:
            return []
        tool = self._settings.ssh_to_age_binary or self._which("ssh-to-age")
        if not tool:
            log(
                "sops: ssh-to-age not found on PATH (set sops.sshToAgeBinary); "
                "sshKeyPaths are not converted to age keys",
                Color.YELLOW,
            )
            return []
        keys = []
        for path in paths:
            try:
                rc, out, err = self._runner([tool, "-private-key", "-i", path], self._env)
            except OSError as e:
                log(f"sops: cannot convert ssh key {path}: {e}", Color.YELLOW)
                continue
            key = out.decode("utf-8", "replace").strip()
            if rc != 0 or not key.startswith("AGE-SECRET-KEY-") or "\n" in key:
                last = (err or "").strip().splitlines()[-1:] or [f"exit {rc}"]
                log(f"sops: cannot convert ssh key {path}: {last[0][:200]}", Color.YELLOW)
                continue
            keys.append(key)
        return keys

    def _usable_ssh_key(self, path: str) -> bool:
        """A regular file this process can open for reading (actually opened:
        permission bits alone miss root and ACLs)."""
        if path not in self._usable:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                try:
                    self._usable[path] = stat.S_ISREG(os.fstat(fd).st_mode)
                finally:
                    os.close(fd)
            except OSError:
                self._usable[path] = False
            if not self._usable[path]:
                log(f"sops: skipping ssh key {path}: not a readable file", Color.YELLOW)
        return self._usable[path]

    def _age_keys(self) -> List[str]:
        if self._ssh_age_keys is None:
            self._ssh_age_keys = self._convert_ssh_keys()
        return self._ssh_age_keys

    def _run(self, sops_file: str, whole: bool, fmt: str = "yaml"):
        cache_key = (sops_file, whole, fmt)
        if cache_key not in self._cache:
            try:
                self._cache[cache_key] = self._decrypt(sops_file, whole, fmt)
            except DecryptError as e:
                self._cache[cache_key] = e
        cached = self._cache[cache_key]
        if isinstance(cached, DecryptError):
            raise cached
        return cached

    def _decrypt(self, sops_file: str, whole: bool, fmt: str = "yaml"):
        if not os.path.isfile(sops_file):
            raise DecryptError(f"sops file not found: {sops_file}")
        env = dict(self._env)
        if self._settings.age_key_file:
            env["SOPS_AGE_KEY_FILE"] = self._settings.age_key_file
        # sops reads one SSH key itself (for ssh-ed25519 recipients); the
        # converted identities cover age1 recipients, as in sops-nix.
        native = next(
            (p for p in self._settings.age_ssh_key_files if self._usable_ssh_key(p)), None
        )
        if native:
            env["SOPS_AGE_SSH_PRIVATE_KEY_FILE"] = native
        ssh_keys = self._age_keys()
        if ssh_keys:
            existing = env.get("SOPS_AGE_KEY")
            env["SOPS_AGE_KEY"] = "\n".join(([existing] if existing else []) + ssh_keys)
        # Without an explicit output type sops guesses it from the file extension.
        out_type = fmt if whole else "json"
        cmd = [self._binary(), "--decrypt", "--input-type", fmt, "--output-type", out_type]
        cmd.append(sops_file)
        try:
            rc, out, err = self._runner(cmd, env)
        except OSError as e:
            raise DecryptError(f"cannot run sops: {e}") from None
        if rc != 0:
            # Last line only: sops prints its error summary there, never values.
            last = (err or "").strip().splitlines()[-1:] or ["no output"]
            raise DecryptError(f"sops failed for {sops_file} (exit {rc}): {last[0][:200]}")
        if whole:
            return out
        try:
            return json.loads(out)
        except ValueError:
            raise DecryptError(f"sops output for {sops_file} is not JSON") from None

    def value(self, secret: Secret) -> bytes:
        if secret.generate is not None:
            raise DecryptError(f"{secret.name}: generated secrets are not decrypted")
        if secret.key is None:
            return self._run(secret.sops_file, True, secret.format)
        return _extract(self._run(secret.sops_file, False, secret.format), secret.key, secret.name)


# --------------------------------------------------------------------------
# Plan: read current, compute desired, diff. Shared by deploy and diff.
# --------------------------------------------------------------------------


@dataclass
class Change:
    secret: Secret
    # create | update | replace | chmod | generate | adopt | unchanged | pending | error
    action: str
    desired: Optional[bytes] = None
    error: Optional[str] = None


@dataclass
class Plan:
    errors: List[str]
    changes: List[Change]
    remove: List[str]  # paths
    forget: List[Tuple[str, str]]  # (path, reason)
    keep: List[Tuple[str, str]] = field(default_factory=list)  # (path, reason), still tracked


def plan(
    parsed: Parsed,
    installed: Mapping[str, Dict],
    decryptor: Decryptor,
    *,
    conflicts: Optional[Mapping[str, str]] = None,
    select: Optional[Callable[[str], bool]] = None,
    tail: bool = True,
) -> Plan:
    """``conflicts`` maps refused paths to a reason (see ``secrets_flow.Ownership``).

    State keys are matched to declared paths by file identity, not spelling.
    ``select`` limits the changes to some paths; ``tail`` adds the section's
    config errors and removals (judged against every declared secret).
    """
    conflicts = {} if conflicts is None else conflicts
    installed = fsutil.respell(dict(installed), [s.path for s in parsed.secrets])
    changes: List[Change] = []
    for s in parsed.secrets:
        if select is not None and not select(s.path):
            continue
        if s.path in conflicts:
            changes.append(Change(s, "error", error=f"{s.name}: {s.path} is {conflicts[s.path]}"))
            continue
        try:
            uid, gid = fsutil.resolve_uid(s.owner), fsutil.resolve_gid(s.group)
        except (KeyError, ValueError) as e:
            changes.append(Change(s, "error", error=f"{s.name}: unknown owner/group {e}"))
            continue
        current = fsutil.read_current(s.path)
        tracked = fsutil.same_identity(s.path, installed.get(s.path))

        if s.generate is not None:
            # The value is never rewritten; only mode/owner are reconciled.
            if not current.exists:
                changes.append(Change(s, "generate"))
            elif not tracked and not s.force:
                changes.append(
                    Change(
                        s,
                        "error",
                        error=f"{s.name}: {s.path} exists and is not tracked by tools; "
                        "set force: true to adopt it as the generated value",
                    )
                )
            elif current.error:
                changes.append(
                    Change(s, "error", error=f"{s.name}: cannot read {s.path} ({current.error})")
                )
            elif not fsutil.attrs_match(current, s.mode, uid, gid):
                changes.append(Change(s, "chmod"))
            else:
                changes.append(Change(s, "unchanged" if tracked else "adopt"))
            continue

        try:
            desired = decryptor.value(s)
        except DecryptError as e:
            awaiting = getattr(conflicts, "awaiting_clone", None)
            reason = awaiting(s.sops_file) if awaiting and s.sops_file else None
            if reason and not os.path.exists(s.sops_file):
                changes.append(Change(s, "pending", error=f"sopsFile {reason}"))
            else:
                changes.append(Change(s, "error", error=str(e)))
            continue
        if current.error:
            changes.append(
                Change(s, "error", error=f"{s.name}: cannot read {s.path} ({current.error})")
            )
        elif not current.exists:
            changes.append(Change(s, "create", desired))
        elif current.content != desired and not tracked:
            if s.force:
                changes.append(Change(s, "replace", desired))
            else:
                changes.append(
                    Change(
                        s,
                        "error",
                        error=f"{s.name}: {s.path} exists and is not tracked by tools; "
                        "set force: true to replace it",
                    )
                )
        elif current.content != desired:
            changes.append(Change(s, "update", desired))
        elif not fsutil.attrs_match(current, s.mode, uid, gid):
            changes.append(Change(s, "chmod", desired))
        else:
            changes.append(Change(s, "unchanged" if tracked else "adopt", desired))

    desired_paths = {s.path for s in parsed.secrets}
    remove: List[str] = []
    forget: List[Tuple[str, str]] = []
    keep: List[Tuple[str, str]] = []
    for path in sorted(set(installed) - desired_paths):
        blocked = fsutil.removal_blocked(conflicts, path)
        twin = fsutil.same_file_as(path, desired_paths)
        if twin:
            blocked = f"the same file as declared {twin}"
        if blocked:
            keep.append((path, blocked))
            continue
        record = installed.get(path) or {}
        if not os.path.lexists(path):
            forget.append((path, "already gone"))
        elif record.get("generated"):
            forget.append((path, "generated secret; kept on disk, delete by hand if unwanted"))
        elif fsutil.matches_fingerprint(path, record):
            remove.append(path)
        else:
            forget.append((path, "modified since tools wrote it; left in place"))
    if not tail:
        return Plan([], changes, [], [])
    return Plan(parsed.errors, changes, remove, forget, keep)


# --------------------------------------------------------------------------
# Deploy
# --------------------------------------------------------------------------


def _warn_if_shared(secrets_dir: str) -> None:
    """An existing secretsDir is never chmodded (it may be a shared dir)."""
    try:
        mode = fsutil.stat_mode(secrets_dir)
    except OSError:
        return
    if mode & 0o026:
        log(
            f"sops: secretsDir {secrets_dir} is group-writable or world-readable "
            f"({oct(mode)}); secret files stay private by mode, but consider a dedicated dir",
            Color.YELLOW,
        )


def _record(s: Secret) -> Dict:
    record = {"name": s.name, **(fsutil.fingerprint(s.path) or {})}
    if s.generate is not None:
        record["generated"] = True
    return record


@dataclass
class Result:
    success: bool
    changed: List[Secret] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)  # names


def apply(
    p: Plan, settings: Settings, installed: Dict[str, Dict], created_dirs: Optional[list] = None
) -> Result:
    """Execute a plan; mutates ``installed`` (the ownership records).

    Directories it creates are appended to ``created_dirs``.
    """
    res = Result(success=not p.errors)
    fsutil.respell(installed, [ch.secret.path for ch in p.changes])
    for err in p.errors:
        log(f"sops: {err}", Color.RED)
    if any(ch.action in ("create", "update", "replace", "generate") for ch in p.changes):
        _warn_if_shared(settings.secrets_dir)

    for ch in p.changes:
        s = ch.secret
        if ch.action in ("error", "pending"):
            log(f"sops: {ch.error}", Color.RED)
            res.success = False
            res.failed.append(s.name)
            continue
        if ch.action == "unchanged":
            if s.path in installed:
                fsutil.refresh_dev(s.path, installed[s.path])
            if s.generate is not None and s.path in installed:
                installed[s.path]["generated"] = True
            continue
        if ch.action == "adopt":
            installed[s.path] = _record(s)
            continue
        try:
            uid, gid = fsutil.resolve_uid(s.owner), fsutil.resolve_gid(s.group)
            if ch.action == "chmod":
                fsutil.apply_attrs(s.path, s.mode, uid, gid)
            else:
                fsutil.makedirs_secret(os.path.dirname(s.path), settings.secrets_dir, created_dirs)
                if ch.action == "generate":
                    g = s.generate
                    value = "".join(_secrets.choice(g.chars) for _ in range(g.length))
                    fsutil.atomic_write(s.path, value.encode(), s.mode, uid, gid, exclusive=True)
                else:
                    fsutil.atomic_write(s.path, ch.desired, s.mode, uid, gid)
        except (OSError, KeyError, ValueError) as e:
            log(f"sops: failed to write {s.path}: {e}", Color.RED)
            res.success = False
            res.failed.append(s.name)
            continue
        installed[s.path] = _record(s)
        res.changed.append(s)
        verb = {
            "create": "Decrypted",
            "update": "Updated",
            "replace": "Replaced untracked",
            "chmod": "Chmod",
            "generate": "Generated",
        }
        log(f"{verb[ch.action]}: {s.path} (secret)", Color.GREEN)

    # Removal only on a valid config: an unparsed entry looks undesired.
    for kept, reason in p.keep:
        log(f"sops: not removing {kept}: it is {reason}; still tracked", Color.YELLOW)
    if not p.errors:
        for path, reason in p.forget:
            if reason != "already gone":
                log(f"sops: releasing {path}: {reason}", Color.YELLOW)
            installed.pop(path, None)
        for path in p.remove:
            try:
                os.remove(path)
            except OSError as e:
                log(f"sops: failed to remove {path}: {e}", Color.RED)
                res.success = False
                continue
            installed.pop(path, None)
            log(f"Removed secret: {path}", Color.RED)

    if not res.changed and not p.remove:
        debug("All sops secrets up to date", Color.BLUE)
    return res


def describe(p: Plan) -> List[str]:
    """Diff rows. Paths and actions only, never values or hashes."""
    rows = [f"  ! {err}" for err in p.errors]
    labels = {
        "create": "+ decrypt",
        "update": "~ update",
        "replace": "~ replace",
        "chmod": "~ chmod",
        "generate": "+ generate",
    }
    for ch in p.changes:
        s = ch.secret
        if ch.action == "error":
            rows.append(f"  ! {ch.error}")
        elif ch.action == "pending":
            rows.append(f"  ~ decrypt {s.path} (pending: {ch.error})")
        elif ch.action in labels:
            suffix = {"chmod": f" -> {oct(s.mode)}", "replace": " (secret, not tracked by tools)"}
            suffix = suffix.get(ch.action, " (secret)")
            rows.append(f"  {labels[ch.action]} {s.path}{suffix}")
            rows += [f"    notify: {n}" for n in s.notify]
    rows += [f"  ~ keep {path} (is {reason}; not removed)" for path, reason in p.keep]
    return rows + fsutil.describe_removals(p.errors, p.remove, p.forget)


# --------------------------------------------------------------------------
# Secret lookup for templates (injected there as a callable)
# --------------------------------------------------------------------------


class SecretUnavailable(Exception):
    """A declared secret has no value yet (pending, failed or unreadable)."""


def make_lookup(
    parsed: Parsed,
    *,
    decryptor: Optional[Decryptor] = None,
    failed: Tuple[str, ...] = (),
    installed: Optional[Mapping[str, Dict]] = None,
) -> Callable[[str], str]:
    """name -> secret text.

    With a decryptor, decrypted secrets come from memory; deploy and diff
    both pass one, so they render the same value. Generated secrets (and
    every secret without a decryptor) are read from their file; with
    ``installed`` (sops state), only if it is still the file tools wrote.
    """
    by_name = {s.name: s for s in parsed.secrets}
    if installed is not None:
        installed = fsutil.respell(dict(installed), [s.path for s in parsed.secrets])

    def lookup(name: str) -> str:
        s = by_name.get(name)
        if s is None:
            if parsed.errors:
                raise PlaceholderError(
                    f"${{sops:{name}}}: sops config is invalid: {'; '.join(parsed.errors)}"
                )
            raise KeyError(name)
        if name in failed:
            raise SecretUnavailable(f"sops secret {name!r} failed to decrypt this run")
        try:
            if decryptor is not None and s.generate is None:
                data = decryptor.value(s)
            else:
                current = fsutil.read_current(s.path)
                if not current.exists:
                    raise SecretUnavailable(f"sops secret {name!r} is not on disk yet")
                if current.error:
                    raise OSError(current.error)
                if installed is not None and not fsutil.same_identity(
                    s.path, installed.get(s.path)
                ):
                    raise SecretUnavailable(
                        f"sops secret {name!r}: {s.path} is not the file tools wrote"
                    )
                data = current.content
        except (OSError, DecryptError) as e:
            raise SecretUnavailable(f"sops secret {name!r} unavailable: {e}") from None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise SecretUnavailable(f"sops secret {name!r} is not UTF-8 text") from None

    return lookup


def has_work(raw, state: Dict) -> bool:
    # Any present section reaches parse_sops, so its errors are reported.
    return raw is not None or bool(state.get("sops", {}).get("installed"))
