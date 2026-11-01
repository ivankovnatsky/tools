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

    State is not consulted here: a dropped service only needs
    `release_services`, which makes no network calls.
    """
    if not isinstance(config, dict):
        return bool(config)  # malformed: run so it is reported, not ignored
    return any(v is not None for v in config.values())


def _released(config, state: Dict, apply: bool) -> List[Tuple[str, List[str]]]:
    """Services in state but no longer configured: shown, their state dropped.

    Remote objects are never deleted on the strength of state.
    """
    if config is None:
        config = {}
    if not isinstance(config, dict):
        return []  # malformed: reported by run_services, nothing released
    results = []
    for name in sorted(state):
        if name in SERVICES and config.get(name) is None:
            row = f"  = {name} removed from config; remote objects are kept"
            results.append((name, [row]))
            if apply:
                log(f"{name}:", Color.BLUE)
                log(row, Color.YELLOW)
                del state[name]
    return results


def release_services(config, state: Dict) -> None:
    """Deploy side of a dropped section: forget its state, touch nothing remote."""
    _released(config, state.get("services", {}), apply=True)


def released_rows(config, state: Dict) -> List[Tuple[str, List[str]]]:
    rows = _released(config, copy.deepcopy(state.get("services", {})), apply=False)
    return [(name, [f"  {row}" for row in r]) for name, r in rows]


def run_services(
    config: Dict,
    state: Dict,
    *,
    apply: bool,
    base_dir: Optional[str] = None,
    context_factory: Optional[Callable[..., Context]] = None,
    fingerprint_key: Optional[bytes] = None,
    secrets_dir: Optional[str] = None,
    secrets_dir_error: Optional[str] = None,
) -> Tuple[List[Tuple[str, List[str]]], bool]:
    """Plan (apply=False) or apply every configured service.

    Returns per-service change rows and overall success. One service failing
    (unreachable, auth, bad spec) never stops the others. ``secrets_dir`` is
    the sops section's ``secretsDir``; None, with ``secrets_dir_error`` saying
    why, when that section is invalid or has no secrets.
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
            if module is nextdns:
                spec = module.parse(config[name], base_dir, secrets_dir, secrets_dir_error)
            else:
                spec = module.parse(config[name], base_dir)
            module.reconcile(spec, ctx)
        except ServiceError as e:
            ctx.fail(str(e))
        except Exception as e:  # noqa: BLE001 - one service's bug must not stop the others
            ctx.fail(f"unexpected {type(e).__name__}: {e}")
        if ctx.changes:
            results.append((name, ctx.changes))
        ok &= ctx.ok
    results += _released(config, state, apply)
    return results, ok


def diff_services(
    config: Dict,
    config_dir: str,
    state: Dict,
    key_file: Optional[str] = None,
    secrets_dir: Optional[str] = None,
    secrets_dir_error: Optional[str] = None,
) -> List[Tuple[str, List[str]]]:
    scratch = copy.deepcopy(state.get("services", {}))
    # diff never creates the key: without one every masked secret reads as changed.
    try:
        key = load_key(key_file, create=False) if key_file else None
    except KeyFileError as e:
        return [("services", [f"    ! {e}"])]
    results, _ = run_services(
        config,
        scratch,
        apply=False,
        base_dir=config_dir,
        fingerprint_key=key,
        secrets_dir=secrets_dir,
        secrets_dir_error=secrets_dir_error,
    )
    out = [(name, [f"  {row}" for row in rows]) for name, rows in results]
    mode = loose_mode(key_file) if key and key_file else None
    if mode is not None:
        # diff is read-only; deploy tightens it.
        out.insert(0, ("services", [f"    ~ {key_file} mode {mode:04o} -> 0600"]))
    return out


def install_services(
    config: Dict,
    config_dir: str,
    state: Dict,
    key_file: Optional[str] = None,
    secrets_dir: Optional[str] = None,
    secrets_dir_error: Optional[str] = None,
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
        secrets_dir=secrets_dir,
        secrets_dir_error=secrets_dir_error,
    )
    if not ok:
        log("services: one or more services failed (see ! rows above)", Color.RED)
    return ok
