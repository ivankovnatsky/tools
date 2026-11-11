"""macOS permissions, login items, appearance and host settings through settingsctl
(`settings`).

Config shape::

    settingsctl:
      path: /usr/local/bin/settings              # optional; see "Finding settings" below
      fullDiskAccess: [kitty, Terminal]          # settings fulldiskaccess --enable
      accessibility: [Hammerspoon]               # settings accessibility set
      loginItems: [Hammerspoon, LosslessSwitcher]  # settings login set
      appearanceInit: true                       # settings appearance --init
      defaultBrowser: firefox                    # settings defaultbrowser set
      # root only (sudo tools deploy):
      hostName: my-mac                           # settings hostname set --host-name
      localHostName: my-mac                      # ... --local-host-name
      computerName: My Mac                       # ... --computer-name
      timeZone: Europe/Uzhgorod                  # settings timezone set
      sleep: {computer: never, display: 10, disk: never}  # settings sleep set
      remoteLogin: true                          # settings remotelogin set on|off

The lists are passed on every deploy; settingsctl decides what to change. Every other
key is read first (`settings ... get --json`, the appearance state file) and set
only when it differs. Root keys run in the sudo phase; as the
user they are read and reported in a note. A key that leaves config is left as is.

Finding settings: `path` when given; otherwise, as the user, `settings` on PATH and
then in `paths.uvBin`. Root never searches its own PATH: it uses `path`, `paths.uvBin`
or the user's usual tool dirs (~/.local/bin, /opt/homebrew/bin, /usr/local/bin) and
fails when none has it.
"""

import json
import os
import shutil
import sys
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from tools import privilege
from tools.log import Color, log
from tools.util import run_command

LIST_KEYS = ("fullDiskAccess", "accessibility", "loginItems")
BOOL_KEYS = ("appearanceInit",)
NAME_KEYS = {
    "hostName": "--host-name",
    "localHostName": "--local-host-name",
    "computerName": "--computer-name",
}
ROOT_KEYS = (*NAME_KEYS, "timeZone", "sleep", "remoteLogin")
USER_KEYS = LIST_KEYS + BOOL_KEYS + ("defaultBrowser",)
KEYS = ("path",) + USER_KEYS + ROOT_KEYS
SLEEP_KEYS = ("computer", "display", "disk")
ROOT_HINT = "run sudo tools deploy --scope settingsctl"

APPEARANCE_STATE = "~/.local/state/settings/appearance/last-run"


def _command(key: str, apps: List[str]) -> List[str]:
    joined = ",".join(apps)
    if key == "fullDiskAccess":
        return ["fulldiskaccess", "--enable", joined]
    if key == "accessibility":
        return ["accessibility", "set", joined]
    return ["login", "set", joined]


def _sleep_error(value) -> Optional[str]:
    if value == "never":
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 180:
        return f"must be minutes (1-180) or 'never', got {value!r}"
    return None


def _value_error(key: str, value) -> Optional[str]:
    if key in BOOL_KEYS or key == "remoteLogin":
        return None if isinstance(value, bool) else "must be true or false"
    if key == "path":
        if not isinstance(value, str) or not os.path.isabs(os.path.expanduser(value)):
            return "must be an absolute path to the settings binary"
        return None
    if key == "sleep":
        if not isinstance(value, dict) or not value:
            return f"must map {list(SLEEP_KEYS)} to minutes or 'never'"
        unknown = set(value) - set(SLEEP_KEYS)
        if unknown:
            return f"unknown keys {sorted(unknown, key=str)} (expected {list(SLEEP_KEYS)})"
        for name, minutes in value.items():
            why = _sleep_error(minutes)
            if why:
                return f"{name}: {why}"
        return None
    if not isinstance(value, str) or not value.strip() or "\n" in value:
        return "must be a non-empty single-line string"
    if value.startswith("-"):
        return "must not start with '-'"
    return None


def parse(raw) -> Tuple[Dict, List[str]]:
    """Validate `settingsctl`. Returns ({key: value} for declared keys, errors); any error
    holds the whole section. Names, zones and browsers are checked by settingsctl."""
    if raw is None:
        return {}, []
    if not isinstance(raw, dict):
        return {}, [f"settingsctl must be a mapping, got {type(raw).__name__}"]
    errors = []
    unknown = set(raw) - set(KEYS)
    if unknown:
        errors.append(f"unknown keys {sorted(unknown, key=str)} (expected {sorted(KEYS)})")
    spec: Dict = {}
    for key in LIST_KEYS:
        apps = raw.get(key)
        if apps is None:
            continue
        if (
            not isinstance(apps, list)
            or not apps
            or not all(isinstance(a, str) and a.strip() and "," not in a for a in apps)
        ):
            errors.append(
                f"{key}: must be a non-empty list of app names without commas"
                " (omit the key to leave it alone)"
            )
            continue
        spec[key] = [a.strip() for a in apps]
    for key in KEYS:
        if key in LIST_KEYS or raw.get(key) is None:
            continue
        why = _value_error(key, raw[key])
        if why:
            errors.append(f"{key}: {why}")
            continue
        spec[key] = raw[key]
    return ({} if errors else spec), errors


# --------------------------------------------------------------------------
# Finding and running settings
# --------------------------------------------------------------------------


@dataclass
class Runner:
    cli: str
    run: Callable

    def get(self, *args: str) -> Dict:
        cmd = [self.cli, *args, "--json"]
        rc, stdout, stderr = self.run(cmd)
        if rc != 0:
            raise RuntimeError(f"settings {' '.join(args)}: {(stderr or stdout).strip() or rc}")
        try:
            data = json.loads(stdout)
        except ValueError as e:
            raise RuntimeError(f"settings {' '.join(args)}: not JSON: {e}") from e
        if not isinstance(data, dict):
            raise RuntimeError(f"settings {' '.join(args)}: expected a JSON object")
        return data


def _executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def find_cli(
    spec: Dict,
    paths: Optional[Dict],
    root: bool,
    which: Callable = shutil.which,
    executable: Callable[[str], bool] = _executable,
    home: Optional[str] = None,
) -> Tuple[Optional[str], str]:
    """(absolute path of `settings` or None, where it was looked for)."""
    if spec.get("path"):
        path = os.path.expanduser(spec["path"])
        return (path if executable(path) else None), path
    dirs = []
    uv_bin = (paths or {}).get("uvBin")
    if isinstance(uv_bin, str) and uv_bin:
        dirs.append(os.path.expanduser(uv_bin))
    if root:
        home = home or privilege.invoker().home
        dirs += [d.format(home=home) for d in privilege.USER_PATH_DIRS]
    else:
        found = which("settings")
        if found:
            return os.path.abspath(found), "PATH"
    for d in dict.fromkeys(dirs):
        candidate = os.path.join(d, "settings")
        if executable(candidate):
            return candidate, candidate
    return None, ("PATH, " if not root else "") + ", ".join(dict.fromkeys(dirs))


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------


def _fmt(value) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if value is None:
        return "(not set)"
    return repr(value) if isinstance(value, str) and value != "never" else str(value)


def _shown(args: List[str]) -> str:
    return " ".join(a if a.startswith("-") or " " not in a else repr(a) for a in args)


def plan_root(spec: Dict, runner: Runner) -> Tuple[List[Tuple[str, List[str]]], List[str]]:
    """([(diff row, settings args)], read errors) for declared root keys; reads only."""
    todo: List[Tuple[str, List[str]]] = []
    errors: List[str] = []

    def guarded(key, fn):
        try:
            fn()
        except RuntimeError as e:
            errors.append(f"{key}: {e}")

    def names():
        current = runner.get("hostname", "get")
        args, was = [], []
        for key, flag in NAME_KEYS.items():
            if key in spec and current.get(key) != spec[key]:
                args += [flag, spec[key]]
                was.append(f"{key} {_fmt(current.get(key))}")
        if args:
            cmd = ["hostname", "set", *args]
            todo.append((f"  ~ settings {_shown(cmd)} (now {', '.join(was)})", cmd))

    def zone():
        want = spec["timeZone"]
        current = runner.get("timezone", "get", want)
        if not current.get("matches"):
            cmd = ["timezone", "set", want]
            now = _fmt(current.get("timeZone"))
            todo.append((f"  ~ settings {_shown(cmd)} (now {now})", cmd))

    def sleep():
        sources = runner.get("sleep", "get").get("sources") or {}
        if not sources:
            raise RuntimeError("settings sleep get: no power sources")
        args, was = [], []
        for name, want in spec["sleep"].items():
            values = {src: (vals or {}).get(name) for src, vals in sources.items()}
            if all(v == want for v in values.values()):
                continue
            args += [f"--{name}", str(want)]
            seen = set(values.values())
            now = (
                _fmt(next(iter(seen)))
                if len(seen) == 1
                else ", ".join(f"{s} {_fmt(v)}" for s, v in values.items())
            )
            was.append(f"{name} {now}")
        if args:
            cmd = ["sleep", "set", *args]
            todo.append((f"  ~ settings {_shown(cmd)} (now {'; '.join(was)})", cmd))

    def remote_login():
        want = spec["remoteLogin"]
        current = runner.get("remotelogin", "get")
        enabled, loaded = bool(current.get("enabled")), bool(current.get("loaded"))
        done = (enabled and loaded) if want else not (enabled or loaded)
        if not done:
            mode = "on" if want else "off"
            cmd = ["remotelogin", "set", mode]
            state = f"enabled {_fmt(enabled)}, loaded {_fmt(loaded)}"
            todo.append((f"  ~ settings {_shown(cmd)} (now {state})", cmd))

    if any(k in spec for k in NAME_KEYS):
        guarded("hostname", names)
    if "timeZone" in spec:
        guarded("timeZone", zone)
    if "sleep" in spec:
        guarded("sleep", sleep)
    if "remoteLogin" in spec:
        guarded("remoteLogin", remote_login)
    return todo, errors


def plan_user(
    spec: Dict,
    runner: Optional[Runner],
    exists: Callable[[str], bool] = os.path.exists,
) -> Tuple[List[Tuple[str, List[str]]], List[str]]:
    """([(diff row, settings args)], read errors) for declared user keys."""
    todo: List[Tuple[str, List[str]]] = []
    errors: List[str] = []
    for key in LIST_KEYS:
        if key in spec:
            args = _command(key, spec[key])
            todo.append((f"  ~ settings {' '.join(args[:-1])} {args[-1]!r}", args))
    if spec.get("appearanceInit") and not exists(os.path.expanduser(APPEARANCE_STATE)):
        todo.append(("  ~ settings appearance --init", ["appearance", "--init"]))
    if "defaultBrowser" in spec and runner:
        want = spec["defaultBrowser"]
        try:
            current = runner.get("defaultbrowser", "get")
            if want not in (current.get("browsers") or []):
                errors.append(
                    f"defaultBrowser: {want!r} is not an installed browser"
                    f" (have {', '.join(current.get('browsers') or []) or 'none'})"
                )
            elif current.get("defaultBrowser") != want:
                cmd = ["defaultbrowser", "set", want]
                now = _fmt(current.get("defaultBrowser"))
                todo.append((f"  ~ settings {_shown(cmd)} (now {now})", cmd))
        except RuntimeError as e:
            errors.append(f"defaultBrowser: {e}")
    return todo, errors


def _needs_cli_as_user(spec: Dict) -> bool:
    return any(k in spec for k in USER_KEYS)


# --------------------------------------------------------------------------
# Section entry points
# --------------------------------------------------------------------------


def has_work(raw) -> bool:
    return raw is not None


def has_root_work(raw) -> bool:
    """Root keys declared, or a config that fails to parse (so the sudo phase stops)."""
    if raw is None:
        return False
    spec, errors = parse(raw)
    return bool(errors) or any(k in spec for k in ROOT_KEYS)


def _is_root() -> bool:
    return os.geteuid() == 0


def diff_settingsctl(
    raw,
    paths: Optional[Dict] = None,
    which: Callable = shutil.which,
    exists: Callable[[str], bool] = os.path.exists,
    run: Optional[Callable] = None,
    root: Optional[bool] = None,
    executable: Callable[[str], bool] = _executable,
) -> List[str]:
    if sys.platform != "darwin":
        return []
    root = _is_root() if root is None else root
    spec, errors = parse(raw)
    if errors:
        return [f"  ! {err}" for err in errors]
    cli, looked = find_cli(spec, paths, root, which, executable)
    runner = Runner(cli, run or run_command) if cli else None
    has_root = any(k in spec for k in ROOT_KEYS)
    if root:
        if not has_root:
            return []
        if not runner:
            return [f"  ! `settings` not found (looked in {looked})"]
        todo, read_errors = plan_root(spec, runner)
        return [f"  ? {e}" for e in read_errors] + [row for row, _ in todo]
    rows: List[str] = []
    needs = _needs_cli_as_user(spec)
    if needs and not runner and "defaultBrowser" in spec:
        rows.append(f"  ? `settings` not found (looked in {looked})")
    todo, read_errors = plan_user(spec, runner, exists)
    rows += [f"  ? {e}" for e in read_errors] + [row for row, _ in todo]
    if has_root and not privilege.UNDER_SUDO:
        if not runner:
            log(f"settingsctl: cannot check system settings, `settings` not found; {ROOT_HINT}")
        else:
            pending, unreadable = plan_root(spec, runner)
            if pending:
                msg = f"{len(pending)} system setting(s) differ; {ROOT_HINT}"
                log(f"settingsctl: {msg}", Color.YELLOW)
            for err in unreadable:
                log(f"settingsctl: cannot check {err}; {ROOT_HINT}", Color.YELLOW)
    return rows


def _apply(runner: Runner, todo: List[Tuple[str, List[str]]]) -> bool:
    success = True
    for _row, args in todo:
        rc, stdout, stderr = runner.run([runner.cli, *args])
        for line in stdout.splitlines():
            log(f"  {line}")
        for line in stderr.splitlines():
            log(f"  {line}", Color.YELLOW)
        if rc != 0:
            log(f"settingsctl: settings {' '.join(args[:2])} failed (exit {rc})", Color.RED)
            success = False
    return success


def install_settingsctl(
    raw,
    paths: Optional[Dict] = None,
    which: Callable = shutil.which,
    exists: Callable[[str], bool] = os.path.exists,
    run: Optional[Callable] = None,
    root: Optional[bool] = None,
    executable: Callable[[str], bool] = _executable,
) -> bool:
    if sys.platform != "darwin":
        log("settingsctl: skipping, not macOS", Color.YELLOW)
        return True
    root = _is_root() if root is None else root
    spec, errors = parse(raw)
    for err in errors:
        log(f"settingsctl: {err}", Color.RED)
    if errors:
        return False
    has_root = any(k in spec for k in ROOT_KEYS)
    if root and not has_root:
        return True
    if not root:
        if has_root and not privilege.UNDER_SUDO:
            count = sum(1 for k in ROOT_KEYS if k in spec)
            msg = f"{count} system setting(s) not managed as user; {ROOT_HINT}"
            log(f"settingsctl: {msg}", Color.YELLOW)
        if not _needs_cli_as_user(spec):
            return True
    cli, looked = find_cli(spec, paths, root, which, executable)
    if not cli:
        log(f"settingsctl: `settings` not found (looked in {looked})", Color.RED)
        return False
    runner = Runner(cli, run or run_command)
    if root:
        todo, read_errors = plan_root(spec, runner)
    else:
        todo, read_errors = plan_user(spec, runner, exists)
    for err in read_errors:
        log(f"settingsctl: {err}", Color.RED)
    return _apply(runner, todo) and not read_errors
