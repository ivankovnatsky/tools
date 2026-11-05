"""Wire sops and templates to tools' state file, shared by deploy and diff.

The providers themselves take parsed resources and injected callables;
this module is the only place that knows their state keys and how the
two sections feed each other.
"""

import os
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Set, Tuple

from tools import fsutil, util
from tools.user import git_repos, sops, templates


@dataclass
class RunReport:
    """What changed this deploy, for dependents (a future launchd section)."""

    changed: Dict[str, List[str]] = field(default_factory=dict)  # section -> names
    failed_secrets: Tuple[str, ...] = ()
    notify: List[Tuple[str, str]] = field(default_factory=list)  # (target, "section/name")


def _installed(state: Dict, section: str) -> Dict[str, Dict]:
    return dict(state.get(section, {}).get("installed", {}) or {})


def _norm(path: str) -> str:
    return os.path.normpath(os.path.abspath(os.path.expanduser(path)))


_link_key = fsutil.canonical_path
_tree_key = fsutil.canonical_real


class Ownership(dict):
    """Paths file-writing sections must refuse, mapped to the reason.

    The dict items are paths claimed by more than one of files, sops and
    templates. Lookups also refuse a path that is, or resolves through, a
    symlink managed by `links`/`skills`, or that lies inside a directory
    `skills` manages, or inside what a managed link points at; those are
    reported but not listed as items. Both sides compare in one canonical
    form (``fsutil.canonical_path``/``canonical_real``); items and messages
    keep the paths as spelled.
    """

    def __init__(
        self,
        exact=(),
        links: Optional[Dict[str, str]] = None,
        trees=None,
        repos=(),
        clone_pending: bool = False,
    ):
        super().__init__(exact)
        self._exact = {_link_key(p): reason for p, reason in dict(exact).items()}
        self.links = {_link_key(p): (p, owner) for p, owner in (links or {}).items()}
        self.trees = {_tree_key(p): (p, owner) for p, owner in (trees or {}).items()}
        self.repos = {_tree_key(p): p for p in repos}
        self.clone_pending = clone_pending
        # State-only `files:` directories holding another section's path (canonical keys).
        self.release_dirs: Set[str] = set()

    def __bool__(self) -> bool:
        return bool(len(self) or self.links or self.trees or self.repos)

    def reason(self, path: str) -> Optional[str]:
        n = _norm(path)
        exact = self._exact.get(_link_key(n))
        if exact is not None:
            return exact
        return self.managed_reason(n) or self.repo_reason(n)

    def repo_reason(self, path: str) -> Optional[str]:
        """Why a write to ``path`` would pre-empt or break a gitRepos clone.

        Checked at write time; ``clone_pending`` means the clone is still to come.
        """
        if not self.repos:
            return None
        real = _tree_key(path)
        for root, dest in self.repos.items():
            if not fsutil.is_within(real, root):
                continue
            if real == root:
                return f"the gitRepos checkout {dest}"
            if git_repos.is_checkout(dest):
                continue
            if os.path.lexists(dest):
                return f"inside gitRepos checkout {dest}, which is not a git checkout"
            if not self.clone_pending:
                return f"inside gitRepos checkout {dest}, which is not cloned yet"
        return None

    def awaiting_clone(self, path: str) -> Optional[str]:
        """Why ``path`` may be missing now but present by the time it is
        read: it lies in a gitRepos destination that is still to be cloned."""
        if not self.clone_pending:
            return None
        real = _tree_key(path)
        for root, dest in self.repos.items():
            if fsutil.is_within(real, root) and not os.path.lexists(dest):
                return f"arrives with the gitRepos clone of {dest}"
        return None

    def managed_reason(self, path: str) -> Optional[str]:
        """Why ``path`` is under `links`/`skills` management, ignoring claims.

        Removals check only this: a state-only claim must still free itself.
        """
        n = _norm(path)
        if not self.links and not self.trees:
            return None
        current = n
        while True:
            key = _link_key(current)
            if key in self.links:
                spelled, owner = self.links[key]
                if current == n:
                    return f"a symlink managed by {owner}"
                return f"under {spelled}, a symlink managed by {owner}"
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        real = _tree_key(n)
        for root, (spelled, owner) in self.trees.items():
            if fsutil.is_within(real, root):
                return f"inside {spelled}, {owner}"
        return None

    def __contains__(self, path) -> bool:
        return isinstance(path, str) and self.reason(path) is not None

    def __getitem__(self, path):
        reason = self.reason(path) if isinstance(path, str) else None
        if reason is None:
            raise KeyError(path)
        return reason

    def get(self, path, default=None):
        reason = self.reason(path) if isinstance(path, str) else None
        return default if reason is None else reason


def _link_owned_paths(links_raw, skills_raw, state: Dict, config_dir: str):
    from tools.user import links as links_section
    from tools.user import skills as skills_section

    link_paths: Dict[str, str] = {}
    trees: Dict[str, str] = {}
    pointee = "the target of a symlink managed by links"
    for path, record in (state.get("links", {}).get("installed", {}) or {}).items():
        link_paths[_norm(path)] = "links"
        source = record.get("source") if isinstance(record, dict) else None
        if isinstance(source, str) and source:
            trees.setdefault(_norm(source), pointee)
    desired, _errors = links_section.resolve([] if links_raw is None else links_raw)
    for path, source in desired.items():
        link_paths[_norm(path)] = "links"
        trees.setdefault(_norm(source), pointee)

    managed = "a directory managed by skills"
    skills_state = state.get("skills", {})
    for path in skills_state.get("links", {}) or {}:
        link_paths.setdefault(_norm(path), "skills")
    for path in skills_state.get("installed", {}) or {}:
        trees[_norm(path)] = managed
    if isinstance(skills_raw, Mapping) and skills_raw:
        try:
            desired, skill_links, _errors, pending = skills_section.resolve(skills_raw, config_dir)
            trees[_norm(skills_section.cache_dir_for(skills_raw))] = managed
            copies, pending_links = skills_section.pending_paths(skills_raw, pending)
        except (AttributeError, TypeError, ValueError, OSError):
            # The skills section reports its own config errors; state still guards.
            desired, skill_links, copies, pending_links = {}, {}, [], []
        for path in [*desired, *copies]:
            trees[_norm(path)] = managed
        for path in [*skill_links, *pending_links]:
            link_paths.setdefault(_norm(path), "skills")
    return link_paths, trees


def _movable():
    """Sections a path can move between: name -> (state store, module).

    Each module defines record_unmodified, record_for and record_secret.
    """
    from tools.user import files

    return {
        "files": (lambda st: st.setdefault("files", {}), files),
        "sops": (lambda st: st.setdefault("sops", {}).setdefault("installed", {}), sops),
        "templates": (
            lambda st: st.setdefault("templates", {}).setdefault("installed", {}),
            templates,
        ),
    }


def _declared(files_entries, sops_raw, templates_raw, config_dir):
    """section -> [(path, name)] declared now, or None on any config error."""
    from tools.user.files import _resolve_entries

    entries, files_errors = _resolve_entries(files_entries or [], config_dir)
    parsed_sops = sops.parse_sops(sops_raw)
    tmpls, tmpl_errors = templates.parse_templates(templates_raw, *parsed_sops.secrets_dir())
    if files_errors or parsed_sops.errors or tmpl_errors:
        return None
    return {
        "files": [(e.target, "") for e in entries if not e.directory],
        "sops": [(x.path, x.name) for x in parsed_sops.secrets],
        "templates": [(t.target, t.name) for t in tmpls],
    }


def move_between_sections(
    state: Dict,
    *,
    files_entries=None,
    sops_raw=None,
    templates_raw=None,
    config_dir: str = ".",
) -> List[str]:
    """Move state records of paths whose owner changed in config.

    When exactly one movable section declares a path that another only tracks
    in state, the record moves to the declaring section so it adopts the file
    instead of both refusing it. Only a file still as tools wrote it moves (a
    missing one just drops the record); an edited one keeps its record and
    stays refused. Skipped while any of them has config errors. Mutates
    ``state`` and returns ``"<path> (<from> -> <to>)"`` per move.
    """
    declared_by = _declared(files_entries, sops_raw, templates_raw, config_dir)
    if declared_by is None:
        return []
    declared: Dict[str, Tuple[str, str, str]] = {}  # key -> (section, path, name)
    counts: Dict[str, int] = {}
    for section, items in declared_by.items():
        for path, name in items:
            if isinstance(path, str) and path:
                key = _link_key(path)
                counts[key] = counts.get(key, 0) + 1
                declared[key] = (section, path, name)

    sections = _movable()
    stores = {name: store(state) for name, (store, _) in sections.items()}
    moved: List[str] = []
    for old, (_, old_mod) in sections.items():
        for path, record in list(stores[old].items()):
            key = _link_key(path) if isinstance(path, str) and path else None
            if key not in declared or counts[key] > 1:
                continue
            new, new_path, name = declared[key]
            if new == old or not isinstance(record, dict) or record.get("dir"):
                continue
            if os.path.lexists(path):
                if not old_mod.record_unmodified(path, record):
                    continue
                new_mod = sections[new][1]
                stores[new][new_path] = new_mod.record_for(
                    path, name, old_mod.record_secret(record)
                )
            del stores[old][path]
            moved.append(f"{new_path} ({old} -> {new})")
    return moved


def ownership_conflicts(
    *,
    files_entries=None,
    sops_raw=None,
    templates_raw=None,
    links_raw=None,
    skills_raw=None,
    state: Optional[Dict] = None,
    config_dir: str = ".",
    git_repos_raw=None,
    clone_pending: bool = False,
    env_raw=None,
) -> Ownership:
    """Paths every file-writing section (files, sops, templates, env) refuses.

    A path claimed by more than one of them, declared now or tracked in
    state, is refused by all, so two owners never overwrite each other. A
    claim that exists only in state frees itself: its owner removes or
    forgets it on that deploy. Symlinks and directories that `links` and
    `skills` manage are refused too, including writes through them, as
    are paths inside a ``gitRepos`` destination that is not a checkout
    (``clone_pending``: the clone happens before the write).
    """
    state = state or {}
    claims: Dict[str, Set[str]] = {}
    spelled: Dict[str, str] = {}

    def claim(path, section: str) -> None:
        if isinstance(path, str) and path:
            key = _link_key(path)
            claims.setdefault(key, set()).add(section)
            spelled.setdefault(key, _norm(path))

    from tools.user.files import _resolve_entries

    parsed_sops = sops.parse_sops(sops_raw)
    secrets_root = parsed_sops.secrets_dir()[0]
    private_mode: Dict[str, str] = {}
    directories: Set[str] = set()
    tracked_dirs: Set[str] = set()
    for entry in _resolve_entries(files_entries or [], config_dir)[0]:
        claim(entry.target, "files")
        if not entry.directory:
            continue
        directories.add(_link_key(entry.target))
        if (
            secrets_root
            and entry.mode not in (None, fsutil.SECRETS_DIR_MODE)
            and fsutil.is_within(
                fsutil.canonical_real(entry.target), fsutil.canonical_real(secrets_root)
            )
        ):
            private_mode[_norm(entry.target)] = (
                f"inside secretsDir, whose directories stay "
                f"{oct(fsutil.SECRETS_DIR_MODE)}; drop mode or move it"
            )
    for path, record in (state.get("files", {}) or {}).items():
        claim(path, "files")
        if isinstance(record, dict) and record.get("dir") and isinstance(path, str) and path:
            tracked_dirs.add(_link_key(path))
    for secret in sops.parse_sops(sops_raw).secrets:
        claim(secret.path, "sops")
    for path in state.get("sops", {}).get("installed", {}) or {}:
        claim(path, "sops")
    for tmpl in templates.parse_templates(templates_raw, *sops.parse_sops(sops_raw).secrets_dir())[
        0
    ]:
        claim(tmpl.target, "templates")
    for path in state.get("templates", {}).get("installed", {}) or {}:
        claim(path, "templates")
    if env_raw is not None:
        from tools.user import env as env_section

        spec, _errors = env_section.parse(env_raw)
        for path in spec.files() if spec else ():
            claim(path, "env")
    for path in state.get("env", {}).get("files", {}) or {}:
        claim(path, "env")
    exact = {
        spelled[path]: "claimed by " + " and ".join(sorted(sections))
        for path, sections in claims.items()
        if len(sections) > 1
    }
    # A managed file may not be a directory on another managed file's path,
    # unless it is a `files:` directory entry, declared or known only from state.
    # A state-only one holding another section's path is released, not removed.
    tracked_dirs -= directories
    release_dirs: Set[str] = set()
    for key in claims:
        child, anc = key, os.path.dirname(key)
        while anc != child:
            if anc in tracked_dirs and claims[key] - {"files"}:
                release_dirs.add(anc)
            if anc in claims and anc not in directories and anc not in tracked_dirs:
                exact.setdefault(
                    spelled[key],
                    f"inside {spelled[anc]}, which {' and '.join(sorted(claims[anc]))} manages",
                )
                exact.setdefault(
                    spelled[anc],
                    f"a parent of {spelled[key]}, which "
                    f"{' and '.join(sorted(claims[key]))} manages",
                )
                break
            child, anc = anc, os.path.dirname(anc)
    for path, reason in private_mode.items():
        exact.setdefault(path, reason)
    link_paths, trees = _link_owned_paths(links_raw, skills_raw, state, config_dir)
    repos = [_norm(d) for d in _mapping(git_repos_raw) if isinstance(d, str) and d]
    ownership = Ownership(exact, link_paths, trees, repos, clone_pending)
    ownership.release_dirs = release_dirs
    return ownership


def _mapping(raw) -> Mapping:
    return raw if isinstance(raw, Mapping) else {}


def _private_dirs(state: Dict) -> list:
    """Directories tools created inside secretsDir; their mode is re-asserted each run."""
    dirs = state.setdefault("sops", {}).setdefault("createdDirs", [])
    dirs[:] = [d for d in dirs if isinstance(d, str) and os.path.isdir(d)]
    fsutil.fix_private_dirs(dirs)
    return dirs


EARLY, LATE = "early", "late"


def paths_in_repos(git_repos_raw, sops_raw, templates_raw) -> Set[str]:
    """Declared sops/templates paths inside a ``gitRepos`` destination, plus
    secrets decrypted from a ``sopsFile`` inside one, and templates using
    such a secret, so each runs after what it needs exists.

    Taken before any clone: a checkout may bring in symlinks that move them.
    """
    roots = [_tree_key(_norm(d)) for d in _mapping(git_repos_raw) if isinstance(d, str) and d]
    if not roots:
        return set()
    parsed = sops.parse_sops(sops_raw)
    tmpls, _errors = templates.parse_templates(templates_raw, *parsed.secrets_dir())
    paths = [s.path for s in parsed.secrets] + [t.target for t in tmpls]

    def in_repo(path: str) -> bool:
        return any(fsutil.is_within(_tree_key(path), r) for r in roots)

    inside = {p for p in paths if in_repo(p)}
    inside |= {s.path for s in parsed.secrets if s.sops_file and in_repo(s.sops_file)}
    late_secrets = {s.name for s in parsed.secrets if s.path in inside}
    if late_secrets:
        inside |= {t.target for t in tmpls if templates.secret_refs(t) & late_secrets}
    return inside


def _split(phase: Optional[str], inside) -> dict:
    """plan() arguments: EARLY runs before gitRepos (paths outside every
    destination), LATE after it (paths inside, plus errors and removals)."""
    if phase is None:
        return {}
    if phase == EARLY:
        return {"select": lambda p: p not in inside, "tail": False}
    return {"select": lambda p: p in inside, "tail": True}


def _merge(report: RunReport, section: str, names: List[str], phase) -> None:
    if phase == LATE:
        report.changed[section] = report.changed.get(section, []) + names
    else:
        report.changed[section] = names


def deploy_sops(
    raw,
    state: Dict,
    report: RunReport,
    decryptor: Optional[sops.Decryptor] = None,
    conflicts: Optional[Mapping[str, str]] = None,
    *,
    phase: Optional[str] = None,
    inside=frozenset(),
):
    parsed = sops.parse_sops(raw)
    installed = _installed(state, "sops")
    p = sops.plan(
        parsed,
        installed,
        decryptor or sops.Decryptor(parsed.settings),
        conflicts=conflicts,
        **_split(phase, inside),
    )
    res = sops.apply(p, parsed.settings, installed, _private_dirs(state))
    state.setdefault("sops", {})["installed"] = installed
    _merge(report, "sops", [s.name for s in res.changed], phase)
    failed = tuple(res.failed)
    report.failed_secrets = report.failed_secrets + failed if phase == LATE else failed
    report.notify += [(n, f"sops/{s.name}") for s in res.changed for n in s.notify]
    return res.success


def deploy_templates(
    raw,
    sops_raw,
    state: Dict,
    report: RunReport,
    conflicts: Optional[Mapping[str, str]] = None,
    decryptor: Optional[sops.Decryptor] = None,
    *,
    phase: Optional[str] = None,
    inside=frozenset(),
) -> bool:
    parsed_sops = sops.parse_sops(sops_raw)
    tmpls, errors = templates.parse_templates(raw, *parsed_sops.secrets_dir())
    installed = _installed(state, "templates")
    lookup = sops.make_lookup(
        parsed_sops,
        decryptor=decryptor or sops.Decryptor(parsed_sops.settings),
        failed=report.failed_secrets,
        installed=_installed(state, "sops"),
    )
    p = templates.plan(
        tmpls,
        errors,
        installed,
        lookup,
        conflicts=conflicts,
        pending_errors=(sops.SecretUnavailable,),
        **_split(phase, inside),
    )
    res = templates.apply(p, installed, parsed_sops.secrets_dir()[0], _private_dirs(state))
    state.setdefault("templates", {})["installed"] = installed
    _merge(report, "templates", [t.name for t in res.changed], phase)
    report.notify += [(n, f"templates/{t.name}") for t in res.changed for n in t.notify]
    # In state at once, so a later phase failing or an interrupt still retries them.
    util.add_pending_hooks(
        state, "templates", {t.on_change: [t.target] for t in res.changed if t.on_change}
    )
    return res.success


def template_hooks(raw) -> set:
    """Every onChange in the raw templates config, parseable or not."""
    if not isinstance(raw, Mapping):
        return set()
    return {
        s["onChange"]
        for s in raw.values()
        if isinstance(s, Mapping) and isinstance(s.get("onChange"), str)
    }


def run_template_hooks(raw, state: Dict) -> bool:
    """Templates' onChange commands, once after every phase has written."""
    return util.settle_hooks(state, "templates", {}, template_hooks(raw))


def plan_sops(
    raw,
    state: Dict,
    decryptor: Optional[sops.Decryptor] = None,
    conflicts: Optional[Mapping[str, str]] = None,
    *,
    phase: Optional[str] = None,
    inside=frozenset(),
) -> sops.Plan:
    parsed = sops.parse_sops(raw)
    return sops.plan(
        parsed,
        _installed(state, "sops"),
        decryptor or sops.Decryptor(parsed.settings),
        conflicts=conflicts,
        **_split(phase, inside),
    )


def failed_secrets(p: sops.Plan) -> Tuple[str, ...]:
    """Secrets deploy would report failed for this plan (before any write)."""
    return tuple(ch.secret.name for ch in p.changes if ch.action == "error")


def diff_sops(
    raw,
    state: Dict,
    decryptor: Optional[sops.Decryptor] = None,
    conflicts: Optional[Mapping[str, str]] = None,
) -> List[str]:
    return sops.describe(plan_sops(raw, state, decryptor, conflicts))


def diff_templates(
    raw,
    sops_raw,
    state: Dict,
    decryptor: Optional[sops.Decryptor] = None,
    conflicts: Optional[Mapping[str, str]] = None,
    failed: Tuple[str, ...] = (),
    *,
    phase: Optional[str] = None,
    inside=frozenset(),
) -> List[str]:
    """``failed``: the sops plan's failed secrets when sops runs too, as in deploy."""
    parsed_sops = sops.parse_sops(sops_raw)
    tmpls, errors = templates.parse_templates(raw, *parsed_sops.secrets_dir())
    lookup = sops.make_lookup(
        parsed_sops,
        decryptor=decryptor or sops.Decryptor(parsed_sops.settings),
        failed=failed,
        installed=_installed(state, "sops"),
    )
    p = templates.plan(
        tmpls,
        errors,
        _installed(state, "templates"),
        lookup,
        conflicts=conflicts,
        pending_errors=(sops.SecretUnavailable,),
        **_split(phase, inside),
    )
    rows = templates.describe(p)
    if phase != EARLY:
        declared = template_hooks(raw)
        listed = {
            ch.template.on_change for ch in p.changes if ch.action in ("create", "update", "chmod")
        }
        rows += [
            f"  ~ run onChange: {cmd} (failed last deploy)"
            for cmd in util.pending_hooks(state, "templates", declared)
            if cmd not in listed
        ]
    return rows


def files_targets(entries, config_dir: str) -> List[str]:
    """Targets the current `files:` config declares (valid entries only)."""
    from tools.user.files import _resolve_entries

    resolved, _errors = _resolve_entries(entries or [], config_dir)
    return [e.target for e in resolved]


def format_notify(report: RunReport) -> List[str]:
    return [
        f"notify {target} (changed: {source}); no handler yet" for target, source in report.notify
    ]
