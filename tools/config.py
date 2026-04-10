import json
import os
import socket
from typing import Any, Dict, Optional, Set

from tools.log import Color, log

SUPPORTED_SUFFIXES = (".json", ".yaml", ".yml", ".toml")


def deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-merge overlay into base.

    Rules:
      - scalar values: overlay wins
      - dict values: recurse
      - list values: overlay REPLACES (not appends)
      - missing keys: keep existing value
    """
    result = dict(base)
    for key, overlay_value in overlay.items():
        base_value = result.get(key)
        if isinstance(base_value, dict) and isinstance(overlay_value, dict):
            result[key] = deep_merge(base_value, overlay_value)
        else:
            result[key] = overlay_value
    return result


def _load_raw(path: str) -> Dict[str, Any]:
    """Load a single config file without processing includes."""
    suffix = os.path.splitext(path)[1].lower()

    if suffix == ".json":
        with open(path, "r") as f:
            content = f.read()
        if not content.strip():
            return {}
        return json.loads(content)

    if suffix in (".yaml", ".yml"):
        import yaml  # lazy import

        with open(path, "r") as f:
            data = yaml.safe_load(f)
        return data or {}

    if suffix == ".toml":
        import tomllib  # stdlib, Python 3.11+

        with open(path, "rb") as f:
            return tomllib.load(f)

    raise ValueError(f"Unsupported config file suffix {suffix!r}: {path}")


def load_config(path: str, _seen: Optional[Set[str]] = None) -> Dict[str, Any]:
    """Load a config file and resolve include directives.

    If the file contains an ``include:`` key with a list of relative
    paths, each included file is loaded (recursively) and deep-merged
    in order. The including file's own keys are then merged on top,
    so local values override included ones.

    Paths in ``include:`` are resolved relative to the directory
    containing the file that declares them.
    """
    abs_path = os.path.abspath(path)

    if _seen is None:
        _seen = set()
    if abs_path in _seen:
        raise ValueError(
            f"Circular include detected: {path} was already included "
            f"(chain: {' -> '.join(_seen)} -> {abs_path})"
        )
    _seen = _seen | {abs_path}  # copy to avoid cross-branch pollution

    data = _load_raw(path)
    if not isinstance(data, dict):
        return data

    includes = data.pop("include", None)
    if not includes:
        return data

    if not isinstance(includes, list):
        raise TypeError(
            f"include: must be a list of paths, got {type(includes).__name__} in {path}"
        )

    base_dir = os.path.dirname(abs_path)
    merged: Dict[str, Any] = {}
    for inc_path in includes:
        resolved = os.path.normpath(os.path.join(base_dir, inc_path))
        if not os.path.isfile(resolved):
            raise FileNotFoundError(
                f"include: {inc_path!r} resolved to {resolved} but file does not exist "
                f"(included from {path})"
            )
        included = load_config(resolved, _seen)
        merged = deep_merge(merged, included)

    return deep_merge(merged, data)


def _get_hostname() -> str:
    """Get the short hostname (strip .local suffix on macOS)."""
    hostname = socket.gethostname()
    # macOS commonly appends .local
    if hostname.endswith(".local"):
        hostname = hostname[: -len(".local")]
    return hostname


def load_config_dir(path: str) -> Dict[str, Any]:
    """Load config from a tools-config directory structure.

    Supports two layouts:

    1. **Host-based** (preferred): the directory contains a
       ``machines/`` subdirectory. The loader detects the current
       hostname, looks for ``machines/<hostname>.yaml`` (or
       .yml/.json/.toml), and loads it (with ``include:``
       resolution). Top-level files in the directory (e.g. a
       nix-generated ``99-nix-paths.json``) are deep-merged
       underneath the host config (host values win).

    2. **Flat**: no ``machines/`` subdirectory. All supported config
       files are loaded in lexicographic order and deep-merged (the
       original behaviour).

    Merge rules: scalars replace, dicts recurse, lists replace.
    Hidden files (names starting with '.') are skipped.
    """
    if not os.path.isdir(path):
        raise NotADirectoryError(f"Not a directory: {path}")

    machines_dir = os.path.join(path, "machines")

    if os.path.isdir(machines_dir):
        return _load_host_config(path, machines_dir)

    return _load_flat_dir(path)


def _load_host_config(config_dir: str, machines_dir: str) -> Dict[str, Any]:
    """Load config for the current host from machines/<hostname>.*."""
    hostname = _get_hostname()

    # Find the host config file
    host_file = None
    for suffix in SUPPORTED_SUFFIXES:
        candidate = os.path.join(machines_dir, hostname + suffix)
        if os.path.isfile(candidate):
            host_file = candidate
            break

    if not host_file:
        log(
            f"No machine config found for hostname {hostname!r} in {machines_dir}",
            Color.YELLOW,
        )

    # Load top-level files first (e.g. nix-generated paths JSON)
    merged: Dict[str, Any] = {}
    top_level = sorted(
        name
        for name in os.listdir(config_dir)
        if not name.startswith(".")
        and os.path.splitext(name)[1].lower() in SUPPORTED_SUFFIXES
        and os.path.isfile(os.path.join(config_dir, name))
    )
    for name in top_level:
        loaded = load_config(os.path.join(config_dir, name))
        if isinstance(loaded, dict):
            merged = deep_merge(merged, loaded)

    # Host config (with includes) merged on top — host values win
    if host_file:
        host_config = load_config(host_file)
        merged = deep_merge(merged, host_config)

    return merged


def _load_flat_dir(path: str) -> Dict[str, Any]:
    """Load and deep-merge every supported config file in a flat directory."""
    entries = sorted(
        name
        for name in os.listdir(path)
        if not name.startswith(".")
        and os.path.splitext(name)[1].lower() in SUPPORTED_SUFFIXES
        and os.path.isfile(os.path.join(path, name))
    )

    merged: Dict[str, Any] = {}
    for name in entries:
        loaded = load_config(os.path.join(path, name))
        if not isinstance(loaded, dict):
            raise ValueError(f"Config file {name} did not produce a mapping at top level")
        merged = deep_merge(merged, loaded)

    return merged
