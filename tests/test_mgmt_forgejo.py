import contextlib
import io
import os
import stat
import tempfile
import unittest
from unittest import mock

from mgmt_fakes import Harness

from tools.mgmt import forgejo

URL = "http://svc.example.com"
FILES = {
    "/s/admin-name": "root",
    "/s/admin-email": "root@example.com",
    "/s/admin-pass": "admin-secret",
    "/s/user-email": "me@example.com",
    "/s/user-pass": "user-secret",
}


def paged(items, size=1):
    """Forgejo-style pages: `size` items per page, then an empty page."""

    def handler(body, query):
        page = int(query.get("page", ["1"])[0])
        return items[(page - 1) * size : page * size]

    return handler


HEADER = "ID   Username   Email   IsActive   IsAdmin   2FA\n"


class Runner:
    def __init__(
        self, listing=HEADER + "1   root   root@example.com   true   true   false\n", rc=0
    ):
        self.listing = listing
        self.rc = rc
        self.cmds = []

    def __call__(self, cmd, env=None, cwd=None):
        self.cmds.append(cmd)
        if "list" in cmd:
            return self.rc, self.listing, "boom" if self.rc else ""
        return 0, "", ""


class ForgejoTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.token_file = os.path.join(self._tmp.name, "token")
        self.runner = Runner()
        self.h = Harness(files=FILES, runner=self.runner)
        real = self.h.reader

        def reader(path):
            if path.startswith(self._tmp.name):
                with open(path) as f:
                    return f.read()
            return real(path)

        self.h.reader = reader
        self.h.server.on("GET", "/api/v1/settings/api", {})
        self.h.server.on("GET", "/api/v1/users/root/tokens", [])
        self.h.server.on("GET", "/api/v1/user/gpg_keys", [])
        self.out = contextlib.redirect_stdout(io.StringIO())
        self.out.__enter__()

    def tearDown(self):
        self.out.__exit__(None, None, None)
        self._tmp.cleanup()

    def cfg(self, **extra):
        return {
            "forgejo": {
                "baseUrl": URL,
                "configFile": "/f/app.ini",
                "workPath": "/f",
                "tokenFile": self.token_file,
                "users": [
                    {
                        "usernameFile": "/s/admin-name",
                        "admin": True,
                        "emailFile": "/s/admin-email",
                        "passwordFile": "/s/admin-pass",
                    },
                    {
                        "username": "me",
                        "emailFile": "/s/user-email",
                        "passwordFile": "/s/user-pass",
                    },
                ],
                "repositories": [{"name": "notes", "owner": "me", "description": ""}],
                **extra,
            }
        }

    def test_failed_user_listing_is_a_failure_not_empty(self):
        self.runner.rc = 1
        rows, ok, _ = self.h.run(self.cfg())
        self.assertFalse(ok)
        self.assertFalse(any("create" in c for c in self.runner.cmds))

    def test_diff_uses_basic_auth_and_mints_nothing(self):
        self.h.server.on("GET", "/api/v1/users/me", None, status=404)
        rows, ok, _ = self.h.run(self.cfg(), apply=False)
        self.assertTrue(ok, rows)
        self.assertIn("  + admin API token", rows)
        self.assertIn("  + user me", rows)
        self.assertIn("  + repository me/notes", rows)
        self.assertEqual(self.h.server.writes(), [])
        self.assertFalse(os.path.exists(self.token_file))

    def test_deploy_creates_token_file_private_and_user_and_repo(self):
        self.h.server.on("POST", "/api/v1/users/root/tokens", {"sha1": "tok-secret"})
        self.h.server.on("GET", "/api/v1/users/me", None, status=404)
        self.h.server.on("POST", "/api/v1/admin/users", {})
        self.h.server.on("POST", "/api/v1/admin/users/me/repos", {})
        with mock.patch.object(forgejo, "gpg_key_id", return_value=""):
            rows, ok, _ = self.h.run(self.cfg())
        self.assertTrue(ok, rows)
        with open(self.token_file) as f:
            self.assertEqual(f.read(), "tok-secret")
        self.assertEqual(stat.S_IMODE(os.stat(self.token_file).st_mode), 0o600)
        (user,) = self.h.server.made("POST", "/api/v1/admin/users")
        self.assertEqual(user[4]["Authorization"], "token tok-secret")
        self.assertFalse(any("tok-secret" in r or "user-secret" in r for r in rows))

    def test_valid_stored_token_is_reused_and_existing_repo_updated(self):
        with open(self.token_file, "w") as f:
            f.write("stored")
        self.h.server.on("GET", "/api/v1/user", {"login": "root"})
        self.h.server.on("GET", "/api/v1/users/me", {"login": "me"})
        self.h.server.on("GET", "/api/v1/repos/me/notes", {"description": "old", "private": True})
        self.h.server.on("PATCH", "/api/v1/repos/me/notes", {})
        rows, ok, _ = self.h.run(self.cfg())
        self.assertTrue(ok, rows)
        self.assertEqual(self.h.server.writes(), [("PATCH", "/api/v1/repos/me/notes")])
        self.assertEqual(
            self.h.server.made("PATCH", "/api/v1/repos/me/notes")[0][3], {"description": ""}
        )

    def test_admin_users_are_skipped_by_user_sync(self):
        config = self.cfg(repositories=[], tokenFile=None)
        config["forgejo"]["users"][1]["admin"] = True
        rows, ok, _ = self.h.run(config, apply=False)
        self.assertTrue(ok, rows)
        self.assertEqual(self.h.server.made("GET", "/api/v1/users/me"), [])
        self.assertEqual(self.h.server.made("GET", "/api/v1/users/root"), [])

    def test_lost_admin_token_replaces_only_tools_owned_token(self):
        tokens = [
            {"id": 3, "name": "tools-admin-0ld"},
            {"id": 4, "name": "laptop"},
            {"id": 5, "name": "forgejo-mgmt"},  # legacy: never touched
        ]
        self.h.server.on("GET", "/api/v1/users/root/tokens", paged(tokens))
        self.h.server.on("DELETE", "/api/v1/users/root/tokens/3", None)
        self.h.server.on("POST", "/api/v1/users/root/tokens", {"sha1": "fresh"})
        self.h.server.on("GET", "/api/v1/users/me", {"login": "me"})
        self.h.server.on("GET", "/api/v1/repos/me/notes", {"description": "", "private": True})
        rows, ok, _ = self.h.run(self.cfg())
        self.assertTrue(ok, rows)
        writes = self.h.server.writes()
        self.assertEqual(writes[0], ("POST", "/api/v1/users/root/tokens"))  # create first
        self.assertIn(("DELETE", "/api/v1/users/root/tokens/3"), writes)
        self.assertNotIn(("DELETE", "/api/v1/users/root/tokens/4"), writes)
        self.assertNotIn(("DELETE", "/api/v1/users/root/tokens/5"), writes)
        name = self.h.server.made("POST", "/api/v1/users/root/tokens")[0][3]["name"]
        self.assertTrue(name.startswith("tools-admin-"))

    def test_failed_token_create_keeps_old_token(self):
        self.h.server.on(
            "GET", "/api/v1/users/root/tokens", paged([{"id": 3, "name": "tools-admin-0ld"}])
        )
        self.h.server.on("POST", "/api/v1/users/root/tokens", {"message": "x"}, status=500)
        rows, ok, _ = self.h.run(self.cfg())
        self.assertFalse(ok)
        self.assertNotIn(("DELETE", "/api/v1/users/root/tokens/3"), self.h.server.writes())

    def test_unreadable_token_file_is_error_not_remint(self):
        def reader(path):
            if path == self.token_file:
                raise PermissionError(13, "Permission denied", path)
            return FILES[path]

        self.h.reader = reader
        rows, ok, _ = self.h.run(self.cfg())
        self.assertFalse(ok)
        self.assertEqual(self.h.server.made("POST", "/api/v1/users/root/tokens"), [])
        self.assertTrue(any("Permission denied" in r for r in rows))

    def test_lost_user_token_file_replaces_tools_token(self):
        with open(self.token_file, "w") as f:
            f.write("stored")
        user_token = os.path.join(self._tmp.name, "me-token")
        config = self.cfg(repositories=[])
        config["forgejo"]["users"][1]["tokenFile"] = user_token
        self.h.server.on("GET", "/api/v1/user", {"login": "root"})
        self.h.server.on("GET", "/api/v1/users/me", {"login": "me"})
        self.h.server.on(
            "GET",
            "/api/v1/users/me/tokens",
            paged([{"id": 9, "name": "tools-user-0ld"}, {"id": 8, "name": "forgejo-mgmt"}]),
        )
        self.h.server.on("DELETE", "/api/v1/users/me/tokens/9", None)
        self.h.server.on("POST", "/api/v1/users/me/tokens", {"sha1": "user-tok"})
        rows, ok, _ = self.h.run(config)
        self.assertTrue(ok, rows)
        self.assertIn(("DELETE", "/api/v1/users/me/tokens/9"), self.h.server.writes())
        self.assertNotIn(("DELETE", "/api/v1/users/me/tokens/8"), self.h.server.writes())
        with open(user_token) as f:
            self.assertEqual(f.read(), "user-tok")

    def test_absent_repo_description_defaults_to_empty(self):
        with open(self.token_file, "w") as f:
            f.write("stored")
        config = self.cfg(repositories=[{"name": "notes", "owner": "me"}])
        self.h.server.on("GET", "/api/v1/user", {"login": "root"})
        self.h.server.on("GET", "/api/v1/users/me", {"login": "me"})
        self.h.server.on("GET", "/api/v1/repos/me/notes", {"description": "hand", "private": True})
        self.h.server.on("PATCH", "/api/v1/repos/me/notes", {})
        rows, ok, _ = self.h.run(config)
        self.assertTrue(ok, rows)
        (patch,) = self.h.server.made("PATCH", "/api/v1/repos/me/notes")
        self.assertEqual(patch[3], {"description": ""})

    def test_admin_not_created_when_users_exist(self):
        self.runner.listing = HEADER + "1   someone   s@example.com   true   true   false\n"
        rows, ok, _ = self.h.run(self.cfg(), apply=False)
        self.assertFalse(any("admin user" in r for r in rows), rows)

    def test_cli_error_is_redacted_before_truncation(self):
        self.runner.rc = 1
        secret = FILES["/s/admin-pass"]
        runner = self.runner

        def failing(cmd, env=None, cwd=None):
            runner.cmds.append(cmd)
            return 1, "", "x" * 395 + secret

        self.h.runner = failing
        rows, ok, _ = self.h.run(self.cfg(), apply=False)
        self.assertFalse(ok)
        for r in rows:
            for n in range(6, len(secret) + 1):  # "admin-" onwards
                self.assertNotIn(secret[:n], r)

    def test_duplicate_users_after_resolution_rejected(self):
        config = self.cfg()
        config["forgejo"]["users"][1] = {
            **config["forgejo"]["users"][1],
            "username": None,
            "usernameFile": "/s/admin-name",
        }
        config["forgejo"]["users"][1].pop("username")
        rows, ok, _ = self.h.run(config, apply=False)
        self.assertFalse(ok)
        self.assertEqual(self.h.server.calls, [])

    def test_no_admin_is_a_spec_error(self):
        config = self.cfg()
        config["forgejo"]["users"] = config["forgejo"]["users"][1:]
        rows, ok, _ = self.h.run(config, apply=False)
        self.assertFalse(ok)
        self.assertEqual(self.h.server.calls, [])

    def test_first_admin_created_via_cli(self):
        self.runner.listing = HEADER
        rows, ok, _ = self.h.run(self.cfg(), apply=False)
        self.assertIn("  + admin user *** (via forgejo CLI)", rows)
        self.assertFalse(any("create" in c for c in self.runner.cmds))

    def test_users_differing_only_in_case_rejected(self):
        config = self.cfg()
        config["forgejo"]["users"][1]["username"] = "ROOT"
        rows, ok, _ = self.h.run(config, apply=False)
        self.assertFalse(ok)
        self.assertEqual(self.h.server.calls, [])

    def test_duplicate_admin_listing_rows_abort(self):
        self.runner.listing = HEADER + "1 root a true true false\n2 ROOT b true false false\n"
        rows, ok, _ = self.h.run(self.cfg(), apply=False)
        self.assertFalse(ok)

    def test_bad_gpg_key_shape_fails_that_user_only(self):
        self.h.files["/s/gpg"] = "-----BEGIN PGP PUBLIC KEY BLOCK-----"
        self.h.server.on("GET", "/api/v1/user/gpg_keys", paged([{"primary_key_id": 123}]))
        self.h.server.on("GET", "/api/v1/users/me", {"login": "me"})
        self.h.server.on("GET", "/api/v1/repos/me/notes", {"description": "", "private": True})
        config = self.cfg(tokenFile=None)
        config["forgejo"]["users"][1]["gpgKeyFile"] = "/s/gpg"
        with mock.patch.object(forgejo, "gpg_key_id", return_value="A" * 16):
            rows, ok, _ = self.h.run(config, apply=False)
        self.assertFalse(ok)
        self.assertTrue(any("gpg_keys[0].primary_key_id" in r for r in rows), rows)
        self.assertFalse(any("AttributeError" in r for r in rows), rows)
        self.assertEqual(len(self.h.server.made("GET", "/api/v1/repos/me/notes")), 1)

    def test_admin_token_minted_without_token_file(self):
        self.h.server.on("POST", "/api/v1/users/root/tokens", {"sha1": "fresh-tok"})
        self.h.server.on("GET", "/api/v1/users/me", {"login": "me"})
        self.h.server.on("GET", "/api/v1/repos/me/notes", {"description": "", "private": True})
        rows, ok, _ = self.h.run(self.cfg(tokenFile=None))
        self.assertTrue(ok, rows)
        self.assertEqual(len(self.h.server.made("POST", "/api/v1/users/root/tokens")), 1)
        (repo,) = self.h.server.made("GET", "/api/v1/repos/me/notes")
        self.assertEqual(repo[4]["Authorization"], "token fresh-tok")

    def test_diff_without_token_file_reports_no_token_change(self):
        self.h.server.on("GET", "/api/v1/users/me", {"login": "me"})
        self.h.server.on("GET", "/api/v1/repos/me/notes", {"description": "", "private": True})
        rows, ok, _ = self.h.run(self.cfg(tokenFile=None), apply=False)
        self.assertTrue(ok, rows)
        self.assertEqual(rows, [])

    def test_token_file_on_admin_user_rejected(self):
        config = self.cfg()
        config["forgejo"]["users"][0]["tokenFile"] = "/s/admin-token"
        rows, ok, _ = self.h.run(config, apply=False)
        self.assertFalse(ok)
        self.assertTrue(any("tokenFile is not used on an admin" in r for r in rows), rows)
        self.assertEqual(self.h.server.calls, [])

    def test_gpg_key_file_on_admin_user_rejected(self):
        config = self.cfg()
        config["forgejo"]["users"][0]["gpgKeyFile"] = "/s/gpg"
        rows, ok, _ = self.h.run(config, apply=False)
        self.assertFalse(ok)
        self.assertTrue(any("gpgKeyFile is not used on an admin" in r for r in rows), rows)
        self.assertEqual(self.h.server.calls, [])

    def test_unparseable_gpg_key_is_a_config_error_before_writes(self):
        self.runner.listing = HEADER
        self.h.files["/s/gpg"] = "not a key"
        config = self.cfg(tokenFile=None)
        config["forgejo"]["users"][1]["gpgKeyFile"] = "/s/gpg"
        bad = mock.Mock(returncode=2, stdout="", stderr="no valid OpenPGP data")
        with mock.patch.object(forgejo.subprocess, "run", return_value=bad):
            rows, ok, _ = self.h.run(config)
        self.assertFalse(ok)
        self.assertTrue(any("not a readable armored GPG public key" in r for r in rows), rows)
        self.assertFalse(any("create" in c for c in self.runner.cmds))
        self.assertEqual(self.h.server.writes(), [])

    def test_missing_gpg_binary_keeps_original_fallback(self):
        with mock.patch.object(forgejo.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(forgejo.gpg_key_id("armored"), "")

    def test_users_and_repos_dropped_from_config_are_shown_as_kept(self):
        self.h.server.on("GET", "/api/v1/users/me", {"login": "me"})
        self.h.server.on("GET", "/api/v1/repos/me/notes", {"description": "", "private": True})
        state = {
            "forgejo": {
                "managed": {
                    "users": self.h.managed(
                        "users", "gone", "me", ("root", "usernameFile /s/admin-name")
                    ),
                    "repositories": self.h.managed("repositories", "me/notes", "me/old"),
                }
            }
        }
        rows, ok, _ = self.h.run(self.cfg(tokenFile=None), state=state, apply=False)
        self.assertTrue(ok, rows)
        self.assertIn("  = user gone removed from config, kept (users are never deleted)", rows)
        self.assertIn(
            "  = repository me/old removed from config, kept (repositories are never deleted)",
            rows,
        )
        self.assertEqual(len(rows), 2, rows)
        self.h.server.on("POST", "/api/v1/users/root/tokens", {"sha1": "fresh-tok"})
        _, _, state = self.h.run(self.cfg(tokenFile=None), state=state)
        self.assertEqual(list(state["forgejo"]["managed"]["repositories"].values()), ["me/notes"])
        self.assertFalse(any(m == "DELETE" for m, _ in self.h.server.writes()))
        rows, _, _ = self.h.run(self.cfg(tokenFile=None), state=state, apply=False)
        self.assertEqual(rows, [])

    def test_fresh_instance_diff_lists_everything_deploy_creates(self):
        self.runner.listing = HEADER
        config = self.cfg()
        config["forgejo"]["users"][1]["tokenFile"] = os.path.join(self._tmp.name, "user-token")
        rows, ok, _ = self.h.run(config, apply=False)
        self.assertIn("  + admin user *** (via forgejo CLI)", rows)
        self.assertIn("  + admin API token", rows)
        self.assertIn("  + user me", rows)
        self.assertTrue(any(r.startswith("  + access token for me") for r in rows), rows)
        self.assertIn("  + repository me/notes", rows)
        self.assertEqual(self.h.server.writes(), [])

    def test_username_moved_to_file_is_not_shown_as_removed(self):
        self.h.files["/s/me-name"] = "me"
        self.h.server.on("GET", "/api/v1/users/me", {"login": "me"})
        self.h.server.on("GET", "/api/v1/repos/me/notes", {"description": "", "private": True})
        self.h.server.on("POST", "/api/v1/users/root/tokens", {"sha1": "fresh-tok"})
        _, ok, state = self.h.run(self.cfg(tokenFile=None))
        self.assertTrue(ok)
        moved = self.cfg(tokenFile=None)
        moved["forgejo"]["users"][1].pop("username")
        moved["forgejo"]["users"][1]["usernameFile"] = "/s/me-name"
        moved["forgejo"]["repositories"][0].pop("owner")
        moved["forgejo"]["repositories"][0]["ownerFile"] = "/s/me-name"
        rows, ok, _ = self.h.run(moved, state=state, apply=False)
        self.assertTrue(ok, rows)
        self.assertEqual(rows, [])

    def test_removed_file_sourced_user_never_prints_its_name(self):
        self.h.files["/s/me-name"] = "hidden-user"
        self.h.server.on("GET", "/api/v1/users/hidden-user", {"login": "hidden-user"})
        self.h.server.on("GET", "/api/v1/users/me", {"login": "me"})
        self.h.server.on("GET", "/api/v1/repos/me/notes", {"description": "", "private": True})
        self.h.server.on("POST", "/api/v1/users/root/tokens", {"sha1": "fresh-tok"})
        config = self.cfg(tokenFile=None)
        config["forgejo"]["users"].append(
            {
                "usernameFile": "/s/me-name",
                "emailFile": "/s/user-email",
                "passwordFile": "/s/user-pass",
            }
        )
        _, ok, state = self.h.run(config, state={})
        rows, ok, _ = self.h.run(self.cfg(tokenFile=None), state=state, apply=False)
        self.assertIn(
            "  = user usernameFile /s/me-name removed from config, kept (users are never deleted)",
            rows,
        )
        self.assertFalse(any("hidden-user" in r for r in rows), rows)

    def test_failed_cleanup_of_unstored_token_is_reported(self):
        self.h.server.on("POST", "/api/v1/users/root/tokens", {"sha1": "tok-secret"})
        bad = os.path.join(self._tmp.name, "missing-dir", "token")
        rows, ok, _ = self.h.run(self.cfg(tokenFile=bad))
        self.assertFalse(ok)
        self.assertTrue(any("cannot write tokenFile" in r for r in rows), rows)
        self.assertTrue(any("could not be removed" in r for r in rows), rows)


if __name__ == "__main__":
    unittest.main()
