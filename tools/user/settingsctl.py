"""macOS permissions, login items, appearance and host settings through settingsctl
(`settings`).

Config shape::

    settingsctl:
      path: /usr/local/bin/settings              # optional; see "Finding settings" below
      fullDiskAccess: [kitty, Terminal]          # settings fulldiskaccess set
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

A list runs when it differs from what settingsctl last applied (its state files under
~/.local/state/settings) or, for login items, from `settings login list`. Privacy lists
use `set`, or `enable` when settingsctl's state names apps tools never recorded (so
`set` would revoke them). After an apply the granted names are recorded in state.json
under `settingsctl`; a recorded name that leaves its list (or the key or section goes,
or the list is `[]`) is revoked: `settings <pane> revoke` for privacy panes, `settings
login remove` per login item. A failed revoke stays recorded and is retried.

Every other key is read first (`settings ... get --json`, the appearance state file)
and set only when it differs. Root keys run in the sudo phase; as the user they are
read and reported in a note. A scalar key that leaves config is left as is.

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

STATE_DIR = "~/.local/state/settings"
APPEARANCE_STATE = f"{STATE_DIR}/appearance/last-run"
LIST_STATE = {"fullDiskAccess": "fda-enabled", "accessibility": "accessibility-enabled"}
PANE = {"fullDiskAccess": "fulldiskaccess", "accessibility": "accessibility"}


ENABLE = {
    "fullDiskAccess": ["fulldiskaccess", "--enable"],
    "accessibility": ["accessibility", "enable"],
}


def _command(key: str, apps: List[str], enable: bool = False) -> List[str]:
    joined = ",".join(apps)
    if key == "loginItems":
        return ["login", "set", joined]
    return [*ENABLE[key], joined] if enable else [PANE[key], "set", joined]


def _names(key: str, apps: List[str]) -> List[str]:
    if key == "loginItems":
        return [os.path.splitext(os.path.basename(a))[0] if "/" in a else a for a in apps]
    return [a.partition("=")[0].strip() for a in apps]


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
        if not isinstance(apps, list) or not all(
            isinstance(a, str) and a.strip() and "," not in a for a in apps
        ):
            errors.append(f"{key}: must be a list of app names without commas")
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


def _read_state(name: str) -> Optional[str]:
    try:
        with open(os.path.join(os.path.expanduser(STATE_DIR), name)) as f:
            return f.read().strip()
    except OSError:
        return None


def _login_items(runner: Optional[Runner]) -> Optional[List[str]]:
    """Current login item names, or None when they cannot be read."""
    if not runner:
        return None
    rc, stdout, _ = runner.run([runner.cli, "login", "list"])
    if rc != 0:
        return None
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    return [] if lines == ["No login items found."] else lines


def _state_names(key: str) -> List[str]:
    return [a.strip() for a in (_read_state(LIST_STATE[key]) or "").split(",") if a.strip()]


def recorded(state: Optional[Dict]) -> Dict[str, List[str]]:
    """{list key: names tools granted} from state.json."""
    section = (state or {}).get("settingsctl")
    if not isinstance(section, dict):
        return {}
    return {
        key: [n for n in names if isinstance(n, str)]
        for key, names in section.items()
        if key in LIST_KEYS and isinstance(names, list) and names
    }


@dataclass
class ListStep:
    key: str
    row: str
    args: List[str]
    revoke: List[str]  # names this step revokes; empty for an apply
    method: str = "set"


@dataclass
class ListPlan:
    steps: List[ListStep]
    forget: Dict[str, List[str]]  # recorded login items already gone


def _apply_row(args: List[str], now: Optional[str]) -> str:
    row = f"  ~ settings {' '.join(args[:-1])} {args[-1]!r}"
    if now is not None:
        row += f" (now {now!r})" if now else " (now none)"
    return row


def plan_lists(spec: Dict, runner: Optional[Runner], record: Dict[str, List[str]]) -> ListPlan:
    """Applies and revokes for the list keys; reads only."""
    result = ListPlan([], {})
    for key in LIST_KEYS:
        target = _names(key, spec.get(key) or [])
        dropped = sorted(set(record.get(key) or []) - set(target))
        if not target and not dropped:
            continue
        if key == "loginItems":
            current = _login_items(runner)
            if target:
                if current is None or set(current) != set(target):
                    args = _command(key, spec[key])
                    now = None if current is None else ",".join(sorted(current))
                    result.steps.append(ListStep(key, _apply_row(args, now), args, []))
                continue
            gone = [n for n in dropped if current is not None and n not in current]
            if gone:
                result.forget[key] = gone
            for name in dropped:
                if name in gone:
                    continue
                args = ["login", "remove", name]
                result.steps.append(ListStep(key, f"  - settings {_shown(args)}", args, [name]))
            continue
        raw = _read_state(LIST_STATE[key])
        applied = _state_names(key)
        revoke = dropped
        if target:
            extras = set(applied) - set(record.get(key) or []) - set(target)
            if sorted(applied) != sorted(target):
                args = _command(key, spec[key], enable=bool(extras))
                row = _apply_row(args, raw)
                if extras:
                    row += f" (leaves {','.join(sorted(extras))!r} alone)"
                method = "enable" if extras else "set"
                result.steps.append(ListStep(key, row, args, [], method))
                if not extras:
                    revoke = [n for n in dropped if n not in applied]
        if revoke:
            args = [PANE[key], "revoke", ",".join(revoke)]
            row = f"  - settings {' '.join(args[:-1])} {args[-1]!r}"
            result.steps.append(ListStep(key, row, args, revoke))
    return result


def plan_user(
    spec: Dict,
    runner: Optional[Runner],
    exists: Callable[[str], bool] = os.path.exists,
    record: Optional[Dict[str, List[str]]] = None,
) -> Tuple[List[Tuple[str, List[str]]], List[str]]:
    """([(diff row, settings args)], read errors) for user keys and recorded lists."""
    todo, errors = plan_other(spec, runner, exists)
    lists = [(step.row, step.args) for step in plan_lists(spec, runner, record or {}).steps]
    return lists + todo, errors


def plan_other(
    spec: Dict, runner: Optional[Runner], exists: Callable[[str], bool] = os.path.exists
) -> Tuple[List[Tuple[str, List[str]]], List[str]]:
    """([(diff row, settings args)], read errors) for the user keys that are not lists."""
    todo: List[Tuple[str, List[str]]] = []
    errors: List[str] = []
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


def _needs_cli_as_user(spec: Dict, record: Dict[str, List[str]]) -> bool:
    others = any(k in spec for k in USER_KEYS if k not in LIST_KEYS)
    return others or bool(record) or any(spec.get(k) for k in LIST_KEYS)


# --------------------------------------------------------------------------
# Section entry points
# --------------------------------------------------------------------------


def has_work(raw, state: Optional[Dict] = None) -> bool:
    return raw is not None or bool(recorded(state))


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
    state: Optional[Dict] = None,
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
    record = recorded(state)
    if _needs_cli_as_user(spec, record) and not runner:
        rows.append(f"  ? `settings` not found (looked in {looked})")
    todo, read_errors = plan_user(spec, runner, exists, record)
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


def _still_recorded(
    key: str,
    spec: Dict,
    record: Dict[str, List[str]],
    plan: ListPlan,
    results: List[Tuple[ListStep, bool]],
    runner: Runner,
) -> List[str]:
    """Names tools granted for key after this run's steps: what it applied, plus dropped
    names whose revoke did not succeed."""
    target = set(_names(key, spec.get(key) or []))
    had = set(record.get(key) or [])
    steps = [(step, ok) for step, ok in results if step.key == key]
    applied = next(((step, ok) for step, ok in steps if not step.revoke), None)
    names = had & target
    after: Optional[set] = None
    if target:
        if applied is None or applied[1]:
            names |= target
        elif key == "loginItems":
            current = _login_items(runner)
            after = None if current is None else set(current)
            names |= target & (after or set())
        elif applied[0].method == "set":
            after = set(_state_names(key))
            names |= target & after
        else:
            names |= target
    gone = set(plan.forget.get(key) or [])
    for name in had - target:
        if name in gone:
            continue
        revoke = next((ok for step, ok in steps if name in step.revoke), None)
        if revoke is not None:
            if not revoke:
                names.add(name)
        elif applied is not None and not applied[1] and (after is None or name in after):
            names.add(name)
    return sorted(names)


def install_settingsctl(
    raw,
    paths: Optional[Dict] = None,
    which: Callable = shutil.which,
    exists: Callable[[str], bool] = os.path.exists,
    run: Optional[Callable] = None,
    root: Optional[bool] = None,
    executable: Callable[[str], bool] = _executable,
    state: Optional[Dict] = None,
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
    record = {} if root else recorded(state)
    if not root:
        if has_root and not privilege.UNDER_SUDO:
            count = sum(1 for k in ROOT_KEYS if k in spec)
            msg = f"{count} system setting(s) not managed as user; {ROOT_HINT}"
            log(f"settingsctl: {msg}", Color.YELLOW)
        if not _needs_cli_as_user(spec, record):
            return True
    cli, looked = find_cli(spec, paths, root, which, executable)
    if not cli:
        log(f"settingsctl: `settings` not found (looked in {looked})", Color.RED)
        return False
    runner = Runner(cli, run or run_command)
    if root:
        todo, read_errors = plan_root(spec, runner)
        for err in read_errors:
            log(f"settingsctl: {err}", Color.RED)
        return _apply(runner, todo) and not read_errors
    plan = plan_lists(spec, runner, record)
    todo, read_errors = plan_other(spec, runner, exists)
    for err in read_errors:
        log(f"settingsctl: {err}", Color.RED)
    results = [(step, _apply(runner, [(step.row, step.args)])) for step in plan.steps]
    success = all(ok for _step, ok in results)
    success = _apply(runner, todo) and success
    if state is not None:
        section = state.get("settingsctl")
        section = section if isinstance(section, dict) else {}
        for key in LIST_KEYS:
            names = _still_recorded(key, spec, record, plan, results, runner)
            if names:
                section[key] = names
            else:
                section.pop(key, None)
        if section:
            state["settingsctl"] = section
        else:
            state.pop("settingsctl", None)
    return success and not read_errors
