"""`services:` section: reconcile running services through their HTTP APIs.

Each service module exposes ``parse(raw, base_dir) -> Spec`` (validation, the
only step that sees config-shaped data) and ``reconcile(spec, ctx)`` (API
logic on typed specs, with I/O injected through ``Context``). This module is
the thin adapter between the config layer and those two.
"""

import copy
from typing import Callable, Dict, List, Optional, Tuple

from tools.log import Color, log
from tools.services import arr, beszel, forgejo, jellyfin, nextdns, syncthing
from tools.services.engine import Context
from tools.services.http import ServiceError
from tools.services.keys import KeyFileError, load_key, loose_mode

# Order matters only for readability of output; services are independent.
SERVICES = {
    "arr": arr,
    "beszel": beszel,
    "forgejo": forgejo,
    "jellyfin": jellyfin,
    "nextdns": nextdns,
    "syncthing": syncthing,
}


def has_work(config) -> bool:
    """Gate: an absent or empty section means no-op and no network calls.

    State is not consulted: nothing here is deleted on the strength of state,
    so dropping the section simply stops managing those services.
    """
    if not isinstance(config, dict):
        return bool(config)  # malformed: run so it is reported, not ignored
    return any(v is not None for v in config.values())


def run_services(
    config: Dict,
    state: Dict,
    *,
    apply: bool,
    base_dir: Optional[str] = None,
    context_factory: Optional[Callable[..., Context]] = None,
    fingerprint_key: Optional[bytes] = None,
) -> Tuple[List[Tuple[str, List[str]]], bool]:
    """Plan (apply=False) or apply every configured service.

    Returns per-service change rows and overall success. One service failing
    (unreachable, auth, bad spec) never stops the others.
    """
    make = context_factory or Context
    results: List[Tuple[str, List[str]]] = []
    ok = True
    if not isinstance(config, dict):
        return [("services", ["  ! services must be a mapping"])], False
    for name in config:
        if name not in SERVICES and config[name] is not None:
            results.append((name, [f"  ! unknown service {name!r}"]))
            if apply:
                log(f"services: unknown service {name!r}", Color.RED)
            ok = False
    for name, module in SERVICES.items():
        if config.get(name) is None:  # absent or null: unmanaged
            continue
        section_state = state.setdefault(name, {})
        ctx = make(name, apply=apply, state=section_state, fingerprint_key=fingerprint_key)
        try:
            spec = module.parse(config[name], base_dir)
            module.reconcile(spec, ctx)
        except ServiceError as e:
            ctx.fail(str(e))
        except Exception as e:  # noqa: BLE001 - one service's bug must not stop the others
            ctx.fail(f"unexpected {type(e).__name__}: {e}")
        if ctx.changes:
            results.append((name, ctx.changes))
        ok &= ctx.ok
    return results, ok


def diff_services(
    config: Dict, config_dir: str, state: Dict, key_file: Optional[str] = None
) -> List[Tuple[str, List[str]]]:
    scratch = copy.deepcopy(state.get("services", {}))
    # diff never creates the key: without one every masked secret reads as changed.
    try:
        key = load_key(key_file, create=False) if key_file else None
    except KeyFileError as e:
        return [("services", [f"    ! {e}"])]
    results, _ = run_services(
        config, scratch, apply=False, base_dir=config_dir, fingerprint_key=key
    )
    out = [(name, [f"  {row}" for row in rows]) for name, rows in results]
    mode = loose_mode(key_file) if key and key_file else None
    if mode is not None:
        # diff is read-only; deploy tightens it.
        out.insert(0, ("services", [f"    ~ {key_file} mode {mode:04o} -> 0600"]))
    return out


def install_services(
    config: Dict, config_dir: str, state: Dict, key_file: Optional[str] = None
) -> bool:
    try:
        key = load_key(key_file, create=True) if key_file else None
    except KeyFileError as e:
        # Without a usable key masked secrets would be re-pushed every run.
        log(f"services: {e}", Color.RED)
        return False
    _, ok = run_services(
        config,
        state.setdefault("services", {}),
        apply=True,
        base_dir=config_dir,
        fingerprint_key=key,
    )
    if not ok:
        log("services: one or more services failed (see ! rows above)", Color.RED)
    return ok
