"""NextDNS profiles (ported from nix-config nextdns-mgmt `update`).

Deletion: profiles are never deleted. `denylist`, `allowlist` and `rewrites`
present in the profile file are authoritative, as in nextdns-mgmt: entries on
the profile that the file does not list are removed.
"""

import copy
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from tools.mgmt import identity
from tools.mgmt import spec as s
from tools.mgmt.engine import Context, Endpoint
from tools.mgmt.http import (
    BadResponse,
    InvalidSpec,
    ServiceError,
    check_dict,
    check_list,
    list_field,
)

API_BASE = "https://api.nextdns.io"
_BLOCKLIST_METADATA = ("name", "website", "description", "entries", "updatedOn")
# Domains are case-insensitive: `Example.com` and `example.com` are one entry.
REWRITE = identity.compound(identity.folded, identity.folded)


def _rewrite_key(r: Dict) -> tuple:
    return (r["name"], r["content"])


def _name_matches(remote_name, name: str) -> bool:
    """Unnamed or oddly named remote profiles simply don't match."""
    try:
        return identity.canon(remote_name, "profile name", remote=True) == name
    except BadResponse:
        return False


def _wanted(desired: Dict, kind: str) -> List[Dict]:
    """As in nextdns-mgmt, an `active: false` entry is not wanted (removed if present)."""
    if kind == "rewrites":
        return desired[kind]
    return [e for e in desired[kind] if e.get("active", True)]


def _entries(items: List[Dict], what: str, kind: str, remote: bool = False) -> Dict:
    """{canonical identity: entry} for denylist/allowlist ids or rewrites."""
    if kind == "rewrites":
        return identity.index(items, what, key=_rewrite_key, kind=REWRITE, remote=remote)
    return identity.index(items, what, key=lambda e: e["id"], kind=identity.folded, remote=remote)


@dataclass
class Profile:
    name: str
    profile_file: str
    api_key_file: str
    profile_id_file: Optional[str] = None
    profile_id_dir: Optional[str] = None
    vars: Dict[str, str] = field(default_factory=dict)
    vars_files: Dict[str, str] = field(default_factory=dict)


@dataclass
class NextDnsSpec:
    endpoint: Endpoint
    profiles: List[Profile]
    prune: bool = False


def parse(
    raw,
    base_dir: Optional[str] = None,
    secrets_dir: Optional[str] = None,
    secrets_dir_error: Optional[str] = None,
) -> NextDnsSpec:
    """``profileIdDir`` defaults to ``secrets_dir`` (the sops section's ``secretsDir``)."""
    raw = s.mapping(raw, "nextdns")
    s.check_keys(raw, s.ENDPOINT_KEYS | {"profiles"}, "nextdns")
    profiles = []
    entries = s.mapping(raw.get("profiles"), "nextdns.profiles")
    names = identity.unique(entries, "nextdns.profiles")
    for name, p in zip(names, entries.values()):
        if p is None:  # commented-out profile: unmanaged (profiles are never deleted)
            continue
        where = f"nextdns.profiles.{name}"
        p = s.mapping(p, where)
        s.check_keys(
            p,
            {"profileFile", "apiKeyFile", "profileIdFile", "profileIdDir", "vars", "varsFiles"},
            where,
        )
        profile_file = s.path(p, "profileFile", where, base_dir)
        api_key = s.path(p, "apiKeyFile", where, base_dir)
        if not profile_file or not api_key:
            raise InvalidSpec(f"{where}: profileFile and apiKeyFile are required")
        vars_ = s.mapping(p.get("vars"), f"{where}.vars")
        if not all(isinstance(v, str) for v in vars_.values()):
            raise InvalidSpec(f"{where}.vars values must be strings")
        id_file = s.path(p, "profileIdFile", where, base_dir)
        if "profileIdDir" in p:
            id_dir = s.path(p, "profileIdDir", where, base_dir)
        elif secrets_dir or id_file:
            id_dir = secrets_dir
        else:
            why = secrets_dir_error or "no sops section"
            raise InvalidSpec(f"{where}: profileIdDir defaults to sops.secretsDir ({why})")
        vars_files = {
            k: s.path({"p": v}, "p", f"{where}.varsFiles.{k}", base_dir)
            for k, v in s.mapping(p.get("varsFiles"), f"{where}.varsFiles").items()
        }
        profiles.append(
            Profile(
                name=name,
                profile_file=profile_file,
                api_key_file=api_key,
                profile_id_file=id_file,
                profile_id_dir=id_dir,
                vars=dict(vars_),
                vars_files=vars_files,
            )
        )
    return NextDnsSpec(
        endpoint=s.endpoint(raw, "nextdns", API_BASE, base_dir),
        profiles=profiles,
        prune=s.prune(raw, "nextdns"),
    )


def matches(desired, current) -> bool:
    """Is `desired` already satisfied by `current`?

    Dicts match on the desired keys only, so read-only fields the API adds
    never force a write. Lists of `{id: ...}` objects match as id sets.
    """
    if isinstance(desired, dict):
        return isinstance(current, dict) and all(
            matches(v, current.get(k)) for k, v in desired.items()
        )
    if isinstance(desired, list):
        if not isinstance(current, list):
            return False
        if all(isinstance(d, dict) and "id" in d for d in desired) and all(
            isinstance(c, dict) and "id" in c for c in current
        ):
            by_id = {c["id"]: c for c in current}
            return set(by_id) == {d["id"] for d in desired} and all(
                matches(d, by_id[d["id"]]) for d in desired
            )
        return desired == current
    return desired == current


@dataclass
class _Inputs:
    desired: Dict
    api_key: str
    profile_id: Optional[str]


def _resolve_inputs(profile: Profile, ctx: Context) -> _Inputs:
    """Every file a profile needs, read and validated; no request is made."""
    desired = _load_profile(profile, ctx)
    key = ctx.secret(profile.api_key_file, f"{profile.name} apiKeyFile")
    profile_id = None
    if profile.profile_id_file:
        profile_id = ctx.secret(profile.profile_id_file, f"{profile.name} profileIdFile")
    elif profile.profile_id_dir:
        # As nextdns-mgmt `update --name`: a per-name id file, else name lookup.
        path = f"{profile.profile_id_dir}/nextdns-profile-{profile.name}"
        profile_id = ctx.optional_secret(path, f"{profile.name} profile id")
    return _Inputs(desired, key, profile_id)


def reconcile(spec: NextDnsSpec, ctx: Context) -> None:
    # Inputs of every profile before the first write: a bad file in a later
    # profile must not follow writes to an earlier one.
    inputs = {}
    for profile in spec.profiles:
        try:
            inputs[profile.name] = _resolve_inputs(profile, ctx)
        except ServiceError as e:
            ctx.fail(f"{profile.name}: {e}")
    if not ctx.ok:
        return
    if spec.profiles:
        ctx.client(spec.endpoint)
    ctx.kept(
        "profiles",
        "profile",
        ((p.name, p.name) for p in spec.profiles),
        "profiles are never deleted",
    )
    for profile in spec.profiles:
        try:
            _sync_profile(spec, profile, inputs[profile.name], ctx)
        except ServiceError as e:
            ctx.fail(f"{profile.name}: {e}")
        except Exception as e:  # noqa: BLE001 - one profile's bug must not skip the rest
            ctx.fail(f"{profile.name}: unexpected {type(e).__name__}: {e}")


def _list_profiles(client) -> List[Dict]:
    """All profiles, following NextDNS cursor pagination."""
    items: List[Dict] = []
    cursor = None
    for _ in range(100):
        resp = client.get_dict("/profiles", params={"cursor": cursor} if cursor else None)
        items += check_list(resp.get("data"), "profiles", ("id",))
        meta = resp.get("meta")
        pagination = meta.get("pagination") if isinstance(meta, dict) else None
        cursor = pagination.get("cursor") if isinstance(pagination, dict) else None
        if not cursor:
            return items
    raise BadResponse("profiles: too many pages")


def _substitute(value, subs: Dict[str, str]):
    """Replace `@NAME@` inside string values (and keys) of parsed JSON.

    Substituting after parsing means a value can never change the JSON
    structure, whatever quotes or backslashes it holds.
    """
    if isinstance(value, str):
        for key, sub in subs.items():
            value = value.replace(f"@{key}@", sub)
        return value
    if isinstance(value, list):
        return [_substitute(v, subs) for v in value]
    if isinstance(value, dict):
        return {_substitute(k, subs): _substitute(v, subs) for k, v in value.items()}
    return value


def _load_profile(profile: Profile, ctx: Context) -> Dict:
    text = ctx.text(profile.profile_file, f"{profile.name} profileFile")
    try:
        data = json.loads(text)
    except ValueError as e:
        raise InvalidSpec(f"{profile.profile_file}: invalid JSON ({e.msg})") from None
    subs = dict(profile.vars)
    for key, path in profile.vars_files.items():
        subs[key] = ctx.secret(path, f"{profile.name} varsFiles.{key}").replace("\n", "")
        ctx.sensitive(subs[key])
    data = _substitute(data, subs)
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return validate_profile(data["data"], profile.profile_file)
    if (
        isinstance(data, dict)
        and isinstance(data.get("profile"), dict)
        and isinstance(data["profile"].get("data"), dict)
    ):
        return validate_profile(data["profile"]["data"], profile.profile_file)
    raise InvalidSpec(f'{profile.profile_file}: expected {{"data": {{...}}}}')


def validate_profile(data: Dict, where: str) -> Dict:
    """Shape-check the whole profile before any request, so a bad entry can
    never leave a profile half-written."""
    for section in ("security", "privacy", "parentalControl", "settings"):
        if section in data and not isinstance(data[section], dict):
            raise InvalidSpec(f"{where}: {section} must be an object")
    for kind in ("denylist", "allowlist"):
        if kind not in data:
            continue
        entries = data[kind]
        if not isinstance(entries, list):
            raise InvalidSpec(f"{where}: {kind} must be a list")
        for i, e in enumerate(entries):
            if not isinstance(e, dict) or not isinstance(e.get("id"), str) or not e["id"]:
                raise InvalidSpec(f"{where}: {kind}[{i}] needs a string id")
            if "active" in e and not isinstance(e["active"], bool):
                raise InvalidSpec(f"{where}: {kind}[{i}].active must be true or false")
    if "rewrites" in data:
        if not isinstance(data["rewrites"], list):
            raise InvalidSpec(f"{where}: rewrites must be a list")
        for i, r in enumerate(data["rewrites"]):
            if not isinstance(r, dict) or not all(
                isinstance(r.get(k), str) and r.get(k) for k in ("name", "content")
            ):
                raise InvalidSpec(f"{where}: rewrites[{i}] needs string name and content")
    for kind in ("denylist", "allowlist", "rewrites"):
        if kind in data:
            _entries(data[kind], f"{where}: {kind}", kind)
    privacy = data.get("privacy") or {}
    if "blocklists" in privacy and not (
        isinstance(privacy["blocklists"], list)
        and all(isinstance(b, dict) and "id" in b for b in privacy["blocklists"])
    ):
        raise InvalidSpec(f"{where}: privacy.blocklists must be a list of objects with id")
    return data


_SINGULAR = {"blocklists": "blocklist", "natives": "native", "tlds": "tld"}


def _redacted(ctx: Context, value):
    if isinstance(value, str):
        return ctx.redact(value)
    if isinstance(value, list):
        return [_redacted(ctx, v) for v in value]
    if isinstance(value, dict):
        return {_redacted(ctx, k): _redacted(ctx, v) for k, v in value.items()}
    return value


def _show(ctx: Context, value) -> str:
    """Secrets are masked in the raw value, before any escaping could hide them."""
    value = _redacted(ctx, value)
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _ids(items) -> Dict[str, object]:
    """{raw identity: value} for `{id: ...}` objects, or plain list items."""
    if not isinstance(items, list):
        return {}
    out = {}
    for i in items:
        value = i["id"] if isinstance(i, dict) and "id" in i else i
        out[json.dumps(value, sort_keys=True)] = value
    return out


def _list_rows(ctx: Context, what: str, want, have, path: str = "") -> None:
    """Id rows for every list nested in a PATCH body: each one replaces the remote list."""
    if not isinstance(want, dict):
        return
    have = have if isinstance(have, dict) else {}
    for key, value in want.items():
        name = f"{path}.{key}" if path else _SINGULAR.get(key, key)
        if isinstance(value, list):
            _id_rows(ctx, f"{what} {name}", value, have.get(key))
        else:
            _list_rows(ctx, what, value, have.get(key), name)


def _id_rows(ctx: Context, what: str, want, have) -> None:
    """The ids a replacing write adds and drops, so no removal hides in a `~` row."""
    want_ids, have_ids = _ids(want), _ids(have)
    for i in sorted(want_ids.keys() - have_ids.keys()):
        ctx.change(f"+ {what} {_show(ctx, want_ids[i])}")
    for i in sorted(have_ids.keys() - want_ids.keys()):
        ctx.change(f"- {what} {_show(ctx, have_ids[i])}")


def _sync_profile(spec: NextDnsSpec, profile: Profile, inputs: _Inputs, ctx: Context) -> None:
    desired = inputs.desired
    client = ctx.client(spec.endpoint, {"X-Api-Key": inputs.api_key})
    client.label = f"nextdns/{profile.name}"

    profile_id = inputs.profile_id
    if profile_id is None:
        found = [p for p in _list_profiles(client) if _name_matches(p.get("name"), profile.name)]
        if len(found) > 1:
            # Picking one would overwrite an arbitrary profile.
            raise BadResponse(
                f"{len(found)} remote profiles are named {profile.name!r}; set profileIdFile"
            )
        profile_id = found[0]["id"] if found else None

    if profile_id is None:
        ctx.change(f"+ profile {profile.name}")
        if ctx.apply:
            try:
                created = client.request("POST", "/profiles", {"name": profile.name})
                profile_id = created["data"]["id"]
            except (ServiceError, KeyError, TypeError) as e:
                ctx.fail(f"create profile {profile.name}: {e}")
                return
        # A dry run plans the new profile as empty, exactly as deploy writes it.
        current = {}
    else:
        resp = client.get_dict(f"/profiles/{profile_id}")
        current = check_dict(resp.get("data"), "profile data")
    remote_entries = {}
    for kind in ("denylist", "allowlist", "rewrites"):
        if kind in desired:
            keys = ("id", "name", "content") if kind == "rewrites" else ("id",)
            remote_entries[kind] = _entries(
                list_field(current, kind, "profile", keys), f"profile {kind}", kind, remote=True
            )
            ctx.check_prune(
                f"{profile.name} {kind} entries",
                len(_wanted(desired, kind)),
                len(remote_entries[kind]),
                spec.prune,
            )
    for section in ("security", "privacy", "parentalControl", "settings"):
        if current.get(section) is not None and not isinstance(current[section], dict):
            raise BadResponse(f"profile {section}: expected an object")

    base = f"/profiles/{profile_id}"
    label = profile.name

    for section in ("security", "privacy", "parentalControl"):
        if section not in desired:
            continue
        want = copy.deepcopy(desired[section])
        have = current.get(section) or {}  # shape checked on read; only `want` is sent
        arrays = {}
        if section == "privacy":
            for bl in want.get("blocklists", []) or []:
                for k in _BLOCKLIST_METADATA:
                    bl.pop(k, None)
        if section == "parentalControl":
            for k in ("categories", "services"):
                if k in want:
                    arrays[k] = want.pop(k)
        if not matches(want, have):
            ctx.change(f"~ {label} {section}")
            _list_rows(ctx, f"{label} {section}", want, have)
            ctx.do(f"{label} {section}", lambda: client.request("PATCH", f"{base}/{section}", want))
        for k, items in arrays.items():
            if not matches(items, have.get(k)):
                ctx.change(f"~ {label} {section}/{k}")
                _id_rows(ctx, f"{label} {section}/{k}", items, have.get(k))
                ctx.do(
                    f"{label} {section}/{k}",
                    lambda k=k, items=items: client.request("PUT", f"{base}/{section}/{k}", items),
                )

    for kind in ("denylist", "allowlist"):
        if kind not in desired:
            continue
        want = _entries(_wanted(desired, kind), kind, kind)
        have = remote_entries[kind]
        adds_ok = True
        for key in sorted(want.keys() - have.keys()):
            domain = want[key]["id"]
            ctx.change(f"+ {label} {kind} {domain}")
            if (
                not ctx.do(
                    f"{label} {kind} add {domain}",
                    lambda d=domain: client.request(
                        "POST", f"{base}/{kind}", {"id": d, "active": True}
                    ),
                )
                and ctx.apply
            ):
                adds_ok = False
        for key in sorted(have.keys() - want.keys()):
            domain = have[key]["id"]
            ctx.change(f"- {label} {kind} {domain}")
            if not adds_ok:
                ctx.fail(f"{label}: kept {kind} {domain}, an add failed")
                continue
            ctx.do(
                f"{label} {kind} remove {domain}",
                lambda d=domain: client.request("DELETE", f"{base}/{kind}/{d}"),
            )

    if "rewrites" in desired:
        want = _entries(desired["rewrites"], "rewrites", "rewrites")
        have = remote_entries["rewrites"]
        adds_ok = True
        for key in sorted(want.keys() - have.keys()):
            name, content = _rewrite_key(want[key])
            ctx.change(f"+ {label} rewrite {name} -> {content}")
            if (
                not ctx.do(
                    f"{label} rewrite {name}",
                    lambda n=name, c=content: client.request(
                        "POST", f"{base}/rewrites", {"name": n, "content": c}
                    ),
                )
                and ctx.apply
            ):
                adds_ok = False
        for key in sorted(have.keys() - want.keys()):
            name, content = _rewrite_key(have[key])
            rid = have[key].get("id")
            if not rid:
                ctx.change(f"- {label} rewrite {name} -> {content} (no id, cannot be removed)")
                continue
            ctx.change(f"- {label} rewrite {name} -> {content}")
            if not adds_ok:
                ctx.fail(f"{label}: kept rewrite {name}, an add failed")
                continue
            ctx.do(
                f"{label} remove rewrite {name}",
                lambda rid=rid: client.request("DELETE", f"{base}/rewrites/{rid}"),
            )

    if "settings" in desired and not matches(desired["settings"], current.get("settings")):
        ctx.change(f"~ {label} settings")
        _list_rows(ctx, f"{label} settings", desired["settings"], current.get("settings"))
        ctx.do(
            f"{label} settings",
            lambda: client.request("PATCH", f"{base}/settings", desired["settings"]),
        )
