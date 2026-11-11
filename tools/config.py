import json
import os
import socket
from typing import Any, Dict, List, Optional

SUPPORTED_SUFFIXES = (".json", ".yaml", ".yml", ".toml")

SECTIONS = (
    "bun",
    "npm",
    "uv",
    "go",
    "cargo",
    "pi",
    "mcp",
    "curlShell",
    "gitRepos",
    "sops",
    "templates",
    "files",
    "fetch",
    "env",
    "skills",
    "links",
    "brew",
    "ollamaModels",
    "flatpak",
    "settingsctl",
    "launchd",
    "defaults",
    "dock",
    "mgmt",
)

# Top-level keys that are not sections. `include` is consumed per file by
# load_config and never reaches the merged config.
SETTINGS_KEYS = ("stateFile", "paths", "vars", "include")
TOP_LEVEL_KEYS = frozenset(SECTIONS + SETTINGS_KEYS)
_RENAMED_KEYS = {"services": "mgmt"}


class ConfigError(Exception):
    """Raised when the config cannot be resolved into a usable desired state."""


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


def check_top_level_keys(data: Dict[str, Any], source: str) -> None:
    """Reject top-level keys no section or setting reads."""
    unknown = sorted((k for k in data if k not in TOP_LEVEL_KEYS), key=str)
    if not unknown:
        return
    hints = "".join(
        f"; use {_RENAMED_KEYS[key]!r} instead of {key!r} (renamed)"
        for key in unknown
        if key in _RENAMED_KEYS
    )
    raise ConfigError(
        f"{source}: unknown top-level key(s) {', '.join(repr(k) for k in unknown)}{hints}"
        f" (valid keys: {', '.join(sorted(TOP_LEVEL_KEYS))})"
    )


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


def load_config(
    path: str, _seen: Optional[Dict[str, None]] = None, loaded: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Load a config file and resolve include directives.

    If the file contains an ``include:`` key with a list of relative
    paths, each included file is loaded (recursively) and deep-merged
    in order. The including file's own keys are then merged on top,
    so local values override included ones.

    Paths in ``include:`` are resolved relative to the directory
    containing the file that declares them. Every file read is appended to
    ``loaded``.
    """
    abs_path = os.path.abspath(path)
    if loaded is not None:
        loaded.append(abs_path)

    # Insertion-ordered dict-as-set: the error message renders the actual
    # include chain, not an arbitrary set ordering.
    if _seen is None:
        _seen = {}
    if abs_path in _seen:
        raise ValueError(
            f"Circular include detected: {path} was already included "
            f"(chain: {' -> '.join(_seen)} -> {abs_path})"
        )
    _seen = {**_seen, abs_path: None}  # copy to avoid cross-branch pollution

    data = _load_raw(path)
    if not isinstance(data, dict):
        return data
    check_top_level_keys(data, path)

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
        included = load_config(resolved, _seen, loaded)
        merged = deep_merge(merged, included)

    return deep_merge(merged, data)


def _get_hostname() -> str:
    """Get the short hostname (strip .local suffix on macOS)."""
    hostname = socket.gethostname()
    # macOS commonly appends .local
    if hostname.endswith(".local"):
        hostname = hostname[: -len(".local")]
    return hostname


def load_config_dir(path: str, loaded: Optional[List[str]] = None) -> Dict[str, Any]:
    """Load config for the current host from a tools-config directory.

    The directory must contain ``machines/``. The loader detects the
    current hostname, loads ``machines/<hostname>.yaml`` (or
    .yml/.json/.toml) and the files it names in ``include:``. Nothing else
    in the directory is read.
    """
    if not os.path.isdir(path):
        raise NotADirectoryError(f"Not a directory: {path}")

    machines_dir = os.path.join(path, "machines")
    if not os.path.isdir(machines_dir):
        raise ConfigError(
            f"No machines/ directory in {path}; pass a config file or a "
            "directory with machines/<hostname>.yaml"
        )

    return _load_host_config(path, machines_dir, loaded)


def _load_host_config(
    config_dir: str, machines_dir: str, loaded: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Load config for the current host from machines/<hostname>.*."""
    hostname = _get_hostname()

    # Find the host config file
    host_file = None
    for suffix in SUPPORTED_SUFFIXES:
        candidate = os.path.join(machines_dir, hostname + suffix)
        if loaded is not None:
            # A higher-priority candidate appearing later changes which file loads.
            loaded.append(os.path.abspath(candidate))
        if os.path.isfile(candidate):
            host_file = candidate
            break

    if not host_file:
        # Continuing would hand the reconcilers an empty desired state, which
        # now reads as "remove everything we track" rather than "nothing to do".
        raise ConfigError(f"No machine config found for hostname {hostname!r} in {machines_dir}")

    return load_config(host_file, loaded=loaded)


# --------------------------------------------------------------------------
# Placeholders (`vars`, ${var}, ${env:NAME}, ${sopsPath:NAME})
# --------------------------------------------------------------------------


def _config_relative(path: Any, config_dir: Optional[str]) -> Any:
    if not isinstance(path, str) or not path or not config_dir:
        return path
    expanded = os.path.expanduser(path)
    return expanded if os.path.isabs(expanded) else os.path.join(config_dir, expanded)


def _prepare_sops(raw: Any, config_dir: Optional[str]) -> Any:
    if not isinstance(raw, dict):
        return raw
    out = dict(raw)
    if "defaultSopsFile" in out:
        out["defaultSopsFile"] = _config_relative(out["defaultSopsFile"], config_dir)
    secrets = out.get("secrets")
    if isinstance(secrets, dict):
        out["secrets"] = {
            name: (
                {**spec, "sopsFile": _config_relative(spec["sopsFile"], config_dir)}
                if isinstance(spec, dict) and "sopsFile" in spec
                else spec
            )
            for name, spec in secrets.items()
        }
    return out


def _prepare_templates(
    raw: Any, resolver, config_dir: Optional[str], sources: Optional[List[str]] = None
) -> Any:
    from tools.placeholders import PlaceholderError

    if not isinstance(raw, dict):
        return raw
    out = {}
    for name, spec in raw.items():
        where = f"templates.{name}"
        if not isinstance(spec, dict):
            out[name] = spec
            continue
        spec = dict(spec)
        if "file" in spec:
            if "content" in spec:
                raise ConfigError(f"{where}: set either file or content, not both")
            try:
                source = resolver.resolve(spec.pop("file"), f"{where}.file")
            except PlaceholderError as exc:
                raise ConfigError(str(exc)) from None
            if not isinstance(source, str) or not source:
                raise ConfigError(f"{where}.file must be a non-empty path string")
            source = _config_relative(source, config_dir)
            if sources is not None:
                sources.append(source)
            try:
                with open(source, "r", encoding="utf-8") as f:
                    spec["content"] = f.read()
            except OSError as exc:
                raise ConfigError(f"{where}.file: cannot read {source}: {exc}") from None
            except UnicodeDecodeError:
                raise ConfigError(f"{where}.file: {source} is not UTF-8 text") from None
            spec.setdefault("format", "raw")
        try:
            out[name] = {
                key: resolver.resolve(value, f"{where}.{key}", keep_secrets=key == "content")
                for key, value in spec.items()
            }
        except PlaceholderError as exc:
            raise ConfigError(str(exc)) from None
    return out


def _sops_path_lookup(sops_raw: Any):
    from tools.placeholders import PlaceholderError
    from tools.user.sops import parse_sops

    parsed = parse_sops(sops_raw)
    paths = {s.name: s.path for s in parsed.secrets}

    def lookup(name: str) -> str:
        if name in paths:
            return paths[name]
        if parsed.errors:
            raise PlaceholderError(
                f"${{sopsPath:{name}}}: sops config is invalid: {'; '.join(parsed.errors)}"
            )
        raise KeyError(name)

    return lookup


def resolve_config(
    config: Dict[str, Any],
    config_dir: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    sources: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Resolve placeholders across a fully merged config.

    Runs once after every file and ``--config`` path is merged, so a
    per-host ``vars`` value overrides a shared one. ``${sops:...}`` is
    only allowed inside ``templates`` content, where it stays verbatim
    for the render-time pass. Relative ``sopsFile`` and template
    ``source`` paths resolve against ``config_dir``. A ``${NAME}`` that
    is not a var stays literal; the environment is read only through
    ``${env:NAME}``. Template ``file`` paths read are appended to ``sources``.
    """
    from tools.placeholders import PlaceholderError, Resolver

    env = dict(os.environ) if env is None else env
    raw_vars = config.get("vars")
    raw_vars = {} if raw_vars is None else raw_vars
    if not isinstance(raw_vars, dict):
        raise ConfigError(f"vars must be a mapping, got {type(raw_vars).__name__}")

    out: Dict[str, Any] = {}
    try:
        # sops first, without ${sopsPath:}: the paths it defines come from here.
        if "sops" in config:
            sops_resolver = Resolver(raw_vars, env=env)
            out["sops"] = _prepare_sops(sops_resolver.resolve(config["sops"], "sops"), config_dir)
        resolver = Resolver(raw_vars, env=env, sops_path=_sops_path_lookup(out.get("sops")))
        for key, value in config.items():
            if key in ("vars", "sops"):
                continue
            if key == "templates":
                out[key] = _prepare_templates(value, resolver, config_dir, sources)
            else:
                out[key] = resolver.resolve(value, key)
        if raw_vars:
            out["vars"] = resolver.vars()
    except PlaceholderError as exc:
        raise ConfigError(str(exc)) from None
    return out
