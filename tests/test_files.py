import hashlib
import os
import tempfile
import unittest
from unittest import mock

from tools.user import files


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


if __name__ == "__main__":
    unittest.main()
