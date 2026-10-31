"""Canonical object identities, shared by config and remote objects.

Every match between a desired and a remote object goes through `index`, so
both sides are normalized the same way and two entries that normalize to the
same identity are an error before any write.
"""

import re
from typing import Any, Callable, Dict, Hashable, Iterable, List, Optional, Type

from tools.services.http import BadResponse, InvalidSpec, ServiceError, norm_path

Canon = Callable[[Any], Hashable]

_DEVICE_CHARS = re.compile(r"^[A-Z2-7]{56}$")


class _Bad(Exception):
    pass


def name(value: Any) -> str:
    """Generic identity. YAML `1` and `"1"` both become "1", so they collide."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise _Bad(f"expected a string, got {type(value).__name__}")
    value = str(value)
    if not value:
        raise _Bad("empty identity")
    return value


def folded(value: Any) -> str:
    """Case-insensitive names (Forgejo users and repositories)."""
    return name(value).lower()


def path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise _Bad(f"expected a path, got {value!r}")
    return norm_path(value)


def device_id(value: Any) -> str:
    """Syncthing device ID: uppercase, eight dash-separated groups of seven."""
    if not isinstance(value, str):
        raise _Bad(f"expected a device ID string, got {type(value).__name__}")
    raw = re.sub(r"[\s-]", "", value).upper()
    if not _DEVICE_CHARS.match(raw):
        raise _Bad(f"{value!r} is not a Syncthing device ID")
    return "-".join(raw[i : i + 7] for i in range(0, 56, 7))


def as_device_id(value: Any) -> Optional[str]:
    try:
        return device_id(value)
    except _Bad:
        return None


def compound(*kinds: Canon) -> Canon:
    """An identity made of several parts, e.g. (owner, repository)."""

    def canon_parts(value: Any) -> tuple:
        if not isinstance(value, tuple) or len(value) != len(kinds):
            raise _Bad(f"expected {len(kinds)} parts, got {value!r}")
        return tuple(k(v) for k, v in zip(kinds, value))

    return canon_parts


def canon(value: Any, what: str, kind: Canon = name, remote: bool = False) -> Hashable:
    error: Type[ServiceError] = BadResponse if remote else InvalidSpec
    try:
        return kind(value)
    except _Bad as e:
        raise error(f"{what}: {e}") from None


def index(
    items: Iterable,
    what: str,
    key: Callable[[Any], Any] = lambda x: x,
    kind: Canon = name,
    remote: bool = False,
) -> Dict[Hashable, Any]:
    """{canonical identity: item}; a duplicate makes the target ambiguous."""
    error: Type[ServiceError] = BadResponse if remote else InvalidSpec
    out: Dict[Hashable, Any] = {}
    for item in items:
        k = canon(key(item), what, kind, remote)
        if k in out:
            side = "more than one remote object is" if remote else "duplicate"
            raise error(f"{what}: {side} {k!r}")
        out[k] = item
    return out


def unique(values: Iterable, what: str, kind: Canon = name) -> List[Hashable]:
    """Canonical config identities, in order; duplicates are a config error."""
    return list(index(values, what, kind=kind))
