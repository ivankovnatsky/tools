import contextlib
import hashlib
import io
import os
import stat
import tarfile
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

    def backups(self):
        d, base = os.path.split(self.target)
        return sorted(os.path.join(d, n) for n in os.listdir(d) if n == base + ".tools")

    def aside(self, reason):
        return f"{reason}; old file moved aside to {self.target}.tools"

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

    def test_modified_file_moved_aside_and_fetched_again(self):
        raw = {self.target: URL}
        self.deploy(raw)
        self.write(b"edited")
        why = self.aside("modified since tools wrote it")
        self.assertEqual(self.diff(raw), [f"  ~ replace {self.target} <- {URL} ({why})"])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.read(), b"v1")
        [backup] = self.backups()
        with open(backup, "rb") as f:
            self.assertEqual(f.read(), b"edited")
        self.assertEqual(self.diff(raw), [])

    def test_modified_file_not_removed(self):
        raw = {self.target: URL}
        self.deploy(raw)
        self.write(b"edited")
        self.assertEqual(
            self.diff({}), [f"  ~ keep {self.target} (modified since tools wrote it; not removed)"]
        )
        self.assertTrue(self.deploy({}))
        self.assertEqual(self.read(), b"edited")
        self.assertIn(self.target, self.state["fetch"]["installed"])

    def test_untracked_existing_file_moved_aside(self):
        self.write(b"mine")
        os.chmod(self.target, 0o600)
        raw = {self.target: URL}
        why = self.aside("not tracked by tools")
        self.assertEqual(self.diff(raw), [f"  ~ replace {self.target} <- {URL} ({why})"])
        with mock.patch("tools.user.fetch.log") as log:
            self.assertTrue(fetch.install_fetch(raw, self.state, None, False, self.opener))
        self.assertEqual(self.read(), b"v1")
        [backup] = self.backups()
        self.assertEqual(backup, self.target + ".tools")
        self.assertEqual(stat.S_IMODE(os.lstat(backup).st_mode), 0o600)
        with open(backup, "rb") as f:
            self.assertEqual(f.read(), b"mine")
        self.assertIn(backup, repr(log.call_args_list))
        self.assertEqual(self.diff(raw), [])

    def test_failed_download_leaves_untracked_file_in_place(self):
        self.write(b"mine")
        self.opener.body = urllib.error.URLError("down")
        self.assertFalse(self.deploy({self.target: URL}))
        self.assertEqual(self.read(), b"mine")
        self.assertEqual(self.backups(), [])

    def test_symlink_target_is_moved_aside_not_followed(self):
        other = os.path.join(self.tmp, "other")
        with open(other, "wb") as f:
            f.write(b"theirs")
        os.makedirs(os.path.dirname(self.target))
        os.symlink(other, self.target)
        raw = {self.target: URL}
        self.assertIn(self.aside("a symlink"), self.diff(raw)[0])
        self.assertTrue(self.deploy(raw))
        self.assertFalse(os.path.islink(self.target))
        self.assertEqual(self.read(), b"v1")
        [backup] = self.backups()
        self.assertEqual(os.readlink(backup), other)
        with open(other, "rb") as f:
            self.assertEqual(f.read(), b"theirs")

    def test_second_drift_refused_until_backup_removed(self):
        self.write(b"one")
        raw = {self.target: URL}
        self.assertTrue(self.deploy(raw))
        [backup] = self.backups()
        self.write(b"two")
        rows = self.diff(raw)
        self.assertEqual(
            rows, [f"  ! {self.target} changed again; an earlier backup {backup} exists; remove it"]
        )
        self.assertFalse(self.deploy(raw))
        self.assertEqual(self.read(), b"two")
        self.assertEqual(self.backups(), [backup])
        os.remove(backup)
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.read(), b"v1")
        self.assertEqual(len(self.backups()), 1)

    def test_force_key_is_an_error(self):
        rows = self.diff({self.target: {"url": URL, "force": True}})
        self.assertIn("tools now always moves the old file aside", rows[0])

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

    def test_pinned_file_edited_is_moved_aside(self):
        raw = {self.target: {"url": URL, "sha256": sha(b"v1")}}
        self.deploy(raw)
        self.write(b"edited")
        self.assertEqual(len(self.diff(raw)), 1)
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.read(), b"v1")
        self.assertEqual(len(self.opener.calls), 2)
        self.assertEqual(len(self.backups()), 1)

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

    def test_bad_port_is_config_error(self):
        entries, errors = fetch.parse({self.target: "https://example.com:not-a-port/f"})
        self.assertEqual(entries, [])
        self.assertEqual(len(errors), 1)

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


def targz(files) -> bytes:
    """``files``: name -> (bytes, mode)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, (data, mode) in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


APP = {
    "dist/App.app/Contents/MacOS/App": (b"bin", 0o755),
    "dist/App.app/Contents/Info.plist": (b"plist", 0o644),
}
BIN_SCRIPT = 'tar -xzf "$src"\nmv tool "$out"\n'
APP_SCRIPT = 'tar -xzf "$src"\nmv dist/App.app "$out"\n'


class FetchInstallTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.bin = os.path.join(self.tmp, "bin", "tool")
        self.app = os.path.join(self.tmp, "Applications", "App.app")
        self.state = {}
        self.opener = FakeOpener(targz({"LICENSE": (b"l", 0o644), "tool": (b"t1", 0o755)}))

    def tearDown(self):
        self._tmp.cleanup()

    def deploy(self, raw, update=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = fetch.install_fetch(raw, self.state, None, update, self.opener)
        self.output = out.getvalue()
        return ok

    def diff(self, raw):
        return fetch.diff_fetch(raw, self.state)

    def app_raw(self, files=APP, script=APP_SCRIPT, **kw):
        self.opener.body = targz(files)
        return {self.app: {"url": URL, "sha256": sha(self.opener.body), "install": script, **kw}}

    def read(self, path):
        with open(path, "rb") as f:
            return f.read()

    def leftovers(self, path):
        return [n for n in os.listdir(os.path.dirname(path)) if n.startswith(".")]

    def test_file_result_with_mode(self):
        raw = {self.bin: {"url": URL, "install": BIN_SCRIPT, "mode": "0700"}}
        self.assertEqual(self.diff(raw), [f"  + fetch {self.bin} <- {URL} (install)"])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.read(self.bin), b"t1")
        self.assertEqual(stat.S_IMODE(os.stat(self.bin).st_mode), 0o700)
        record = self.state["fetch"]["installed"][self.bin]
        self.assertEqual(
            {k: v for k, v in record.items() if k not in ("dev", "ino", "mtime_ns", "birthtime")},
            {"url": URL, "sha256": sha(self.opener.body), "install": BIN_SCRIPT, "type": "file"},
        )
        self.assertEqual(record["ino"], os.lstat(self.bin).st_ino)
        self.assertEqual(self.leftovers(self.bin), [])
        self.assertEqual(self.diff(raw), [])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(len(self.opener.calls), 1)

    def test_file_result_keeps_its_mode_without_mode(self):
        raw = {self.bin: {"url": URL, "install": BIN_SCRIPT}}
        self.assertTrue(self.deploy(raw))
        self.assertEqual(stat.S_IMODE(os.stat(self.bin).st_mode), 0o755)
        self.assertEqual(self.diff(raw), [])

    def test_script_env_and_cwd(self):
        script = 'test "$PWD" = "$tmp"\ntest -z "$(ls -A)"\ncp "$src" "$out"\n'
        raw = {self.bin: {"url": URL, "install": script}}
        self.assertTrue(self.deploy(raw), self.output)
        self.assertEqual(self.read(self.bin), self.opener.body)

    def test_directory_result_records_inputs_only(self):
        raw = self.app_raw()
        self.assertTrue(self.deploy(raw), self.output)
        exe = os.path.join(self.app, "Contents", "MacOS", "App")
        self.assertEqual(self.read(exe), b"bin")
        self.assertEqual(stat.S_IMODE(os.stat(exe).st_mode), 0o755)
        record = self.state["fetch"]["installed"][self.app]
        self.assertEqual(
            {k: v for k, v in record.items() if k not in ("dev", "ino", "mtime_ns", "birthtime")},
            {"url": URL, "sha256": raw[self.app]["sha256"], "install": APP_SCRIPT, "type": "dir"},
        )
        self.assertEqual(record["ino"], os.lstat(self.app).st_ino)
        self.assertEqual(self.leftovers(self.app), [])
        self.assertEqual(self.diff(raw), [])
        self.assertTrue(self.deploy(raw, update=True))
        self.assertEqual(len(self.opener.calls), 1)

    def test_sha256_pins_the_download(self):
        raw = self.app_raw()
        raw[self.app]["sha256"] = sha(b"other")
        self.assertFalse(self.deploy(raw))
        self.assertIn("sha256 mismatch", self.output)
        self.assertFalse(os.path.exists(self.app))
        self.assertEqual(self.leftovers(self.app), [])

    def test_failure_keeps_old(self):
        self.assertTrue(self.deploy(self.app_raw()))
        exe = os.path.join(self.app, "Contents", "MacOS", "App")
        v2 = dict(APP, **{"dist/App.app/Contents/MacOS/App": (b"bin2", 0o755)})
        raw = self.app_raw(v2, APP_SCRIPT + "echo boom\nfalse\n")
        self.assertEqual(self.diff(raw), [f"  ~ fetch {self.app} <- {URL} (install changed)"])
        raw[self.bin] = {"url": URL, "install": 'cp "$src" "$out"'}
        self.assertFalse(self.deploy(raw))
        self.assertIn("install script failed (exit 1)", self.output)
        self.assertIn("boom", self.output)
        self.assertEqual(self.read(exe), b"bin")
        self.assertTrue(os.path.isfile(self.bin))  # the other entry still ran
        self.assertEqual(self.leftovers(self.app), [])
        self.assertEqual(self.diff(raw), [f"  ~ fetch {self.app} <- {URL} (install changed)"])

    def test_missing_out_fails_and_keeps_old(self):
        raw = {self.bin: {"url": URL, "install": BIN_SCRIPT}}
        self.assertTrue(self.deploy(raw))
        raw = {self.bin: {"url": URL, "install": 'tar -xzf "$src"\n'}}
        self.assertFalse(self.deploy(raw))
        self.assertIn("did not create $out", self.output)
        self.assertEqual(self.read(self.bin), b"t1")
        self.assertEqual(self.leftovers(self.bin), [])

    def test_missing_out_on_fresh_target(self):
        self.assertFalse(self.deploy({self.bin: {"url": URL, "install": "true"}}))
        self.assertFalse(os.path.exists(self.bin))
        self.assertEqual(self.leftovers(self.bin), [])

    def test_script_change_reruns(self):
        raw = {self.bin: {"url": URL, "install": BIN_SCRIPT}}
        self.assertTrue(self.deploy(raw))
        script = 'tar -xzf "$src"\nmv LICENSE "$out"\n'
        raw = {self.bin: {"url": URL, "install": script}}
        self.assertEqual(self.diff(raw), [f"  ~ fetch {self.bin} <- {URL} (install changed)"])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.read(self.bin), b"l")
        self.assertEqual(self.diff(raw), [])

    def test_sha_change_replaces_directory(self):
        self.deploy(self.app_raw())
        v2 = {"dist/App.app/Contents/MacOS/App": (b"bin2", 0o755)}
        raw = self.app_raw(v2)
        self.assertEqual(self.diff(raw), [f"  ~ fetch {self.app} <- {URL} (sha256 changed)"])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.read(os.path.join(self.app, "Contents", "MacOS", "App")), b"bin2")
        self.assertFalse(os.path.exists(os.path.join(self.app, "Contents", "Info.plist")))
        self.assertFalse(os.path.exists(self.app + ".tools"))
        self.assertEqual(self.leftovers(self.app), [])

    def test_unpinned_update_reruns(self):
        raw = {self.bin: {"url": URL, "install": BIN_SCRIPT}}
        self.deploy(raw)
        self.opener.body = targz({"tool": (b"t2", 0o755)})
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.read(self.bin), b"t1")
        self.assertTrue(self.deploy(raw, update=True))
        self.assertEqual(self.read(self.bin), b"t2")

    def test_plain_entry_switching_to_install(self):
        self.opener.body = b"plain"
        self.deploy({self.bin: URL})
        self.opener.body = targz({"tool": (b"t2", 0o755)})
        raw = {self.bin: {"url": URL, "install": BIN_SCRIPT}}
        self.assertEqual(self.diff(raw), [f"  ~ fetch {self.bin} <- {URL} (install changed)"])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.read(self.bin), b"t2")

    def test_edited_plain_file_switching_to_install_is_moved_aside(self):
        self.opener.body = b"plain"
        self.deploy({self.bin: URL})
        with open(self.bin, "wb") as f:
            f.write(b"edited")
        self.opener.body = targz({"tool": (b"t2", 0o755)})
        raw = {self.bin: {"url": URL, "install": BIN_SCRIPT}}
        self.assertTrue(self.deploy(raw), self.output)
        self.assertEqual(self.read(self.bin), b"t2")
        self.assertEqual(self.read(self.bin + ".tools"), b"edited")

    def test_install_file_moves_to_files(self):
        self.deploy({self.bin: {"url": URL, "install": BIN_SCRIPT}})
        src = os.path.join(self.tmp, "src")
        with open(src, "wb") as f:
            f.write(b"t1")
        moved = secrets_flow.move_between_sections(
            self.state,
            files_entries=[{"source": src, "target": self.bin}],
            config_dir=self.tmp,
        )
        self.assertEqual(moved, [f"{self.bin} (fetch -> files)"])
        self.assertEqual(self.state["files"][self.bin]["hash"], sha(b"t1"))

    def test_read_only_scratch_cleaned_up(self):
        script = APP_SCRIPT + "mkdir -p ro/x\nchmod 0500 ro/x ro\n"
        self.assertTrue(self.deploy(self.app_raw(script=script)), self.output)
        self.assertEqual(self.leftovers(self.app), [])

    def test_edited_result_left_alone(self):
        raw = self.app_raw()
        self.deploy(raw)
        plist = os.path.join(self.app, "Contents", "Info.plist")
        with open(plist, "wb") as f:
            f.write(b"edited")
        self.assertEqual(self.diff(raw), [])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.read(plist), b"edited")
        self.assertEqual(len(self.opener.calls), 1)

    def test_installed_result_never_read(self):
        raw = self.app_raw()
        raw[self.bin] = {"url": URL, "install": 'cp "$src" "$out"', "mode": "0700"}
        self.assertTrue(self.deploy(raw), self.output)
        os.chmod(self.bin, 0o644)
        no_read = mock.patch.object(fetch, "_file_hash", side_effect=AssertionError("read"))
        with no_read:
            self.assertEqual(self.diff(raw), [f"  ~ chmod {self.bin} -> 0o700"])
            self.assertTrue(self.deploy(raw), self.output)
            self.assertEqual(self.diff(raw), [])
            self.assertEqual(self.diff({}), [f"  - remove {self.app}", f"  - remove {self.bin}"])
            self.assertTrue(self.deploy({}), self.output)
        self.assertFalse(os.path.lexists(self.app))
        self.assertFalse(os.path.lexists(self.bin))

    def _hand_installed_app(self):
        # Old result kept alive until the copy exists, so its inode is not reused.
        old = os.path.join(self.tmp, "old")
        os.rename(self.app, old)
        os.makedirs(os.path.join(self.app, "Contents"))
        fetch._force_rmtree(old)
        mine = os.path.join(self.app, "Contents", "mine")
        with open(mine, "wb") as f:
            f.write(b"hand")
        return mine

    def test_hand_installed_copy_not_removed(self):
        self.deploy(self.app_raw())
        mine = self._hand_installed_app()
        self.assertEqual(
            self.diff({}), [f"  ~ keep {self.app} (no longer the dir tools installed; not removed)"]
        )
        self.assertTrue(self.deploy({}), self.output)
        self.assertEqual(self.read(mine), b"hand")

    def test_reused_inode_is_not_the_installed_directory(self):
        self.deploy(self.app_raw())
        record = self.state["fetch"]["installed"][self.app]
        fake = {**record, "ino": record["ino"], "mtime_ns": record["mtime_ns"] + 1}
        if "birthtime" in record:
            fake["birthtime"] = record["birthtime"] + 1
        self.state["fetch"]["installed"][self.app] = fake
        self.assertEqual(
            self.diff({}), [f"  ~ keep {self.app} (no longer the dir tools installed; not removed)"]
        )
        self.assertTrue(self.deploy({}), self.output)
        self.assertTrue(os.path.isdir(self.app))

    def test_directory_without_birthtime_matches_on_mtime(self):
        self.deploy(self.app_raw())
        record = dict(self.state["fetch"]["installed"][self.app])
        record.pop("birthtime", None)
        self.assertTrue(fetch._same_result(self.app, record))
        self.assertFalse(fetch._same_result(self.app, {**record, "mtime_ns": 0}))
        legacy = {k: v for k, v in record.items() if k not in ("dev", "ino", "mtime_ns")}
        self.assertTrue(fetch._same_result(self.app, legacy))

    def test_hand_installed_copy_moved_aside_on_sha_change(self):
        self.deploy(self.app_raw())
        self._hand_installed_app()
        raw = self.app_raw({"dist/App.app/Contents/MacOS/App": (b"bin2", 0o755)})
        self.assertTrue(self.deploy(raw), self.output)
        self.assertEqual(self.read(os.path.join(self.app + ".tools", "Contents", "mine")), b"hand")
        self.assertEqual(self.read(os.path.join(self.app, "Contents", "MacOS", "App")), b"bin2")

    def test_missing_result_reinstalled(self):
        raw = self.app_raw()
        self.deploy(raw)
        fetch._force_rmtree(self.app)
        self.assertEqual(self.diff(raw), [f"  + fetch {self.app} <- {URL} (install)"])
        self.assertTrue(self.deploy(raw))
        self.assertTrue(os.path.isdir(self.app))

    def test_type_change_moved_aside(self):
        raw = self.app_raw()
        self.deploy(raw)
        fetch._force_rmtree(self.app)
        with open(self.app, "wb") as f:
            f.write(b"x")
        self.assertEqual(
            self.diff(raw),
            [
                f"  ~ replace {self.app} <- {URL} (no longer the dir tools installed; "
                f"old file moved aside to {self.app}.tools)"
            ],
        )
        self.assertEqual(
            self.diff({}), [f"  ~ keep {self.app} (no longer the dir tools installed; not removed)"]
        )
        self.assertTrue(self.deploy(raw))
        self.assertTrue(os.path.isdir(self.app))
        self.assertEqual(self.read(self.app + ".tools"), b"x")

    def test_untracked_directory_moved_aside(self):
        os.makedirs(self.app)
        raw = self.app_raw()
        self.assertEqual(
            self.diff(raw),
            [
                f"  ~ replace {self.app} <- {URL} (not tracked by tools; "
                f"old file moved aside to {self.app}.tools)"
            ],
        )
        self.assertTrue(self.deploy(raw))
        self.assertTrue(os.path.isdir(self.app + ".tools"))
        self.assertTrue(os.path.isfile(os.path.join(self.app, "Contents", "Info.plist")))

    def test_directory_without_install_is_an_error(self):
        os.makedirs(self.app)
        self.assertFalse(self.deploy({self.app: URL}))

    def test_removal_when_dropped(self):
        self.deploy(self.app_raw())
        self.assertEqual(self.diff({}), [f"  - remove {self.app}"])
        self.assertTrue(self.deploy({}))
        self.assertFalse(os.path.exists(self.app))
        self.assertEqual(self.state["fetch"]["installed"], {})

    def test_read_only_directory_removed(self):
        self.deploy(self.app_raw(script=APP_SCRIPT + 'chmod 0500 "$out/Contents"\n'))
        self.assertTrue(self.deploy({}), self.output)
        self.assertFalse(os.path.exists(self.app))

    def test_special_file_result_fails(self):
        raw = self.app_raw(script='mkfifo "$out"\n')
        self.assertFalse(self.deploy(raw))
        self.assertIn("must be a regular file or a directory", self.output)
        self.assertFalse(os.path.exists(self.app))
        self.assertEqual(self.leftovers(self.app), [])

    def test_edited_directory_removed_when_dropped(self):
        self.deploy(self.app_raw())
        with open(os.path.join(self.app, "new"), "wb") as f:
            f.write(b"x")
        self.assertTrue(self.deploy({}))
        self.assertFalse(os.path.lexists(self.app))
        self.assertEqual(self.state["fetch"]["installed"], {})

    def test_diff_never_runs_script_or_downloads(self):
        marker = os.path.join(self.tmp, "ran")
        raw = {self.bin: {"url": URL, "install": f'touch "{marker}"\ncp "$src" "$out"\n'}}
        self.assertEqual(self.diff(raw), [f"  + fetch {self.bin} <- {URL} (install)"])
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(self.opener.calls, [])

    def test_restart_units_owed_on_write_only(self):
        raw = {self.bin: {"url": URL, "install": BIN_SCRIPT, "restartUnits": ["launchd:a.b"]}}
        self.assertEqual(
            self.diff(raw), [f"  + fetch {self.bin} <- {URL} (install)", "    notify: launchd:a.b"]
        )
        self.assertTrue(self.deploy(raw))
        self.assertEqual(secrets_flow.pending_restarts(self.state), {"a.b": [f"fetch/{self.bin}"]})
        secrets_flow.settle_restarts(self.state, {"a.b"})
        self.assertTrue(self.deploy(raw))
        self.assertEqual(secrets_flow.pending_restarts(self.state), {})

    def test_restart_units_not_owed_on_failure(self):
        raw = {self.bin: {"url": URL, "install": "false", "restartUnits": ["launchd:a.b"]}}
        self.assertFalse(self.deploy(raw))
        self.assertEqual(secrets_flow.pending_restarts(self.state), {})

    def test_parse_errors(self):
        for bad in ("", "  ", 3, ["x"]):
            _entries, errors = fetch.parse({self.bin: {"url": URL, "install": bad}})
            self.assertEqual(
                errors, [f"fetch.{self.bin}: install must be a non-empty shell script"]
            )
        for bad in ("launchd:x", [1], {"a": 1}):
            _entries, errors = fetch.parse({self.bin: {"url": URL, "restartUnits": bad}})
            self.assertEqual(errors, [f"fetch.{self.bin}: restartUnits must be a list of strings"])
        _entries, errors = fetch.parse({self.bin: {"url": URL, "extract": "x"}})
        self.assertEqual(errors, [f"fetch.{self.bin}: unknown keys extract"])
        entries, _ = fetch.parse({self.bin: {"url": URL, "install": "x"}})
        self.assertIsNone(entries[0].mode)
        entries, _ = fetch.parse({self.bin: URL})
        self.assertEqual(entries[0].mode, fetch.DEFAULT_MODE)


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
