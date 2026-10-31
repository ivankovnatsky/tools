"""Wire sops and templates to tools' state file, shared by deploy and diff.

The providers themselves take parsed resources and injected callables;
this module is the only place that knows their state keys and how the
two sections feed each other.
"""

import os
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Set, Tuple

from tools import fsutil
from tools.user import sops, templates


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

    def __init__(self, exact=(), links: Optional[Dict[str, str]] = None, trees=None):
        super().__init__(exact)
        self._exact = {_link_key(p): reason for p, reason in dict(exact).items()}
        self.links = {_link_key(p): (p, owner) for p, owner in (links or {}).items()}
        self.trees = {_tree_key(p): (p, owner) for p, owner in (trees or {}).items()}

    def __bool__(self) -> bool:
        return bool(len(self) or self.links or self.trees)

    def reason(self, path: str) -> Optional[str]:
        n = _norm(path)
        exact = self._exact.get(_link_key(n))
        return exact if exact is not None else self.managed_reason(n)

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


def ownership_conflicts(
    *,
    files_entries=None,
    sops_raw=None,
    templates_raw=None,
    links_raw=None,
    skills_raw=None,
    state: Optional[Dict] = None,
    config_dir: str = ".",
) -> Ownership:
    """Paths every file-writing section (files, sops, templates) refuses.

    A path claimed by more than one of them, declared now or tracked in
    state, is refused by all, so two owners never overwrite each other. A
    claim that exists only in state frees itself: its owner removes or
    forgets it on that deploy. Symlinks and directories that `links` and
    `skills` manage are refused too, including writes through them.
    """
    state = state or {}
    claims: Dict[str, Set[str]] = {}
    spelled: Dict[str, str] = {}

    def claim(path, section: str) -> None:
        if isinstance(path, str) and path:
            key = _link_key(path)
            claims.setdefault(key, set()).add(section)
            spelled.setdefault(key, _norm(path))

    for path in files_targets(files_entries, config_dir):
        claim(path, "files")
    for path in state.get("files", {}) or {}:
        claim(path, "files")
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
    exact = {
        spelled[path]: "claimed by " + " and ".join(sorted(sections))
        for path, sections in claims.items()
        if len(sections) > 1
    }
    # A managed file may not be a directory on another managed file's path.
    for key in claims:
        child, anc = key, os.path.dirname(key)
        while anc != child:
            if anc in claims:
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
    link_paths, trees = _link_owned_paths(links_raw, skills_raw, state, config_dir)
    return Ownership(exact, link_paths, trees)


def _private_dirs(state: Dict) -> list:
    """Directories tools created inside secretsDir; their mode is re-asserted each run."""
    dirs = state.setdefault("sops", {}).setdefault("createdDirs", [])
    dirs[:] = [d for d in dirs if isinstance(d, str) and os.path.isdir(d)]
    fsutil.fix_private_dirs(dirs)
    return dirs


def deploy_sops(
    raw,
    state: Dict,
    report: RunReport,
    decryptor: Optional[sops.Decryptor] = None,
    conflicts: Optional[Mapping[str, str]] = None,
):
    parsed = sops.parse_sops(raw)
    installed = _installed(state, "sops")
    p = sops.plan(
        parsed, installed, decryptor or sops.Decryptor(parsed.settings), conflicts=conflicts
    )
    res = sops.apply(p, parsed.settings, installed, _private_dirs(state))
    state.setdefault("sops", {})["installed"] = installed
    report.changed["sops"] = [s.name for s in res.changed]
    report.failed_secrets = tuple(res.failed)
    report.notify += [(n, f"sops/{s.name}") for s in res.changed for n in s.notify]
    return res.success


def deploy_templates(
    raw,
    sops_raw,
    state: Dict,
    report: RunReport,
    conflicts: Optional[Mapping[str, str]] = None,
    decryptor: Optional[sops.Decryptor] = None,
) -> bool:
    parsed_sops = sops.parse_sops(sops_raw)
    tmpls, errors = templates.parse_templates(raw, *parsed_sops.secrets_dir())
    installed = _installed(state, "templates")
    lookup = sops.make_lookup(
        parsed_sops,
        decryptor=decryptor or sops.Decryptor(parsed_sops.settings),
        failed=report.failed_secrets,
    )
    p = templates.plan(
        tmpls,
        errors,
        installed,
        lookup,
        conflicts=conflicts,
        pending_errors=(sops.SecretUnavailable,),
    )
    res = templates.apply(p, installed, parsed_sops.secrets_dir()[0], _private_dirs(state))
    state.setdefault("templates", {})["installed"] = installed
    report.changed["templates"] = [t.name for t in res.changed]
    report.notify += [(n, f"templates/{t.name}") for t in res.changed for n in t.notify]
    return res.success


def plan_sops(
    raw,
    state: Dict,
    decryptor: Optional[sops.Decryptor] = None,
    conflicts: Optional[Mapping[str, str]] = None,
) -> sops.Plan:
    parsed = sops.parse_sops(raw)
    return sops.plan(
        parsed,
        _installed(state, "sops"),
        decryptor or sops.Decryptor(parsed.settings),
        conflicts=conflicts,
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
) -> List[str]:
    """``failed``: the sops plan's failed secrets when sops runs too, as in deploy."""
    parsed_sops = sops.parse_sops(sops_raw)
    tmpls, errors = templates.parse_templates(raw, *parsed_sops.secrets_dir())
    lookup = sops.make_lookup(
        parsed_sops, decryptor=decryptor or sops.Decryptor(parsed_sops.settings), failed=failed
    )
    p = templates.plan(
        tmpls,
        errors,
        _installed(state, "templates"),
        lookup,
        conflicts=conflicts,
        pending_errors=(sops.SecretUnavailable,),
    )
    return templates.describe(p)


def files_targets(entries, config_dir: str) -> List[str]:
    """Targets the current `files:` config declares (valid entries only)."""
    from tools.user.files import _resolve_entries

    resolved, _errors = _resolve_entries(entries or [], config_dir)
    return [target for target, _source, _mode, _secrets in resolved]


def format_notify(report: RunReport) -> List[str]:
    return [
        f"notify {target} (changed: {source}); no handler yet" for target, source in report.notify
    ]
