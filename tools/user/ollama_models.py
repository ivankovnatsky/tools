"""Manage Ollama models declaratively.

Pulls desired models via `ollama pull` and tracks them in state.
When `removeUntracked` is enabled, previously-managed models that
fall out of the desired list are removed via `ollama rm`. The
reconciler degrades gracefully when the Ollama CLI is missing or the
daemon is not reachable, matching the prior shell-snippet behavior.
"""

import os
import shutil
from typing import Dict, List, Optional, Set

from tools.log import Color, log
from tools.util import run_command

_OLLAMA_PATH_FALLBACKS = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]


def _canonical(model: str) -> str:
    """Normalize a model ref so untagged names match `ollama list` output.

    `ollama pull foo` is reported by `ollama list` as `foo:latest`, so a
    naive set comparison would never converge. We append `:latest` to
    any ref that lacks an explicit tag, both before comparing against
    the live list and before persisting to state.
    """
    return model if ":" in model else f"{model}:latest"


def _find_ollama() -> Optional[str]:
    ollama = shutil.which("ollama")
    if ollama:
        return ollama
    for d in _OLLAMA_PATH_FALLBACKS:
        candidate = os.path.join(d, "ollama")
        if os.path.exists(candidate):
            return candidate
    return None


def _build_env(config: Dict) -> Dict[str, str]:
    env = os.environ.copy()
    if config.get("host"):
        env["OLLAMA_HOST"] = config["host"]
    if config.get("modelsPath"):
        env["OLLAMA_MODELS"] = os.path.expanduser(config["modelsPath"])
    return env


def _list_installed(ollama: str, env: Dict[str, str]) -> Optional[Set[str]]:
    """Return the set of installed model tags, or None if the daemon is down."""
    rc, stdout, _ = run_command([ollama, "list"], env=env)
    if rc != 0:
        return None
    installed: Set[str] = set()
    lines = stdout.splitlines()
    for line in lines[1:] if lines else []:
        parts = line.split()
        if parts:
            installed.add(parts[0])
    return installed


def diff_ollama_models(config: Dict, state: Dict) -> List[str]:
    """Return human-readable changes the reconciler would apply."""
    desired_models = [_canonical(m) for m in (config.get("models", []) or [])]
    tracked = [_canonical(m) for m in state.get("ollamaModels", {}).get("installed", [])]
    if not desired_models and not tracked:
        return []

    ollama = _find_ollama()
    if not ollama:
        return ["  ? ollama CLI not found"]

    env = _build_env(config)
    installed = _list_installed(ollama, env)
    if installed is None:
        return ["  ? ollama daemon not reachable"]

    changes: List[str] = []
    desired = set(desired_models)
    managed = set(tracked)
    for model in sorted(desired - installed):
        changes.append(f"  + pull {model}")

    # Force deploy to run when a desired model already exists on disk
    # but isn't in state — otherwise show_diff returns "no changes",
    # deploy short-circuits, and the model is never adopted into state.
    for model in sorted((desired & installed) - managed):
        changes.append(f"  ~ adopt {model}")

    if config.get("removeUntracked"):
        for model in sorted((managed & installed) - desired):
            changes.append(f"  - remove {model}")

    return changes


def install_ollama_models(config: Dict, state: Dict) -> bool:
    """Reconcile installed Ollama models toward the desired list."""
    desired_models = [_canonical(m) for m in (config.get("models", []) or [])]
    tracked = {_canonical(m) for m in state.get("ollamaModels", {}).get("installed", [])}
    if not desired_models and not tracked:
        return True

    ollama = _find_ollama()
    if not ollama:
        log("ollama CLI not found, skipping model management", Color.YELLOW)
        return True

    env = _build_env(config)
    installed = _list_installed(ollama, env)
    if installed is None:
        log("ollama daemon not reachable, skipping model downloads", Color.YELLOW)
        return True

    success = True
    desired = set(desired_models)
    newly_pulled: Set[str] = set()

    for model in sorted(desired - installed):
        log(f"Pulling {model} ...", Color.GREEN)
        rc, _, stderr = run_command([ollama, "pull", model], env=env)
        if rc != 0:
            log(f"Failed to pull {model}: {stderr.strip()}", Color.RED)
            success = False
            continue
        tracked.add(model)
        newly_pulled.add(model)

    # Adopt models that already exist on disk but were missing from
    # state — otherwise removeUntracked-driven cleanup later would
    # treat them as never-managed and silently keep them around.
    for model in desired & installed:
        tracked.add(model)

    if config.get("removeUntracked"):
        for model in sorted((tracked & installed) - desired):
            log(f"Removing {model} ...", Color.GREEN)
            rc, _, stderr = run_command([ollama, "rm", model], env=env)
            if rc != 0:
                log(f"Failed to remove {model}: {stderr.strip()}", Color.RED)
                success = False
                continue
            tracked.discard(model)
        # Drop entries that vanished from ollama out-of-band so state
        # converges. Union in `newly_pulled` because `installed` is the
        # pre-pull snapshot and would otherwise discard models pulled
        # in this same run.
        tracked &= installed | newly_pulled

    state.setdefault("ollamaModels", {})["installed"] = sorted(tracked)
    return success
