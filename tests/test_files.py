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


def _replace_row(path, reason):
    return f"  ~ replace {path} ({reason}; old file moved aside to {path}.tools; content not shown)"


def _backups(path):
    d, base = os.path.split(path)
    return sorted(os.path.join(d, n) for n in os.listdir(d) if n == base + ".tools")


def _read(path):
    with open(path) as f:
        return f.read()


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
            self.assertEqual(rows, [_replace_row(target, "not tracked by tools")])
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
            self.assertEqual(rows, [_replace_row(target, "not tracked by tools")])
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


class OnUpdateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = os.path.realpath(self._tmp.name)
        self.marker = os.path.join(self.d, "ran")
        self.hook = f"echo x >> {self.marker}"
        _write(os.path.join(self.d, "src", "a.conf"), "one\n")
        self.target = os.path.join(self.d, "out", "a.conf")

    def tearDown(self):
        self._tmp.cleanup()

    def runs(self):
        if not os.path.exists(self.marker):
            return 0
        with open(self.marker) as f:
            return len(f.read().splitlines())

    def install(self, entries, state, update):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = files.install_files(entries, self.d, state, None, None, update)
        return ok, out.getvalue()

    def test_runs_only_with_update_changed_or_not(self):
        from tools.diff import _diff_files

        entries = [{"source": "src/a.conf", "target": self.target, "onUpdate": self.hook}]
        state = {}
        self.assertEqual([r for r in _diff_files(entries, self.d, state) if "onUpdate" in r], [])
        (ok, _out) = self.install(entries, state, False)
        self.assertTrue(ok)
        self.assertEqual(self.runs(), 0)
        self.assertEqual(_diff_files(entries, self.d, state), [])
        (ok, out) = self.install(entries, state, True)
        self.assertTrue(ok)
        self.assertEqual(self.runs(), 1)
        self.assertIn("files: onUpdate (", out)
        self.install(entries, state, True)
        self.assertEqual(self.runs(), 2)
        self.install(entries, state, False)
        self.assertEqual(self.runs(), 2)

    def test_explicit_entry_overrides_dir_entry(self):
        _write(os.path.join(self.d, "tree", ".cfg", "a"), "a\n")
        home = os.path.join(self.d, "home")
        entries = [
            {"dir": "tree", "onUpdate": "exit 9"},
            {"source": "tree/.cfg/a", "target": "~/.cfg/a", "onUpdate": self.hook},
        ]
        with mock.patch.dict(os.environ, {"HOME": home}):
            (ok, _out) = self.install(entries, {}, True)
        self.assertTrue(ok)
        self.assertEqual(self.runs(), 1)

    def test_failure_fails_deploy_and_is_not_pending(self):
        from tools.diff import _diff_files

        entries = [{"source": "src/a.conf", "target": self.target, "onUpdate": "exit 3"}]
        state = {}
        (ok, out) = self.install(entries, state, True)
        self.assertFalse(ok)
        self.assertIn("onUpdate failed (exit 3)", out)
        self.assertTrue(os.path.exists(self.target))
        self.assertNotIn("onChange", state)
        self.assertEqual(_diff_files(entries, self.d, state), [])
        (ok, _out) = self.install(entries, state, False)
        self.assertTrue(ok)

    def install_with(self, entries, conflicts):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = files.install_files(entries, self.d, {}, conflicts, None, True)
        return ok, out.getvalue()

    def test_skipped_when_target_refused(self):
        entries = [{"source": "src/a.conf", "target": self.target, "onUpdate": self.hook}]
        (ok, out) = self.install_with(entries, {self.target: "owned by templates"})
        self.assertFalse(ok)
        self.assertEqual(self.runs(), 0)
        self.assertIn("onUpdate skipped", out)

    def test_skipped_when_any_entry_failed_to_write(self):
        os.makedirs(self.target)
        good = os.path.join(self.d, "out", "b.conf")
        entries = [
            {"source": "src/a.conf", "target": good, "onUpdate": self.hook},
            {"source": "src/a.conf", "target": self.target, "onUpdate": self.hook},
        ]
        (ok, out) = self.install(entries, {}, True)
        self.assertFalse(ok)
        self.assertTrue(os.path.exists(good))
        self.assertEqual(self.runs(), 0)
        self.assertIn("onUpdate skipped", out)

    def test_skipped_when_on_change_failed(self):
        entries = [
            {"source": "src/a.conf", "target": self.target, "onChange": "exit 3"},
            {
                "source": "src/a.conf",
                "target": os.path.join(self.d, "out", "b.conf"),
                "onUpdate": self.hook,
            },
        ]
        (ok, out) = self.install(entries, {}, True)
        self.assertFalse(ok)
        self.assertEqual(self.runs(), 0)
        self.assertIn("onUpdate skipped", out)

    def test_skipped_when_an_entry_fails_to_resolve(self):
        entries = [
            {"source": "src/missing", "target": self.target, "onUpdate": self.hook},
            {
                "source": "src/a.conf",
                "target": os.path.join(self.d, "out", "b.conf"),
                "onUpdate": self.hook,
            },
        ]
        (ok, out) = self.install(entries, {}, True)
        self.assertFalse(ok)
        self.assertEqual(self.runs(), 0)
        self.assertIn("onUpdate skipped", out)

    def test_unrelated_failure_does_not_skip(self):
        os.makedirs(self.target)
        entries = [
            {"source": "src/a.conf", "target": self.target},
            {
                "source": "src/a.conf",
                "target": os.path.join(self.d, "out", "b.conf"),
                "onUpdate": self.hook,
            },
        ]
        (ok, _out) = self.install(entries, {}, True)
        self.assertFalse(ok)
        self.assertEqual(self.runs(), 1)

    def test_invalid_on_update_is_config_error(self):
        for bad in ("", 3, ["x"]):
            entries = [{"source": "src/a.conf", "target": "~/x", "onUpdate": bad}]
            _resolved, errors = files._resolve_entries(entries, self.d)
            self.assertEqual(len(errors), 1, bad)
            self.assertIn("onUpdate", errors[0])


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


class MoveAsideTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = os.path.realpath(self._tmp.name)
        _write(os.path.join(self.d, "src"), "fresh = 1\n")
        self.target = os.path.join(self.d, "out", "app.ini")
        self.state = {}

    def tearDown(self):
        self._tmp.cleanup()

    def entries(self, **extra):
        return [{"source": "src", "target": self.target, **extra}]

    def deploy(self, entries):
        with mock.patch("tools.user.files.log") as log:
            ok = files.install_files(entries, self.d, self.state)
        return ok, repr(log.call_args_list)

    def diff(self, entries):
        from tools.diff import _diff_files

        return _diff_files(entries, self.d, self.state)

    def test_untracked_differing_target_moved_aside(self):
        _write(self.target, "mine\n", 0o600)
        self.assertEqual(
            self.diff(self.entries()), [_replace_row(self.target, "not tracked by tools")]
        )
        ok, log = self.deploy(self.entries())
        self.assertTrue(ok)
        [backup] = _backups(self.target)
        self.assertEqual(backup, self.target + ".tools")
        self.assertIn(backup, log)
        self.assertEqual(_read(backup), "mine\n")
        self.assertEqual(os.stat(backup).st_mode & 0o777, 0o600)
        self.assertEqual(_read(self.target), "fresh = 1\n")
        self.assertEqual(self.diff(self.entries()), [])

    def test_untracked_identical_target_adopted_without_backup(self):
        _write(self.target, "fresh = 1\n")
        self.assertTrue(self.deploy(self.entries())[0])
        self.assertEqual(_backups(self.target), [])
        self.assertIn(self.target, self.state["files"])

    def test_tracked_unchanged_target_replaced_without_backup(self):
        self.assertTrue(self.deploy(self.entries())[0])
        _write(os.path.join(self.d, "src"), "fresh = 2\n")
        self.assertEqual(self.diff(self.entries())[0], f"  ~ update {self.target}")
        self.assertTrue(self.deploy(self.entries())[0])
        self.assertEqual(_read(self.target), "fresh = 2\n")
        self.assertEqual(_backups(self.target), [])

    def test_tracked_hand_edited_target_moved_aside(self):
        self.assertTrue(self.deploy(self.entries())[0])
        with open(self.target, "w") as f:
            f.write("edited\n")
        self.assertEqual(
            self.diff(self.entries()),
            [_replace_row(self.target, "modified since tools wrote it")],
        )
        self.assertTrue(self.deploy(self.entries())[0])
        [backup] = _backups(self.target)
        self.assertEqual(_read(backup), "edited\n")
        self.assertEqual(_read(self.target), "fresh = 1\n")

    def test_symlink_target_renamed_not_followed(self):
        pointee = os.path.join(self.d, "pointee")
        _write(pointee, "theirs\n")
        os.makedirs(os.path.dirname(self.target))
        os.symlink(pointee, self.target)
        self.assertEqual(self.diff(self.entries()), [_replace_row(self.target, "a symlink")])
        self.assertTrue(self.deploy(self.entries())[0])
        self.assertFalse(os.path.islink(self.target))
        [backup] = _backups(self.target)
        self.assertEqual(os.readlink(backup), pointee)
        self.assertEqual(_read(pointee), "theirs\n")

    def test_second_drift_refused_until_backup_removed(self):
        _write(self.target, "one\n")
        self.assertTrue(self.deploy(self.entries())[0])
        [backup] = _backups(self.target)
        _write(self.target, "two\n")
        _write(os.path.join(self.d, "other-src"), "x\n")
        other = os.path.join(self.d, "out", "other.ini")
        entries = self.entries() + [{"source": "other-src", "target": other}]
        rows = self.diff(entries)
        self.assertEqual(rows[0][:4], "  ! ")
        self.assertIn(f"changed again; an earlier backup {backup} exists", rows[0])
        ok, log = self.deploy(entries)
        self.assertFalse(ok)
        self.assertIn("changed again", log)
        self.assertEqual(_read(self.target), "two\n")
        self.assertEqual(_backups(self.target), [backup])
        self.assertEqual(_read(other), "x\n")
        os.remove(backup)
        self.assertTrue(self.deploy(entries)[0])
        [again] = _backups(self.target)
        self.assertEqual(_read(again), "two\n")
        self.assertEqual(_read(self.target), "fresh = 1\n")

    def test_unrelated_backup_names_ignored(self):
        _write(self.target + ".tools-20260101-000000", "x\n")
        _write(self.target + "x.tools", "x\n")
        _write(self.target, "mine\n")
        self.assertTrue(self.deploy(self.entries())[0])
        self.assertEqual(_read(self.target), "fresh = 1\n")

    def test_force_key_is_an_error(self):
        ok, log = self.deploy(self.entries(force=True))
        self.assertFalse(ok)
        self.assertIn("tools now always moves the old file aside", log)
        self.assertFalse(os.path.exists(self.target))

    def test_unknown_key_is_an_error(self):
        ok, log = self.deploy(self.entries(skp=True))
        self.assertFalse(ok)
        self.assertIn("unknown keys ['skp']", log)
        self.assertFalse(os.path.exists(self.target))

    def test_once_key_is_unknown(self):
        _, errors = files._resolve_entries(self.entries(once=True), self.d)
        self.assertIn("unknown keys ['once']", errors[0])

    def _appearing(self, content):
        real = files.atomic_write

        def write(path, *args, **kwargs):
            if kwargs.get("exclusive") and not os.path.lexists(path):
                _write(path, content)
            return real(path, *args, **kwargs)

        return mock.patch.object(files, "atomic_write", side_effect=write)

    def test_target_appearing_after_plan_is_moved_aside(self):
        with self._appearing("raced\n"):
            self.assertTrue(self.deploy(self.entries())[0])
        self.assertEqual(_read(self.target), "fresh = 1\n")
        [backup] = _backups(self.target)
        self.assertEqual(_read(backup), "raced\n")


class SkipTest(MoveAsideTest):
    def test_skipped_entry_is_not_written_and_keeps_its_record(self):
        self.assertTrue(self.deploy(self.entries())[0])
        record = dict(self.state["files"][self.target])
        _write(self.target, "app rewrote this\n")
        _write(os.path.join(self.d, "src"), "fresh = 2\n")
        skipped = self.entries(skip=True, onChange="false", onUpdate="false")

        self.assertEqual(self.diff(skipped), [f"  = skip {self.target} (skip: true; not written)"])
        with mock.patch("tools.user.files.log") as log:
            ok = files.install_files(skipped, self.d, self.state, update=True)
        self.assertTrue(ok)
        self.assertIn("skipped", repr(log.call_args_list))
        self.assertEqual(_read(self.target), "app rewrote this\n")
        self.assertEqual(_backups(self.target), [])
        self.assertEqual(self.state["files"][self.target], record)

        # Flipped off: the edited target is moved aside once, as before.
        self.assertEqual(
            self.diff(self.entries()), [_replace_row(self.target, "modified since tools wrote it")]
        )

    def test_skipped_untracked_target_is_not_tracked(self):
        _write(self.target, "mine\n")
        self.assertTrue(self.deploy(self.entries(skip=True))[0])
        self.assertEqual(_read(self.target), "mine\n")
        self.assertNotIn(self.target, self.state.get("files", {}))

    def test_skip_on_dir_entry_is_a_config_error(self):
        _write(os.path.join(self.d, "tree", "a.conf"), "a\n")
        entries = [{"dir": "tree", "skip": True}]
        ok, _ = self.deploy(entries)
        self.assertFalse(ok)
        self.assertIn("not dir: trees", self.diff(entries)[0])

    def test_non_bool_skip_is_a_config_error(self):
        ok, _ = self.deploy(self.entries(skip="yes"))
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(self.target))
        self.assertIn("`skip` must be a bool", self.diff(self.entries(skip="yes"))[0])

    def test_pending_hook_of_skipped_entry_neither_runs_nor_is_dropped(self):
        marker = os.path.join(self.d, "ran")
        hook = f"touch {marker}"
        self.state = {"onChange": {"files": {hook: [self.target]}}}
        skipped = self.entries(skip=True, onChange=hook)
        self.assertEqual(self.diff(skipped), [f"  = skip {self.target} (skip: true; not written)"])
        self.assertTrue(self.deploy(skipped)[0])
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(self.state["onChange"], {"files": {hook: [self.target]}})

        self.assertTrue(self.deploy(self.entries(onChange=hook))[0])
        self.assertTrue(os.path.exists(marker))
        self.assertNotIn("onChange", self.state)

    def test_only_skipped_rows_do_not_count_as_changes(self):
        from tools.diff import _print_sections

        with mock.patch("tools.diff.log"):
            self.assertTrue(_print_sections([("files", ["  = skip /x (skip: true; not written)"])]))
            self.assertFalse(
                _print_sections([("files", ["  = skip /x (skip: true)", "  + create /y"])])
            )


class BackupFalseTest(MoveAsideTest):
    def test_modified_target_overwritten_without_backup(self):
        self.assertTrue(self.deploy(self.entries(backup=False))[0])
        _write(self.target, "app rewrote this\n")
        self.assertEqual(
            self.diff(self.entries(backup=False)),
            [
                f"  ~ replace {self.target} (modified since tools wrote it; "
                "overwritten, backup: false; content not shown)"
            ],
        )
        ok, log = self.deploy(self.entries(backup=False))
        self.assertTrue(ok)
        self.assertIn("backup: false", log)
        self.assertEqual(_read(self.target), "fresh = 1\n")
        self.assertEqual(_backups(self.target), [])

    def test_earlier_backup_does_not_block(self):
        _write(self.target + ".tools", "old backup\n")
        _write(self.target, "mine\n")
        self.assertEqual(self.diff(self.entries(backup=False))[0][:4], "  ~ ")
        self.assertTrue(self.deploy(self.entries(backup=False))[0])
        self.assertEqual(_read(self.target), "fresh = 1\n")
        self.assertEqual(_read(self.target + ".tools"), "old backup\n")

    def test_dir_tree_propagates_backup_false(self):
        _write(os.path.join(self.d, "tree", "a.conf"), "a\n")
        with mock.patch.dict(os.environ, {"HOME": os.path.join(self.d, "home")}):
            target = os.path.join(self.d, "home", "a.conf")
            _write(target, "mine\n")
            self.assertTrue(self.deploy([{"dir": "tree", "backup": False}])[0])
            self.assertEqual(_read(target), "a\n")
            self.assertEqual(_backups(target), [])

    def test_non_bool_backup_is_a_config_error(self):
        ok, _ = self.deploy(self.entries(backup="no"))
        self.assertFalse(ok)
        self.assertIn("`backup` must be a bool", self.diff(self.entries(backup="no"))[0])

    def test_backup_on_directory_entry_is_a_config_error(self):
        entries = [{"target": os.path.join(self.d, "made"), "directory": True, "backup": False}]
        ok, _ = self.deploy(entries)
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(os.path.join(self.d, "made")))
