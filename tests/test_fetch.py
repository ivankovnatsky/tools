import contextlib
import hashlib
import io
import os
import stat
import tempfile
import unittest
import urllib.error
from unittest import mock

from tools import cli, diff
from tools.user import fetch, secrets_flow
from tools.user.files import _record as files_record

URL = "https://example.com/uk.utf-8.spl"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeOpener:
    def __init__(self, body=b"v1"):
        self.body = body
        self.calls = []

    def __call__(self, url, timeout):
        self.calls.append(url)
        if isinstance(self.body, Exception):
            raise self.body
        return io.BytesIO(self.body)


class FetchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.target = os.path.join(self.tmp, "spell", "uk.utf-8.spl")
        self.state = {}
        self.opener = FakeOpener()

    def tearDown(self):
        self._tmp.cleanup()

    def deploy(self, raw, update=False, conflicts=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return fetch.install_fetch(raw, self.state, conflicts, update, self.opener)

    def diff(self, raw, conflicts=None):
        return fetch.diff_fetch(raw, self.state, conflicts)

    def read(self):
        with open(self.target, "rb") as f:
            return f.read()

    def write(self, data: bytes):
        os.makedirs(os.path.dirname(self.target), exist_ok=True)
        with open(self.target, "wb") as f:
            f.write(data)

    def test_short_form_downloads_missing_target(self):
        raw = {self.target: URL}
        self.assertEqual(self.diff(raw), [f"  + fetch {self.target} <- {URL}"])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.read(), b"v1")
        self.assertEqual(stat.S_IMODE(os.stat(self.target).st_mode), 0o644)
        record = self.state["fetch"]["installed"][self.target]
        self.assertEqual((record["hash"], record["url"]), (sha(b"v1"), URL))
        self.assertEqual(self.diff(raw), [])
        self.assertEqual(
            [n for n in os.listdir(os.path.dirname(self.target)) if n.startswith(".")], []
        )

    def test_mode_applied(self):
        self.assertTrue(self.deploy({self.target: {"url": URL, "mode": "0755"}}))
        self.assertEqual(stat.S_IMODE(os.stat(self.target).st_mode), 0o755)

    def test_tracked_file_untouched_on_plain_deploy(self):
        raw = {self.target: URL}
        self.deploy(raw)
        self.opener.body = b"v2"
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.read(), b"v1")
        self.assertEqual(len(self.opener.calls), 1)

    def test_update_redownloads(self):
        raw = {self.target: URL}
        self.deploy(raw)
        self.opener.body = b"v2"
        self.assertTrue(self.deploy(raw, update=True))
        self.assertEqual(self.read(), b"v2")
        self.assertEqual(self.state["fetch"]["installed"][self.target]["hash"], sha(b"v2"))

    def test_url_change_redownloads(self):
        self.deploy({self.target: URL})
        self.opener.body = b"v2"
        other = "https://example.com/other.spl"
        self.assertEqual(
            self.diff({self.target: other}), [f"  ~ fetch {self.target} <- {other} (url changed)"]
        )
        self.assertTrue(self.deploy({self.target: other}))
        self.assertEqual(self.read(), b"v2")

    def test_sha_mismatch_refuses_install(self):
        raw = {self.target: {"url": URL, "sha256": sha(b"other")}}
        self.assertFalse(self.deploy(raw))
        self.assertFalse(os.path.exists(self.target))
        self.assertEqual(os.listdir(os.path.dirname(self.target)), [])
        self.assertNotIn(self.target, self.state["fetch"]["installed"])

    def test_sha_change_redownloads(self):
        self.deploy({self.target: {"url": URL, "sha256": sha(b"v1")}})
        self.opener.body = b"v2"
        raw = {self.target: {"url": URL, "sha256": sha(b"v2")}}
        self.assertEqual(self.diff(raw), [f"  ~ fetch {self.target} <- {URL} (sha256 changed)"])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.read(), b"v2")
        # Pinned and matching: --update has nothing to do.
        self.assertTrue(self.deploy(raw, update=True))
        self.assertEqual(len(self.opener.calls), 2)

    def test_modified_file_not_updated_or_removed(self):
        raw = {self.target: URL}
        self.deploy(raw)
        self.write(b"edited")
        self.assertFalse(self.deploy(raw, update=True))
        self.assertEqual(self.read(), b"edited")
        self.assertTrue(self.deploy(raw))
        self.assertEqual(
            self.diff({}), [f"  ~ keep {self.target} (modified since tools wrote it; not removed)"]
        )
        self.assertTrue(self.deploy({}))
        self.assertEqual(self.read(), b"edited")
        self.assertIn(self.target, self.state["fetch"]["installed"])

    def test_untracked_existing_file_refused_without_force(self):
        self.write(b"mine")
        raw = {self.target: URL}
        self.assertIn("not tracked by tools; set force: true", self.diff(raw)[0])
        self.assertFalse(self.deploy(raw))
        self.assertEqual(self.read(), b"mine")
        self.assertEqual(self.opener.calls, [])
        forced = {self.target: {"url": URL, "force": True}}
        self.assertEqual(
            self.diff(forced), [f"  ~ replace {self.target} <- {URL} (not tracked by tools)"]
        )
        self.assertTrue(self.deploy(forced))
        self.assertEqual(self.read(), b"v1")

    def test_untracked_file_matching_sha_is_adopted(self):
        self.write(b"v1")
        raw = {self.target: {"url": URL, "sha256": sha(b"v1")}}
        self.assertEqual(self.diff(raw), [f"  ~ adopt {self.target} (matches sha256)"])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.opener.calls, [])
        self.assertIn(self.target, self.state["fetch"]["installed"])
        self.assertEqual(self.diff(raw), [])

    def test_truncated_download_refused(self):
        class Short(io.BytesIO):
            headers = {"Content-Length": "10"}

        self.opener = lambda url, timeout: Short(b"v1")
        self.assertFalse(self.deploy({self.target: URL}))
        self.assertFalse(os.path.exists(self.target))
        self.assertEqual(os.listdir(os.path.dirname(self.target)), [])
        self.assertNotIn(self.target, self.state["fetch"]["installed"])

    def test_complete_download_with_length_accepted(self):
        class Full(io.BytesIO):
            headers = {"Content-Length": "2"}

        self.opener = lambda url, timeout: Full(b"v1")
        self.assertTrue(self.deploy({self.target: URL}))
        self.assertEqual(self.read(), b"v1")

    def test_removal_when_dropped(self):
        self.deploy({self.target: URL})
        self.assertEqual(self.diff({}), [f"  - remove {self.target}"])
        self.assertTrue(self.deploy({}))
        self.assertFalse(os.path.exists(self.target))
        self.assertEqual(self.state["fetch"]["installed"], {})

    def test_config_error_holds_removals(self):
        self.deploy({self.target: URL})
        bad = {os.path.join(self.tmp, "x"): "http://insecure.example/x"}
        self.assertFalse(self.deploy(bad))
        self.assertTrue(os.path.exists(self.target))
        self.assertIn(
            f"  ~ hold {self.target} (would remove; held until config errors are fixed)",
            self.diff(bad),
        )

    def test_download_error_does_not_abort_other_entries(self):
        other = os.path.join(self.tmp, "other")
        calls = []

        def opener(url, timeout):
            calls.append(url)
            if url.endswith("/bad"):
                raise urllib.error.URLError("boom")
            return io.BytesIO(b"ok")

        raw = {self.target: "https://example.com/bad", other: "https://example.com/good"}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(fetch.install_fetch(raw, self.state, None, False, opener))
        self.assertFalse(os.path.exists(self.target))
        with open(other, "rb") as f:
            self.assertEqual(f.read(), b"ok")

    def test_parse_errors(self):
        _entries, errors = fetch.parse(
            {
                "relative/path": URL,
                "/a": "http://example.com/a",
                "/b": {"url": URL, "sha256": "zz"},
                "/c": {"url": URL, "mode": 644},
                "/d": {"url": URL, "bogus": 1},
            }
        )
        self.assertEqual(len(errors), 5, errors)

    def test_redirect_to_http_refused(self):
        handler = fetch._HttpsRedirects()
        with self.assertRaises(fetch.FetchError):
            handler.redirect_request(None, None, 302, "Found", {}, "http://example.com/x")

    def test_conflict_with_files_refused_by_both(self):
        src = os.path.join(self.tmp, "src")
        with open(src, "w") as f:
            f.write("x")
        raw = {self.target: URL}
        conflicts = secrets_flow.ownership_conflicts(
            files_entries=[{"source": src, "target": self.target}],
            fetch_raw=raw,
            config_dir=self.tmp,
        )
        self.assertEqual(conflicts.get(self.target), "claimed by fetch and files")
        self.assertEqual(
            self.diff(raw, conflicts), [f"  ! refusing {self.target}: claimed by fetch and files"]
        )
        self.assertFalse(self.deploy(raw, conflicts=conflicts))
        self.assertFalse(os.path.exists(self.target))

    def test_pinned_file_edited_is_left_alone(self):
        raw = {self.target: {"url": URL, "sha256": sha(b"v1")}}
        self.deploy(raw)
        self.write(b"edited")
        self.assertEqual(self.diff(raw), [])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.read(), b"edited")
        self.assertEqual(len(self.opener.calls), 1)

    def test_move_from_files_to_fetch_downloads_and_keeps_no_secret_mode(self):
        src = os.path.join(self.tmp, "src")
        with open(src, "wb") as f:
            f.write(b"secret")
        self.write(b"secret")
        os.chmod(self.target, 0o600)
        self.state["files"] = {self.target: files_record(self.target, sha(b"secret"))}
        moved = secrets_flow.move_between_sections(self.state, fetch_raw={self.target: URL})
        self.assertEqual(moved, [f"{self.target} (files -> fetch)"])
        raw = {self.target: URL}
        self.assertEqual(self.diff(raw), [f"  ~ fetch {self.target} <- {URL} (new url)"])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.read(), b"v1")
        self.assertEqual(self.state["fetch"]["installed"][self.target]["url"], URL)

    def test_malformed_url_is_config_error(self):
        entries, errors = fetch.parse({self.target: "https://[broken"})
        self.assertEqual(entries, [])
        self.assertEqual(len(errors), 1)

    def test_file_appearing_during_download_is_kept(self):
        target = self.target

        def opener(url, timeout):
            self.write(b"user")  # created while the download runs
            return io.BytesIO(b"v1")

        self.opener = opener
        self.assertFalse(self.deploy({target: URL}))
        self.assertEqual(self.read(), b"user")
        self.assertNotIn(target, self.state["fetch"]["installed"])

    def test_edit_during_update_is_kept(self):
        raw = {self.target: URL}
        self.deploy(raw)

        def opener(url, timeout):
            self.write(b"edited")
            return io.BytesIO(b"v2")

        self.opener = opener
        self.assertFalse(self.deploy(raw, update=True))
        self.assertEqual(self.read(), b"edited")

    def test_records_survive_interrupted_run(self):
        other = os.path.join(self.tmp, "spell", "b.spl")
        calls = []

        def opener(url, timeout):
            calls.append(url)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return io.BytesIO(b"v1")

        self.opener = opener
        with self.assertRaises(KeyboardInterrupt):
            self.deploy({self.target: URL, other: "https://example.com/b"})
        self.assertIn(self.target, self.state["fetch"]["installed"])

    def test_pinned_url_change_keeps_file_and_records_url(self):
        pin = sha(b"v1")
        self.deploy({self.target: {"url": URL, "sha256": pin}})
        other = "https://example.com/mirror.spl"
        raw = {self.target: {"url": other, "sha256": pin}}
        self.assertEqual(self.diff(raw), [])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(len(self.opener.calls), 1)
        self.assertEqual(self.state["fetch"]["installed"][self.target]["url"], other)
        # Dropping the pin later is not a url change.
        self.assertEqual(self.diff({self.target: other}), [])

    def test_same_file_declared_twice_through_alias(self):
        os.makedirs(os.path.dirname(self.target), exist_ok=True)
        alias_dir = os.path.join(self.tmp, "alias")
        os.symlink(os.path.dirname(self.target), alias_dir)
        alias = os.path.join(alias_dir, os.path.basename(self.target))
        entries, errors = fetch.parse({self.target: URL, alias: URL})
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(errors), 1)

    def test_pinned_chmod_records_url(self):
        pin = sha(b"v1")
        self.deploy({self.target: {"url": URL, "sha256": pin}})
        other = "https://example.com/mirror.spl"
        self.assertTrue(self.deploy({self.target: {"url": other, "sha256": pin, "mode": "0755"}}))
        self.assertEqual(self.state["fetch"]["installed"][self.target]["url"], other)

    def test_tracked_file_already_holding_new_pin_is_adopted(self):
        self.deploy({self.target: {"url": URL, "sha256": sha(b"v1")}})
        self.write(b"v2")  # placed by hand, then the pin is bumped to match
        raw = {self.target: {"url": URL, "sha256": sha(b"v2")}}
        self.assertEqual(self.diff(raw), [f"  ~ adopt {self.target} (matches sha256)"])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(len(self.opener.calls), 1)
        self.assertEqual(self.state["fetch"]["installed"][self.target]["hash"], sha(b"v2"))
        self.assertEqual(self.diff(raw), [])

    def test_edit_during_other_download_blocks_removal(self):
        dropped = os.path.join(self.tmp, "spell", "old.spl")
        self.target, kept = dropped, self.target
        self.deploy({dropped: URL})
        self.target = kept

        def opener(url, timeout):
            with open(dropped, "wb") as f:
                f.write(b"edited")
            return io.BytesIO(b"v1")

        self.opener = opener
        self.deploy({kept: URL})
        with open(dropped, "rb") as f:
            self.assertEqual(f.read(), b"edited")
        self.assertIn(dropped, self.state["fetch"]["installed"])

    def test_mode_out_of_range_is_config_error(self):
        for bad in ("77777", "-1"):
            entries, errors = fetch.parse({self.target: {"url": URL, "mode": bad}})
            self.assertEqual(entries, [])
            self.assertEqual(len(errors), 1)

    def test_edit_during_other_download_skips_chmod(self):
        first = os.path.join(self.tmp, "spell", "a.spl")
        self.deploy({self.target: URL})
        os.chmod(self.target, 0o600)  # plan will want a chmod back to 0644
        self.write(b"v1")  # unchanged content, still tracked

        def opener(url, timeout):
            self.write(b"edited")  # edit lands while the other entry downloads
            os.chmod(self.target, 0o600)
            return io.BytesIO(b"x")

        self.opener = opener
        self.assertTrue(self.deploy({first: URL, self.target: URL}))
        self.assertEqual(self.read(), b"edited")
        self.assertEqual(stat.S_IMODE(os.stat(self.target).st_mode), 0o600)

    def test_move_from_fetch_to_files(self):
        self.deploy({self.target: URL})
        src = os.path.join(self.tmp, "src")
        with open(src, "wb") as f:
            f.write(b"v1")
        moved = secrets_flow.move_between_sections(
            self.state,
            files_entries=[{"source": src, "target": self.target}],
            config_dir=self.tmp,
        )
        self.assertEqual(moved, [f"{self.target} (fetch -> files)"])
        self.assertIn(self.target, self.state["files"])


class FetchCliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.target = os.path.join(self.tmp, "f")
        self.state_file = os.path.join(self.tmp, "state.json")
        self.config = {"stateFile": self.state_file, "fetch": {self.target: URL}}

    def tearDown(self):
        self._tmp.cleanup()

    def run_deploy(self, scope=(), update=False):
        opener = FakeOpener()
        with (
            mock.patch.object(fetch, "_open", opener),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            ok = cli._deploy(self.config, self.tmp, scope, update)
        return ok, opener

    def test_scope_gating(self):
        ok, opener = self.run_deploy(scope=("files",))
        self.assertTrue(ok)
        self.assertEqual(opener.calls, [])
        self.assertFalse(os.path.exists(self.target))
        ok, opener = self.run_deploy(scope=("fetch",))
        self.assertTrue(ok)
        self.assertEqual(opener.calls, [URL])
        self.assertTrue(os.path.exists(self.target))

    def test_update_flag_reaches_fetch(self):
        self.run_deploy()
        _ok, opener = self.run_deploy()
        self.assertEqual(opener.calls, [])
        _ok, opener = self.run_deploy(update=True)
        self.assertEqual(opener.calls, [URL])

    def test_diff_makes_no_network_calls(self):
        out = io.StringIO()
        with (
            mock.patch.object(fetch, "_open", side_effect=AssertionError("network")),
            contextlib.redirect_stdout(out),
        ):
            clean = diff.show_diff(self.config, self.tmp, ("fetch",))
        self.assertFalse(clean)
        self.assertIn(f"+ fetch {self.target} <- {URL}", out.getvalue())


if __name__ == "__main__":
    unittest.main()
