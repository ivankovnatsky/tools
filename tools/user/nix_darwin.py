"""Take over a Mac from nix-darwin: opt-in removal of what it and home-manager left.

Config shape::

    nixDarwin:
      takeOver: true   # default false: nothing is read or changed

Under sudo: daemons in /Library/LaunchDaemons that run from /nix/store are
booted out and moved to the Trash (org.nixos.activate-system would re-activate
nix-darwin at boot). Each /etc symlink into /etc/static is replaced in one step
by its ``<path>.before-nix-darwin`` (file or symlink, saved by nix-darwin's
first activation), or moved to the Trash when there is none; then /etc/static
itself. As your user: agents in ~/Library/LaunchAgents that run from
/nix/store, so launchd jobs with the same labels can be created. Jobs tools
tracks in state are never touched; Determinate Nix's own daemons don't run
from /nix/store and are left alone.

Runs before `files` (root) and `launchd` (user). Nothing is recorded in state:
once nothing is left, the section is a no-op. Darwin only.
"""

import os
import plistlib
import sys
from typing import Dict, Iterable, List, Optional, Set, Tuple

from tools import fsutil
from tools.log import Color, log
from tools.util import run_command

KEYS = ("takeOver",)
ETC = "/etc"
STATIC = "/etc/static"
BEFORE = ".before-nix-darwin"
STORE = "/nix/store/"
LAUNCHCTL = "/bin/launchctl"
SHELLS = ("/bin/sh", "/bin/bash", "/bin/zsh")
WAIT_STORE = "/bin/wait4path /nix/store"
# nix-darwin's own Nix daemon (nix.enable): booting it out would stop Nix itself.
KEEP = frozenset({"org.nixos.nix-daemon"})
DAEMON_DIR = "/Library/LaunchDaemons"
AGENT_DIR = "~/Library/LaunchAgents"
# launchctl bootout: ESRCH (3) and 113 mean the job was not loaded.
_NOT_LOADED = (3, 113)

# (kind, path, detail): "job" (detail: label), "restore" (detail: backup), "remove"
Action = Tuple[str, str, Optional[str]]


def parse(raw) -> Tuple[bool, List[str]]:
    """(takeOver, errors)."""
    if raw is None:
        return False, []
    if not isinstance(raw, dict):
        return False, [f"nixDarwin must be a mapping, got {type(raw).__name__}"]
    unknown = sorted(set(raw) - set(KEYS), key=str)
    if unknown:
        return False, [f"nixDarwin: unknown keys {unknown}"]
    take = raw.get("takeOver", False)
    if not isinstance(take, bool):
        return False, [f"nixDarwin.takeOver must be a bool, got {type(take).__name__}"]
    return take, []


def has_work(raw) -> bool:
    return raw is not None


def _static_target(target: str, static: str) -> bool:
    return target == static or target.startswith(static + "/")


def etc_links(etc: str = ETC, static: str = STATIC) -> List[str]:
    """Symlinks under ``etc`` (not /etc/static itself) that point into ``static``."""
    real = os.path.realpath(etc)  # /etc is a symlink to /private/etc
    skip = os.path.join(real, "static")
    found = []
    for dirpath, dirnames, filenames in os.walk(real):
        # os.walk lists symlinks to directories in dirnames and does not follow them.
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            if path == skip or not os.path.islink(path):
                continue
            try:
                target = os.readlink(path)
            except OSError:
                continue
            if _static_target(target, static):
                found.append(etc + path[len(real) :])
    return sorted(found)


def _runs_from_store(plist: Dict) -> bool:
    """The job's executable is in /nix/store, or it is nix-darwin's shell wrapper
    (`/bin/sh -c "/bin/wait4path /nix/store && exec ..."`). A store path that is
    only a data argument does not count."""
    args = plist.get("ProgramArguments")
    args = args if isinstance(args, list) else []
    program = plist.get("Program") or (args[0] if args else None)
    if isinstance(program, str) and program.startswith(STORE):
        return True
    return (
        program in SHELLS
        and len(args) >= 3
        and args[1] == "-c"
        and isinstance(args[2], str)
        and args[2].lstrip().startswith(WAIT_STORE)
    )


def nix_jobs(directory: str, owned: Iterable[str] = ()) -> List[Tuple[str, str]]:
    """(plist path, label) of jobs in ``directory`` that run from /nix/store."""
    owned = set(owned)
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    jobs = []
    for name in names:
        if not name.endswith(".plist"):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, "rb") as f:
                plist = plistlib.load(f)
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
        label = plist.get("Label") if isinstance(plist, dict) else None
        if (
            isinstance(label, str)
            and label not in owned
            and label not in KEEP
            and _runs_from_store(plist)
        ):
            jobs.append((path, label))
    return jobs


def _owned(state: Optional[Dict]) -> Set[str]:
    services = ((state or {}).get("launchd") or {}).get("services") or {}
    return set(services) if isinstance(services, dict) else set()


def plan(
    root: bool,
    state: Optional[Dict] = None,
    etc: str = ETC,
    static: str = STATIC,
    daemons: str = DAEMON_DIR,
    agents: str = AGENT_DIR,
) -> List[Action]:
    jobs_dir = daemons if root else os.path.expanduser(agents)
    actions: List[Action] = [("job", p, label) for p, label in nix_jobs(jobs_dir, _owned(state))]
    if not root:
        return actions
    for link in etc_links(etc, static):
        backup = link + BEFORE
        if os.path.islink(backup) or os.path.isfile(backup):
            actions.append(("restore", link, backup))
        else:
            actions.append(("remove", link, None))
    static_link = os.path.join(etc, "static")
    if os.path.islink(static_link) and os.readlink(static_link).startswith(STORE):
        actions.append(("remove", static_link, None))
    return actions


def row(action: Action) -> str:
    kind, path, detail = action
    if kind == "job":
        return f"  - {detail}: boot out, move {path} to Trash (runs from /nix/store)"
    if kind == "restore":
        return f"  ~ {path}: replace link into /etc/static with {detail}"
    return f"  - {path}: nix-darwin link, move to Trash"


def _target(label: str, root: bool) -> str:
    return f"system/{label}" if root else f"gui/{os.getuid()}/{label}"


def _do(action: Action, root: bool) -> Optional[str]:
    """None on success, else why it failed."""
    kind, path, detail = action
    try:
        if kind == "job":
            rc, out, err = run_command([LAUNCHCTL, "bootout", _target(detail, root)])
            if rc != 0 and rc not in _NOT_LOADED:
                return f"bootout {detail} failed ({rc}): {(err or out).strip()}"
            fsutil.trash(path)
        elif kind == "restore":
            # Replaces the link in one step: on failure nothing has changed.
            os.rename(detail, path)
        else:
            fsutil.trash(path)
    except OSError as e:
        return f"{path}: {e}"
    return None


def diff_nix_darwin(raw, state: Optional[Dict] = None, root: Optional[bool] = None) -> List[str]:
    if sys.platform != "darwin":
        return []
    take, errors = parse(raw)
    if errors:
        return [f"  ! {e}" for e in errors]
    if not take:
        return []
    root = os.geteuid() == 0 if root is None else root
    return [row(a) for a in plan(root, state)]


def install_nix_darwin(raw, state: Optional[Dict] = None, root: Optional[bool] = None) -> bool:
    if sys.platform != "darwin":
        return True
    take, errors = parse(raw)
    for e in errors:
        log(e, Color.RED)
    if errors or not take:
        return not errors
    root = os.geteuid() == 0 if root is None else root
    ok = True
    for action in plan(root, state):
        kind, path, _ = action
        if not ok and kind == "remove" and os.path.basename(path) == "static":
            # A link left in place still points into /etc/static: keep it working.
            log(f"nixDarwin: keeping {path}: earlier steps failed", Color.YELLOW)
            continue
        err = _do(action, root)
        if err:
            log(f"nixDarwin: {err}", Color.RED)
            ok = False
        else:
            log(f"nixDarwin: {row(action).strip()[2:]}", Color.GREEN)
    return ok
