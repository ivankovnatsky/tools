import os
import subprocess
from typing import Dict

from tools.log import Color, log
from tools.system_paths import system_bin, system_dir, system_dir_optional


def install_curl_shell_scripts(scripts: Dict[str, str], state: Dict):
    """Install scripts via curl piped to shell interpreter."""
    if not scripts:
        return True

    installed = set(state.get("curlShell", {}).get("installed", []))
    desired = set(scripts.keys())
    to_install = desired - installed

    if not to_install:
        log("All curl shell scripts already installed", Color.BLUE)
        return True

    curl_bin = system_bin("curl")
    bash_dir = system_dir("bash")
    curl_dir = system_dir("curl")
    perl_dir = system_dir_optional("perl")

    env = os.environ.copy()
    path_parts = [bash_dir, curl_dir]
    if perl_dir:
        path_parts.append(perl_dir)
    path_parts.append(env.get("PATH", ""))
    env["PATH"] = ":".join(path_parts)

    state_changed = False

    for url in to_install:
        shell = scripts[url]
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
            continue

        if shell_proc.returncode != 0:
            log(f"Failed to run script from {url}: {stderr.decode()}", Color.RED)
            continue

        log(f"Successfully installed from {url}", Color.GREEN)
        installed.add(url)
        state_changed = True

    if state_changed:
        state.setdefault("curlShell", {})["installed"] = list(installed)

    return True
