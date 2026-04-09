import os
import subprocess
from typing import Dict

from tools.log import Color, log


def install_curl_shell_scripts(scripts: Dict[str, str], paths: Dict, state: Dict):
    """Install scripts via curl piped to shell interpreter."""
    if not scripts:
        return True

    installed = set(state.get("curlShell", {}).get("installed", []))
    desired = set(scripts.keys())
    to_install = desired - installed

    if not to_install:
        log("All curl shell scripts already installed", Color.BLUE)
        return True

    env = os.environ.copy()
    env["PATH"] = (
        f"{paths.get('bash', '/bin')}:{paths['curl']}:"
        f"{paths.get('perl', '')}:{paths.get('coreutils', '')}:{env.get('PATH', '')}"
    )

    state_changed = False

    for url in to_install:
        shell = scripts[url]
        log(f"Running: curl -fsSL {url} | {shell}", Color.GREEN)

        shell_path = f"{paths.get('bash', '/bin')}/{shell}" if shell == "bash" else shell

        curl_cmd = [f"{paths['curl']}/curl", "-fsSL", url]
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

        if shell_proc.returncode != 0:
            log(f"Failed to run script from {url}: {stderr.decode()}", Color.RED)
            continue

        log(f"Successfully installed from {url}", Color.GREEN)
        installed.add(url)
        state_changed = True

    if state_changed:
        state.setdefault("curlShell", {})["installed"] = list(installed)

    return True
