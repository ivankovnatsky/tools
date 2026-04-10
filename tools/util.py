import os
import subprocess
from typing import Dict, List

from tools.log import Color, log


def run_command(cmd: List[str], env: Dict = None, cwd: str = None) -> tuple[int, str, str]:
    result = subprocess.run(
        cmd, capture_output=True, text=True, env=env or os.environ.copy(), cwd=cwd
    )
    return result.returncode, result.stdout, result.stderr


def get_pkg_binary(pkg_info: Dict) -> str:
    """Extract binary name from package info dict."""
    return pkg_info.get("binary", "")


def get_pkg_version(pkg_info: Dict) -> str:
    """Extract version from package info dict."""
    return pkg_info.get("version", "latest")


def get_pkg_subpackages(pkg_info: Dict) -> Dict:
    """Extract subpackages dict from package info dict."""
    return pkg_info.get("subpackages", {})


def get_pkg_post_install(pkg_info: Dict) -> str:
    """Extract postInstall command from package info dict."""
    return pkg_info.get("postInstall", "")


def get_pkg_source(pkg_info: Dict) -> str:
    """Extract source URL from package info dict."""
    return pkg_info.get("source", "")


def pkg_install_spec(name: str, version: str, source: str = "") -> str:
    """Build package install specifier."""
    if source:
        return source
    if version and version != "latest":
        return f"{name}@{version}"
    return name


def version_changed(pkg: str, pkg_info, state: Dict, manager: str) -> bool:
    """Check if declared version or source differs from state."""
    pkg_state = state.get(manager, {}).get("packages", {}).get(pkg, {})
    declared_version = get_pkg_version(pkg_info)
    stored_version = pkg_state.get("version", "latest")
    declared_source = get_pkg_source(pkg_info)
    stored_source = pkg_state.get("source", "")
    return declared_version != stored_version or declared_source != stored_source


class SecretSubstitutionError(Exception):
    """Raised when a referenced secret file cannot be read."""


def substitute_secrets(text: str, secret_paths: Dict[str, str]) -> str:
    """Replace @VARIABLE@ placeholders with content from secret files.

    Raises SecretSubstitutionError if a referenced secret file cannot be read,
    so callers fail fast instead of passing half-substituted text downstream.
    """
    result = text
    for var_name, file_path in secret_paths.items():
        placeholder = f"@{var_name}@"
        if placeholder not in result:
            continue
        try:
            with open(file_path, "r") as f:
                secret_value = f.read().strip()
        except Exception as e:
            log(f"Failed to read secret file {file_path}: {e}", Color.RED)
            raise SecretSubstitutionError(
                f"cannot read secret file {file_path} for @{var_name}@: {e}"
            ) from e
        result = result.replace(placeholder, secret_value)
    return result
