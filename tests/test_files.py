import contextlib
import hashlib
import io
import os
import tempfile
import unittest
from unittest import mock

from tools.user import files


class ParseModeTest(unittest.TestCase):
    def test_quoted_octal_string_accepted(self):
        self.assertEqual(files._parse_mode("0644"), 0o644)
        self.assertEqual(files._parse_mode("600"), 0o600)
        self.assertIsNone(files._parse_mode(None))

    def test_bare_int_rejected_as_ambiguous(self):
        # YAML 1.1 parses `0644` as octal into 420, indistinguishable from a
        # literal 420 — either way one of them silently gets the wrong bits.
        with self.assertRaises(TypeError):
            files._parse_mode(644)
        with self.assertRaises(TypeError):
            files._parse_mode(True)


def _write(path, content, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, mode)
    return hashlib.sha256(content.encode()).hexdigest()


class FilesCleanupTest(unittest.TestCase):
    def test_unmodified_managed_file_is_removed(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "managed.conf")
            digest = _write(target, "ours\n")
            state = {"files": {target: {"hash": digest}}}

            files.install_files([], d, state)

            self.assertFalse(os.path.exists(target))
            self.assertNotIn(target, state["files"])

    def test_locally_modified_file_is_kept(self):
        # The stored hash is the only evidence of who last wrote the file;
        # a mismatch means it holds work this tool never authored.
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "managed.conf")
            digest = _write(target, "ours\n")
            _write(target, "hand edited\n")
            state = {"files": {target: {"hash": digest}}}

            files.install_files([], d, state)

            self.assertTrue(os.path.exists(target))
            with open(target) as f:
                self.assertEqual(f.read(), "hand edited\n")
            self.assertIn(target, state["files"])

    def test_failed_deletion_stays_tracked_for_retry(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "managed.conf")
            digest = _write(target, "ours\n")
            state = {"files": {target: {"hash": digest}}}

            with mock.patch.object(files.os, "remove", side_effect=OSError("denied")):
                files.install_files([], d, state)

            self.assertIn(target, state["files"])

    def test_managed_file_is_written_and_tracked(self):
        with tempfile.TemporaryDirectory() as d:
            source = os.path.join(d, "src", "app.conf")
            _write(source, "hello\n")
            target = os.path.join(d, "out", "app.conf")

            state = {}
            files.install_files([{"source": "src/app.conf", "target": target}], d, state)

            with open(target) as f:
                self.assertEqual(f.read(), "hello\n")
            self.assertIn(target, state["files"])

    def test_untracked_file_is_never_removed(self):
        with tempfile.TemporaryDirectory() as d:
            stray = os.path.join(d, "not-ours.conf")
            _write(stray, "someone else\n")

            files.install_files([], d, {"files": {}})

            self.assertTrue(os.path.exists(stray))


class FilesDiffRemovalHoldTest(unittest.TestCase):
    def test_malformed_entry_holds_removal_in_diff_like_deploy(self):
        from tools.diff import _diff_files

        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "out", "tracked")
            h = _write(target, "x")
            state = {"files": {target: {"hash": h}}}
            entries = ["not-a-mapping", {"source": "missing", "target": "/nowhere"}]
            rows = _diff_files(entries, d, state)
            self.assertFalse(any(r.startswith("  - remove") for r in rows), rows)
            self.assertTrue(any(r.startswith("  !") for r in rows), rows)
            with mock.patch("tools.user.files.log"):
                self.assertFalse(files.install_files(entries, d, state))
            self.assertTrue(os.path.exists(target))
            self.assertEqual(_diff_files([], d, state), [f"  - remove {target}"])


class FilesRefusalTest(unittest.TestCase):
    def test_refused_target_does_not_block_unrelated_removal(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "src"), "new")
            refused = os.path.join(d, "out", "refused")
            h_refused = _write(refused, "mine")
            stale = os.path.join(d, "out", "stale")
            h_stale = _write(stale, "old")
            state = {"files": {refused: {"hash": h_refused}, stale: {"hash": h_stale}}}
            conflicts = {refused: "claimed by files and templates"}
            entries = [{"source": "src", "target": refused}]
            with mock.patch("tools.user.files.log"):
                self.assertFalse(files.install_files(entries, d, state, conflicts))
            self.assertFalse(os.path.exists(stale))
            self.assertNotIn(stale, state["files"])
            self.assertEqual(state["files"][refused], {"hash": h_refused})
            with open(refused) as f:
                self.assertEqual(f.read(), "mine")

            from tools.diff import _diff_files

            state["files"][stale] = {"hash": h_stale}
            _write(stale, "old")
            rows = _diff_files(entries, d, state, conflicts)
            self.assertIn(f"  - remove {stale}", rows)
            self.assertNotIn(f"  - remove {refused}", rows)

    def test_unreadable_source_keeps_tracked_target(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "out", "t")
            h = _write(target, "ours")
            state = {"files": {target: {"hash": h}}}
            src = os.path.join(d, "src")
            _write(src, "x")
            entries = [{"source": "src", "target": target}]
            with (
                mock.patch("tools.user.files.log"),
                mock.patch("builtins.open", side_effect=OSError("boom")),
            ):
                self.assertFalse(files.install_files(entries, d, state))
            self.assertTrue(os.path.exists(target))
            self.assertEqual(state["files"][target], {"hash": h})


class FilesManagedLinkRemovalTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = os.path.realpath(self._tmp.name)
        self.repo = os.path.join(self.d, "repo")
        self.repo_file = os.path.join(self.repo, "init.lua")
        h = _write(self.repo_file, "x")
        self.link = os.path.join(self.d, "cfg")
        os.symlink(self.repo, self.link)
        self.tracked = os.path.join(self.link, "init.lua")
        self.state = {
            "files": {self.tracked: {"hash": h}},
            "links": {"installed": {self.link: {"source": self.repo}}},
        }

    def tearDown(self):
        self._tmp.cleanup()

    def test_stale_target_under_managed_link_not_deleted_through_it(self):
        from tools.diff import _diff_files
        from tools.user import secrets_flow

        c = secrets_flow.ownership_conflicts(
            files_entries=[], links_raw=[], state=self.state, config_dir=self.d
        )
        rows = _diff_files([], self.d, self.state, c)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].startswith(f"  ~ keep {self.tracked} (is under"), rows)
        with mock.patch("tools.user.files.log"):
            self.assertTrue(files.install_files([], self.d, self.state, c))
        self.assertTrue(os.path.exists(self.repo_file))
        self.assertIn(self.tracked, self.state["files"])


class FilesUntrackedContentTest(unittest.TestCase):
    def test_untracked_existing_target_content_never_shown(self):
        from tools.diff import _diff_files

        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "src"), "fresh = 1\n")
            target = os.path.join(d, "out", "app.ini")
            _write(target, "INTERNAL_TOKEN = handwrittenvalue\n")
            entries = [{"source": "src", "target": target}]
            rows = _diff_files(entries, d, {})
            self.assertEqual(
                rows, [f"  ~ replace {target} (not tracked by tools; content not shown)"]
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch("tools.user.files.log") as log:
                files.install_files(entries, d, {})
            self.assertNotIn("handwrittenvalue", out.getvalue() + repr(log.call_args_list))


class FilesFailedWriteTrackingTest(unittest.TestCase):
    def test_failed_write_does_not_track_untracked_target(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "src"), "new")
            target = os.path.join(d, "out", "hand.conf")
            _write(target, "handwritten")
            state = {}
            entries = [{"source": "src", "target": target}]
            with (
                mock.patch("tools.user.files.log"),
                mock.patch.object(files, "_copy_file", return_value=(False, "h")),
            ):
                self.assertFalse(files.install_files(entries, d, state))
            self.assertNotIn(target, state.get("files", {}))


class FilesRespellTest(unittest.TestCase):
    def test_respelled_target_keeps_file_and_single_entry(self):
        from tools import fsutil
        from tools.diff import _diff_files

        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            _write(os.path.join(d, "src"), "v")
            real = os.path.join(d, "Conf")
            os.makedirs(real)
            os.symlink(real, os.path.join(d, "alias"))
            pairs = [(os.path.join(real, "a"), os.path.join(d, "alias", "a"))]
            if fsutil._case_insensitive(real):
                pairs.append((os.path.join(real, "b"), os.path.join(d, "conf", "B")))
            for first, second in pairs:
                state = {}
                with mock.patch("tools.user.files.log"):
                    files.install_files([{"source": "src", "target": first}], d, state)
                    entries = [{"source": "src", "target": second}]
                    self.assertEqual(_diff_files(entries, d, state), [], (first, second))
                    self.assertTrue(files.install_files(entries, d, state))
                self.assertTrue(os.path.exists(first), (first, second))
                self.assertEqual(list(state["files"]), [second])


class FilesIdentityTest(unittest.TestCase):
    def test_repointed_parent_symlink_is_untracked(self):
        from tools.diff import _diff_files

        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            _write(os.path.join(d, "src"), "fresh = 1\n")
            a, b = os.path.join(d, "a"), os.path.join(d, "b")
            os.makedirs(a)
            os.makedirs(b)
            link = os.path.join(d, "link")
            os.symlink(a, link)
            target = os.path.join(link, "app.ini")
            entries = [{"source": "src", "target": target}]
            state = {}
            with mock.patch("tools.user.files.log"):
                files.install_files(entries, d, state)
            self.assertEqual(state["files"][target]["ino"], os.lstat(target).st_ino)
            os.remove(link)
            os.symlink(b, link)
            _write(os.path.join(b, "app.ini"), "fresh = 1\nhandwrittenvalue = 2\n")
            rows = _diff_files(entries, d, state)
            self.assertEqual(
                rows, [f"  ~ replace {target} (not tracked by tools; content not shown)"]
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch("tools.user.files.log") as log:
                files.install_files(entries, d, state)
            self.assertNotIn("handwrittenvalue", out.getvalue() + repr(log.call_args_list))

    def test_identity_kept_for_unchanged_file(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "src"), "v")
            target = os.path.join(d, "out")
            entries = [{"source": "src", "target": target}]
            state = {}
            with mock.patch("tools.user.files.log"):
                files.install_files(entries, d, state)
                files.install_files(entries, d, state)
            self.assertEqual(state["files"][target]["ino"], os.lstat(target).st_ino)

    def test_changed_device_number_keeps_tracking(self):
        from tools.diff import _diff_files

        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "src"), "v = 1\n")
            target = os.path.join(d, "out")
            entries = [{"source": "src", "target": target}]
            state = {}
            with mock.patch("tools.user.files.log"):
                files.install_files(entries, d, state)
            dev = state["files"][target]["dev"]
            state["files"][target]["dev"] = dev + 1
            _write(os.path.join(d, "src"), "v = 2\n")
            rows = _diff_files(entries, d, state)
            self.assertEqual(rows[0], f"  ~ update {target}")
            with mock.patch("tools.user.files.log"):
                files.install_files(entries, d, state)
            self.assertEqual(state["files"][target]["dev"], dev)

    def test_modified_kept_file_is_shown_in_diff(self):
        from tools.diff import _diff_files

        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "managed.conf")
            digest = _write(target, "ours\n")
            _write(target, "hand edited\n")
            state = {"files": {target: {"hash": digest}}}
            rows = _diff_files([], d, state)
            self.assertEqual(
                rows, [f"  ~ keep {target} (modified since tools wrote it; not removed)"]
            )


if __name__ == "__main__":
    unittest.main()
