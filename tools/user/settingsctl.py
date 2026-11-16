"""macOS permissions, login items, appearance and host settings through settingsctl
(`settings`).

Config shape::

    settingsctl:
      path: /usr/local/bin/settings              # optional; see "Finding settings" below
      fullDiskAccess: [kitty, Terminal]          # settings fulldiskaccess --enable / revoke
      accessibility: [Hammerspoon]               # settings accessibility enable / revoke
      loginItems: [Hammerspoon, LosslessSwitcher]  # settings login add / remove
      appearanceInit: true                       # settings appearance --init, once
      defaultBrowser: firefox                    # settings defaultbrowser set
      # root only (sudo tools deploy):
      hostName: my-mac                           # settings hostname set --host-name
      localHostName: my-mac                      # ... --local-host-name
      computerName: My Mac                       # ... --computer-name
      timeZone: Europe/Uzhgorod                  # settings timezone set
      sleep: {computer: never, display: 10, disk: never}  # settings sleep set
      remoteLogin: true                          # settings remotelogin set on|off

Every command runs as `settings --no-state ...`, so settingsctl keeps no state of its
own; state.json under `settingsctl` records what tools granted. A privacy list with
names not yet recorded reads the pane (`settings <pane> list --json`) and enables the
ones not on; login items are read with `settings login list` every run and missing
ones added with `settings login add`. A recorded name that leaves its list (or the key
or section goes, or the list is `[]`) is revoked: `settings <pane> revoke` for privacy
panes, `settings login remove` per login item. Names tools never recorded are left
alone. After a failed step the live list is read again and the names it shows granted
are recorded; when it cannot be read, failed revokes stay recorded and failed applies
record their targets.

`appearanceInit` runs `settings appearance --init` once and records it. Every other
key is read first (`settings ... get --json`) and set only when it differs. Root keys
run in the sudo phase; as the user they are read and reported in a note. A scalar key
that leaves config is left as is.

Finding settings: `path` when given; otherwise, as the user, `settings` on PATH and
then in `paths.uvBin`. Root never searches its own PATH: it uses `path`, `paths.uvBin`
or the user's usual tool dirs (~/.local/bin, /opt/homebrew/bin, /usr/local/bin) and
fails when none has it.
"""

import json
import os
import pwd
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

NO_STATE = "--no-state"
SUDO_VARS = ("SUDO_USER", "SUDO_UID", "SUDO_GID", "SUDO_COMMAND")
APPEARANCE_INIT = ["appearance", "--init"]
PANE = {"fullDiskAccess": "fulldiskaccess", "accessibility": "accessibility"}
ENABLE = {
    "fullDiskAccess": ["fulldiskaccess", "--enable"],
    "accessibility": ["accessibility", "enable"],
}


def _name(key: str, app: str) -> str:
    if key == "loginItems":
        return os.path.splitext(os.path.basename(app))[0] if "/" in app else app
    return app.partition("=")[0].strip()


def _names(key: str, apps: List[str]) -> List[str]:
    return [_name(key, a) for a in apps]


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
    env: Optional[Dict[str, str]] = None

    def call(self, *args: str) -> Tuple[int, str, str]:
        rc, stdout, stderr = self.run([self.cli, NO_STATE, *args], env=self.env)
        if rc != 0 and NO_STATE in (stderr or "") and "No such option" in (stderr or ""):
            stderr = (
                f"{self.cli} is too old for {NO_STATE}; update it "
                "(tools deploy --update --only uv:settingsctl, or rebuild its nix package)"
            )
        return rc, stdout, stderr

    def get(self, *args: str) -> Dict:
        rc, stdout, stderr = self.call(*args, "--json")
        if rc != 0:
            raise RuntimeError(f"settings {' '.join(args)}: {(stderr or stdout).strip() or rc}")
        try:
            data = json.loads(stdout)
        except ValueError as e:
            raise RuntimeError(f"settings {' '.join(args)}: not JSON: {e}") from e
        if not isinstance(data, dict):
            raise RuntimeError(f"settings {' '.join(args)}: expected a JSON object")
        return data


def _env(root: bool) -> Optional[Dict[str, str]]:
    """As the user: HOME from passwd and no SUDO_*, since activation may run us with
    root's HOME or SUDO_USER=root and settingsctl derives its home from them."""
    if root:
        return None
    env = {k: v for k, v in os.environ.items() if k not in SUDO_VARS}
    env["HOME"] = pwd.getpwuid(os.getuid()).pw_dir
    return env


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


def _login_items(runner: Optional[Runner]) -> Optional[List[str]]:
    """Current login item names, or None when they cannot be read."""
    if not runner:
        return None
    rc, stdout, _ = runner.call("login", "list")
    if rc != 0:
        return None
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    return [] if lines == ["No login items found."] else lines


def _live(key: str, runner: Optional[Runner]) -> Optional[set]:
    """Names granted now (enabled pane rows, or login items); None when unreadable."""
    if key == "loginItems":
        items = _login_items(runner)
        return None if items is None else set(items)
    if not runner:
        return None
    try:
        items = runner.get(PANE[key], "list").get("items")
    except RuntimeError:
        return None
    if not isinstance(items, list):
        return None
    return {
        item["name"]
        for item in items
        if isinstance(item, dict) and item.get("enabled") and isinstance(item.get("name"), str)
    }


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


def _legacy_appearance_marker() -> str:
    home = pwd.getpwuid(os.getuid()).pw_dir
    return os.path.join(home, ".local/state/settings/appearance/last-run")


def _inited(state: Optional[Dict]) -> bool:
    section = (state or {}).get("settingsctl")
    return isinstance(section, dict) and section.get("appearanceInit") is True


def _init_done(state: Optional[Dict]) -> bool:
    """appearanceInit already ran: recorded, or settingsctl's own marker from before
    tools recorded it, so an upgrade never forces dark mode again."""
    return _inited(state) or os.path.exists(_legacy_appearance_marker())


@dataclass
class ListStep:
    key: str
    row: str
    args: List[str]
    revoke: List[str]  # names this step revokes; empty for an apply


def _step(key: str, sign: str, args: List[str], revoke: List[str]) -> ListStep:
    return ListStep(key, f"  {sign} settings {_shown(args)}", args, revoke)


def plan_lists(
    spec: Dict, runner: Optional[Runner], record: Dict[str, List[str]]
) -> List[ListStep]:
    """Applies and revokes for the list keys; reads only. Only recorded names are
    revoked; live reads decide which targets still need an apply."""
    steps: List[ListStep] = []
    for key in LIST_KEYS:
        apps = spec.get(key) or []
        had = set(record.get(key) or [])
        dropped = sorted(had - set(_names(key, apps)))
        if key == "loginItems":
            if not apps and not dropped:
                continue
            current = _live(key, runner)
            known = had if current is None else current
            for app in apps:
                if _name(key, app) not in known:
                    steps.append(_step(key, "+", ["login", "add", app], []))
            for name in dropped:
                if current is None or name in current:
                    steps.append(_step(key, "-", ["login", "remove", name], [name]))
            continue
        pending = [a for a in apps if _name(key, a) not in had]
        if pending:
            current = _live(key, runner)
            if current is not None:
                pending = [a for a in apps if _name(key, a) not in current]
        if pending:
            steps.append(_step(key, "+", [*ENABLE[key], ",".join(pending)], []))
        if dropped:
            args = [PANE[key], "revoke", ",".join(dropped)]
            steps.append(_step(key, "-", args, dropped))
    return steps


def plan_user(
    spec: Dict,
    runner: Optional[Runner],
    record: Optional[Dict[str, List[str]]] = None,
    inited: bool = False,
) -> Tuple[List[Tuple[str, List[str]]], List[str]]:
    """([(diff row, settings args)], read errors) for user keys and recorded lists."""
    todo, errors = plan_other(spec, runner, inited)
    lists = [(step.row, step.args) for step in plan_lists(spec, runner, record or {})]
    return lists + todo, errors


def plan_other(
    spec: Dict, runner: Optional[Runner], inited: bool = False
) -> Tuple[List[Tuple[str, List[str]]], List[str]]:
    """([(diff row, settings args)], read errors) for the user keys that are not lists."""
    todo: List[Tuple[str, List[str]]] = []
    errors: List[str] = []
    if spec.get("appearanceInit") and not inited:
        todo.append((f"  ~ settings {_shown(APPEARANCE_INIT)}", APPEARANCE_INIT))
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
    return raw is not None or bool(recorded(state)) or _inited(state)


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
    runner = Runner(cli, run or run_command, _env(root)) if cli else None
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
    todo, read_errors = plan_user(spec, runner, record, _init_done(state))
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
        rc, stdout, stderr = runner.call(*args)
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
    results: List[Tuple[ListStep, bool]],
    runner: Runner,
) -> List[str]:
    """Names tools granted for key after this run. When a step failed, the ones the live
    list shows granted; when that cannot be read, the targets plus dropped names whose
    revoke failed."""
    target = set(_names(key, spec.get(key) or []))
    steps = [(step, ok) for step, ok in results if step.key == key]
    if all(ok for _step, ok in steps):
        return sorted(target)
    had = set(record.get(key) or [])
    live = _live(key, runner)
    if live is not None:
        return sorted((had | target) & live)
    failed = {name for step, ok in steps if not ok for name in step.revoke}
    return sorted(target | failed)


def _store(state: Optional[Dict], lists: Optional[Dict[str, List[str]]], inited: bool) -> None:
    """Write the `settingsctl` record; lists None keeps the recorded lists."""
    if state is None:
        return
    section = state.get("settingsctl")
    section = section if isinstance(section, dict) else {}
    for key in LIST_KEYS:
        if lists is None:
            continue
        if lists.get(key):
            section[key] = lists[key]
        else:
            section.pop(key, None)
    if inited:
        section["appearanceInit"] = True
    else:
        section.pop("appearanceInit", None)
    if section:
        state["settingsctl"] = section
    else:
        state.pop("settingsctl", None)


def install_settingsctl(
    raw,
    paths: Optional[Dict] = None,
    which: Callable = shutil.which,
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
    inited = bool(spec.get("appearanceInit")) and _init_done(state)
    if not root:
        if has_root and not privilege.UNDER_SUDO:
            count = sum(1 for k in ROOT_KEYS if k in spec)
            msg = f"{count} system setting(s) not managed as user; {ROOT_HINT}"
            log(f"settingsctl: {msg}", Color.YELLOW)
        if not _needs_cli_as_user(spec, record):
            _store(state, None, inited)
            return True
    cli, looked = find_cli(spec, paths, root, which, executable)
    if not cli:
        log(f"settingsctl: `settings` not found (looked in {looked})", Color.RED)
        return False
    runner = Runner(cli, run or run_command, _env(root))
    if root:
        todo, read_errors = plan_root(spec, runner)
        for err in read_errors:
            log(f"settingsctl: {err}", Color.RED)
        return _apply(runner, todo) and not read_errors
    plan = plan_lists(spec, runner, record)
    todo, read_errors = plan_other(spec, runner, inited)
    for err in read_errors:
        log(f"settingsctl: {err}", Color.RED)
    results = [(step, _apply(runner, [(step.row, step.args)])) for step in plan]
    success = all(ok for _step, ok in results)
    for row, args in todo:
        ok = _apply(runner, [(row, args)])
        success = ok and success
        if args == APPEARANCE_INIT and ok:
            inited = True
    if state is not None:
        lists = {key: _still_recorded(key, spec, record, results, runner) for key in LIST_KEYS}
        _store(state, lists, inited)
    return success and not read_errors
