import errno
import os
import stat
import tempfile
import unittest
from unittest import mock

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
