"""macOS Dock layout: ordered apps, folders, spacers and URLs.

Config shape::

    dock:
      apps:                                   # persistent-apps, left of the divider
        - /System/Applications/Mail.app
        - spacer                              # or small-spacer, flexible-spacer
        - {spacer: small}                     # same, long form
        - /Applications/Safari.app
      others:                                 # persistent-others, right of the divider
        - path: ~/Downloads
          view: grid                          # auto | fan | grid | list
          display: stack                      # stack | folder
          sort: dateadded                     # name | dateadded | datemodified | datecreated | kind
        - ~/Documents/notes.txt               # a file tile
        - {url: "https://example.com", label: Example}
      restart: false                          # true: killall Dock after a change

Only declared sections are managed: leave out `others` and the right side
is never touched. Each section is written whole through the `defaults`
runner (`defaults write com.apple.dock persistent-apps <plist>`). Like
nix-config's dockutil module (`--no-restart`), Dock is not restarted unless
`restart: true`; then it restarts once, after `defaults:` has also written.
Tiles use dockutil's form and folder defaults (view auto, display folder,
sort datemodified), and `/Applications` paths resolve the way dockutil does.

The live layout is compared item by item, by resolved path, spacer kind
and only the folder options the config sets, so Dock's own bookkeeping
(GUIDs, bookmarks, labels) never forces a rewrite. A path that does not
exist is skipped with a warning; the rest of the layout still applies.

Ownership follows `defaults`: when a section leaves config and the live
layout still matches `written` or `confirmed`, the prior array is restored,
or the key is deleted if it did not exist before tools; a layout rearranged
by hand is left as is and forgotten.
"""

import difflib
import os
import random
import sys
import urllib.parse
from dataclasses import dataclass, replace
from typing import Callable, Dict, List, Optional, Tuple

from tools.log import Color, debug, log
from tools.user.defaults import (
    DOCK_DOMAIN,
    MISSING,
    DefaultsCLI,
    DefaultsError,
    from_xml,
    queue_restart,
    run_restarts,
    to_xml,
    xml_error,
)

SECTIONS = {"apps": "persistent-apps", "others": "persistent-others"}
_KEYS = set(SECTIONS) | {"restart"}

VIEW = {"auto": 0, "fan": 1, "grid": 2, "list": 3}
DISPLAY = {"stack": 0, "folder": 1}
SORT = {"name": 1, "dateadded": 2, "datemodified": 3, "datecreated": 4, "kind": 5}
_OPTION_FIELDS = (
    ("view", "showas", VIEW),
    ("display", "displayas", DISPLAY),
    ("sort", "arrangement", SORT),
)

SPACERS = {"regular": "spacer-tile", "small": "small-spacer-tile", "flexible": "flex-spacer-tile"}
_SPACER_WORDS = {"spacer": "regular", "small-spacer": "small", "flexible-spacer": "flexible"}
_SPACER_TILES = {v: k for k, v in SPACERS.items()}


# --------------------------------------------------------------------------
# Resources
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DockItem:
    kind: str  # path | spacer | url
    path: str = ""
    spacer: str = ""
    url: str = ""
    label: str = ""
    view: Optional[str] = None
    display: Optional[str] = None
    sort: Optional[str] = None

    def describe(self) -> str:
        if self.kind == "spacer":
            return f"spacer ({self.spacer})"
        if self.kind == "url":
            return f"{self.url} ({self.label})" if self.label else self.url
        opts = [
            f"{name}={getattr(self, name)}" for name, _, _ in _OPTION_FIELDS if getattr(self, name)
        ]
        return f"{self.path} ({', '.join(opts)})" if opts else self.path


@dataclass
class DockSpec:
    sections: Dict[str, List[DockItem]]  # defaults key -> ordered items, declared sections only
    restart: Optional[bool] = None  # None: not set in config


# --------------------------------------------------------------------------
# Parse: raw mapping -> resources
# --------------------------------------------------------------------------


def _expand(path: str) -> str:
    return os.path.normpath(os.path.expanduser(os.path.expandvars(path)))


def _parse_item(entry, section: str) -> Tuple[Optional[DockItem], Optional[str]]:
    where = f"{section}: {entry!r}"
    if isinstance(entry, str) and entry in _SPACER_WORDS:
        return DockItem("spacer", spacer=_SPACER_WORDS[entry]), None
    if isinstance(entry, str):
        entry = {"path": entry}
    if not isinstance(entry, dict):
        return None, f"{where}: must be a path, a spacer or a mapping"

    if "spacer" in entry:
        if (
            set(entry) != {"spacer"}
            or not isinstance(entry["spacer"], str)
            or entry["spacer"] not in SPACERS
        ):
            return None, f"{where}: spacer must be one of {sorted(SPACERS)}"
        return DockItem("spacer", spacer=entry["spacer"]), None

    if "url" in entry:
        if section == "apps":
            return None, f"{where}: URLs belong in others"
        if set(entry) - {"url", "label"} or not isinstance(entry["url"], str) or not entry["url"]:
            return None, f"{where}: url entries take `url` and optional `label`"
        label = "" if entry.get("label") is None else entry["label"]
        if not isinstance(label, str):
            return None, f"{where}: label must be a string"
        err = xml_error([entry["url"], label], where)
        if err:
            return None, err
        return DockItem("url", url=entry["url"], label=label), None

    unknown = set(entry) - {"path", "view", "display", "sort"}
    if unknown:
        return None, f"{where}: unknown keys {sorted(unknown)}"
    path = entry.get("path")
    if not isinstance(path, str) or not path:
        return None, f"{where}: needs a `path`, `spacer` or `url`"
    path = _expand(path)
    if not os.path.isabs(path):
        return None, f"{where}: path must be absolute (or start with ~)"
    opts = {}
    for name, _, allowed in _OPTION_FIELDS:
        value = entry.get(name)
        if value is None:
            continue
        if section == "apps":
            return None, f"{where}: `{name}` only applies to folders in others"
        if not isinstance(value, str) or value not in allowed:
            return None, f"{where}: {name} must be one of {sorted(allowed)}"
        opts[name] = value
    if section == "apps" and not path.endswith(".app"):
        return None, f"{where}: apps holds only .app bundles and spacers"
    err = xml_error(path, where)
    if err:
        return None, err
    return DockItem("path", path=path, **opts), None


def parse_dock(raw) -> Tuple[DockSpec, List[str]]:
    """Validate a raw `dock` mapping. Returns (spec, errors)."""
    spec = DockSpec(sections={})
    errors: List[str] = []
    if raw is None:
        return spec, errors
    if not isinstance(raw, dict):
        return spec, [f"dock must be a mapping, got {type(raw).__name__}"]
    unknown = set(raw) - _KEYS
    if unknown:
        errors.append(f"unknown keys {sorted(unknown)} (expected {sorted(_KEYS)})")
    restart = raw.get("restart")
    restart_ok = restart is None or isinstance(restart, bool)
    if not restart_ok:
        errors.append(f"restart must be true or false, got {restart!r}")
    else:
        spec.restart = restart

    for name, key in SECTIONS.items():
        # A bare `apps:` (every entry commented out) is unmanaged; only `[]` empties it.
        entries = raw.get(name)
        if entries is None:
            continue
        if not isinstance(entries, list):
            errors.append(f"{name}: must be a list")
            continue
        items = []
        section_errors = []
        for entry in entries:
            item, err = _parse_item(entry, name)
            if err:
                section_errors.append(err)
            else:
                items.append(item)
        errors += section_errors
        spec.sections[key] = items
    # Any error holds the whole dock: no writes, no restores, no Dock kill.
    if errors:
        return DockSpec(sections={}), errors
    return spec, errors


# --------------------------------------------------------------------------
# Tiles
# --------------------------------------------------------------------------


@dataclass
class FS:
    exists: Callable[[str], bool] = os.path.exists
    isdir: Callable[[str], bool] = os.path.isdir
    realpath: Callable[[str], str] = os.path.realpath


# dockutil: a missing /Applications app falls back to /System/Applications,
# and a symlink into the Safari cryptex is stored as its target.
APPLICATIONS = "/Applications/"
SYSTEM_ROOT = "/System"
CRYPTEX_APPS = "/System/Volumes/Preboot/Cryptexes/App/System/"
FOLDER_DEFAULTS = {"showas": VIEW["auto"], "displayas": DISPLAY["folder"], "arrangement": 3}


def dockutil_path(path: str, fs: FS) -> str:
    if not path.startswith(APPLICATIONS):
        return path
    if not fs.exists(path) and fs.exists(SYSTEM_ROOT + path):
        path = SYSTEM_ROOT + path
    resolved = fs.realpath(path)
    if resolved != path and resolved.startswith(CRYPTEX_APPS):
        return resolved
    return path


def _resolve(item: DockItem, fs: FS) -> DockItem:
    if item.kind != "path":
        return item
    path = dockutil_path(item.path, fs)
    return item if path == item.path else replace(item, path=path)


def _file_url(path: str, is_dir: bool) -> str:
    url = "file://" + urllib.parse.quote(path.rstrip("/"), safe="/")
    return url + "/" if is_dir else url


def _tile_path(tile: Dict) -> Optional[str]:
    data = (tile.get("tile-data") or {}).get("file-data") or {}
    url = data.get("_CFURLString")
    if not isinstance(url, str):
        return None
    if url.startswith("file://"):
        url = urllib.parse.unquote(urllib.parse.urlparse(url).path)
    return url.rstrip("/") or "/"


def _is_folder(item: DockItem, fs: FS) -> bool:
    return item.kind == "path" and fs.isdir(item.path) and not item.path.endswith(".app")


def _has_options(item: DockItem) -> bool:
    return any(getattr(item, name) is not None for name, _, _ in _OPTION_FIELDS)


def _norm_path(path: str, fs: FS) -> str:
    # Dock stores resolved targets (e.g. Safari lives in a cryptex), so compare realpaths.
    return fs.realpath(path).rstrip("/") or "/"


def build_tile(item: DockItem, fs: FS) -> Dict:
    if item.kind == "spacer":
        return {"tile-data": {}, "tile-type": SPACERS[item.spacer]}
    if item.kind == "url":
        data = {"url": {"_CFURLString": item.url, "_CFURLStringType": 15}}
        if item.label:
            data["label"] = item.label
        return {"tile-data": data, "tile-type": "url-tile"}
    folder = _is_folder(item, fs)
    label = os.path.basename(item.path)
    if label.endswith(".app"):
        label = label[: -len(".app")]
    data = {
        "file-data": {"_CFURLString": item.path, "_CFURLStringType": 0},
        "file-label": label,
        "file-type": 41,
    }
    if folder:
        data.update(directory=1, **FOLDER_DEFAULTS)
        data["file-type"] = 2
        for name, plist_key, allowed in _OPTION_FIELDS:
            value = getattr(item, name)
            if value is not None:
                data[plist_key] = allowed[value]
    return {
        "GUID": random.randint(1000000000, 9999999999),
        "tile-data": data,
        "tile-type": "directory-tile" if folder else "file-tile",
    }


def _identity(item: DockItem, fs: FS) -> str:
    if item.kind == "spacer":
        return f"spacer:{item.spacer}"
    if item.kind == "url":
        return f"url:{item.url}"
    return f"path:{_norm_path(item.path, fs)}"


def _tile_identity(tile, fs: FS) -> str:
    if not isinstance(tile, dict):
        return f"unknown:{tile!r}"
    kind = tile.get("tile-type")
    if kind in _SPACER_TILES:
        return f"spacer:{_SPACER_TILES[kind]}"
    if kind == "url-tile":
        url = ((tile.get("tile-data") or {}).get("url") or {}).get("_CFURLString")
        return f"url:{url}"
    if kind in ("file-tile", "directory-tile"):
        path = _tile_path(tile)
        if path is not None:
            return f"path:{_norm_path(path, fs)}"
    return f"unknown:{kind}"


def _tile_label(tile, fs: FS) -> str:
    ident = _tile_identity(tile, fs)
    kind, _, rest = ident.partition(":")
    return f"spacer ({rest})" if kind == "spacer" else rest


def item_matches(item: DockItem, tile, fs: FS) -> bool:
    if _identity(item, fs) != _tile_identity(tile, fs):
        return False
    data = tile.get("tile-data") or {}
    if item.kind == "url":
        return (data.get("label") or "") == item.label or not item.label
    # build_tile drops folder options on non-folders, so matching must too.
    if _is_folder(item, fs):
        for name, plist_key, allowed in _OPTION_FIELDS:
            value = getattr(item, name)
            if value is not None and data.get(plist_key) != allowed[value]:
                return False
    return True


def layout_matches(items: List[DockItem], tiles, fs: FS) -> bool:
    tiles = tiles if isinstance(tiles, list) else []
    return len(items) == len(tiles) and all(item_matches(i, t, fs) for i, t in zip(items, tiles))


_SETTINGS = ("showas", "displayas", "arrangement", "label")


def _recorded(raw):
    """Tiles tools recorded, or None. Older item-list records carry no tiles."""
    if not isinstance(raw, str):
        return None
    tiles = from_xml(raw)
    return tiles if isinstance(tiles, list) else None


def _tile_settings(tile) -> Tuple:
    data = tile.get("tile-data") if isinstance(tile, dict) else None
    data = data if isinstance(data, dict) else {}
    return tuple(data.get(k) for k in _SETTINGS)


def _tiles_match(a, b, fs: FS) -> bool:
    """Same tiles by identity and folder/URL settings; Dock rewrites GUIDs and
    bookmarks on relaunch, so those are ignored."""
    if a is MISSING or b is MISSING:
        return a is b
    if not isinstance(a, list) or not isinstance(b, list) or len(a) != len(b):
        return False
    return all(
        _tile_identity(x, fs) == _tile_identity(y, fs) and _tile_settings(x) == _tile_settings(y)
        for x, y in zip(a, b)
    )


def layout_changes(items: List[DockItem], tiles, fs: FS) -> List[str]:
    """Item-level preview lines (current -> desired)."""
    tiles = tiles if isinstance(tiles, list) else []
    have = [_tile_identity(t, fs) for t in tiles]
    want = [_identity(i, fs) for i in items]
    lines = []
    for op, a1, a2, b1, b2 in difflib.SequenceMatcher(a=have, b=want, autojunk=False).get_opcodes():
        if op == "equal":
            for tile, item in zip(tiles[a1:a2], items[b1:b2]):
                if not item_matches(item, tile, fs):
                    lines.append(f"      ~ {item.describe()}")
            continue
        lines += [f"      - {_tile_label(t, fs)}" for t in tiles[a1:a2]]
        lines += [f"      + {i.describe()}" for i in items[b1:b2]]
    return lines


# --------------------------------------------------------------------------
# Plan: shared by deploy and diff
# --------------------------------------------------------------------------


def _tracked(state: Dict) -> Dict:
    return state.get("dock", {}).get("sections", {}) or {}


def plan(spec: DockSpec, errors: List[str], state: Dict, cli: DefaultsCLI, fs: FS) -> Dict:
    if errors:
        spec = DockSpec(sections={})
    tracked = _tracked(state)
    result: Dict = {
        "errors": list(errors),
        "read_error": None,
        "missing": [],  # (section, item, kept): kept means its live tile stays in place
        "skipped": [],  # section: no declared path resolves, so it is left untouched
        "rewrite": [],  # (section, items, current tiles or MISSING, {index: live tile})
        "refresh": [],  # (section, written, confirmed): live layout already matches
        "restore": [],  # (section, prior tiles)
        "delete": [],  # section: not set before tools
        "forget": [],  # (section, reason)
        "restart": False,
    }
    try:
        current = cli.export("user", DOCK_DOMAIN)
    except DefaultsError as e:
        result["read_error"] = str(e)
        return result

    for key, items in spec.sections.items():
        items = [_resolve(i, fs) for i in items]
        cur = current.get(key, MISSING)
        live = list(cur) if isinstance(cur, list) else []
        present: List[DockItem] = []
        kept: Dict[int, Dict] = {}
        declared_paths = resolved_paths = 0
        for item in items:
            if item.kind != "path":
                present.append(item)
                continue
            declared_paths += 1
            if fs.exists(item.path):
                present.append(item)
                resolved_paths += 1
                continue
            # A missing app (cask not installed yet, volume unmounted) must not
            # shrink the live Dock: its live tile, if any, stays where declared.
            ident = _identity(item, fs)
            tile = next((t for t in live if _tile_identity(t, fs) == ident), None)
            result["missing"].append((key, item, tile is not None))
            if tile is not None:
                live.remove(tile)
                kept[len(present)] = tile
                present.append(item)
                resolved_paths += 1
        if declared_paths and not resolved_paths:
            result["skipped"].append(key)
            continue
        if layout_matches(present, cur, fs) and cur is not MISSING:
            entry = tracked.get(key)
            rec = _recorded(entry.get("written")) if entry else None
            # A recorded layout that still satisfies config is kept, so a Dock
            # UI change to an option the config leaves unset is not adopted.
            if entry is None:
                pass
            elif rec is None or not layout_matches(present, rec, fs):
                result["refresh"].append((key, to_xml(cur), to_xml(cur)))
            elif entry.get("confirmed") != entry["written"] and _tiles_match(rec, cur, fs):
                result["refresh"].append((key, entry["written"], entry["written"]))
        else:
            result["rewrite"].append((key, present, cur, kept))

    if not errors:
        for key in sorted(set(tracked) - set(spec.sections)):
            entry = tracked[key] or {}
            cur = current.get(key, MISSING)
            prior = from_xml(entry.get("prior"))
            ours = [_recorded(entry.get("written")), _recorded(entry.get("confirmed"))]
            if _tiles_match(cur, prior, fs):
                result["forget"].append((key, "already gone"))
            elif any(rec is not None and _tiles_match(rec, cur, fs) for rec in ours):
                if prior is MISSING:
                    result["delete"].append(key)
                else:
                    result["restore"].append((key, prior))
            else:
                result["forget"].append((key, "rearranged by hand; left as is"))

    if spec.restart is not None:
        restart = spec.restart
    elif spec.sections:
        restart = False
    else:
        restart = state.get("dock", {}).get("restart", False)
    result["restart"] = bool(restart) and bool(
        result["rewrite"] or result["restore"] or result["delete"]
    )
    return result


# --------------------------------------------------------------------------
# Deploy
# --------------------------------------------------------------------------


def apply_dock(
    spec: DockSpec,
    errors: List[str],
    state: Dict,
    cli: DefaultsCLI,
    fs: FS,
    restarts: Optional[List[str]] = None,
    persist: Callable[[], None] = lambda: None,
) -> bool:
    """With `restarts`, a Dock restart is queued there for the caller to run.
    `persist` saves state; it runs before each write so `written` is on disk first."""
    queue: List[str] = [] if restarts is None else restarts
    try:
        return _apply_dock(spec, errors, state, cli, fs, queue, persist)
    finally:
        if restarts is None:
            run_restarts(queue, cli)


def _apply_dock(
    spec: DockSpec,
    errors: List[str],
    state: Dict,
    cli: DefaultsCLI,
    fs: FS,
    queue: List[str],
    persist: Callable[[], None],
) -> bool:
    section = state.setdefault("dock", {})
    tracked = section.setdefault("sections", {})
    success = True

    p = plan(spec, errors, state, cli, fs)
    for err in p["errors"]:
        log(f"dock: {err}", Color.RED)
        success = False
    if p["read_error"]:
        log(f"dock: cannot read layout: {p['read_error']}", Color.RED)
        return False
    for key, item, kept in p["missing"]:
        if kept:
            log(f"dock: {item.path} in {key} not found; keeping its live tile", Color.YELLOW)
        else:
            log(f"dock: skipping {item.path} in {key}: not found", Color.YELLOW)
    for key in p["skipped"]:
        log(f"dock: leaving {key} untouched: none of its paths exist", Color.YELLOW)
    for key, items in spec.sections.items():
        for item in items:
            if _has_options(item) and fs.exists(item.path) and not _is_folder(item, fs):
                log(
                    f"dock: {item.path} in {key} is not a folder; ignoring view/display/sort",
                    Color.YELLOW,
                )

    changed = False

    def mark_changed() -> None:
        nonlocal changed
        changed = True
        if p["restart"]:
            queue_restart(queue, "Dock")

    def failed(key: str, e: BaseException) -> None:
        nonlocal success
        success = False
        if isinstance(e, DefaultsError):
            log(f"dock: {e}", Color.RED)
        else:
            log(f"dock: {key}: {type(e).__name__}: {e}", Color.RED)
            mark_changed()

    for key, items, cur, kept in p["rewrite"]:
        try:
            tiles = [kept.get(n) or build_tile(i, fs) for n, i in enumerate(items)]
        except Exception as e:
            log(f"dock: cannot build {key}: {type(e).__name__}: {e}", Color.RED)
            success = False
            continue
        fresh = key not in tracked
        if fresh:
            tracked[key] = {"prior": None if cur is MISSING else to_xml(cur), "written": None}
        before = tracked[key]["written"]
        # `confirmed` is the last layout known to have landed.
        rec = _recorded(before)
        if rec is not None and _tiles_match(rec, cur, fs):
            tracked[key]["confirmed"] = before
        tracked[key].setdefault("confirmed", before)
        tracked[key]["written"] = to_xml(tiles)
        persist()
        try:
            cli.write("user", DOCK_DOMAIN, key, tiles)
        except BaseException as e:
            failed(key, e)
            if isinstance(e, DefaultsError):
                if fresh:
                    tracked.pop(key, None)
                else:
                    tracked[key]["written"] = before
            elif not isinstance(e, Exception):
                raise
            continue
        tracked[key]["confirmed"] = tracked[key]["written"]
        mark_changed()
        log(f"Dock: wrote {key} ({len(items)} items)", Color.GREEN)

    for key, written, confirmed in p["refresh"]:
        tracked[key]["written"] = written
        tracked[key]["confirmed"] = confirmed

    for key, prior in p["restore"]:
        try:
            cli.write("user", DOCK_DOMAIN, key, prior)
        except BaseException as e:
            failed(key, e)
            if not isinstance(e, Exception):
                raise
            continue
        tracked.pop(key, None)
        mark_changed()
        log(f"Dock: restored prior {key}", Color.YELLOW)

    for key in p["delete"]:
        try:
            cli.delete("user", DOCK_DOMAIN, key)
        except BaseException as e:
            failed(key, e)
            if not isinstance(e, Exception):
                raise
            continue
        tracked.pop(key, None)
        mark_changed()
        log(f"Dock: deleted {key}", Color.YELLOW)

    for key, reason in p["forget"]:
        if reason != "already gone":
            log(f"dock: releasing {key}: {reason}", Color.YELLOW)
        tracked.pop(key, None)

    if not changed:
        debug("Dock up to date", Color.BLUE)

    if spec.sections:
        section["restart"] = bool(spec.restart)
    if not tracked:
        state.pop("dock", None)
    return success


# --------------------------------------------------------------------------
# Diff (read-only)
# --------------------------------------------------------------------------


def preview_dock(
    spec: DockSpec,
    errors: List[str],
    state: Dict,
    cli: DefaultsCLI,
    fs: FS,
    restarts: Optional[List[str]] = None,
) -> List[str]:
    p = plan(spec, errors, state, cli, fs)
    changes = [f"  ! {err}" for err in p["errors"]]
    if p["read_error"]:
        return changes + [f"  ? cannot read Dock layout: {p['read_error']}"]
    for key, item, kept in p["missing"]:
        if kept:
            changes.append(f"  ! keep {item.path} in {key}: not found, live tile stays")
        else:
            changes.append(f"  ! skip {item.path} in {key}: not found")
    changes += [f"  ! leave {key} untouched: none of its paths exist" for key in p["skipped"]]
    for key, items, cur, _ in p["rewrite"]:
        changes.append(f"  ~ rewrite {key} ({len(items)} items)")
        changes += layout_changes(items, [] if cur is MISSING else cur, fs)
    changes += [f"  - restore prior {key}" for key, _ in p["restore"]]
    changes += [f"  - delete {key}" for key in p["delete"]]
    changes += [
        f"  ~ forget {key} ({reason})" for key, reason in p["forget"] if reason != "already gone"
    ]
    if p["restart"]:
        if restarts is None:
            changes.append("  ~ restart Dock")
        else:
            queue_restart(restarts, "Dock")
    return changes


# --------------------------------------------------------------------------
# Config-dict adapters used by cli/diff
# --------------------------------------------------------------------------


def has_work(raw, state: Dict) -> bool:
    return raw is not None or bool(_tracked(state))


def install_dock(
    raw,
    state: Dict,
    cli: Optional[DefaultsCLI] = None,
    fs: Optional[FS] = None,
    restarts: Optional[List[str]] = None,
    persist: Callable[[], None] = lambda: None,
) -> bool:
    if sys.platform != "darwin":
        log("dock: skipping, not macOS", Color.YELLOW)
        return True
    cli = cli or DefaultsCLI.for_host(allow_sudo=False)
    if cli is None:
        log("dock: `defaults` binary not found, skipping", Color.RED)
        return False
    spec, errors = parse_dock(raw)
    return apply_dock(spec, errors, state, cli, fs or FS(), restarts, persist)


def diff_dock(
    raw,
    state: Dict,
    cli: Optional[DefaultsCLI] = None,
    fs: Optional[FS] = None,
    restarts: Optional[List[str]] = None,
) -> List[str]:
    if sys.platform != "darwin":
        return []
    cli = cli or DefaultsCLI.for_host(allow_sudo=False)
    if cli is None:
        return ["  ? `defaults` binary not found, cannot diff"]
    spec, errors = parse_dock(raw)
    return preview_dock(spec, errors, state, cli, fs or FS(), restarts)
