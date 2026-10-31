"""Shared UI automation for Privacy & Security app lists (macOS only).

Accessibility and Full Disk Access share the same pane layout: an outline of
app rows with checkboxes, and +/- buttons below it. Everything here goes
through osascript and the System Settings UI.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PANE_GROUP = (
    "group 1 of scroll area 1 of group 1 of group 3 of splitter group 1 "
    "of group 1 of window 1"
)
PANE_OUTLINE = f"outline 1 of scroll area 1 of {PANE_GROUP}"
PANE_READY_POLLS = 60  # x 0.5s

# On current macOS the file picker runs in its own process, not as a sheet.
OPEN_PANEL_PROCESS = "com.apple.appkit.xpc.openAndSavePanelService"


def _app_dirs() -> list[Path]:
    home = Path.home()
    return [
        Path("/Applications"),
        Path("/System/Applications"),
        Path("/System/Applications/Utilities"),
        home / "Applications",
        home / "Applications" / "Home Manager Apps",
    ]


def parse_app_specs(spec: str) -> dict[str, str | None]:
    """Parse "Name" or "Name=/path" entries into {name: path or None}."""
    apps: dict[str, str | None] = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        name, _, path = item.partition("=")
        apps[name.strip()] = path.strip() or None
    return apps


def resolve_app_path(name: str, path: str | None) -> str | None:
    """Return an existing path for the app: the given one, else NAME.app in the app dirs."""
    candidates = [Path(path)] if path else [d / f"{name}.app" for d in _app_dirs()]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def quit_settings() -> None:
    subprocess.run(
        ["osascript", "-e", 'tell application "System Settings" to quit'],
        capture_output=True,
    )


def run_in_pane(pane_url: str, body: str, timeout: int = 30) -> str | None:
    """Open the pane, run body inside `tell process "System Settings"`, quit.

    Returns osascript stdout, or None if the script failed or timed out.
    """
    # The app list is populated asynchronously after the pane opens; a fixed
    # delay is not enough under load (e.g. during nix-darwin activation), so
    # wait for the outline to exist before touching it.
    script = f"""
tell application "System Settings" to quit
delay 0.5
do shell script "open '{pane_url}'"
tell application "System Events"
    tell process "System Settings"
        set frontmost to true
        repeat {PANE_READY_POLLS} times
            if exists ({PANE_OUTLINE}) then exit repeat
            delay 0.5
        end repeat
        if not (exists ({PANE_OUTLINE})) then error "app list did not appear"
        delay 0.5
{body}
    end tell
end tell
"""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print("  timed out driving System Settings", file=sys.stderr)
        return None
    finally:
        quit_settings()
    if result.returncode != 0:
        if result.stderr:
            print(f"  {result.stderr.strip()}", file=sys.stderr)
        return None
    return result.stdout


def _for_each_checkbox(inner: str) -> str:
    """AppleScript looping over every app checkbox `el` (and its row `r`).

    Each row holds one UI element wrapping the app's checkbox; `entire contents`
    of a row cannot be iterated on macOS 27 (-1700), so address it directly.
    """
    return f"""
        set theOutline to {PANE_OUTLINE}
        repeat with r in (every row of theOutline)
            repeat with el in (every checkbox of UI element 1 of r)
{inner}
            end repeat
        end repeat"""


def _parse_pairs(output: str | None) -> dict[str, str] | None:
    if output is None:
        return None
    pairs: dict[str, str] = {}
    for line in output.strip().splitlines():
        if ":" in line:
            name, value = line.rsplit(":", 1)
            pairs[name.strip()] = value.strip()
    return pairs


def list_apps(pane_url: str) -> list[dict] | None:
    """List {name, enabled} for every app in the pane, or None on UI failure."""
    body = (
        '        set output to ""'
        + _for_each_checkbox(
            '                    set output to output & (name of el) & ":" & (value of el) & linefeed'
        )
        + "\n        return output"
    )
    pairs = _parse_pairs(run_in_pane(pane_url, body))
    if pairs is None:
        return None
    return [{"name": n, "enabled": v == "1"} for n, v in pairs.items()]


def toggle_app(pane_url: str, name: str) -> bool | None:
    """Toggle one app's checkbox. Returns the new state, or None if not found."""
    body = (
        _for_each_checkbox(f'''                    if name of el is "{name}" then
                        click el
                        delay 0.5
                        return value of el as string
                    end if''')
        + '\n        return "not found"'
    )
    out = (run_in_pane(pane_url, body) or "").strip()
    return out == "1" if out in ("0", "1") else None


def remove_app(pane_url: str, name: str) -> bool:
    """Select an app's row and click the remove (-) button."""
    body = (
        _for_each_checkbox(f'''                    if name of el is "{name}" then
                        select r
                        delay 0.3
                        repeat with btn in (every button of {PANE_GROUP})
                            try
                                click btn
                                delay 0.5
                                return "removed"
                            end try
                        end repeat
                    end if''')
        + '\n        return "not found"'
    )
    return "removed" in (run_in_pane(pane_url, body) or "")


def enable_listed(pane_url: str, apps: list[str]) -> dict[str, str] | None:
    """Enable listed apps in one session.

    Returns {name: "enabled" | "already enabled"} for apps found in the pane,
    or None if the UI could not be read.
    """
    if not apps:
        return {}
    checks = " or ".join(f'name of el is "{app}"' for app in apps)
    body = (
        '        set output to ""'
        + _for_each_checkbox(f"""                    if {checks} then
                        set appName to name of el
                        if value of el is 0 then
                            click el
                            -- Wait for the password sheet to appear and be dismissed
                            delay 2
                            repeat 120 times
                                delay 1
                                if (count of sheets of window 1) is 0 then exit repeat
                            end repeat
                            delay 0.5
                            set output to output & appName & ":enabled" & linefeed
                        else
                            set output to output & appName & ":already enabled" & linefeed
                        end if
                    end if""")
        + "\n        return output"
    )
    return _parse_pairs(run_in_pane(pane_url, body, timeout=300))


def add_apps(pane_url: str, paths: list[str]) -> None:
    """Add apps in one session: per path click +, authenticate, pick it.

    The picker is driven with keystrokes (Go to Folder, then Return to open),
    since it lives in a separate process. Callers verify the result by
    re-reading the pane.
    """
    if not paths:
        return
    picks = "\n".join(
        f"""        click button 1 of {PANE_GROUP}
        -- Wait for authentication, then for the file picker to take focus
        set opened to false
        repeat 120 times
            delay 1
            if exists (first process whose frontmost is true and name is "{OPEN_PANEL_PROCESS}") then
                set opened to true
                exit repeat
            end if
        end repeat
        if not opened then return "no picker"
        delay 0.5
        keystroke "g" using {{command down, shift down}}
        delay 1
        keystroke "{path}"
        delay 0.5
        keystroke return
        delay 1
        keystroke return
        delay 2
        set frontmost to true"""
        for path in paths
    )
    out = run_in_pane(
        pane_url, picks + '\n        return "done"', timeout=150 * len(paths)
    )
    if out is not None and "no picker" in out:
        print("  file picker did not open (authentication cancelled?)", file=sys.stderr)


def ensure_enabled(
    pane_url: str, label: str, apps: dict[str, str | None], add_missing: bool = False
) -> bool:
    """Enable apps; with add_missing, first add unlisted ones by path.

    Returns True when the pane could be read. Apps that are not listed are
    reported, not retried: adding needs a person to authenticate, so it only
    runs when explicitly requested (add_missing), never from activation.
    """
    names = list(apps)
    found = enable_listed(pane_url, names)
    if found is None:
        print(f"Skipping {label} (could not read UI)", file=sys.stderr)
        return False

    missing = [name for name in names if name not in found]
    if missing and add_missing:
        to_add: dict[str, str] = {}
        for name in missing:
            path = resolve_app_path(name, apps[name])
            if path:
                to_add[name] = path
            else:
                print(f"  {name}: no app found to add (pass NAME=/path)")
        add_apps(pane_url, list(to_add.values()))
        again = enable_listed(pane_url, list(to_add)) or {}
        for name, path in to_add.items():
            if name in again:
                found[name] = f"added from {path}, {again[name]}"
            else:
                print(
                    f"  {name}: not listed after adding {path}; check the {label} "
                    "pane (it may show under another name)",
                    file=sys.stderr,
                )
        missing = [name for name in names if name not in found]

    for name in names:
        if name in found:
            print(f"  {name}: {found[name]}")
    for name in missing:
        hint = "" if add_missing else " (run with --add-missing to add it)"
        print(f"  {name}: not in {label} list{hint}")
    return True
