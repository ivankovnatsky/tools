"""Jellyfin libraries and bind address (ported from nix-config jellyfin-mgmt).

Deletion: libraries are never deleted. Within a listed library the `paths`
list is authoritative: media paths not listed are removed from it.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from tools.mgmt import identity
from tools.mgmt import spec as s
from tools.mgmt.engine import Context, Endpoint
from tools.mgmt.http import ApiClient, BadResponse, InvalidSpec, dict_field, list_field

LIBRARY_TYPES = ("movies", "tvshows", "music", "books", "photos")


@dataclass
class Library:
    name: str
    paths: List[str]
    type: str = "movies"
    enable_realtime_monitor: Optional[bool] = None
    automatic_refresh_interval_days: Optional[int] = None

    @property
    def has_options(self) -> bool:
        return (
            self.enable_realtime_monitor is not None
            or self.automatic_refresh_interval_days is not None
        )


@dataclass
class JellyfinSpec:
    endpoint: Endpoint
    api_key_file: str
    bind_address: Optional[str] = None
    libraries: Optional[List[Library]] = None


def parse(raw, base_dir: Optional[str] = None) -> JellyfinSpec:
    raw = s.mapping(raw, "jellyfin")
    s.check_keys(raw, s.ENDPOINT_KEYS | {"apiKeyFile", "bindAddress", "libraries"}, "jellyfin")
    key = s.path(raw, "apiKeyFile", "jellyfin", base_dir)
    if not key:
        raise InvalidSpec("jellyfin.apiKeyFile is required")
    libraries = None
    if s.present(raw, "libraries"):
        libraries = []
        for i, x in enumerate(s.items(raw["libraries"], "jellyfin.libraries")):
            where = f"jellyfin.libraries[{i}]"
            s.check_keys(
                x,
                {"name", "type", "paths", "enableRealtimeMonitor", "automaticRefreshIntervalDays"},
                where,
            )
            kind = s.string(x, "type", where, "movies")
            if kind not in LIBRARY_TYPES:
                raise InvalidSpec(f"{where}.type must be one of {', '.join(LIBRARY_TYPES)}")
            paths = identity.unique(
                s.strings(x.get("paths"), f"{where}.paths"), f"{where}.paths", identity.path
            )
            if not paths:
                raise InvalidSpec(f"{where}.paths is required")
            # New libraries get their paths in the query string, where Jellyfin
            # splits on commas even when they are URL-encoded.
            if any("," in p for p in paths):
                raise InvalidSpec(f"{where}.paths: a path cannot contain a comma")
            libraries.append(
                Library(
                    name=s.required(x, "name", where),
                    paths=paths,
                    type=kind,
                    enable_realtime_monitor=s.boolean(x, "enableRealtimeMonitor", where),
                    automatic_refresh_interval_days=s.integer(
                        x, "automaticRefreshIntervalDays", where
                    ),
                )
            )
    if libraries is not None:
        identity.unique((lib.name for lib in libraries), "jellyfin.libraries")
    return JellyfinSpec(
        endpoint=s.endpoint(raw, "jellyfin", "http://localhost:8096", base_dir),
        api_key_file=key,
        bind_address=s.string(raw, "bindAddress", "jellyfin"),
        libraries=libraries,
    )


def reconcile(spec: JellyfinSpec, ctx: Context) -> None:
    key = ctx.secret(spec.api_key_file, "apiKeyFile")
    client = ctx.client(spec.endpoint, {"X-Emby-Token": key})
    ctx.kept(
        "libraries",
        "library",
        ((lib.name, lib.name) for lib in spec.libraries or []),
        "libraries are never deleted",
    )
    # Both snapshots are read and validated before the first write.
    network = _read_network(client) if spec.bind_address is not None else None
    libraries = _read_libraries(client, spec.libraries) if spec.libraries is not None else None
    if network is not None:
        _sync_network(ctx, client, [spec.bind_address], network)
    if libraries is not None:
        _sync_libraries(ctx, client, spec.libraries, *libraries)


def _libraries(client: ApiClient) -> Dict:
    return identity.index(
        client.get_list("/Library/VirtualFolders", ("Name",)),
        "libraries",
        key=lambda x: x["Name"],
        remote=True,
    )


def _read_network(client: ApiClient) -> Dict:
    current = client.get_dict("/System/Configuration/network")
    have = list_field(current, "LocalNetworkAddresses", "network config")
    if not all(isinstance(a, str) for a in have):
        raise BadResponse("network config.LocalNetworkAddresses: expected strings")
    return current


def _sync_network(ctx: Context, client: ApiClient, addresses: List[str], current: Dict) -> None:
    have = list_field(current, "LocalNetworkAddresses", "network config")
    if set(have) == set(addresses):
        return
    ctx.change(f"~ LocalNetworkAddresses {have} -> {addresses} (Jellyfin restart needed)")
    ctx.do(
        "network config",
        lambda: client.request(
            "POST",
            "/System/Configuration/network",
            {**current, "LocalNetworkAddresses": addresses},
        ),
    )


def _read_libraries(client: ApiClient, libraries: List[Library]):
    current = _libraries(client)
    locations = {
        n: identity.index(
            list_field(cur, "Locations", f"library {n}"),
            f"library {n} paths",
            kind=identity.path,
            remote=True,
        )
        for n, cur in current.items()
    }
    for lib in libraries:
        if lib.has_options and lib.name in current:
            dict_field(current[lib.name], "LibraryOptions", f"library {lib.name}")
    return current, locations


def _sync_libraries(
    ctx: Context, client: ApiClient, libraries: List[Library], current: Dict, locations: Dict
) -> None:
    failed_creates = set()
    paths_changed = set()
    for lib in libraries:
        cur = current.get(lib.name)
        if cur is None:
            ctx.change(f"+ library {lib.name} ({lib.type}): {', '.join(lib.paths)}")
            if (
                not ctx.do(
                    f"create library {lib.name}",
                    lambda lib=lib: client.request(
                        "POST",
                        "/Library/VirtualFolders",
                        params={
                            "name": lib.name,
                            "collectionType": lib.type,
                            "paths": lib.paths,
                            "refreshLibrary": True,
                        },
                    ),
                )
                and ctx.apply
            ):
                failed_creates.add(lib.name)
            continue
        # Compared normalized; removals send the remote's own spelling.
        have = locations[lib.name]
        remove = [orig for norm, orig in have.items() if norm not in lib.paths]
        add = [p for p in lib.paths if p not in have]
        # Add before remove: a library left with no paths can have its items
        # purged by the next scan, so stale paths go only once every add worked.
        adds_ok = True
        applied_before = ctx.applied
        for i, p in enumerate(add):
            ctx.change(f"+ library {lib.name} path {p}")
            if (
                not ctx.do(
                    f"add {lib.name} path {p}",
                    lambda i=i, p=p: client.request(
                        "POST",
                        "/Library/VirtualFolders/Paths",
                        {"Name": lib.name, "Path": p},
                        params={"refreshLibrary": i == len(add) - 1},
                    ),
                )
                and ctx.apply
            ):
                adds_ok = False
        for p in remove:
            ctx.change(f"- library {lib.name} path {p}")
            if not adds_ok:
                ctx.fail(f"library {lib.name}: kept path {p}, an add failed")
                continue
            ctx.do(
                f"remove {lib.name} path {p}",
                lambda p=p: client.request(
                    "DELETE", "/Library/VirtualFolders/Paths", params={"name": lib.name, "path": p}
                ),
            )
        if ctx.applied > applied_before:
            paths_changed.add(lib.name)

    # Options depend on the library existing: skip those whose create failed.
    wanted = [lib for lib in libraries if lib.has_options and lib.name not in failed_creates]
    if not wanted:
        return
    if ctx.apply and any(lib.name not in current or lib.name in paths_changed for lib in wanted):
        # New libraries only get an ItemId once they exist, and the options
        # carry PathInfos: posting the pre-change copy would undo path edits.
        current = _libraries(client)
    for lib in wanted:
        cur = current.get(lib.name)
        if cur is None:
            if not ctx.apply:
                ctx.change(f"~ library {lib.name} options (after create)")
            else:
                ctx.fail(f"library {lib.name}: not found after create, options not set")
            continue
        options = dict(dict_field(cur, "LibraryOptions", f"library {lib.name}"))
        item_id = cur.get("ItemId")
        if not isinstance(item_id, str) or not item_id:
            raise BadResponse(f"library {lib.name}: no ItemId")
        changes = []
        if lib.enable_realtime_monitor is not None:
            have = options.get("EnableRealtimeMonitor", False)
            if have != lib.enable_realtime_monitor:
                changes.append(f"EnableRealtimeMonitor: {have} -> {lib.enable_realtime_monitor}")
                options["EnableRealtimeMonitor"] = lib.enable_realtime_monitor
        if lib.automatic_refresh_interval_days is not None:
            have = options.get("AutomaticRefreshIntervalDays", 0)
            if have != lib.automatic_refresh_interval_days:
                changes.append(
                    f"AutomaticRefreshIntervalDays: {have} -> {lib.automatic_refresh_interval_days}"
                )
                options["AutomaticRefreshIntervalDays"] = lib.automatic_refresh_interval_days
        if not changes:
            continue
        ctx.change(f"~ library {lib.name} options ({', '.join(changes)})")
        ctx.do(
            f"library {lib.name} options",
            lambda item_id=item_id, options=options: client.request(
                "POST",
                "/Library/VirtualFolders/LibraryOptions",
                {"Id": item_id, "LibraryOptions": options},
            ),
        )
