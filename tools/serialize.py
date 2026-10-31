"""Serialize plain Python data into the on-disk formats services expect."""

import datetime
import json
import math
import re
from typing import Any, List, Mapping

FORMATS = ("yaml", "json", "toml", "ini", "env", "raw")

_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TOML_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


class SerializeError(ValueError):
    pass


def serialize(fmt: str, data: Any, *, quote: bool = True) -> str:
    """``quote`` applies to ``env`` only (see ``to_env``)."""
    if fmt == "raw":
        if not isinstance(data, str):
            raise SerializeError(f"raw content must be a string, got {type(data).__name__}")
        return data
    if fmt == "json":
        try:
            return json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        except (TypeError, ValueError) as e:
            raise SerializeError(f"json: {e}") from None
    if fmt == "yaml":
        import yaml

        try:
            return yaml.safe_dump(
                data, sort_keys=False, default_flow_style=False, allow_unicode=True
            )
        except yaml.YAMLError as e:
            raise SerializeError(f"yaml: {e}") from None
    if fmt == "toml":
        return to_toml(data)
    if fmt == "ini":
        return to_ini(data)
    if fmt == "env":
        return to_env(data, quote=quote)
    raise SerializeError(f"unknown format {fmt!r} (expected one of {', '.join(FORMATS)})")


def _require_mapping(fmt: str, data: Any) -> Mapping:
    if not isinstance(data, Mapping):
        raise SerializeError(f"{fmt} content must be a mapping, got {type(data).__name__}")
    return data


def _scalar_text(fmt: str, key: str, value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        text = str(value)
        if "\n" in text or "\r" in text:
            raise SerializeError(f"{fmt}: value of {key!r} contains a newline")
        return text
    raise SerializeError(f"{fmt}: value of {key!r} must be a scalar, got {type(value).__name__}")


def to_env(data: Any, *, quote: bool = True) -> str:
    """KEY='value' lines; ``'`` is written ``'\\''`` so ``. file`` in sh reads
    every value back byte for byte. ``quote=False`` writes KEY=value as-is
    for consumers that do not strip quotes."""
    lines = []
    for key, value in _require_mapping("env", data).items():
        if not isinstance(key, str) or not _ENV_KEY.fullmatch(key):
            raise SerializeError(f"env: invalid variable name {key!r}")
        text = _scalar_text("env", key, value)
        if quote:
            text = "'" + text.replace("'", "'\\''") + "'"
        lines.append(f"{key}={text}")
    return "".join(f"{line}\n" for line in lines)


def to_ini(data: Any) -> str:
    """Top-level scalars first (sectionless, as app.ini has), then [sections]."""
    data = _require_mapping("ini", data)
    head: List[str] = []
    body: List[str] = []
    for key, value in data.items():
        if isinstance(value, Mapping):
            if "]" in str(key) or "\n" in str(key):
                raise SerializeError(f"ini: invalid section name {key!r}")
            body.append(f"\n[{key}]" if head or body else f"[{key}]")
            for sub_key, sub_value in value.items():
                if isinstance(sub_value, Mapping):
                    raise SerializeError(
                        f"ini: [{key}] {sub_key!r} is nested too deep; "
                        'name the section "a.b" instead'
                    )
                body.append(_ini_line(sub_key, sub_value))
        else:
            head.append(_ini_line(key, value))
    return "".join(f"{line}\n" for line in head + body)


def _ini_line(key: Any, value: Any) -> str:
    key = str(key)
    if not key or "=" in key or "\n" in key:
        raise SerializeError(f"ini: invalid key {key!r}")
    return f"{key} = {_scalar_text('ini', key, value)}"


# --------------------------------------------------------------------------
# TOML (stdlib only reads it; tomllib round-trips the output in tests)
# --------------------------------------------------------------------------


def _toml_key(key: Any) -> str:
    if not isinstance(key, str):
        raise SerializeError(f"toml: keys must be strings, got {key!r}")
    return key if _TOML_BARE_KEY.fullmatch(key) else json.dumps(key, ensure_ascii=False)


def _toml_value(value: Any, where: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, str):
        # JSON string escapes are a subset of TOML basic-string escapes.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v, f"{where}[]") for v in value) + "]"
    if isinstance(value, Mapping):
        items = ", ".join(f"{_toml_key(k)} = {_toml_value(v, where)}" for k, v in value.items())
        return "{" + items + "}" if items else "{}"
    if value is None:
        raise SerializeError(f"toml: {where} is null; TOML has no null")
    raise SerializeError(f"toml: unsupported value at {where}: {type(value).__name__}")


def _is_table_array(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and bool(value)
        and all(isinstance(v, Mapping) for v in value)
    )


def to_toml(data: Any) -> str:
    lines: List[str] = []
    _toml_table(_require_mapping("toml", data), [], lines)
    return "".join(f"{line}\n" for line in lines).lstrip("\n")


def _toml_table(table: Mapping, path: List[str], lines: List[str]) -> None:
    where = ".".join(path) or "<root>"
    subtables = []
    for key, value in table.items():
        if isinstance(value, Mapping) or _is_table_array(value):
            subtables.append((key, value))
        else:
            lines.append(f"{_toml_key(key)} = {_toml_value(value, f'{where}.{key}')}")
    for key, value in subtables:
        sub_path = path + [_toml_key(key)]
        header = ".".join(sub_path)
        if isinstance(value, Mapping):
            lines.append("")
            lines.append(f"[{header}]")
            _toml_table(value, sub_path, lines)
        else:
            for item in value:
                lines.append("")
                lines.append(f"[[{header}]]")
                _toml_table(item, sub_path, lines)
