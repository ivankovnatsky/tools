"""templates: render config files from plain data into a service's own format.

Input is plain, already-resolved data keyed by template name::

    {
      "<name>": {
        "path": "~/.config/app/app.ini",     # identity; default <secretsDir>/rendered/<name>
        "format": "ini",                     # yaml | json | toml | ini | env | raw
        "content": {...} | "text",           # mapping/list, or text for raw
        "mode": "0400", "owner": ..., "group": ...,
        "secrets": False,                    # force secret handling
        "restartUnits": ["launchd:org.example.app"],
        "onChange": "shell command",         # run once after a write
      },
    }

Option names and the 0400 mode default follow sops-nix templates.

String leaves use the render grammar: ``${sops:NAME}`` is resolved through
an injected ``secret_lookup(name) -> str`` and ``$$`` is a literal ``$``
(``placeholders.encode_literal`` produces it). Any other placeholder is an
error; the frontend resolves vars before this point.
A template that pulls in a secret (or sets ``secrets: true``) never shows
content in diff or deploy output.
"""

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from tools import fsutil
from tools.log import Color, debug, log
from tools.placeholders import PlaceholderError, Resolver
from tools.serialize import FORMATS, SerializeError, serialize
from tools.util import format_diff_bytes, looks_like_secret, parse_on_change

_KEYS = {
    "path",
    "format",
    "content",
    "mode",
    "owner",
    "group",
    "secrets",
    "restartUnits",
    "quote",
    "force",
    "onChange",
}
DEFAULT_MODE = 0o400

SecretLookup = Callable[[str], str]


@dataclass(frozen=True)
class Template:
    name: str
    target: str  # identity
    format: str
    content: Any
    mode: Optional[int] = None
    owner: Optional[str] = None
    group: Optional[str] = None
    secrets: bool = False
    notify: Tuple[str, ...] = ()
    quote: bool = True
    force: bool = False  # replace an existing file tools does not track
    on_change: Optional[str] = None


def parse_templates(
    raw: Optional[Mapping],
    secrets_dir: Optional[str] = None,
    secrets_dir_error: Optional[str] = None,
) -> Tuple[List[Template], List[str]]:
    """``secrets_dir`` (the sops section's) gives the default ``path``;
    ``secrets_dir_error`` says why there is none (an invalid sops section)."""
    raw = {} if raw is None else raw
    if not isinstance(raw, Mapping):
        return [], [f"templates must be a mapping of name -> spec, got {type(raw).__name__}"]
    result: List[Template] = []
    errors: List[str] = []
    seen: Dict[str, str] = {}
    for name, spec in raw.items():
        try:
            if not isinstance(spec, Mapping):
                raise ValueError(f"{name}: spec must be a mapping")
            unknown = set(spec) - _KEYS
            if unknown:
                raise ValueError(f"{name}: unknown keys {sorted(unknown)}")
            path = spec.get("path")
            if path is None and secrets_dir_error:
                raise ValueError(
                    f"{name}: no path, and the default needs a valid sops section "
                    f"({secrets_dir_error})"
                )
            if path is None and secrets_dir:
                if not isinstance(name, str) or "/" in name or name in ("", ".", ".."):
                    raise ValueError(f"{name}: name cannot be a file name; set path")
                path = os.path.join(secrets_dir, "rendered", name)
            if not isinstance(path, str) or not path:
                raise ValueError(f"{name}: path is required")
            target = os.path.normpath(os.path.expanduser(path))
            if not os.path.isabs(target):
                raise ValueError(f"{name}: path must be absolute (or start with ~)")
            canon = fsutil.canonical_path(target)
            if canon in seen:
                raise ValueError(f"{name}: path {target} already used by {seen[canon]}")
            fmt = spec.get("format", "raw")
            if fmt not in FORMATS:
                raise ValueError(f"{name}: format must be one of {', '.join(FORMATS)}")
            if "content" not in spec:
                raise ValueError(f"{name}: content is required")
            if fmt == "raw" and not isinstance(spec["content"], str):
                raise ValueError(f"{name}: raw content must be a string")
            if fmt in ("ini", "env", "toml") and not isinstance(spec["content"], Mapping):
                raise ValueError(f"{name}: {fmt} content must be a mapping")
            if fmt in ("json", "yaml") and not isinstance(spec["content"], (Mapping, list)):
                raise ValueError(f"{name}: {fmt} content must be a mapping or a list")
            secret_flag = spec.get("secrets", False)
            if not isinstance(secret_flag, bool):
                raise ValueError(f"{name}: secrets must be a bool")
            notify = spec.get("restartUnits")
            notify = [] if notify is None else notify
            if not isinstance(notify, list) or not all(isinstance(n, str) for n in notify):
                raise ValueError(f"{name}: restartUnits must be a list of strings")
            force = spec.get("force", False)
            if not isinstance(force, bool):
                raise ValueError(f"{name}: force must be a bool")
            quote = spec.get("quote", True)
            if not isinstance(quote, bool) or ("quote" in spec and fmt != "env"):
                raise ValueError(f"{name}: quote must be a bool and only applies to format env")
            on_change = parse_on_change(spec.get("onChange"), name)
            mode = fsutil.parse_mode(spec.get("mode"), f"templates.{name}")
            fsutil.check_owner(spec.get("owner"), f"{name}: owner")
            fsutil.check_owner(spec.get("group"), f"{name}: group")
        except (ValueError, TypeError) as e:
            errors.append(str(e))
            continue
        seen[canon] = name
        result.append(
            Template(
                name=name,
                target=target,
                format=fmt,
                content=spec["content"],
                mode=mode,
                owner=spec.get("owner"),
                group=spec.get("group"),
                secrets=secret_flag,
                notify=tuple(notify),
                quote=quote,
                force=force,
                on_change=on_change,
            )
        )
    return result, errors


def render(t: Template, secret_lookup: Optional[SecretLookup]) -> Tuple[bytes, bool]:
    """Return (bytes, secret_derived). Raises PlaceholderError/SerializeError,
    or whatever ``secret_lookup`` raises for an unavailable secret."""
    resolver = Resolver(secret=secret_lookup, render=True)
    data = resolver.resolve(t.content, f"templates.{t.name}.content")
    text = serialize(t.format, data, quote=t.quote)
    return text.encode("utf-8"), t.secrets or bool(resolver.used_secrets)


def secret_refs(t: Template) -> set:
    """Names of the ``${sops:NAME}`` secrets ``t`` uses."""
    names: set = set()

    def record(name: str) -> str:
        names.add(name)
        return ""

    try:
        render(t, record)
    except (PlaceholderError, SerializeError):
        pass
    return names


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------


@dataclass
class Change:
    template: Template
    # create | update | chmod | adopt | unchanged | pending | error
    action: str
    desired: Optional[bytes] = None
    secret: bool = False
    mode: int = 0o644
    masked: bool = False
    untracked: bool = False  # replaces a file tools never wrote: content never shown
    diff: Optional[str] = None
    error: Optional[str] = None


@dataclass
class Plan:
    errors: List[str]
    changes: List[Change]
    remove: List[str]
    forget: List[Tuple[str, str]]
    keep: List[Tuple[str, str]] = field(default_factory=list)  # (path, reason), still tracked
    held: bool = False  # removals held by an invalid entry outside ``select``


def plan(
    templates: List[Template],
    errors: List[str],
    installed: Mapping[str, Dict],
    secret_lookup: Optional[SecretLookup],
    *,
    conflicts: Optional[Mapping[str, str]] = None,
    pending_errors: Tuple[type, ...] = (),
    select: Optional[Callable[[str], bool]] = None,
    tail: bool = True,
) -> Plan:
    """``conflicts`` maps refused paths to a reason (see ``secrets_flow.Ownership``).

    Exceptions in ``pending_errors`` raised by ``secret_lookup`` mean the
    secret is not available yet (shown as pending, not as an error).
    ``select``/``tail`` split the plan as in ``sops.plan``.
    """
    conflicts = {} if conflicts is None else conflicts
    installed = fsutil.respell(dict(installed), [t.target for t in templates])
    errors = list(errors) if tail else []
    changes: List[Change] = []
    held = False
    for t in templates:
        if select is not None and not select(t.target):
            # Reported by the plan that selects it; here it only holds removals.
            if tail and not held:
                try:
                    render(t, secret_lookup)
                    fsutil.resolve_uid(t.owner), fsutil.resolve_gid(t.group)
                except pending_errors:
                    pass
                except (PlaceholderError, SerializeError, KeyError, ValueError):
                    held = True
            continue
        record = installed.get(t.target) or {}
        tracked = fsutil.same_identity(t.target, record)
        if t.target in conflicts:
            changes.append(
                Change(t, "error", error=f"{t.name}: {t.target} is {conflicts[t.target]}")
            )
            continue
        try:
            desired, secret = render(t, secret_lookup)
        except pending_errors as e:
            changes.append(Change(t, "pending", error=str(e)))
            continue
        except (PlaceholderError, SerializeError) as e:
            # An invalid entry, like a parse error, holds every removal.
            errors.append(f"{t.name}: {e}")
            continue
        mode = DEFAULT_MODE if t.mode is None else t.mode
        try:
            uid, gid = fsutil.resolve_uid(t.owner), fsutil.resolve_gid(t.group)
        except (KeyError, ValueError) as e:
            errors.append(f"{t.name}: unknown owner/group {e}")
            continue
        current = fsutil.read_current(t.target)
        ch = Change(t, "unchanged", desired, secret=secret, mode=mode)
        if current.error:
            ch.action, ch.error = "error", f"{t.name}: cannot read {t.target} ({current.error})"
        elif not current.exists:
            ch.action = "create"
        elif current.content != desired and not tracked and not t.force:
            ch.action = "error"
            ch.error = (
                f"{t.name}: {t.target} exists and is not tracked by tools; "
                "set force: true to replace it"
            )
        elif current.content != desired:
            ch.action = "update"
            ch.untracked = not tracked
            # The old content may hold a secret the new one no longer does.
            ch.masked = bool(
                ch.untracked
                or secret
                or record.get("secret")
                or looks_like_secret(desired)
                or looks_like_secret(current.content)
            )
            if not ch.masked:
                ch.diff = format_diff_bytes(desired, current.content, t.target)
        elif not fsutil.attrs_match(current, mode, uid, gid):
            ch.action = "chmod"
            # Same bytes as before: whatever made them secret still holds.
            ch.secret = secret or bool(record.get("secret"))
        elif not tracked:
            ch.action = "adopt"
        changes.append(ch)

    desired_targets = {t.target for t in templates}
    remove: List[str] = []
    forget: List[Tuple[str, str]] = []
    keep: List[Tuple[str, str]] = []
    for target in sorted(set(installed) - desired_targets):
        blocked = fsutil.removal_blocked(conflicts, target)
        twin = fsutil.same_file_as(target, desired_targets)
        if twin:
            blocked = f"the same file as declared {twin}"
        if blocked:
            keep.append((target, blocked))
            continue
        if not os.path.lexists(target):
            forget.append((target, "already gone"))
        elif fsutil.matches_fingerprint(target, installed.get(target) or {}):
            remove.append(target)
        else:
            forget.append((target, "modified since tools wrote it; left in place"))
    if not tail:
        return Plan(errors, changes, [], [])
    return Plan(errors, changes, remove, forget, keep, held)


# --------------------------------------------------------------------------
# Deploy / diff
# --------------------------------------------------------------------------


@dataclass
class Result:
    success: bool
    changed: List[Template] = field(default_factory=list)


def _record(ch: Change) -> Dict:
    return {
        "name": ch.template.name,
        "secret": ch.secret,
        **(fsutil.fingerprint(ch.template.target) or {}),
    }


def apply(
    p: Plan,
    installed: Dict[str, Dict],
    private_root: Optional[str] = None,
    created_dirs: Optional[list] = None,
) -> Result:
    """Directories under ``private_root`` (the secretsDir) are created like the
    sops section's; those created are appended to ``created_dirs``."""
    res = Result(success=not p.errors)
    fsutil.respell(installed, [ch.template.target for ch in p.changes])
    for err in p.errors:
        log(f"templates: {err}", Color.RED)
    for ch in p.changes:
        t = ch.template
        if ch.action in ("error", "pending"):
            log(f"templates: {ch.error}", Color.RED)
            res.success = False
            continue
        if ch.action == "unchanged":
            fsutil.refresh_dev(t.target, installed[t.target])
            if ch.secret and not installed[t.target].get("secret"):
                installed[t.target]["secret"] = True
            continue
        if ch.action == "adopt":
            installed[t.target] = _record(ch)
            continue
        try:
            uid, gid = fsutil.resolve_uid(t.owner), fsutil.resolve_gid(t.group)
            if ch.action == "chmod":
                fsutil.apply_attrs(t.target, ch.mode, uid, gid)
            else:
                fsutil.makedirs_secret(os.path.dirname(t.target), private_root, created_dirs)
                fsutil.atomic_write(t.target, ch.desired, ch.mode, uid, gid)
        except (OSError, KeyError, ValueError) as e:
            log(f"templates: failed to write {t.target}: {e}", Color.RED)
            res.success = False
            continue
        installed[t.target] = _record(ch)
        res.changed.append(t)
        verb = {"create": "Rendered", "update": "Updated", "chmod": "Chmod"}[ch.action]
        if ch.untracked:
            verb = "Replaced untracked"
        log(f"{verb}: {t.target}{' (secret)' if ch.secret else ''}", Color.GREEN)
        if ch.diff:
            print(ch.diff)

    for kept, reason in p.keep:
        log(f"templates: not removing {kept}: it is {reason}; still tracked", Color.YELLOW)
    if not p.errors and not p.held:
        for target, reason in p.forget:
            if reason != "already gone":
                log(f"templates: releasing {target}: {reason}", Color.YELLOW)
            installed.pop(target, None)
        for target in p.remove:
            try:
                os.remove(target)
            except OSError as e:
                log(f"templates: failed to remove {target}: {e}", Color.RED)
                res.success = False
                continue
            installed.pop(target, None)
            log(f"Removed: {target}", Color.RED)

    if not res.changed and not p.remove:
        debug("All templates up to date", Color.BLUE)
    return res


def describe(p: Plan) -> List[str]:
    rows = [f"  ! {err}" for err in p.errors]
    for ch in p.changes:
        t = ch.template
        if ch.action == "error":
            rows.append(f"  ! {ch.error}")
            continue
        if ch.action == "pending":
            rows.append(f"  ~ render {t.target} (pending: {ch.error})")
        elif ch.action == "create":
            rows.append(f"  + render {t.target}{' (secret)' if ch.secret else ''}")
        elif ch.action == "update":
            if ch.untracked:
                rows.append(f"  ~ replace {t.target} (not tracked by tools; content not shown)")
            elif ch.masked:
                rows.append(f"  ~ update {t.target} (secret, diff suppressed)")
            else:
                rows.append(f"  ~ update {t.target}")
                if ch.diff:
                    rows.append(ch.diff)
        elif ch.action == "chmod":
            rows.append(f"  ~ chmod {t.target} -> {oct(ch.mode)}")
        else:
            continue
        rows += [f"    notify: {n}" for n in t.notify]
        if t.on_change and ch.action != "pending":
            rows.append(f"    run onChange: {t.on_change}")
    rows += [f"  ~ keep {target} (is {reason}; not removed)" for target, reason in p.keep]
    return rows + fsutil.describe_removals(p.errors or p.held, p.remove, p.forget)


def has_work(raw, state: Dict) -> bool:
    if raw is not None and not isinstance(raw, Mapping):
        return True
    return bool(raw or state.get("templates", {}).get("installed"))
