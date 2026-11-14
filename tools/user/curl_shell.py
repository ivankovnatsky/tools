import os
import subprocess
from collections.abc import Mapping
from typing import Dict, List, Optional, Tuple

from tools.log import Color, debug, log
from tools.system_paths import system_bin, system_dir, system_dir_optional
from tools.util import run_command

_KEYS = {"shell", "uninstall"}

Script = Tuple[str, Optional[str]]


def parse(raw) -> Tuple[Dict[str, Script], List[str]]:
    """Map each url to (shell, uninstall command or None), plus config errors."""
    if raw is None:
        return {}, []
    if not isinstance(raw, Mapping):
        return {}, [f"curlShell must be a mapping of url -> shell, got {type(raw).__name__}"]
    scripts: Dict[str, Script] = {}
    errors: List[str] = []
    for url, spec in raw.items():
        where = f"curlShell.{url}"
        if isinstance(spec, str):
            spec = {"shell": spec}
        if not isinstance(spec, Mapping):
            errors.append(f"{where}: must be a shell or a mapping with shell")
            continue
        unknown = set(spec) - _KEYS
        if unknown:
            errors.append(f"{where}: unknown keys {', '.join(sorted(map(str, unknown)))}")
            continue
        shell = spec.get("shell")
        if not isinstance(shell, str) or not shell.strip():
            errors.append(f"{where}: shell must be a non-empty string")
            continue
        uninstall = spec.get("uninstall")
        if uninstall is not None and (not isinstance(uninstall, str) or not uninstall.strip()):
            errors.append(f"{where}: uninstall must be a non-empty shell command")
            continue
        scripts[url] = (shell, uninstall)
    return scripts, errors


def _env() -> Dict[str, str]:
    env = os.environ.copy()
    path_parts = [system_dir("bash"), system_dir("curl")]
    perl_dir = system_dir_optional("perl")
    if perl_dir:
        path_parts.append(perl_dir)
    path_parts.append(env.get("PATH", ""))
    env["PATH"] = ":".join(path_parts)
    return env


def install_curl_shell_scripts(raw: Dict, state: Dict):
    """Install scripts via curl piped to shell interpreter."""
    section = state.get("curlShell", {})
    installed = set(section.get("installed", []))
    recorded = dict(section.get("uninstall", {}))
    if raw is None and not installed:
        return True

    scripts, errors = parse(raw)
    if errors:
        for err in errors:
            log(err, Color.RED)
        return False

    desired = set(scripts)
    to_install = sorted(desired - installed)
    stale = sorted(installed - desired)
    success = True
    env = _env() if to_install or any(url in recorded for url in stale) else None

    # A dropped url with no recorded uninstall can only be released from
    # state — otherwise re-adding it later would never re-run.
    for url in stale:
        command = recorded.get(url)
        if command:
            log(f"Uninstalling {url}: {command}", Color.RED)
            rc, _, stderr = run_command(["/bin/sh", "-c", command], env=env)
            if rc != 0:
                log(f"Failed to uninstall {url} (exit {rc}): {stderr}", Color.RED)
                success = False
                continue
        installed.discard(url)
        recorded.pop(url, None)

    # The config entry is gone by removal time, so the command must already
    # be in state; keep it current for entries installed earlier.
    for url in desired & installed:
        uninstall = scripts[url][1]
        if uninstall:
            recorded[url] = uninstall
        else:
            recorded.pop(url, None)

    if to_install:
        curl_bin = system_bin("curl")
        bash_dir = system_dir("bash")
    elif not stale:
        debug("All curl shell scripts already installed", Color.BLUE)

    for url in to_install:
        shell, uninstall = scripts[url]
        log(f"Running: curl -fsSL {url} | {shell}", Color.GREEN)

        shell_path = f"{bash_dir}/{shell}" if shell == "bash" else shell

        curl_cmd = [curl_bin, "-fsSL", url]
        curl_proc = subprocess.Popen(
            curl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
        )

        shell_proc = subprocess.Popen(
            [shell_path],
            stdin=curl_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        curl_proc.stdout.close()
        stdout, stderr = shell_proc.communicate()
        curl_proc.wait()

        if curl_proc.returncode != 0:
            curl_stderr = curl_proc.stderr.read().decode() if curl_proc.stderr else ""
            log(
                f"Failed to fetch {url} (curl exit {curl_proc.returncode}): {curl_stderr}",
                Color.RED,
            )
            success = False
            continue

        if shell_proc.returncode != 0:
            log(f"Failed to run script from {url}: {stderr.decode()}", Color.RED)
            success = False
            continue

        log(f"Successfully installed from {url}", Color.GREEN)
        installed.add(url)
        if uninstall:
            recorded[url] = uninstall

    if installed != set(section.get("installed", [])) or recorded != section.get("uninstall", {}):
        out = state.setdefault("curlShell", {})
        out["installed"] = sorted(installed)
        if recorded:
            out["uninstall"] = dict(sorted(recorded.items()))
        else:
            out.pop("uninstall", None)

    return success


def diff_curl_shell(raw, state: Dict) -> List[str]:
    scripts, errors = parse(raw)
    if errors:
        return [f"  ! {err}" for err in errors]
    section = state.get("curlShell", {})
    installed = set(section.get("installed", []))
    recorded = section.get("uninstall", {})
    changes = [f"  + install {url}" for url in sorted(set(scripts) - installed)]
    for url in sorted(installed - set(scripts)):
        if recorded.get(url):
            changes.append(f"  - uninstall {url} (runs: {recorded[url]})")
        else:
            # Deploy releases it from state; the preview must show that or the
            # interactive path never reconciles it.
            changes.append(f"  ~ forget {url} (no uninstall; releases tracking)")
    return changes
