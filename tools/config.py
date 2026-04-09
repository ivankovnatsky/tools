import json
import os
from typing import Any, Dict

SUPPORTED_SUFFIXES = (".json", ".yaml", ".yml", ".toml")


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
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
            result[key] = _deep_merge(base_value, overlay_value)
        else:
            result[key] = overlay_value
    return result


def load_config(path: str) -> Dict[str, Any]:
    """Load a single config file. Dispatch on suffix.

    .json        -> json.load
    .yaml / .yml -> yaml.safe_load   (lazy import)
    .toml        -> tomllib.load     (binary mode, stdlib, Python 3.11+)
    Unknown suffix -> raise ValueError.
    Empty file -> return {}.
    """
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


def load_config_dir(path: str) -> Dict[str, Any]:
    """Load and deep-merge every supported config file in a directory.

    Walks the directory non-recursively, picks up files whose suffix is
    .json, .yaml, .yml, or .toml, sorts them lexicographically by
    filename, loads each via load_config, and deep-merges them into a
    single dict.

    Merge rules: scalars replace, dicts recurse, lists replace.
    Hidden files (names starting with '.') are skipped.
    Empty directory -> return {}.
    """
    if not os.path.isdir(path):
        raise NotADirectoryError(f"Not a directory: {path}")

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
        merged = _deep_merge(merged, loaded)

    return merged
