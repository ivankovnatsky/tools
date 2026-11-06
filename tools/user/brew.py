import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from tools.log import Color, debug, log
from tools.system_paths import system_bin, system_bin_optional
from tools.util import run_command

_BREW_INSTALL_URL = "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"


def _normalize_tap(tap: str) -> str:
    """Normalize tap name: user/homebrew-foo -> user/foo."""
    parts = tap.split("/", 1)
    if len(parts) == 2 and parts[1].startswith("homebrew-"):
        return f"{parts[0]}/{parts[1][len('homebrew-') :]}"
    return tap


def _brew_bin() -> str | None:
    """Return the brew binary path, checking system_paths first then PATH."""
    path = system_bin_optional("brew")
    if path:
        return path
    return shutil.which("brew")


def _bootstrap_brew() -> str | None:
    """Install Homebrew non-interactively. Returns brew path or None on failure.

    WARNING: This runs a script from the internet with elevated privileges.
    The installer may prompt for sudo. Only runs on macOS and only when
    brew is completely absent from the system.
    """
    if sys.platform != "darwin":
        log("brew bootstrap is only supported on macOS", Color.RED)
        return None

    log("Installing Homebrew...", Color.GREEN)
    curl = system_bin("curl")
    bash = system_bin("bash")

    # Fetch the installer first, then hand its body to bash. Passing the
    # literal `$(curl ...)` as the -c script would make *bash* substitute
    # and word-split the downloaded script, then try to execute its first
    # token ("#!/bin/bash") as a command name — rc 127, every time.
    rc, script, stderr = run_command([curl, "-fsSL", _BREW_INSTALL_URL])
    if rc != 0 or not script.strip():
        log(f"Failed to download Homebrew installer: {stderr}", Color.RED)
        return None

    rc, stdout, stderr = run_command(
        [bash, "-c", script],
        {**os.environ.copy(), "NONINTERACTIVE": "1"},
    )
    if rc != 0:
        log(f"Failed to install Homebrew: {stderr}", Color.RED)
        return None

    log("Homebrew installed successfully", Color.GREEN)
    return _brew_bin()


def _mas_bin_from_env(env: dict) -> str | None:
    """Return the mas binary path, checking system_paths, then env PATH."""
    path = system_bin_optional("mas")
    if path:
        return path
    return shutil.which("mas", path=env.get("PATH", ""))


def _installed_subset(brew: str, env: dict, candidates: list[str], kind: str) -> set[str] | None:
    """Which of `candidates` brew currently reports as installed.

    Only used to repair bookkeeping after a batch command failed partway;
    the happy path never queries brew. Returns None when brew itself cannot
    be queried — "unknown" must not be conflated with "none installed", or
    a failed uninstall would silently drop ownership of packages that are
    still on disk.
    """
    rc, stdout, _ = run_command([brew, "list", kind, "-1"], env)
    if rc != 0:
        return None
    present = {line.strip() for line in stdout.splitlines() if line.strip()}
    return present & set(candidates)


_KEYS = {"brews", "casks", "taps", "masApps", "environment", "caskArgs", "onActivation", "trust"}
_CASK_ARGS_KEYS = {"no_quarantine"}
_ON_ACTIVATION_KEYS = {"autoUpdate", "cleanup"}
_TRUST_KEYS = {"taps"}
CLEANUP_MODES = ("uninstall", "zap")

_FORMULA_NAME_KEYS = ("name", "full_name", "aliases", "oldnames")
_CASK_NAME_KEYS = ("token", "full_token", "old_tokens")
# brew bundle never untaps homebrew/core; cleanup also leaves the other built-in taps.
_CORE_TAP = "homebrew/core"
_CLEANUP_KEPT_TAPS = {_CORE_TAP, "homebrew/cask", "homebrew/bundle"}


@dataclass
class BrewSpec:
    brews: Set[str] = field(default_factory=set)
    casks: Set[str] = field(default_factory=set)
    taps: Set[str] = field(default_factory=set)
    mas: Dict[str, Any] = field(default_factory=dict)
    environment: Dict[str, Any] = field(default_factory=dict)
    no_quarantine: bool = False
    auto_update: Optional[bool] = None
    cleanup: Optional[str] = None
    trusted_taps: Set[str] = field(default_factory=set)


def _str_list(raw: Dict, key: str, where: str, errors: List[str]) -> List[str]:
    value = raw.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        errors.append(f"{where} must be a list of non-empty strings")
        return []
    return value


def _sub_mapping(raw: Dict, key: str, allowed: Set[str], errors: List[str]) -> Dict:
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        errors.append(f"{key} must be a mapping, got {type(value).__name__}")
        return {}
    unknown = set(value) - allowed
    if unknown:
        errors.append(f"{key}: unknown keys {sorted(unknown)} (expected {sorted(allowed)})")
    return value


def _bool(raw: Dict, key: str, where: str, errors: List[str]) -> Optional[bool]:
    value = raw.get(key)
    if value is None or isinstance(value, bool):
        return value
    errors.append(f"{where} must be true or false, got {value!r}")
    return None


def parse_brew(raw) -> Tuple[BrewSpec, List[str]]:
    """Validate a raw `brew` mapping. Returns (spec, errors); any error empties the spec."""
    if raw is None:
        return BrewSpec(), []
    if not isinstance(raw, dict):
        return BrewSpec(), [f"brew must be a mapping, got {type(raw).__name__}"]
    errors: List[str] = []
    unknown = set(raw) - _KEYS
    if unknown:
        errors.append(f"unknown keys {sorted(unknown)} (expected {sorted(_KEYS)})")

    mas = raw.get("masApps") or {}
    if not isinstance(mas, dict) or not all(
        isinstance(k, str) and not isinstance(v, bool) and str(v).isdigit() for k, v in mas.items()
    ):
        errors.append("masApps must map app names to numeric App Store ids")
        mas = {}
    environment = raw.get("environment") or {}
    if not isinstance(environment, dict):
        errors.append(f"environment must be a mapping, got {type(environment).__name__}")
        environment = {}

    cask_args = _sub_mapping(raw, "caskArgs", _CASK_ARGS_KEYS, errors)
    on_activation = _sub_mapping(raw, "onActivation", _ON_ACTIVATION_KEYS, errors)
    trust = _sub_mapping(raw, "trust", _TRUST_KEYS, errors)
    cleanup = on_activation.get("cleanup")
    if cleanup is not None and cleanup not in CLEANUP_MODES:
        errors.append(f"onActivation.cleanup must be one of {list(CLEANUP_MODES)}, got {cleanup!r}")

    spec = BrewSpec(
        brews=set(_str_list(raw, "brews", "brews", errors)),
        casks=set(_str_list(raw, "casks", "casks", errors)),
        taps={_normalize_tap(t) for t in _str_list(raw, "taps", "taps", errors)},
        mas=dict(mas),
        environment=dict(environment),
        no_quarantine=bool(_bool(cask_args, "no_quarantine", "caskArgs.no_quarantine", errors)),
        auto_update=_bool(on_activation, "autoUpdate", "onActivation.autoUpdate", errors),
        cleanup=cleanup,
        trusted_taps={_normalize_tap(t) for t in _str_list(trust, "taps", "trust.taps", errors)},
    )
    if errors:
        return BrewSpec(), errors
    return spec, []


def _plan(spec: BrewSpec, prev: Dict) -> Dict[str, list]:
    """State-only diff: desired config against what tools recorded installing."""
    prev_brews = set(prev.get("brews", []))
    prev_casks = set(prev.get("casks", []))
    prev_taps = {_normalize_tap(t) for t in prev.get("taps", [])}
    prev_mas = dict(prev.get("masApps", {}))
    prev_trust = set(prev.get("trustedTaps", []))
    prev_ids = {str(v) for v in prev_mas.values()}
    desired_ids = {str(v) for v in spec.mas.values()}
    return {
        "taps_add": sorted(spec.taps - prev_taps),
        "taps_remove": sorted(t for t in prev_taps - spec.taps if t.lower() != _CORE_TAP),
        "trust_add": sorted(spec.trusted_taps - prev_trust),
        "trust_remove": sorted(prev_trust - spec.trusted_taps),
        "brews_install": sorted(spec.brews - prev_brews),
        "brews_remove": sorted(prev_brews - spec.brews),
        "casks_install": sorted(spec.casks - prev_casks),
        "casks_remove": sorted(prev_casks - spec.casks),
        "mas_install": sorted((n, i) for n, i in spec.mas.items() if str(i) not in prev_ids),
        "mas_remove": sorted((n, i) for n, i in prev_mas.items() if str(i) not in desired_ids),
    }


def _brew_env(brew: str, spec: BrewSpec, update: bool = False) -> Dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{os.path.dirname(brew)}:{env.get('PATH', '')}"
    if not update:
        # `brew install` otherwise upgrades an already-installed target and
        # the installed dependents of anything it upgrades.
        env["HOMEBREW_NO_INSTALL_UPGRADE"] = "1"
        env["HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK"] = "1"
    if spec.auto_update is False:
        env["HOMEBREW_NO_AUTO_UPDATE"] = "1"
    elif spec.auto_update:
        env.pop("HOMEBREW_NO_AUTO_UPDATE", None)
    for key, value in spec.environment.items():
        env[key] = str(value)
    return env


def _variants(names) -> Set[str]:
    """Each name plus its short form, so `user/tap/foo` and `foo` match each other."""
    return {v for n in names for v in (n, n.rsplit("/", 1)[-1])}


def _entry_names(entry: Dict, keys: Tuple[str, ...]) -> Set[str]:
    names = []
    for key in keys:
        value = entry.get(key)
        names += value if isinstance(value, list) else [value]
    return _variants(n for n in names if isinstance(n, str) and n)


def _lines(text: str) -> List[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _formula_deps(formula: Dict) -> List[str]:
    installed = formula.get("installed") or [{}]
    runtime = installed[0].get("runtime_dependencies")
    if runtime is not None:
        return [d["full_name"] for d in runtime if isinstance(d, dict) and d.get("full_name")]
    return list(formula.get("dependencies") or [])


def _cask_deps(cask: Dict) -> Tuple[List[str], List[str]]:
    depends_on = cask.get("depends_on") or {}
    return list(depends_on.get("formula") or []), list(depends_on.get("cask") or [])


def query_installed(brew: str, env: Dict, spec: BrewSpec) -> Tuple[Optional[Dict], Optional[str]]:
    """Read-only snapshot of live brew for cleanup. (None, error) when brew can't be queried.

    `pending` holds the dependencies of configured packages not installed yet,
    so cleanup never removes something an install in the same run needs.
    `deferred` lists configured taps not tapped yet: their packages' deps can't
    be resolved, so cleanup waits for the deploy after they are added.
    """
    out = {}
    for key, args in (
        ("leaves", ["leaves"]),
        ("taps", ["tap"]),
        ("info", ["info", "--json=v2", "--installed"]),
    ):
        rc, stdout, stderr = run_command([brew] + args, env)
        if rc != 0:
            return None, f"`brew {' '.join(args)}` failed: {stderr.strip()}"
        out[key] = stdout
    try:
        info = json.loads(out["info"])
    except ValueError as exc:
        return None, f"cannot parse `brew info --json=v2 --installed`: {exc}"
    formulae = info.get("formulae") or []
    casks = info.get("casks") or []

    live_taps = _lines(out["taps"])
    snapshot = {
        "leaves": _lines(out["leaves"]),
        "taps": live_taps,
        "formulae": formulae,
        "casks": casks,
        "pending": [],
        "deferred": [],
    }

    have_brews = set().union(*(_entry_names(f, _FORMULA_NAME_KEYS) for f in formulae))
    have_casks = set().union(*(_entry_names(c, _CASK_NAME_KEYS) for c in casks))
    missing_brews = sorted(n for n in spec.brews if not _variants([n]) & have_brews)
    missing_casks = sorted(n for n in spec.casks if not _variants([n]) & have_casks)

    tapped = {t.lower() for t in live_taps}
    wanted_taps = {t.lower() for t in spec.taps}
    wanted_taps |= {t for t in map(_tap_of, missing_brews + missing_casks) if t}
    if wanted_taps - tapped:
        snapshot["deferred"] = sorted(wanted_taps - tapped)
        return snapshot, None

    pending: Set[str] = set()
    formula_roots = set(missing_brews)
    to_resolve, seen = list(missing_casks), set()
    while to_resolve:
        args = ["info", "--json=v2", "--cask"] + to_resolve
        rc, stdout, stderr = run_command([brew] + args, env)
        if rc != 0:
            return None, f"`brew {' '.join(args)}` failed: {stderr.strip()}"
        try:
            entries = json.loads(stdout).get("casks") or []
        except ValueError as exc:
            return None, f"cannot parse `brew {' '.join(args[:3])}`: {exc}"
        seen |= set(to_resolve)
        to_resolve = []
        for entry in entries:
            formula_deps, cask_deps = _cask_deps(entry)
            formula_roots |= set(formula_deps)
            pending |= set(formula_deps) | set(cask_deps)
            to_resolve += [
                d for d in cask_deps if d not in seen and not _variants([d]) & have_casks
            ]
        to_resolve = sorted(set(to_resolve))
    if formula_roots:
        args = ["deps", "--formula", "--union", "--full-name"] + sorted(formula_roots)
        rc, stdout, stderr = run_command([brew] + args, env)
        if rc != 0:
            return None, f"`brew {' '.join(args)}` failed: {stderr.strip()}"
        pending |= set(_lines(stdout))
    snapshot["pending"] = sorted(pending)
    return snapshot, None


def _tap_of(name: str) -> Optional[str]:
    return _normalize_tap(name.rsplit("/", 1)[0]).lower() if name.count("/") == 2 else None


def cleanup_targets(spec: BrewSpec, prev: Dict, plan: Dict, live: Dict) -> Dict[str, List[str]]:
    """Installed items that are neither in config nor recorded in state.

    Owned items stay with the state plan, so nothing here duplicates it.
    Nothing a remaining package (or a pending install) depends on is targeted.
    `orphans` are dependency-only formulae nothing remaining needs, in
    removal order; they replace `brew autoremove` so diff can list them.
    A tap stays while it is configured, trusted, owned, or still provides a package.
    """
    if live.get("deferred"):
        return _no_cleanup()
    # nix-darwin installs `mas` alongside masApps; it is needed to manage them.
    implied = {"mas"} if spec.mas or plan["mas_remove"] else set()
    keep_brews = _variants(
        spec.brews | set(prev.get("brews", [])) | set(live.get("pending", [])) | implied
    )
    keep_casks = _variants(spec.casks | set(prev.get("casks", [])) | set(live.get("pending", [])))
    removed_brews = _variants(plan["brews_remove"])
    removed_casks = _variants(plan["casks_remove"])

    casks = {c.get("full_token") or c["token"]: c for c in live["casks"] if c.get("token")}
    cask_names = {name: _entry_names(c, _CASK_NAME_KEYS) for name, c in casks.items()}
    needed_casks: Set[str] = set()
    queue = [
        n for n, names in cask_names.items() if names & keep_casks and not names & removed_casks
    ]
    while queue:
        name = queue.pop()
        if name in needed_casks:
            continue
        needed_casks.add(name)
        for dep in _cask_deps(casks[name])[1]:
            queue += [n for n, names in cask_names.items() if _variants([dep]) & names]
    rm_casks = sorted(
        n for n, names in cask_names.items() if n not in needed_casks and not names & keep_casks
    )
    gone_casks = set(rm_casks) | {n for n, names in cask_names.items() if names & removed_casks}

    formulae = {f.get("full_name") or f["name"]: f for f in live["formulae"] if f.get("name")}
    names_of = {name: _entry_names(f, _FORMULA_NAME_KEYS) for name, f in formulae.items()}

    def lookup(dep: str) -> List[str]:
        return [n for n, names in names_of.items() if _variants([dep]) & names]

    candidates = set()
    for leaf in live["leaves"]:
        match = lookup(leaf) or [leaf]
        names = set().union(*(names_of.get(m, _variants([m])) for m in match))
        if not names & keep_brews:
            candidates |= set(match)

    def on_request(name: str) -> bool:
        installed = formulae[name].get("installed") or [{}]
        return installed[0].get("installed_on_request") is not False

    roots = [
        n
        for n in formulae
        if n not in candidates
        and not names_of[n] & removed_brews
        and (on_request(n) or names_of[n] & keep_brews)
    ]
    for name in casks:
        if name not in gone_casks:
            roots += [m for dep in _cask_deps(casks[name])[0] for m in lookup(dep)]
    needed: Set[str] = set()
    while roots:
        name = roots.pop()
        if name in needed:
            continue
        needed.add(name)
        roots += [m for dep in _formula_deps(formulae[name]) for m in lookup(dep)]
    rm_brews = sorted(candidates - needed)

    leftover = {
        n
        for n in formulae
        if n not in needed
        and n not in candidates
        and not names_of[n] & (keep_brews | removed_brews)
    }
    orphans: List[str] = []
    while leftover:
        users = {m for n in leftover for dep in _formula_deps(formulae[n]) for m in lookup(dep)}
        ready = sorted(leftover - users) or sorted(leftover)
        orphans += ready
        leftover -= set(ready)

    gone_brews = (
        set(rm_brews) | set(orphans) | {n for n, names in names_of.items() if names & removed_brews}
    )
    kept_taps = {t.lower() for t in spec.taps | spec.trusted_taps}
    kept_taps |= {
        _normalize_tap(t).lower() for t in prev.get("taps", []) + prev.get("trustedTaps", [])
    }
    kept_taps |= {
        t for t in map(_tap_of, spec.brews | spec.casks | set(live.get("pending", []))) if t
    }
    for entries, gone in ((formulae, gone_brews), (casks, gone_casks)):
        for name, entry in entries.items():
            if entry.get("tap") and name not in gone:
                kept_taps.add(entry["tap"].lower())
    kept_taps |= _CLEANUP_KEPT_TAPS
    taps = sorted(t for t in live["taps"] if t.lower() not in kept_taps)

    def cask_deps(name: str) -> Set[str]:
        entries = [casks[n] for n, names in cask_names.items() if _variants([name]) & names]
        return {d for e in entries for d in _cask_deps(e)[1]}

    def formula_deps(name: str) -> Set[str]:
        return {d for m in lookup(name) for d in _formula_deps(formulae[m])}

    return {
        "brews": rm_brews,
        "orphans": orphans,
        "casks": rm_casks,
        "taps": taps,
        "cask_order": _dependents_first(set(plan["casks_remove"]) | set(rm_casks), cask_deps),
        "brew_order": _dependents_first(
            set(plan["brews_remove"]) | set(rm_brews) | set(orphans), formula_deps
        ),
    }


def _dependents_first(names: Set[str], deps_of) -> List[str]:
    """Order removals so nothing is uninstalled while a remaining one still needs it."""
    remaining = set(names)
    order: List[str] = []
    while remaining:
        needed = {
            r
            for n in remaining
            for r in remaining
            if r != n and _variants(deps_of(n)) & _variants([r])
        }
        ready = sorted(remaining - needed) or sorted(remaining)
        order += ready
        remaining -= set(ready)
    return order


def _no_cleanup() -> Dict[str, List[str]]:
    return {"brews": [], "orphans": [], "casks": [], "taps": [], "cask_order": [], "brew_order": []}


def _remove(
    brew: str, env: Dict, cmd: List[str], names: List[str], owned: Set[str], kind: str
) -> bool:
    rc, _, stderr = run_command(cmd + names, env)
    if rc == 0:
        owned.difference_update(names)
        return True
    log(f"Failed to remove {', '.join(names)}: {stderr}", Color.RED)
    subset = _installed_subset(brew, env, names, kind)
    if subset is not None:
        # Unknown (None) keeps everything owned so removal is retried.
        owned.difference_update(set(names) - subset)
    return False


def _record(state: Dict, brews, casks, taps, mas, trusted) -> None:
    state["brew"] = {
        "brews": sorted(brews),
        "casks": sorted(casks),
        "taps": sorted(taps),
        "masApps": dict(mas),
        "trustedTaps": sorted(trusted),
    }


def install_brew_packages(brew_config: dict, state: dict, update: bool = False) -> bool:
    """Declarative Homebrew package management.

    State is the source of truth: we reconcile the desired config against what
    we recorded installing last run (state["brew"]), never against live
    `brew list`/`leaves`/`tap`/`mas list` output. This keeps every deploy fast
    and only shells out to brew when there is an actual diff to apply. Manual
    drift (a hand `brew uninstall`) is not detected — remove the entry from
    state and re-deploy to force a reinstall.

    The exception is `onActivation.cleanup`, which queries live brew on every
    deploy and also removes installed items that tools never recorded.

    Installed packages are only upgraded with ``update``: `brew upgrade` on
    the tracked formulae and casks, `mas upgrade` on the tracked apps.
    """
    if sys.platform != "darwin":
        return True

    spec, errors = parse_brew(brew_config)
    if errors:
        for err in errors:
            log(f"brew: {err}", Color.RED)
        return False

    prev = state.get("brew", {})
    plan = _plan(spec, prev)

    # tracked sets mirror what we believe is installed; mutate on success only.
    inst_brews = set(prev.get("brews", []))
    inst_casks = set(prev.get("casks", []))
    inst_taps = {_normalize_tap(t) for t in prev.get("taps", [])}
    inst_mas = dict(prev.get("masApps", {}))
    inst_trust = set(prev.get("trustedTaps", []))
    # homebrew/core is never untapped, so a dropped entry is just forgotten.
    forgotten = {t for t in inst_taps - spec.taps if t.lower() == _CORE_TAP}
    if forgotten and "brew" in state:
        inst_taps -= forgotten
        state["brew"]["taps"] = sorted(inst_taps)

    def unchanged() -> bool:
        if "brew" not in state:
            _record(state, inst_brews, inst_casks, inst_taps, inst_mas, inst_trust)
        elif inst_mas != spec.mas:
            # No install/remove work means the app-id sets already match, so
            # any dict difference is a pure rename ({"Old": 1} -> {"New": 1}).
            # Adopt the new names or state carries the stale key forever.
            state["brew"]["masApps"] = dict(spec.mas)
        return True

    upgrade = {
        "--formula": sorted(inst_brews & spec.brews),
        "--cask": sorted(inst_casks & spec.casks),
        "mas": sorted({str(i) for i in inst_mas.values()} & {str(i) for i in spec.mas.values()}),
    }
    upgrades = update and any(upgrade.values())
    planned = any(plan.values())
    if not planned and not upgrades and not spec.cleanup:
        debug("All brew packages in sync", Color.BLUE)
        return unchanged()

    brew = _brew_bin()
    if not brew and not planned:
        return unchanged()
    if not brew:
        brew = _bootstrap_brew()
    if not brew:
        log("brew not found and bootstrap failed, skipping", Color.RED)
        return False

    env = _brew_env(brew, spec, update)
    success = True

    clean = _no_cleanup()
    cleanup_planned = False
    if spec.cleanup:
        live, err = query_installed(brew, env, spec)
        if err:
            log(f"brew cleanup skipped: {err}", Color.RED)
            success = False
        elif live["deferred"]:
            log(f"brew cleanup deferred until tapped: {', '.join(live['deferred'])}", Color.YELLOW)
        else:
            clean = cleanup_targets(spec, prev, plan, live)
            cleanup_planned = True
    if not planned and not upgrades and not any(clean.values()):
        debug("All brew packages in sync", Color.BLUE)
        return unchanged() and success

    # --- Taps and trust (before installs that load from them) ---
    failed_taps = set()
    for tap in plan["taps_add"]:
        log(f"Tapping {tap}", Color.GREEN)
        rc, _, stderr = run_command([brew, "tap", tap], env)
        if rc != 0:
            log(f"Failed to tap {tap}: {stderr}", Color.RED)
            success = False
            failed_taps.add(tap)
        else:
            inst_taps.add(tap)
    for tap in plan["trust_add"]:
        if tap in failed_taps:
            log(f"Not trusting {tap}: tapping it failed", Color.YELLOW)
            continue
        log(f"Trusting tap {tap}", Color.GREEN)
        rc, _, stderr = run_command([brew, "trust", "--tap", tap], env)
        if rc != 0:
            log(f"Failed to trust tap {tap}: {stderr}", Color.RED)
            success = False
        else:
            inst_trust.add(tap)

    # --- Upgrades of what was already installed, before installs and removals ---
    if upgrades:
        for kind in ("--formula", "--cask"):
            if upgrade[kind]:
                log(f"Upgrading {', '.join(upgrade[kind])}", Color.BLUE)
                rc, _, stderr = run_command([brew, "upgrade", kind] + upgrade[kind], env)
                if rc != 0:
                    log(f"Failed to upgrade {', '.join(upgrade[kind])}: {stderr}", Color.RED)
                    success = False
        if upgrade["mas"] and (mas := _mas_bin_from_env(env)):
            log(f"Upgrading Mac App Store apps: {', '.join(upgrade['mas'])}", Color.BLUE)
            rc, _, stderr = run_command([mas, "upgrade"] + upgrade["mas"], env)
            if rc != 0:
                log(f"Failed to upgrade Mac App Store apps: {stderr}", Color.RED)
                success = False

    # --- Installs ---
    brews_to_install = plan["brews_install"]
    if brews_to_install:
        log(f"Installing formulae: {', '.join(brews_to_install)}", Color.GREEN)
        rc, _, stderr = run_command([brew, "install"] + brews_to_install, env)
        if rc != 0:
            log(f"Failed to install formulae: {stderr}", Color.RED)
            success = False
            # brew installs targets in order, so an earlier one can land before
            # a later failure. Recording none of them would leave a package we
            # installed permanently unowned and never cleaned up.
            subset = _installed_subset(brew, env, brews_to_install, "--formula")
            if subset is not None:
                inst_brews |= subset
        else:
            inst_brews |= set(brews_to_install)
    casks_to_install = plan["casks_install"]
    if casks_to_install:
        log(f"Installing casks: {', '.join(casks_to_install)}", Color.GREEN)
        cmd = [brew, "install", "--cask"]
        if spec.no_quarantine:
            cmd.append("--no-quarantine")
        cmd += casks_to_install
        rc, _, stderr = run_command(cmd, env)
        if rc != 0:
            log(f"Failed to install casks: {stderr}", Color.RED)
            success = False
            subset = _installed_subset(brew, env, casks_to_install, "--cask")
            if subset is not None:
                inst_casks |= subset
        else:
            inst_casks |= set(casks_to_install)

    # --- Removals ---
    zap = spec.cleanup == "zap"
    cask_cmd = [brew, "uninstall", "--cask"] + (["--zap"] if zap else [])
    removals = []
    if cleanup_planned:
        # One package per command, casks then formulae, dependents first, so a
        # refusal blocks only itself. Homebrew's own autoremove after each
        # uninstall would delete listed orphans and pending deps out of order.
        rm_env = {**env, "HOMEBREW_NO_AUTOREMOVE": "1"}
        removals += [(cask_cmd, c, inst_casks, "--cask") for c in clean["cask_order"]]
        removals += [([brew, "uninstall"], f, inst_brews, "--formula") for f in clean["brew_order"]]
        for cmd, name, owned, kind in removals:
            log(f"{'Zapping' if kind == '--cask' and zap else 'Removing'} {name}", Color.RED)
            if not _remove(brew, rm_env, cmd, [name], owned, kind):
                success = False
    else:
        for cmd, names, owned, kind in (
            ([brew, "uninstall"], plan["brews_remove"], inst_brews, "--formula"),
            (cask_cmd, plan["casks_remove"], inst_casks, "--cask"),
        ):
            if names:
                removals.append(names)
                verb = "Zapping" if cmd is cask_cmd and zap else "Removing"
                log(f"{verb} {', '.join(names)}", Color.RED)
                if not _remove(brew, env, cmd, names, owned, kind):
                    success = False

    # --- Mac App Store apps ---
    mas_to_install, mas_to_remove = plan["mas_install"], plan["mas_remove"]
    if (mas_to_install or mas_to_remove) and not (mas := _mas_bin_from_env(env)):
        log("mas not found, skipping Mac App Store apps", Color.RED)
        success = False
    elif mas_to_install or mas_to_remove:
        # Removals run first: a same-name app_id swap ({"App": 1} -> {"App": 2})
        # queues both a remove (old id) and an install (new id) under one name.
        # Installing first then popping the name would drop the fresh entry from
        # state, so reconcile removals before installs.
        for name, app_id in mas_to_remove:
            log(f"Removing Mac App Store app: {name} ({app_id})", Color.RED)
            rc, _, stderr = run_command([mas, "uninstall", str(app_id)], env)
            if rc != 0:
                log(f"Failed to remove {name}: {stderr}", Color.RED)
                success = False
            else:
                inst_mas.pop(name, None)
        for name, app_id in mas_to_install:
            log(f"Installing Mac App Store app: {name} ({app_id})", Color.GREEN)
            rc, _, stderr = run_command([mas, "install", str(app_id)], env)
            if rc != 0:
                log(f"Failed to install {name}: {stderr}", Color.RED)
                success = False
            else:
                inst_mas[name] = app_id

    # --- Autoremove orphaned deps; cleanup already removed the ones it listed ---
    if removals and not cleanup_planned:
        rc, stdout, stderr = run_command([brew, "autoremove"], env)
        if rc == 0 and stdout.strip():
            log("Autoremoved orphaned deps", Color.RED)
        elif rc != 0:
            log(f"Failed to autoremove: {stderr}", Color.RED)

    # --- Untap and untrust last, once nothing installed still loads from them ---
    for tap in sorted(set(plan["taps_remove"]) | set(clean["taps"])):
        log(f"Untapping {tap}", Color.RED)
        rc, _, stderr = run_command([brew, "untap", tap], env)
        if rc != 0:
            log(f"Failed to untap {tap}: {stderr}", Color.RED)
            success = False
        else:
            inst_taps.discard(tap)
    for tap in plan["trust_remove"]:
        log(f"Untrusting tap {tap}", Color.RED)
        rc, _, stderr = run_command([brew, "untrust", "--tap", tap], env)
        if rc != 0:
            log(f"Failed to untrust tap {tap}: {stderr}", Color.RED)
            success = False
        else:
            inst_trust.discard(tap)

    _record(state, inst_brews, inst_casks, inst_taps, inst_mas, inst_trust)
    return success


def diff_brew(brew_config, state: Dict) -> List[Tuple[str, List[str]]]:
    """Dry-run rows for `tools diff`, as (subsection, rows) pairs."""
    if sys.platform != "darwin":
        return []
    spec, errors = parse_brew(brew_config)
    if errors:
        return [("errors", [f"    ! {err}" for err in errors])]

    prev = state.get("brew", {})
    plan = _plan(spec, prev)
    clean = _no_cleanup()
    notes = []
    if spec.cleanup and (brew := _brew_bin()):
        live, err = query_installed(brew, _brew_env(brew, spec), spec)
        if err:
            notes.append(f"    ? cannot plan cleanup: {err}")
        elif live["deferred"]:
            notes.append(f"    ? cleanup deferred until tapped: {', '.join(live['deferred'])}")
        else:
            clean = cleanup_targets(spec, prev, plan, live)

    unowned = " (not installed by tools)"
    cask_verb = "zap" if spec.cleanup == "zap" else "remove"
    taps = [f"    + tap {t}" for t in plan["taps_add"]]
    taps += [f"    - untap {t}" for t in plan["taps_remove"]]
    taps += [f"    - untap {t}{unowned}" for t in clean["taps"]]
    trust = [f"    + trust tap {t}" for t in plan["trust_add"]]
    trust += [f"    - untrust tap {t}" for t in plan["trust_remove"]]
    brews = [f"    + install {f}" for f in plan["brews_install"]]
    brews += [f"    - remove {f}" for f in plan["brews_remove"]]
    brews += [f"    - remove {f}{unowned}" for f in clean["brews"]]
    brews += [f"    - remove {f} (dependency nothing else needs)" for f in clean["orphans"]]
    casks = [f"    + install {c}" for c in plan["casks_install"]]
    casks += [f"    - {cask_verb} {c}" for c in plan["casks_remove"]]
    casks += [f"    - {cask_verb} {c}{unowned}" for c in clean["casks"]]
    # Mac App Store apps are reconciled by deploy, so they have to appear here
    # too — otherwise a MAS-only difference short-circuits and its uninstalls
    # never show up in the approval preview.
    mas = [f"    + install {n} ({i})" for n, i in plan["mas_install"]]
    mas += [f"    - remove {n} ({i})" for n, i in plan["mas_remove"]]

    rows = (
        ("taps", taps),
        ("trust", trust),
        ("brews", brews),
        ("casks", casks),
        ("masApps", mas),
        ("cleanup", notes),
    )
    return [(title, items) for title, items in rows if items]
