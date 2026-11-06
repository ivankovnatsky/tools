"""Validation helpers for turning raw mappings into service specs."""

import os
from typing import Any, Dict, Iterable, List, Optional

from tools.mgmt.engine import Endpoint
from tools.mgmt.http import InvalidSpec

# Common to every service (`prune` lifts the delete-everything guard).
ENDPOINT_KEYS = {"baseUrl", "baseUrlFiles", "timeout", "retries", "retryDelay", "prune"}

# Inline values for these keys would put credentials into config files.
INLINE_SECRET_KEYS = {"apiKey", "password", "token", "webhook", "discordWebhook"}


def present(raw: Dict, key: str) -> bool:
    """Managed only when set: a YAML null (children commented out) is unmanaged."""
    return raw.get(key) is not None


def prune(raw: Dict, where: str) -> bool:
    return bool(boolean(raw, "prune", where, False))


def mapping(raw: Any, where: str) -> Dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise InvalidSpec(f"{where} must be a mapping, got {type(raw).__name__}")
    return raw


def items(raw: Any, where: str) -> List[Dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise InvalidSpec(f"{where} must be a list, got {type(raw).__name__}")
    return [mapping(x, f"{where}[{i}]") for i, x in enumerate(raw)]


def strings(raw: Any, where: str) -> List[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise InvalidSpec(f"{where} must be a list of strings")
    return list(raw)


def check_keys(raw: Dict, allowed: Iterable[str], where: str) -> None:
    inline = sorted(set(raw) & INLINE_SECRET_KEYS)
    if inline:
        raise InvalidSpec(
            f"{where}: inline secret {inline[0]!r} is not allowed; use {inline[0]}File"
        )
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        raise InvalidSpec(f"{where}: unknown key(s) {', '.join(unknown)}")


def string(raw: Dict, key: str, where: str, default: Optional[str] = None) -> Optional[str]:
    value = raw.get(key, default)
    if value is not None and not isinstance(value, str):
        raise InvalidSpec(f"{where}.{key} must be a string")
    return value


def required(raw: Dict, key: str, where: str) -> str:
    value = string(raw, key, where)
    if not value:
        raise InvalidSpec(f"{where}.{key} is required")
    return value


def boolean(raw: Dict, key: str, where: str, default: Optional[bool] = None) -> Optional[bool]:
    value = raw.get(key, default)
    if value is not None and not isinstance(value, bool):
        raise InvalidSpec(f"{where}.{key} must be true or false")
    return value


def integer(raw: Dict, key: str, where: str, default: Optional[int] = None) -> Optional[int]:
    value = raw.get(key, default)
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise InvalidSpec(f"{where}.{key} must be an integer")
    return value


def path(raw: Dict, key: str, where: str, base_dir: Optional[str]) -> Optional[str]:
    value = string(raw, key, where)
    if not value:
        return None
    value = os.path.expanduser(value)
    if base_dir and not os.path.isabs(value):
        value = os.path.join(base_dir, value)
    return value


def one_of(raw: Dict, key: str, where: str, base_dir: Optional[str]) -> Dict[str, str]:
    """Exactly one of `key` (inline, non-secret) or `keyFile`."""
    inline = string(raw, key, where)
    file = path(raw, f"{key}File", where, base_dir)
    if bool(inline) == bool(file):
        raise InvalidSpec(f"{where}: set exactly one of {key} or {key}File")
    return {"value": inline} if inline else {"file": file}


def endpoint(
    raw: Dict, where: str, default_url: Optional[str], base_dir: Optional[str]
) -> Endpoint:
    url = string(raw, "baseUrl", where, default_url)
    if not url:
        raise InvalidSpec(f"{where}.baseUrl is required")
    files = mapping(raw.get("baseUrlFiles"), f"{where}.baseUrlFiles")
    resolved = {}
    for name, p in files.items():
        if f"@{name}@" not in url:
            raise InvalidSpec(f"{where}.baseUrlFiles.{name}: no @{name}@ in baseUrl")
        resolved[name] = path({"p": p}, "p", f"{where}.baseUrlFiles.{name}", base_dir)
    ep = Endpoint(url=url, url_files=resolved)
    timeout = raw.get("timeout")
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise InvalidSpec(f"{where}.timeout must be a positive number")
        ep.timeout = float(timeout)
    retries = integer(raw, "retries", where)
    if retries is not None:
        if retries < 0:
            raise InvalidSpec(f"{where}.retries must be >= 0")
        ep.retries = retries
    delay = raw.get("retryDelay")
    if delay is not None:
        if isinstance(delay, bool) or not isinstance(delay, (int, float)) or delay < 0:
            raise InvalidSpec(f"{where}.retryDelay must be a non-negative number")
        ep.retry_delay = float(delay)
    return ep
