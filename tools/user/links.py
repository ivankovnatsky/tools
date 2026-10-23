"""Symlinks: declare a link path and what it points at.

Config shape::

    links:
      - source: ~/.agents/skills/jira    # what the link points at
        target: ~/.claude/skills/jira    # the symlink to create

Both paths expand ``~`` and ``$VARS`` and must be absolute afterwards.
Links are written with absolute targets, parent dirs created as needed.

Ownership follows the `files:` and `skills:` model: every link created is
recorded in state, and deploy only ever repoints or removes links it
recorded, and only while they still point where tools left them. An
existing file, directory or foreign symlink is never replaced. The one
exception is a symlink that already points at the configured source
(relative or absolute form alike): it is adopted into state silently.

A source that does not exist is skipped with a warning rather than
linked, so deploy never makes a dangling link. It is not an error: the
source may appear later (a skill not installed yet) and the next deploy
picks it up.
"""

import os
from typing import Dict, List, Optional, Tuple

from tools.log import Color, debug, log

# Distinct from skills' ".tools-tmp": both stage beside ~/.claude/skills/*.
_TMP_SUFFIX = ".tools-link-tmp"
_KEYS = {"source", "target"}


def _expand(path: str) -> str:
    return os.path.normpath(os.path.expanduser(os.path.expandvars(str(path))))


def _readlink(link: str) -> Optional[str]:
    try:
        return os.readlink(link)
    except OSError:
        return None


def _points_to(link: str, source: str) -> bool:
    """True if link is a symlink whose text names source.

    Relative link text is resolved against the link's directory, so
    ``../../.agents/skills/x`` and its absolute form match; the kernel
    resolves it against the real directory, so that is tried too. The
    link's own final target is never resolved: a foreign link straight
    into whatever source itself points at is not ours.
    """
    text = _readlink(link) if os.path.islink(link) else None
    if text is None:
        return False
    parent = os.path.dirname(link)
    return any(
        os.path.normpath(os.path.join(base, text)) == source
        for base in (parent, os.path.realpath(parent))
    )


def _tracked_source(installed: Dict, target: str) -> Optional[str]:
    entry = installed.get(target)
    source = entry.get("source") if isinstance(entry, dict) else None
    return source if isinstance(source, str) else None


def resolve(entries) -> Tuple[Dict[str, str], List[str]]:
    """Validate config into {target: source}. Returns (desired, errors)."""
    desired: Dict[str, str] = {}
    errors: List[str] = []
    if not isinstance(entries, list):
        return desired, [f"links must be a list, got {type(entries).__name__}"]

    for entry in entries:
        if not isinstance(entry, dict) or not all(
            isinstance(entry.get(k), str) and entry.get(k) for k in _KEYS
        ):
            errors.append(f"entry must have string `source` and `target`: {entry!r}")
            continue
        unknown = set(entry) - _KEYS
        if unknown:
            errors.append(f"unknown keys {sorted(unknown)} in entry {entry!r}")
            continue
        source, target = _expand(entry["source"]), _expand(entry["target"])
        if not os.path.isabs(source) or not os.path.isabs(target):
            errors.append(f"source and target must be absolute (or start with ~): {entry!r}")
            continue
        if source == target:
            errors.append(f"{target}: links to itself")
            continue
        if target in desired:
            errors.append(f"{target}: declared more than once")
            continue
        desired[target] = source

    return desired, errors


# --------------------------------------------------------------------------
# Plan: shared by deploy and diff so the two cannot disagree
# --------------------------------------------------------------------------


def plan(entries, state: Dict) -> Dict[str, List]:
    installed = state.get("links", {}).get("installed", {}) or {}
    # Paths skills.linkTargets owns; two owners would remove each other's link.
    owned_by_skills = state.get("skills", {}).get("links", {}) or {}
    desired, errors = resolve(entries)

    result: Dict[str, List] = {
        "errors": errors,
        "create": [],  # (target, source)
        "update": [],  # (target, source), repoint a tracked link
        "adopt": [],  # (target, source), already pointing at source
        "missing": [],  # (target, source), source does not exist
        "blocked": [],  # (target, reason)
        "remove": [],  # target
        "forget": [],  # (target, reason) tracked, but gone or no longer ours
    }

    for target, source in sorted(desired.items()):
        if not os.path.lexists(source):
            result["missing"].append((target, source))
            continue
        tracked = _tracked_source(installed, target)
        if target in owned_by_skills:
            result["blocked"].append((target, "managed by skills.linkTargets"))
        elif not os.path.lexists(target):
            result["create"].append((target, source))
        elif _points_to(target, source):
            if tracked != source:
                result["adopt"].append((target, source))
        elif tracked and _points_to(target, tracked):
            result["update"].append((target, source))
        elif os.path.islink(target):
            reason = "changed since tools made it" if tracked else "not managed by tools"
            result["blocked"].append((target, f"symlink to {_readlink(target)} {reason}"))
        else:
            result["blocked"].append((target, "exists and is not a symlink"))

    for target in sorted(set(installed) - set(desired)):
        tracked = _tracked_source(installed, target)
        if not os.path.lexists(target):
            result["forget"].append((target, "already gone"))
        elif tracked and _points_to(target, tracked):
            result["remove"].append(target)
        else:
            result["forget"].append((target, "changed since tools made it; left in place"))

    return result


# --------------------------------------------------------------------------
# Deploy
# --------------------------------------------------------------------------


def _make_link(source: str, target: str) -> None:
    """Point target at source, replacing a link only via an atomic rename."""
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    if not os.path.lexists(target):
        os.symlink(source, target)
        return
    tmp = target + _TMP_SUFFIX
    if os.path.islink(tmp):
        os.remove(tmp)
    os.symlink(source, tmp)
    try:
        os.replace(tmp, target)
    except OSError:
        os.remove(tmp)
        raise


def install_links(entries, state: Dict) -> bool:
    entries = entries or []
    section = state.setdefault("links", {})
    installed: Dict[str, Dict] = dict(section.get("installed", {}) or {})
    success = True

    p = plan(entries, {"links": {"installed": installed}, "skills": state.get("skills", {})})
    for err in p["errors"]:
        log(f"links: {err}", Color.RED)
        success = False
    for target, source in p["missing"]:
        log(f"links: skipping {target}: source {source} not found", Color.YELLOW)
    for target, reason in p["blocked"]:
        log(f"links: skipping {target}: {reason}", Color.YELLOW)

    for target, source in p["adopt"]:
        installed[target] = {"source": source}

    for action, label in (("create", "Linked"), ("update", "Relinked")):
        for target, source in p[action]:
            try:
                _make_link(source, target)
            except OSError as e:
                log(f"links: failed to link {target}: {e}", Color.RED)
                success = False
                continue
            installed[target] = {"source": source}
            log(f"{label}: {target} -> {source}", Color.GREEN)

    # Removal only on a clean config: an invalid entry makes its link look
    # undesired when it is merely unparsed. Gated on errors, not success,
    # so a failed write elsewhere does not hold removals the diff showed.
    if not p["errors"]:
        for target, reason in p["forget"]:
            if reason != "already gone":
                log(f"links: releasing {target}: {reason}", Color.YELLOW)
            installed.pop(target, None)
        for target in p["remove"]:
            try:
                os.remove(target)
            except OSError as e:
                log(f"links: failed to remove {target}: {e}", Color.RED)
                success = False
                continue
            installed.pop(target, None)
            log(f"Removed link: {target}", Color.RED)

    if not any(p[k] for k in ("create", "update", "remove")):
        debug("All links up to date", Color.BLUE)

    section["installed"] = installed
    return success


# --------------------------------------------------------------------------
# Diff (read-only)
# --------------------------------------------------------------------------


def diff_links(entries, state: Dict) -> List[str]:
    p = plan(entries or [], state)
    changes = [f"  ! {err}" for err in p["errors"]]
    changes += [f"  + link {t} -> {s}" for t, s in p["create"]]
    changes += [f"  ~ relink {t} -> {s}" for t, s in p["update"]]
    changes += [f"  ! skip {t}: source {s} not found" for t, s in p["missing"]]
    changes += [f"  ! skip {t}: {reason}" for t, reason in p["blocked"]]
    # Deploy holds every removal while anything is in error; so does the preview.
    if not p["errors"]:
        changes += [f"  - remove {t}" for t in p["remove"]]
        changes += [f"  ~ forget {t} ({reason})" for t, reason in p["forget"]]
    return changes


def has_work(entries, state: Dict) -> bool:
    return bool(entries or state.get("links", {}).get("installed"))
