import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from tools import cli, diff, privilege
from tools.user import files


def _write(path, content, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, mode)


def _read(path):
    with open(path) as f:
        return f.read()


def _quiet(fn, *args, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        result = fn(*args, **kwargs)
    return result, out.getvalue()


class SystemFilesBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = os.path.realpath(self._tmp.name)
        self.config_dir = os.path.join(self.tmp, "config")
        self.etc = os.path.join(self.tmp, "etc")
        os.makedirs(self.etc)
        _write(os.path.join(self.config_dir, "etc/shells"), "/bin/sh\n")
        self.target = os.path.join(self.etc, "shells")
        self.entries = [{"source": "etc/shells", "target": self.target, "mode": "0644"}]
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        for patch in (
            mock.patch.object(privilege, "UNDER_SUDO", False),
            mock.patch.dict(os.environ, {"HOME": self.home}),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def as_root(self, why=None):
        """Root's target check with the tmp dir standing in for a root-only /etc."""
        return mock.patch.object(files.privilege, "root_dir_error", return_value=why)


class ResolveTest(SystemFilesBase):
    def test_system_entries_resolve_only_for_root(self):
        user, errors = files._resolve_entries(self.entries, self.config_dir)
        self.assertEqual((user, errors), ([], []))
        root, errors = files._resolve_entries(self.entries, self.config_dir, system=True)
        self.assertEqual(errors, [])
        self.assertEqual([e.target for e in root], [self.target])
        self.assertEqual(root[0].mode, 0o644)

    def test_targets_under_home_are_user_files(self):
        for target in ("~/u", os.path.join(self.home, "u")):
            with self.subTest(target=target):
                entry = {"source": "etc/shells", "target": target}
                self.assertEqual(
                    files._resolve_entries([entry], self.config_dir, system=True), ([], [])
                )
                user, errors = files._resolve_entries([entry], self.config_dir)
                self.assertEqual(errors, [])
                self.assertEqual([e.target for e in user], [os.path.join(self.home, "u")])

    def test_invalid_system_entries_are_errors_in_both_runs(self):
        source = {"source": "etc/shells"}
        bad = {
            "onChange": {**source, "target": self.target, "onChange": "true"},
            "onUpdate": {**source, "target": self.target, "onUpdate": "true"},
            "directory": {"target": self.etc, "directory": True},
        }
        for case, entry in bad.items():
            for system in (False, True):
                with self.subTest(case=case, system=system):
                    resolved, errors = files._resolve_entries([entry], self.config_dir, system)
                    self.assertEqual(resolved, [])
                    self.assertEqual(len(errors), 1, errors)
                    self.assertIn("a system entry takes only", errors[0])

    def test_system_key_is_an_error(self):
        for value in (True, False):
            entry = {**self.entries[0], "system": value}
            resolved, errors = files._resolve_entries([entry], self.config_dir, system=True)
            self.assertEqual(resolved, [])
            self.assertEqual(len(errors), 1)
            self.assertIn("`system` is not needed", errors[0])


class RootDeployTest(SystemFilesBase):
    def deploy(self, state, entries=None):
        with self.as_root():
            return _quiet(
                files.install_files,
                self.entries if entries is None else entries,
                self.config_dir,
                state,
                system=True,
            )

    def test_creates_and_tracks(self):
        state = {}
        ok, _ = self.deploy(state)
        self.assertTrue(ok)
        self.assertEqual(_read(self.target), "/bin/sh\n")
        self.assertEqual(os.stat(self.target).st_mode & 0o7777, 0o644)
        self.assertIn(self.target, state["files"])

    def test_symlink_is_moved_aside_once(self):
        store = os.path.join(self.tmp, "store-shells")
        _write(store, "/old/shell\n", 0o444)
        os.symlink(store, self.target)
        state = {}
        ok, out = self.deploy(state)
        self.assertTrue(ok, out)
        self.assertFalse(os.path.islink(self.target))
        self.assertEqual(_read(self.target), "/bin/sh\n")
        self.assertEqual(os.readlink(self.target + ".tools"), store)
        self.assertEqual(_read(store), "/old/shell\n")

        _write(self.target, "hand edit\n")
        ok, out = self.deploy(state)
        self.assertFalse(ok)
        self.assertIn("earlier backup", out)
        self.assertEqual(_read(self.target), "hand edit\n")

    def test_same_content_not_owned_by_root_is_replaced(self):
        _write(self.target, "/bin/sh\n")
        with self.as_root():
            rows, _ = _quiet(diff._diff_files, self.entries, self.config_dir, {}, system=True)
        self.assertEqual(len(rows), 1)
        self.assertIn(f"~ replace {self.target} (not owned by uid 0;", rows[0])
        state = {}
        ok, out = self.deploy(state)
        self.assertTrue(ok, out)
        self.assertTrue(os.path.exists(self.target + ".tools"))
        self.assertIn(self.target, state["files"])

    def test_same_content_owned_by_root_is_unchanged(self):
        _write(self.target, "/bin/sh\n")
        with self.as_root(), mock.patch.object(files, "_uid", return_value=0):
            rows, _ = _quiet(diff._diff_files, self.entries, self.config_dir, {}, system=True)
            ok, _ = _quiet(files.install_files, self.entries, self.config_dir, {}, system=True)
        self.assertEqual(rows, [])
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(self.target + ".tools"))

    def test_user_run_ignores_ownership(self):
        target = os.path.join(self.home, "u.conf")
        _write(target, "/bin/sh\n")
        entry = {"source": "etc/shells", "target": target, "mode": "0644"}
        rows, _ = _quiet(diff._diff_files, [entry], self.config_dir, {})
        self.assertEqual(rows, [])

    def test_dropped_entry_is_removed(self):
        state = {}
        self.deploy(state)
        ok, _ = self.deploy(state, entries=[])
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(self.target))
        self.assertEqual(state["files"], {})

    def test_unsafe_directory_is_refused(self):
        state = {}
        ok, out = _quiet(files.install_files, self.entries, self.config_dir, state, system=True)
        self.assertFalse(ok)
        self.assertIn("not root-only", out)
        self.assertFalse(os.path.exists(self.target))

    def test_user_entries_are_left_alone(self):
        user_target = os.path.join(self.home, "user.conf")
        entries = self.entries + [{"source": "etc/shells", "target": user_target}]
        state = {}
        ok, _ = self.deploy(state, entries)
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(user_target))
        self.assertEqual(list(state["files"]), [self.target])


class UserRunTest(SystemFilesBase):
    def test_deploy_leaves_system_files_and_notes_sudo(self):
        state = {}
        ok, out = _quiet(files.install_files, self.entries, self.config_dir, state)
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(self.target))
        self.assertEqual(state["files"], {})
        self.assertIn(f"1 system file(s) differ ({self.target})", out)
        self.assertIn("run sudo tools deploy --scope files", out)

    def test_no_note_when_in_sync_or_under_sudo(self):
        _write(self.target, "/bin/sh\n")
        with mock.patch.object(files, "_uid", return_value=0):
            _, out = _quiet(files.install_files, self.entries, self.config_dir, {})
        self.assertNotIn("system file", out)
        _, out = _quiet(files.install_files, self.entries, self.config_dir, {})
        self.assertIn("1 system file(s) differ", out)
        os.remove(self.target)
        with mock.patch.object(privilege, "UNDER_SUDO", True):
            _, out = _quiet(files.install_files, self.entries, self.config_dir, {})
        self.assertNotIn("system file", out)

    def test_diff_shows_no_rows_only_the_note(self):
        rows, out = _quiet(diff._diff_files, self.entries, self.config_dir, {})
        self.assertEqual(rows, [])
        self.assertIn("system file(s) differ", out)


class RootDiffTest(SystemFilesBase):
    def test_root_diff_rows(self):
        with self.as_root():
            rows, _ = _quiet(diff._diff_files, self.entries, self.config_dir, {}, system=True)
        self.assertEqual(rows, [f"  + create {self.target}"])
        os.symlink("/nonexistent", self.target)
        with self.as_root():
            rows, _ = _quiet(diff._diff_files, self.entries, self.config_dir, {}, system=True)
        self.assertEqual(len(rows), 1)
        self.assertIn(f"~ replace {self.target} (a symlink; old file moved aside", rows[0])

    def test_root_diff_flags_unsafe_dir(self):
        rows, _ = _quiet(diff._diff_files, self.entries, self.config_dir, {}, system=True)
        self.assertEqual(len(rows), 1)
        self.assertIn("! refusing", rows[0])

    def test_show_diff_as_root_includes_files(self):
        root_state = os.path.join(self.tmp, "root", "state.json")
        with (
            self.as_root(),
            mock.patch.object(privilege, "ROOT_STATE_FILE", root_state),
        ):
            clean, out = _quiet(
                diff.show_diff, {"files": self.entries}, self.config_dir, (), root=True
            )
        self.assertFalse(clean)
        self.assertIn(f"+ create {self.target}", out)


class RootSectionsTest(SystemFilesBase):
    def test_root_run_deploys_system_files_into_root_state(self):
        state = {}
        with self.as_root():
            ok, _ = _quiet(
                cli._run_root_sections, {"files": self.entries}, self.config_dir, state, {"files"}
            )
        self.assertTrue(ok)
        self.assertIn(self.target, state["files"])

    def test_root_run_skips_files_without_system_work(self):
        user_only = [{"source": "etc/shells", "target": os.path.join(self.home, "u")}]
        with mock.patch.object(cli, "install_files") as install:
            cli._run_root_sections({"files": user_only}, self.config_dir, {}, {"files"})
        install.assert_not_called()
        with mock.patch.object(cli, "install_files", return_value=True) as install:
            cli._run_root_sections({}, self.config_dir, {"files": {self.target: {}}}, {"files"})
        install.assert_called_once()
        self.assertTrue(install.call_args.kwargs["system"])

    def test_files_is_a_root_section(self):
        self.assertIn("files", cli._system_scope(()))
        self.assertEqual(cli._system_scope(("files", "brew")), ("files",))


if __name__ == "__main__":
    unittest.main()
