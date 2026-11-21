"""macOS permissions, login items, appearance and host settings through settingsctl
(`settings`).

Config shape::

    settingsctl:
      path: /usr/local/bin/settings              # optional; see "Finding settings" below
      fullDiskAccess: [kitty, Terminal]          # settings fulldiskaccess --enable / revoke
      accessibility: [Hammerspoon]               # settings accessibility enable / revoke
      loginItems: [Hammerspoon, LosslessSwitcher]  # settings login add / remove
      appearanceInit: true                       # settings appearance --init, once
      scalingInit: true                          # settings scaling --init, every run
      locationInit: true                         # settings location --init, every run
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
names not recorded as granted reads the pane (`settings <pane> list --json`) and enables the
ones not on; login items are read with `settings login list` every run and missing
ones added with `settings login add`. A recorded name that leaves its list (or the key
or section goes, or the list is `[]`) is revoked: `settings <pane> revoke` for privacy
panes, `settings login remove` per login item. Names tools never recorded are left
alone. The record lists every name tools granted or tried to grant, so revokes cover
them all; `settingsctl.unconfirmed` holds the ones a failed step left unverified, which
count as not granted until a live read or an apply confirms them. Names whose revoke
failed stay recorded. An unreadable login item list fails the section.

`appearanceInit` runs `settings appearance --init` once and records it. `scalingInit`
and `locationInit` run `settings scaling --init` and `settings location --init` on every
deploy, like an activation script; both commands check first and skip when there is
nothing to do (location only reports when Location Services is off). Every other
key is read first (`settings ... get --json`) and set only when it differs. Root keys
run in the sudo phase; as the user they are read and reported in a note. The first set
of a scalar key records its prior value under `settingsctl.previous` (root's records for
root keys); when the key leaves config that value is set again if the key still holds
what tools wrote, and the record is forgotten when it holds the prior value already, was
changed by hand, or the prior value cannot be set with `settings` (Remote Login is never
turned off, to keep SSH). A restore that fails, or a browser change macOS asks to confirm,
is tried once and forgotten with a warning; a failed read keeps the record. Keys tools
set before it recorded prior values are left as is. Root's records wait for `sudo tools
deploy`; without sudo the user phase names them in a note.

With `settings` missing while records remain, the section fails and keeps them all, even
when the section was removed; keep `settings` installed until removal finishes.

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
from tools.state import ROOT_KEY
from tools.util import run_command

LIST_KEYS = ("fullDiskAccess", "accessibility", "loginItems")
BOOL_KEYS = ("appearanceInit", "scalingInit", "locationInit")
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
PREVIOUS = "previous"
ROOT_IDS = (*NAME_KEYS, "timeZone", *(f"sleep.{n}" for n in SLEEP_KEYS), "remoteLogin")
USER_IDS = ("defaultBrowser",)

NO_STATE = "--no-state"
SUDO_VARS = ("SUDO_USER", "SUDO_UID", "SUDO_GID", "SUDO_COMMAND")
APPEARANCE_INIT = ["appearance", "--init"]
EVERY_RUN = {"scalingInit": ["scaling", "--init"], "locationInit": ["location", "--init"]}
REVOKE_DELETES = ("accessibility",)
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


def plan_root(
    spec: Dict, runner: Runner, priors: Optional[Dict] = None, held: Optional[set] = None
) -> Tuple[List[Tuple[str, List[str]]], List[str]]:
    """([(diff row, settings args)], read errors) for declared root keys; reads only.
    `priors` gets {record id: current value} for every key a set would change, `held`
    the ids a read showed at their declared value."""
    priors = {} if priors is None else priors
    held = set() if held is None else held
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
                priors[key] = current.get(key)
                was.append(f"{key} {_fmt(current.get(key))}")
            elif key in spec:
                held.add(key)
        if args:
            cmd = ["hostname", "set", *args]
            todo.append((f"  ~ settings {_shown(cmd)} (now {', '.join(was)})", cmd))

    def zone():
        want = spec["timeZone"]
        current = runner.get("timezone", "get", want)
        if not current.get("matches"):
            cmd = ["timezone", "set", want]
            priors["timeZone"] = current.get("timeZone")
            now = _fmt(current.get("timeZone"))
            todo.append((f"  ~ settings {_shown(cmd)} (now {now})", cmd))
        else:
            held.add("timeZone")

    def sleep():
        sources = runner.get("sleep", "get").get("sources") or {}
        if not sources:
            raise RuntimeError("settings sleep get: no power sources")
        args, was = [], []
        for name, want in spec["sleep"].items():
            values = {src: (vals or {}).get(name) for src, vals in sources.items()}
            if all(v == want for v in values.values()):
                held.add(f"sleep.{name}")
                continue
            args += [f"--{name}", str(want)]
            priors[f"sleep.{name}"] = values
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
            priors["remoteLogin"] = _remote_login(current)
            mode = "on" if want else "off"
            cmd = ["remotelogin", "set", mode]
            state = f"enabled {_fmt(enabled)}, loaded {_fmt(loaded)}"
            todo.append((f"  ~ settings {_shown(cmd)} (now {state})", cmd))
        else:
            held.add("remoteLogin")

    if any(k in spec for k in NAME_KEYS):
        guarded("hostname", names)
    if "timeZone" in spec:
        guarded("timeZone", zone)
    if "sleep" in spec:
        guarded("sleep", sleep)
    if "remoteLogin" in spec:
        guarded("remoteLogin", remote_login)
    return todo, errors


def _login_items(runner: Runner) -> List[str]:
    """Current login item names; RuntimeError when they cannot be read."""
    rc, stdout, stderr = runner.call("login", "list")
    if rc != 0:
        raise RuntimeError(f"settings login list: {(stderr or stdout).strip() or rc}")
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    return [] if lines == ["No login items found."] else lines


def _read(key: str, runner: Optional[Runner]) -> Optional[Tuple[set, set]]:
    """(names granted now, names still revocable) for key; None when unreadable."""
    if not runner:
        return None
    if key == "loginItems":
        try:
            items = set(_login_items(runner))
        except RuntimeError:
            return None
        return items, items
    try:
        items = runner.get(PANE[key], "list").get("items")
    except RuntimeError:
        return None
    if not isinstance(items, list):
        return None
    rows = [i for i in items if isinstance(i, dict) and isinstance(i.get("name"), str)]
    granted = {i["name"] for i in rows if i.get("enabled")}
    return granted, ({i["name"] for i in rows} if key in REVOKE_DELETES else granted)


def _live(key: str, runner: Optional[Runner]) -> Optional[set]:
    """Names granted now (enabled pane rows, or login items); None when unreadable."""
    read = _read(key, runner)
    return None if read is None else read[0]


def _lists(section) -> Dict[str, List[str]]:
    if not isinstance(section, dict):
        return {}
    return {
        key: [n for n in names if isinstance(n, str)]
        for key, names in section.items()
        if key in LIST_KEYS and isinstance(names, list) and names
    }


def recorded(state: Optional[Dict]) -> Dict[str, List[str]]:
    """{list key: names tools granted or tried to grant} from state.json."""
    return _lists((state or {}).get("settingsctl"))


def unconfirmed(state: Optional[Dict]) -> Dict[str, List[str]]:
    """{list key: recorded names a failed step left unverified} from state.json."""
    section = (state or {}).get("settingsctl")
    return _lists(section.get("unconfirmed") if isinstance(section, dict) else None)


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
    revoke: List[str]
    grant: List[str]


def _step(key: str, sign: str, args: List[str], revoke: List[str], grant: List[str]) -> ListStep:
    return ListStep(key, f"  {sign} settings {_shown(args)}", args, revoke, grant)


def plan_lists(
    spec: Dict,
    runner: Optional[Runner],
    record: Dict[str, List[str]],
    doubtful: Optional[Dict[str, List[str]]] = None,
) -> Tuple[List[ListStep], List[str]]:
    """(applies and revokes for the list keys, read errors); reads only. Every recorded
    name is revocable; unconfirmed ones count as not granted. Live reads decide which
    targets still need an apply."""
    steps: List[ListStep] = []
    errors: List[str] = []
    for key in LIST_KEYS:
        apps = spec.get(key) or []
        had = set(record.get(key) or [])
        granted = had - set((doubtful or {}).get(key) or [])
        dropped = sorted(had - set(_names(key, apps)))
        if key == "loginItems":
            if not apps and not dropped:
                continue
            current = None
            if runner:
                try:
                    current = set(_login_items(runner))
                except RuntimeError as e:
                    errors.append(f"loginItems: {e}")
            known = granted if current is None else current
            for app in apps:
                name = _name(key, app)
                if name not in known:
                    steps.append(_step(key, "+", ["login", "add", app], [], [name]))
            for name in dropped:
                if current is None or name in current:
                    steps.append(_step(key, "-", ["login", "remove", name], [name], []))
            continue
        pending = [a for a in apps if _name(key, a) not in granted]
        if pending:
            current = _live(key, runner)
            if current is not None:
                pending = [a for a in apps if _name(key, a) not in current]
        if pending:
            args = [*ENABLE[key], ",".join(pending)]
            steps.append(_step(key, "+", args, [], _names(key, pending)))
        if dropped:
            args = [PANE[key], "revoke", ",".join(dropped)]
            steps.append(_step(key, "-", args, dropped, []))
    return steps, errors


def plan_user(
    spec: Dict,
    runner: Optional[Runner],
    record: Optional[Dict[str, List[str]]] = None,
    inited: bool = False,
    doubtful: Optional[Dict[str, List[str]]] = None,
) -> Tuple[List[Tuple[str, List[str]]], List[str]]:
    """([(diff row, settings args)], read errors) for user keys and recorded lists."""
    steps, list_errors = plan_lists(spec, runner, record or {}, doubtful)
    todo, errors = plan_other(spec, runner, inited)
    return [(step.row, step.args) for step in steps] + todo, list_errors + errors


def plan_other(
    spec: Dict,
    runner: Optional[Runner],
    inited: bool = False,
    priors: Optional[Dict] = None,
    held: Optional[set] = None,
) -> Tuple[List[Tuple[str, List[str]]], List[str]]:
    """([(diff row, settings args)], read errors) for the user keys that are not lists."""
    priors = {} if priors is None else priors
    held = set() if held is None else held
    todo: List[Tuple[str, List[str]]] = []
    errors: List[str] = []
    if spec.get("appearanceInit") and not inited:
        todo.append((f"  ~ settings {_shown(APPEARANCE_INIT)}", APPEARANCE_INIT))
    for key, cmd in EVERY_RUN.items():
        if spec.get(key):
            todo.append((f"  ~ settings {_shown(cmd)}", cmd))
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
                priors["defaultBrowser"] = current.get("defaultBrowser")
                now = _fmt(current.get("defaultBrowser"))
                todo.append((f"  ~ settings {_shown(cmd)} (now {now})", cmd))
            else:
                held.add("defaultBrowser")
        except RuntimeError as e:
            errors.append(f"defaultBrowser: {e}")
    return todo, errors


# --------------------------------------------------------------------------
# Prior values of scalar keys
# --------------------------------------------------------------------------


def _remote_login(current: Dict) -> Optional[bool]:
    """On, off, or None when sshd is only half on (enabled xor loaded)."""
    enabled, loaded = bool(current.get("enabled")), bool(current.get("loaded"))
    return enabled if enabled == loaded else None


def previous(state: Optional[Dict]) -> Dict[str, Dict]:
    """{record id: {"prior", "written"}} from state.json."""
    section = (state or {}).get("settingsctl")
    records = section.get(PREVIOUS) if isinstance(section, dict) else None
    if not isinstance(records, dict):
        return {}
    return {k: dict(v) for k, v in records.items() if isinstance(v, dict) and "prior" in v}


def _store_previous(state: Optional[Dict], records: Dict[str, Dict]) -> None:
    if state is None:
        return
    section = state.get("settingsctl")
    section = section if isinstance(section, dict) else {}
    if records:
        section[PREVIOUS] = records
    else:
        section.pop(PREVIOUS, None)
    if section:
        state["settingsctl"] = section
    else:
        state.pop("settingsctl", None)


def _wanted(spec: Dict, ids) -> Dict[str, object]:
    """{record id: declared value} for the ids in spec."""
    out = {}
    for rid in ids:
        key, _, sub = rid.partition(".")
        if sub:
            if sub in (spec.get(key) or {}):
                out[rid] = spec[key][sub]
        elif key in spec:
            out[rid] = spec[key]
    return out


def _remember(records: Dict[str, Dict], wanted: Dict[str, object], priors: Dict, held: set) -> None:
    """Record the prior value of ids first set now. Recorded ids a read showed at their
    declared value take it as `written`; ids about to be set keep theirs until the set
    succeeds (`_apply_sets`), and ids whose read failed keep theirs."""
    for rid, cur in priors.items():
        if rid not in records:
            records[rid] = {"prior": cur}
    for rid in held:
        if rid in records and rid in wanted:
            records[rid]["written"] = wanted[rid]


SET_IDS = {
    "hostname": tuple(NAME_KEYS),
    "timezone": ("timeZone",),
    "sleep": tuple(f"sleep.{n}" for n in SLEEP_KEYS),
    "remotelogin": ("remoteLogin",),
    "defaultbrowser": ("defaultBrowser",),
}


def _apply_sets(
    runner: Runner,
    todo: List[Tuple[str, List[str]]],
    records: Dict[str, Dict],
    wanted: Dict[str, object],
    priors: Dict,
) -> bool:
    """Apply each set; a successful one records its declared values as `written`. A failed
    one may have applied some values (`hostname set`, `sleep set`), so those a read now
    shows at their declared value are recorded as `written` too."""
    success = True
    for row, args in todo:
        ok = _apply(runner, [(row, args)])
        success = ok and success
        for rid in SET_IDS.get(args[0], ()):
            if rid not in priors or rid not in records:
                continue
            if not ok:
                try:
                    if not _holds(rid, wanted[rid], runner):
                        continue
                except (RuntimeError, UnknownValue):
                    continue
            records[rid]["written"] = wanted[rid]
    return success


def _unrestorable(rid: str, prior, runner: Runner) -> Optional[str]:
    """Why `settings` cannot set prior back, or None."""
    key, _, sub = rid.partition(".")
    if sub:
        seen = set(prior.values()) if isinstance(prior, dict) and prior else {None}
        if len(seen) != 1:
            return "it differed per power source and settings sets all of them at once"
        value = next(iter(seen))
        if value is None:
            return "it was not set"
        return _sleep_error(value)
    if prior is None:
        return {
            "remoteLogin": "sshd was half on",
            "timeZone": "it was not a zoneinfo zone",
        }.get(key, "it was not set and settings cannot unset it")
    if key == "remoteLogin" and not prior:
        return (
            "turning Remote Login off could cut off SSH; turn it off by hand with"
            " `sudo settings remotelogin set off`"
        )
    if key == "defaultBrowser":
        browsers = runner.get("defaultbrowser", "get").get("browsers") or []
        if prior not in browsers:
            return "it is no longer installed"
    return None


def _restore_args(rid: str, prior) -> List[str]:
    key, _, sub = rid.partition(".")
    if sub:
        return ["sleep", "set", f"--{sub}", str(next(iter(prior.values())))]
    if key in NAME_KEYS:
        return ["hostname", "set", NAME_KEYS[key], prior]
    if key == "remoteLogin":
        return ["remotelogin", "set", "on" if prior else "off"]
    return [{"timeZone": "timezone", "defaultBrowser": "defaultbrowser"}[key], "set", prior]


class UnknownValue(Exception):
    """A recorded value settings rejects (a zone it no longer knows)."""


def _holds(rid: str, value, runner: Runner) -> bool:
    """The live setting for rid equals value (a recorded prior or written value). A failed
    read raises RuntimeError; a value settings rejects raises UnknownValue."""
    key, _, sub = rid.partition(".")
    if sub:
        sources = runner.get("sleep", "get").get("sources") or {}
        if not sources:
            raise RuntimeError("settings sleep get: no power sources")
        now = {src: (vals or {}).get(sub) for src, vals in sources.items()}
        if isinstance(value, dict):
            return now == value
        return bool(now) and all(v == value for v in now.values())
    if key in NAME_KEYS:
        return runner.get("hostname", "get").get(key) == value
    if key == "timeZone":
        if not value:
            return runner.get("timezone", "get").get("timeZone") is None
        try:
            return bool(runner.get("timezone", "get", value).get("matches"))
        except RuntimeError:
            runner.get("timezone", "get")
            raise UnknownValue(f"settings no longer knows zone {value!r}") from None
    if key == "remoteLogin":
        return _remote_login(runner.get("remotelogin", "get")) == value
    return runner.get("defaultbrowser", "get").get("defaultBrowser") == value


def plan_restore(
    spec: Dict, runner: Runner, records: Dict[str, Dict], ids
) -> Tuple[List[Tuple[str, List[str], str]], List[Tuple[str, str]], List[str]]:
    """([(diff row, settings args, record id)] restores, [(record id, reason)] to forget,
    read errors) for recorded ids among `ids` that left config; reads only."""
    restores: List[Tuple[str, List[str], str]] = []
    forget: List[Tuple[str, str]] = []
    errors: List[str] = []
    declared = _wanted(spec, ids)
    for rid in [r for r in ids if r in records and r not in declared]:
        entry = records[rid]
        prior = entry.get("prior")
        try:
            written = "written" in entry and _holds(rid, entry["written"], runner)
            if _holds(rid, prior, runner):
                forget.append((rid, "already gone"))
                continue
            if not written:
                forget.append((rid, "changed by hand; left as is"))
                continue
            why = _unrestorable(rid, prior, runner)
        except UnknownValue as e:
            forget.append((rid, f"{e}; left as is"))
            continue
        except RuntimeError as e:
            # A failed read keeps the record for the next run.
            errors.append(f"{rid}: {e}")
            continue
        if why:
            forget.append((rid, f"cannot restore {_fmt_prior(prior)}: {why}; left as is"))
            continue
        args = _restore_args(rid, prior)
        restores.append((f"  - settings {_shown(args)} (restore {rid})", args, rid))
    return restores, forget, errors


def _fmt_prior(prior) -> str:
    if isinstance(prior, dict):
        return ", ".join(f"{src} {_fmt(v)}" for src, v in prior.items()) or "(not set)"
    return _fmt(prior)


def _forget_rows(forget: List[Tuple[str, str]]) -> List[str]:
    return [f"  ~ forget {rid} ({why})" for rid, why in forget if why != "already gone"]


def _settle(
    runner: Runner,
    records: Dict[str, Dict],
    restores: List[Tuple[str, List[str], str]],
    forget: List[Tuple[str, str]],
) -> bool:
    """Forget and restore. A failed restore fails this run and is forgotten with a
    warning, so a prior value settings cannot set again never fails every deploy."""
    for rid, why in forget:
        if why != "already gone":
            log(f"settingsctl: releasing {rid}: {why}", Color.YELLOW)
        records.pop(rid, None)
    success = True
    for row, args, rid in restores:
        prior = records.get(rid, {}).get("prior")
        if _apply(runner, [(row, args)]):
            records.pop(rid, None)
            log(f"Restored: {rid} = {_fmt_prior(prior)}", Color.YELLOW)
            if rid == "defaultBrowser":
                log(
                    "settingsctl: macOS asks to confirm the browser change; if declined,"
                    " the browser stays as is and is no longer managed"
                )
        else:
            log(f"settingsctl: releasing {rid}: restore failed; left as is", Color.YELLOW)
            records.pop(rid, None)
            success = False
    return success


def _needs_cli_as_user(
    spec: Dict, record: Dict[str, List[str]], prev: Optional[Dict] = None
) -> bool:
    others = any(k in spec for k in USER_KEYS if k not in LIST_KEYS)
    return others or bool(record) or bool(prev) or any(spec.get(k) for k in LIST_KEYS)


# --------------------------------------------------------------------------
# Section entry points
# --------------------------------------------------------------------------


def has_work(raw, state: Optional[Dict] = None) -> bool:
    return (
        raw is not None
        or bool(recorded(state))
        or _inited(state)
        or bool(previous(state))
        or bool(_root_previous(state))
    )


def _root_previous(state: Optional[Dict]) -> Dict[str, Dict]:
    """Root's prior-value records, read from the user's state (never root's own)."""
    root = (state or {}).get(ROOT_KEY)
    return previous(root if isinstance(root, dict) else None)


def _hint_root_restores(spec: Dict, state: Optional[Dict]) -> None:
    """As the user without sudo: say when host keys that left config wait in root's
    records, since only `sudo tools deploy` restores or forgets them."""
    if privilege.UNDER_SUDO:
        return
    declared = _wanted(spec, ROOT_IDS)
    waiting = [rid for rid in _root_previous(state) if rid in ROOT_IDS and rid not in declared]
    if waiting:
        msg = f"{len(waiting)} removed system setting(s) wait in root's records"
        log(f"settingsctl: {msg} ({', '.join(waiting)}); {ROOT_HINT}", Color.YELLOW)


def has_root_work(raw, state: Optional[Dict] = None) -> bool:
    """Root keys declared or recorded (root's records in state), or a config that fails to
    parse (so the sudo phase stops)."""
    if previous(state):
        return True
    if raw is None:
        return False
    spec, errors = parse(raw)
    return bool(errors) or any(k in spec for k in ROOT_KEYS)


KEEP_CLI = "keep `settings` installed until its records are revoked or restored"


def _has_records(state: Optional[Dict]) -> bool:
    """Records that need `settings` to revoke or restore (appearanceInit needs none)."""
    return bool(recorded(state) or previous(state))


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
    prev = previous(state)
    if not runner and _has_records(state):
        return [f"  ! `settings` not found (looked in {looked}); {KEEP_CLI}"]
    if root:
        if not has_root and not prev:
            return []
        if not runner:
            return [f"  ! `settings` not found (looked in {looked})"]
        todo, read_errors = plan_root(spec, runner)
        restores, forget, restore_errors = plan_restore(spec, runner, prev, ROOT_IDS)
        rows = [f"  ? {e}" for e in read_errors + restore_errors] + [row for row, _ in todo]
        return rows + [row for row, _, _ in restores] + _forget_rows(forget)
    rows: List[str] = []
    record = recorded(state)
    if _needs_cli_as_user(spec, record, prev) and not runner:
        rows.append(f"  ? `settings` not found (looked in {looked})")
    todo, read_errors = plan_user(spec, runner, record, _init_done(state), unconfirmed(state))
    rows += [f"  ? {e}" for e in read_errors] + [row for row, _ in todo]
    if runner and prev:
        restores, forget, restore_errors = plan_restore(spec, runner, prev, USER_IDS)
        rows += [f"  ? {e}" for e in restore_errors] + [row for row, _, _ in restores]
        rows += _forget_rows(forget)
    _hint_root_restores(spec, state)
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
    doubtful: Dict[str, List[str]],
    results: List[Tuple[ListStep, bool]],
    runner: Runner,
) -> Tuple[List[str], List[str]]:
    """(names tools own for key after this run, those not confirmed granted). Owned are
    the targets plus dropped names whose revoke failed and the live list still shows
    (any Accessibility row, since its revoke deletes rows), or all of them when it
    cannot be read. After a failed step the targets the live list lacks are unconfirmed;
    when it cannot be read, the targets of failed applies and earlier unconfirmed ones
    no apply confirmed."""
    target = set(_names(key, spec.get(key) or []))
    steps = [(step, ok) for step, ok in results if step.key == key]
    if all(ok for _step, ok in steps):
        return sorted(target), []
    unrevoked = {name for step, ok in steps if not ok for name in step.revoke}
    read = _read(key, runner)
    if read is not None:
        live, present = read
        return sorted(target | (unrevoked & present)), sorted(target - live)
    owned = target | unrevoked
    failed = {name for step, ok in steps if not ok for name in step.grant}
    applied = {name for step, ok in steps if ok for name in step.grant}
    earlier = set(doubtful.get(key) or []) - applied
    return sorted(owned), sorted((failed | earlier) & target)


def _store(
    state: Optional[Dict],
    lists: Optional[Dict[str, Tuple[List[str], List[str]]]],
    inited: bool,
) -> None:
    """Write the `settingsctl` record; lists None keeps the recorded lists."""
    if state is None:
        return
    section = state.get("settingsctl")
    section = section if isinstance(section, dict) else {}
    if lists is not None:
        doubtful = {}
        for key in LIST_KEYS:
            owned, unsure = lists.get(key) or ([], [])
            if owned:
                section[key] = owned
            else:
                section.pop(key, None)
            if unsure:
                doubtful[key] = unsure
        if doubtful:
            section["unconfirmed"] = doubtful
        else:
            section.pop("unconfirmed", None)
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
    prev = previous(state)
    if root and not has_root and not prev:
        return True
    record = {} if root else recorded(state)
    inited = bool(spec.get("appearanceInit")) and _init_done(state)
    if not root:
        if has_root and not privilege.UNDER_SUDO:
            count = sum(1 for k in ROOT_KEYS if k in spec)
            msg = f"{count} system setting(s) not managed as user; {ROOT_HINT}"
            log(f"settingsctl: {msg}", Color.YELLOW)
        _hint_root_restores(spec, state)
        if not _needs_cli_as_user(spec, record, prev):
            _store(state, None, inited)
            return True
    cli, looked = find_cli(spec, paths, root, which, executable)
    if not cli:
        hint = f"; {KEEP_CLI}" if _has_records(state) else ""
        log(f"settingsctl: `settings` not found (looked in {looked}){hint}", Color.RED)
        return False
    runner = Runner(cli, run or run_command, _env(root))
    if root:
        priors: Dict = {}
        held: set = set()
        todo, read_errors = plan_root(spec, runner, priors, held)
        restores, forget, restore_errors = plan_restore(spec, runner, prev, ROOT_IDS)
        read_errors += restore_errors
        for err in read_errors:
            log(f"settingsctl: {err}", Color.RED)
        wanted = _wanted(spec, ROOT_IDS)
        _remember(prev, wanted, priors, held)
        _store_previous(state, prev)
        success = _apply_sets(runner, todo, prev, wanted, priors)
        success = _settle(runner, prev, restores, forget) and success
        _store_previous(state, prev)
        return success and not read_errors
    doubtful = unconfirmed(state)
    plan, list_errors = plan_lists(spec, runner, record, doubtful)
    priors = {}
    held = set()
    todo, other_errors = plan_other(spec, runner, inited, priors, held)
    restores, forget, restore_errors = plan_restore(spec, runner, prev, USER_IDS)
    read_errors = list_errors + other_errors + restore_errors
    wanted = _wanted(spec, USER_IDS)
    _remember(prev, wanted, priors, held)
    _store_previous(state, prev)
    for err in read_errors:
        log(f"settingsctl: {err}", Color.RED)
    results = [(step, _apply(runner, [(step.row, step.args)])) for step in plan]
    success = all(ok for _step, ok in results)
    for row, args in todo:
        ok = _apply(runner, [(row, args)])
        success = ok and success
        if args == APPEARANCE_INIT and ok:
            inited = True
        if ok and args[0] == "defaultbrowser" and "defaultBrowser" in prev:
            prev["defaultBrowser"]["written"] = wanted["defaultBrowser"]
    success = _settle(runner, prev, restores, forget) and success
    if state is not None:
        lists = {key: _still_recorded(key, spec, doubtful, results, runner) for key in LIST_KEYS}
        _store(state, lists, inited)
        _store_previous(state, prev)
    return success and not read_errors
