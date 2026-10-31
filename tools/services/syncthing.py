"""Syncthing devices, folders, GUI credentials (ported from nix-config syncthing-mgmt).

Deletion, as in syncthing-mgmt `declarative`: with `devices` set, devices not
named there or by a folder are removed; with `folders` or `foldersFile` set,
folders not listed are removed. The local device is never removed.
"""

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Dict, List, Optional

from tools.services import identity
from tools.services import spec as s
from tools.services.engine import Context, Endpoint
from tools.services.http import (
    ApiClient,
    BadResponse,
    InvalidSpec,
    ServiceError,
    list_field,
    norm_path,
)

PASSWORD_IDENTITY = "syncthing/gui/password"
BCRYPT = re.compile(r"\$2[abxy]\$\d{2}\$[./A-Za-z0-9]{53}")


@dataclass
class Folder:
    id: str
    path: str
    label: Optional[str] = None
    # None: not managed (an absent key must not wipe the remote value).
    devices: Optional[List[str]] = None
    ignore_patterns: Optional[List[str]] = None


@dataclass
class Gui:
    username: Dict[str, str]
    password_file: str


@dataclass
class SyncthingSpec:
    endpoint: Endpoint
    api_key_file: Optional[str] = None
    config_dir: Optional[str] = None
    local_device_name: Optional[str] = None
    gui: Optional[Gui] = None
    devices: Optional[List[str]] = None
    device_definitions_file: Optional[str] = None
    folders: Optional[List[Folder]] = None
    folders_file: Optional[str] = None
    restart: bool = False
    prune: bool = False


def parse_folders(raw, where: str) -> List[Folder]:
    folders = []
    entries = s.mapping(raw, where)
    for fid, f in zip(identity.unique(entries, where), entries.values()):
        fw = f"{where}.{fid}"
        if f is None:
            # Folders are authoritative: treating a null entry as absent
            # would delete the folder remotely.
            raise InvalidSpec(f"{fw} is null; remove the entry or give it a path")
        f = s.mapping(f, fw)
        s.check_keys(f, {"path", "label", "devices", "ignorePatterns"}, fw)
        folders.append(
            Folder(
                id=fid,
                path=s.required(f, "path", fw),
                label=s.string(f, "label", fw),
                devices=s.strings(f["devices"], f"{fw}.devices")
                if s.present(f, "devices")
                else None,
                ignore_patterns=(
                    s.strings(f["ignorePatterns"], f"{fw}.ignorePatterns")
                    if s.present(f, "ignorePatterns")
                    else None
                ),
            )
        )
    return folders


def parse(raw, base_dir: Optional[str] = None) -> SyncthingSpec:
    raw = s.mapping(raw, "syncthing")
    s.check_keys(
        raw,
        s.ENDPOINT_KEYS
        | {
            "apiKeyFile", "configDir", "localDeviceName", "gui", "devices",
            "deviceDefinitionsFile", "folders", "foldersFile", "restart",
        },
        "syncthing",
    )  # fmt: skip
    api_key = s.path(raw, "apiKeyFile", "syncthing", base_dir)
    config_dir = s.path(raw, "configDir", "syncthing", base_dir)
    if bool(api_key) == bool(config_dir):
        raise InvalidSpec("syncthing: set exactly one of apiKeyFile or configDir")
    gui = None
    if raw.get("gui") is not None:
        g = s.mapping(raw["gui"], "syncthing.gui")
        s.check_keys(g, {"username", "usernameFile", "passwordFile"}, "syncthing.gui")
        pw = s.path(g, "passwordFile", "syncthing.gui", base_dir)
        if not pw:
            raise InvalidSpec("syncthing.gui.passwordFile is required")
        gui = Gui(username=s.one_of(g, "username", "syncthing.gui", base_dir), password_file=pw)
    if s.present(raw, "folders") and s.present(raw, "foldersFile"):
        raise InvalidSpec("syncthing: set folders or foldersFile, not both")
    registry = s.path(raw, "deviceDefinitionsFile", "syncthing", base_dir)
    devices = s.strings(raw["devices"], "syncthing.devices") if s.present(raw, "devices") else None
    if devices is not None:
        identity.unique(devices, "syncthing.devices")
    if devices and not registry:
        raise InvalidSpec("syncthing.devices needs deviceDefinitionsFile to resolve names")
    return SyncthingSpec(
        endpoint=s.endpoint(raw, "syncthing", "http://localhost:8384", base_dir),
        api_key_file=api_key,
        config_dir=config_dir,
        local_device_name=s.string(raw, "localDeviceName", "syncthing"),
        gui=gui,
        devices=devices,
        device_definitions_file=registry,
        folders=(
            parse_folders(raw["folders"], "syncthing.folders")
            if s.present(raw, "folders")
            else None
        ),
        folders_file=s.path(raw, "foldersFile", "syncthing", base_dir),
        restart=bool(s.boolean(raw, "restart", "syncthing", False)),
        prune=s.prune(raw, "syncthing"),
    )


def _api_key(spec: SyncthingSpec, ctx: Context) -> str:
    if spec.api_key_file:
        return ctx.secret(spec.api_key_file, "apiKeyFile")
    path = f"{spec.config_dir}/config.xml"
    text = ctx.text(path, "config.xml")
    try:
        node = ET.fromstring(text).find(".//gui/apikey")
    except ET.ParseError as e:
        raise InvalidSpec(f"{path}: {e}") from None
    if node is None or not (node.text or "").strip():
        raise InvalidSpec(f"{path}: no <gui><apikey>")
    key = node.text.strip()
    ctx.sensitive(key)
    return key


def _unique_keys(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate key {k!r}")
        out[k] = v
    return out


def _json_file(ctx: Context, path: str, what: str):
    try:
        return json.loads(ctx.secret(path, what), object_pairs_hook=_unique_keys)
    except ValueError as e:
        raise InvalidSpec(f"{what} {path}: invalid JSON ({e})") from None


def _registry(ctx: Context, path: str) -> Dict[str, str]:
    """{name: canonical device ID}; two names for one device is an error."""
    raw = _json_file(ctx, path, "deviceDefinitionsFile")
    if not isinstance(raw, dict):
        raise InvalidSpec("deviceDefinitionsFile must map device names to IDs")
    by_id = identity.index(
        raw.items(), "deviceDefinitionsFile", key=lambda kv: kv[1], kind=identity.device_id
    )
    return {name: device_id for device_id, (name, _) in by_id.items()}


def reconcile(spec: SyncthingSpec, ctx: Context) -> None:
    client = ctx.client(spec.endpoint, {"X-API-Key": _api_key(spec, ctx)})
    status = client.get_dict("/rest/system/status")
    local_id = status.get("myID")
    if local_id is not None:
        local_id = identity.canon(local_id, "system status myID", identity.device_id, remote=True)

    # Resolve all inputs before the first write, so a bad file cannot leave
    # a half-applied config.
    folders = spec.folders
    if spec.folders_file:
        folders = parse_folders(
            _json_file(ctx, spec.folders_file, "foldersFile"), "syncthing.foldersFile"
        )
    wanted_names = set(spec.devices or [])
    registry: Dict[str, str] = {}
    if spec.device_definitions_file:
        registry = _registry(ctx, spec.device_definitions_file)
    # Every name must resolve before any write: an unresolved name would be
    # shared as a bogus ID and its real device removed as "not configured".
    unknown = wanted_names - set(registry)
    folder_names: set = set()
    resolved: Dict[str, List[str]] = {}
    for f in folders or []:
        if f.devices is None:
            continue
        ids = []
        for ref in f.devices:
            device_id = identity.as_device_id(ref)
            if device_id is None and ref in registry:
                device_id = registry[ref]
                folder_names.add(ref)
            if device_id is None:
                unknown.add(ref)
                continue
            ids.append(device_id)
        resolved[f.id] = identity.unique(
            ids, f"syncthing.folders.{f.id}.devices", identity.device_id
        )
    if unknown:
        raise InvalidSpec(
            f"device names not in deviceDefinitionsFile: {', '.join(sorted(unknown))}"
        )
    name_to_id = {n: registry[n] for n in wanted_names | folder_names}
    # Devices are managed only when `devices` is set, as in syncthing-mgmt.
    device_map = name_to_id if spec.devices is not None else None
    if (device_map is not None or folders is not None or spec.local_device_name) and not local_id:
        # Without it the local device is neither protected from removal nor
        # normalized in folder device lists.
        raise InvalidSpec("cannot determine local device ID from /rest/system/status")

    # Every read happens before the first write: a bad read must abort the
    # service, not drive creates or deletes from a partial picture.
    devices_now = None
    if device_map is not None or spec.local_device_name or any(resolved.values()):
        devices_now = identity.index(
            client.get_list("/rest/config/devices", ("deviceID",)),
            "syncthing devices",
            key=lambda d: d["deviceID"],
            kind=identity.device_id,
            remote=True,
        )
    gui_now = None
    if spec.gui is not None:
        ctx.value(spec.gui.username, "gui username")
        ctx.secret(spec.gui.password_file, "gui passwordFile")
        gui_now = client.get_dict("/rest/config/gui")
    folders_now = ignores_now = shares_now = None
    if folders is not None:
        folders_now = identity.index(
            client.get_list("/rest/config/folders", ("id",)),
            "syncthing folders",
            key=lambda f: f["id"],
            remote=True,
        )
        shares_now = {
            fid: identity.index(
                list_field(cur, "devices", f"folder {fid}", ("deviceID",)),
                f"folder {fid} devices",
                key=lambda d: d["deviceID"],
                kind=identity.device_id,
                remote=True,
            )
            for fid, cur in folders_now.items()
        }
        ignores_now = {
            f.id: _read_ignores(client, f.id)
            for f in folders
            if f.id in folders_now and f.ignore_patterns is not None
        }
    # A folder device that will not exist after the device sync would be
    # dropped from the folder by the server, so the diff would never settle.
    if device_map is not None:
        known = set(device_map.values()) | {local_id}
    else:
        known = set(devices_now or {}) | {local_id}
    unconfigured = {fid: [d for d in ids if d not in known] for fid, ids in resolved.items()}
    if device_map is not None:
        desired_peers = set(device_map.values()) - {local_id}
        peers = set(devices_now) - {local_id}
        ctx.check_prune("syncthing devices", len(desired_peers), len(peers), spec.prune)
    if folders is not None:
        ctx.check_prune("syncthing folders", len(folders), len(folders_now), spec.prune)

    before = ctx.applied
    if spec.local_device_name:
        _sync_local_name(ctx, client, devices_now, local_id, spec.local_device_name)
    if spec.gui is not None:
        _sync_gui(ctx, client, spec.gui, gui_now)
    failed_devices: set = set()
    if device_map is not None:
        failed_devices = _sync_devices(
            ctx, client, devices_now, device_map, local_id, not spec.local_device_name
        )
    if folders is not None:
        _sync_folders(
            ctx,
            client,
            folders,
            folders_now,
            shares_now,
            ignores_now,
            resolved,
            unconfigured,
            local_id,
            failed_devices,
        )

    if spec.restart and ctx.applied > before:
        ctx.change("~ restart syncthing")
        ctx.do("restart", lambda: client.request("POST", "/rest/system/restart"))
    elif spec.restart and not ctx.apply and ctx.changes:
        ctx.change("~ restart syncthing")


def _read_ignores(client: ApiClient, folder_id: str) -> List[str]:
    resp = client.get_dict("/rest/db/ignores", params={"folder": folder_id})
    # A folder without .stignore reports "ignore": null.
    ignores = resp.get("ignore")
    if ignores is None:
        return []
    if not isinstance(ignores, list):
        raise BadResponse(f"ignores for {folder_id}: expected a list")
    return ignores


def _short(device_id: str) -> str:
    return f"{device_id[:7]}..."


def _sync_local_name(ctx: Context, client: ApiClient, devices: Dict, local_id: str, name: str):
    local = devices.get(local_id)
    if local is None:
        ctx.fail(f"local device {_short(local_id)} missing from config")
        return
    if local.get("name", "") == name:
        return
    ctx.change(f"~ local device name {local.get('name', '')!r} -> {name!r}")
    ctx.do(
        "local device name",
        lambda: client.request(
            "PATCH", f"/rest/config/devices/{local['deviceID']}", {"name": name}
        ),
    )


def _sync_gui(ctx: Context, client: ApiClient, gui: Gui, current: Dict):
    username = ctx.value(gui.username, "gui username")
    password = ctx.secret(gui.password_file, "gui passwordFile")
    remote_hash = current.get("password", "")
    patch = {}
    if current.get("user") != username:
        patch["user"] = username
    if BCRYPT.fullmatch(password):
        if remote_hash != password:
            patch["password"] = password
    elif ctx.secret_changed(client.base_url, PASSWORD_IDENTITY, password) or ctx.secret_changed(
        client.base_url, PASSWORD_IDENTITY + "#remote", remote_hash
    ):
        # A plaintext password is hashed by Syncthing itself; the remote hash
        # we saw afterwards tells us whether someone changed it since.
        patch["password"] = password
    if not patch:
        return
    ctx.change(f"~ gui credentials ({', '.join('username' if k == 'user' else k for k in patch)})")
    if ctx.do("gui credentials", lambda: client.request("PATCH", "/rest/config/gui", patch)):
        if not BCRYPT.fullmatch(password):
            try:
                after = client.get_dict("/rest/config/gui")
            except ServiceError:
                return
            ctx.record_secret(client.base_url, PASSWORD_IDENTITY, password)
            ctx.record_secret(
                client.base_url, PASSWORD_IDENTITY + "#remote", after.get("password", "")
            )


def _sync_devices(
    ctx: Context,
    client: ApiClient,
    current: Dict,
    device_map: Dict[str, str],
    local_id,
    rename_local: bool,
) -> set:
    """Returns device IDs whose add failed, so dependent folder writes are skipped.

    The local device is never added or removed, and is renamed here only
    without `localDeviceName` (two owners would rename it back and forth).
    """
    failed = set()
    for name, device_id in sorted(device_map.items()):
        if device_id == local_id and not rename_local:
            continue
        cur = current.get(device_id)
        if cur is None and device_id == local_id:
            continue
        if cur is None:
            ctx.change(f"+ device {name} ({_short(device_id)})")
            if (
                not ctx.do(
                    f"add device {name}",
                    lambda n=name, d=device_id: client.request(
                        "PUT", f"/rest/config/devices/{d}", {"deviceID": d, "name": n}
                    ),
                )
                and ctx.apply
            ):
                failed.add(device_id)
        elif cur.get("name", "") != name:
            ctx.change(f"~ device {cur.get('name', '')} -> {name} ({_short(device_id)})")
            ctx.do(
                f"rename device {name}",
                lambda n=name, d=cur["deviceID"]: client.request(
                    "PATCH", f"/rest/config/devices/{d}", {"name": n}
                ),
            )
    wanted = set(device_map.values())
    for device_id, cur in current.items():
        if device_id in wanted or device_id == local_id:
            continue
        ctx.change(f"- device {cur.get('name', 'Unknown')} ({_short(device_id)})")
        if failed:
            ctx.fail(f"kept device {_short(device_id)}, a device add failed")
            continue
        ctx.do(
            f"remove device {cur.get('name', 'Unknown')}",
            lambda d=cur["deviceID"]: client.request("DELETE", f"/rest/config/devices/{d}"),
        )
    return failed


def _sync_folders(
    ctx: Context,
    client: ApiClient,
    folders: List[Folder],
    current: Dict,
    shares: Dict[str, Dict[str, Dict]],
    ignores: Dict[str, List[str]],
    resolved: Dict[str, List[str]],
    unconfigured: Dict[str, List[str]],
    local_id: str,
    failed_devices: set = frozenset(),
):
    adds_ok = True
    for folder in folders:
        stray = unconfigured.get(folder.id)
        if stray:
            ctx.fail(
                f"folder {folder.id}: held, device {_short(stray[0])} is not configured; "
                "add it to devices or deviceDefinitionsFile"
            )
            adds_ok = False
            continue
        devices = None
        if folder.devices is not None:
            devices = list(resolved[folder.id])
            # Syncthing always lists the local device on a folder; without it
            # in the desired set the comparison never converges.
            if local_id not in devices:
                devices.append(local_id)
        # An absent label is unmanaged on an existing folder; new ones get the ID.
        label = folder.label
        payload = {"label": folder.id if label is None else label, "path": folder.path}
        if label is None and folder.id in current:
            payload.pop("label")
        cur = current.get(folder.id)
        have_devices = shares.get(folder.id, {})
        if devices is not None:
            if cur is None:
                payload["devices"] = [{"deviceID": d} for d in devices]
            else:
                # Merge, not replace: existing entries carry per-device
                # settings (encryptionPassword, introducedBy) we must keep.
                kept = [entry for d, entry in have_devices.items() if d in devices]
                payload["devices"] = kept + [
                    {"deviceID": d} for d in devices if d not in have_devices
                ]
        missing = sorted(set(devices or []) & set(failed_devices))
        if missing:
            # Syncthing would silently drop the unknown device from the folder.
            ctx.fail(f"folder {folder.id}: skipped, device add failed for {len(missing)}")
            adds_ok = False
            continue
        if cur is None:
            ctx.change(f"+ folder {folder.id} ({folder.path})")
            if (
                not ctx.do(
                    f"add folder {folder.id}",
                    lambda f=folder, p=payload: client.request(
                        "PUT", f"/rest/config/folders/{f.id}", {"id": f.id, **p}
                    ),
                )
                and ctx.apply
            ):
                adds_ok = False
                continue  # ignores belong to a folder that does not exist
        else:
            diffs = []
            if label is not None and cur.get("label", "") != label:
                diffs.append("label")
            if norm_path(cur.get("path", "")) != norm_path(folder.path):
                diffs.append("path")
            if devices is not None and set(have_devices) != set(devices):
                diffs.append("devices")
            elif "devices" in payload:
                payload.pop("devices")  # unchanged: leave the remote list alone
            if diffs:
                ctx.change(f"~ folder {folder.id} ({', '.join(diffs)})")
                if "devices" in diffs:
                    for d in sorted(set(devices) - set(have_devices)):
                        ctx.change(f"+ folder {folder.id} device {_short(d)}")
                    for d in sorted(set(have_devices) - set(devices)):
                        ctx.change(f"- folder {folder.id} device {_short(d)}")
                ctx.do(
                    f"update folder {folder.id}",
                    lambda f=folder, p=payload: client.request(
                        "PATCH", f"/rest/config/folders/{f.id}", p
                    ),
                )

        if folder.ignore_patterns is None:
            continue
        have_ignores = ignores.get(folder.id, [])
        if have_ignores != folder.ignore_patterns:
            ctx.change(f"~ folder {folder.id} ignores")
            ctx.do(
                f"folder {folder.id} ignores",
                lambda f=folder: client.request(
                    "POST",
                    "/rest/db/ignores",
                    {"ignore": f.ignore_patterns},
                    params={"folder": f.id},
                ),
            )

    wanted = {f.id for f in folders}
    for fid, cur in current.items():
        if fid not in wanted:
            ctx.change(f"- folder {cur.get('label') or fid} ({fid})")
            if not adds_ok:
                ctx.fail(f"kept folder {fid}, a folder add failed")
                continue
            ctx.do(
                f"remove folder {fid}",
                lambda fid=fid: client.request("DELETE", f"/rest/config/folders/{fid}"),
            )
