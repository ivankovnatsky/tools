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
      system:                        # `sudo -n defaults write <path> ...`
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
that landed). A key that leaves config gets its prior value back only if it
existed before tools and still holds `written` or `confirmed`; otherwise it
is left as is and forgotten.

The provider takes resolved data (`parse_defaults`) and a `DefaultsCLI`
runner, so any config frontend can drive it and tests can mock every call.
"""

import json
import os
import plistlib
import re
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from tools.log import Color, debug, log
from tools.system_paths import system_bin_optional
from tools.util import run_command

SCOPES = ("user", "currentHost", "system")
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
    domain: str
    key: str
    value: object

    @property
    def id(self) -> Tuple[str, str, str]:
        return (self.scope, self.domain, self.key)


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
_CASE_INSENSITIVE: Dict[int, bool] = {}


def _case_insensitive(directory: str) -> bool:
    """Whether the volume holding `directory` folds case; cached per device."""
    try:
        dev = os.stat(directory).st_dev
    except OSError:
        return False
    if dev not in _CASE_INSENSITIVE:
        result = False
        try:
            names = os.listdir(directory)[:64]
        except OSError:
            names = []
        for name in names:
            if name.swapcase() == name:
                continue
            try:
                a = os.lstat(os.path.join(directory, name))
                b = os.lstat(os.path.join(directory, name.swapcase()))
                result = (a.st_ino, a.st_dev) == (b.st_ino, b.st_dev)
            except OSError:
                pass
            break
        _CASE_INSENSITIVE[dev] = result
    return _CASE_INSENSITIVE[dev]


def _disk_path(path: str) -> str:
    """One spelling per file: parent symlinks resolved, each component in its
    on-disk case; a missing tail is case folded on case-insensitive volumes."""
    path = os.path.normpath(path)
    full = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
    out = "/"
    parts = [p for p in full.split("/") if p]
    for i, comp in enumerate(parts):
        insensitive = _case_insensitive(out)
        try:
            names = os.listdir(out)
        except OSError:
            names = []
        match = comp if comp in names else None
        if match is None and insensitive:
            match = next((n for n in names if n.casefold() == comp.casefold()), None)
        if match is None:
            rest = "/".join(parts[i:])
            return os.path.join(out, rest.casefold() if insensitive else rest)
        out = os.path.join(out, match)
    return out


def canonical_domain(domain: str) -> str:
    """The id of a preferences domain: `~/Library/Preferences/<name>` is just
    `<name>`, other paths are resolved by `_disk_path`, `.plist` is dropped."""
    if domain in _GLOBAL_NAMES:
        return "NSGlobalDomain"
    path = os.path.expanduser(domain) if domain.startswith("~") else domain
    if not os.path.isabs(path):
        return domain
    path = os.path.normpath(path)
    if path.lower().endswith(".plist"):
        path = path[: -len(".plist")]
    path = _disk_path(path + ".plist")[: -len(".plist")]
    prefs = _disk_path(os.path.join(os.path.expanduser("~"), "Library", "Preferences"))
    if os.path.dirname(path) == prefs:
        name = os.path.basename(path)
        return "NSGlobalDomain" if name in _GLOBAL_NAMES else name
    return path


def _normalize_domain(scope: str, domain: str) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(domain, str) or not domain.strip():
        return None, f"{scope}: domain must be a non-empty string, got {domain!r}"
    domain = canonical_domain(domain.strip())
    if scope == "system" and not os.path.isabs(domain):
        return None, f"system: domain must be an absolute plist path, got {domain!r}"
    return domain, None


_BYHOST_DOCK = re.compile(r"com\.apple\.dock(\.[0-9a-f-]{8,})?")


def is_dock_domain(domain: str) -> bool:
    """True if a canonical user/currentHost domain is the Dock's own prefs."""
    if not os.path.isabs(domain):
        return domain.lower() == DOCK_DOMAIN
    prefs = _disk_path(os.path.join(os.path.expanduser("~"), "Library", "Preferences"))
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
        spec.restart[normalized] = [p.strip() for p in procs]

    errors += structural_errors + restart_errors
    # Any error holds the whole section: no writes, no restores, no kills.
    if errors:
        return DefaultsSpec(), errors
    return spec, errors


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


@dataclass
class DefaultsCLI:
    """Every subprocess the defaults and dock providers make goes through here.

    `allow_sudo` is False for diff: a dry run never escalates, so a system
    plist unreadable to the user is reported instead of read.
    """

    defaults: str
    killall: Optional[str] = None
    sudo: Optional[str] = None
    run: Callable = run_command
    allow_sudo: bool = False
    is_root: bool = False
    readable: Callable[[str], bool] = lambda p: os.access(p, os.R_OK)
    exists: Callable[[str], bool] = os.path.exists

    @classmethod
    def for_host(cls, allow_sudo: bool) -> Optional["DefaultsCLI"]:
        defaults = system_bin_optional("defaults")
        if not defaults:
            return None
        return cls(
            defaults=defaults,
            killall=system_bin_optional("killall"),
            sudo=system_bin_optional("sudo"),
            allow_sudo=allow_sudo,
            is_root=os.geteuid() == 0,
        )

    def _prefix(self, scope: str, privileged: bool) -> List[str]:
        if scope == "currentHost":
            return [self.defaults, "-currentHost"]
        if scope == "system" and privileged and not self.is_root:
            if not self.sudo:
                raise DefaultsError("sudo not found")
            # -n: output is captured, so an interactive password prompt would hang unseen.
            return [self.sudo, "-n", self.defaults]
        return [self.defaults]

    def _check(self, cmd: List[str], what: str) -> str:
        rc, stdout, stderr = self.run(cmd)
        if rc != 0:
            msg = (stderr or stdout or "").strip() or f"exit {rc}"
            if cmd[0] == self.sudo and "password" in msg:
                msg += " (run `sudo -v` first)"
            raise DefaultsError(f"{what}: {msg}")
        return stdout

    def export(self, scope: str, domain: str) -> Dict:
        privileged = False
        if scope == "system":
            plist = domain + ".plist"
            # `defaults export` prints an empty dict for an unreadable file, which
            # would read as "no prior value" and skip a later restore.
            if self.exists(plist) and not self.readable(plist):
                if not self.allow_sudo:
                    raise DefaultsError(f"{plist} is not readable without sudo")
                privileged = True
        out = self._check(
            self._prefix(scope, privileged) + ["export", domain, "-"], f"export {domain}"
        )
        try:
            data = plistlib.loads(out.encode())
        except Exception as e:
            raise DefaultsError(f"export {domain}: unparsable output: {e}") from e
        if not isinstance(data, dict):
            raise DefaultsError(f"export {domain}: expected a dict")
        return data

    def write(self, scope: str, domain: str, key: str, value) -> None:
        self._check(
            self._prefix(scope, True) + ["write", domain, key, to_xml(value)],
            f"write {domain} {key}",
        )

    def restart(self, process: str) -> Optional[str]:
        """Kill process so it relaunches with new prefs. Returns a warning or None."""
        if not self.killall:
            return "killall not found"
        rc, stdout, stderr = self.run([self.killall, process])
        if rc != 0:
            return (stderr or stdout or "").strip() or f"exit {rc}"
        return None


# --------------------------------------------------------------------------
# Plan: shared by deploy and diff
# --------------------------------------------------------------------------


def _managed(state: Dict) -> Dict:
    return state.get("defaults", {}).get("managed", {}) or {}


def _tracked_entries(managed: Dict):
    for scope, domains in managed.items():
        for domain, keys in (domains or {}).items():
            for key, entry in (keys or {}).items():
                yield (scope, domain, key), entry


def plan(spec: DefaultsSpec, errors: List[str], state: Dict, cli: DefaultsCLI) -> Dict[str, List]:
    if errors:
        spec = DefaultsSpec()
    managed = _managed(state)
    tracked = dict(_tracked_entries(managed))
    result: Dict[str, List] = {
        "errors": list(errors),
        "read_errors": [],  # (scope, domain, message)
        "set": [],  # (DefaultsKey, current)
        "refresh": [],  # DefaultsKey: already equal, state records a stale write
        "restore": [],  # (id, prior)
        "forget": [],  # (id, reason)
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

    for res in spec.keys:
        values = read(res.scope, res.domain)
        if values is None:
            continue
        cur = values.get(res.key, MISSING)
        if same(cur, res.value):
            entry = tracked.get(res.id)
            want = to_xml(res.value)
            if entry is not None and (
                entry.get("written") != want or entry.get("confirmed", want) != want
            ):
                result["refresh"].append(res)
        else:
            result["set"].append((res, cur))

    # Removals wait for a clean config: an invalid entry looks undesired when
    # it is merely unparsed.
    desired = {res.id for res in spec.keys}
    if not errors:
        for ident in sorted(set(tracked) - desired):
            entry = tracked[ident] or {}
            values = read(ident[0], ident[1])
            if values is None:
                continue
            cur = values.get(ident[2], MISSING)
            prior = from_xml(entry.get("prior"))
            ours = [from_xml(entry.get("written")), from_xml(entry.get("confirmed"))]
            reason = _removal(cur, prior, ours)
            if reason:
                result["forget"].append((ident, reason))
            else:
                result["restore"].append((ident, prior))

    old_restart = state.get("defaults", {}).get("restart", {}) or {}
    changed = [res.domain for res, _ in result["set"]]
    changed += [ident[1] for ident, _ in result["restore"]]
    desired_domains = {res.domain for res in spec.keys}
    result["restart"] = restarts_for(changed, spec.restart, old_restart, desired_domains)
    return result


def _eq(a, b) -> bool:
    return a is b if MISSING in (a, b) else same(a, b)


def _removal(cur, prior, ours: List) -> Optional[str]:
    """Why a key that left config is forgotten, or None to restore prior."""
    if _eq(cur, prior):
        return "already gone"
    if prior is MISSING:
        return "not set before tools; left as is"
    if any(v is not MISSING and _eq(cur, v) for v in ours):
        return None
    return "changed by hand; left as is"


def restarts_for(
    domains: List[str], restart: Dict, fallback: Dict, desired_domains: set
) -> List[str]:
    """Processes to restart. The current `restart:` entry wins; the last
    deploy's map only covers domains that left config entirely."""
    procs: List[str] = []
    for domain in domains:
        for proc in _restart_entry(domain, restart, fallback, desired_domains):
            if proc not in procs:
                procs.append(proc)
    return procs


def _restart_entry(domain: str, restart: Dict, fallback: Dict, desired_domains: set) -> List[str]:
    if domain in restart:
        return restart[domain]
    if domain in desired_domains:
        return []
    return fallback.get(domain, [])


def _label(scope: str, domain: str, key: str) -> str:
    where = domain if scope == "user" else f"{scope}:{domain}"
    suffix = " (sudo)" if scope == "system" else ""
    return f"{where} {key}{suffix}"


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
    cli = cli or DefaultsCLI.for_host(allow_sudo=False)
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
    section = state.setdefault("defaults", {})
    managed = section.setdefault("managed", {})
    success = True

    p = plan(spec, errors, state, cli)
    for err in p["errors"]:
        log(f"defaults: {err}", Color.RED)
        success = False
    for scope, domain, msg in p["read_errors"]:
        log(f"defaults: skipping {scope}:{domain}: {msg}", Color.RED)
        success = False

    old_restart = section.get("restart", {}) or {}
    desired_domains = {res.domain for res in spec.keys}
    changed_domains: List[str] = []

    def changed(domain: str) -> None:
        changed_domains.append(domain)
        for proc in restarts_for([domain], spec.restart, old_restart, desired_domains):
            queue_restart(queue, proc)

    def failed(ident, e: BaseException) -> None:
        nonlocal success
        success = False
        if isinstance(e, DefaultsError):
            log(f"defaults: {e}", Color.RED)
        else:
            log(f"defaults: {_label(*ident)}: {type(e).__name__}: {e}", Color.RED)
            changed(ident[1])

    for res, cur in p["set"]:
        slot = _entry_slot(managed, res.id)
        fresh = res.key not in slot
        if fresh:
            slot[res.key] = {"prior": None if cur is MISSING else to_xml(cur), "written": None}
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
            failed(res.id, e)
            if isinstance(e, DefaultsError):
                if fresh:
                    _drop(managed, res.id)
                else:
                    slot[res.key]["written"] = before
            elif not isinstance(e, Exception):
                raise
            continue
        slot[res.key]["confirmed"] = slot[res.key]["written"]
        changed(res.domain)
        log(f"Wrote: {_label(*res.id)} = {fmt_value(res.value)}", Color.GREEN)

    for res in p["refresh"]:
        slot = _entry_slot(managed, res.id)[res.key]
        slot["written"] = slot["confirmed"] = to_xml(res.value)

    for ident, prior in p["restore"]:
        try:
            cli.write(ident[0], ident[1], ident[2], prior)
        except BaseException as e:
            failed(ident, e)
            if not isinstance(e, Exception):
                raise
            continue
        _drop(managed, ident)
        changed(ident[1])
        log(f"Restored: {_label(*ident)} = {fmt_value(prior)}", Color.YELLOW)

    for ident, reason in p["forget"]:
        if reason != "already gone":
            log(f"defaults: releasing {_label(*ident)}: {reason}", Color.YELLOW)
        _drop(managed, ident)

    # Kept per tracked domain so a later restore still restarts the right
    # process after the whole section (and its restart map) left config.
    new_restart = {}
    for domain in sorted({d for domains in managed.values() for d in domains}):
        procs = _restart_entry(domain, spec.restart, old_restart, desired_domains)
        if procs:
            new_restart[domain] = procs
    section["restart"] = new_restart

    if not changed_domains:
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
    p = plan(spec, errors, state, cli)
    changes = [f"  ! {err}" for err in p["errors"]]
    changes += [
        f"  ? cannot read {scope}:{domain}: {msg}" for scope, domain, msg in p["read_errors"]
    ]
    for res, cur in p["set"]:
        label = _label(*res.id)
        if cur is MISSING:
            changes.append(f"  + write {label} = {fmt_value(res.value)}")
        else:
            changes.append(f"  ~ write {label}: {fmt_value(cur)} -> {fmt_value(res.value)}")
    changes += [f"  - restore {_label(*i)} -> {fmt_value(v)}" for i, v in p["restore"]]
    changes += [
        f"  ~ forget {_label(*i)} ({reason})"
        for i, reason in p["forget"]
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
    cli = cli or DefaultsCLI.for_host(allow_sudo=True)
    if cli is None:
        log("defaults: `defaults` binary not found, skipping", Color.RED)
        return False
    spec, errors = parse_defaults(raw)
    return apply_defaults(spec, errors, state, cli, restarts, persist)


def diff_defaults(
    raw, state: Dict, cli: Optional[DefaultsCLI] = None, restarts: Optional[List[str]] = None
) -> List[str]:
    if sys.platform != "darwin":
        return []
    cli = cli or DefaultsCLI.for_host(allow_sudo=False)
    if cli is None:
        return ["  ? `defaults` binary not found, cannot diff"]
    spec, errors = parse_defaults(raw)
    return preview_defaults(spec, errors, state, cli, restarts)
