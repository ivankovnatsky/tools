"""Per-OS hardcoded paths for system utilities.

The reconcilers used to receive every binary path through the config
dict, injected by the nix-config module from `${pkgs.foo}/bin`. That
coupled the runtime to nix-store hashes and forced every nixpkgs bump
to flow through the activation config.

This module replaces that injection for tier-1 system utilities —
tools that ship with the OS and live at stable, hand-writable paths.
Tier-2 tool managers (`bun`, `uv`, `nodejs`, `python`) still come
through the config dict for now.

The long-term direction is "tools drops nix" — Linux is treated as a
generic Unix distribution (Debian, Arch, Fedora, ...) using
`/usr/bin/<tool>`. NixOS is recognised as a Linux variant with its
own stable system-profile paths (`/run/current-system/sw/bin/<tool>`)
so existing NixOS hosts (e.g. `a3`) keep working while the migration
proceeds slowly. NixOS detection uses the `/etc/NIXOS` marker file.
"""

import os
import sys

# macOS: standard locations from the base system / Xcode CLT.
_DARWIN = {
    "git": "/usr/bin/git",
    "curl": "/usr/bin/curl",
    "bash": "/bin/bash",
    "perl": "/usr/bin/perl",
}

# Generic Linux (Debian, Arch, Ubuntu, Fedora, ...).
_LINUX = {
    "git": "/usr/bin/git",
    "curl": "/usr/bin/curl",
    "bash": "/bin/bash",
    "perl": "/usr/bin/perl",
}

# NixOS variant of Linux: stable system-profile paths, no per-build
# hash baked in. Resolved when /etc/NIXOS is present.
_NIXOS = {
    "git": "/run/current-system/sw/bin/git",
    "curl": "/run/current-system/sw/bin/curl",
    "bash": "/run/current-system/sw/bin/bash",
    "perl": "/run/current-system/sw/bin/perl",
}


def _table():
    if sys.platform == "darwin":
        return _DARWIN
    if os.path.exists("/etc/NIXOS"):
        return _NIXOS
    return _LINUX


def system_bin(name: str) -> str:
    """Return the absolute path to a system binary.

    Raises KeyError if the name is unknown, FileNotFoundError if the
    hardcoded path does not exist on disk.
    """
    table = _table()
    if name not in table:
        raise KeyError(
            f"system_paths: no entry for {name!r} on {sys.platform} (known: {sorted(table)})"
        )
    path = table[name]
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"system_paths: {name!r} expected at {path} but the file does not exist"
        )
    return path


def system_bin_optional(name: str) -> str | None:
    """Like system_bin but returns None instead of raising."""
    try:
        return system_bin(name)
    except (KeyError, FileNotFoundError):
        return None


def system_dir(name: str) -> str:
    """Return the directory containing a system binary (for PATH building)."""
    return os.path.dirname(system_bin(name))


def system_dir_optional(name: str) -> str | None:
    """Like system_dir but returns None instead of raising."""
    path = system_bin_optional(name)
    return os.path.dirname(path) if path else None
