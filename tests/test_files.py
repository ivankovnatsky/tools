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


def _quiet(fn, *args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        return fn(*args), out.getvalue()


class OnChangeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = os.path.realpath(self._tmp.name)
        self.marker = os.path.join(self.d, "ran")
        self.hook = f"echo x >> {self.marker}"

    def tearDown(self):
        self._tmp.cleanup()

    def runs(self):
        if not os.path.exists(self.marker):
            return 0
        with open(self.marker) as f:
            return len(f.read().splitlines())

    def test_file_hook_runs_only_when_written(self):
        from tools.diff import _diff_files

        _write(os.path.join(self.d, "src", "a.conf"), "one\n")
        target = os.path.join(self.d, "out", "a.conf")
        entries = [{"source": "src/a.conf", "target": target, "onChange": self.hook}]
        state = {}
        self.assertIn(f"  ~ run onChange: {self.hook}", _diff_files(entries, self.d, state))
        self.assertEqual(self.runs(), 0)
        (ok, out) = _quiet(files.install_files, entries, self.d, state)
        self.assertTrue(ok)
        self.assertEqual(self.runs(), 1)
        self.assertIn("files: onChange (", out)
        _quiet(files.install_files, entries, self.d, state)
        self.assertEqual(self.runs(), 1)
        self.assertEqual(_diff_files(entries, self.d, state), [])
        _write(os.path.join(self.d, "src", "a.conf"), "two\n")
        _quiet(files.install_files, entries, self.d, state)
        self.assertEqual(self.runs(), 2)

    def test_dir_entry_hook_runs_once(self):
        from tools.diff import _diff_files

        _write(os.path.join(self.d, "tree", ".cfg", "a"), "a\n")
        _write(os.path.join(self.d, "tree", ".cfg", "b"), "b\n")
        entries = [{"dir": "tree", "onChange": self.hook}]
        home = os.path.join(self.d, "home")
        with mock.patch.dict(os.environ, {"HOME": home}):
            rows = _diff_files(entries, self.d, {})
            self.assertEqual(rows.count(f"  ~ run onChange: {self.hook}"), 1)
            (ok, out) = _quiet(files.install_files, entries, self.d, {})
        self.assertTrue(ok)
        self.assertEqual(self.runs(), 1)
        self.assertIn("and 1 more", out)

    def test_failing_hook_fails_deploy_and_is_retried(self):
        from tools.diff import _diff_files

        _write(os.path.join(self.d, "src", "a.conf"), "one\n")
        target = os.path.join(self.d, "a.conf")
        flag = os.path.join(self.d, "flag")
        hook = f"if [ -e {flag} ]; then {self.hook}; else touch {flag}; exit 3; fi"
        entries = [{"source": "src/a.conf", "target": target, "onChange": hook}]
        state = {}
        (ok, out) = _quiet(files.install_files, entries, self.d, state)
        self.assertFalse(ok)
        self.assertIn("onChange failed (exit 3)", out)
        self.assertTrue(os.path.exists(target))
        self.assertIn(target, state["files"])
        self.assertEqual(_diff_files(entries, self.d, state), [f"  ~ run onChange: {hook}"])
        (ok, _out) = _quiet(files.install_files, entries, self.d, state)
        self.assertTrue(ok)
        self.assertEqual(self.runs(), 1)
        self.assertNotIn("onChange", state)
        self.assertEqual(_diff_files(entries, self.d, state), [])

    def test_pending_hook_kept_while_entry_fails_to_resolve(self):
        _write(os.path.join(self.d, "src", "a.conf"), "one\n")
        target = os.path.join(self.d, "a.conf")
        state = {}
        entries = [{"source": "src/a.conf", "target": target, "onChange": "exit 3"}]
        _quiet(files.install_files, entries, self.d, state)
        os.remove(os.path.join(self.d, "src", "a.conf"))
        (ok, _out) = _quiet(files.install_files, entries, self.d, state)
        self.assertFalse(ok)
        self.assertIn("exit 3", state["onChange"]["files"])

    def test_pending_hook_dropped_when_no_longer_declared(self):
        _write(os.path.join(self.d, "src", "a.conf"), "one\n")
        target = os.path.join(self.d, "a.conf")
        state = {}
        entries = [{"source": "src/a.conf", "target": target, "onChange": "exit 3"}]
        _quiet(files.install_files, entries, self.d, state)
        (ok, _out) = _quiet(
            files.install_files, [{"source": "src/a.conf", "target": target}], self.d, state
        )
        self.assertTrue(ok)
        self.assertNotIn("onChange", state)

    def test_invalid_on_change_is_config_error(self):
        _write(os.path.join(self.d, "src", "a.conf"), "one\n")
        for bad in ("", 3, ["x"]):
            entries = [{"source": "src/a.conf", "target": "~/x", "onChange": bad}]
            _resolved, errors = files._resolve_entries(entries, self.d)
            self.assertEqual(len(errors), 1, bad)
            self.assertIn("onChange", errors[0])


class DirectoryEntryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = os.path.realpath(self._tmp.name)
        self.target = os.path.join(self.d, "a", "Screenshots")

    def tearDown(self):
        self._tmp.cleanup()

    def entries(self, **extra):
        return [{"target": self.target, "directory": True, **extra}]

    def test_create_track_chmod_and_remove_when_empty(self):
        from tools.diff import _diff_files

        state = {}
        entries = self.entries(mode="0700")
        self.assertEqual(
            _diff_files(entries, self.d, state), [f"  + create directory {self.target}"]
        )
        (ok, _out) = _quiet(files.install_files, entries, self.d, state)
        self.assertTrue(ok)
        self.assertTrue(os.path.isdir(self.target))
        self.assertEqual(os.stat(self.target).st_mode & 0o777, 0o700)
        self.assertTrue(state["files"][self.target]["dir"])
        self.assertEqual(_diff_files(entries, self.d, state), [])

        os.chmod(self.target, 0o755)
        self.assertEqual(_diff_files(entries, self.d, state), [f"  ~ chmod {self.target} -> 0o700"])
        _quiet(files.install_files, entries, self.d, state)
        self.assertEqual(os.stat(self.target).st_mode & 0o777, 0o700)

        self.assertEqual(_diff_files([], self.d, state), [f"  - remove {self.target}"])
        (ok, _out) = _quiet(files.install_files, [], self.d, state)
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(self.target))
        self.assertEqual(state["files"], {})

    def test_non_empty_directory_is_kept(self):
        from tools.diff import _diff_files

        state = {}
        _quiet(files.install_files, self.entries(), self.d, state)
        _write(os.path.join(self.target, "shot.png"), "x")
        self.assertEqual(
            _diff_files([], self.d, state), [f"  ~ keep {self.target} (not empty; not removed)"]
        )
        (ok, _out) = _quiet(files.install_files, [], self.d, state)
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(os.path.join(self.target, "shot.png")))
        self.assertIn(self.target, state["files"])

    def test_existing_directory_is_not_adopted(self):
        os.makedirs(self.target)
        state = {}
        (ok, _out) = _quiet(files.install_files, self.entries(mode="0700"), self.d, state)
        self.assertTrue(ok)
        self.assertEqual(os.stat(self.target).st_mode & 0o777, 0o700)
        self.assertEqual(state["files"], {})
        _quiet(files.install_files, [], self.d, state)
        self.assertTrue(os.path.isdir(self.target))

    def test_file_or_symlink_at_target_refused(self):
        from tools.diff import _diff_files

        os.makedirs(os.path.dirname(self.target))
        os.symlink(self.d, self.target)
        state = {}
        rows = _diff_files(self.entries(), self.d, state)
        self.assertEqual(rows, [f"  ! {self.target} exists and is not a directory"])
        (ok, _out) = _quiet(files.install_files, self.entries(), self.d, state)
        self.assertFalse(ok)
        self.assertTrue(os.path.islink(self.target))

    def test_hook_runs_on_create_only(self):
        marker = os.path.join(self.d, "ran")
        entries = self.entries(onChange=f"echo x >> {marker}")
        state = {}
        _quiet(files.install_files, entries, self.d, state)
        _quiet(files.install_files, entries, self.d, state)
        with open(marker) as f:
            self.assertEqual(f.read(), "x\n")

    def test_files_inside_a_directory_entry_are_not_conflicts(self):
        from tools.user import secrets_flow

        _write(os.path.join(self.d, "src", "k"), "k\n")
        entries = self.entries(mode="0700") + [
            {"source": "src/k", "target": os.path.join(self.target, "k")}
        ]
        conflicts = secrets_flow.ownership_conflicts(
            files_entries=entries, state={}, config_dir=self.d
        )
        (ok, _out) = _quiet(files.install_files, entries, self.d, {}, conflicts)
        self.assertTrue(ok)
        self.assertTrue(os.path.isfile(os.path.join(self.target, "k")))

    def test_directory_tracked_and_hooked_when_declared_after_its_files(self):
        from tools.diff import _diff_files

        _write(os.path.join(self.d, "src", "k"), "k\n")
        marker = os.path.join(self.d, "ran")
        entries = [{"source": "src/k", "target": os.path.join(self.target, "k")}] + self.entries(
            onChange=f"echo x >> {marker}"
        )
        state = {}
        rows = _diff_files(entries, self.d, state)
        self.assertEqual(rows[0], f"  + create directory {self.target}")
        (ok, _out) = _quiet(files.install_files, entries, self.d, state)
        self.assertTrue(ok)
        self.assertTrue(state["files"][self.target]["dir"])
        self.assertTrue(os.path.exists(marker))

    def test_directory_removed_with_its_files_in_one_deploy(self):
        from tools.diff import _diff_files

        _write(os.path.join(self.d, "src", "k"), "k\n")
        inner = os.path.join(self.target, "inner")
        entries = [
            {"source": "src/k", "target": os.path.join(inner, "k")},
            {"target": inner, "directory": True},
        ] + self.entries()
        state = {}
        _quiet(files.install_files, entries, self.d, state)
        self.assertEqual(len(state["files"]), 3)
        rows = _diff_files([], self.d, state)
        self.assertEqual(
            rows,
            [
                f"  - remove {os.path.join(inner, 'k')}",
                f"  - remove {inner}",
                f"  - remove {self.target}",
            ],
        )
        (ok, _out) = _quiet(files.install_files, [], self.d, state)
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(self.target))
        self.assertEqual(state["files"], {})

    def test_record_survives_device_renumbering(self):
        state = {}
        _quiet(files.install_files, self.entries(), self.d, state)
        state["files"][self.target]["dev"] += 1
        (ok, _out) = _quiet(files.install_files, self.entries(), self.d, state)
        self.assertTrue(ok)
        self.assertIn(self.target, state["files"])
        state["files"][self.target]["dev"] += 1
        _quiet(files.install_files, [], self.d, state)
        self.assertFalse(os.path.exists(self.target))

    def test_mode_inside_secrets_dir_refused(self):
        from tools.fsutil import SECRETS_DIR_MODE
        from tools.user import secrets_flow

        secrets_dir = os.path.join(self.d, "secrets")
        sops_raw = {"secretsDir": secrets_dir}
        target = os.path.join(secrets_dir, "sub")
        for mode, refused in (("0700", True), (oct(SECRETS_DIR_MODE)[2:], False), (None, False)):
            entry = {"target": target, "directory": True}
            if mode:
                entry["mode"] = mode
            conflicts = secrets_flow.ownership_conflicts(
                files_entries=[entry], sops_raw=sops_raw, state={}, config_dir=self.d
            )
            self.assertEqual(files._conflict(target, conflicts) is not None, refused, mode)

    def test_state_only_directory_never_blocks_other_sections(self):
        from tools.diff import _diff_files
        from tools.user import secrets_flow

        _write(os.path.join(self.d, "src", "k"), "k\n")
        state = {}
        _quiet(files.install_files, self.entries(), self.d, state)
        inner = os.path.join(self.target, "k")
        files_only = secrets_flow.ownership_conflicts(
            files_entries=[{"source": "src/k", "target": inner}], state=state, config_dir=self.d
        )
        self.assertIsNone(files._conflict(inner, files_only))

        tmpl = {"t": {"path": inner, "content": "x"}}
        sops_file = os.path.join(self.d, "secrets.yaml")
        _write(sops_file, "ENC")
        sops_raw = {
            "binary": "/fake/sops",
            "secretsDir": os.path.join(self.d, "secrets"),
            "defaultSopsFile": sops_file,
            "secrets": {"s": {"path": inner + "2", "key": "s"}},
        }
        self.assertEqual(
            [x.path for x in secrets_flow.sops.parse_sops(sops_raw).secrets], [inner + "2"]
        )
        for others in ({"templates_raw": tmpl}, {"sops_raw": sops_raw}):
            st = {"files": dict(state["files"])}
            for _deploy in range(2):
                c = secrets_flow.ownership_conflicts(state=st, config_dir=self.d, **others)
                self.assertIsNone(files._conflict(inner, c), others)
                self.assertIsNone(files._conflict(inner + "2", c), others)
                self.assertIsNone(files._conflict(self.target, c), others)
                if _deploy == 0:
                    self.assertEqual(
                        _diff_files([], self.d, st, c),
                        [f"  ~ release {self.target} (another section manages a path inside)"],
                    )
                    _write(inner, "x")
                (ok, _out) = _quiet(files.install_files, [], self.d, st, c)
                self.assertTrue(ok)
                self.assertNotIn(self.target, st["files"])
            self.assertTrue(os.path.isdir(self.target))
            os.remove(inner)

    def test_trailing_slash_symlink_never_followed(self):
        from tools.diff import _diff_files

        real = os.path.join(self.d, "real")
        os.makedirs(real)
        os.chmod(real, 0o755)
        link = os.path.join(self.d, "link")
        os.symlink(real, link)
        entries = [{"target": link + "/", "directory": True, "mode": "0700"}]
        state = {}
        self.assertEqual(
            _diff_files(entries, self.d, state), [f"  ! {link} exists and is not a directory"]
        )
        (ok, _out) = _quiet(files.install_files, entries, self.d, state)
        self.assertFalse(ok)
        self.assertEqual(os.stat(real).st_mode & 0o777, 0o755)

        state = {"files": {link + "/": {"dir": True, "ino": os.stat(real).st_ino}}}
        (ok, _out) = _quiet(files.install_files, [], self.d, state)
        self.assertTrue(os.path.isdir(real))
        self.assertTrue(os.path.islink(link))

    def test_directory_inside_secrets_dir_is_private(self):
        from tools.fsutil import SECRETS_DIR_MODE

        secrets_dir = os.path.join(self.d, "secrets")
        target = os.path.join(secrets_dir, "sub")
        state = {}
        (ok, _out) = _quiet(
            files.install_files,
            [{"target": target, "directory": True}],
            self.d,
            state,
            None,
            secrets_dir,
        )
        self.assertTrue(ok)
        for d in (secrets_dir, target):
            self.assertEqual(os.stat(d).st_mode & 0o777, SECRETS_DIR_MODE)
        self.assertEqual(state["sops"]["createdDirs"], [secrets_dir, target])
        self.assertTrue(state["files"][target]["dir"])

    def test_malformed_directory_entries(self):
        for entry in (
            {"directory": True},
            {"directory": True, "target": "~/x", "source": "y"},
            {"directory": "yes", "target": "~/x"},
        ):
            _resolved, errors = files._resolve_entries([entry], self.d)
            self.assertEqual(len(errors), 1, entry)
