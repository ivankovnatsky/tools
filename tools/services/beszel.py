"""Beszel hub systems and user settings (ported from nix-config beszel-mgmt).

Deletion: a present `systems` list is authoritative, as in beszel-mgmt:
systems on the hub that are not listed are deleted. The Discord webhook is
only ever added, never removed.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from tools.services import identity
from tools.services import spec as s
from tools.services.engine import Context, Endpoint
from tools.services.http import (
    ApiClient,
    ApiError,
    AuthFailed,
    BadResponse,
    InvalidSpec,
    check_dict,
    check_list,
    list_field,
)

DEFAULT_PORT = "45876"
PER_PAGE = 200
_DISCORD = re.compile(r"https://discord\.com/api/webhooks/(\d+)/([A-Za-z0-9_-]+)")


@dataclass
class System:
    name: str
    host: str
    port: str = DEFAULT_PORT


@dataclass
class BeszelSpec:
    endpoint: Endpoint
    email_file: str
    password_file: str
    discord_webhook_file: Optional[str] = None
    systems: Optional[List[System]] = None
    user_settings: Dict = field(default_factory=dict)
    prune: bool = False


def parse(raw, base_dir: Optional[str] = None) -> BeszelSpec:
    raw = s.mapping(raw, "beszel")
    s.check_keys(
        raw,
        s.ENDPOINT_KEYS
        | {"emailFile", "passwordFile", "discordWebhookFile", "systems", "userSettings"},
        "beszel",
    )
    email = s.path(raw, "emailFile", "beszel", base_dir)
    password = s.path(raw, "passwordFile", "beszel", base_dir)
    if not email or not password:
        raise InvalidSpec("beszel: emailFile and passwordFile are required")
    systems = None
    if s.present(raw, "systems"):
        systems = []
        for i, x in enumerate(s.items(raw["systems"], "beszel.systems")):
            where = f"beszel.systems[{i}]"
            s.check_keys(x, {"name", "host", "port"}, where)
            port = x.get("port", DEFAULT_PORT)
            if isinstance(port, bool) or not isinstance(port, (str, int)):
                raise InvalidSpec(f"{where}.port must be a string or integer")
            systems.append(
                System(
                    name=s.required(x, "name", where),
                    host=s.required(x, "host", where),
                    port=str(port),
                )
            )
    if systems is not None:
        identity.unique((x.name for x in systems), "beszel.systems")
    return BeszelSpec(
        endpoint=s.endpoint(raw, "beszel", None, base_dir),
        email_file=email,
        password_file=password,
        discord_webhook_file=s.path(raw, "discordWebhookFile", "beszel", base_dir),
        systems=systems,
        user_settings=dict(s.mapping(raw.get("userSettings"), "beszel.userSettings")),
        prune=s.prune(raw, "beszel"),
    )


def _shoutrrr(webhook: str) -> str:
    match = _DISCORD.match(webhook)
    if not match:
        raise InvalidSpec("discordWebhookFile does not hold a Discord webhook URL")
    webhook_id, token = match.groups()
    return f"discord://{token}@{webhook_id}"


def reconcile(spec: BeszelSpec, ctx: Context) -> None:
    email = ctx.secret(spec.email_file, "emailFile")
    password = ctx.secret(spec.password_file, "passwordFile")
    anon = ctx.client(spec.endpoint)
    try:
        auth = anon.request(
            "POST",
            "/api/collections/users/auth-with-password",
            {"identity": email, "password": password},
            idempotent=True,
        )
    except ApiError as e:
        if e.status == 400:
            raise AuthFailed(f"beszel: authentication failed (HTTP 400): {e.detail}") from None
        raise
    auth = check_dict(auth, "auth-with-password")
    record = auth.get("record")
    token = auth.get("token")
    user_id = record.get("id") if isinstance(record, dict) else None
    if not isinstance(token, str) or not token or not user_id:
        raise AuthFailed("beszel: authentication returned no token or user id")
    client = anon.with_headers(Authorization=token)
    # Read systems before any settings write, so a bad read aborts cleanly.
    systems_now = None
    if spec.systems is not None:
        systems_now = _list_systems(client)
        ctx.check_prune("beszel systems", len(spec.systems), len(systems_now), spec.prune)

    webhook = (
        ctx.optional_secret(spec.discord_webhook_file, "discordWebhookFile")
        if spec.discord_webhook_file
        else None
    )
    if webhook:
        url = _shoutrrr(webhook)
        ctx.sensitive(url)
        _sync_settings(ctx, client, user_id, webhook=url)
    if spec.user_settings:
        _sync_settings(ctx, client, user_id, patch=spec.user_settings)
    if spec.systems is not None:
        _sync_systems(ctx, client, user_id, spec.systems, systems_now)


def _user_settings(client: ApiClient, user_id: str):
    data = client.get_dict(
        "/api/collections/user_settings/records", params={"filter": f"user='{user_id}'"}
    )
    items = check_list(data.get("items"), "user_settings items", ("id",))
    if not items:
        return None, {}
    if len(items) > 1:
        raise BadResponse(f"user_settings: {len(items)} records for one user")
    record = items[0]
    settings = record.get("settings")
    if settings is None:
        raise BadResponse("user_settings.settings is null")
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except ValueError:
            raise BadResponse("user_settings.settings is not valid JSON") from None
    return record, check_dict(settings, "user_settings.settings")


def _sync_settings(ctx: Context, client: ApiClient, user_id: str, webhook=None, patch=None):
    record, settings = _user_settings(client, user_id)
    if webhook is not None:
        webhooks = list(list_field(settings, "webhooks", "user_settings.settings"))
        if webhook in webhooks:
            return
        ctx.change("+ discord webhook")
        updated = {
            **settings,
            "emails": list_field(settings, "emails", "user_settings.settings"),
            "webhooks": webhooks + [webhook],
        }
        what = "discord webhook"
    else:
        if record is not None and all(settings.get(k) == v for k, v in patch.items()):
            return
        changed = sorted(k for k, v in patch.items() if settings.get(k) != v)
        ctx.change(f"~ user settings ({', '.join(changed)})")
        updated = {**settings, **patch}
        what = "user settings"
    if record is not None:
        ctx.do(
            what,
            lambda: client.request(
                "PATCH",
                f"/api/collections/user_settings/records/{record['id']}",
                {"settings": updated},
            ),
        )
    else:
        if webhook is not None:
            updated = {"emails": [], "webhooks": [webhook]}
        ctx.do(
            what,
            lambda: client.request(
                "POST",
                "/api/collections/user_settings/records",
                {"user": user_id, "settings": updated},
            ),
        )


def _list_systems(client: ApiClient) -> Dict[str, Dict]:
    """All systems across pages; a missed page would become duplicate creates."""
    items: List[Dict] = []
    page = 1
    while True:
        data = client.get_dict(
            "/api/collections/systems/records", params={"page": page, "perPage": PER_PAGE}
        )
        batch = check_list(data.get("items"), "systems items", ("id", "name"))
        items += batch
        total_pages = data.get("totalPages")
        if not isinstance(total_pages, int):
            raise BadResponse("systems: response has no totalPages")
        if page >= total_pages or not batch:
            break
        page += 1
    total = data.get("totalItems")
    if isinstance(total, int) and total != len(items):
        raise BadResponse(f"systems: got {len(items)} of {total} items")
    return identity.index(items, "beszel systems", key=lambda x: x["name"], remote=True)


def _sync_systems(
    ctx: Context, client: ApiClient, user_id: str, systems: List[System], current: Dict
):
    desired = identity.index(systems, "beszel.systems", key=lambda x: x.name)

    creates_ok = True
    for name, want in desired.items():
        cur = current.get(name)
        if cur is None:
            ctx.change(f"+ system {name} ({want.host}:{want.port})")
            if (
                not ctx.do(
                    f"create system {name}",
                    lambda want=want: client.request(
                        "POST",
                        "/api/collections/systems/records",
                        {
                            "name": want.name,
                            "host": want.host,
                            "port": want.port,
                            "users": [user_id],
                        },
                    ),
                )
                and ctx.apply
            ):
                creates_ok = False
        elif cur.get("host") != want.host or str(cur.get("port")) != want.port:
            ctx.change(
                f"~ system {name} ({cur.get('host')}:{cur.get('port')} -> {want.host}:{want.port})"
            )
            ctx.do(
                f"update system {name}",
                lambda cur=cur, want=want: client.request(
                    "PATCH",
                    f"/api/collections/systems/records/{cur['id']}",
                    {"host": want.host, "port": want.port},
                ),
            )

    for name in sorted(set(current) - set(desired)):
        ctx.change(f"- system {name}")
        if not creates_ok:
            ctx.fail(f"kept system {name}, a create failed")
            continue
        ctx.do(
            f"delete system {name}",
            lambda sid=current[name]["id"]: client.request(
                "DELETE", f"/api/collections/systems/records/{sid}"
            ),
        )
