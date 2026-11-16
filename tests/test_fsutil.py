import errno
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

import trash_fake

from tools import fsutil


class RefreshDevTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "secret")
        with open(self.path, "w") as f:
            f.write("x")
        self.st = os.stat(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_without_dev_gains_it(self):
        record = {"ino": self.st.st_ino, "mtime_ns": self.st.st_mtime_ns}
        fsutil.refresh_dev(self.path, record)
        self.assertEqual(record["dev"], self.st.st_dev)

    def test_changed_dev_is_re_recorded(self):
        record = {"ino": self.st.st_ino, "dev": self.st.st_dev + 1}
        fsutil.refresh_dev(self.path, record)
        self.assertEqual(record["dev"], self.st.st_dev)

    def test_other_inode_is_left_alone(self):
        record = {"ino": self.st.st_ino + 1}
        fsutil.refresh_dev(self.path, record)
        self.assertNotIn("dev", record)

    def test_legacy_record_is_strict_after_refresh(self):
        record = {"ino": self.st.st_ino, "mtime_ns": self.st.st_mtime_ns}
        fsutil.refresh_dev(self.path, record)
        record["dev"] += 1
        record["mtime_ns"] += 1
        self.assertFalse(fsutil.same_identity(self.path, record))


if __name__ == "__main__":
    unittest.main()


class MoveAsideTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = os.path.realpath(self._tmp.name)
        self.path = os.path.join(self.d, "f")

    def tearDown(self):
        self._tmp.cleanup()

    def test_moves_file_keeping_mode(self):
        with open(self.path, "w") as f:
            f.write("old")
        os.chmod(self.path, 0o600)
        dest = fsutil.move_aside(self.path)
        self.assertEqual(dest, self.path + ".tools")
        self.assertFalse(os.path.lexists(self.path))
        self.assertEqual(stat.S_IMODE(os.lstat(dest).st_mode), 0o600)

    def test_moves_symlink_itself(self):
        os.symlink("/nonexistent/pointee", self.path)
        dest = fsutil.move_aside(self.path)
        self.assertEqual(os.readlink(dest), "/nonexistent/pointee")
        self.assertFalse(os.path.lexists(self.path))

    def test_existing_backup_is_never_replaced(self):
        with open(self.path, "w") as f:
            f.write("old")
        with open(self.path + ".tools", "w") as f:
            f.write("earlier")
        with self.assertRaises(FileExistsError):
            fsutil.move_aside(self.path)
        with open(self.path + ".tools") as f:
            self.assertEqual(f.read(), "earlier")
        with open(self.path) as f:
            self.assertEqual(f.read(), "old")

    def test_no_hard_links_is_an_error_not_a_rename(self):
        with open(self.path, "w") as f:
            f.write("old")
        with mock.patch("os.link", side_effect=OSError(errno.EXDEV, "cross-device")):
            with self.assertRaises(OSError):
                fsutil.move_aside(self.path)
        self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.lexists(self.path + ".tools"))


class TrashArgvTest(unittest.TestCase):
    def test_macos_uses_system_trash_with_absolute_path(self):
        with (
            mock.patch.object(fsutil.sys, "platform", "darwin"),
            mock.patch.object(fsutil.os.path, "exists", return_value=True),
        ):
            argv = fsutil.trash_argv("rel/-file")
        self.assertEqual(argv, ["/usr/bin/trash", os.path.abspath("rel/-file")])

    def test_macos_without_trash_raises(self):
        with (
            mock.patch.object(fsutil.sys, "platform", "darwin"),
            mock.patch.object(fsutil.os.path, "exists", return_value=False),
        ):
            with self.assertRaises(fsutil.TrashError):
                fsutil.trash_argv("/x")

    def test_linux_prefers_gio_then_trash_put(self):
        tools = {"gio": "/bin/gio", "trash-put": "/bin/trash-put"}
        with (
            mock.patch.object(fsutil.sys, "platform", "linux"),
            mock.patch.object(fsutil.shutil, "which", side_effect=tools.get),
        ):
            self.assertEqual(fsutil.trash_argv("/x"), ["/bin/gio", "trash", "/x"])
            del tools["gio"]
            self.assertEqual(fsutil.trash_argv("/x"), ["/bin/trash-put", "/x"])
            del tools["trash-put"]
            with self.assertRaises(fsutil.TrashError) as cm:
                fsutil.trash_argv("/x")
        self.assertIn("no trash tool", str(cm.exception))


class TrashTest(unittest.TestCase):
    def setUp(self):
        trash_fake.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()
        trash_fake.stop()

    def _in_trash(self):
        return os.listdir(trash_fake.trash_dir())

    def test_missing_path_is_a_no_op(self):
        self.assertFalse(fsutil.trash(os.path.join(self.d, "nope")))
        self.assertEqual(self._in_trash(), [])

    def test_file_directory_and_dangling_symlink_are_moved(self):
        f = os.path.join(self.d, "f")
        tree = os.path.join(self.d, "tree")
        link = os.path.join(self.d, "link")
        with open(f, "w") as fh:
            fh.write("x")
        os.makedirs(os.path.join(tree, "sub"))
        os.symlink(os.path.join(self.d, "gone"), link)
        for path in (f, tree, link):
            self.assertTrue(fsutil.trash(path))
            self.assertFalse(os.path.lexists(path))
        self.assertEqual(len(self._in_trash()), 3)

    def test_failing_tool_raises_and_keeps_path(self):
        f = os.path.join(self.d, "f")
        open(f, "w").close()
        with mock.patch.object(
            fsutil, "trash_argv", return_value=[sys.executable, "-c", "raise SystemExit(5)"]
        ):
            with self.assertRaises(fsutil.TrashError):
                fsutil.trash(f)
        self.assertTrue(os.path.exists(f))

    def test_tool_that_leaves_the_path_raises(self):
        f = os.path.join(self.d, "f")
        open(f, "w").close()
        with mock.patch.object(fsutil, "trash_argv", return_value=[sys.executable, "-c", ""]):
            with self.assertRaises(fsutil.TrashError):
                fsutil.trash(f)

    def test_trash_error_is_an_oserror(self):
        self.assertTrue(issubclass(fsutil.TrashError, OSError))
