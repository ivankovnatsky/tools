"""Agent skills: directories containing SKILL.md, fanned out to agent skill dirs.

Config shape::

    skills:
      cacheDir: ~/.cache/tools/skills        # optional
      sources:
        anthropic:
          url: https://github.com/anthropics/skills
          ref: main                          # optional branch, tag or commit
          subdir: skills                     # optional
        mine:
          path: ./skills                     # relative to the config dir
      targets:                               # default destinations
        - ~/.claude/skills
        - ~/.codex/skills
      enable:
        - anthropic/pdf
        - mine/*                             # every skill in a source
        - skill: mine/claw-only              # per-skill destinations
          targets: [~/.openclaw-skills]
          as: claw                           # optional install name

Each enabled skill is copied to ``<target>/<name>/``. Ownership follows
the `files:` model: every installed directory is recorded in state with a
content hash, and deploy never overwrites a directory it did not create,
never updates or removes one modified since it was written, and only ever
removes directories it recorded.

Git sources are cloned into ``cacheDir/<source>``. That directory belongs
to this tool entirely, so it is fetched and force-checked-out freely.
"""

import hashlib
import json
import os
import re
import shutil
from typing import Dict, List, Optional, Tuple

from tools.log import Color, debug, log
from tools.system_paths import system_bin, system_dir
from tools.util import run_command

DEFAULT_CACHE_DIR = "~/.cache/tools/skills"

IGNORE_DIRS = {".git", ".hg", ".svn", "__pycache__"}
IGNORE_FILES = {".DS_Store"}

_TMP_SUFFIX = ".tools-tmp"


def _ignored(name: str) -> bool:
    """Names left out of copies and hashes alike, file or directory."""
    return name in IGNORE_DIRS or name in IGNORE_FILES


# --------------------------------------------------------------------------
# Paths and hashing
# --------------------------------------------------------------------------


def _expand(path: str) -> str:
    return os.path.normpath(os.path.expanduser(os.path.expandvars(str(path))))


def cache_dir_for(skills_config: Dict) -> str:
    return _expand(skills_config.get("cacheDir") or DEFAULT_CACHE_DIR)


def tree_hash(root: str) -> Optional[str]:
    """Content hash of a directory tree, or None if it does not exist.

    Covers relative paths, file bytes, the executable bit and symlink
    targets. Ignored names match what `_copy_tree` skips, so a fresh copy
    hashes identically to its source.
    """
    if not os.path.isdir(root) or os.path.islink(root):
        return None
    entries = []

    def _raise(err):
        raise err

    try:
        for dirpath, dirnames, filenames in os.walk(root, onerror=_raise):
            dirnames[:] = sorted(d for d in dirnames if not _ignored(d))
            rel_dir = os.path.relpath(dirpath, root)
            for name in dirnames + sorted(filenames):
                if _ignored(name):
                    continue
                full = os.path.join(dirpath, name)
                rel = os.path.normpath(os.path.join(rel_dir, name))
                if os.path.islink(full):
                    entries.append(["L", rel, os.readlink(full)])
                elif os.path.isdir(full):
                    entries.append(["D", rel])
                else:
                    with open(full, "rb") as f:
                        digest = hashlib.sha256(f.read()).hexdigest()
                    executable = bool(os.stat(full).st_mode & 0o111)
                    entries.append(["F", rel, digest, executable])
    except OSError:
        return None
    entries.sort()
    return hashlib.sha256(json.dumps(entries).encode()).hexdigest()


def discover(root: str) -> Dict[str, str]:
    """Map relative skill path -> absolute dir for every dir holding SKILL.md.

    The relative path uses '/' separators; a SKILL.md at the root itself
    is keyed as '.'. Directories inside a skill are not searched.
    """
    found: Dict[str, str] = {}
    if not os.path.isdir(root):
        return found
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not _ignored(d))
        if "SKILL.md" in filenames:
            rel = os.path.relpath(dirpath, root).replace(os.sep, "/")
            found[rel] = dirpath
            # Anything below belongs to this skill (examples, templates).
            dirnames[:] = []
    return found


# --------------------------------------------------------------------------
# Resolution: config -> desired {dest: (skill_id, source_dir)}
# --------------------------------------------------------------------------


def _source_base(name: str, source: Dict, cache_dir: str, config_dir: str) -> str:
    if source.get("path") is not None:
        path = os.path.expanduser(os.path.expandvars(str(source["path"])))
        return os.path.normpath(os.path.join(config_dir, path))
    return os.path.join(cache_dir, name)


def _subdir(source: Dict) -> str:
    return os.path.normpath(str(source.get("subdir") or "."))


def source_root(name: str, source: Dict, cache_dir: str, config_dir: str) -> str:
    base = _source_base(name, source, cache_dir, config_dir)
    return os.path.normpath(os.path.join(base, _subdir(source)))


def validate_sources(sources: Dict) -> List[str]:
    errors = []
    if not isinstance(sources, dict):
        return [f"sources must be a mapping, got {type(sources).__name__}"]
    for name, source in sources.items():
        if not isinstance(name, str) or not name or "/" in name or name in (".", ".."):
            errors.append(f"invalid source name {name!r}")
            continue
        if not isinstance(source, dict):
            errors.append(f"source {name!r} must be a mapping")
            continue
        has_url = bool(source.get("url"))
        has_path = source.get("path") is not None
        if has_url == has_path:
            errors.append(f"source {name!r} needs exactly one of `url` or `path`")
        for key in ("url", "ref"):
            if str(source.get(key) or "").startswith("-"):
                errors.append(f"source {name!r}: {key} must not start with '-'")
        subdir = _subdir(source)
        if os.path.isabs(subdir) or ".." in subdir.replace("\\", "/").split("/"):
            errors.append(f"source {name!r}: subdir must be relative without '..'")
    return errors


def _normalize_entry(entry) -> Tuple[Optional[Dict], Optional[str]]:
    if isinstance(entry, str):
        return {"skill": entry}, None
    if isinstance(entry, dict) and isinstance(entry.get("skill"), str):
        unknown = set(entry) - {"skill", "targets", "as"}
        if unknown:
            return None, f"unknown keys {sorted(unknown)} in enable entry {entry!r}"
        return entry, None
    return None, f"enable entry must be a string or a mapping with `skill`: {entry!r}"


def _validate_targets(targets, where: str) -> Tuple[List[str], List[str]]:
    if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
        return [], [f"{where}: targets must be a list of paths"]
    resolved, errors = [], []
    for t in targets:
        expanded = _expand(t)
        if not os.path.isabs(expanded):
            errors.append(f"{where}: target {t!r} must be absolute (or start with ~)")
            continue
        resolved.append(expanded)
    return resolved, errors


def resolve(
    skills_config: Dict, config_dir: str
) -> Tuple[Dict[str, Tuple[str, str]], List[str], List[str]]:
    """Resolve the desired install set from config and on-disk sources.

    Returns (desired, errors, pending):
      desired  dest_dir -> (skill_id, source_dir)
      errors   human-readable config problems
      pending  enable entries whose git source is not fetched yet
    """
    errors: List[str] = []
    pending: List[str] = []
    desired: Dict[str, Tuple[str, str]] = {}

    sources = skills_config.get("sources") or {}
    errors += validate_sources(sources)
    if errors:
        return desired, errors, pending

    default_targets, target_errors = _validate_targets(skills_config.get("targets") or [], "skills")
    errors += target_errors

    enable = skills_config.get("enable") or []
    if not isinstance(enable, list):
        return desired, errors + ["enable must be a list"], pending

    cache_dir = cache_dir_for(skills_config)
    catalogs: Dict[str, Optional[Dict[str, str]]] = {}

    for raw in enable:
        entry, err = _normalize_entry(raw)
        if err:
            errors.append(err)
            continue
        skill = entry["skill"]
        source_name, _, rel = skill.partition("/")
        rel = rel or "."
        if source_name not in sources:
            errors.append(f"{skill}: unknown source {source_name!r}")
            continue

        if "targets" in entry:
            targets, target_errors = _validate_targets(entry["targets"], skill)
            errors += target_errors
        else:
            targets = default_targets
        if not targets:
            errors.append(f"{skill}: no targets (set skills.targets or per-entry targets)")
            continue

        if source_name not in catalogs:
            source = sources[source_name]
            root = source_root(source_name, source, cache_dir, config_dir)
            base = _source_base(source_name, source, cache_dir, config_dir)
            if source.get("url") and not _is_checkout(base):
                if os.path.lexists(base):
                    errors.append(f"source {source_name!r}: {base} is not a git checkout")
                    catalogs[source_name] = {}
                    continue
                catalogs[source_name] = None  # not fetched yet
            elif source.get("url") and not os.path.isdir(root):
                # Outside the sparse cone until the next sync re-sparses it.
                catalogs[source_name] = None
            elif not os.path.isdir(root):
                errors.append(f"source {source_name!r}: directory not found: {root}")
                catalogs[source_name] = {}
                continue
            else:
                catalogs[source_name] = discover(root)

        catalog = catalogs[source_name]
        if catalog is None:
            pending.append(skill)
            continue

        if rel == "*":
            if "as" in entry:
                errors.append(f"{skill}: `as` cannot be used with '*'")
                continue
            matches = sorted(catalog.items())
            if not matches:
                errors.append(f"{skill}: source has no skills")
                continue
        elif rel in catalog:
            matches = [(rel, catalog[rel])]
        else:
            errors.append(f"{skill}: no SKILL.md found at that path")
            continue

        for match_rel, src_dir in matches:
            if "as" in entry:
                install_name = str(entry["as"])
            elif match_rel == ".":
                install_name = source_name
            else:
                install_name = match_rel.rsplit("/", 1)[-1]
            if not install_name or "/" in install_name or install_name in (".", ".."):
                errors.append(f"{skill}: invalid install name {install_name!r}")
                continue
            skill_id = source_name if match_rel == "." else f"{source_name}/{match_rel}"
            for target in targets:
                dest = os.path.join(target, install_name)
                existing = desired.get(dest)
                if existing and existing[0] != skill_id:
                    errors.append(f"{dest}: claimed by both {existing[0]} and {skill_id}")
                    continue
                desired[dest] = (skill_id, src_dir)

    return desired, errors, pending


# --------------------------------------------------------------------------
# Git sources
# --------------------------------------------------------------------------


def _is_checkout(path: str) -> bool:
    return os.path.isdir(os.path.join(path, ".git"))


def _git_env() -> Dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{system_dir('git')}:{env.get('PATH', '')}"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _sync_sparse(git: str, dest: str, subdir: str, env: Dict[str, str]) -> Optional[str]:
    """Limit the checkout to subdir; a no-op when the cone already matches.

    Returns an error message, or None on success.
    """
    rc, out, _ = run_command([git, "-C", dest, "sparse-checkout", "list"], env)
    current = out.splitlines() if rc == 0 else None  # None: not sparse
    if subdir == ".":
        if current is None:
            return None
        cmd = [git, "-C", dest, "sparse-checkout", "disable"]
    else:
        if current == [subdir]:
            return None
        cmd = [git, "-C", dest, "sparse-checkout", "set", "--cone", "--", subdir]
    rc, _, err = run_command(cmd, env)
    return err.strip() or "failed" if rc != 0 else None


def sync_git_source(name: str, source: Dict, cache_dir: str) -> bool:
    """Clone or fetch a git source into the cache and check out its ref."""
    git = system_bin("git")
    env = _git_env()
    dest = os.path.join(cache_dir, name)
    url = str(source["url"])
    ref = source.get("ref")

    if _is_checkout(dest):
        run_command([git, "-C", dest, "remote", "set-url", "origin", url], env)
        rc, _, err = run_command(
            [git, "-C", dest, "fetch", "--force", "--tags", "--prune", "origin"], env
        )
        if rc != 0:
            log(f"skills: failed to fetch {name}: {err.strip()}", Color.RED)
            return False
        # A changed default branch upstream would otherwise leave origin/HEAD stale.
        run_command([git, "-C", dest, "remote", "set-head", "origin", "--auto"], env)
    else:
        if os.path.lexists(dest):
            # cacheDir is user-settable; never delete what we did not clone.
            log(f"skills: {dest} exists and is not a git checkout; not touching it", Color.RED)
            return False
        os.makedirs(cache_dir, exist_ok=True)
        log(f"skills: cloning {url} -> {dest}", Color.GREEN)
        # Blobless and unchecked-out: where the server supports partial
        # clone, only the blobs under subdir are downloaded, when the sparse
        # checkout below materializes them. History stays complete, so any
        # ref or commit hash still resolves.
        rc, _, err = run_command(
            [git, "clone", "--quiet", "--filter=blob:none", "--no-checkout", "--", url, dest],
            env,
        )
        if rc != 0:
            log(f"skills: failed to clone {name}: {err.strip()}", Color.RED)
            return False

    err = _sync_sparse(git, dest, _subdir(source), env)
    if err:
        log(f"skills: {name}: cannot set sparse checkout: {err}", Color.RED)
        return False

    # Resolve to a commit first: a bare `git checkout <name>` that is not a
    # revision is taken as a pathspec and succeeds without moving HEAD. A
    # bare ref is only tried as a commit hash: as a name it could match the
    # local branch left by the clone (or HEAD) and pin stale content.
    if not ref:
        candidates = ["origin/HEAD"]
    else:
        candidates = [f"origin/{ref}", f"refs/tags/{ref}"]
        if re.fullmatch(r"[0-9a-fA-F]{4,64}", str(ref)):
            candidates.append(str(ref))
    commit = None
    for candidate in candidates:
        rc, out, _ = run_command(
            [git, "-C", dest, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"],
            env,
        )
        if rc == 0:
            commit = out.strip()
            break
    if not commit:
        log(f"skills: {name}: ref {ref or 'origin/HEAD'} not found", Color.RED)
        return False

    rc, _, err = run_command(
        [git, "-C", dest, "checkout", "--quiet", "--force", "--detach", commit], env
    )
    if rc != 0:
        log(f"skills: {name}: cannot check out {commit}: {err.strip()}", Color.RED)
        return False
    run_command([git, "-C", dest, "clean", "-ffdxq"], env)
    return True


# --------------------------------------------------------------------------
# Copying
# --------------------------------------------------------------------------


def _ignore(_dir, names):
    return [n for n in names if _ignored(n)]


def _copy_tree(src: str, dest: str) -> None:
    """Replace dest with a copy of src, staging beside it first."""
    tmp = dest + _TMP_SUFFIX
    if os.path.lexists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copytree(src, tmp, symlinks=True, ignore=_ignore)
    if os.path.lexists(dest):
        shutil.rmtree(dest)
    os.replace(tmp, dest)


# --------------------------------------------------------------------------
# Plan: shared by deploy and diff so the two cannot disagree
# --------------------------------------------------------------------------


def plan(skills_config: Dict, config_dir: str, state: Dict) -> Dict[str, List]:
    """Compute what deploy would do against the current on-disk sources."""
    installed = state.get("skills", {}).get("installed", {}) or {}
    desired, errors, pending = resolve(skills_config, config_dir)

    result: Dict[str, List] = {
        "errors": errors,
        "pending": pending,
        "install": [],  # (dest, skill_id, src_dir, src_hash)
        "update": [],  # (dest, skill_id, src_dir, src_hash)
        "adopt": [],  # tracked, already matching source: (dest, skill_id, src_hash)
        "blocked": [],  # (dest, reason)
        "remove": [],  # dest
        "forget": [],  # dest (tracked, already gone)
        "keep": [],  # dest (dropped from config but modified locally)
    }

    for dest, (skill_id, src_dir) in sorted(desired.items()):
        src_hash = tree_hash(src_dir)
        if src_hash is None:
            result["errors"].append(f"{skill_id}: cannot read {src_dir}")
            continue
        tracked = installed.get(dest)
        if not os.path.lexists(dest):
            result["install"].append((dest, skill_id, src_dir, src_hash))
            continue
        if not tracked:
            result["blocked"].append((dest, "exists and is not managed by tools"))
            continue
        current = tree_hash(dest)
        if current == src_hash:
            if tracked.get("hash") != src_hash or tracked.get("skill") != skill_id:
                result["adopt"].append((dest, skill_id, src_hash))
            continue
        if current != tracked.get("hash"):
            if src_hash != tracked.get("hash"):
                result["blocked"].append((dest, "modified since tools wrote it"))
            continue
        result["update"].append((dest, skill_id, src_dir, src_hash))

    # Skills from a source not fetched yet are unresolved, not undesired.
    pending_sources = {skill.partition("/")[0] for skill in pending}
    for dest in sorted(set(installed) - set(desired)):
        if _from_source((installed[dest] or {}).get("skill", ""), pending_sources):
            continue
        if not os.path.lexists(dest):
            result["forget"].append(dest)
        elif tree_hash(dest) == (installed[dest] or {}).get("hash"):
            result["remove"].append(dest)
        else:
            result["keep"].append(dest)

    return result


def _git_sources(skills_config: Dict) -> Dict[str, Dict]:
    """Git sources that at least one enable entry refers to."""
    sources = skills_config.get("sources") or {}
    enable = skills_config.get("enable") or []
    if not isinstance(sources, dict) or not isinstance(enable, list):
        return {}
    used = set()
    for raw in enable:
        entry, _ = _normalize_entry(raw)
        if entry:
            used.add(entry["skill"].partition("/")[0])
    return {n: s for n, s in sources.items() if n in used and isinstance(s, dict) and s.get("url")}


def _from_source(skill_id: str, names) -> bool:
    return skill_id.partition("/")[0] in names


def _source_fingerprint(source: Dict) -> Dict:
    return {"url": source.get("url"), "ref": source.get("ref"), "subdir": _subdir(source)}


# --------------------------------------------------------------------------
# Deploy
# --------------------------------------------------------------------------


def install_skills(skills_config: Dict, config_dir: str, state: Dict) -> bool:
    skills_config = skills_config or {}
    section = state.setdefault("skills", {})
    installed: Dict[str, Dict] = dict(section.get("installed", {}) or {})
    cached: Dict[str, Dict] = dict(section.get("sources", {}) or {})
    success = True

    source_errors = validate_sources(skills_config.get("sources") or {})
    cache_dir = cache_dir_for(skills_config)
    git_sources = {} if source_errors else _git_sources(skills_config)

    failed = set()
    for name, source in sorted(git_sources.items()):
        if sync_git_source(name, source, cache_dir):
            cached[name] = _source_fingerprint(source)
        else:
            failed.add(name)
            success = False

    p = plan(skills_config, config_dir, {"skills": {"installed": installed}})
    # A failed fetch or checkout leaves the old ref in the cache; installing
    # from it would record the wrong content as the configured one.
    for key in ("install", "update", "adopt"):
        p[key] = [item for item in p[key] if not _from_source(item[1], failed)]
    for err in p["errors"]:
        log(f"skills: {err}", Color.RED)
        success = False
    for skill in p["pending"]:
        log(f"skills: {skill}: source not available", Color.RED)
        success = False
    for dest, reason in p["blocked"]:
        log(f"skills: skipping {dest}: {reason}", Color.YELLOW)

    for dest, skill_id, src_hash in p["adopt"]:
        installed[dest] = {"skill": skill_id, "hash": src_hash}

    for action, label in (("install", "Installed"), ("update", "Updated")):
        for dest, skill_id, src_dir, src_hash in p[action]:
            try:
                _copy_tree(src_dir, dest)
            except OSError as e:
                log(f"skills: failed to write {dest}: {e}", Color.RED)
                success = False
                continue
            installed[dest] = {"skill": skill_id, "hash": src_hash}
            log(f"{label} skill {skill_id}: {dest}", Color.GREEN)

    # Removal only on a fully resolved run: a failed fetch or a config error
    # makes skills look undesired when they are merely unresolved.
    if success:
        for dest in p["forget"]:
            installed.pop(dest, None)
        for dest in p["keep"]:
            log(
                f"Keeping {dest}: modified since tools wrote it. "
                "Delete it by hand if it is no longer wanted.",
                Color.YELLOW,
            )
        for dest in p["remove"]:
            try:
                shutil.rmtree(dest)
            except OSError as e:
                log(f"skills: failed to remove {dest}: {e}", Color.RED)
                success = False
                continue
            installed.pop(dest, None)
            log(f"Removed skill: {dest}", Color.RED)

        for name in sorted(set(cached) - set(git_sources)):
            path = os.path.join(cache_dir, name)
            if _is_checkout(path):
                try:
                    shutil.rmtree(path)
                except OSError as e:
                    log(f"skills: failed to drop cache {path}: {e}", Color.RED)
                    success = False
                    continue
                log(f"skills: dropped cache {path}", Color.RED)
            cached.pop(name, None)

    if not any(p[k] for k in ("install", "update", "remove")):
        debug("All skills up to date", Color.BLUE)

    section["installed"] = installed
    section["sources"] = cached
    return success


# --------------------------------------------------------------------------
# Diff (read-only: never fetches, never writes)
# --------------------------------------------------------------------------


def diff_skills(skills_config: Dict, config_dir: str, state: Dict) -> List[str]:
    skills_config = skills_config or {}
    changes: List[str] = []
    cached = state.get("skills", {}).get("sources", {}) or {}
    cache_dir = cache_dir_for(skills_config)
    git_sources = (
        {}
        if validate_sources(skills_config.get("sources") or {})
        else (_git_sources(skills_config))
    )

    # Upstream commits on an unchanged url/ref are not previewed (like
    # gitRepos pulls): seeing them would need the network.
    for name, source in sorted(git_sources.items()):
        path = os.path.join(cache_dir, name)
        if os.path.lexists(path) and not _is_checkout(path):
            continue  # reported by plan() as an error
        if not _is_checkout(path):
            changes.append(f"  + fetch {name} ({source['url']})")
        elif cached.get(name) != _source_fingerprint(source):
            changes.append(f"  ~ refetch {name} (url, ref or subdir changed)")

    p = plan(skills_config, config_dir, state)
    changes += [f"  ! {err}" for err in p["errors"]]
    changes += [f"  + install {skill} (after fetch)" for skill in p["pending"]]
    changes += [f"  + install {sid} -> {dest}" for dest, sid, _, _ in p["install"]]
    changes += [f"  ~ update {sid} -> {dest}" for dest, sid, _, _ in p["update"]]
    changes += [f"  ! skip {dest}: {reason}" for dest, reason in p["blocked"]]
    # Deploy holds every removal while anything is in error; so does the preview.
    if not p["errors"]:
        changes += [f"  - remove {dest}" for dest in p["remove"]]
        changes += [f"  ~ forget {dest}" for dest in p["forget"]]
        for name in sorted(set(cached) - set(git_sources)):
            changes.append(f"  - drop cache {name}")
    return changes


def has_work(skills_config: Dict, state: Dict) -> bool:
    section = state.get("skills", {})
    return bool(skills_config or section.get("installed") or section.get("sources"))
