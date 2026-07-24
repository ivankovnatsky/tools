import os
import shutil
import sys

from tools.log import Color, debug, log
from tools.system_paths import system_bin, system_bin_optional
from tools.util import run_command

_BREW_INSTALL_URL = "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"


def _normalize_tap(tap: str) -> str:
    """Normalize tap name: user/homebrew-foo -> user/foo."""
    parts = tap.split("/", 1)
    if len(parts) == 2 and parts[1].startswith("homebrew-"):
        return f"{parts[0]}/{parts[1][len('homebrew-') :]}"
    return tap


def _brew_bin() -> str | None:
    """Return the brew binary path, checking system_paths first then PATH."""
    path = system_bin_optional("brew")
    if path:
        return path
    return shutil.which("brew")


def _bootstrap_brew() -> str | None:
    """Install Homebrew non-interactively. Returns brew path or None on failure.

    WARNING: This runs a script from the internet with elevated privileges.
    The installer may prompt for sudo. Only runs on macOS and only when
    brew is completely absent from the system.
    """
    if sys.platform != "darwin":
        log("brew bootstrap is only supported on macOS", Color.RED)
        return None

    log("Installing Homebrew...", Color.GREEN)
    curl = system_bin("curl")
    bash = system_bin("bash")

    # Fetch the installer first, then hand its body to bash. Passing the
    # literal `$(curl ...)` as the -c script would make *bash* substitute
    # and word-split the downloaded script, then try to execute its first
    # token ("#!/bin/bash") as a command name — rc 127, every time.
    rc, script, stderr = run_command([curl, "-fsSL", _BREW_INSTALL_URL])
    if rc != 0 or not script.strip():
        log(f"Failed to download Homebrew installer: {stderr}", Color.RED)
        return None

    rc, stdout, stderr = run_command(
        [bash, "-c", script],
        {**os.environ.copy(), "NONINTERACTIVE": "1"},
    )
    if rc != 0:
        log(f"Failed to install Homebrew: {stderr}", Color.RED)
        return None

    log("Homebrew installed successfully", Color.GREEN)
    return _brew_bin()


def _mas_bin_from_env(env: dict) -> str | None:
    """Return the mas binary path, checking system_paths, then env PATH."""
    path = system_bin_optional("mas")
    if path:
        return path
    return shutil.which("mas", path=env.get("PATH", ""))


def _installed_subset(brew: str, env: dict, candidates: list[str], kind: str) -> set[str] | None:
    """Which of `candidates` brew currently reports as installed.

    Only used to repair bookkeeping after a batch command failed partway;
    the happy path never queries brew. Returns None when brew itself cannot
    be queried — "unknown" must not be conflated with "none installed", or
    a failed uninstall would silently drop ownership of packages that are
    still on disk.
    """
    rc, stdout, _ = run_command([brew, "list", kind, "-1"], env)
    if rc != 0:
        return None
    present = {line.strip() for line in stdout.splitlines() if line.strip()}
    return present & set(candidates)


def install_brew_packages(brew_config: dict, state: dict) -> bool:
    """Declarative Homebrew package management.

    State is the source of truth: we reconcile the desired config against what
    we recorded installing last run (state["brew"]), never against live
    `brew list`/`leaves`/`tap`/`mas list` output. This keeps every deploy fast
    and only shells out to brew when there is an actual diff to apply. Manual
    drift (a hand `brew uninstall`) is not detected — remove the entry from
    state and re-deploy to force a reinstall.

    Config keys: brews, casks, taps, masApps
    """
    if sys.platform != "darwin":
        return True

    desired_brews = set(brew_config.get("brews", []))
    desired_casks = set(brew_config.get("casks", []))
    desired_taps = {_normalize_tap(t) for t in brew_config.get("taps", [])}
    desired_mas = brew_config.get("masApps", {}) or {}  # {name: app_id}

    prev = state.get("brew", {})
    prev_brews = set(prev.get("brews", []))
    prev_casks = set(prev.get("casks", []))
    prev_taps = {_normalize_tap(t) for t in prev.get("taps", [])}
    prev_mas = dict(prev.get("masApps", {}))  # {name: app_id}

    # tracked sets mirror what we believe is installed; mutate on success only.
    inst_brews = set(prev_brews)
    inst_casks = set(prev_casks)
    inst_taps = set(prev_taps)
    inst_mas = dict(prev_mas)

    # Compute the diff purely from state before deciding whether to touch brew.
    taps_to_add = sorted(desired_taps - prev_taps)
    taps_to_remove = sorted(prev_taps - desired_taps)
    brews_to_install = sorted(desired_brews - prev_brews)
    brews_to_remove = sorted(prev_brews - desired_brews)
    casks_to_install = sorted(desired_casks - prev_casks)
    casks_to_remove = sorted(prev_casks - desired_casks)

    prev_ids = {str(v) for v in prev_mas.values()}
    desired_ids = {str(v) for v in desired_mas.values()}
    mas_to_install = sorted(
        (name, app_id) for name, app_id in desired_mas.items() if str(app_id) not in prev_ids
    )
    mas_to_remove = sorted(
        (name, app_id) for name, app_id in prev_mas.items() if str(app_id) not in desired_ids
    )

    any_changes = (
        taps_to_add
        or taps_to_remove
        or brews_to_install
        or brews_to_remove
        or casks_to_install
        or casks_to_remove
        or mas_to_install
        or mas_to_remove
    )
    if not any_changes:
        debug("All brew packages in sync", Color.BLUE)
        if "brew" not in state:
            state["brew"] = {
                "brews": sorted(inst_brews),
                "casks": sorted(inst_casks),
                "taps": sorted(inst_taps),
                "masApps": inst_mas,
            }
        elif prev_mas != desired_mas:
            # No install/remove work means the app-id sets already match, so
            # any dict difference is a pure rename ({"Old": 1} -> {"New": 1}).
            # Adopt the new names or state carries the stale key forever.
            state["brew"]["masApps"] = dict(desired_mas)
        return True

    # Only resolve/bootstrap brew when there is real work to do.
    brew = _brew_bin()
    if not brew:
        brew = _bootstrap_brew()
    if not brew:
        log("brew not found and bootstrap failed, skipping", Color.RED)
        return False

    env = os.environ.copy()
    brew_dir = os.path.dirname(brew)
    env["PATH"] = f"{brew_dir}:{env.get('PATH', '')}"
    for key, value in brew_config.get("environment", {}).items():
        env[key] = str(value)

    success = True

    # --- Taps ---
    for tap in taps_to_add:
        log(f"Tapping {tap}", Color.GREEN)
        rc, _, stderr = run_command([brew, "tap", tap], env)
        if rc != 0:
            log(f"Failed to tap {tap}: {stderr}", Color.RED)
            success = False
        else:
            inst_taps.add(tap)
    for tap in taps_to_remove:
        log(f"Untapping {tap}", Color.RED)
        rc, _, stderr = run_command([brew, "untap", tap], env)
        if rc != 0:
            log(f"Failed to untap {tap}: {stderr}", Color.RED)
            success = False
        else:
            inst_taps.discard(tap)

    # --- Formulas ---
    if brews_to_install:
        log(f"Installing formulas: {', '.join(brews_to_install)}", Color.GREEN)
        rc, _, stderr = run_command([brew, "install"] + brews_to_install, env)
        if rc != 0:
            log(f"Failed to install formulas: {stderr}", Color.RED)
            success = False
            # brew installs targets in order, so an earlier one can land before
            # a later failure. Recording none of them would leave a package we
            # installed permanently unowned and never cleaned up.
            subset = _installed_subset(brew, env, brews_to_install, "--formula")
            if subset is not None:
                inst_brews |= subset
        else:
            inst_brews |= set(brews_to_install)
    if brews_to_remove:
        log(f"Removing formulas: {', '.join(brews_to_remove)}", Color.RED)
        rc, _, stderr = run_command([brew, "uninstall"] + brews_to_remove, env)
        if rc != 0:
            log(f"Failed to remove formulas: {stderr}", Color.RED)
            success = False
            subset = _installed_subset(brew, env, brews_to_remove, "--formula")
            if subset is not None:
                # Unknown (None) keeps everything owned so removal is retried.
                inst_brews &= subset | (inst_brews - set(brews_to_remove))
        else:
            inst_brews -= set(brews_to_remove)

    # --- Casks ---
    no_quarantine = brew_config.get("caskArgs", {}).get("no_quarantine", False)
    if casks_to_install:
        log(f"Installing casks: {', '.join(casks_to_install)}", Color.GREEN)
        cmd = [brew, "install", "--cask"]
        if no_quarantine:
            cmd.append("--no-quarantine")
        cmd += casks_to_install
        rc, _, stderr = run_command(cmd, env)
        if rc != 0:
            log(f"Failed to install casks: {stderr}", Color.RED)
            success = False
            subset = _installed_subset(brew, env, casks_to_install, "--cask")
            if subset is not None:
                inst_casks |= subset
        else:
            inst_casks |= set(casks_to_install)
    if casks_to_remove:
        log(f"Removing casks: {', '.join(casks_to_remove)}", Color.RED)
        rc, _, stderr = run_command([brew, "uninstall", "--cask"] + casks_to_remove, env)
        if rc != 0:
            log(f"Failed to remove casks: {stderr}", Color.RED)
            success = False
            subset = _installed_subset(brew, env, casks_to_remove, "--cask")
            if subset is not None:
                inst_casks &= subset | (inst_casks - set(casks_to_remove))
        else:
            inst_casks -= set(casks_to_remove)

    # --- Mac App Store apps ---
    if (mas_to_install or mas_to_remove) and not (mas := _mas_bin_from_env(env)):
        log("mas not found, skipping Mac App Store apps", Color.YELLOW)
    elif mas_to_install or mas_to_remove:
        # Removals run first: a same-name app_id swap ({"App": 1} -> {"App": 2})
        # queues both a remove (old id) and an install (new id) under one name.
        # Installing first then popping the name would drop the fresh entry from
        # state, so reconcile removals before installs.
        for name, app_id in mas_to_remove:
            log(f"Removing Mac App Store app: {name} ({app_id})", Color.RED)
            rc, _, stderr = run_command([mas, "uninstall", str(app_id)], env)
            if rc != 0:
                log(f"Failed to remove {name}: {stderr}", Color.RED)
                success = False
            else:
                inst_mas.pop(name, None)
        for name, app_id in mas_to_install:
            log(f"Installing Mac App Store app: {name} ({app_id})", Color.GREEN)
            rc, _, stderr = run_command([mas, "install", str(app_id)], env)
            if rc != 0:
                log(f"Failed to install {name}: {stderr}", Color.RED)
                success = False
            else:
                inst_mas[name] = app_id

    # --- Autoremove orphaned deps (only after actual removals) ---
    if brews_to_remove or casks_to_remove:
        rc, stdout, stderr = run_command([brew, "autoremove"], env)
        if rc == 0 and stdout.strip():
            log("Autoremoved orphaned deps", Color.RED)
        elif rc != 0:
            log(f"Failed to autoremove: {stderr}", Color.RED)

    # --- Update state from what we actually installed ---
    state["brew"] = {
        "brews": sorted(inst_brews),
        "casks": sorted(inst_casks),
        "taps": sorted(inst_taps),
        "masApps": inst_mas,
    }

    return success
