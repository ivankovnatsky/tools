import contextlib
import io
import unittest
import urllib.error
from unittest import mock

from mgmt_fakes import FakeServer, Harness

from tools import cli
from tools import diff as diff_mod
from tools.mgmt.http import ApiClient, Unreachable

URL = "http://svc.example.com"
U = URL
KEY = "/secrets/jellyfin-key"


def jellyfin(**extra):
    return {
        "jellyfin": {"baseUrl": URL, "apiKeyFile": KEY, "retryDelay": 1, "libraries": [], **extra}
    }


class GatingTest(unittest.TestCase):
    def _deploy(self, config, state):
        with (
            mock.patch.object(cli, "load_json", return_value=state),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(cli, "save_json"),
            mock.patch.object(cli, "state_lock"),
            mock.patch.object(cli, "install_mgmt", return_value=True) as install,
        ):
            cli._deploy(config, "/tmp")
        return install

    def test_absent_section_is_a_no_op(self):
        self._deploy({}, {}).assert_not_called()

    def test_state_alone_does_not_trigger_network(self):
        # Nothing is deleted on the strength of state, so stale fingerprints
        # must not wake the reconciler.
        state = {"mgmt": {"arr": {"fingerprints": {"x": "y"}}}}
        self._deploy({}, state).assert_not_called()

    def test_all_null_services_is_a_no_op(self):
        self._deploy({"mgmt": {"jellyfin": None, "arr": None}}, {}).assert_not_called()

    def test_null_service_is_unmanaged_next_to_real_one(self):
        h = Harness(files={KEY: "k"})
        h.server.on("GET", "/Library/VirtualFolders", [])
        rows, ok, _ = h.run({**jellyfin(), "beszel": None, "bogus": None})
        self.assertTrue(ok, rows)
        self.assertEqual(rows, [])

    def test_dropped_service_is_shown_once_and_released(self):
        h = Harness(files={KEY: "k"})
        h.server.on("GET", "/Library/VirtualFolders", [])
        state = {"arr": {"fingerprints": {"x": "y"}}, "jellyfin": {}}
        row = "  = arr removed from config; remote objects are kept"
        rows, ok, _ = h.run(jellyfin(), state=state, apply=False)
        self.assertTrue(ok, rows)
        self.assertEqual(rows, [row])
        with contextlib.redirect_stdout(io.StringIO()):
            rows, ok, state = h.run(jellyfin(), state=state)
        self.assertEqual(rows, [row])
        self.assertNotIn("arr", state)
        rows, _, _ = h.run(jellyfin(), state=state)
        self.assertEqual(rows, [])

    def test_absent_section_releases_state_without_network(self):
        state = {"mgmt": {"arr": {"fingerprints": {"x": "y"}}}}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self._deploy({}, state).assert_not_called()
        self.assertEqual(state["mgmt"], {})
        self.assertIn("arr removed from config", out.getvalue())

    def test_diff_shows_dropped_service_without_network(self):
        import json
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            state_file = os.path.join(tmp, "state.json")
            with open(state_file, "w") as f:
                json.dump({"mgmt": {"beszel": {}}}, f)
            out = io.StringIO()
            with (
                mock.patch.object(diff_mod, "diff_mgmt") as d,
                contextlib.redirect_stdout(out),
            ):
                clean = diff_mod.show_diff({"stateFile": state_file}, "/tmp")
            d.assert_not_called()
            self.assertFalse(clean)
            self.assertIn("beszel removed from config", out.getvalue())
            with open(state_file) as f:
                self.assertEqual(json.load(f), {"mgmt": {"beszel": {}}})

    def test_present_section_runs(self):
        self._deploy({"mgmt": jellyfin()}, {}).assert_called_once()

    def test_diff_skips_absent_section(self):
        with (
            mock.patch.object(diff_mod, "diff_mgmt") as d,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            diff_mod.show_diff({"stateFile": "/nonexistent/state.json"}, "/tmp")
        d.assert_not_called()

    def test_diff_renders_service_rows_and_reports_pending(self):
        out = io.StringIO()
        with (
            mock.patch.object(diff_mod, "diff_mgmt", return_value=[("jellyfin", ["    + x"])]),
            contextlib.redirect_stdout(out),
        ):
            clean = diff_mod.show_diff(
                {"stateFile": "/nonexistent/state.json", "mgmt": jellyfin()}, "/tmp"
            )
        self.assertFalse(clean)
        self.assertIn("jellyfin:", out.getvalue())

    def test_failure_propagates_to_deploy(self):
        with (
            mock.patch.object(cli, "load_json", return_value={}),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(cli, "save_json"),
            mock.patch.object(cli, "state_lock"),
            mock.patch.object(cli, "install_mgmt", return_value=False),
        ):
            self.assertFalse(cli._deploy({"mgmt": jellyfin()}, "/tmp"))


class ReachabilityTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(files={KEY: "k3y-secret\n"})
        self.h.server.down = True

    def test_unreachable_retries_with_backoff_then_fails(self):
        with contextlib.redirect_stdout(io.StringIO()):
            rows, ok, _ = self.h.run({"jellyfin": {**jellyfin()["jellyfin"], "retries": 3}})
        self.assertFalse(ok)
        self.assertEqual(self.h.sleeps, [1, 2, 4])
        self.assertTrue(any("unreachable after 4 attempt(s)" in r for r in rows))

    def test_diff_does_not_wait_for_a_down_service(self):
        rows, ok, _ = self.h.run(jellyfin(), apply=False)
        self.assertEqual(self.h.sleeps, [])
        self.assertEqual(len(self.h.server.calls), 1)
        self.assertTrue(any("! " in r and "unreachable" in r for r in rows))

    def test_one_service_down_does_not_stop_others(self):
        # An unknown service fails on its own; jellyfin is still attempted.
        config = {**jellyfin(), "bogus": {}}
        with contextlib.redirect_stdout(io.StringIO()):
            rows, ok, _ = self.h.run(config)
        self.assertFalse(ok)
        self.assertTrue(any("unknown service 'bogus'" in r for r in rows))
        self.assertTrue(any("unreachable" in r for r in rows))

    def test_writes_are_not_retried(self):
        server = FakeServer()
        server.down = True
        sleeps = []
        client = ApiClient(URL, label="t", retries=3, opener=server, sleep=sleeps.append)
        with self.assertRaises(Unreachable):
            client.request("POST", "/x", {"a": 1})
        self.assertEqual(len(server.calls), 1)
        self.assertEqual(sleeps, [])

    def test_503_is_retried_for_reads(self):
        server = FakeServer()
        replies = iter([(503, None), (200, {"ok": True})])
        server.on("GET", "/x", lambda b, q: next(replies))
        client = ApiClient(URL, label="t", retries=2, opener=server, sleep=lambda s: None)
        self.assertEqual(client.request("GET", "/x"), {"ok": True})


class AuthAndSecretsTest(unittest.TestCase):
    def test_auth_failure_fails_service_without_retry(self):
        h = Harness(files={KEY: "k3y-secret"})
        h.server.on("GET", "/Library/VirtualFolders", {"message": "nope"}, status=401)
        rows, ok, _ = h.run(jellyfin(libraries=[]), apply=False)
        self.assertFalse(ok)
        self.assertEqual(len(h.server.calls), 1)
        self.assertTrue(any("authentication failed (HTTP 401" in r for r in rows))

    def test_inline_secret_is_rejected(self):
        h = Harness()
        rows, ok, _ = h.run({"jellyfin": {"baseUrl": URL, "apiKey": "plain"}}, apply=False)
        self.assertFalse(ok)
        self.assertEqual(h.server.calls, [])
        self.assertTrue(any("use apiKeyFile" in r for r in rows))

    def test_missing_secret_file_names_path_only(self):
        h = Harness()
        rows, ok, _ = h.run(jellyfin(), apply=False)
        self.assertFalse(ok)
        self.assertTrue(any(KEY in r for r in rows))
        self.assertEqual(h.server.calls, [])

    def test_secret_base_url_is_redacted(self):
        h = Harness(files={KEY: "k", "/secrets/domain": "hidden-domain.example.org\n"})
        h.server.down = True
        cfg = jellyfin(baseUrl="http://jf.@DOMAIN@", baseUrlFiles={"DOMAIN": "/secrets/domain"})
        rows, _, _ = h.run(cfg, apply=False)
        self.assertTrue(h.server.urls[0].startswith("http://jf.hidden-domain.example.org/"))
        self.assertFalse(any("hidden-domain" in r for r in rows))

    def test_diff_never_mutates_state(self):
        h = Harness(files={KEY: "k"})
        h.server.on("GET", "/Library/VirtualFolders", [])
        state = {"mgmt": {}}
        with contextlib.redirect_stdout(io.StringIO()):
            from tools.mgmt import diff_mgmt

            with mock.patch("tools.mgmt.Context", h.factory):
                diff_mgmt(jellyfin(libraries=[]), "/tmp", state)
        self.assertEqual(state, {"mgmt": {}})


class TruncationTest(unittest.TestCase):
    def test_long_server_error_redacted_before_truncation(self):
        from tools.mgmt.engine import MAX_LINE, Context

        ctx = Context("x", apply=False, state={}, reader=lambda p: "s3cr3t-value-long")
        ctx.secret("/s", "x")
        ctx.fail("a" * (MAX_LINE - 10) + "s3cr3t-value-long")
        (row,) = ctx.changes
        self.assertNotIn("s3cr3t", row)
        self.assertLessEqual(len(row), MAX_LINE + 4)


class HttpErrorDetailTest(unittest.TestCase):
    def test_error_body_detail_is_reported(self):
        server = FakeServer().on("GET", "/x", {"message": "bad thing"}, status=400)
        client = ApiClient(URL, label="t", opener=server)
        with self.assertRaisesRegex(Exception, "HTTP 400.*bad thing"):
            client.request("GET", "/x")

    def test_allowed_status_returns_none(self):
        server = FakeServer()
        client = ApiClient(URL, label="t", opener=server)
        from tools.mgmt.http import ABSENT

        self.assertIs(client.request("GET", "/missing", allow=(404,)), ABSENT)

    def test_urlerror_reason_reported(self):
        def opener(req, timeout=None):
            raise urllib.error.URLError("dns failure")

        client = ApiClient(URL, label="t", opener=opener)
        with self.assertRaisesRegex(Unreachable, "dns failure"):
            client.request("GET", "/x")


class FingerprintKeyTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_key_created_0600_once_and_reused(self):
        import os
        import stat

        from tools.mgmt.keys import key_path, load_key

        path = key_path(os.path.join(self.dir, "state.json"))
        self.assertIsNone(load_key(path, create=False))
        self.assertFalse(os.path.exists(path))
        key = load_key(path, create=True)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(load_key(path, create=True), key)

    def test_digest_is_keyed_hmac_not_plain_hash(self):
        import hashlib

        from tools.mgmt.engine import Context

        a = Context("x", apply=True, state={}, fingerprint_key=b"a" * 64)
        b = Context("x", apply=True, state={}, fingerprint_key=b"b" * 64)
        a.record_secret(U, "id", "hunter2")
        b.record_secret(U, "id", "hunter2")
        da, db = a.state["fingerprints"]["id"], b.state["fingerprints"]["id"]
        self.assertNotEqual(da, db)
        self.assertNotIn(hashlib.sha256(b"hunter2").hexdigest(), (da, db))
        self.assertNotIn("salt", a.state)
        self.assertFalse(a.secret_changed(U, "id", "hunter2"))
        self.assertFalse(a.secret_changed("HTTP://Svc.example.com/", "id", "hunter2"))
        self.assertTrue(a.secret_changed("http://other.example.com", "id", "hunter2"))
        self.assertTrue(a.secret_changed(U, "id", "other"))

    def test_without_key_secrets_read_as_changed_and_nothing_recorded(self):
        from tools.mgmt.engine import Context

        ctx = Context("x", apply=True, state={"fingerprints": {"id": "zz"}})
        self.assertTrue(ctx.secret_changed(U, "id", "v"))
        ctx.record_secret(U, "id", "v")
        self.assertEqual(ctx.state["fingerprints"], {"id": "zz"})

    def test_state_file_written_0600(self):
        import os
        import stat

        from tools.state import save_json

        path = os.path.join(self.dir, "state.json")
        with open(path, "w") as f:
            f.write("{}")
        os.chmod(path, 0o644)
        save_json(path, {"a": 1})
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_diff_does_not_create_key_but_deploy_does(self):
        import os

        from tools.mgmt import diff_mgmt, install_mgmt

        key = os.path.join(self.dir, "mgmt.key")
        h = Harness(files={KEY: "k"})
        h.server.on("GET", "/Library/VirtualFolders", [])
        with (
            mock.patch("tools.mgmt.Context", h.factory),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            diff_mgmt(jellyfin(), "/tmp", {}, key)
            self.assertFalse(os.path.exists(key))
            install_mgmt(jellyfin(), "/tmp", {}, key)
        self.assertTrue(os.path.exists(key))

    def test_diff_leaves_loose_key_mode_and_reports_it(self):
        import os
        import stat

        from tools.mgmt import diff_mgmt, install_mgmt

        key = os.path.join(self.dir, "mgmt.key")
        with open(key, "w") as f:
            f.write("k" * 64)
        os.chmod(key, 0o644)
        before = os.stat(key)
        listing = sorted(os.listdir(self.dir))
        h = Harness(files={KEY: "k"})
        h.server.on("GET", "/Library/VirtualFolders", [])
        with (
            mock.patch("tools.mgmt.Context", h.factory),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            out = diff_mgmt(jellyfin(), "/tmp", {}, key)
            after = os.stat(key)
            self.assertEqual(stat.S_IMODE(after.st_mode), 0o644)
            self.assertEqual(
                (after.st_mtime_ns, after.st_ctime_ns), (before.st_mtime_ns, before.st_ctime_ns)
            )
            self.assertEqual(sorted(os.listdir(self.dir)), listing)
            self.assertTrue(any("0644 -> 0600" in r for _, rows in out for r in rows), out)
            install_mgmt(jellyfin(), "/tmp", {}, key)
        self.assertEqual(stat.S_IMODE(os.stat(key).st_mode), 0o600)


class KeyFileErrorTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.key = f"{self._tmp.name}/mgmt.key"

    def tearDown(self):
        self._tmp.cleanup()

    def test_truncated_key_is_an_error_row_not_a_crash(self):
        from tools.mgmt import diff_mgmt, install_mgmt

        with open(self.key, "w") as f:
            f.write("short")
        rows = diff_mgmt(jellyfin(), "/tmp", {}, self.key)
        self.assertTrue(any("empty or truncated" in r for _, rs in rows for r in rs))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(install_mgmt(jellyfin(), "/tmp", {}, self.key))

    def test_deploy_continues_other_sections_on_bad_key(self):
        with open(self.key, "w") as f:
            f.write("")
        with (
            mock.patch.object(cli, "load_json", return_value={}),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(cli, "save_json"),
            mock.patch.object(cli, "state_lock"),
            mock.patch.object(cli, "mgmt_key_path", return_value=self.key),
            mock.patch.object(cli, "install_flatpak_packages", return_value=True) as fp,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            ok = cli._deploy({"mgmt": jellyfin()}, "/tmp")
        self.assertFalse(ok)
        fp.assert_called_once()

    def test_create_race_uses_the_winners_key(self):
        import os

        from tools.mgmt import keys

        real_link = os.link

        def racing_link(src, dst):
            with open(dst, "w") as f:
                f.write("w" * 64)
            raise FileExistsError(17, "File exists", dst)

        with mock.patch.object(keys.os, "link", racing_link):
            self.assertEqual(keys.load_key(self.key, create=True), b"w" * 64)
        self.assertEqual(os.listdir(self._tmp.name), ["mgmt.key"])
        self.assertIs(os.link, real_link)


class SubstitutionRedactionTest(unittest.TestCase):
    def test_base_url_file_with_inner_newline_is_redacted(self):
        h = Harness(files={KEY: "k", "/secrets/domain": "hid\nden.example.org\n"})
        h.server.on("GET", "/Library/VirtualFolders", {"message": "x"}, status=500)
        cfg = jellyfin(baseUrl="http://jf.@DOMAIN@/x", baseUrlFiles={"DOMAIN": "/secrets/domain"})
        rows, ok, _ = h.run(cfg, apply=False)
        self.assertFalse(ok)
        self.assertTrue(h.server.urls[0].startswith("http://jf.hidden.example.org/"))
        self.assertFalse(any("hidden.example" in r for r in rows), rows)


if __name__ == "__main__":
    unittest.main()
