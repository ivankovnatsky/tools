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
        self.root_dirs = {self.etc}
        for patch in (
            mock.patch.object(privilege, "UNDER_SUDO", False),
            mock.patch.dict(os.environ, {"HOME": self.home}),
            mock.patch.object(files, "_user_mode", side_effect=self.user_mode),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def user_mode(self, path, st, inv):
        """The tmp dirs in root_dirs stand in for root-owned 0755 directories."""
        return 5 if path in self.root_dirs else privilege.user_mode(st, inv)

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

    def test_user_writable_dir_outside_home_is_user_file(self):
        cache = os.path.join(self.tmp, "Data", ".opentofu", "plugin-cache")
        entry = {"target": cache, "directory": True}
        self.assertFalse(files.is_system_entry(entry))
        self.assertEqual(files._resolve_entries([entry], self.config_dir, system=True), ([], []))
        user, errors = files._resolve_entries([entry], self.config_dir)
        self.assertEqual(errors, [])
        self.assertEqual([e.target for e in user], [cache])

    def test_existing_directory_entry_is_classified_by_itself(self):
        self.root_dirs.add(self.tmp)
        for target in ("~", self.home):
            with self.subTest(target=target):
                entry = {"target": target, "directory": True, "mode": "0700"}
                self.assertFalse(files.is_system_entry(entry))
                user, errors = files._resolve_entries([entry], self.config_dir)
                self.assertEqual(errors, [])
                self.assertEqual([e.target for e in user], [self.home])
        self.assertTrue(
            files.is_system_entry({"target": os.path.join(self.tmp, "new"), "directory": True})
        )

    def test_nearest_existing_ancestor_decides(self):
        deep = {"source": "etc/shells", "target": os.path.join(self.etc, "a", "b", "c")}
        self.assertTrue(files.is_system_entry(deep))
        self.root_dirs = set()
        self.assertFalse(files.is_system_entry(self.entries[0]))

    def test_symlink_target_into_home_is_classified_by_its_directory(self):
        dotfile = os.path.join(self.home, "foo")
        _write(dotfile, "x\n")
        os.symlink(dotfile, self.target)
        self.assertTrue(files.is_system_entry(self.entries[0]))
        with mock.patch.object(files.privilege, "root_dir_error", return_value=None):
            self.assertIsNone(files.root_target_error(self.target))

    def test_root_refuses_the_home_path_and_its_ancestors(self):
        link = os.path.join(self.etc, "home-link")
        os.symlink(self.home, link)
        with (
            mock.patch.dict(os.environ, {"HOME": link}),
            mock.patch.object(files.privilege, "root_dir_error", return_value=None),
        ):
            for target in (link, self.home, self.tmp):
                with self.subTest(target=target):
                    self.assertIn("the user's home", files.root_target_error(target) or "")
            self.assertIsNone(files.root_target_error(self.target))

    def test_invalid_system_entries_are_errors_in_both_runs(self):
        source = {"source": "etc/shells"}
        bad = {
            "onChange": {**source, "target": self.target, "onChange": "true"},
            "onUpdate": {**source, "target": self.target, "onUpdate": "true"},
            "directory": {"target": os.path.join(self.etc, "d"), "directory": True},
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

    def test_unreadable_system_file_is_noted_as_unchecked(self):
        _write(self.target, "/bin/sh\n", 0)
        if os.access(self.target, os.R_OK):
            self.skipTest("running as root")
        _, out = _quiet(files.install_files, self.entries, self.config_dir, {})
        self.assertNotIn("differ", out)
        self.assertIn(f"cannot check 1 system file(s) ({self.target})", out)

    def test_unsearchable_system_dir_is_noted_as_unchecked(self):
        locked = os.path.join(self.tmp, "locked")
        os.makedirs(locked, 0o000)
        self.addCleanup(os.chmod, locked, 0o755)
        if os.access(locked, os.X_OK):
            self.skipTest("running as root")
        self.root_dirs.add(locked)
        entries = [{"source": "etc/shells", "target": os.path.join(locked, "f")}]
        _, out = _quiet(files.install_files, entries, self.config_dir, {})
        self.assertNotIn("differ", out)
        self.assertIn("cannot check 1 system file(s)", out)

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


class OtherRunRecordTest(SystemFilesBase):
    """A record left in one run's state for a target the other run now deploys."""

    def record(self, target):
        _write(target, "/bin/sh\n")
        return {"files": {target: files._record(target, files._file_hash(target))}}

    def test_user_run_keeps_a_target_now_deployed_by_root(self):
        state = self.record(self.target)
        rows, _ = _quiet(diff._diff_files, self.entries, self.config_dir, state)
        self.assertEqual(rows, [])
        ok, out = _quiet(files.install_files, self.entries, self.config_dir, state)
        self.assertTrue(ok, out)
        self.assertTrue(os.path.exists(self.target))
        self.assertIn(self.target, state["files"])

    def test_root_run_keeps_a_target_now_deployed_by_the_user(self):
        target = os.path.join(self.tmp, "data", "user.conf")
        state = self.record(target)
        entries = [{"source": "etc/shells", "target": target}]
        with self.as_root():
            rows, _ = _quiet(diff._diff_files, entries, self.config_dir, state, system=True)
            ok, out = _quiet(files.install_files, entries, self.config_dir, state, system=True)
        self.assertEqual(rows, [])
        self.assertTrue(ok, out)
        self.assertTrue(os.path.exists(target))
        self.assertIn(target, state["files"])


class ClassifyOnceTest(unittest.TestCase):
    def test_chmod_by_a_directory_entry_does_not_drop_a_file_inside(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = os.path.join(tmp.name, "cfg")
        _write(os.path.join(cfg, "s"), "x\n")
        d = os.path.join(os.path.realpath(tmp.name), "D")
        f = os.path.join(d, "f")
        entries = [{"target": d, "directory": True, "mode": "0755"}, {"source": "s", "target": f}]
        state = {}
        ok, out = _quiet(files.install_files, entries, cfg, state)
        self.assertTrue(ok, out)
        self.assertIn(f, state["files"])
        os.chmod(d, 0o555)
        for run in (diff._diff_files, files.install_files):
            with self.subTest(run=run.__name__):
                result, out = _quiet(run, entries, cfg, state)
                self.assertNotIn("remove", str(result) + out)
                self.assertTrue(os.path.exists(f))
                self.assertIn(f, state["files"])


class DirectoryModeTest(unittest.TestCase):
    def test_own_read_only_directory_stays_a_user_entry(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = os.path.join(os.path.realpath(tmp.name), "ro")
        self.addCleanup(lambda: os.path.isdir(d) and os.chmod(d, 0o755))
        entries = [{"target": d, "directory": True, "mode": "0555"}]
        state = {}
        for run in (1, 2):
            with self.subTest(run=run):
                ok, out = _quiet(files.install_files, entries, tmp.name, state)
                self.assertTrue(ok, out)
                self.assertFalse(files.is_system_entry(entries[0]))
                self.assertEqual(os.stat(d).st_mode & 0o777, 0o555)
                self.assertIn(d, state["files"])


class DecisionDirTest(unittest.TestCase):
    """A root-owned 0700 dir holding a user-owned dir: root can look inside, the user
    cannot, yet both stop at the same directory."""

    USER = privilege.Invoker(True, 501, "/Users/u", "u", gid=20)
    TREE = {
        "/": (0, 0o40755),
        "/r": (0, 0o40700),
        "/r/u": (501, 0o40755),
        "/r/u/f": (501, 0o100644),
    }

    def lstat_as(self, root):
        def lstat(path):
            parent = os.path.dirname(path)
            if not root and path != "/" and parent == "/r":
                raise PermissionError(path)
            if path not in self.TREE:
                raise FileNotFoundError(path)
            uid, mode = self.TREE[path]
            return os.stat_result((mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))

        return lstat

    def test_root_and_user_reach_the_same_directory(self):
        with mock.patch.object(privilege.pwd, "getpwuid", side_effect=KeyError):
            for directory, target in ((False, "/r/u/f"), (True, "/r/u")):
                got = {
                    root: files._decision_dir(target, directory, self.USER, self.lstat_as(root))
                    for root in (True, False)
                }
                self.assertEqual(got[True][0], "/r")
                self.assertEqual(got[True][0], got[False][0])
                self.assertEqual(got[True][2], got[False][2])
                self.assertFalse(got[True][2])


class UserCanWriteTest(unittest.TestCase):
    def check(self, uid, gid, mode):
        inv = privilege.Invoker(True, os.getuid(), "/home/u", None)
        st = os.stat_result((mode, 0, 0, 1, uid, gid, 0, 0, 0, 0))
        return privilege.user_can_write("/d", inv, lambda _p: st)

    def test_mode_bits_for_the_invoking_user(self):
        me, my_gid = os.getuid(), os.getgid()
        self.assertTrue(self.check(me, 0, 0o40755))
        self.assertFalse(self.check(me, 0, 0o40555))
        self.assertTrue(self.check(0, my_gid, 0o40775))
        self.assertFalse(self.check(0, my_gid, 0o40755))
        self.assertTrue(self.check(0, 0, 0o41777))
        self.assertFalse(self.check(0, 0, 0o40755))

    def test_uid_without_passwd_entry_uses_its_groups(self):
        inv = privilege.Invoker(False, os.getuid(), "/home/u", None, gid=4242)
        with mock.patch.object(privilege.pwd, "getpwuid", side_effect=KeyError):
            for gid, mode, want in ((4242, 0o40775, True), (1, 0o40775, False)):
                st = os.stat_result((mode, 0, 0, 1, 0, gid, 0, 0, 0, 0))
                self.assertEqual(privilege.user_can_write("/d", inv, lambda _p: st), want)


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
