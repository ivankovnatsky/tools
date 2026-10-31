"""launchd agents (user domain) and daemons (system domain) on macOS.

Config shape::

    launchd:
      labelPrefix: com.example        # default label is <labelPrefix>.<name>
      logDir: ~/Library/Logs/tools    # optional; per-domain defaults otherwise
      sudo: false                     # allow `sudo` for system-domain changes
      services:
        caddy:
          domain: system              # user (default) | system
          waitForPaths: [/Volumes/Storage]
          command: caddy run --config /var/lib/caddy/Caddyfile
          restartOn: [/var/lib/caddy/Caddyfile]

Layers, so a non-YAML frontend can build `Service` objects directly:

- `parse_launchd(raw)` validates plain data into `Settings` + `Service`s.
- `plan(settings, services, state, env)` reads current state (plist on
  disk, `launchctl print`) and decides create/update/load/restart/remove.
- `apply(...)` executes a plan. `Env` injects every path and the command
  runner, so nothing here reads global config or touches the real system
  unless the default `Env` is used.

Ownership follows `files:`/`links:`: state records each label tools wrote,
keyed by label. A plist tools did not write is never replaced (one that
already matches the desired plist exactly is adopted), and a removed label
is booted out and deleted only while its plist still matches what tools
wrote. State only says which labels tools owns: plist paths are always
derived from the validated label and domain.

tools never runs as root (it refuses euid 0). User agents are plain file ops
plus `launchctl ... gui/<uid>`. Daemons need `launchd.sudo: true` and then go
through `_priv`: `sudo -n install`, `sudo -n launchctl ... system/<label>`,
`sudo -n rm -f`, and nix-style `mkdir`/`touch`/`chown` for daemon logs with a user.
"""

import copy
import hashlib
import os
import plistlib
import pwd
import re
import shlex
import stat
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from xml.parsers.expat import ExpatError

from tools.log import Color, debug, log
from tools.user.files import _parse_mode
from tools.util import format_diff_bytes, looks_like_secret, run_command

BASH = "/bin/bash"
LAUNCHCTL = "/bin/launchctl"
SUDO = "/usr/bin/sudo"

DOMAINS = ("user", "system")
PROCESS_TYPES = ("Background", "Standard", "Adaptive", "Interactive")

_KEEP_ALIVE_KEYS = {
    "successfulExit": "SuccessfulExit",
    "crashed": "Crashed",
    "pathState": "PathState",
    "networkState": "NetworkState",
    "otherJobEnabled": "OtherJobEnabled",
    "afterInitialDemand": "AfterInitialDemand",
}
_CALENDAR_KEYS = {
    "minute": "Minute",
    "hour": "Hour",
    "day": "Day",
    "weekday": "Weekday",
    "month": "Month",
}
_SECTION_KEYS = {"labelPrefix", "logDir", "sudo", "bash", "services"}
_SERVICE_KEYS = {
    "label",
    "domain",
    "command",
    "programArguments",
    "environment",
    "workingDirectory",
    "dataDir",
    "extraDirs",
    "preStart",
    "waitForPaths",
    "waitForPath",
    "waitForSecrets",
    "keepAlive",
    "runAtLoad",
    "startInterval",
    "startCalendarInterval",
    "throttleInterval",
    "logDir",
    "stdoutPath",
    "stderrPath",
    "user",
    "group",
    "processType",
    "nice",
    "extraServiceConfig",
    "restartOn",
    "restartOnChange",
    "disabled",
    "secrets",
}
# Keys tools derives itself; overriding them would desync path, label and state.
_RESERVED_EXTRA = {"Label", "ProgramArguments", "Program"}

# launchctl exit codes (errno values) seen from bootout/bootstrap.
_NOT_LOADED = {3, 113}  # ESRCH, "Could not find specified service"
_IN_PROGRESS = 36  # EINPROGRESS: bootout still tearing the job down
_BOOTSTRAP_RETRY = {5, 17, 37}  # EIO, EEXIST, EALREADY: previous instance lingering
_SERVICE_DISABLED = 119


# --------------------------------------------------------------------------
# Resolved data: what any frontend hands the provider
# --------------------------------------------------------------------------


@dataclass
class ExtraDir:
    path: str
    mode: Optional[int] = None
    owner: Optional[str] = None
    group: Optional[str] = None


@dataclass
class Service:
    name: str
    label: str
    domain: str = "user"
    command: Optional[str] = None
    program_arguments: Optional[List[str]] = None
    environment: Dict[str, str] = field(default_factory=dict)
    working_directory: Optional[str] = None
    data_dir: Optional[str] = None
    extra_dirs: List[ExtraDir] = field(default_factory=list)
    pre_start: str = ""
    wait_for_paths: List[str] = field(default_factory=list)
    wait_for_secrets: bool = False  # nix-config `waitForSecrets`: block until /run/secrets
    keep_alive: Any = True  # bool or plist dict
    run_at_load: bool = True
    start_interval: Optional[int] = None
    start_calendar_interval: Any = None  # plist dict or list of dicts
    throttle_interval: int = 10
    log_dir: Optional[str] = None
    stdout_path: Optional[str] = None
    stderr_path: Optional[str] = None
    user: Optional[str] = None
    group: Optional[str] = None
    process_type: Optional[str] = None
    nice: Optional[int] = None
    extra_service_config: Dict[str, Any] = field(default_factory=dict)
    restart_on: List[str] = field(default_factory=list)
    restart_on_change: bool = True
    disabled: bool = False
    secrets: bool = False


@dataclass
class Settings:
    sudo: bool = False
    log_dir: Optional[str] = None
    bash: Optional[str] = None  # overrides Env.bash
    errors: List[str] = field(default_factory=list)  # section-level: hold every change


def _login_user() -> Tuple[int, str]:
    """(uid, home) of the user tools runs as; launchd agents live in `gui/<uid>`."""
    uid = os.getuid()
    try:
        return uid, pwd.getpwuid(uid).pw_dir
    except KeyError:
        return uid, os.path.expanduser("~")


DEFAULT_LOG_DIR = "/tmp/log/launchd"
DEFAULT_AGENT_LOG_DIR = "/tmp/agents/log/launchd"
SECRETS_DIR = "/run/secrets"
AGENT_SECRETS_DIR = ".config/sops-nix/secrets"  # under the user's home, as home-manager sops-nix


@dataclass
class Env:
    """Everything the provider touches outside its arguments.

    tools always runs as the login user. System-domain changes go through a
    fixed set of `sudo -n` commands (see `_priv`), never by running tools as root.
    """

    agents_dir: Optional[str] = None  # default <home>/Library/LaunchAgents
    daemons_dir: str = "/Library/LaunchDaemons"
    # Same defaults as nix-config's home (agents) and darwin (daemons) local.launchd `logDir`.
    user_log_dir: Optional[str] = DEFAULT_AGENT_LOG_DIR
    system_log_dir: str = DEFAULT_LOG_DIR
    bash: str = BASH  # root-owned and stable; `launchd.bash` overrides
    stat: Callable[[str], Any] = os.stat
    lstat: Callable[[str], Any] = os.lstat
    # Daemon log paths must sit under one of these (or system_log_dir).
    system_log_roots: Tuple[str, ...] = (
        "/tmp/log",
        "/private/tmp/log",
        "/Library/Logs",
        "/var/log",
        "/private/var/log",
    )
    # Missing daemon workingDirectories are created as root only under one of these.
    system_dir_roots: Tuple[str, ...] = (
        "/var/lib",
        "/private/var/lib",
        "/usr/local/var",
        "/opt/homebrew/var",
        "/Library/Application Support",
    )
    volumes_root: str = "/Volumes"
    run: Callable[[List[str]], Tuple[int, str, str]] = run_command
    sleep: Callable[[float], None] = time.sleep
    euid: int = field(default_factory=os.geteuid)
    uid: Optional[int] = None
    home: Optional[str] = None
    login: Optional[str] = None  # login user name; default from uid
    ismount: Callable[[str], bool] = os.path.ismount
    allow_sudo: bool = False  # set from `launchd.sudo`; the only gate for escalation
    is_darwin: bool = field(default_factory=lambda: sys.platform == "darwin")
    bootout_polls: int = 50
    poll_interval: float = 0.2
    bootstrap_attempts: int = 3

    def __post_init__(self):
        if self.uid is None:
            self.uid, home = _login_user()
            if self.home is None:
                self.home = home
        if self.home is None:
            try:
                self.home = pwd.getpwuid(self.uid).pw_dir
            except KeyError:
                self.home = os.path.expanduser("~")
        if self.login is None:
            try:
                self.login = pwd.getpwuid(self.uid).pw_name
            except KeyError:
                self.login = None
        if self.agents_dir is None:
            self.agents_dir = os.path.join(self.home, "Library", "LaunchAgents")
        if self.user_log_dir is None:
            self.user_log_dir = DEFAULT_AGENT_LOG_DIR


ROOT_ERROR = (
    "tools is running as root; run it as your normal user. "
    "System-domain jobs use `sudo -n` internally when `launchd.sudo: true`"
)


def root_refusal(env: Env) -> Optional[str]:
    return ROOT_ERROR if env.euid == 0 or env.uid == 0 else None


# --------------------------------------------------------------------------
# Parse: plain data -> Settings + Services
# --------------------------------------------------------------------------


def expand_path(path: str, home: Optional[str] = None) -> str:
    """Expand $VARS and `~`; `~` is `home` when given (see `Env.home`)."""
    p = os.path.expandvars(str(path))
    if home and (p == "~" or p.startswith("~/")):
        p = home + p[1:]
    else:
        p = os.path.expanduser(p)
    return os.path.normpath(p)


_VAR = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def _env_dependent(path) -> bool:
    """Expanding path would read the environment ($VARS, `~`, `~/...`)."""
    return isinstance(path, str) and ("$" in path or path == "~" or path.startswith("~/"))


def shell_path(path: str) -> str:
    """path as one shell word: literal parts quoted, `$NAME`/`${NAME}` expanded by the shell."""
    out, pos = [], 0
    for m in _VAR.finditer(path):
        if m.start() > pos:
            out.append(shlex.quote(path[pos : m.start()]))
        out.append('"${%s}"' % (m.group(1) or m.group(2)))
        pos = m.end()
    if pos < len(path) or not out:
        out.append(shlex.quote(path[pos:]))
    return "".join(out)


def _runtime_path(value, what: str, home: Optional[str] = None) -> str:
    """A wrapper-only path, expanded by the job's shell like nix-config's starter.

    `~` is `home` when given (agents), else the job's `$HOME`.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} must be a non-empty string")
    if value == "~" or value.startswith("~/"):
        value = (home or "$HOME") + value[1:]
    elif value.startswith("~"):
        value = os.path.expanduser(value)
    if "$" not in value:
        path = os.path.normpath(value)
        if not os.path.isabs(path):
            raise ValueError(f"{what}: {path!r} must be absolute (or start with ~ or $VAR)")
        return path
    if "$" in _VAR.sub("", value):
        raise ValueError(f"{what}: {value!r}: only $NAME and ${{NAME}} are expanded")
    if not (value.startswith("/") or _VAR.match(value)):
        raise ValueError(f"{what}: {value!r} must be absolute (or start with ~ or $VAR)")
    return value


def _runtime_paths(value, what: str, home: Optional[str] = None) -> List[str]:
    return [_runtime_path(p, what, home) for p in _str_list(value, what)]


def _str_list(value, what: str) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) and v for v in value):
        return list(value)
    raise ValueError(f"{what} must be a string or a list of strings")


def _abs_paths(value, what: str, home: Optional[str] = None) -> List[str]:
    paths = [expand_path(p, home) for p in _str_list(value, what)]
    for p in paths:
        if not os.path.isabs(p):
            raise ValueError(f"{what}: {p!r} must be absolute (or start with ~)")
    return paths


def _opt_abs(value, what: str, home: Optional[str] = None) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} must be a non-empty string")
    return _abs_paths(value, what, home)[0]


def _opt_log(value, what: str, home: Optional[str] = None) -> Optional[str]:
    if isinstance(value, str) and ".." in value.split("/"):
        raise ValueError(f"{what}: {value!r} must not contain `..`")
    return _opt_abs(value, what, home)


def _int(value, what: str, minimum: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise ValueError(f"{what} must be >= {minimum}, got {value}")
    return value


def _bool(value, what: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{what} must be true or false, got {value!r}")
    return value


def _keep_alive(
    value, home: Optional[str] = None, check: Optional[Callable[[Any, str], None]] = None
):
    if value is None or isinstance(value, bool):
        return value
    if not isinstance(value, dict) or not value:
        raise ValueError("keepAlive must be a bool or a non-empty mapping")
    out = {}
    for key, v in value.items():
        if key not in _KEEP_ALIVE_KEYS:
            raise ValueError(f"keepAlive: unknown key {key!r} (known: {sorted(_KEEP_ALIVE_KEYS)})")
        plist_key = _KEEP_ALIVE_KEYS[key]
        if key in ("successfulExit", "crashed", "networkState", "afterInitialDemand"):
            out[plist_key] = _bool(v, f"keepAlive.{key}")
        else:
            if not isinstance(v, dict) or not all(isinstance(b, bool) for b in v.values()):
                raise ValueError(f"keepAlive.{key} must map names/paths to true/false")
            if key == "pathState" and check:
                for p in v:
                    check(p, "keepAlive.pathState")
            out[plist_key] = (
                {expand_path(p, home): b for p, b in v.items()} if key == "pathState" else dict(v)
            )
    return out


def _calendar(value):
    if value is None:
        return None
    entries = value if isinstance(value, list) else [value]
    if not entries:
        raise ValueError("startCalendarInterval must not be empty")
    out = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry:
            raise ValueError("startCalendarInterval entries must be non-empty mappings")
        item = {}
        for key, v in entry.items():
            if key not in _CALENDAR_KEYS:
                raise ValueError(
                    f"startCalendarInterval: unknown key {key!r} (known: {sorted(_CALENDAR_KEYS)})"
                )
            item[_CALENDAR_KEYS[key]] = _int(v, f"startCalendarInterval.{key}", 0)
        out.append(item)
    return out if isinstance(value, list) else out[0]


def _extra_dirs(value, home: Optional[str] = None) -> List[ExtraDir]:
    """Paths are wrapper-only: see `_runtime_path`."""
    if not isinstance(value, list):
        raise ValueError("extraDirs must be a list")
    out = []
    for entry in value:
        if isinstance(entry, str):
            out.append(ExtraDir(_runtime_path(entry, "extraDirs", home)))
            continue
        if not isinstance(entry, dict) or "path" not in entry:
            raise ValueError("extraDirs entries must be a path or {path, mode?, owner?, group?}")
        unknown = set(entry) - {"path", "mode", "owner", "group"}
        if unknown:
            raise ValueError(f"extraDirs: unknown keys {sorted(unknown)}")
        for key in ("owner", "group"):
            if entry.get(key) is not None and not isinstance(entry[key], str):
                raise ValueError(f"extraDirs.{key} must be a string")
        try:
            mode = _parse_mode(entry.get("mode"))
        except (TypeError, ValueError) as e:
            raise ValueError(f"extraDirs.mode: {e}") from e
        if mode is not None and not 0 <= mode <= 0o7777:
            raise ValueError(f"extraDirs.mode {entry['mode']!r} must be between 0000 and 7777")
        out.append(
            ExtraDir(
                _runtime_path(entry["path"], "extraDirs.path", home),
                mode,
                entry.get("owner"),
                entry.get("group"),
            )
        )
    return out


_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SAFE_ACCOUNT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")  # macOS service users start with _


def valid_label(label) -> bool:
    """Labels name files and sudo arguments: no `/`, no `..`, no leading `-`."""
    return isinstance(label, str) and bool(_SAFE_NAME.fullmatch(label)) and ".." not in label


def runs_as_root(svc: Service) -> bool:
    return svc.domain == "system" and log_account(svc)[0] in (None, "root")


_ROOT_DIRS_ERROR = (
    "extraDirs mode/owner/group need a root daemon (domain: system, no user): "
    "the wrapper's chmod/chown would fail under `set -e` and the job would crash-loop"
)


def _parse_service(
    name: str,
    raw: Dict,
    label_prefix: Optional[str],
    home: Optional[str] = None,
    login: Optional[str] = None,
    section_log_dir=None,
) -> Service:
    if not isinstance(raw, dict):
        raise ValueError("must be a mapping")
    unknown = set(raw) - _SERVICE_KEYS
    if unknown:
        raise ValueError(f"unknown keys {sorted(unknown)}")

    if not _SAFE_NAME.fullmatch(name) or ".." in name:
        raise ValueError(f"name {name!r} must match [A-Za-z0-9._-] (no `..`, no `/`)")
    label = raw.get("label")
    if label is None:
        if not label_prefix:
            raise ValueError("set `label`, or `launchd.labelPrefix` for a default")
        label = f"{label_prefix}.{name}"
    if not valid_label(label):
        raise ValueError(f"label {label!r} must be reverse-DNS style ([A-Za-z0-9._-])")

    domain = raw.get("domain", "user")
    if domain not in DOMAINS:
        raise ValueError(f"domain must be one of {DOMAINS}, got {domain!r}")

    svc = Service(name=name, label=label, domain=domain)
    h = home
    # Wrapper-only paths: `~` is the job's $HOME for daemons, expanded at runtime.
    rh = home if domain == "user" else None
    extra_raw = raw.get("extraServiceConfig")
    account = raw.get("user") or (
        extra_raw.get("UserName") if isinstance(extra_raw, dict) else None
    )
    other_user = domain == "system" and (account or "root") != login

    def fixed(value, what: str) -> None:
        """Paths launchd reads from the plist or tools reads at deploy: no runtime expansion."""
        if other_user and _env_dependent(value):
            raise ValueError(
                f"{what}: {value!r} would be expanded at deploy from your environment, not "
                f"{account or 'root'}'s (launchd never expands it); write the path out"
            )

    for key in ("workingDirectory", "logDir", "stdoutPath", "stderrPath"):
        fixed(raw.get(key), key)
    if raw.get("logDir") is None:
        fixed(section_log_dir, "launchd.logDir")
    if isinstance(raw.get("restartOn"), list):
        for path in raw["restartOn"]:
            fixed(path, "restartOn")
    else:
        fixed(raw.get("restartOn"), "restartOn")
    if isinstance(extra_raw, dict):
        for key in ("WorkingDirectory", "StandardOutPath", "StandardErrorPath"):
            fixed(extra_raw.get(key), f"extraServiceConfig.{key}")

    command, args = raw.get("command"), raw.get("programArguments")
    if (command is None) == (args is None):
        raise ValueError("set exactly one of `command` or `programArguments`")
    if command is not None:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        svc.command = command
    else:
        if not isinstance(args, list) or not args or not all(isinstance(a, str) for a in args):
            raise ValueError("programArguments must be a non-empty list of strings")
        svc.program_arguments = list(args)

    env = raw.get("environment") or {}
    if not isinstance(env, dict):
        raise ValueError("environment must be a mapping")
    for k, v in env.items():
        if isinstance(v, bool) or not isinstance(v, (str, int, float)):
            raise ValueError(f"environment.{k} must be a string or number")
    svc.environment = {str(k): str(v) for k, v in env.items()}

    svc.working_directory = _opt_abs(raw.get("workingDirectory"), "workingDirectory", h)
    if raw.get("dataDir") is not None:
        svc.data_dir = _runtime_path(raw["dataDir"], "dataDir", rh)
    if raw.get("extraDirs") is not None:
        svc.extra_dirs = _extra_dirs(raw["extraDirs"], rh)
    pre_start = raw.get("preStart") or ""
    if not isinstance(pre_start, str):
        raise ValueError("preStart must be a string")
    svc.pre_start = pre_start
    if raw.get("waitForPath") is not None:
        if not isinstance(raw["waitForPath"], str):
            raise ValueError("waitForPath must be a string (use waitForPaths for a list)")
        svc.wait_for_paths = _runtime_paths(raw["waitForPath"], "waitForPath", rh)
    if raw.get("waitForPaths") is not None:
        svc.wait_for_paths += _runtime_paths(raw["waitForPaths"], "waitForPaths", rh)
    if raw.get("restartOn") is not None:
        svc.restart_on = _abs_paths(raw["restartOn"], "restartOn", h)

    if raw.get("keepAlive") is not None:
        svc.keep_alive = _keep_alive(raw["keepAlive"], h, fixed)
    for key, attr in (
        ("waitForSecrets", "wait_for_secrets"),
        ("runAtLoad", "run_at_load"),
        ("restartOnChange", "restart_on_change"),
        ("disabled", "disabled"),
        ("secrets", "secrets"),
    ):
        if key in raw:
            setattr(svc, attr, _bool(raw[key], key))
    if raw.get("startInterval") is not None:
        svc.start_interval = _int(raw["startInterval"], "startInterval", 1)
    svc.start_calendar_interval = _calendar(raw.get("startCalendarInterval"))
    if raw.get("throttleInterval") is not None:
        svc.throttle_interval = _int(raw["throttleInterval"], "throttleInterval", 0)

    svc.log_dir = _opt_log(raw.get("logDir"), "logDir", h)
    svc.stdout_path = _opt_log(raw.get("stdoutPath"), "stdoutPath", h)
    svc.stderr_path = _opt_log(raw.get("stderrPath"), "stderrPath", h)

    for key in ("user", "group"):
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value:
            raise ValueError(f"{key} must be a non-empty string")
        if domain != "system":
            raise ValueError(f"{key} only applies to domain: system (agents run as you)")
        setattr(svc, key, value)

    if raw.get("processType") is not None:
        if raw["processType"] not in PROCESS_TYPES:
            raise ValueError(f"processType must be one of {PROCESS_TYPES}")
        svc.process_type = raw["processType"]
    if raw.get("nice") is not None:
        svc.nice = _int(raw["nice"], "nice")
        if not -20 <= svc.nice <= 20:
            raise ValueError("nice must be between -20 and 20")

    extra = raw.get("extraServiceConfig") or {}
    if not isinstance(extra, dict):
        raise ValueError("extraServiceConfig must be a mapping")
    reserved = _RESERVED_EXTRA & set(extra)
    if reserved:
        raise ValueError(f"extraServiceConfig may not set {sorted(reserved)}")
    extra = dict(extra)
    for plist_key, attr, key in (
        ("StandardOutPath", "stdout_path", "stdoutPath"),
        ("StandardErrorPath", "stderr_path", "stderrPath"),
    ):
        if plist_key in extra:
            if getattr(svc, attr) is not None:
                raise ValueError(f"set {key} or extraServiceConfig.{plist_key}, not both")
            setattr(svc, attr, _opt_log(extra.pop(plist_key), f"extraServiceConfig.{plist_key}", h))
    if "WorkingDirectory" in extra:
        if svc.working_directory is not None:
            raise ValueError(
                "set workingDirectory or extraServiceConfig.WorkingDirectory, not both"
            )
        svc.working_directory = _opt_abs(
            extra.pop("WorkingDirectory"), "extraServiceConfig.WorkingDirectory", h
        )
    for plist_key, key in (("UserName", "user"), ("GroupName", "group")):
        if plist_key in extra and raw.get(key) is not None:
            raise ValueError(f"set {key} or extraServiceConfig.{plist_key}, not both")
    try:
        plistlib.dumps(extra)
    except (TypeError, OverflowError) as e:
        raise ValueError(f"extraServiceConfig is not plist-serializable: {e}") from e
    svc.extra_service_config = dict(extra)
    if any(d.mode is not None or d.owner or d.group for d in svc.extra_dirs) and not runs_as_root(
        svc
    ):
        raise ValueError(_ROOT_DIRS_ERROR)
    return svc


def parse_launchd(
    raw, home: Optional[str] = None, login: Optional[str] = None
) -> Tuple[Settings, List[Service], List[str]]:
    """Validate a plain `launchd:` mapping. Returns (settings, services, errors).

    `home` is the login user's home for `~` (see `Env.home`); `login` is the login
    user name (default: from the uid), which daemons running as anyone else are checked against.
    """
    if raw is None:
        raw = {}
    if login is None:
        try:
            login = pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            login = None
    errors: List[str] = []
    if not isinstance(raw, dict):
        msg = f"launchd must be a mapping, got {type(raw).__name__}"
        return Settings(errors=[msg]), [], [msg]
    settings = Settings(errors=errors)
    unknown = set(raw) - _SECTION_KEYS
    if unknown:
        errors.append(f"unknown keys {sorted(unknown)}")
    for key, parse in (
        ("sudo", lambda v: _bool(v, "sudo")),
        ("logDir", lambda v: _opt_log(v, "logDir", home)),
        ("bash", lambda v: _opt_abs(v, "bash")),
    ):
        if raw.get(key) is None:
            continue
        try:
            setattr(settings, {"logDir": "log_dir"}.get(key, key), parse(raw[key]))
        except ValueError as e:
            errors.append(str(e))
    prefix = raw.get("labelPrefix")
    if prefix is not None and (not isinstance(prefix, str) or not prefix):
        errors.append("labelPrefix must be a non-empty string")
        prefix = None

    services_raw = raw.get("services")
    if services_raw is None:
        services_raw = {}
    if not isinstance(services_raw, dict):
        errors.append("services must be a mapping of name -> service")
        return settings, [], list(errors)
    settings.errors = list(errors)
    errors = list(errors)

    services: List[Service] = []
    seen: Dict[str, str] = {}
    for name in sorted(services_raw):
        try:
            svc = _parse_service(
                str(name), services_raw[name], prefix, home, login, raw.get("logDir")
            )
        except ValueError as e:
            errors.append(f"{name}: {e}")
            continue
        if svc.label in seen:
            errors.append(f"{name}: label {svc.label} already used by {seen[svc.label]}")
            continue
        seen[svc.label] = name
        services.append(svc)
    return settings, services, errors


# --------------------------------------------------------------------------
# Desired plist
# --------------------------------------------------------------------------


def volume_of(path: str, volumes_root: str) -> Optional[str]:
    """`<volumes_root>/<name>` when path lives on that external volume, else None."""
    rel = os.path.relpath(path, volumes_root)
    if rel == "." or rel.startswith(".."):
        return None
    return os.path.join(volumes_root, rel.split(os.sep, 1)[0])


def _script(svc: Service, log_dir: Optional[str] = None, secrets_dir: str = SECRETS_DIR) -> str:
    """The inline starter body, structured like nix-config's local.launchd starter."""
    lines = ["set -e", 'export PATH="/bin:/usr/bin:$PATH"']
    if log_dir:
        q = shlex.quote(log_dir)
        lines.append(f"/bin/mkdir -p {q}")
        if svc.domain == "user":
            # A dir the agent doesn't own (e.g. root's) would fail chmod and kill the job.
            lines.append(f"if [ -O {q} ]; then /bin/chmod 755 {q}; fi")
        else:
            lines.append(f"/bin/chmod 755 {q} || true")
    if svc.wait_for_secrets:
        q = shlex.quote(secrets_dir)
        lines += [
            'echo "Waiting for sops secrets..."',
            f"while [ ! -e {q} ]; do sleep 1; done",
            'echo "Sops secrets available."',
        ]
    for path in svc.wait_for_paths:
        q = shell_path(path)
        if _VAR.search(path):
            waiting, available = f"'Waiting for '{q}...", f"{q}' available.'"
        else:
            waiting = shlex.quote(f"Waiting for {path}...")
            available = shlex.quote(f"{path} available.")
        lines += [
            f"echo {waiting}",
            f"while [ ! -e {q} ]; do sleep 1; done",
            f"echo {available}",
        ]
    dirs = ([ExtraDir(svc.data_dir)] if svc.data_dir else []) + svc.extra_dirs
    for d in dirs:
        q = shell_path(d.path)
        lines.append(f"/bin/mkdir -p {q}")
        if d.mode is not None:
            lines.append(f"/bin/chmod -h {d.mode:o} {q}")
        if d.owner or d.group:
            owner = (d.owner or "") + (f":{d.group}" if d.group else "")
            lines.append(f"/usr/sbin/chown -h {shlex.quote(owner)} {q}")
    if svc.pre_start.strip():
        lines.append(svc.pre_start.rstrip("\n"))
    if svc.program_arguments is not None:
        lines.append('exec "$@"')
    else:
        lines.append(f"exec {svc.command.strip(chr(10))}")
    return "\n".join(lines) + "\n"


def _inside(base: str, leaf: str) -> str:
    path = os.path.normpath(os.path.join(base, leaf))
    if os.path.dirname(path) != os.path.normpath(base):
        raise ValueError(f"{leaf!r} would escape {base}")
    return path


def log_specs(svc: Service, settings: Settings, env: Env) -> List[Tuple[str, bool]]:
    """[(path, generated)] for stdout and stderr; generated = `<logDir>/<name>...` default."""
    log_dir = log_dir_of(svc, settings, env)
    out = []
    for explicit, suffix in ((svc.stdout_path, ".log"), (svc.stderr_path, ".error.log")):
        out.append((explicit, False) if explicit else (_inside(log_dir, svc.name + suffix), True))
    return out


def log_dir_of(svc: Service, settings: Settings, env: Env) -> str:
    default_dir = env.system_log_dir if svc.domain == "system" else env.user_log_dir
    return os.path.normpath(svc.log_dir or settings.log_dir or default_dir)


def wrapper_log_dir(svc: Service, settings: Settings, env: Env) -> Optional[str]:
    """The logDir the job wrapper always creates, as nix-config does."""
    return log_dir_of(svc, settings, env)


def log_paths(svc: Service, settings: Settings, env: Env) -> Tuple[str, str]:
    (out, _), (err, _) = log_specs(svc, settings, env)
    return out, err


def plist_path(svc_or_label, domain: str, env: Env) -> str:
    """The only source of plist paths: derived from a validated label, never from state."""
    label = svc_or_label.label if isinstance(svc_or_label, Service) else svc_or_label
    if not valid_label(label) or domain not in DOMAINS:
        raise ValueError(f"invalid label {label!r} or domain {domain!r}")
    base = env.daemons_dir if domain == "system" else env.agents_dir
    return _inside(base, f"{label}.plist")


def _log_path_error(path: str, roots: Tuple[str, ...], is_dir: bool = False) -> Optional[str]:
    """String checks only: daemon logs are opened by launchd as root, never by tools."""
    if not os.path.isabs(path) or os.path.normpath(path) != path:
        return "must be an absolute, normalized path"
    if ".." in path.split(os.sep):
        return "must not contain `..`"
    for root in roots:
        root = root.rstrip(os.sep)
        if path.startswith(root + os.sep) or (is_dir and path == root):
            return None
    return f"must be inside {', '.join(roots)} for domain: system"


def service_errors(svc: Service, settings: Settings, env: Env) -> List[str]:
    """Checks that also cover Services a non-YAML frontend built directly."""
    errors = []
    extra = svc.extra_service_config
    for attr, plist_key in (("user", "UserName"), ("group", "GroupName"),
                            ("stdout_path", "StandardOutPath"),
                            ("stderr_path", "StandardErrorPath"),
                            ("working_directory", "WorkingDirectory")):  # fmt: skip
        if getattr(svc, attr) is not None and plist_key in extra:
            errors.append(f"{svc.name}: {attr} and extraServiceConfig.{plist_key} both set")
    wd = svc.working_directory or extra.get("WorkingDirectory")
    if wd is not None and (
        not isinstance(wd, str) or not os.path.isabs(wd) or os.path.normpath(wd) != wd
    ):
        errors.append(f"{svc.name}: workingDirectory {wd!r} must be an absolute, normalized path")
    for d in svc.extra_dirs:
        if d.mode is not None and not 0 <= d.mode <= 0o7777:
            errors.append(f"{svc.name}: extraDirs mode {d.mode:o} must be between 0000 and 7777")
    if any(d.mode is not None or d.owner or d.group for d in svc.extra_dirs) and not runs_as_root(
        svc
    ):
        errors.append(f"{svc.name}: {_ROOT_DIRS_ERROR}")
    if svc.domain == "system" and settings.bash and not _root_only(settings.bash, env):
        errors.append(
            f"{svc.name}: bash {settings.bash} must be root-owned and not group/world-writable "
            "(with every parent dir) for domain: system"
        )
    return errors + system_log_errors(svc, settings, env)


def _root_only(path: str, env: Env) -> bool:
    """Only root can change what path runs: every component of the original path is
    root-owned (non-symlinks also not group/world-writable), and so is its canonical target."""
    if not os.path.isabs(path):
        return False
    current = "/"
    for part in [""] + [c for c in os.path.normpath(path).split(os.sep) if c]:
        current = os.path.join(current, part) if part else current
        try:
            st = env.lstat(current)
        except OSError:
            return False
        if st.st_uid != 0:
            return False
        if not stat.S_ISLNK(st.st_mode) and st.st_mode & 0o022:
            return False
    current = os.path.realpath(path)
    while True:
        try:
            st = env.stat(current)
        except OSError:
            return False
        if st.st_uid != 0 or st.st_mode & 0o022:
            return False
        parent = os.path.dirname(current)
        if parent == current:
            return True
        current = parent


def system_log_errors(svc: Service, settings: Settings, env: Env) -> List[str]:
    """Validate a daemon's log paths, also for Services a frontend built directly."""
    if svc.domain != "system":
        return []
    roots = tuple(dict.fromkeys((env.system_log_dir, *env.system_log_roots)))
    log_dir = svc.log_dir or settings.log_dir
    declared = [
        ("logDir", log_dir),
        ("stdoutPath", svc.stdout_path),
        ("stderrPath", svc.stderr_path),
        ("extraServiceConfig.StandardOutPath", svc.extra_service_config.get("StandardOutPath")),
        ("extraServiceConfig.StandardErrorPath", svc.extra_service_config.get("StandardErrorPath")),
    ]
    errors = []
    for key, path in declared:
        if path is None:
            continue
        if not isinstance(path, str):
            errors.append(f"{svc.name}: {key} must be a string")
            continue
        error = _log_path_error(path, roots, is_dir=key == "logDir")
        if error:
            errors.append(f"{svc.name}: {key} {path} {error}")
    if errors:
        return errors
    for path in log_paths(svc, settings, env):
        error = _log_path_error(path, roots)
        if error:
            errors.append(f"{svc.name}: log path {path} {error}")
    for key, value in zip(("user", "group"), log_account(svc)):
        if value is not None and not (isinstance(value, str) and _SAFE_ACCOUNT.fullmatch(value)):
            errors.append(f"{svc.name}: {key} {value!r} must match [A-Za-z0-9_][A-Za-z0-9._-]*")
    return errors


def _open_regular(path: str, flags: int, mode: int = 0o644) -> int:
    """os.open with O_NOFOLLOW; the fd must be a regular file."""
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(f"{path} is not a regular file")
    return fd


def desired_plist(svc: Service, settings: Settings, env: Env) -> Dict[str, Any]:
    secrets_dir = (
        SECRETS_DIR if svc.domain == "system" else os.path.join(env.home, AGENT_SECRETS_DIR)
    )
    script = _script(svc, wrapper_log_dir(svc, settings, env), secrets_dir)
    # Inline via `bash -c`: launchd children get EPERM opening scripts on external volumes.
    bash = settings.bash or env.bash
    if svc.domain == "system" and settings.bash:
        # Validated in service_errors; the plist gets the canonical, root-only target.
        bash = os.path.realpath(settings.bash)
    argv = [bash, "-c", script]
    if svc.program_arguments is not None:
        argv += [svc.label, *svc.program_arguments]

    keep_alive = svc.keep_alive
    stdout, stderr = log_paths(svc, settings, env)

    plist: Dict[str, Any] = {
        "Label": svc.label,
        "ProgramArguments": argv,
        "RunAtLoad": svc.run_at_load,
        "KeepAlive": keep_alive,
        "ThrottleInterval": svc.throttle_interval,
        "StandardOutPath": stdout,
        "StandardErrorPath": stderr,
    }
    optional = {
        "EnvironmentVariables": svc.environment or None,
        "WorkingDirectory": svc.working_directory,
        "StartInterval": svc.start_interval,
        "StartCalendarInterval": svc.start_calendar_interval,
        "UserName": svc.user,
        "GroupName": svc.group,
        "ProcessType": svc.process_type,
        "Nice": svc.nice,
    }
    plist.update({k: v for k, v in optional.items() if v is not None})
    plist.update(svc.extra_service_config)
    return plist


def dump_plist(plist: Dict[str, Any]) -> bytes:
    return plistlib.dumps(plist, sort_keys=True)


def plist_hash(plist: Dict[str, Any]) -> str:
    return hashlib.sha256(dump_plist(plist)).hexdigest()


def read_plist(path: str) -> Tuple[bool, Optional[Dict[str, Any]], bool]:
    """(exists, parsed, readable). parsed is None when malformed or unreadable."""
    if not os.path.lexists(path):
        return False, None, True
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return True, None, False
    return True, _loads(data), True


def _loads(data: bytes) -> Optional[Dict[str, Any]]:
    """Parsed plist, or None when malformed (truncated XML, bad binary, not a dict)."""
    try:
        parsed = plistlib.loads(data)
    except (plistlib.InvalidFileException, ValueError, ExpatError, OverflowError):
        return None
    return parsed if isinstance(parsed, dict) else None


def read_current(path: str, domain: str, env: Env) -> Tuple[bool, Optional[Dict[str, Any]], bool]:
    """read_plist, falling back to a gated `sudo -n plutil` for a root-only daemon plist."""
    exists, parsed, readable = read_plist(path)
    if readable or not exists or not _needs_sudo(domain) or not env.allow_sudo:
        return exists, parsed, readable
    rc, out, _err = _run_priv(["/usr/bin/plutil", "-convert", "xml1", "-o", "-", path], domain, env)
    if rc != 0:
        return True, None, False
    return True, _loads(out.encode()), True


# --------------------------------------------------------------------------
# launchctl
# --------------------------------------------------------------------------


def domain_target(domain: str, env: Env) -> str:
    return "system" if domain == "system" else f"gui/{env.uid}"


def service_target(label: str, domain: str, env: Env) -> str:
    return f"{domain_target(domain, env)}/{label}"


def _needs_sudo(domain: str) -> bool:
    return domain == "system"


def can_change(domain: str, settings: Settings, env: Env) -> bool:
    return not _needs_sudo(domain) or env.allow_sudo


def _priv(cmd: List[str], domain: str, env: Env) -> Optional[List[str]]:
    """The one escalation gate: sudo only when allowed, and never interactively."""
    if not _needs_sudo(domain):
        return cmd
    if not env.allow_sudo:
        return None
    return [SUDO, "-n", *cmd]


def _run_priv(cmd: List[str], domain: str, env: Env) -> Tuple[int, str, str]:
    gated = _priv(cmd, domain, env)
    if gated is None:
        return 1, "", "needs root: set `launchd.sudo: true` (tools runs `sudo -n`)"
    return env.run(gated)


def launchctl(args: List[str], domain: str, env: Env, privileged: bool = True):
    cmd = [LAUNCHCTL, *args]
    return _run_priv(cmd, domain, env) if privileged else env.run(cmd)


def is_disabled(label: str, domain: str, env: Env) -> Optional[bool]:
    """Read-only: is the label in launchd's disabled-services database? None if unknown."""
    rc, out, _err = launchctl(["print-disabled", domain_target(domain, env)], domain, env, False)
    if rc != 0:
        return None
    return re.search(rf'"{re.escape(label)}"\s*=>\s*(disabled|true)', out) is not None


@dataclass
class Status:
    loaded: bool
    state: Optional[str] = None
    pid: Optional[int] = None
    last_exit: Optional[str] = None
    path: Optional[str] = None  # plist launchd loaded the job from


def _field(text: str, name: str) -> Optional[str]:
    # Top-level fields carry one tab of indent; nested blocks reuse names like `state`.
    m = re.search(rf"^\t{re.escape(name)} = (.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else None


def status(label: str, domain: str, env: Env) -> Status:
    """Read-only: `launchctl print`, never escalated."""
    rc, out, _err = launchctl(["print", service_target(label, domain, env)], domain, env, False)
    if rc != 0:
        return Status(loaded=False)
    pid = _field(out, "pid")
    return Status(
        loaded=True,
        state=_field(out, "state"),
        pid=int(pid) if pid and pid.isdigit() else None,
        last_exit=_field(out, "last exit code"),
        path=_field(out, "path"),
    )


def _wait_gone(label: str, domain: str, env: Env) -> bool:
    for _ in range(env.bootout_polls):
        if not status(label, domain, env).loaded:
            return True
        env.sleep(env.poll_interval)
    return False


def bootout(label: str, domain: str, env: Env) -> bool:
    rc, _out, err = launchctl(["bootout", service_target(label, domain, env)], domain, env)
    if rc in _NOT_LOADED:
        return True
    if rc not in (0, _IN_PROGRESS) and status(label, domain, env).loaded:
        log(f"launchd: bootout {label} failed ({rc}): {err.strip()}", Color.RED)
        return False
    if not _wait_gone(label, domain, env):
        log(f"launchd: {label} still loaded after bootout", Color.RED)
        return False
    return True


def bootstrap(label: str, domain: str, path: str, env: Env) -> bool:
    enabled_once = False
    attempt = 0
    while True:
        rc, _out, err = launchctl(["bootstrap", domain_target(domain, env), path], domain, env)
        if rc == 0:
            return True
        # Recent macOS may report a disabled job as EIO rather than 119. When the
        # database can't be read, enable anyway: callers only bootstrap wanted jobs.
        disabled = rc == _SERVICE_DISABLED or (
            rc == 5 and not enabled_once and is_disabled(label, domain, env) is not False
        )
        if disabled and not enabled_once:
            # Only called for jobs config wants enabled. Enabling is a fix-up, not an attempt.
            log(f"launchd: {label} is disabled in launchd; enabling", Color.YELLOW)
            if not _set_enabled(label, domain, True, env):
                return False
            enabled_once = True
            continue
        attempt += 1
        if rc in _BOOTSTRAP_RETRY and attempt < env.bootstrap_attempts:
            debug(f"launchd: bootstrap {label} returned {rc}; retrying", Color.YELLOW)
            bootout(label, domain, env)
            env.sleep(float(attempt))
            continue
        log(f"launchd: bootstrap {label} failed ({rc}): {err.strip()}", Color.RED)
        return False


def kickstart(label: str, domain: str, env: Env) -> bool:
    rc, _out, err = launchctl(["kickstart", "-k", service_target(label, domain, env)], domain, env)
    if rc != 0:
        log(f"launchd: kickstart {label} failed ({rc}): {err.strip()}", Color.RED)
        return False
    return True


def _set_enabled(label: str, domain: str, enabled: bool, env: Env) -> bool:
    verb = "enable" if enabled else "disable"
    rc, _out, err = launchctl([verb, service_target(label, domain, env)], domain, env)
    if rc != 0:
        log(f"launchd: {verb} {label} failed ({rc}): {err.strip()}", Color.RED)
        return False
    return True


# --------------------------------------------------------------------------
# Files (plist + logs), escalated for the system domain
# --------------------------------------------------------------------------


INSTALL = "/usr/bin/install"


def _write_plist(path: str, data: bytes, domain: str, env: Env, mode: int = 0o644) -> bool:
    """Write atomically: as the user for agents, `sudo -n install -S` for daemons."""
    parent = os.path.dirname(path)
    try:
        if not _needs_sudo(domain):
            os.makedirs(parent, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=parent, prefix=".tools-", suffix=".plist")
            try:
                os.fchmod(fd, mode)
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
            except BaseException:
                if os.path.lexists(tmp):
                    os.unlink(tmp)
                raise
            return True

        fd, tmp = tempfile.mkstemp(prefix="tools-launchd-", suffix=".plist")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            # launchd rejects daemon plists not owned by root:wheel.
            cmd = [INSTALL, "-S", "-m", f"{mode:04o}", "-o", "root", "-g", "wheel", tmp, path]
            rc, _out, err = _run_priv(cmd, domain, env)
            if rc != 0:
                log(f"launchd: install {path} failed: {err.strip()}", Color.RED)
                return False
            return True
        finally:
            os.unlink(tmp)
    except OSError as e:
        log(f"launchd: failed to write {path}: {e}", Color.RED)
        return False


def _remove_plist(path: str, domain: str, env: Env) -> bool:
    if not os.path.lexists(path):
        return True
    if not _needs_sudo(domain):
        try:
            os.remove(path)
            return True
        except OSError as e:
            log(f"launchd: failed to remove {path}: {e}", Color.RED)
            return False
    rc, _out, err = _run_priv(["/bin/rm", "-f", path], domain, env)
    if rc != 0:
        log(f"launchd: failed to remove {path}: {err.strip()}", Color.RED)
        return False
    return True


def _ensure_logs(item: "Item", settings: Settings, env: Env) -> None:
    """launchd opens StandardOutPath/StandardErrorPath before exec; pre-create agent logs.

    Daemons get nix's `mkdir` (plus `touch`/`chown` with a user) via sudo. Also creates
    workingDirectory, which launchd enters before the wrapper runs.
    Sets `item.owned_logs`: agent log files tools created, tracked, or at the
    generated default path.
    """
    svc = item.svc
    tracked = set((item.tracked or {}).get("logs") or [])
    item.owned_logs = sorted(tracked)
    _ensure_workdir(svc, settings, env)
    if svc.domain != "user":
        if can_change(svc.domain, settings, env):
            item.owned_logs = sorted(tracked | set(_daemon_logs(svc, settings, env)))
        return
    try:
        specs = log_specs(svc, settings, env)
    except ValueError as e:
        log(f"launchd: {svc.label}: {e}", Color.YELLOW)
        return
    wanted = []
    for p, generated in specs:
        volume = volume_of(p, env.volumes_root)
        if volume and not env.ismount(volume):
            # mkdir here would plant a plain dir that shadows the real mount point.
            log(f"launchd: {svc.label}: {volume} is not mounted; not creating {p}", Color.YELLOW)
            continue
        wanted.append((p, generated or p in tracked))
    item.owned_logs = sorted(tracked | set(_user_logs(svc, wanted)))


def log_account(svc: Service) -> Tuple[Optional[str], Optional[str]]:
    """(user, group) a daemon runs as: `user`/`group`, else extraServiceConfig's."""
    extra = svc.extra_service_config
    return svc.user or extra.get("UserName"), svc.group or extra.get("GroupName")


def _root_mkdir_ok(path: str, env: Env) -> bool:
    """path may be created by root: inside an existing `Env.system_dir_roots` entry, with no
    symlink or non-directory between that root and the first missing component."""
    if _log_path_error(path, env.system_dir_roots):
        return False
    for root in env.system_dir_roots:
        root = os.path.normpath(root)
        if not path.startswith(root.rstrip(os.sep) + os.sep) or not os.path.isdir(root):
            continue
        current = root
        for part in os.path.relpath(path, root).split(os.sep):
            current = os.path.join(current, part)
            if os.path.islink(current):
                return False
            if not os.path.lexists(current):
                return True
            if not os.path.isdir(current):
                return False
        return True
    return False


def _dir_exists(path: str, domain: str, env: Env) -> bool:
    """isdir, asking root (`sudo -n test -d`) for a daemon path the user can't reach."""
    if os.path.isdir(path):
        return True
    if not _needs_sudo(domain) or not env.allow_sudo:
        return False
    return _run_priv(["/bin/test", "-d", path], domain, env)[0] == 0


def workdir_block(svc: Service, env: Env) -> Optional[str]:
    """Why a daemon that will be started has a workingDirectory root can't create, or None."""
    wd = svc.working_directory
    if svc.domain != "system" or svc.disabled or wd is None or _dir_exists(wd, svc.domain, env):
        return None
    volume = volume_of(wd, env.volumes_root)
    if (volume and not env.ismount(volume)) or _root_mkdir_ok(wd, env):
        return None
    return (
        f"workingDirectory {wd} is missing and tools only creates it as root inside an "
        f"existing {', '.join(env.system_dir_roots)}; create it yourself"
    )


def _ensure_workdir(svc: Service, settings: Settings, env: Env) -> None:
    """Create a missing workingDirectory: as the user for agents, `sudo -n mkdir -p` for daemons
    (only where `_root_mkdir_ok`)."""
    wd = svc.working_directory
    if wd is None or os.path.isdir(wd) or not can_change(svc.domain, settings, env):
        return
    volume = volume_of(wd, env.volumes_root)
    if volume and not env.ismount(volume):
        log(f"launchd: {svc.label}: {volume} is not mounted; not creating {wd}", Color.YELLOW)
        return
    if svc.domain == "user":
        try:
            os.makedirs(wd, exist_ok=True)
        except OSError as e:
            log(f"launchd: {svc.label}: cannot create {wd}: {e}", Color.YELLOW)
        return
    if _dir_exists(wd, svc.domain, env):
        return
    if not _root_mkdir_ok(wd, env):
        if not svc.disabled:
            log(f"launchd: {svc.label}: {workdir_block(svc, env) or wd}", Color.YELLOW)
        return
    _run_log_cmd(["/bin/mkdir", "-p", wd], svc, env)


def _daemon_logs(svc: Service, settings: Settings, env: Env) -> List[str]:
    """nix-config parity: `sudo -n mkdir -p <logDir>` for every daemon (launchd opens the
    logs before the wrapper runs), then `touch` and `chown user:group` when it has a user.

    The paths are the string-checked `<logDir>/<name>.log`/`.error.log`; names are
    validated in `plan`.
    """
    log_dir = log_dir_of(svc, settings, env)
    if os.path.islink(log_dir):
        log(f"launchd: {svc.label}: {log_dir} is a symlink; not touching its logs", Color.YELLOW)
        return []
    if not os.path.isdir(log_dir) and not _run_log_cmd(["/bin/mkdir", "-p", log_dir], svc, env):
        return []
    user, group = log_account(svc)
    files = [p for p, generated in log_specs(svc, settings, env) if generated]
    links = [p for p in files if os.path.islink(p)]
    if links:
        log(f"launchd: {svc.label}: {', '.join(links)} is a symlink; skipped", Color.YELLOW)
        files = [p for p in files if p not in links]
    if not user or not files:
        return []
    if not _run_log_cmd(["/usr/bin/touch", "-h", *files], svc, env):
        return []
    _run_log_cmd(["/usr/sbin/chown", "-h", f"{user}:{group or 'staff'}", *files], svc, env)
    return files


def _run_log_cmd(cmd: List[str], svc: Service, env: Env) -> bool:
    rc, _out, err = _run_priv(cmd, svc.domain, env)
    if rc != 0:
        log(f"launchd: {cmd[0]} for {svc.label} logs failed: {err.strip()}", Color.YELLOW)
        return False
    return True


def _user_logs(svc: Service, wanted: List[Tuple[str, bool]]) -> List[str]:
    owned = []
    for p, ours in wanted:
        try:
            if not os.path.lexists(p):
                os.makedirs(os.path.dirname(p), exist_ok=True)
                os.close(_open_regular(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
                owned.append(p)
            elif ours and stat.S_ISREG(os.lstat(p).st_mode):
                owned.append(p)
        except OSError as e:
            log(f"launchd: cannot create log file for {svc.label}: {e}", Color.YELLOW)
    return owned


# --------------------------------------------------------------------------
# Plan: shared by deploy and diff
# --------------------------------------------------------------------------


@dataclass
class Item:
    svc: Service
    path: str
    plist: Dict[str, Any]
    data: bytes
    hash: str
    restart_hashes: Dict[str, Optional[str]]
    old_data: Optional[bytes] = None  # existing plist, canonicalised, for diffs
    tracked: Optional[Dict] = None
    loaded: Optional[bool] = None
    changed_files: List[str] = field(default_factory=list)
    retire: Optional[Dict] = None  # same label tracked in another domain, removed first
    fix_perms: bool = False  # chmod/chown the plist in place (adopted, or mode drifted)
    owned_logs: Optional[List[str]] = None  # log files tools may re-own; see _ensure_logs
    mode_wrong: bool = False
    live_disabled: Optional[bool] = None  # launchd's override, read in plan


@dataclass
class Plan:
    errors: List[str] = field(default_factory=list)
    create: List[Item] = field(default_factory=list)
    update: List[Item] = field(default_factory=list)
    adopt: List[Item] = field(default_factory=list)
    load: List[Item] = field(default_factory=list)  # unchanged, should be loaded, is not
    unload: List[Item] = field(default_factory=list)  # unchanged, disabled, still loaded
    enable: List[Item] = field(default_factory=list)  # disabled -> enabled, plist unchanged
    disable: List[Item] = field(default_factory=list)  # enabled -> disabled, plist unchanged
    restart: List[Item] = field(default_factory=list)  # restartOn file changed
    retry: List[Item] = field(default_factory=list)  # last activation failed, plist current
    unknown: List[Tuple[str, str]] = field(default_factory=list)  # held: deploy fails
    ignored: List[Tuple[str, str]] = field(default_factory=list)  # malformed state, warn only
    ok: List[Item] = field(default_factory=list)  # nothing to do
    blocked: List[Tuple[str, str]] = field(default_factory=list)  # (label, reason)
    remove: List[Tuple[str, Dict]] = field(default_factory=list)
    forget: List[Tuple[str, Dict, str]] = field(default_factory=list)


def _plist_mode(svc: Service) -> int:
    return 0o600 if svc.secrets else 0o644


def _file_mode(path: str) -> Optional[int]:
    try:
        return os.lstat(path).st_mode & 0o777
    except OSError:
        return None


def _state_entries(state: Dict) -> Tuple[Dict[str, Dict], List[str]]:
    """(valid entries by label, labels of malformed entries). Malformed ones are never acted on."""
    if state_error(state):
        return {}, []
    raw = (state.get("launchd") or {}).get("services") or {}
    valid, malformed = {}, []
    for label, entry in raw.items():
        if isinstance(entry, dict) and _tracked_domain(label, entry)[1] is None:
            valid[label] = entry
        else:
            malformed.append(label)
    return valid, malformed


def state_error(state: Dict) -> Optional[str]:
    """Why `state["launchd"]` can't be used at all, or None. Such state is never touched."""
    section = state.get("launchd")
    if section is None:
        return None
    if not isinstance(section, dict):
        return "state `launchd` is not a mapping; fix or delete it in the state file"
    services = section.get("services")
    if services is not None and not isinstance(services, dict):
        return "state `launchd.services` is not a mapping; fix or delete it in the state file"
    return None


def _tracked(state: Dict) -> Dict[str, Dict]:
    return _state_entries(state)[0]


SHASUM = "/usr/bin/shasum"


def _hash(path: str, domain: str = "user", env: Optional[Env] = None) -> Optional[str]:
    """sha256 of path; None when missing. Root-only daemon files are read via `sudo -n shasum`."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return None
    except OSError as e:
        unreadable = e
    usable = os.path.isabs(path) and os.path.normpath(path) == path
    if env is not None and usable and _needs_sudo(domain) and env.allow_sudo:
        rc, out, _err = _run_priv([SHASUM, "-a", "256", path], domain, env)
        digest = out.split()[0] if rc == 0 and out.split() else ""
        if re.fullmatch(r"[0-9a-f]{64}", digest):
            return digest
    log(f"launchd: restartOn {path} unreadable ({unreadable}); changes not tracked", Color.YELLOW)
    return None


def _file_hashes(
    paths: List[str], domain: str = "user", env: Optional[Env] = None
) -> Dict[str, Optional[str]]:
    return {p: _hash(p, domain, env) for p in paths}


def _shared_logs(services: List[Service], settings: Settings, env: Env) -> Dict[str, List[str]]:
    """label -> error, for services declaring a log path another service also declares."""
    seen: Dict[str, List[str]] = {}
    for svc in services:
        try:
            for path in log_paths(svc, settings, env):
                seen.setdefault(os.path.normpath(path), []).append(svc.label)
        except ValueError:
            continue
    clash: Dict[str, List[str]] = {}
    for path, labels in seen.items():
        if len(set(labels)) > 1:
            for label in labels:
                clash.setdefault(label, []).append(path)
    return clash


def plan(settings: Settings, services: List[Service], state: Dict, env: Env, errors=()) -> Plan:
    p = Plan(errors=list(errors))
    if settings.errors:
        # A broken section (e.g. a `sudo` typo) could change every job: hold them all.
        for err in settings.errors:
            if err not in p.errors:
                p.errors.append(err)
        return p
    tracked, malformed = _state_entries(state)
    desired_labels = {s.label for s in services}
    for label in malformed:
        reason = "malformed state entry, ignored"
        if label in desired_labels:
            p.unknown.append((repr(label), f"{reason}; job held: {_FIX_STATE}"))
        else:
            p.ignored.append((repr(label), reason))
    clash = _shared_logs(services, settings, env)

    for svc in services:
        if svc.label in malformed:
            continue
        if svc.label in clash:
            p.errors.append(f"{svc.name}: log path {', '.join(clash[svc.label])} is shared")
            continue
        try:
            path = plist_path(svc, svc.domain, env)
            pl = desired_plist(svc, settings, env)
            data = dump_plist(pl)
        except (TypeError, OverflowError, ValueError) as e:
            p.errors.append(f"{svc.name}: cannot build plist: {e}")
            continue
        log_errors = service_errors(svc, settings, env)
        if log_errors:
            p.errors += log_errors
            continue
        blocked = workdir_block(svc, env)
        if blocked:
            p.blocked.append((svc.label, blocked))
            continue
        item = Item(
            svc=svc,
            path=path,
            plist=pl,
            data=data,
            hash=hashlib.sha256(data).hexdigest(),
            restart_hashes=_file_hashes(svc.restart_on, svc.domain, env),
            tracked=tracked.get(svc.label),
        )
        if item.tracked is not None and item.tracked.get("domain", "user") != svc.domain:
            # Same label, new domain: the old job must be gone before this one exists.
            old = Plan()
            _plan_removal(old, svc.label, item.tracked, settings, env)
            if old.errors or old.unknown:
                p.errors += old.errors
                p.unknown += old.unknown
                continue
            if old.forget and old.forget[0][2] not in ("already gone", "never written"):
                old_domain = item.tracked.get("domain", "user")
                p.blocked.append((svc.label, f"old {old_domain} job: {old.forget[0][2]}"))
                continue
            if old.remove:
                item.retire = item.tracked
            item.tracked = None
        exists, current, readable = read_current(path, svc.domain, env)
        if not readable:
            p.unknown.append((svc.label, f"{path} unreadable; {_FIX_UNREADABLE}"))
            continue
        live = status(svc.label, svc.domain, env)
        if _foreign(live, path):
            p.errors.append(
                f"{svc.name}: {svc.label} is already loaded from {live.path}, not {path}; "
                "boot that job out or pick another label"
            )
            continue
        if item.tracked is not None and item.tracked.get("hash") is None:
            # Only logs were ever recorded: behave as untracked (create or adopt), keep the logs.
            item.tracked = {"logs": item.tracked.get("logs") or []}
        if current is not None:
            item.old_data = dump_plist(current)
        ours = item.tracked is not None and item.tracked.get("hash") is not None
        old_hashes = (item.tracked or {}).get("restartOn") or {}
        item.changed_files = [
            f
            for f, h in item.restart_hashes.items()
            # A file that appears (None -> hash) counts; a newly listed one does not.
            if h is not None and f in old_hashes and old_hashes[f] != h
        ]

        if not exists:
            bucket = p.create
        elif current == pl:
            bucket = None if ours else p.adopt
            item.mode_wrong = _file_mode(path) != _plist_mode(svc)
            # An adopted plist was written by someone else: enforce mode and owner now.
            item.fix_perms = item.mode_wrong or not ours
        elif ours:
            bucket = p.update
        else:
            reason = "malformed plist" if current is None else "exists and is not managed by tools"
            p.blocked.append((svc.label, f"{path}: {reason}"))
            continue

        if bucket is not None and bucket is not p.adopt:
            if not can_change(svc.domain, settings, env):
                p.errors.append(_sudo_error(svc.label))
                continue
            bucket.append(item)
            continue

        # Plist already right: reconcile enabled/loaded state and restartOn.
        adopting = bucket is p.adopt
        was_disabled = bool((item.tracked or {}).get("disabled"))
        live_disabled = is_disabled(svc.label, svc.domain, env)
        if live_disabled is not None:
            was_disabled = live_disabled
        item.live_disabled = live_disabled
        item.loaded = live.loaded

        target = None
        if (item.tracked or {}).get("pending"):
            target = p.retry
        elif svc.disabled and not was_disabled:
            target = p.disable
        elif not svc.disabled and was_disabled:
            target = p.enable
        elif svc.disabled and item.loaded:
            target = p.unload
        elif not svc.disabled and not item.loaded:
            target = p.load
        elif not svc.disabled and item.changed_files:
            target = p.restart
        if (target is not None or item.fix_perms) and not can_change(svc.domain, settings, env):
            p.errors.append(_sudo_error(svc.label))
        elif target is None:
            (p.adopt if adopting else p.ok).append(item)
        else:
            target.append(item)

    for label in sorted(set(tracked) - desired_labels):
        _plan_removal(p, label, tracked[label], settings, env)
    return p


_ENTRY_TYPES = {
    "name": (str,),
    "path": (str,),
    "hash": (str, type(None)),
    "disabled": (bool,),
    "restartOn": (dict,),
    "logs": (list,),
    "pending": (str,),
}


def _tracked_domain(label: str, entry: Dict) -> Tuple[Optional[str], Optional[str]]:
    """(domain, error) for a state entry. State only names labels; paths are derived."""
    domain = entry.get("domain", "user")
    if not valid_label(label) or domain not in DOMAINS:
        return None, f"{label!r}: invalid label or domain {domain!r} in state; left alone"
    bad = [k for k, types in _ENTRY_TYPES.items() if k in entry and not isinstance(entry[k], types)]
    restart_on = entry.get("restartOn") or {}
    if isinstance(restart_on, dict) and not all(
        isinstance(k, str) and isinstance(v, (str, type(None))) for k, v in restart_on.items()
    ):
        bad.append("restartOn")
    logs = entry.get("logs") or []
    if isinstance(logs, list) and not all(isinstance(x, str) for x in logs):
        bad.append("logs")
    if entry.get("pending") not in (None, "activate", "enable", "write"):
        bad.append("pending")
    if bad:
        return None, f"{label!r}: state fields {sorted(set(bad))} have the wrong type; left alone"
    return domain, None


def _ownership(label: str, domain: str, entry: Dict, env: Env) -> Tuple[str, str]:
    """("ours"|"gone"|"modified"|"unknown", plist path) for a tracked label, read fresh."""
    path = plist_path(label, domain, env)
    exists, current, readable = read_current(path, domain, env)
    if not readable:
        return "unknown", path
    if not exists:
        return "gone", path
    if current is None or plist_hash(current) != entry.get("hash"):
        return "modified", path
    return "ours", path


def _plan_removal(p: Plan, label: str, entry: Dict, settings: Settings, env: Env) -> None:
    domain, error = _tracked_domain(label, entry)
    if error:
        p.errors.append(error)
        return
    if entry.get("hash") is None:
        # Only logs were recorded (plist write failed): tools never owned a job here.
        p.forget.append((label, entry, "never written"))
        return
    owned, path = _ownership(label, domain, entry, env)
    live = status(label, domain, env) if owned in ("ours", "gone") else Status(loaded=False)
    if owned == "unknown":
        p.unknown.append((label, f"{path} unreadable; {_FIX_UNREADABLE}"))
    elif owned == "modified":
        p.forget.append((label, entry, "modified since tools wrote it; left in place"))
    elif _foreign(live, path):
        p.forget.append((label, entry, f"loaded from {live.path}, not tools' plist; left alone"))
    elif (
        owned == "gone"
        and not entry.get("disabled")
        and entry.get("pending") != "enable"
        and not live.loaded
    ):
        p.forget.append((label, entry, "already gone"))
    elif not can_change(domain, settings, env):
        p.errors.append(_sudo_error(label))
    else:
        p.remove.append((label, entry))


_FIX_STATE = "fix or delete this entry under launchd.services in the state file"
_FIX_UNREADABLE = "set `launchd.sudo: true` and run `sudo -v` so tools can read it"


def _foreign(live: Status, path: str) -> bool:
    """The label is loaded from a plist other than tools' own path."""
    return live.loaded and bool(live.path) and os.path.normpath(live.path) != path


def _sudo_error(label: str) -> str:
    return f"{label}: system domain needs root; set `launchd.sudo: true` (tools runs `sudo -n`)"


# --------------------------------------------------------------------------
# Apply
# --------------------------------------------------------------------------


def _record(
    services: Dict, item: Item, pending: Optional[str] = None, disabled: Optional[bool] = None
) -> None:
    """Record what succeeded.

    `disabled` is the launchd override tools has set (defaults to config).
    `pending="activate"`: plist written, bootout/bootstrap not done; retried next run.
    `pending="kickstart"`: only a restartOn restart failed; old hashes are kept so the
    next run plans that restart again, and nothing else.
    """
    logs = item.owned_logs
    if logs is None:
        logs = (item.tracked or {}).get("logs") or []
    entry = {
        "name": item.svc.name,
        "domain": item.svc.domain,
        "path": item.path,
        "hash": item.hash,
        "disabled": item.svc.disabled if disabled is None else disabled,
        "restartOn": item.restart_hashes,
        "logs": list(logs),
    }
    if pending:
        entry["restartOn"] = dict((item.tracked or {}).get("restartOn") or {})
    if pending == "activate":
        entry["pending"] = "activate"
    services[item.svc.label] = entry


def _activate(item: Item, env: Env, new: bool) -> Tuple[Optional[str], bool]:
    """Bring launchd in line with the plist on disk.

    Returns (pending kind or None, launchd disabled override now in effect).
    """
    svc = item.svc
    was_disabled = bool((item.tracked or {}).get("disabled")) or bool(item.live_disabled)
    retrying = bool((item.tracked or {}).get("pending"))
    if svc.disabled:
        if not _set_enabled(svc.label, svc.domain, False, env):
            return "activate", was_disabled
        return (None if bootout(svc.label, svc.domain, env) else "activate"), True
    if (was_disabled or retrying) and not _set_enabled(svc.label, svc.domain, True, env):
        return "activate", was_disabled
    if (
        not new
        and not retrying
        and not svc.restart_on_change
        and status(svc.label, svc.domain, env).loaded
    ):
        if item.changed_files:
            # restartOn is its own trigger; restartOnChange only covers plist edits.
            return (None if kickstart(svc.label, svc.domain, env) else "kickstart"), False
        log(f"launchd: {svc.label} plist updated; running job left as is", Color.YELLOW)
        return None, False
    # kickstart would rerun the cached job; only bootout+bootstrap reads the new plist.
    ok = bootout(svc.label, svc.domain, env) and bootstrap(svc.label, svc.domain, item.path, env)
    return (None if ok else "activate"), False


def _fix_perms(item: Item, env: Env) -> bool:
    """Plist mode and owner as tools would have written them, never through a symlink."""
    svc, path, mode = item.svc, item.path, _plist_mode(item.svc)
    if not _needs_sudo(svc.domain):
        try:
            fd = _open_regular(path, os.O_RDONLY)
            try:
                os.fchmod(fd, mode)
            finally:
                os.close(fd)
            return True
        except OSError as e:
            log(f"launchd: cannot fix permissions of {path}: {e}", Color.RED)
            return False
    if os.path.islink(path):
        log(f"launchd: {path} is a symlink; not fixing permissions", Color.RED)
        return False
    # Same content, so reinstalling sets root:wheel and the mode without sudo chmod/chown.
    return _write_plist(path, item.data, svc.domain, env, mode)


def _converge(item: Item, settings: Settings, env: Env, services: Dict, write: bool, new: bool):
    """Shared steps for every job that needs launchd work; records each step that succeeded."""
    _ensure_logs(item, settings, env)
    if write:
        if not _write_plist(item.path, item.data, item.svc.domain, env, _plist_mode(item.svc)):
            _record_logs(services, item)
            return False
    elif item.fix_perms and not _fix_perms(item, env):
        return False
    pending, override = _activate(item, env, new)
    adopted = item.tracked is None and not write
    if pending and adopted and not override:
        # Nothing changed in launchd: do not take ownership on a failed first step.
        return False
    _record(services, item, pending, override)
    return pending is None


def _record_logs(services: Dict, item: Item) -> None:
    """Keep log files created this run in state although the plist write failed.

    A job tools never wrote gets `hash: None`, so it is never treated as owning a plist.
    """
    logs = sorted(set(item.owned_logs or []))
    known = (item.tracked or {}).get("logs") or []
    if logs == sorted(known):
        return
    if item.tracked is not None:
        services[item.svc.label] = {**item.tracked, "logs": logs}
        return
    services[item.svc.label] = {
        "name": item.svc.name,
        "domain": item.svc.domain,
        "path": item.path,
        "hash": None,
        "disabled": False,
        "restartOn": {},
        "logs": logs,
        "pending": "write",
    }


def _retire(label: str, entry: Dict, services: Dict, env: Env) -> bool:
    """Boot out and delete a tracked job; drop it from state only once it is gone."""
    domain, error = _tracked_domain(label, entry)
    if error:
        log(f"launchd: {error}", Color.RED)
        return False
    owned, path = _ownership(label, domain, entry, env)
    if owned not in ("ours", "gone"):
        log(f"launchd: {label}: plist {owned} since planning; not removing {path}", Color.RED)
        return False
    live = status(label, domain, env)
    if _foreign(live, path):
        log(f"launchd: {label}: loaded from {live.path}; not booting it out", Color.RED)
        return False
    if not bootout(label, domain, env) or not _remove_plist(path, domain, env):
        return False
    if entry.get("disabled") and not _set_enabled(label, domain, True, env):
        # Gone from disk, but the override tools set stays: re-enable next run.
        services[label] = {**entry, "pending": "enable"}
        return False
    services.pop(label, None)
    log(f"Removed launchd job: {domain}/{label}", Color.RED)
    return True


def apply(settings: Settings, p: Plan, state: Dict, env: Env) -> bool:
    bad_state = state_error(state)
    if bad_state:
        log(f"launchd: {bad_state}; nothing done", Color.RED)
        return False
    section = state.setdefault("launchd", {})
    services = dict(section.get("services") or {})
    try:
        return _apply(settings, p, env, services)
    finally:
        # Also on an unexpected error: jobs applied so far stay recorded.
        section["services"] = services


def _apply(settings: Settings, p: Plan, env: Env, services: Dict) -> bool:
    success = True
    for err in p.errors:
        log(f"launchd: {err}", Color.RED)
        success = False
    for label, reason in p.blocked:
        log(f"launchd: skipping {label}: {reason}", Color.YELLOW)
    for label, reason in p.unknown:
        log(f"launchd: {label}: state unknown, {reason}; left as is", Color.RED)
        success = False
    for label, reason in p.ignored:
        log(f"launchd: {label}: {reason}", Color.YELLOW)

    # Removals first: a label moving domains or a renamed job may share a port.
    # Held while anything is in error, as an unparsed entry only looks undesired.
    if not p.errors:
        for label, entry, reason in p.forget:
            if reason not in ("already gone", "never written"):
                log(f"launchd: releasing {label}: {reason}", Color.YELLOW)
            services.pop(label, None)
        for label, entry in p.remove:
            success &= _retire(label, entry, services, env)

    # A domain move retires the old job first; if that fails the label stays
    # tracked at its old path and nothing is created under it this run.
    stuck = set()
    every = p.create + p.update + p.adopt + p.load + p.unload + p.enable + p.disable + p.retry
    for item in every + p.restart + p.ok:
        if item.retire is None:
            continue
        if not _retire(item.svc.label, item.retire, services, env):
            log(f"launchd: {item.svc.label}: old job not retired; not moving it", Color.RED)
            stuck.add(id(item))
            success = False
    if stuck:
        for name in ("create", "update", "adopt", "load", "unload", "enable", "disable",
                     "restart", "retry", "ok"):  # fmt: skip
            setattr(p, name, [i for i in getattr(p, name) if id(i) not in stuck])

    steps = (
        (p.create, True, True, "Created"),
        (p.update, True, False, "Updated"),
        (p.retry, False, False, "Activated"),
        (p.disable, False, False, "Disabled"),
        (p.unload, False, False, "Unloaded"),
        (p.enable, False, False, "Enabled"),
        (p.load, False, False, "Loaded"),
    )
    for items, write, new, verb in steps:
        for item in items:
            if _converge(item, settings, env, services, write, new):
                log(f"{verb} launchd job: {item.svc.label}", Color.GREEN)
            else:
                success = False

    for item in p.adopt:
        _ensure_logs(item, settings, env)
        if _fix_perms(item, env):
            _record(services, item)
        else:
            success = False

    for item in p.restart:
        ok = (not item.fix_perms or _fix_perms(item, env)) and kickstart(
            item.svc.label, item.svc.domain, env
        )
        success &= ok
        if ok:
            _record(services, item)
            log(
                f"Restarted {item.svc.label}: {', '.join(item.changed_files)} changed",
                Color.GREEN,
            )

    for item in p.ok:
        # Logs again: a volume may have mounted since the last run.
        _ensure_logs(item, settings, env)
        if item.fix_perms and not _fix_perms(item, env):
            success = False
            continue
        # Refresh restartOn hashes (e.g. a newly listed file) without restarting.
        _record(services, item)
    return success


# --------------------------------------------------------------------------
# Frontend glue: raw `launchd:` mapping in, reconciler out
# --------------------------------------------------------------------------


def _with_sudo(env: Env, settings: Settings) -> Env:
    # copy, not dataclasses.replace: __post_init__ would re-derive defaults.
    env = copy.copy(env)
    env.allow_sudo = settings.sudo
    return env


def install_launchd(raw, state: Dict, env: Optional[Env] = None) -> bool:
    env = env or Env()
    if not env.is_darwin:
        log("launchd: skipping, not macOS", Color.YELLOW)
        return True
    refusal = root_refusal(env)
    if refusal:
        log(f"launchd: {refusal}", Color.RED)
        return False
    bad_state = state_error(state)
    if bad_state:
        log(f"launchd: {bad_state}; nothing done", Color.RED)
        return False
    settings, services, errors = parse_launchd(raw, env.home, env.login)
    env = _with_sudo(env, settings)
    p = plan(settings, services, state, env, errors)
    ok = apply(settings, p, state, env)
    work = (p.create, p.update, p.load, p.unload, p.enable, p.disable, p.restart, p.retry, p.remove)
    if not any(work):
        debug("All launchd jobs up to date", Color.BLUE)
    return ok


def _plist_diff(item: Item) -> List[str]:
    if item.old_data is None:
        return []
    if item.svc.secrets or looks_like_secret(item.data) or looks_like_secret(item.old_data):
        return ["    (secret, diff suppressed)"]
    text = format_diff_bytes(item.data, item.old_data, item.path)
    return [text] if text else []


def diff_launchd(raw, state: Dict, env: Optional[Env] = None) -> List[str]:
    env = env or Env()
    if not env.is_darwin:
        return []
    refusal = root_refusal(env) or state_error(state)
    if refusal:
        return [f"  ! {refusal}"]
    settings, services, errors = parse_launchd(raw, env.home, env.login)
    env = _with_sudo(env, settings)
    p = plan(settings, services, state, env, errors)

    def name(item_or_label, domain=None) -> str:
        if isinstance(item_or_label, Item):
            label, domain = item_or_label.svc.label, item_or_label.svc.domain
        else:
            label = item_or_label
        suffix = " (sudo)" if _needs_sudo(domain) else ""
        return f"{domain}/{label}{suffix}"

    changes = [f"  ! {err}" for err in p.errors]
    changes += [f"  ! skip {label}: {reason}" for label, reason in p.blocked]
    changes += [f"  ? {label}: unknown, {reason}" for label, reason in p.unknown]
    changes += [f"  ? {label}: {reason}" for label, reason in p.ignored]
    for label, entry in p.remove:
        target = name(label, entry.get("domain", "user"))
        changes.append(
            f"  ! held: remove {target} (config has errors)" if p.errors else f"  - remove {target}"
        )
    for label, entry, reason in p.forget:
        if reason not in ("already gone", "never written"):
            held = "  ! held: forget" if p.errors else "  ~ forget"
            changes.append(f"{held} {label} ({reason})")
    lists = (p.create, p.update, p.adopt, p.load, p.unload, p.enable, p.disable, p.restart)
    for item in [i for lst in lists + (p.retry, p.ok) for i in lst]:
        if item.retire is not None:
            changes.append(f"  - remove {name(item.svc.label, item.retire.get('domain', 'user'))}")
    for item in p.create:
        changes.append(f"  + create {name(item)} -> {item.path}")
    changes += [f"  ~ adopt {name(i)} (identical plist)" for i in p.adopt]
    for item in p.update:
        note = ""
        if not item.svc.restart_on_change and not item.svc.disabled:
            note = " (running job not restarted: restartOnChange is false)"
            if item.changed_files:
                note = f" (kickstart: {', '.join(item.changed_files)} changed)"
        changes.append(f"  ~ update {name(item)}{note}")
        changes += _plist_diff(item)
    changes += [f"  ~ disable {name(i)}" for i in p.disable]
    changes += [f"  ~ unload {name(i)} (disabled but loaded)" for i in p.unload]
    changes += [f"  ~ enable {name(i)}" for i in p.enable]
    changes += [f"  ~ load {name(i)} (not loaded)" for i in p.load]
    changes += [f"  ~ restart {name(i)} ({', '.join(i.changed_files)} changed)" for i in p.restart]
    changes += [f"  ~ retry {name(i)} (last activation failed)" for i in p.retry]
    changes += [
        f"  ~ chmod {name(i)} -> {_plist_mode(i.svc):04o}"
        for lst in (p.ok, p.adopt, p.load, p.unload, p.enable, p.disable, p.restart, p.retry)
        for i in lst
        if i.mode_wrong
    ]
    return changes


def status_rows(raw, state: Dict, env: Optional[Env] = None) -> List[Tuple[str, str, Status]]:
    """(label, domain, Status) for every configured or tracked job. Read-only.

    Raises PermissionError when tools runs as root.
    """
    env = env or Env()
    if not env.is_darwin:
        return []
    refusal = root_refusal(env)
    if refusal:
        raise PermissionError(refusal)
    _settings, services, _errors = parse_launchd(raw, env.home, env.login)
    rows = {(s.label, s.domain) for s in services}
    for label, entry in _tracked(state).items():
        domain, error = _tracked_domain(label, entry)
        if error is None:
            rows.add((label, domain))
    return [(label, domain, status(label, domain, env)) for label, domain in sorted(rows)]


def has_work(raw, state: Dict) -> bool:
    section = state.get("launchd")
    if section is not None and not isinstance(section, dict):
        return True
    if raw is not None and not isinstance(raw, dict):
        return True
    services = (raw or {}).get("services")
    if services is not None and not isinstance(services, dict):
        return True
    return bool(services or (section or {}).get("services"))
