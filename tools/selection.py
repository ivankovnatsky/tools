"""`--only SECTION:NAME`: narrow a run to single items of a section.

A narrowed section sees only its selected items, in config and in state. Its
other state records are set aside before the section runs and put back after,
so nothing unselected is removed, restarted or forgotten. Selected names must
exist in config, so a narrowed section never removes anything either.
"""

import copy
import os
from typing import Dict, FrozenSet, List, Tuple

from tools.config import SECTIONS
from tools.fsutil import canonical_path
from tools.user import defaults as defaults_section
from tools.user import launchd as launchd_section
from tools.user import sops as sops_section
from tools.user import templates as templates_section
from tools.user.brew import _normalize_tap, parse_brew
from tools.user.files import _resolve_entries, declared_hooks
from tools.user.secrets_flow import template_hooks

SUPPORTED = ("brew", "defaults", "files", "launchd", "npm", "templates", "uv")

Only = Dict[str, FrozenSet[str]]


class SelectionError(Exception):
    pass


def parse(values) -> Only:
    """`SECTION:NAME` values into {section: names}."""
    only: Dict[str, set] = {}
    for value in values:
        section, sep, name = value.partition(":")
        if not sep or not name:
            raise SelectionError(f"--only {value!r}: expected SECTION:NAME")
        if section not in SECTIONS:
            raise SelectionError(f"--only {value!r}: unknown section {section!r}")
        if section not in SUPPORTED:
            raise SelectionError(
                f"--only {value!r}: {section} cannot be narrowed to items "
                f"(supported: {', '.join(SUPPORTED)}); use --scope {section}"
            )
        only.setdefault(section, set()).add(name)
    return {s: frozenset(names) for s, names in only.items()}


def combine(scope, only: Only) -> Tuple[str, ...]:
    """Sections a run covers: --scope ones whole, --only ones narrowed. Empty means all."""
    both = sorted(set(scope) & set(only))
    if both:
        raise SelectionError(
            f"{', '.join(both)}: given to both --scope (whole section) and --only; pick one"
        )
    return tuple(scope) + tuple(s for s in only if s not in scope)


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _bad_state(section: str) -> SelectionError:
    return SelectionError(
        f"state for {section} is malformed; run it whole (--scope {section}) first"
    )


class _Ctx:
    def __init__(self, config: dict, config_dir: str):
        self.config = config
        self.config_dir = config_dir

    def secrets_dir(self):
        return sops_section.parse_sops(self.config.get("sops")).secrets_dir()


# --------------------------------------------------------------------------
# Sections. Each: unmatched names, narrowed config, split/hide/restore state.
# --------------------------------------------------------------------------


class _Packages:
    """uv/npm: `packages` by name; ownership is tied to a tool dir or prefix."""

    def __init__(self, section: str, context_key: str, context):
        self.section = section
        self.context_key = context_key
        self.context = context

    def errors(self, raw, ctx):
        return []

    def unmatched(self, raw, names, ctx):
        return names - set(_dict(_dict(raw).get("packages")))

    def config(self, raw, names, ctx):
        packages = _dict(_dict(raw).get("packages"))
        # Not an item: `configFile` writes a dotfile once.
        out = {k: v for k, v in _dict(raw).items() if k != "configFile"}
        out["packages"] = {k: v for k, v in packages.items() if k in names}
        return out

    def split(self, state, view, ctx):
        sec = state.get(self.section)
        if sec is None:
            return {}
        if not isinstance(sec, dict) or not isinstance(sec.get("packages", {}), dict):
            raise _bad_state(self.section)
        wanted = set(view["packages"])
        stash = {k: v for k, v in (sec.get("packages") or {}).items() if k not in wanted}
        current = self.context(ctx.config.get("paths") or {})
        if stash and sec.get(self.context_key, current) != current:
            # The section would release every tracked package, the set-aside ones too.
            raise SelectionError(
                f"{self.section}: install location changed since the last deploy; "
                f"run it whole (--scope {self.section}) first"
            )
        return stash

    def hide(self, state, stash):
        if stash:
            packages = state[self.section]["packages"]
            state[self.section]["packages"] = {k: v for k, v in packages.items() if k not in stash}

    def restore(self, state, stash):
        if stash:
            sec = state.setdefault(self.section, {})
            sec["packages"] = {**(sec.get("packages") or {}), **stash}


_BREW_LISTS = ("brews", "casks", "taps", "trustedTaps")


class _Brew:
    def _names(self, raw):
        raw = _dict(raw)

        def strings(key, source=raw):
            value = source.get(key)
            return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []

        return {
            "brews": set(strings("brews")),
            "casks": set(strings("casks")),
            "taps": {_normalize_tap(t) for t in strings("taps")},
            "trust": {_normalize_tap(t) for t in strings("taps", _dict(raw.get("trust")))},
            "masApps": set(_dict(raw.get("masApps"))),
        }

    def errors(self, raw, ctx):
        return parse_brew(raw)[1]

    def unmatched(self, raw, names, ctx):
        n = self._names(raw)
        known = n["brews"] | n["casks"] | n["taps"] | n["trust"] | n["masApps"]
        return {x for x in names if x not in known and _normalize_tap(x) not in known}

    def config(self, raw, names, ctx):
        raw = _dict(raw)
        taps = {_normalize_tap(x) for x in names}
        out = dict(raw)
        for key, keep in (("brews", names), ("casks", names), ("taps", taps)):
            if isinstance(raw.get(key), list):
                out[key] = [v for v in raw[key] if v in keep or _normalize_tap(str(v)) in keep]
        if isinstance(raw.get("masApps"), dict):
            out["masApps"] = {k: v for k, v in raw["masApps"].items() if k in names}
        trust = raw.get("trust")
        if isinstance(trust, dict) and isinstance(trust.get("taps"), list):
            out["trust"] = {
                **trust,
                "taps": [t for t in trust["taps"] if _normalize_tap(str(t)) in taps],
            }
        environment = raw.get("environment")
        if environment is None or isinstance(environment, dict):
            # `brew upgrade` otherwise also upgrades installed dependents.
            out["environment"] = {
                **(environment or {}),
                "HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK": "1",
            }
        on_activation = raw.get("onActivation")
        if isinstance(on_activation, dict):
            # cleanup removes everything installed and not declared.
            out["onActivation"] = {k: v for k, v in on_activation.items() if k != "cleanup"}
        return out

    def split(self, state, view, ctx):
        prev = state.get("brew")
        if prev is None:
            return {}
        if not isinstance(prev, dict):
            raise _bad_state("brew")
        n = self._names(view)
        keep = {
            "brews": n["brews"],
            "casks": n["casks"],
            "taps": n["taps"],
            "trustedTaps": n["trust"],
        }
        stash: Dict = {}
        for key in _BREW_LISTS:
            value = prev.get(key, [])
            if not isinstance(value, list):
                raise _bad_state("brew")
            stash[key] = [
                v for v in value if (_normalize_tap(v) if "aps" in key else v) not in keep[key]
            ]
        mas = prev.get("masApps", {})
        if not isinstance(mas, dict):
            raise _bad_state("brew")
        wanted = _dict(view.get("masApps"))
        changed = sorted(k for k in n["masApps"] if k in mas and str(mas[k]) != str(wanted[k]))
        if changed:
            # Its old App Store id would be uninstalled.
            raise SelectionError(
                f"brew: {', '.join(changed)}: App Store id changed; run brew whole (--scope brew)"
            )
        stash["masApps"] = {k: v for k, v in mas.items() if k not in n["masApps"]}
        return stash

    def hide(self, state, stash):
        if not stash:
            return
        prev = state["brew"]
        for key in _BREW_LISTS:
            if key in prev:
                prev[key] = [v for v in prev[key] if v not in stash[key]]
        if "masApps" in prev:
            prev["masApps"] = {
                k: v for k, v in prev["masApps"].items() if k not in stash["masApps"]
            }

    def restore(self, state, stash):
        if not stash:
            return
        sec = state.setdefault("brew", {})
        for key in _BREW_LISTS:
            sec[key] = sorted(set(sec.get(key) or []) | set(stash[key]))
        sec["masApps"] = {**(sec.get("masApps") or {}), **stash["masApps"]}


def _split_hooks(state, section: str, declared, targets) -> Dict:
    """Pending hook paths that are not selected: a command shared with other entries
    must not run, or be cleared, for their paths."""
    canon = {canonical_path(t) for t in targets}
    pending = _dict(_dict(state.get("onChange")).get(section))
    stash = {}
    for cmd, paths in pending.items():
        if cmd not in declared or not isinstance(paths, list):
            stash[cmd] = paths
            continue
        other = [p for p in paths if not isinstance(p, str) or canonical_path(p) not in canon]
        if other:
            stash[cmd] = other
    return stash


def _hide_hooks(state, section: str, stash: Dict) -> None:
    if stash:
        pending = state["onChange"][section]
        for cmd, paths in stash.items():
            rest = pending[cmd] if isinstance(pending[cmd], list) else []
            rest = [p for p in rest if p not in paths] if isinstance(paths, list) else []
            if rest:
                pending[cmd] = rest
            else:
                pending.pop(cmd)


def _restore_hooks(state, section: str, stash: Dict) -> None:
    if stash:
        pending = state.setdefault("onChange", {}).setdefault(section, {})
        for cmd, paths in stash.items():
            known = pending.get(cmd)
            if isinstance(known, list) and isinstance(paths, list):
                pending[cmd] = known + [p for p in paths if p not in known]
            else:
                pending[cmd] = paths


def _split_paths(records, keep) -> Dict:
    canon = {canonical_path(p) for p in keep}
    return {
        k: v for k, v in records.items() if not isinstance(k, str) or canonical_path(k) not in canon
    }


def _resolved_files(entries, config_dir: str):
    """User and system (root-owned) entries alike: --only names either kind."""
    return [e for system in (False, True) for e in _resolve_entries(entries, config_dir, system)[0]]


class _Files:
    def errors(self, raw, ctx):
        errors = [
            e for system in (False, True) for e in _resolve_entries(raw, ctx.config_dir, system)[1]
        ]
        return list(dict.fromkeys(errors))

    def unmatched(self, raw, names, ctx):
        entries = raw if isinstance(raw, list) else []
        targets = {canonical_path(e.target) for e in _resolved_files(entries, ctx.config_dir)}
        dirs = {
            os.path.normpath(str(e["dir"])) for e in entries if isinstance(e, dict) and "dir" in e
        }
        return {
            n for n in names if os.path.normpath(n) not in dirs and canonical_path(n) not in targets
        }

    def config(self, raw, names, ctx):
        entries = raw if isinstance(raw, list) else []
        want = {canonical_path(n) for n in names}
        dirs = {os.path.normpath(n) for n in names}
        for entry in entries:
            if isinstance(entry, dict) and "dir" in entry:
                if os.path.normpath(str(entry["dir"])) in dirs:
                    tree = _resolved_files([entry], ctx.config_dir)
                    want |= {canonical_path(e.target) for e in tree}
        # Resolved from the whole list, so precedence and sticky `secrets` match a full run.
        out: List[Dict] = []
        for e in _resolved_files(entries, ctx.config_dir):
            if canonical_path(e.target) not in want:
                continue
            item: Dict = {"target": e.target}
            if e.directory:
                item["directory"] = True
            else:
                item["source"] = e.source
            if e.mode is not None:
                item["mode"] = f"{e.mode:04o}"
            if e.secrets:
                item["secrets"] = True
            if e.on_change:
                item["onChange"] = e.on_change
            if e.on_update:
                item["onUpdate"] = e.on_update
            if e.skip:
                item["skip"] = True
            if not e.backup:
                item["backup"] = False
            out.append(item)
        return out

    def split(self, state, view, ctx):
        records = state.get("files", {})
        if not isinstance(records, dict):
            raise _bad_state("files")
        targets = [e.target for e in _resolved_files(view, ctx.config_dir)]
        declared = declared_hooks(view) | declared_hooks(view, skipped=True)
        return {
            "records": _split_paths(records or {}, targets),
            "hooks": _split_hooks(state, "files", declared, targets),
        }

    def hide(self, state, stash):
        if stash["records"]:
            state["files"] = {k: v for k, v in state["files"].items() if k not in stash["records"]}
        _hide_hooks(state, "files", stash["hooks"])

    def restore(self, state, stash):
        if stash["records"]:
            state["files"] = {**(state.get("files") or {}), **stash["records"]}
        _restore_hooks(state, "files", stash["hooks"])


class _Templates:
    def errors(self, raw, ctx):
        return templates_section.parse_templates(raw, *ctx.secrets_dir())[1]

    def unmatched(self, raw, names, ctx):
        return names - set(_dict(raw))

    def config(self, raw, names, ctx):
        return {k: v for k, v in _dict(raw).items() if k in names}

    def split(self, state, view, ctx):
        sec = state.get("templates", {})
        if not isinstance(sec, dict) or not isinstance(sec.get("installed", {}), dict):
            raise _bad_state("templates")
        tmpls, _errors = templates_section.parse_templates(view, *ctx.secrets_dir())
        targets = [t.target for t in tmpls]
        return {
            "records": _split_paths(sec.get("installed") or {}, targets),
            "hooks": _split_hooks(state, "templates", template_hooks(view), targets),
        }

    def hide(self, state, stash):
        if stash["records"]:
            sec = state["templates"]
            sec["installed"] = {
                k: v for k, v in sec["installed"].items() if k not in stash["records"]
            }
        _hide_hooks(state, "templates", stash["hooks"])

    def restore(self, state, stash):
        if stash["records"]:
            sec = state.setdefault("templates", {})
            sec["installed"] = {**(sec.get("installed") or {}), **stash["records"]}
        _restore_hooks(state, "templates", stash["hooks"])


_LAUNCHD_GROUPS = ("agents", "daemons")


class _Launchd:
    def _jobs(self, raw):
        raw = _dict(raw)
        prefix = raw.get("labelPrefix")
        for group in _LAUNCHD_GROUPS:
            for name, job in _dict(raw.get(group)).items():
                label = _dict(job).get("label")
                if not isinstance(label, str):
                    label = f"{prefix}.{name}" if isinstance(prefix, str) and prefix else None
                yield group, name, label

    def errors(self, raw, ctx):
        if raw is None:
            return []
        env = launchd_section.Env()
        kwargs = sops_section.secrets_dir_kwargs(ctx.config.get("sops"))
        settings, services, errors = launchd_section.parse_launchd(
            raw, env.home, env.login, **kwargs
        )
        if errors:
            return errors
        # plan() checks this only among the jobs it is given.
        clash = launchd_section._shared_logs(services, settings, env)
        return [
            f"{svc.name}: log path {', '.join(clash[svc.label])} is shared"
            for svc in services
            if svc.label in clash
        ]

    def unmatched(self, raw, names, ctx):
        known = set()
        for _group, name, label in self._jobs(raw):
            known |= {name, label}
        return names - known

    def config(self, raw, names, ctx):
        raw = _dict(raw)
        picked = {(g, n) for g, n, label in self._jobs(raw) if n in names or label in names}
        out = dict(raw)
        for group in _LAUNCHD_GROUPS:
            if isinstance(raw.get(group), dict):
                out[group] = {n: j for n, j in raw[group].items() if (group, n) in picked}
        return out

    def split(self, state, view, ctx):
        sec = state.get("launchd")
        if sec is None:
            return {}
        if not isinstance(sec, dict) or not isinstance(sec.get("services", {}), (dict, type(None))):
            raise _bad_state("launchd")
        labels = {label for _g, _n, label in self._jobs(view)}
        return {k: v for k, v in (sec.get("services") or {}).items() if k not in labels}

    def hide(self, state, stash):
        if stash:
            sec = state["launchd"]
            sec["services"] = {k: v for k, v in sec["services"].items() if k not in stash}

    def restore(self, state, stash):
        if stash:
            sec = state.setdefault("launchd", {})
            sec["services"] = {**(sec.get("services") or {}), **stash}


class _Defaults:
    def _matches(self, domain, names) -> bool:
        if not isinstance(domain, str):
            return False
        norm = defaults_section.normalize_domain(domain.strip())
        return domain in names or norm in {defaults_section.normalize_domain(n) for n in names}

    def errors(self, raw, ctx):
        return defaults_section.identify(*defaults_section.parse_defaults(raw))[1]

    def unmatched(self, raw, names, ctx):
        domains = [d for s in defaults_section.SCOPES for d in _dict(_dict(raw).get(s))]
        return {n for n in names if not any(self._matches(d, {n}) for d in domains)}

    def config(self, raw, names, ctx):
        raw = _dict(raw)
        out = dict(raw)
        for scope in defaults_section.SCOPES:
            if isinstance(raw.get(scope), dict):
                out[scope] = {d: k for d, k in raw[scope].items() if self._matches(d, names)}
        return out

    def split(self, state, view, ctx):
        sec = state.get("defaults")
        if sec is None:
            return {}
        if not isinstance(sec, dict):
            raise _bad_state("defaults")
        migrated = defaults_section.migrate_state(copy.deepcopy(sec))
        spec, _errors = defaults_section.identify(*defaults_section.parse_defaults(view))
        wanted = {k.sid for k in spec.keys}
        managed: Dict = {}
        stash: Dict = {}
        for sid, entry in defaults_section._tracked_entries(migrated["managed"]):
            scope, ident, key = sid
            target = managed if sid in wanted else stash
            target.setdefault(scope, {}).setdefault(ident, {})[key] = entry
        selected = {sid[:2] for sid in wanted}
        restart = {
            scope: {i: p for i, p in idents.items() if (scope, i) not in selected}
            for scope, idents in migrated["restart"].items()
        }
        return {
            "view": managed,
            "managed": stash,
            "view_restart": migrated["restart"],
            "restart": restart,
        }

    def hide(self, state, stash):
        if stash:
            sec = state["defaults"]
            sec["managed"] = stash["view"]
            sec["restart"] = copy.deepcopy(stash["view_restart"])

    def restore(self, state, stash):
        if not stash:
            return
        sec = state.setdefault("defaults", {})
        managed = sec.setdefault("managed", {})
        for scope, idents in stash["managed"].items():
            for ident, keys in idents.items():
                managed.setdefault(scope, {}).setdefault(ident, {}).update(keys)
        restart = sec.setdefault("restart", {})
        for scope, idents in stash["restart"].items():
            for ident, procs in idents.items():
                if ident in managed.get(scope, {}) and ident not in restart.get(scope, {}):
                    restart.setdefault(scope, {})[ident] = procs


_SECTIONS = {
    "brew": _Brew(),
    "defaults": _Defaults(),
    "files": _Files(),
    "launchd": _Launchd(),
    "npm": _Packages("npm", "prefix", lambda paths: paths.get("npmBin")),
    "templates": _Templates(),
    "uv": _Packages(
        "uv", "context", lambda paths: f"{paths.get('uvBin')}|{paths.get('uvToolDir')}"
    ),
}
assert set(_SECTIONS) == set(SUPPORTED)


def unmatched(only: Only, config: dict, config_dir: str) -> List[str]:
    """Config errors of a narrowed section (judged on the whole section, as a full run
    does), and one error per selected name that names nothing in config."""
    ctx = _Ctx(config, config_dir)
    out = []
    for section, names in sorted(only.items()):
        raw = config.get(section)
        errors = _SECTIONS[section].errors(raw, ctx)
        out += [f"--only {section}: fix the {section} config first: {e}" for e in errors]
        if not errors:
            out += [
                f"--only {section}:{name}: no such item in the {section} config"
                for name in sorted(_SECTIONS[section].unmatched(raw, set(names), ctx))
            ]
    return out


def state_errors(only: Only, config: dict, config_dir: str, state: dict) -> List[str]:
    """Why the records of a narrowed section can't be set aside in this state. Read-only."""
    view = View(only, config, config_dir)
    out = []
    for section in sorted(only):
        try:
            _SECTIONS[section].split(copy.deepcopy(state), view.config[section], view.ctx)
        except SelectionError as exc:
            out.append(str(exc))
    return out


class View:
    """The narrowed config, and the state records set aside while sections run."""

    def __init__(self, only: Only, config: dict, config_dir: str):
        self.only = only
        self.ctx = _Ctx(config, config_dir)
        self.config = dict(config)
        for section, names in only.items():
            self.config[section] = _SECTIONS[section].config(config.get(section), names, self.ctx)
        self._stash = None

    def hide(self, state: dict) -> None:
        """Set aside every unselected record. All or nothing: raises before any change."""
        stash = {s: _SECTIONS[s].split(state, self.config[s], self.ctx) for s in sorted(self.only)}
        for section, part in stash.items():
            _SECTIONS[section].hide(state, part)
        self._stash = stash

    def restore(self, state: dict) -> None:
        if self._stash is None:
            return
        for section, part in self._stash.items():
            _SECTIONS[section].restore(state, part)
        self._stash = None

    def merged(self, state: dict) -> dict:
        """state with the set-aside records back in, as a copy (state itself when none are)."""
        if self._stash is None:
            return state
        out = copy.deepcopy(state)
        for section, part in self._stash.items():
            _SECTIONS[section].restore(out, part)
        return out


def view(only: Only, config: dict, config_dir: str):
    return View(only, config, config_dir) if only else None
