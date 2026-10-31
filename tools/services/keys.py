"""Per-host HMAC key for write-only secret fingerprints."""

import os
import secrets
import stat
from typing import Optional


class KeyFileError(Exception):
    """The key file exists but cannot be used."""


def key_path(state_file: str) -> str:
    """The key lives next to the state file, never inside it."""
    return os.path.join(os.path.dirname(os.path.abspath(state_file)), "services.key")


def load_key(path: str, create: bool) -> Optional[bytes]:
    """Read the key, creating it (0600) when `create`; None if absent otherwise.

    Raises KeyFileError for an unreadable, empty or truncated key.
    """
    try:
        return _read(path, fix_mode=create)
    except FileNotFoundError:
        if not create:
            return None
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.{secrets.token_hex(4)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_hex(32))
                f.flush()
                os.fsync(f.fileno())
            # link() fails if the key appeared meanwhile, and a reader never
            # sees a half-written key.
            os.link(tmp, path)
        except FileExistsError:
            pass  # another process won the race; use its key
        finally:
            os.unlink(tmp)
    except OSError as e:
        raise KeyFileError(f"cannot create {path}: {e.strerror or e}") from None
    return _read(path, fix_mode=True)


def loose_mode(path: str) -> Optional[int]:
    """The key's mode when group or others can access it, else None."""
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None
    return mode if mode & 0o077 else None


def _read(path: str, fix_mode: bool) -> bytes:
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if fix_mode and mode & 0o077:
            os.chmod(path, 0o600)
        with open(path, "r") as f:
            value = f.read().strip()
    except FileNotFoundError:
        raise
    except OSError as e:
        raise KeyFileError(f"cannot read {path}: {e.strerror or e}") from None
    if len(value) < 32:
        raise KeyFileError(f"{path} is empty or truncated; remove it to regenerate")
    return value.encode()
