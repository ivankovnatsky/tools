"""*arr stack: Radarr, Sonarr, Lidarr, Prowlarr (ported from nix-config arr-mgmt).

Deletion: listing `rootFolders`, `indexers` or `applications` makes that list
authoritative, as in arr-mgmt: remote entries not listed are deleted. An
absent key leaves the remote list alone. Download clients are never deleted.
"""

import copy
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from tools.services import identity
from tools.services import spec as s
from tools.services.engine import Context, Endpoint
from tools.services.http import ApiClient, InvalidSpec, ServiceError, list_field

MASK = "********"
DEFAULT_URLS = {
    "radarr": "http://localhost:7878",
    "sonarr": "http://localhost:8989",
    "lidarr": "http://localhost:8686",
    "prowlarr": "http://localhost:9696",
}
API_PREFIX = {"radarr": "/api/v3", "sonarr": "/api/v3", "lidarr": "/api/v1", "prowlarr": "/api/v1"}
CATEGORY_FIELD = {"radarr": "movieCategory", "sonarr": "tvCategory", "lidarr": "musicCategory"}
# Lidarr/Radarr/Sonarr first, then Prowlarr: its applications point at them.
ORDER = ("lidarr", "radarr", "sonarr", "prowlarr")


@dataclass
class DownloadClient:
    name: str
    username: Dict[str, str]
    password_file: str
    host: str = "localhost"
    port: int = 9091
    use_ssl: bool = False
    url_base: str = "/transmission/"
    category: str = ""
    add_paused: bool = False
    enable: bool = True
    priority: int = 1
    remove_completed: bool = True
    remove_failed: bool = True


@dataclass
class Indexer:
    name: str
    definition_name: Optional[str]
    enable: bool = True
    priority: int = 25
    username: Optional[Dict[str, str]] = None
    password_file: Optional[str] = None


@dataclass
class Application:
    name: str
    base_url: str
    api_key_file: str
    prowlarr_url: str = "http://localhost:9696"
    sync_level: str = "fullSync"
    sync_categories: List[int] = field(default_factory=list)
    enable: bool = True


@dataclass
class App:
    kind: str
    endpoint: Endpoint
    api_key_file: str
    bind_address: Optional[str] = None
    download_clients: Optional[List[DownloadClient]] = None
    root_folders: Optional[List[str]] = None
    indexers: Optional[List[Indexer]] = None
    applications: Optional[List[Application]] = None
    prune: bool = False


@dataclass
class ArrSpec:
    apps: List[App]


# -- parse -------------------------------------------------------------------


def _parse_download_client(raw: Dict, where: str, base_dir) -> DownloadClient:
    s.check_keys(
        raw,
        {
            "name", "host", "port", "useSsl", "urlBase", "username", "usernameFile",
            "passwordFile", "category", "addPaused", "enable", "priority",
            "removeCompletedDownloads", "removeFailedDownloads",
        },
        where,
    )  # fmt: skip
    pw = s.path(raw, "passwordFile", where, base_dir)
    if not pw:
        raise InvalidSpec(f"{where}.passwordFile is required")
    return DownloadClient(
        name=s.required(raw, "name", where),
        username=s.one_of(raw, "username", where, base_dir),
        password_file=pw,
        host=s.string(raw, "host", where, "localhost"),
        port=s.integer(raw, "port", where, 9091),
        use_ssl=s.boolean(raw, "useSsl", where, False),
        url_base=s.string(raw, "urlBase", where, "/transmission/"),
        category=s.string(raw, "category", where, ""),
        add_paused=s.boolean(raw, "addPaused", where, False),
        enable=s.boolean(raw, "enable", where, True),
        priority=s.integer(raw, "priority", where, 1),
        remove_completed=s.boolean(raw, "removeCompletedDownloads", where, True),
        remove_failed=s.boolean(raw, "removeFailedDownloads", where, True),
    )


def _parse_indexer(raw: Dict, where: str, base_dir) -> Indexer:
    s.check_keys(
        raw,
        {
            "name",
            "definitionName",
            "enable",
            "priority",
            "username",
            "usernameFile",
            "passwordFile",
        },
        where,
    )
    has_user = bool(raw.get("username") or raw.get("usernameFile"))
    pw = s.path(raw, "passwordFile", where, base_dir)
    if has_user != bool(pw):
        raise InvalidSpec(f"{where}: username(File) and passwordFile go together")
    return Indexer(
        name=s.required(raw, "name", where),
        definition_name=s.string(raw, "definitionName", where),
        enable=s.boolean(raw, "enable", where, True),
        priority=s.integer(raw, "priority", where, 25),
        username=s.one_of(raw, "username", where, base_dir) if has_user else None,
        password_file=pw,
    )


def _parse_application(raw: Dict, where: str, base_dir) -> Application:
    s.check_keys(
        raw,
        {"name", "baseUrl", "apiKeyFile", "prowlarrUrl", "syncLevel", "syncCategories", "enable"},
        where,
    )
    level = s.string(raw, "syncLevel", where, "fullSync")
    if level not in ("disabled", "addOnly", "fullSync"):
        raise InvalidSpec(f"{where}.syncLevel must be disabled, addOnly or fullSync")
    cats = raw.get("syncCategories")
    if cats is not None and (
        not isinstance(cats, list) or not all(isinstance(c, int) for c in cats)
    ):
        raise InvalidSpec(f"{where}.syncCategories must be a list of integers")
    key = s.path(raw, "apiKeyFile", where, base_dir)
    if not key:
        raise InvalidSpec(f"{where}.apiKeyFile is required")
    return Application(
        name=s.required(raw, "name", where),
        base_url=s.required(raw, "baseUrl", where),
        api_key_file=key,
        prowlarr_url=s.string(raw, "prowlarrUrl", where, "http://localhost:9696"),
        sync_level=level,
        sync_categories=list(cats) if cats is not None else [],
        enable=s.boolean(raw, "enable", where, True),
    )


def parse(raw, base_dir: Optional[str] = None) -> ArrSpec:
    raw = s.mapping(raw, "arr")
    s.check_keys(raw, set(ORDER), "arr")
    apps = []
    for kind in ORDER:
        if not s.present(raw, kind):
            continue
        where = f"arr.{kind}"
        sub = s.mapping(raw[kind], where)
        common = s.ENDPOINT_KEYS | {"apiKeyFile", "bindAddress"}
        if kind == "prowlarr":
            s.check_keys(sub, common | {"indexers", "applications"}, where)
        else:
            s.check_keys(sub, common | {"downloadClients", "rootFolders"}, where)
        key = s.path(sub, "apiKeyFile", where, base_dir)
        if not key:
            raise InvalidSpec(f"{where}.apiKeyFile is required")
        app = App(
            kind=kind,
            endpoint=s.endpoint(sub, where, DEFAULT_URLS[kind], base_dir),
            api_key_file=key,
            bind_address=s.string(sub, "bindAddress", where),
            prune=s.prune(sub, where),
        )
        if s.present(sub, "downloadClients"):
            app.download_clients = [
                _parse_download_client(dc, f"{where}.downloadClients[{i}]", base_dir)
                for i, dc in enumerate(s.items(sub["downloadClients"], where))
            ]
        if s.present(sub, "rootFolders"):
            app.root_folders = s.strings(sub["rootFolders"], f"{where}.rootFolders")
        if s.present(sub, "indexers"):
            app.indexers = [
                _parse_indexer(x, f"{where}.indexers[{i}]", base_dir)
                for i, x in enumerate(s.items(sub["indexers"], where))
            ]
        if s.present(sub, "applications"):
            app.applications = [
                _parse_application(x, f"{where}.applications[{i}]", base_dir)
                for i, x in enumerate(s.items(sub["applications"], where))
            ]
        if app.download_clients is not None:
            identity.unique((d.name for d in app.download_clients), f"{where}.downloadClients")
        if app.root_folders is not None:
            app.root_folders = identity.unique(
                app.root_folders, f"{where}.rootFolders", identity.path
            )
        if app.indexers is not None:
            identity.unique((x.name for x in app.indexers), f"{where}.indexers")
        if app.applications is not None:
            identity.unique((x.name for x in app.applications), f"{where}.applications")
        apps.append(app)
    return ArrSpec(apps=apps)


# -- reconcile ---------------------------------------------------------------


def reconcile(spec: ArrSpec, ctx: Context) -> None:
    # Inputs of every app before the first write: a bad file for a later app
    # must not follow writes to an earlier one.
    clients = {}
    for app in spec.apps:
        try:
            clients[app.kind] = _resolve_inputs(app, ctx)
        except ServiceError as e:
            ctx.fail(f"{app.kind}: {e}")
    if not ctx.ok:
        return
    apps = {app.kind: app for app in spec.apps}
    for kind in ORDER:
        if kind in CATEGORY_FIELD:
            dcs = (apps.get(kind) and apps[kind].download_clients) or []
            ctx.kept(
                f"{kind} download clients",
                f"{kind} download client",
                ((dc.name, dc.name) for dc in dcs),
                "download clients are never deleted",
            )
    for app in spec.apps:
        try:
            _reconcile_app(app, clients[app.kind], ctx)
        except ServiceError as e:
            ctx.fail(f"{app.kind}: {e}")
        except Exception as e:  # noqa: BLE001 - one app's bug must not skip the others
            ctx.fail(f"{app.kind}: unexpected {type(e).__name__}: {e}")


def _reconcile_app(app: App, client: ApiClient, ctx: Context) -> None:
    prefix = API_PREFIX[app.kind]

    # Every collection is read and validated before the first write, so a bad
    # read of a later one cannot follow destructive writes to an earlier one.
    steps = []
    if app.bind_address is not None:
        steps.append((_sync_host_config, client.get_dict(f"{prefix}/config/host")))
    if app.download_clients is not None:
        steps.append((_sync_download_clients, _read_download_clients(app, client, prefix)))
    if app.root_folders is not None:
        steps.append((_sync_root_folders, _read_root_folders(app, client, prefix, ctx)))
    if app.indexers is not None:
        steps.append((_sync_indexers, _read_indexers(app, client, ctx)))
    if app.applications is not None:
        steps.append((_sync_applications, _read_applications(app, client, ctx)))
    for sync, current in steps:
        sync(app, client, prefix, ctx, current)


def _resolve_inputs(app: App, ctx: Context) -> ApiClient:
    """Read every input file up front; the sync steps then reuse the cached values."""
    api_key = ctx.secret(app.api_key_file, f"{app.kind} apiKeyFile")
    client = ctx.client(app.endpoint, {"X-Api-Key": api_key})
    client.label = app.kind
    for dc in app.download_clients or []:
        ctx.value(dc.username, f"{dc.name} username")
        ctx.secret(dc.password_file, f"{dc.name} passwordFile")
    for a in app.applications or []:
        ctx.secret(a.api_key_file, f"{a.name} apiKeyFile")
    for idx in app.indexers or []:
        if idx.username is not None:
            ctx.value(idx.username, f"{idx.name} username")
            ctx.secret(idx.password_file, f"{idx.name} passwordFile")
    return client


def _sync_host_config(app: App, client: ApiClient, prefix: str, ctx: Context, current) -> None:
    have = current.get("bindAddress", "*")
    if have == app.bind_address:
        return
    ctx.change(f"~ {app.kind} host config (bindAddress: {have} -> {app.bind_address})")
    ctx.do(
        f"{app.kind} host config",
        lambda: client.request(
            "PUT", f"{prefix}/config/host", {**current, "bindAddress": app.bind_address}
        ),
    )


def _transmission_fields(dc: DownloadClient, username: str, password: str, category_field: str):
    return [
        {"name": "host", "value": dc.host},
        {"name": "port", "value": dc.port},
        {"name": "useSsl", "value": dc.use_ssl},
        {"name": "urlBase", "value": dc.url_base},
        {"name": "username", "value": username},
        {"name": "password", "value": password},
        {"name": category_field, "value": dc.category},
        {"name": "addPaused", "value": dc.add_paused},
    ]


def _read_download_clients(app: App, client: ApiClient, prefix: str) -> Dict:
    current = identity.index(
        client.get_list(f"{prefix}/downloadclient", ("id", "name")),
        f"{app.kind} download clients",
        key=lambda x: x["name"],
        remote=True,
    )
    for dc in app.download_clients:
        if dc.name in current:
            _field_map(current[dc.name], f"{app.kind} download client {dc.name}")
    return current


def _sync_download_clients(
    app: App, client: ApiClient, prefix: str, ctx: Context, current: Dict
) -> None:
    category_field = CATEGORY_FIELD[app.kind]
    for dc in app.download_clients:
        username = ctx.value(dc.username, f"{dc.name} username")
        password = ctx.secret(dc.password_file, f"{dc.name} passwordFile")
        secret_id = f"arr/{app.kind}/downloadClient/{dc.name}/password"
        fields = _transmission_fields(dc, username, password, category_field)

        if dc.name in current:
            cur = current[dc.name]
            cur_fields = _field_map(cur, f"{app.kind} download client {dc.name}")
            changed = [
                f["name"]
                for f in fields
                if f["name"] != "password"
                and f["name"] in cur_fields
                and f["value"] != cur_fields[f["name"]].get("value")
            ]
            if ctx.secret_changed(client.base_url, secret_id, password):
                changed.append("password")
            if not changed:
                continue
            ctx.change(f"~ {app.kind} download client {dc.name} ({', '.join(changed)})")
            payload = {
                **copy.deepcopy(cur),
                "fields": _merge_fields(
                    _fields(cur, f"{app.kind} download client {dc.name}"), fields
                ),
            }
            if ctx.do(
                f"update {app.kind} download client {dc.name}",
                lambda: client.request("PUT", f"{prefix}/downloadclient/{cur['id']}", payload),
            ):
                ctx.record_secret(client.base_url, secret_id, password)
        else:
            ctx.change(f"+ {app.kind} download client {dc.name}")
            payload = {
                "enable": dc.enable,
                "protocol": "torrent",
                "priority": dc.priority,
                "removeCompletedDownloads": dc.remove_completed,
                "removeFailedDownloads": dc.remove_failed,
                "name": dc.name,
                "fields": fields,
                "implementationName": "Transmission",
                "implementation": "Transmission",
                "configContract": "TransmissionSettings",
                "tags": [],
            }
            if ctx.do(
                f"create {app.kind} download client {dc.name}",
                lambda: client.request("POST", f"{prefix}/downloadclient", payload),
            ):
                ctx.record_secret(client.base_url, secret_id, password)


def _read_root_folders(app: App, client: ApiClient, prefix: str, ctx: Context) -> Dict:
    current = identity.index(
        client.get_list(f"{prefix}/rootfolder", ("id", "path")),
        f"{app.kind} root folders",
        key=lambda rf: rf["path"],
        kind=identity.path,
        remote=True,
    )
    ctx.check_prune(f"{app.kind} root folders", len(app.root_folders), len(current), app.prune)
    return current


def _sync_root_folders(
    app: App, client: ApiClient, prefix: str, ctx: Context, current: Dict
) -> None:
    desired = set(app.root_folders)
    missing = [p for p in app.root_folders if p not in current]
    extra: Dict = {}
    if missing and ctx.apply and app.kind == "lidarr":
        # Lidarr refuses a root folder without a name and default profiles.
        quality = client.get_list(f"{prefix}/qualityprofile", ("id",))
        metadata = client.get_list(f"{prefix}/metadataprofile", ("id",))
        if quality:
            extra["defaultQualityProfileId"] = quality[0]["id"]
        if metadata:
            extra["defaultMetadataProfileId"] = metadata[0]["id"]
    creates_ok = True
    for path in missing:
        ctx.change(f"+ {app.kind} root folder {path}")
        data = {"path": path}
        if extra:
            data.update(extra, name=path.rstrip("/").rsplit("/", 1)[-1] or path)
        if (
            not ctx.do(
                f"create {app.kind} root folder {path}",
                lambda data=data: client.request("POST", f"{prefix}/rootfolder", data),
            )
            and ctx.apply
        ):
            creates_ok = False

    # Delete after create, and not at all if a create failed: a swapped root
    # folder must never leave the app with none.
    for path, cur in current.items():
        if path not in desired:
            ctx.change(f"- {app.kind} root folder {path}")
            if not creates_ok:
                ctx.fail(f"{app.kind}: kept root folder {path}, a create failed")
                continue
            ctx.do(
                f"delete {app.kind} root folder {path}",
                lambda cur=cur: client.request("DELETE", f"{prefix}/rootfolder/{cur['id']}"),
            )


def _app_fields(a: Application, api_key: str) -> List[Dict]:
    return [
        {"name": "prowlarrUrl", "value": a.prowlarr_url},
        {"name": "baseUrl", "value": a.base_url},
        {"name": "apiKey", "value": api_key},
        {"name": "syncCategories", "value": a.sync_categories},
    ]


def _fields(obj: Dict, what: str) -> List[Dict]:
    return list_field(obj, "fields", what, ("name",))


def _field_map(obj: Dict, what: str) -> Dict:
    return identity.index(
        _fields(obj, what), f"{what} fields", key=lambda f: f["name"], remote=True
    )


def _merge_fields(existing: List[Dict], managed: List[Dict]) -> List[Dict]:
    """The server's full field list with only the managed values replaced.

    *arr PUTs replace the whole object: sending just our fields would reset
    every setting we do not manage.
    """
    merged = copy.deepcopy(existing or [])
    for f in managed:
        _set_field(merged, f["name"], f["value"])
    return merged


def _read_applications(app: App, client: ApiClient, ctx: Context) -> Dict:
    current = identity.index(
        client.get_list("/api/v1/applications", ("id", "name")),
        "prowlarr applications",
        key=lambda x: x["name"],
        remote=True,
    )
    desired = identity.index(app.applications, "prowlarr.applications", key=lambda a: a.name)
    ctx.check_prune("prowlarr applications", len(desired), len(current), app.prune)
    for name in desired.keys() & current.keys():
        _field_map(current[name], f"prowlarr application {name}")
    return current


def _sync_applications(app: App, client: ApiClient, prefix: str, ctx: Context, current) -> None:
    desired = identity.index(app.applications, "prowlarr.applications", key=lambda a: a.name)
    creates_ok = _create_or_update_applications(ctx, client, desired, current)
    _delete_stale(ctx, client, "application", "/api/v1/applications", current, desired, creates_ok)


def _delete_stale(
    ctx: Context, client: ApiClient, what: str, path: str, current: Dict, desired: Dict, ok: bool
) -> None:
    """Runs after every create, and not at all if one failed: a renamed
    entry must never leave Prowlarr with neither the old nor the new one."""
    for name, cur in current.items():
        if name in desired:
            continue
        ctx.change(f"- prowlarr {what} {name}")
        if not ok:
            ctx.fail(f"prowlarr: kept {what} {name}, a create failed")
            continue
        ctx.do(
            f"delete prowlarr {what} {name}",
            lambda cur=cur: client.request("DELETE", f"{path}/{cur['id']}"),
        )


def _create_or_update_applications(
    ctx: Context, client: ApiClient, desired: Dict, current: Dict
) -> bool:
    creates_ok = True
    for name, a in desired.items():
        api_key = ctx.secret(a.api_key_file, f"{name} apiKeyFile")
        secret_id = f"arr/prowlarr/application/{name}/apiKey"
        fields = _app_fields(a, api_key)
        cur = current.get(name)
        if cur is not None:
            cur_fields = _field_map(cur, f"prowlarr application {name}")
            changed = [
                f["name"]
                for f in fields
                if f["name"] != "apiKey"
                and f["name"] in cur_fields
                and f["value"] != cur_fields[f["name"]].get("value")
            ]
            if ctx.secret_changed(client.base_url, secret_id, api_key):
                changed.append("apiKey")
            if a.sync_level != cur.get("syncLevel"):
                changed.append("syncLevel")
            if not changed:
                continue
            ctx.change(f"~ prowlarr application {name} ({', '.join(changed)})")
            payload = {
                **copy.deepcopy(cur),
                "syncLevel": a.sync_level,
                "fields": _merge_fields(_fields(cur, f"prowlarr application {name}"), fields),
            }
            if ctx.do(
                f"update prowlarr application {name}",
                lambda: client.request("PUT", f"/api/v1/applications/{cur['id']}", payload),
            ):
                ctx.record_secret(client.base_url, secret_id, api_key)
            continue

        ctx.change(f"+ prowlarr application {name}")
        lower = name.lower()
        impl = "Radarr" if "radarr" in lower else "Lidarr" if "lidarr" in lower else "Sonarr"
        payload = {
            "syncLevel": a.sync_level,
            "enable": a.enable,
            "name": name,
            "fields": fields,
            "implementationName": impl,
            "implementation": impl,
            "configContract": f"{impl}Settings",
            "tags": [],
        }
        if ctx.do(
            f"create prowlarr application {name}",
            lambda payload=payload: client.request("POST", "/api/v1/applications", payload),
        ):
            ctx.record_secret(client.base_url, secret_id, api_key)
        elif ctx.apply:
            creates_ok = False
    return creates_ok


def _set_field(fields: List[Dict], name: str, value) -> None:
    for f in fields:
        if f.get("name") == name:
            f["value"] = value
            return
    fields.append({"name": name, "value": value})


def _read_indexers(app: App, client: ApiClient, ctx: Context) -> Dict:
    current = identity.index(
        client.get_list("/api/v1/indexer", ("id", "name")),
        "prowlarr indexers",
        key=lambda x: x["name"],
        remote=True,
    )
    desired = identity.index(app.indexers, "prowlarr.indexers", key=lambda i: i.name)
    ctx.check_prune("prowlarr indexers", len(desired), len(current), app.prune)
    for name in desired.keys() & current.keys():
        _field_map(current[name], f"prowlarr indexer {name}")
    return current


def _sync_indexers(app: App, client: ApiClient, prefix: str, ctx: Context, current) -> None:
    desired = identity.index(app.indexers, "prowlarr.indexers", key=lambda i: i.name)
    creates_ok = _create_or_update_indexers(ctx, client, desired, current)
    _delete_stale(ctx, client, "indexer", "/api/v1/indexer", current, desired, creates_ok)


def _create_or_update_indexers(
    ctx: Context, client: ApiClient, desired: Dict, current: Dict
) -> bool:
    creates_ok = True
    schema_map = None
    for name, idx in desired.items():
        username = password = None
        if idx.username is not None:
            username = ctx.value(idx.username, f"{name} username")
            password = ctx.secret(idx.password_file, f"{name} passwordFile")
        secret_id = f"arr/prowlarr/indexer/{name}/password"

        if name in current:
            cur = current[name]
            changed = []
            if idx.enable != cur.get("enable"):
                changed.append(f"enable: {cur.get('enable')} -> {idx.enable}")
            if idx.priority != cur.get("priority"):
                changed.append(f"priority: {cur.get('priority')} -> {idx.priority}")
            if username is not None:
                cur_fields = _field_map(cur, f"prowlarr indexer {name}")
                if cur_fields.get("username", {}).get("value") != username:
                    changed.append("username")
                if ctx.secret_changed(client.base_url, secret_id, password):
                    changed.append("password")
            if not changed:
                continue
            ctx.change(f"~ prowlarr indexer {name} ({', '.join(changed)})")
            payload = copy.deepcopy(cur)
            payload["enable"] = idx.enable
            payload["priority"] = idx.priority
            if username is not None:
                payload["fields"] = copy.deepcopy(_fields(cur, f"prowlarr indexer {name}"))
                _set_field(payload["fields"], "username", username)
                _set_field(payload["fields"], "password", password)
            if (
                ctx.do(
                    f"update prowlarr indexer {name}",
                    lambda: client.request("PUT", f"/api/v1/indexer/{cur['id']}", payload),
                )
                and password is not None
            ):
                ctx.record_secret(client.base_url, secret_id, password)
            continue

        if not idx.definition_name:
            ctx.fail(f"prowlarr indexer {name}: definitionName is required to create it")
            creates_ok = False
            continue
        ctx.change(f"+ prowlarr indexer {name} (definitionName: {idx.definition_name})")
        if not ctx.apply:
            continue
        if schema_map is None:
            try:
                schema_map = {
                    sc["definitionName"]: sc
                    for sc in client.get_list("/api/v1/indexer/schema", ("definitionName",))
                }
            except ServiceError as e:
                schema_map = e
        if isinstance(schema_map, ServiceError):
            ctx.fail(f"prowlarr indexer {name}: schema unavailable: {schema_map}")
            creates_ok = False
            continue
        schema = schema_map.get(idx.definition_name)
        if schema:
            payload = copy.deepcopy(schema)
            payload.update(name=name, enable=idx.enable, priority=idx.priority, appProfileId=1)
            fields = _fields(payload, f"{idx.definition_name} schema")
            names = {f.get("name") for f in fields}
            if username is not None and "username" in names:
                _set_field(fields, "username", username)
            if password is not None and "password" in names:
                _set_field(fields, "password", password)
            payload["fields"] = fields
        else:
            # Unknown to the schema: public Cardigann definition, as arr-mgmt did.
            fields = [{"name": "definitionFile", "value": idx.definition_name}]
            if username is not None:
                fields += [
                    {"name": "username", "value": username},
                    {"name": "password", "value": password},
                ]
            payload = {
                "definitionName": idx.definition_name,
                "name": name,
                "enable": idx.enable,
                "priority": idx.priority,
                "appProfileId": 1,
                "protocol": "torrent",
                "implementationName": "Cardigann",
                "implementation": "Cardigann",
                "configContract": "CardigannSettings",
                "fields": fields,
            }
        if not ctx.do(
            f"create prowlarr indexer {name}",
            lambda: client.request("POST", "/api/v1/indexer", payload),
        ):
            creates_ok = False
        elif password is not None:
            ctx.record_secret(client.base_url, secret_id, password)
    return creates_ok
