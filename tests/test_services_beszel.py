import contextlib
import io
import unittest

from services_fakes import Harness

URL = "http://svc.example.com"
WEBHOOK = "https://discord.com/api/webhooks/123/tok-SECRET"
FILES = {"/s/email": "me@example.com", "/s/pass": "pw-secret", "/s/hook": WEBHOOK}
AUTH = {"token": "jwt-secret", "record": {"id": "u1"}}


def cfg(**extra):
    return {"beszel": {"baseUrl": URL, "emailFile": "/s/email", "passwordFile": "/s/pass", **extra}}


class BeszelTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(files=FILES)
        self.h.server.on("POST", "/api/collections/users/auth-with-password", AUTH)
        self.out = contextlib.redirect_stdout(io.StringIO())
        self.out.__enter__()

    def tearDown(self):
        self.out.__exit__(None, None, None)

    def systems(self, items):
        self.h.server.on(
            "GET",
            "/api/collections/systems/records",
            {"items": items, "page": 1, "totalPages": 1, "totalItems": len(items)},
        )

    def test_bad_credentials_are_auth_failure(self):
        self.h.server.on(
            "POST", "/api/collections/users/auth-with-password", {"message": "no"}, status=400
        )
        rows, ok, _ = self.h.run(cfg(systems=[]))
        self.assertFalse(ok)
        self.assertTrue(any("authentication failed" in r for r in rows))

    def test_systems_list_is_authoritative(self):
        self.systems(
            [
                {"id": "a", "name": "alpha", "host": "127.0.0.1", "port": "45876"},
                {"id": "b", "name": "gone", "host": "10.0.0.9", "port": "45876"},
                {"id": "c", "name": "beta", "host": "10.0.0.1", "port": "45876"},
            ]
        )
        for m in ("POST", "PATCH", "DELETE"):
            self.h.server.on(m, "/api/collections/systems/records", {})
        self.h.server.on("DELETE", "/api/collections/systems/records/b", None)
        self.h.server.on("PATCH", "/api/collections/systems/records/c", {})
        systems = [
            {"name": "alpha", "host": "127.0.0.1"},
            {"name": "beta", "host": "10.0.0.2", "port": 45876},
            {"name": "new", "host": "10.0.0.3"},
        ]
        rows, ok, _ = self.h.run(cfg(systems=systems))
        self.assertTrue(ok)
        self.assertEqual(
            sorted(self.h.server.writes()),
            sorted(
                [
                    ("POST", "/api/collections/users/auth-with-password"),
                    ("PATCH", "/api/collections/systems/records/c"),
                    ("POST", "/api/collections/systems/records"),
                    ("DELETE", "/api/collections/systems/records/b"),
                ]
            ),
        )
        (create,) = self.h.server.made("POST", "/api/collections/systems/records")
        self.assertEqual(create[3]["users"], ["u1"])
        self.assertEqual(create[4]["Authorization"], "jwt-secret")

    def test_systems_are_read_across_pages(self):
        pages = {
            "1": {"items": [{"id": "a", "name": "one", "host": "h1", "port": "45876"}],
                  "page": 1, "totalPages": 2, "totalItems": 2},
            "2": {"items": [{"id": "b", "name": "two", "host": "h2", "port": "45876"}],
                  "page": 2, "totalPages": 2, "totalItems": 2},
        }  # fmt: skip
        self.h.server.on(
            "GET", "/api/collections/systems/records", lambda b, q: pages[q["page"][0]]
        )
        systems = [{"name": "one", "host": "h1"}, {"name": "two", "host": "h2"}]
        rows, ok, _ = self.h.run(cfg(systems=systems))
        self.assertTrue(ok, rows)
        self.assertEqual(rows, [])

    def test_item_count_mismatch_aborts(self):
        self.h.server.on(
            "GET",
            "/api/collections/systems/records",
            {"items": [], "page": 1, "totalPages": 1, "totalItems": 3},
        )
        rows, ok, _ = self.h.run(cfg(systems=[{"name": "x", "host": "h"}]))
        self.assertFalse(ok)
        self.assertFalse(self.h.server.made("POST", "/api/collections/systems/records"))

    def test_duplicate_system_names_rejected(self):
        rows, ok, _ = self.h.run(
            cfg(systems=[{"name": "a", "host": "h1"}, {"name": "a", "host": "h2"}])
        )
        self.assertFalse(ok)
        self.assertEqual(self.h.server.calls, [])

    def test_null_systems_is_unmanaged(self):
        self.h.run(cfg(systems=None))
        self.assertFalse(
            any(c[1].startswith("/api/collections/systems") for c in self.h.server.calls)
        )

    def test_empty_systems_refuses_to_delete_everything(self):
        self.systems([{"id": "a", "name": "one", "host": "h", "port": "45876"}])
        rows, ok, _ = self.h.run(cfg(systems=[]))
        self.assertFalse(ok)
        self.assertTrue(any("prune: true" in r for r in rows))
        self.assertFalse(self.h.server.made("DELETE", "/api/collections/systems/records/a"))
        self.h.server.on("DELETE", "/api/collections/systems/records/a", None)
        rows, ok, _ = self.h.run(cfg(systems=[], prune=True))
        self.assertTrue(ok, rows)
        self.assertTrue(self.h.server.made("DELETE", "/api/collections/systems/records/a"))

    def test_absent_systems_key_never_deletes(self):
        self.h.run(cfg())
        self.assertFalse(
            any(c[1].startswith("/api/collections/systems") for c in self.h.server.calls)
        )

    def test_webhook_added_once_and_never_shown(self):
        settings = {"items": [{"id": "s1", "settings": {"chartTime": "1h", "webhooks": []}}]}
        self.h.server.on("GET", "/api/collections/user_settings/records", settings)
        self.h.server.on("PATCH", "/api/collections/user_settings/records/s1", {})
        rows, ok, _ = self.h.run(cfg(discordWebhookFile="/s/hook"))
        self.assertTrue(ok)
        (patch,) = self.h.server.made("PATCH", "/api/collections/user_settings/records/s1")
        self.assertEqual(patch[3]["settings"]["webhooks"], ["discord://tok-SECRET@123"])
        self.assertEqual(patch[3]["settings"]["chartTime"], "1h")
        self.assertFalse(any("SECRET" in r for r in rows))

        settings["items"][0]["settings"]["webhooks"] = ["discord://tok-SECRET@123"]
        self.h.run(cfg(discordWebhookFile="/s/hook"))
        self.assertEqual(
            len(self.h.server.made("PATCH", "/api/collections/user_settings/records/s1")), 1
        )

    def test_user_settings_idempotent(self):
        settings = {"items": [{"id": "s1", "settings": {"chartTime": "12h"}}]}
        self.h.server.on("GET", "/api/collections/user_settings/records", settings)
        rows, ok, _ = self.h.run(cfg(userSettings={"chartTime": "12h"}))
        self.assertTrue(ok)
        self.assertEqual(rows, [])

    def test_dry_run_authenticates_but_writes_nothing(self):
        self.systems([])
        rows, ok, _ = self.h.run(cfg(systems=[{"name": "x", "host": "h"}]), apply=False)
        self.assertIn("  + system x (h:45876)", rows)
        self.assertEqual(
            self.h.server.writes(), [("POST", "/api/collections/users/auth-with-password")]
        )

    def test_remote_duplicate_system_names_abort(self):
        self.systems([{"id": "a", "name": "web"}, {"id": "b", "name": "web"}])
        rows, ok, _ = self.h.run(cfg(systems=[{"name": "web", "host": "h"}]))
        self.assertFalse(ok)
        self.assertEqual([w for w in self.h.server.writes() if "auth" not in w[1]], [])

    def test_null_webhooks_are_empty(self):
        settings = {"items": [{"id": "s1", "settings": {"webhooks": None, "emails": None}}]}
        self.h.server.on("GET", "/api/collections/user_settings/records", settings)
        self.h.server.on("PATCH", "/api/collections/user_settings/records/s1", {})
        rows, ok, _ = self.h.run(cfg(discordWebhookFile="/s/hook"))
        self.assertTrue(ok, rows)
        (patch,) = self.h.server.made("PATCH", "/api/collections/user_settings/records/s1")
        self.assertEqual(patch[3]["settings"]["webhooks"], ["discord://tok-SECRET@123"])

    def test_null_settings_object_aborts(self):
        settings = {"items": [{"id": "s1", "settings": None}]}
        self.h.server.on("GET", "/api/collections/user_settings/records", settings)
        rows, ok, _ = self.h.run(cfg(userSettings={"chartTime": "1h"}))
        self.assertFalse(ok)
        self.assertTrue(any("settings is null" in r for r in rows), rows)
        self.assertEqual(
            self.h.server.made("PATCH", "/api/collections/user_settings/records/s1"), []
        )

    def test_failed_system_create_keeps_stale(self):
        self.systems([{"id": "b", "name": "old", "host": "h", "port": "45876"}])
        self.h.server.on("POST", "/api/collections/systems/records", {"message": "x"}, status=400)
        rows, ok, _ = self.h.run(cfg(systems=[{"name": "new", "host": "h"}]))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.made("DELETE", "/api/collections/systems/records/b"), [])

    def test_missing_webhook_file_is_skipped(self):
        self.systems([])
        rows, ok, _ = self.h.run(cfg(discordWebhookFile="/s/missing", systems=[]))
        self.assertTrue(ok, rows)
        self.assertEqual(self.h.server.made("GET", "/api/collections/user_settings/records"), [])


if __name__ == "__main__":
    unittest.main()
