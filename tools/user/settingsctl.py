"""macOS privacy permissions and login items through settingsctl (`settings` on PATH).

Config shape::

    settingsctl:
      fullDiskAccess: [kitty, Terminal]          # settings fulldiskaccess --enable
      accessibility: [Hammerspoon]               # settings accessibility set
      loginItems: [Hammerspoon, LosslessSwitcher]  # settings login set

settingsctl drives System Settings through osascript, so this runs as the user.
"""

import shutil
import sys
from typing import Dict, List, Optional, Tuple

from tools.log import Color, log
from tools.util import run_command

KEYS = ("fullDiskAccess", "accessibility", "loginItems")


def _command(key: str, apps: List[str]) -> List[str]:
    joined = ",".join(apps)
    if key == "fullDiskAccess":
        return ["fulldiskaccess", "--enable", joined]
    if key == "accessibility":
        return ["accessibility", "set", joined]
    return ["login", "set", joined]


def parse(raw) -> Tuple[Dict[str, List[str]], List[str]]:
    """Validate `settingsctl`. Returns ({key: apps} for declared keys, errors)."""
    if raw is None:
        return {}, []
    if not isinstance(raw, dict):
        return {}, [f"settingsctl must be a mapping, got {type(raw).__name__}"]
    errors = []
    unknown = set(raw) - set(KEYS)
    if unknown:
        errors.append(f"unknown keys {sorted(unknown)} (expected {sorted(KEYS)})")
    spec = {}
    for key in KEYS:
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
    return ({} if errors else spec), errors


def diff_settingsctl(raw) -> List[str]:
    if sys.platform != "darwin":
        return []
    spec, errors = parse(raw)
    changes = [f"  ! {err}" for err in errors]
    for key, apps in spec.items():
        changes.append(f"  ~ settings {' '.join(_command(key, apps)[:-1])} {','.join(apps)!r}")
    return changes


def install_settingsctl(raw, which=shutil.which) -> bool:
    if sys.platform != "darwin":
        log("settingsctl: skipping, not macOS", Color.YELLOW)
        return True
    spec, errors = parse(raw)
    for err in errors:
        log(f"settingsctl: {err}", Color.RED)
    if errors:
        return False
    if not spec:
        return True
    cli: Optional[str] = which("settings")
    if not cli:
        log("settingsctl: `settings` not found on PATH, skipping", Color.RED)
        return False
    success = True
    for key, apps in spec.items():
        rc, stdout, stderr = run_command([cli, *_command(key, apps)])
        for line in stdout.splitlines():
            log(f"  {line}")
        for line in stderr.splitlines():
            log(f"  {line}", Color.YELLOW)
        if rc != 0:
            log(f"settingsctl: {key} failed (exit {rc})", Color.RED)
            success = False
    return success
