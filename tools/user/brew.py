import os
import shutil
import sys

from tools.log import Color, log
from tools.system_paths import system_bin, system_bin_optional
from tools.util import run_command

_BREW_INSTALL_URL = "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"


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

    rc, stdout, stderr = run_command(
        [bash, "-c", f"$({curl} -fsSL {_BREW_INSTALL_URL})"],
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


def _get_installed_formulas(brew: str, env: dict) -> set[str]:
    rc, stdout, _ = run_command([brew, "list", "--formula", "-1"], env)
    if rc != 0:
        return set()
    return {line.strip() for line in stdout.splitlines() if line.strip()}


def _get_leaf_formulas(brew: str, env: dict) -> set[str]:
    """Return manually-installed (leaf) formulas, excluding auto-installed deps."""
    rc, stdout, _ = run_command([brew, "leaves"], env)
    if rc != 0:
        return set()
    return {line.strip() for line in stdout.splitlines() if line.strip()}


def _get_installed_casks(brew: str, env: dict) -> set[str]:
    rc, stdout, _ = run_command([brew, "list", "--cask", "-1"], env)
    if rc != 0:
        return set()
    return {line.strip() for line in stdout.splitlines() if line.strip()}


def _get_active_taps(brew: str, env: dict) -> set[str]:
    rc, stdout, _ = run_command([brew, "tap"], env)
    if rc != 0:
        return set()
    return {line.strip() for line in stdout.splitlines() if line.strip()}


def _get_installed_mas_apps(mas: str, env: dict) -> dict[str, str]:
    """Return {app_id: app_name} for installed Mac App Store apps."""
    rc, stdout, _ = run_command([mas, "list"], env)
    if rc != 0:
        return {}
    apps = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # Format: "123456789  App Name  (1.2.3)"
        parts = line.split(None, 1)
        if parts:
            app_id = parts[0]
            apps[app_id] = parts[1] if len(parts) > 1 else ""
    return apps


def install_brew_packages(brew_config: dict, state: dict) -> bool:
    """Declarative Homebrew package management.

    Reads brew config section and reconciles against actual system state
    (brew list), not just state.json.

    Config keys: brews, casks, taps, masApps, onActivation.cleanup
    """
    if sys.platform != "darwin":
        return True

    brew = _brew_bin()
    if not brew:
        brew = _bootstrap_brew()
    if not brew:
        log("brew not found and bootstrap failed, skipping", Color.RED)
        return False

    env = os.environ.copy()
    brew_dir = os.path.dirname(brew)
    env["PATH"] = f"{brew_dir}:{env.get('PATH', '')}"

    desired_brews = set(brew_config.get("brews", []))
    desired_casks = set(brew_config.get("casks", []))
    desired_taps = set(brew_config.get("taps", []))
    desired_mas = brew_config.get("masApps", {})  # {name: app_id}

    cleanup = brew_config.get("onActivation", {}).get("cleanup") == "zap"

    success = True
    state_changed = False

    # --- Taps ---
    installed_taps = _get_active_taps(brew, env)
    taps_to_add = desired_taps - installed_taps
    taps_to_remove = (installed_taps - desired_taps) if cleanup else set()

    if taps_to_add:
        for tap in sorted(taps_to_add):
            log(f"Tapping {tap}", Color.GREEN)
            rc, _, stderr = run_command([brew, "tap", tap], env)
            if rc != 0:
                log(f"Failed to tap {tap}: {stderr}", Color.RED)
                success = False
            else:
                state_changed = True

    if taps_to_remove:
        for tap in sorted(taps_to_remove):
            log(f"Untapping {tap}", Color.RED)
            rc, _, stderr = run_command([brew, "untap", tap], env)
            if rc != 0:
                log(f"Failed to untap {tap}: {stderr}", Color.RED)
                success = False
            else:
                state_changed = True

    # --- Formulas ---
    # Use brew list for install check (all formulas), but brew leaves for
    # cleanup (only manually-installed, not auto-deps) to avoid breaking
    # dependency chains.
    installed_formulas = _get_installed_formulas(brew, env)
    formulas_to_install = desired_brews - installed_formulas
    if cleanup:
        leaf_formulas = _get_leaf_formulas(brew, env)
        formulas_to_remove = leaf_formulas - desired_brews
    else:
        formulas_to_remove = set()

    if formulas_to_install:
        log(f"Installing formulas: {', '.join(sorted(formulas_to_install))}", Color.GREEN)
        cmd = [brew, "install"] + sorted(formulas_to_install)
        rc, _, stderr = run_command(cmd, env)
        if rc != 0:
            log(f"Failed to install formulas: {stderr}", Color.RED)
            success = False
        else:
            state_changed = True

    if formulas_to_remove:
        log(f"Removing formulas: {', '.join(sorted(formulas_to_remove))}", Color.RED)
        cmd = [brew, "uninstall"] + sorted(formulas_to_remove)
        rc, _, stderr = run_command(cmd, env)
        if rc != 0:
            log(f"Failed to remove formulas: {stderr}", Color.RED)
            success = False
        else:
            state_changed = True

    # --- Casks ---
    installed_casks = _get_installed_casks(brew, env)
    casks_to_install = desired_casks - installed_casks
    casks_to_remove = (installed_casks - desired_casks) if cleanup else set()

    if casks_to_install:
        log(f"Installing casks: {', '.join(sorted(casks_to_install))}", Color.GREEN)
        cmd = [brew, "install", "--cask"] + sorted(casks_to_install)
        rc, _, stderr = run_command(cmd, env)
        if rc != 0:
            log(f"Failed to install casks: {stderr}", Color.RED)
            success = False
        else:
            state_changed = True

    if casks_to_remove:
        log(f"Removing casks: {', '.join(sorted(casks_to_remove))}", Color.RED)
        cmd = [brew, "uninstall", "--cask"] + sorted(casks_to_remove)
        rc, _, stderr = run_command(cmd, env)
        if rc != 0:
            log(f"Failed to remove casks: {stderr}", Color.RED)
            success = False
        else:
            state_changed = True

    # --- Mac App Store apps ---
    # Re-discover mas after formula installs (it may have just been installed
    # as a brew formula in this same run).
    mas = _mas_bin_from_env(env)
    mas_to_install: set[str] = set()
    mas_to_remove: set[str] = set()

    if not mas and (desired_mas or cleanup):
        log("mas not found, skipping Mac App Store apps", Color.YELLOW)
    elif mas:
        installed_apps = _get_installed_mas_apps(mas, env)
        installed_ids = set(installed_apps.keys())

        desired_ids = {str(v) for v in desired_mas.values()} if desired_mas else set()

        mas_to_install = desired_ids - installed_ids
        mas_to_remove = (installed_ids - desired_ids) if cleanup else set()

        if mas_to_install:
            for app_id in sorted(mas_to_install):
                name = next((k for k, v in desired_mas.items() if str(v) == app_id), app_id)
                log(f"Installing Mac App Store app: {name} ({app_id})", Color.GREEN)
                rc, _, stderr = run_command([mas, "install", app_id], env)
                if rc != 0:
                    log(f"Failed to install {name}: {stderr}", Color.RED)
                    success = False
                else:
                    state_changed = True

        if mas_to_remove:
            for app_id in sorted(mas_to_remove):
                name = installed_apps.get(app_id, app_id)
                log(f"Removing Mac App Store app: {name} ({app_id})", Color.RED)
                rc, _, stderr = run_command([mas, "uninstall", app_id], env)
                if rc != 0:
                    log(f"Failed to remove {name}: {stderr}", Color.RED)
                    success = False
                else:
                    state_changed = True

    # --- Summary ---
    any_changes = (
        taps_to_add
        or taps_to_remove
        or formulas_to_install
        or formulas_to_remove
        or casks_to_install
        or casks_to_remove
        or mas_to_install
        or mas_to_remove
    )
    if not any_changes:
        log("All brew packages in sync", Color.BLUE)

    # --- Update state ---
    if state_changed or "brew" not in state:
        state["brew"] = {
            "brews": sorted(desired_brews),
            "casks": sorted(desired_casks),
            "taps": sorted(desired_taps),
            "masApps": desired_mas,
        }

    return success
