import os
import tempfile
import unittest

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
