"""macOS preferences: reconcile `defaults` keys.

Config shape::

    defaults:
      user:                          # `defaults write <domain> <key> <value>`
        NSGlobalDomain:
          AppleKeyboardUIMode: 3
        com.apple.dock:
          tilesize: 64
          autohide-delay: 1000.0     # float -> <real>, int -> <integer>
        ~/Library/Preferences/ByHost/com.apple.controlcenter:  # as nix-darwin writes it
          Weather: 2
      currentHost:                   # `defaults -currentHost write ...` (<domain>.<UUID>.plist)
        com.example.app:
          k: 1
      system:                        # as root only: `sudo tools deploy --scope defaults`
        /Library/Preferences/com.example.app:
          Enabled: true
      restart:                       # killed after a change in that domain
        com.apple.dock: Dock
        com.apple.finder: [Finder]

Values may be bool, int, float, string, list or dict, nested freely, and
are always written as an XML plist value, so every type round-trips without
`-bool`/`-int` flags. Current values are read with `defaults export` and
compared type-strictly: `true` is not `1` and `64` is not `64.0`.

Ownership: the first write of a key records its prior value in state, and
`written` is saved before every write (`confirmed` keeps the last value
that landed). A key that leaves config while it still holds `written` or
`confirmed` gets its prior value back, or is deleted if it did not exist
before tools; a key changed by hand is left as is and forgotten.

`parse_defaults` is pure: it validates and spells domains lexically, and
that spelling is what `defaults` is given. `identify` (run by the provider)
resolves, per scope, the plist file each spelling touches (symlinks,
on-disk case, case folded on case-insensitive volumes). That identity keys
state, dedup and restart matching, never a write target; each state entry
also keeps the resolved, case-kept `target` it was written through, so a
symlink repointed elsewhere leaves the old plist restored or deleted there
and the new one gets its own prior. The provider takes parsed data and a
`DefaultsCLI` runner, so any config frontend can drive it and tests can
mock every call.

Each run owns some scopes: the login user `user` and `currentHost`, root
(`sudo tools deploy`) only `system`. Keys and state of the other scopes are
never written; diff as the user still previews `system` keys read-only.
"""

import copy
import json
import os
import plistlib
import re
import sys
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional, Tuple

from tools import fsutil, privilege
from tools.log import Color, debug, log
from tools.system_paths import system_bin_optional
from tools.util import run_command

SCOPES = ("user", "currentHost", "system")
USER_SCOPES = ("user", "currentHost")
ROOT_SCOPES = ("system",)
ROOT_HINT = "run sudo tools deploy --scope defaults"
HOME_ERROR = "is under the user's home; root never writes in user space"
_SECTION_KEYS = set(SCOPES) | {"restart"}

# Owned by the dock section; two writers would fight over the layout.
DOCK_DOMAIN = "com.apple.dock"
DOCK_KEYS = ("persistent-apps", "persistent-others")

_INT_MIN, _INT_MAX = -(2**63), 2**64 - 1


class _Missing:
    def __repr__(self):
        return "<missing>"


MISSING = _Missing()


class DefaultsError(Exception):
    """A `defaults` invocation failed or its output was unusable."""


# --------------------------------------------------------------------------
# Resources
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DefaultsKey:
    scope: str  # user | currentHost | system
    domain: str  # as passed to `defaults`
    key: str
    value: object
    identity: Optional[str] = None  # set by `identify`: keys state
    target: Optional[str] = None  # set by `identify`: resolved, case kept

    @property
    def id(self) -> Tuple[str, str, str]:
        return (self.scope, self.domain, self.key)

    @property
    def sid(self) -> Tuple[str, str, str]:
        return (self.scope, self.identity or self.domain, self.key)


@dataclass
class DefaultsSpec:
    keys: List[DefaultsKey] = field(default_factory=list)
    restart: Dict[str, List[str]] = field(default_factory=dict)  # domain -> processes


# --------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------


def to_xml(value) -> str:
    return plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=False).decode()


def from_xml(text: Optional[str]):
    return MISSING if text is None else plistlib.loads(text.encode())


def same(a, b) -> bool:
    """Plist-typed equality: bool, int and float never compare equal to each other."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is bool and type(b) is bool and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, int) and isinstance(b, int):
        return a == b
    return type(a) is type(b) and a == b


def xml_error(value, where: str) -> Optional[str]:
    """Error if plistlib refuses value (e.g. control characters in a string)."""
    try:
        to_xml(value)
    except (ValueError, TypeError, OverflowError) as e:
        return f"{where}: not plist-safe: {e}"
    return None


def validate_value(value, where: str) -> Optional[str]:
    """Error message if value cannot be stored in a plist, else None."""
    if isinstance(value, bool) or isinstance(value, (str, float, bytes)):
        return None
    if isinstance(value, int):
        if not _INT_MIN <= value <= _INT_MAX:
            return f"{where}: integer {value} out of plist range"
        return None
    if isinstance(value, list):
        for i, item in enumerate(value):
            err = validate_value(item, f"{where}[{i}]")
            if err:
                return err
        return None
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                return f"{where}: dict keys must be strings, got {k!r}"
            err = validate_value(v, f"{where}.{k}")
            if err:
                return err
        return None
    if value is None:
        return f"{where}: null has no plist type"
    return f"{where}: unsupported value type {type(value).__name__}"


def fmt_value(value) -> str:
    if value is MISSING:
        return "<unset>"
    text = json.dumps(
        value,
        ensure_ascii=False,
        default=lambda o: f"<{len(o)} bytes>" if isinstance(o, bytes) else str(o),
    )
    return text if len(text) <= 60 else text[:57] + "..."


# --------------------------------------------------------------------------
# Parse: raw mapping -> resources
# --------------------------------------------------------------------------


_GLOBAL_NAMES = {"-g", ".GlobalPreferences", "NSGlobalDomain"}


def _home_prefs() -> str:
    return os.path.normpath(os.path.join(os.path.expanduser("~"), "Library", "Preferences"))


def normalize_domain(domain: str) -> str:
    """Lexical spelling of a preferences domain, without touching the disk:
    `~/Library/Preferences/<name>` is `<name>`, `-g` is `NSGlobalDomain`,
    other paths are normalized and `.plist` is dropped."""
    if domain in _GLOBAL_NAMES:
        return "NSGlobalDomain"
    path = os.path.expanduser(domain) if domain.startswith("~") else domain
    if not os.path.isabs(path):
        return domain
    path = os.path.normpath(path)
    if path.lower().endswith(".plist"):
        path = path[: -len(".plist")]
    if os.path.dirname(path) == _home_prefs():
        name = os.path.basename(path)
        return "NSGlobalDomain" if name in _GLOBAL_NAMES else name
    return path


def _disk_path(path: str) -> str:
    """Parent symlinks resolved, each existing component in its on-disk case."""
    path = os.path.normpath(path)
    full = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
    out = "/"
    parts = [p for p in full.split("/") if p]
    for i, comp in enumerate(parts):
        insensitive = fsutil._case_insensitive(out)
        try:
            names = os.listdir(out)
        except OSError:
            names = []
        match = comp if comp in names else None
        if match is None and insensitive:
            match = next((n for n in names if n.casefold() == comp.casefold()), None)
        if match is None:
            return os.path.join(out, *parts[i:])
        out = os.path.join(out, match)
    return out


def _disk_prefs(scope: str = "user") -> str:
    """The dir a bare domain name lives in for `scope` (home Preferences, or
    its ByHost for currentHost), fully resolved, symlinks included."""
    prefs = _home_prefs()
    if scope == "currentHost":
        prefs = os.path.join(prefs, "ByHost")
    return _disk_path(os.path.realpath(prefs))


def canonical_domain(domain: str, scope: str = "user") -> str:
    """The plist `defaults` touches for `domain` in `scope`, spelled on disk:
    a path has its parents resolved by `_disk_path`; a file in the scope's
    bare-name dir is its bare name (NSGlobalDomain for `.GlobalPreferences`).
    For currentHost the `.<UUID>` suffix is implied. Case is not folded, so
    this is safe to pass to `defaults`."""
    domain = normalize_domain(domain)
    if domain == "NSGlobalDomain" or (not os.path.isabs(domain) and scope == "system"):
        return domain
    if os.path.isabs(domain):
        path = _disk_path(domain + ".plist")
    else:
        path = _disk_path(os.path.join(_disk_prefs(scope), domain + ".plist"))
    path = path[: -len(".plist")]
    if scope != "system" and os.path.dirname(path) == _disk_prefs(scope):
        name = os.path.basename(path)
        return "NSGlobalDomain" if name in _GLOBAL_NAMES else name
    return path


def domain_identity(domain: str, scope: str = "user") -> str:
    """Which plist `domain` names in `scope`: `canonical_domain`, case folded
    on case-insensitive volumes (where any case of `.GlobalPreferences` is
    NSGlobalDomain). Keys state, dedup and restart matching; never a write
    target."""
    canon = canonical_domain(domain, scope)
    if canon == "NSGlobalDomain":
        return canon
    if os.path.isabs(canon):
        return fsutil._fold(canon, os.path.dirname(canon))
    base = _disk_prefs(scope)
    ident = fsutil._fold(os.path.join(base, canon), base)
    if ident == fsutil._fold(os.path.join(base, ".GlobalPreferences"), base):
        return "NSGlobalDomain"
    return os.path.basename(ident)


def _identity_cache() -> Callable[[str, str], str]:
    cache: Dict[Tuple[str, str], str] = {}

    def ident_of(scope: str, domain: str) -> str:
        if (scope, domain) not in cache:
            cache[(scope, domain)] = domain_identity(domain, scope)
        return cache[(scope, domain)]

    return ident_of


def _merge_procs(into: List[str], procs) -> List[str]:
    into += [p for p in procs if p not in into]
    return into


def _normalize_domain(scope: str, domain: str) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(domain, str) or not domain.strip():
        return None, f"{scope}: domain must be a non-empty string, got {domain!r}"
    domain = normalize_domain(domain.strip())
    if scope == "system" and not os.path.isabs(domain):
        return None, f"system: domain must be an absolute plist path, got {domain!r}"
    return domain, None


_BYHOST_DOCK = re.compile(r"com\.apple\.dock(\.[0-9a-f-]{8,})?")


def is_dock_domain(domain: str, prefs: Optional[str] = None) -> bool:
    """True if a user/currentHost domain is the Dock's own prefs. `prefs` is
    the home Preferences dir as `domain` spells it (lexical by default)."""
    if not os.path.isabs(domain):
        return domain.lower() == DOCK_DOMAIN
    prefs = _home_prefs() if prefs is None else prefs
    parent, base = os.path.split(domain)
    return parent.lower() == os.path.join(prefs, "ByHost").lower() and (
        _BYHOST_DOCK.fullmatch(base.lower()) is not None
    )


def parse_defaults(raw) -> Tuple[DefaultsSpec, List[str]]:
    """Validate a raw `defaults` mapping. Returns (spec, errors)."""
    spec = DefaultsSpec()
    errors: List[str] = []
    if raw is None:
        return spec, errors
    if not isinstance(raw, dict):
        return spec, [f"defaults must be a mapping, got {type(raw).__name__}"]
    unknown = set(raw) - _SECTION_KEYS
    if unknown:
        errors.append(f"unknown keys {sorted(unknown)} (expected {sorted(_SECTION_KEYS)})")

    # Only None means absent: `user: []` must not read as "no keys" and
    # restore everything tracked in that scope.
    structural_errors: List[str] = []
    seen = set()
    for scope in SCOPES:
        domains = raw.get(scope)
        if domains is None:
            continue
        if not isinstance(domains, dict):
            structural_errors.append(f"{scope}: must map domain -> {{key: value}}")
            continue
        for raw_domain, keys in domains.items():
            domain, err = _normalize_domain(scope, raw_domain)
            if err:
                errors.append(err)
                continue
            if not isinstance(keys, dict):
                errors.append(f"{scope}:{domain}: must map key -> value")
                continue
            for key, value in keys.items():
                where = f"{scope}:{domain} {key}"
                if not isinstance(key, str) or not key:
                    errors.append(f"{scope}:{domain}: key must be a non-empty string, got {key!r}")
                    continue
                if value is None:
                    continue
                if scope != "system" and key in DOCK_KEYS and is_dock_domain(domain):
                    errors.append(f"{where}: managed by the dock section")
                    continue
                err = validate_value(value, where) or xml_error(value, where)
                if err:
                    errors.append(err)
                    continue
                ident = (scope, domain, key)
                if ident in seen:
                    errors.append(f"{where}: declared more than once")
                    continue
                seen.add(ident)
                spec.keys.append(DefaultsKey(scope, domain, key, value))

    restart_errors: List[str] = []
    restart = {} if raw.get("restart") is None else raw["restart"]
    if not isinstance(restart, dict):
        restart_errors.append("restart: must map domain -> process name(s)")
        restart = {}
    for domain, procs in restart.items():
        if isinstance(procs, str):
            procs = [procs]
        if (
            not isinstance(domain, str)
            or not isinstance(procs, list)
            or not all(isinstance(p, str) and p.strip() for p in procs)
        ):
            restart_errors.append(f"restart: {domain!r} must map to a process name or list of them")
            continue
        normalized, err = _normalize_domain("restart", domain)
        if err:
            restart_errors.append(err)
            continue
        _merge_procs(spec.restart.setdefault(normalized, []), [p.strip() for p in procs])

    errors += structural_errors + restart_errors
    # Any error holds the whole section: no writes, no restores, no kills.
    if errors:
        return DefaultsSpec(), errors
    return spec, errors


# --------------------------------------------------------------------------
# Identity: resources -> on-disk identities (reads the filesystem)
# --------------------------------------------------------------------------


def identify(spec: DefaultsSpec, errors: List[str]) -> Tuple[DefaultsSpec, List[str]]:
    """Give each key its scope-aware plist identity and resolved target. Two
    spellings of one plist in a scope, a key declared twice, or a Dock key
    reached through a path are config errors that hold the whole section."""
    if errors:
        return DefaultsSpec(), list(errors)
    errors = []
    ident_of = _identity_cache()

    keys: List[DefaultsKey] = []
    seen = set()
    spelled: Dict[Tuple[str, str], str] = {}
    for res in spec.keys:
        where = f"{res.scope}:{res.domain} {res.key}"
        ident = ident_of(res.scope, res.domain)
        target = canonical_domain(res.domain, res.scope)
        first = spelled.setdefault((res.scope, ident), res.domain)
        if first != res.domain:
            errors.append(f"{where}: same plist as {first}; declared more than once")
            continue
        if res.scope != "system" and res.key in DOCK_KEYS and os.path.isabs(res.domain):
            if is_dock_domain(target, _disk_prefs()):
                errors.append(f"{where}: managed by the dock section")
                continue
        if (res.scope, ident, res.key) in seen:
            errors.append(f"{where}: declared more than once")
            continue
        seen.add((res.scope, ident, res.key))
        keys.append(replace(res, identity=ident, target=target))
    if errors:
        return DefaultsSpec(), errors
    return DefaultsSpec(keys=keys, restart=dict(spec.restart)), errors


def migrate_state(section: Dict) -> Dict:
    """Re-key state recorded by domain spelling (before `target` was kept per
    entry) by plist identity, the spelling becoming the entry's `target`, and
    the flat `restart` map into {scope: {identity: procs}}. In place."""
    ident_of = _identity_cache()
    managed: Dict = {}
    for scope, domains in (section.get("managed") or {}).items():
        for domain, keys in (domains or {}).items():
            for key, entry in (keys or {}).items():
                entry = dict(entry or {})
                ident = domain
                if "target" not in entry:
                    entry["target"] = domain
                    ident = ident_of(scope, domain)
                managed.setdefault(scope, {}).setdefault(ident, {}).setdefault(key, entry)
    restart = section.get("restart") or {}
    if any(isinstance(procs, list) for procs in restart.values()):
        nested: Dict = {}
        for scope, domains in managed.items():
            for ident in domains:
                for domain, procs in restart.items():
                    if isinstance(procs, list) and ident_of(scope, domain) == ident:
                        _merge_procs(nested.setdefault(scope, {}).setdefault(ident, []), procs)
        restart = nested
    section["managed"] = managed
    section["restart"] = restart
    return section


@dataclass
class _Restarts:
    """Restart maps matched by scope-aware plist identity, so any spelling of
    a domain in `restart:`, keys or state selects the same processes. The
    current `restart:` entry wins; the last deploy's map only covers plists
    that left config entirely."""

    restart: Dict[str, List[str]]
    fallback: Dict[str, Dict[str, List[str]]]
    desired: set
    ident_of: Callable[[str, str], str]

    @classmethod
    def build(cls, spec: DefaultsSpec, old_restart: Dict) -> "_Restarts":
        return cls(
            restart=spec.restart,
            fallback=old_restart or {},
            desired={(res.scope, res.identity) for res in spec.keys},
            ident_of=_identity_cache(),
        )

    def entry(self, scope: str, ident: str) -> List[str]:
        matched = [
            procs for domain, procs in self.restart.items() if self.ident_of(scope, domain) == ident
        ]
        if matched:
            out: List[str] = []
            for procs in matched:
                _merge_procs(out, procs)
            return out
        if (scope, ident) in self.desired:
            return []
        return list((self.fallback.get(scope) or {}).get(ident, []))

    def for_plists(self, plists) -> List[str]:
        out: List[str] = []
        for scope, ident in plists:
            _merge_procs(out, self.entry(scope, ident))
        return out


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


@dataclass
class DefaultsCLI:
    """Every subprocess the defaults and dock providers make goes through here.

    Writes are gated by who runs tools: `system` only under sudo, the user's scopes only as
    the user. A plist the caller can't read is reported, never escalated.
    """

    defaults: str
    killall: Optional[str] = None
    run: Callable = run_command
    is_root: bool = False
    home: Optional[str] = None  # the invoking user's home; root never writes under it
    login: Optional[str] = None  # the invoking user; root restarts only their processes
    readable: Callable[[str], bool] = lambda p: os.access(p, os.R_OK)
    exists: Callable[[str], bool] = os.path.exists
    stat: Callable[[str], os.stat_result] = os.stat
    lstat: Callable[[str], os.stat_result] = os.lstat

    @classmethod
    def for_host(cls) -> Optional["DefaultsCLI"]:
        defaults = system_bin_optional("defaults")
        if not defaults:
            return None
        is_root = os.geteuid() == 0
        try:
            inv = privilege.invoker()
            home, login = inv.home, inv.login
        except privilege.SudoUserError:
            home = login = None
        return cls(
            defaults=defaults,
            killall=system_bin_optional("killall"),
            is_root=is_root,
            home=home,
            login=login,
        )

    @property
    def scopes(self) -> Tuple[str, ...]:
        """The scopes this run may write."""
        return ROOT_SCOPES if self.is_root else USER_SCOPES

    def _prefix(self, scope: str) -> List[str]:
        if scope == "currentHost":
            return [self.defaults, "-currentHost"]
        return [self.defaults]

    def in_home(self, *paths: Optional[str]) -> Optional[str]:
        """As root, the first path under the user's home (or the home itself, unknown)."""
        if not self.is_root:
            return None
        if not self.home:
            return "(unknown user home)"
        # `defaults` opens <domain>.plist, which may itself be a symlink into the home.
        candidates = [c for p in paths if p for c in (p, p + ".plist")]
        return next((p for p in candidates if privilege.under_home(p, self.home)), None)

    def root_error(self, *paths: Optional[str]) -> Optional[str]:
        """As root, why `defaults` must not write these system domains: under the user's
        home, or `<domain>.plist` is a symlink or in a dir a non-root user can change."""
        if not self.is_root:
            return None
        if self.in_home(*paths):
            return HOME_ERROR
        for p in dict.fromkeys(p for p in paths if p):
            why = privilege.root_file_error(p + ".plist", self.stat, self.lstat)
            if why:
                return why
        return None

    def _writable(self, scope: str, domain: str, what: str) -> None:
        if scope in self.scopes:
            why = self.root_error(domain) if scope == "system" else None
            if why:
                raise DefaultsError(f"{what}: {why}")
            return
        if scope == "system":
            raise DefaultsError(f"{what}: system defaults need root; {ROOT_HINT}")
        raise DefaultsError(f"{what}: root never writes user preferences; run without sudo")

    def _check(self, cmd: List[str], what: str) -> str:
        rc, stdout, stderr = self.run(cmd)
        if rc != 0:
            msg = (stderr or stdout or "").strip() or f"exit {rc}"
            raise DefaultsError(f"{what}: {msg}")
        return stdout

    def export(self, scope: str, domain: str) -> Dict:
        if scope == "system":
            plist = domain + ".plist"
            # `defaults export` prints an empty dict for an unreadable file, which
            # would read as "no prior value" and skip a later restore.
            if self.exists(plist) and not self.readable(plist):
                raise DefaultsError(f"{plist} is not readable")
        out = self._check(self._prefix(scope) + ["export", domain, "-"], f"export {domain}")
        try:
            data = plistlib.loads(out.encode())
        except Exception as e:
            raise DefaultsError(f"export {domain}: unparsable output: {e}") from e
        if not isinstance(data, dict):
            raise DefaultsError(f"export {domain}: expected a dict")
        return data

    def write(self, scope: str, domain: str, key: str, value) -> None:
        what = f"write {domain} {key}"
        self._writable(scope, domain, what)
        self._check(self._prefix(scope) + ["write", domain, key, to_xml(value)], what)

    def delete(self, scope: str, domain: str, key: str) -> None:
        what = f"delete {domain} {key}"
        self._writable(scope, domain, what)
        self._check(self._prefix(scope) + ["delete", domain, key], what)

    def restart(self, process: str) -> Optional[str]:
        """Kill process so it relaunches with new prefs. Returns a warning or None."""
        if not self.killall:
            return "killall not found"
        cmd = [self.killall, process]
        if self.is_root:
            # Root's killall matches every user's process; restart only the invoking user's.
            if not self.login:
                return "no invoking user to restart it for"
            cmd = [self.killall, "-u", self.login, process]
        rc, stdout, stderr = self.run(cmd)
        if rc != 0:
            return (stderr or stdout or "").strip() or f"exit {rc}"
        return None


# --------------------------------------------------------------------------
# Plan: shared by deploy and diff
# --------------------------------------------------------------------------


def _managed(state: Dict) -> Dict:
    return state.get("defaults", {}).get("managed", {}) or {}


def _tracked_entries(managed: Dict):
    for scope, idents in managed.items():
        for ident, keys in (idents or {}).items():
            for key, entry in (keys or {}).items():
                yield (scope, ident, key), entry


def plan(
    spec: DefaultsSpec,
    errors: List[str],
    state: Dict,
    cli: DefaultsCLI,
    scopes: Tuple[str, ...] = SCOPES,
    preview: Tuple[str, ...] = (),
) -> Dict[str, List]:
    """Read-only; `result["spec"]` is the identified spec the plan refers to.
    State ids are (scope, plist identity, key); removal targets are the
    declared spelling of that plist if still declared, else the resolved
    spelling recorded at write time. Only keys and state in `scopes` are
    planned; keys in `preview` are compared too, but never restored or restarted."""
    spec, errors = identify(spec, errors)
    section = migrate_state(copy.deepcopy(state.get("defaults", {}) or {}))
    tracked = {
        sid: entry for sid, entry in _tracked_entries(section["managed"]) if sid[0] in scopes
    }
    result: Dict = {
        "spec": spec,
        "errors": list(errors),
        "read_errors": [],  # (scope, domain, message)
        "root_errors": [],  # (scope, domain, why): under sudo, plist unsafe for root to write
        "set": [],  # (DefaultsKey, current)
        "refresh": [],  # DefaultsKey: already equal, state is stale or retargeted
        "restore": [],  # (state id, (scope, target, key), prior)
        "delete": [],  # (state id, (scope, target, key)): not set before tools
        "forget": [],  # (state id, (scope, target, key), reason)
        "restart": [],  # process names
    }

    current: Dict[Tuple[str, str], object] = {}

    def read(scope: str, domain: str):
        if (scope, domain) not in current:
            try:
                current[(scope, domain)] = cli.export(scope, domain)
            except DefaultsError as e:
                current[(scope, domain)] = e
                result["read_errors"].append((scope, domain, str(e)))
        values = current[(scope, domain)]
        return None if isinstance(values, DefaultsError) else values

    def unsafe(scope: str, domain: str, *paths) -> bool:
        why = cli.root_error(domain, *paths) if scope == "system" else None
        if not why:
            return False
        if not any(e[:2] == (scope, domain) for e in result["root_errors"]):
            result["root_errors"].append((scope, domain, why))
        return True

    keys = [res for res in spec.keys if res.scope in scopes or res.scope in preview]
    for res in keys:
        if unsafe(res.scope, res.domain, res.target):
            continue
        values = read(res.scope, res.domain)
        if values is None:
            continue
        cur = values.get(res.key, MISSING)
        if same(cur, res.value):
            entry = tracked.get(res.sid)
            want = to_xml(res.value)
            if entry is not None and (
                entry.get("written") != want
                or entry.get("confirmed", want) != want
                or entry.get("target") != res.target
            ):
                result["refresh"].append(res)
        else:
            result["set"].append((res, cur))

    # Removals wait for a clean config: an invalid entry looks undesired when
    # it is merely unparsed.
    desired = {res.sid for res in keys}
    declared = {(res.scope, res.identity): res.domain for res in keys}
    if not errors:
        for sid in sorted(set(tracked) - desired):
            entry = tracked[sid] or {}
            scope, ident, key = sid
            target = declared.get((scope, ident)) or entry.get("target") or ident
            wid = (scope, target, key)
            if unsafe(scope, target, ident, entry.get("target")):
                continue
            values = read(scope, target)
            if values is None:
                continue
            cur = values.get(key, MISSING)
            prior = from_xml(entry.get("prior"))
            ours = [from_xml(entry.get("written")), from_xml(entry.get("confirmed"))]
            reason = _removal(cur, prior, ours)
            if reason:
                result["forget"].append((sid, wid, reason))
            elif prior is MISSING:
                result["delete"].append((sid, wid))
            else:
                result["restore"].append((sid, wid, prior))

    changed = [res.sid[:2] for res, _ in result["set"] if res.scope in scopes]
    changed += [sid[:2] for sid, _, _ in result["restore"]]
    changed += [sid[:2] for sid, _ in result["delete"]]
    restarts = _Restarts.build(spec, section["restart"])
    result["restart"] = restarts.for_plists(changed)
    return result


def _eq(a, b) -> bool:
    return a is b if MISSING in (a, b) else same(a, b)


def _removal(cur, prior, ours: List) -> Optional[str]:
    """Why a key that left config is forgotten, or None to restore prior
    (delete it, when prior is MISSING)."""
    if _eq(cur, prior):
        return "already gone"
    if any(v is not MISSING and _eq(cur, v) for v in ours):
        return None
    return "changed by hand; left as is"


def _label(scope: str, domain: str, key: str) -> str:
    where = domain if scope == "user" else f"{scope}:{domain}"
    return f"{where} {key}"


# --------------------------------------------------------------------------
# Deploy
# --------------------------------------------------------------------------


def _entry_slot(managed: Dict, ident) -> Dict:
    scope, domain, _ = ident
    return managed.setdefault(scope, {}).setdefault(domain, {})


def _drop(managed: Dict, ident) -> None:
    scope, domain, key = ident
    keys = managed.get(scope, {}).get(domain, {})
    keys.pop(key, None)
    if not keys:
        managed.get(scope, {}).pop(domain, None)
    if not managed.get(scope):
        managed.pop(scope, None)


def queue_restart(restarts: List[str], process: str) -> None:
    if process not in restarts:
        restarts.append(process)


def run_restarts(restarts: List[str], cli: Optional[DefaultsCLI] = None) -> None:
    """Kill each queued process once, after every section has written."""
    if not restarts:
        return
    cli = cli or DefaultsCLI.for_host()
    for proc in restarts:
        warning = "`defaults` binary not found" if cli is None else cli.restart(proc)
        if warning:
            log(f"restart {proc}: {warning}", Color.YELLOW)
        else:
            log(f"Restarted: {proc}", Color.GREEN)


def apply_defaults(
    spec: DefaultsSpec,
    errors: List[str],
    state: Dict,
    cli: DefaultsCLI,
    restarts: Optional[List[str]] = None,
    persist: Callable[[], None] = lambda: None,
) -> bool:
    """With `restarts`, processes to kill are queued there for the caller to run.
    `persist` saves state; it runs before each write so `written` is on disk first."""
    queue: List[str] = [] if restarts is None else restarts
    try:
        return _apply_defaults(spec, errors, state, cli, queue, persist)
    finally:
        if restarts is None:
            run_restarts(queue, cli)


def _apply_defaults(
    spec: DefaultsSpec,
    errors: List[str],
    state: Dict,
    cli: DefaultsCLI,
    queue: List[str],
    persist: Callable[[], None],
) -> bool:
    p = plan(spec, errors, state, cli, cli.scopes)
    spec = p["spec"]
    section = migrate_state(state.setdefault("defaults", {}))
    managed = section["managed"]
    success = True

    for err in p["errors"]:
        log(f"defaults: {err}", Color.RED)
        success = False
    for scope, domain, msg in p["read_errors"]:
        log(f"defaults: skipping {scope}:{domain}: {msg}", Color.RED)
        success = False
    for scope, domain, why in p["root_errors"]:
        log(f"defaults: skipping {scope}:{domain}: {why}", Color.RED)
        success = False

    restarts = _Restarts.build(spec, section["restart"])
    changed_plists: List[Tuple[str, str]] = []

    def changed(sid) -> None:
        changed_plists.append(sid[:2])
        for proc in restarts.for_plists([sid[:2]]):
            queue_restart(queue, proc)

    def failed(sid, wid, e: BaseException) -> None:
        nonlocal success
        success = False
        if isinstance(e, DefaultsError):
            log(f"defaults: {e}", Color.RED)
        else:
            log(f"defaults: {_label(*wid)}: {type(e).__name__}: {e}", Color.RED)
            changed(sid)

    for res, cur in p["set"]:
        slot = _entry_slot(managed, res.sid)
        fresh = res.key not in slot
        if fresh:
            slot[res.key] = {"prior": None if cur is MISSING else to_xml(cur), "written": None}
        slot[res.key]["target"] = res.target
        before = slot[res.key]["written"]
        # `confirmed` is the last value known to have landed.
        if before is not None and _eq(cur, from_xml(before)):
            slot[res.key]["confirmed"] = before
        slot[res.key].setdefault("confirmed", before)
        slot[res.key]["written"] = to_xml(res.value)
        persist()
        try:
            cli.write(res.scope, res.domain, res.key, res.value)
        except BaseException as e:
            failed(res.sid, res.id, e)
            if isinstance(e, DefaultsError):
                if fresh:
                    _drop(managed, res.sid)
                else:
                    slot[res.key]["written"] = before
            elif not isinstance(e, Exception):
                raise
            continue
        slot[res.key]["confirmed"] = slot[res.key]["written"]
        changed(res.sid)
        log(f"Wrote: {_label(*res.id)} = {fmt_value(res.value)}", Color.GREEN)

    for res in p["refresh"]:
        slot = _entry_slot(managed, res.sid)[res.key]
        slot["written"] = slot["confirmed"] = to_xml(res.value)
        slot["target"] = res.target

    for sid, wid, prior in p["restore"]:
        try:
            cli.write(wid[0], wid[1], wid[2], prior)
        except BaseException as e:
            failed(sid, wid, e)
            if not isinstance(e, Exception):
                raise
            continue
        _drop(managed, sid)
        changed(sid)
        log(f"Restored: {_label(*wid)} = {fmt_value(prior)}", Color.YELLOW)

    for sid, wid in p["delete"]:
        try:
            cli.delete(*wid)
        except BaseException as e:
            failed(sid, wid, e)
            if not isinstance(e, Exception):
                raise
            continue
        _drop(managed, sid)
        changed(sid)
        log(f"Deleted: {_label(*wid)}", Color.YELLOW)

    for sid, wid, reason in p["forget"]:
        if reason != "already gone":
            log(f"defaults: releasing {_label(*wid)}: {reason}", Color.YELLOW)
        _drop(managed, sid)

    # Kept per tracked plist so a later restore still restarts the right
    # process after the whole section (and its restart map) left config.
    new_restart: Dict = {}
    for scope, idents in sorted(managed.items()):
        for ident in sorted(idents):
            if scope not in cli.scopes:
                procs = (section["restart"].get(scope) or {}).get(ident)
            else:
                procs = restarts.entry(scope, ident)
            if procs:
                new_restart.setdefault(scope, {})[ident] = procs
    section["restart"] = new_restart

    if not changed_plists:
        debug("All defaults up to date", Color.BLUE)
    return success


# --------------------------------------------------------------------------
# Diff (read-only)
# --------------------------------------------------------------------------


def preview_defaults(
    spec: DefaultsSpec,
    errors: List[str],
    state: Dict,
    cli: DefaultsCLI,
    restarts: Optional[List[str]] = None,
) -> List[str]:
    preview = () if cli.is_root or privilege.UNDER_SUDO else ROOT_SCOPES
    p = plan(spec, errors, state, cli, cli.scopes, preview)
    # Keys this run can't write are a note, not a change: deploy would not touch them.
    pending = [res for res, _cur in p["set"] if res.scope not in cli.scopes]
    unreadable = [r for r in p["read_errors"] if r[0] not in cli.scopes]
    if pending:
        log(f"defaults: {len(pending)} system key(s) differ; {ROOT_HINT}", Color.YELLOW)
    for scope, domain, msg in unreadable:
        log(f"defaults: cannot read {scope}:{domain}: {msg}; {ROOT_HINT}", Color.YELLOW)
    changes = [f"  ! {err}" for err in p["errors"]]
    changes += [
        f"  ? cannot read {scope}:{domain}: {msg}"
        for scope, domain, msg in p["read_errors"]
        if scope in cli.scopes
    ]
    changes += [f"  ! {scope}:{domain} {why}" for scope, domain, why in p["root_errors"]]
    for res, cur in p["set"]:
        if res.scope not in cli.scopes:
            continue
        label = _label(*res.id)
        if cur is MISSING:
            changes.append(f"  + write {label} = {fmt_value(res.value)}")
        else:
            changes.append(f"  ~ write {label}: {fmt_value(cur)} -> {fmt_value(res.value)}")
    changes += [f"  - restore {_label(*w)} -> {fmt_value(v)}" for _, w, v in p["restore"]]
    changes += [f"  - delete {_label(*w)}" for _, w in p["delete"]]
    changes += [
        f"  ~ forget {_label(*w)} ({reason})"
        for _, w, reason in p["forget"]
        if reason != "already gone"
    ]
    if restarts is None:
        changes += [f"  ~ restart {proc}" for proc in p["restart"]]
    else:
        for proc in p["restart"]:
            queue_restart(restarts, proc)
    return changes


# --------------------------------------------------------------------------
# Config-dict adapters used by cli/diff
# --------------------------------------------------------------------------


def _note_unmanaged(spec: DefaultsSpec, cli: DefaultsCLI) -> None:
    others = [res for res in spec.keys if res.scope not in cli.scopes]
    if not others or privilege.UNDER_SUDO:
        return
    if cli.is_root:
        debug(f"defaults: {len(others)} user key(s) skipped as root; they run as your user")
    else:
        log(f"defaults: {len(others)} system key(s) not managed as user; {ROOT_HINT}", Color.YELLOW)


def has_work(raw, state: Dict) -> bool:
    return raw is not None or bool(_managed(state))


def install_defaults(
    raw,
    state: Dict,
    cli: Optional[DefaultsCLI] = None,
    restarts: Optional[List[str]] = None,
    persist: Callable[[], None] = lambda: None,
) -> bool:
    if sys.platform != "darwin":
        log("defaults: skipping, not macOS", Color.YELLOW)
        return True
    cli = cli or DefaultsCLI.for_host()
    if cli is None:
        log("defaults: `defaults` binary not found, skipping", Color.RED)
        return False
    spec, errors = parse_defaults(raw)
    _note_unmanaged(spec, cli)
    return apply_defaults(spec, errors, state, cli, restarts, persist)


def diff_defaults(
    raw, state: Dict, cli: Optional[DefaultsCLI] = None, restarts: Optional[List[str]] = None
) -> List[str]:
    if sys.platform != "darwin":
        return []
    cli = cli or DefaultsCLI.for_host()
    if cli is None:
        return ["  ? `defaults` binary not found, cannot diff"]
    spec, errors = parse_defaults(raw)
    return preview_defaults(spec, errors, state, cli, restarts)
