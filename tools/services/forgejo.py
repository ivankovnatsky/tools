"""Forgejo users, repositories, GPG keys (ported from nix-config forgejo-mgmt).

Deletion: none. Users, repositories, tokens and GPG keys are only created
(repositories also get description/visibility updates).
"""

import os
import secrets as _secrets
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from tools.services import identity
from tools.services import spec as s
from tools.services.engine import Context, Endpoint
from tools.services.http import (
    ABSENT,
    ApiClient,
    ApiError,
    AuthFailed,
    BadResponse,
    InvalidSpec,
    ServiceError,
)

# Token names are "<prefix>-<random>". Only these prefixes are ever deleted;
# legacy forgejo-mgmt tokens are left alone.
ADMIN_TOKEN_PREFIX = "tools-admin"
USER_TOKEN_PREFIX = "tools-user"
PAGE_LIMIT = 50
MAX_PAGES = 100


@dataclass
class User:
    username: Dict[str, str]
    email_file: str
    password_file: str
    admin: bool = False
    token_file: Optional[str] = None
    gpg_key_file: Optional[str] = None


@dataclass
class Repository:
    name: str
    owner: Dict[str, str]
    description: str = ""
    private: bool = True
    auto_init: bool = False


@dataclass
class ForgejoSpec:
    endpoint: Endpoint
    forgejo_bin: str
    config_file: str
    work_path: str
    token_file: Optional[str]
    users: List[User] = field(default_factory=list)
    repositories: List[Repository] = field(default_factory=list)


def parse(raw, base_dir: Optional[str] = None) -> ForgejoSpec:
    raw = s.mapping(raw, "forgejo")
    s.check_keys(
        raw,
        s.ENDPOINT_KEYS
        | {"forgejoBin", "configFile", "workPath", "tokenFile", "users", "repositories"},
        "forgejo",
    )
    users = []
    for i, u in enumerate(s.items(raw.get("users"), "forgejo.users")):
        where = f"forgejo.users[{i}]"
        s.check_keys(
            u,
            {
                "username", "usernameFile", "emailFile", "passwordFile",
                "admin", "tokenFile", "gpgKeyFile",
            },
            where,
        )  # fmt: skip
        if u.get("admin") and u.get("tokenFile") is not None:
            raise InvalidSpec(f"{where}: tokenFile is not used on an admin; set forgejo.tokenFile")
        email = s.path(u, "emailFile", where, base_dir)
        password = s.path(u, "passwordFile", where, base_dir)
        if not email or not password:
            raise InvalidSpec(f"{where}: emailFile and passwordFile are required")
        users.append(
            User(
                username=s.one_of(u, "username", where, base_dir),
                email_file=email,
                password_file=password,
                admin=bool(s.boolean(u, "admin", where, False)),
                token_file=s.path(u, "tokenFile", where, base_dir),
                gpg_key_file=s.path(u, "gpgKeyFile", where, base_dir),
            )
        )
    if not any(u.admin for u in users):
        raise InvalidSpec("forgejo: at least one user must have admin: true")
    repos = []
    for i, r in enumerate(s.items(raw.get("repositories"), "forgejo.repositories")):
        where = f"forgejo.repositories[{i}]"
        s.check_keys(r, {"name", "owner", "ownerFile", "description", "private", "autoInit"}, where)
        repos.append(
            Repository(
                name=s.required(r, "name", where),
                owner=s.one_of(r, "owner", where, base_dir),
                description=s.string(r, "description", where, ""),
                private=s.boolean(r, "private", where, True),
                auto_init=bool(s.boolean(r, "autoInit", where, False)),
            )
        )
    config_file = s.path(raw, "configFile", "forgejo", base_dir)
    work_path = s.path(raw, "workPath", "forgejo", base_dir)
    if not config_file or not work_path:
        raise InvalidSpec("forgejo: configFile and workPath are required")
    return ForgejoSpec(
        endpoint=s.endpoint(raw, "forgejo", "http://localhost:3300", base_dir),
        forgejo_bin=s.path(raw, "forgejoBin", "forgejo", None) or "/opt/homebrew/bin/forgejo",
        config_file=config_file,
        work_path=work_path,
        token_file=s.path(raw, "tokenFile", "forgejo", base_dir),
        users=users,
        repositories=repos,
    )


def _write_private(path: str, value: str) -> None:
    """Atomic 0600 write: a crash never leaves a half-written token file."""
    tmp = f"{path}.{_secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def reconcile(spec: ForgejoSpec, ctx: Context) -> None:
    # Identities may come from files, so duplicates are only visible once
    # resolved; check before any request.
    # Forgejo names are case-insensitive, so `Ivan` and `ivan` are one user.
    identity.unique(
        (ctx.value(u.username, "username") for u in spec.users), "forgejo.users", identity.folded
    )
    identity.unique(
        ((ctx.value(r.owner, "owner"), r.name) for r in spec.repositories),
        "forgejo.repositories",
        identity.compound(identity.folded, identity.folded),
    )
    anon = ctx.client(spec.endpoint)
    anon.request("GET", "/api/v1/settings/api")  # readiness, retried

    admin = next(u for u in spec.users if u.admin)
    admin_name = ctx.value(admin.username, "admin username")
    admin_password = ctx.secret(admin.password_file, "admin passwordFile")
    _resolve_inputs(spec, ctx, admin)

    if not _ensure_admin_user(spec, ctx, admin, admin_name, admin_password):
        return
    api = _admin_client(spec, ctx, anon, admin_name, admin_password)
    if api is None:
        return

    # Admins only bootstrap the API, as in forgejo-mgmt; the user sync skips them.
    for user in (u for u in spec.users if not u.admin):
        try:
            _sync_user(ctx, api, anon, user)
        except ServiceError as e:
            ctx.fail(f"user: {e}")
        except Exception as e:  # noqa: BLE001 - one user's bug must not skip the rest
            ctx.fail(f"user: unexpected {type(e).__name__}: {e}")
    for repo in spec.repositories:
        try:
            _sync_repo(ctx, api, repo)
        except ServiceError as e:
            ctx.fail(f"repository {repo.name}: {e}")


def _resolve_inputs(spec: ForgejoSpec, ctx: Context, admin: User) -> None:
    """Read every input file before the first write; later reads hit the cache."""
    ctx.secret(admin.email_file, "admin emailFile")
    if spec.token_file:
        ctx.optional_secret(spec.token_file, "tokenFile")
    for user in spec.users:
        if user.admin:
            continue
        ctx.secret(user.email_file, "emailFile")
        ctx.secret(user.password_file, "passwordFile")
        if user.token_file:
            ctx.optional_secret(user.token_file, "tokenFile")
        if user.gpg_key_file:
            gpg_key_id(ctx.text(user.gpg_key_file, "gpgKeyFile").strip(), user.gpg_key_file)


def parse_user_list(out: str) -> Dict[str, bool]:
    """`forgejo admin user list` table -> {lowercased username: is_admin}."""
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    if not lines:
        return {}
    header = lines[0].split()
    try:
        name_col, admin_col = header.index("Username"), header.index("IsAdmin")
    except ValueError:
        raise BadResponse("forgejo admin user list: unexpected header") from None
    rows = []
    for ln in lines[1:]:
        cols = ln.split()
        if len(cols) <= max(name_col, admin_col):
            raise BadResponse("forgejo admin user list: unexpected row")
        rows.append(cols)
    users = identity.index(
        rows,
        "forgejo admin user list",
        key=lambda c: c[name_col],
        kind=identity.folded,
        remote=True,
    )
    return {n: c[admin_col].lower() == "true" for n, c in users.items()}


def _ensure_admin_user(spec, ctx, admin: User, name: str, password: str) -> bool:
    """First admin via the forgejo CLI (works before any API auth), only while
    the instance has no users, as in forgejo-mgmt."""
    base = ["--config", spec.config_file, "--work-path", spec.work_path]
    try:
        rc, out, err = ctx.runner([spec.forgejo_bin, "admin", "user", "list", *base])
    except OSError as e:
        ctx.fail(f"cannot run {spec.forgejo_bin}: {e.strerror or e}")
        return False
    if rc != 0:
        # An empty listing from a failed command must not read as "no users".
        ctx.fail(f"forgejo admin user list failed (exit {rc}): {ctx.redact(err.strip())}")
        return False
    if parse_user_list(out):
        return True
    ctx.change(f"+ admin user {name} (via forgejo CLI)")
    if not ctx.apply:
        return False
    email = ctx.secret(admin.email_file, "admin emailFile")
    # The forgejo CLI only takes the password as argv; it is briefly visible
    # in the process list while this runs.
    rc, _, err = ctx.runner(
        [
            spec.forgejo_bin, "admin", "user", "create", *base,
            "--username", name, "--email", email, "--password", password,
            "--admin", "--must-change-password=false",
        ]
    )  # fmt: skip
    if rc != 0:
        ctx.fail(f"create admin user failed (exit {rc}): {ctx.redact(err.strip())}")
        return False
    ctx.applied += 1
    return True


def _admin_client(spec, ctx, anon: ApiClient, name: str, password: str) -> Optional[ApiClient]:
    token = ctx.optional_secret(spec.token_file, "tokenFile") if spec.token_file else None
    if token:
        candidate = anon.with_headers(Authorization=f"token {token}")
        try:
            candidate.request("GET", "/api/v1/user")
            return candidate
        except AuthFailed:
            pass
    basic = anon.with_basic_auth(name, password)
    # Without tokenFile a fresh token is minted every deploy, as forgejo-mgmt
    # did; that is routine, not drift, so it is not reported as a change.
    if spec.token_file:
        ctx.change("+ admin API token" + (" (stored token rejected)" if token else ""))
    if not ctx.apply:
        # Reads work with basic auth; diff never mints a token.
        return basic
    try:
        new = _rotate_token(ctx, basic, name, ADMIN_TOKEN_PREFIX, spec.token_file)
    except ServiceError as e:
        ctx.fail(f"admin API token: {e}")
        return None
    return anon.with_headers(Authorization=f"token {new}")


def list_all(client: ApiClient, path: str, keys=()) -> List[Dict]:
    """Every item of a paginated Forgejo list (stops at the first empty page).

    Page size is capped server-side (MAX_RESPONSE_ITEMS), so a short page does
    not mean the last one.
    """
    items: List[Dict] = []
    for page in range(1, MAX_PAGES + 1):
        batch = client.get_list(path, keys, params={"page": page, "limit": PAGE_LIMIT})
        if not batch:
            return items
        items += batch
    raise BadResponse(f"GET {path}: more than {MAX_PAGES} pages")


def _rotate_token(
    ctx: Context, as_user: ApiClient, username: str, prefix: str, token_file: Optional[str]
) -> str:
    """Create a new token, store it, and only then delete older ones.

    Creating first means a failed request never leaves the user without a
    working token; a unique suffix avoids Forgejo's duplicate-name refusal.
    """
    new_name = f"{prefix}-{_secrets.token_hex(4)}"
    resp = as_user.request(
        "POST", f"/api/v1/users/{username}/tokens", {"name": new_name, "scopes": ["all"]}
    )
    token = resp.get("sha1") if isinstance(resp, dict) else None
    if not isinstance(token, str) or not token:
        raise ServiceError("no token in response")
    ctx.sensitive(token)
    try:
        if token_file:
            _write_private(token_file, token)
    except OSError as e:
        try:
            as_user.request("DELETE", f"/api/v1/users/{username}/tokens/{new_name}")
        except ServiceError:
            pass
        raise ServiceError(f"cannot write tokenFile {token_file}: {e.strerror or e}") from None
    ctx.applied += 1
    try:
        tokens = list_all(as_user, f"/api/v1/users/{username}/tokens", ("id", "name"))
        for old in tokens:
            old_name = str(old.get("name", ""))
            if old_name.startswith(prefix + "-") and old_name != new_name:
                as_user.request("DELETE", f"/api/v1/users/{username}/tokens/{old['id']}")
    except ServiceError as e:
        ctx.fail(f"new token stored, but removing older {prefix} tokens failed: {e}")
    return token


def _sync_user(ctx: Context, api: ApiClient, anon: ApiClient, user: User) -> None:
    name = ctx.value(user.username, "username")
    password = ctx.secret(user.password_file, "passwordFile")
    exists = api.get_dict(f"/api/v1/users/{name}", allow=(404,)) is not ABSENT
    if not exists:
        email = ctx.secret(user.email_file, "emailFile")
        ctx.change(f"+ user {name}")
        payload = {
            "username": name,
            "email": email,
            "password": password,
            "must_change_password": False,
            "visibility": "private",
        }
        if not ctx.do(
            f"create user {name}", lambda: api.request("POST", "/api/v1/admin/users", payload)
        ):
            if ctx.apply:
                return
    as_user = anon.with_basic_auth(name, password)

    # An unreadable (not absent) token file is an error, never a re-mint.
    if user.token_file and not ctx.optional_secret(user.token_file, "tokenFile"):
        ctx.change(f"+ access token for {name} -> {user.token_file}")
        if ctx.apply:
            try:
                _rotate_token(ctx, as_user, name, USER_TOKEN_PREFIX, user.token_file)
            except ServiceError as e:
                ctx.fail(f"access token for {name}: {e}")

    if user.gpg_key_file:
        armored = ctx.text(user.gpg_key_file, "gpgKeyFile").strip()
        _sync_gpg_key(ctx, as_user, name, armored, exists)


def gpg_key_id(armored: str, where: str = "gpgKeyFile") -> str:
    """Long key ID of an armored public key; "" only when gpg is not installed.

    A key gpg cannot parse is a config error: Forgejo answers 422 for both a
    malformed and a duplicate key, so it would otherwise fail silently.
    """
    try:
        result = subprocess.run(
            ["gpg", "--with-colons", "--import-options", "show-only", "--import"],
            input=armored,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return ""
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            fields = line.split(":")
            if fields[0] == "pub" and len(fields) > 4 and fields[4]:
                return fields[4]
    raise InvalidSpec(f"{where}: not a readable armored GPG public key")


def _sync_gpg_key(ctx: Context, as_user: ApiClient, name: str, armored: str, exists: bool):
    if exists:
        keys = list_all(as_user, "/api/v1/user/gpg_keys")
        for i, key in enumerate(keys):
            for field in ("primary_key_id", "key_id"):
                if key.get(field) is not None and not isinstance(key[field], str):
                    raise BadResponse(f"gpg_keys[{i}].{field}: expected a string")
        wanted = gpg_key_id(armored)
        for key in keys:
            have = key.get("primary_key_id") or key.get("key_id") or ""
            if wanted and len(wanted) >= 16 and have and have.endswith(wanted[-16:]):
                return
        if not wanted and keys:
            return
    ctx.change(f"+ GPG key for {name}")
    if not ctx.apply:
        return
    try:
        as_user.request("POST", "/api/v1/user/gpg_keys", {"armored_public_key": armored})
        ctx.applied += 1
    except ApiError as e:
        if e.status != 422:  # 422: Forgejo already has this key
            ctx.fail(f"upload GPG key for {name}: {e}")


def _sync_repo(ctx: Context, api: ApiClient, repo: Repository) -> None:
    owner = ctx.value(repo.owner, "owner")
    existing = api.get_dict(f"/api/v1/repos/{owner}/{repo.name}", allow=(404,))
    if existing is ABSENT:
        ctx.change(f"+ repository {owner}/{repo.name}")
        ctx.do(
            f"create repository {repo.name}",
            lambda: api.request(
                "POST",
                f"/api/v1/admin/users/{owner}/repos",
                {
                    "name": repo.name,
                    "description": repo.description,
                    "private": repo.private,
                    "auto_init": repo.auto_init,
                },
            ),
        )
        return
    updates = {}
    if existing.get("description", "") != repo.description:
        updates["description"] = repo.description
    if existing.get("private", True) != repo.private:
        updates["private"] = repo.private
    if updates:
        ctx.change(f"~ repository {owner}/{repo.name} ({', '.join(sorted(updates))})")
        ctx.do(
            f"update repository {repo.name}",
            lambda: api.request("PATCH", f"/api/v1/repos/{owner}/{repo.name}", updates),
        )
