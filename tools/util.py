import difflib
import os
import re
import shutil
import subprocess
from typing import Dict, List, Optional

from tools.log import Color, log

# Cap diff output to keep terminals readable when large config files
# diverge. Anything past this is suppressed with a "(... N more lines)"
# footer so the user knows the diff was truncated.
_DIFF_MAX_LINES = 200


def run_command(cmd: List[str], env: Dict = None, cwd: str = None) -> tuple[int, str, str]:
    """(returncode, stdout, stderr); a command that cannot start (e.g. its runtime is
    not installed yet) is exit 127 with the reason in stderr instead of an exception."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, env=env or os.environ.copy(), cwd=cwd
        )
    except OSError as exc:
        return 127, "", f"{exc.filename or cmd[0]}: {exc.strerror or exc}"
    return result.returncode, result.stdout, result.stderr


def run_hooks(
    section: str,
    hooks: Dict[str, List[str]],
    kind: str = "onChange",
    retry: str = "retried next deploy",
) -> Dict[str, List[str]]:
    """Run each hook command once, in order, via ``/bin/sh -c``.

    ``hooks`` maps a command to the paths that triggered it.
    Returns the commands that failed, in the same shape.
    """
    failed: Dict[str, List[str]] = {}
    for command, changed in hooks.items():
        more = f" and {len(changed) - 1} more" if len(changed) > 1 else ""
        log(f"{section}: {kind} ({changed[0]}{more}): {command}", Color.BLUE)
        try:
            code = subprocess.run(["/bin/sh", "-c", command]).returncode
        except OSError as e:
            log(f"{section}: {kind} failed ({e}): {command}", Color.RED)
            failed[command] = changed
            continue
        if code != 0:
            log(f"{section}: {kind} failed (exit {code}); {retry}", Color.RED)
            failed[command] = changed
    return failed


def pending_hooks(state: Dict, section: str, declared) -> Dict[str, List[str]]:
    """Commands that failed on an earlier deploy and are still declared."""
    pending = (state.get("onChange") or {}).get(section) or {}
    if not isinstance(pending, dict):
        return {}
    return {
        cmd: [p for p in paths if isinstance(p, str)] or ["(earlier deploy)"]
        for cmd, paths in pending.items()
        if cmd in declared and isinstance(paths, list)
    }


def add_pending_hooks(state: Dict, section: str, hooks: Dict[str, List[str]]) -> None:
    """Record ``hooks`` as owed in state before they run."""
    if not hooks:
        return
    pending = state.setdefault("onChange", {}).setdefault(section, {})
    for cmd, paths in hooks.items():
        known = pending.setdefault(cmd, [])
        known += [p for p in paths if p not in known]


def settle_hooks(
    state: Dict, section: str, hooks: Dict[str, List[str]], declared, held=frozenset()
) -> bool:
    """Run ``hooks`` plus still-pending ones; a failure stays pending in state.

    Pending ``held`` commands (declared only by ``skip: true`` entries) neither
    run nor drop out of state.
    """
    merged = pending_hooks(state, section, declared)
    pending = (state.get("onChange") or {}).get(section)
    kept = {
        cmd: paths
        for cmd, paths in (pending.items() if isinstance(pending, dict) else ())
        if cmd in held and cmd not in declared
    }
    for cmd, paths in hooks.items():
        merged.setdefault(cmd, [])
        merged[cmd] += [p for p in paths if p not in merged[cmd]]
    store = state.setdefault("onChange", {})
    # Recorded before running, so an interrupted deploy still retries.
    store[section] = {**kept, **merged}
    failed = run_hooks(section, merged)
    if failed or kept:
        store[section] = {**kept, **failed}
    else:
        store.pop(section, None)
    if not store:
        state.pop("onChange", None)
    return not failed


def parse_on_change(value, label: str, key: str = "onChange") -> Optional[str]:
    """Validate a hook value (``onChange``, ``onUpdate``): absent, or a non-empty string."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}: {key} must be a non-empty string, got {value!r}")
    return value


# Diff rows starting with this are notes, not changes: they never prompt.
SKIPPED_ROW = "  = skip "


def skipped_row(target: str) -> str:
    return f"{SKIPPED_ROW}{target} (skip: true; not written)"


def package_entries(section: str, packages) -> tuple[Dict[str, Dict], List[str]]:
    """`<section>.packages` with bare keys (None) as {}, plus config errors."""
    if packages is None:
        return {}, []
    if not isinstance(packages, dict):
        return {}, [f"{section}.packages must be a mapping, got {type(packages).__name__}"]
    entries: Dict[str, Dict] = {}
    errors: List[str] = []
    for name, info in packages.items():
        if info is None:
            info = {}
        if not isinstance(info, dict):
            errors.append(f"{section}.packages.{name} must be a mapping, got {type(info).__name__}")
            continue
        entries[name] = info
    return entries, errors


def checked_packages(section: str, packages) -> tuple[Dict[str, Dict], bool]:
    """package_entries with its errors logged; False means the section must not run."""
    entries, errors = package_entries(section, packages)
    for err in errors:
        log(err, Color.RED)
    return entries, not errors


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


def get_pkg_commit(pkg_info: Dict) -> str:
    """Extract commit pin from package info dict."""
    return pkg_info.get("commit", "")


def pkg_unpinned(pkg_info: Dict) -> bool:
    """No version or commit pin: reinstalling may move it to a newer release."""
    return get_pkg_version(pkg_info) in ("", "latest") and not get_pkg_commit(pkg_info)


def pkg_spec_full(name: str, pkg_info: Dict) -> str:
    """Install spec honoring source and commit (npm/bun style `source#commit`).

    `version_changed` compares source and commit, so the install spec must
    honor them too — otherwise a declared source is silently ignored and the
    package reinstalls from the registry on every run, never converging.
    """
    source = get_pkg_source(pkg_info)
    commit = get_pkg_commit(pkg_info)
    if source and commit:
        return f"{source}#{commit}"
    return pkg_install_spec(name, get_pkg_version(pkg_info), source)


def pkg_state_entry(pkg_info: Dict) -> Dict:
    """Canonical installed-state entry: version/source(/commit) as declared.

    Everything `version_changed` compares must be persisted, or the
    comparison sees a permanent mismatch and reinstalls forever.
    """
    entry = {
        "installed": True,
        "version": get_pkg_version(pkg_info),
        "source": get_pkg_source(pkg_info),
    }
    commit = get_pkg_commit(pkg_info)
    if commit:
        entry["commit"] = commit
    return entry


def version_changed(pkg: str, pkg_info, state: Dict, manager: str) -> bool:
    """Check if declared version, source, or commit differs from state."""
    pkg_state = state.get(manager, {}).get("packages", {}).get(pkg, {})
    declared_version = get_pkg_version(pkg_info)
    stored_version = pkg_state.get("version", "latest")
    declared_source = get_pkg_source(pkg_info)
    stored_source = pkg_state.get("source", "")
    declared_commit = get_pkg_commit(pkg_info)
    stored_commit = pkg_state.get("commit", "")
    # Compare unconditionally: removing a commit pin (declared "" vs stored
    # "abc") must reinstall at the plain version, not be treated as unchanged.
    if declared_commit != stored_commit:
        return True
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


def _is_binary(data: bytes) -> bool:
    """Heuristic: a NUL byte in the first 8 KiB means binary."""
    return b"\x00" in data[:8192]


# PEM-armored private keys (RSA, EC, OpenSSH, generic). Header is
# stable across formats. Scanned anywhere in the file because cert
# bundles routinely place certs (1.5–2 KiB each) before the key,
# pushing the BEGIN marker past any short prefix window.
_PEM_PRIVATE_KEY = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")

# Project-wide secret-substitution template marker (see substitute_secrets).
# A file whose source carries @VAR@ placeholders is a *template* signal:
# the resolved file would contain a credential at that position. Require
# at least three chars between the @ delimiters so single letters and
# common doc usages (`@A@`, `@O@example.com`) do not trip it.
_SECRET_PLACEHOLDER = re.compile(rb"@[A-Z_][A-Z0-9_]{2,}@")


def looks_like_secret(data: bytes) -> bool:
    """Cheap heuristic: does this content likely contain a secret?

    Catches PEM-armored private keys and @VAR@ template placeholders.
    Used as a safety net so deploy/diff output does not leak credentials
    when the user forgot to mark an entry with ``secrets: true``. False
    negatives are expected (AWS keys, tokens in JSON, .netrc, .env, etc.
    are not detected) — this is opportunistic, not a security boundary.
    Always set ``secrets: true`` explicitly on entries known to carry
    credentials and treat this heuristic as a tripwire only.
    """
    if _PEM_PRIVATE_KEY.search(data):
        return True
    if _SECRET_PLACEHOLDER.search(data):
        return True
    return False


def _decode_for_diff(data: bytes) -> Optional[List[str]]:
    """Return text-split lines for diffing, or None if binary/undecodable."""
    if _is_binary(data):
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text.splitlines(keepends=True)


def _try_delta(src_bytes: bytes, tgt_bytes: bytes, label_old: str, label_new: str) -> Optional[str]:
    """Pipe both contents to `delta` if available; return its stdout.

    `delta` reads a unified diff from stdin and renders it. We feed it
    a python-generated diff so we keep one source of truth for the
    diff body and just borrow delta's syntax highlighting.
    """
    delta = shutil.which("delta")
    if not delta:
        return None
    src_lines = _decode_for_diff(src_bytes)
    tgt_lines = _decode_for_diff(tgt_bytes)
    if src_lines is None or tgt_lines is None:
        return None
    unified = "".join(
        difflib.unified_diff(tgt_lines, src_lines, fromfile=label_old, tofile=label_new)
    )
    if not unified:
        return ""
    try:
        result = subprocess.run(
            [delta, "--paging", "never"],
            input=unified,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def format_diff_bytes(
    src_bytes: bytes, tgt_bytes: bytes, label_path: str, indent: str = "    "
) -> Optional[str]:
    """Build a printable diff from in-memory bytes.

    Returns a string ready to print, or None when no readable diff is
    available (binary content, decode failure). Empty string if the
    inputs are byte-equal. Output is capped at ``_DIFF_MAX_LINES`` with
    a truncation footer and left-padded with ``indent``.
    """
    if src_bytes == tgt_bytes:
        return ""

    label_old = f"a/{label_path}"
    label_new = f"b/{label_path}"

    rendered = _try_delta(src_bytes, tgt_bytes, label_old, label_new)
    if rendered is None:
        src_lines = _decode_for_diff(src_bytes)
        tgt_lines = _decode_for_diff(tgt_bytes)
        if src_lines is None or tgt_lines is None:
            return f"{indent}(binary content changed)"
        rendered = "".join(
            difflib.unified_diff(tgt_lines, src_lines, fromfile=label_old, tofile=label_new)
        )

    if not rendered:
        return ""

    lines = rendered.splitlines()
    truncated = False
    if len(lines) > _DIFF_MAX_LINES:
        omitted = len(lines) - _DIFF_MAX_LINES
        lines = lines[:_DIFF_MAX_LINES]
        truncated = True

    body = "\n".join(f"{indent}{line}" for line in lines)
    if truncated:
        body += f"\n{indent}(... {omitted} more lines)"
    return body
