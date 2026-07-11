import tempfile
import unittest
from pathlib import Path

from tools.user.go import install_go_packages


class GoCleanupTest(unittest.TestCase):
    def test_removing_last_package_cleans_managed_gopath(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            go_path = Path(temp_dir) / ".go"
            (go_path / "bin").mkdir(parents=True)
            (go_path / "bin" / "rclone").touch()
            (go_path / "pkg" / "mod" / "example").mkdir(parents=True)
            state = {"go": {"packages": {"rclone": {"binary": "rclone", "installed": True}}}}

            success = install_go_packages(
                {}, {"goPath": str(go_path), "goBin": str(go_path / "bin")}, state
            )

            self.assertTrue(success)
            self.assertFalse(go_path.exists())
            self.assertEqual(state["go"], {"packages": {}})

    def test_unmanaged_binary_preserves_package_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            go_path = Path(temp_dir) / ".go"
            (go_path / "bin").mkdir(parents=True)
            (go_path / "bin" / "rclone").touch()
            (go_path / "bin" / "other").touch()
            (go_path / "pkg" / "mod" / "example").mkdir(parents=True)
            state = {"go": {"packages": {"rclone": {"binary": "rclone", "installed": True}}}}

            success = install_go_packages(
                {}, {"goPath": str(go_path), "goBin": str(go_path / "bin")}, state
            )

            self.assertTrue(success)
            self.assertFalse((go_path / "bin" / "rclone").exists())
            self.assertTrue((go_path / "bin" / "other").exists())
            self.assertTrue((go_path / "pkg").exists())

    def test_separate_go_bin_still_cleans_package_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            go_path = Path(temp_dir) / ".go"
            go_bin = Path(temp_dir) / ".local" / "bin"
            go_bin.mkdir(parents=True)
            (go_bin / "rclone").touch()
            (go_path / "pkg" / "mod" / "example").mkdir(parents=True)
            state = {"go": {"packages": {"rclone": {"binary": "rclone", "installed": True}}}}

            success = install_go_packages({}, {"goPath": str(go_path), "goBin": str(go_bin)}, state)

            self.assertTrue(success)
            self.assertFalse((go_bin / "rclone").exists())
            self.assertTrue(go_bin.exists())
            self.assertFalse(go_path.exists())
            self.assertEqual(state["go"], {"packages": {}})

    def test_read_only_module_cache_is_removed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            go_path = Path(temp_dir) / ".go"
            go_bin = Path(temp_dir) / ".local" / "bin"
            go_bin.mkdir(parents=True)
            (go_bin / "rclone").touch()
            module = go_path / "pkg" / "mod" / "example"
            module.mkdir(parents=True)
            (module / "go.mod").touch()
            # Mirror how `go install` writes the module cache.
            (module / "go.mod").chmod(0o444)
            module.chmod(0o555)
            module.parent.chmod(0o555)
            state = {"go": {"packages": {"rclone": {"binary": "rclone", "installed": True}}}}

            success = install_go_packages({}, {"goPath": str(go_path), "goBin": str(go_bin)}, state)

            self.assertTrue(success)
            self.assertFalse(go_path.exists())

    def test_empty_gopath_bin_does_not_block_cleanup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            go_path = Path(temp_dir) / ".go"
            go_bin = Path(temp_dir) / ".local" / "bin"
            go_bin.mkdir(parents=True)
            (go_bin / "rclone").touch()
            (go_path / "bin").mkdir(parents=True)
            (go_path / "pkg" / "mod" / "example").mkdir(parents=True)
            state = {"go": {"packages": {"rclone": {"binary": "rclone", "installed": True}}}}

            success = install_go_packages({}, {"goPath": str(go_path), "goBin": str(go_bin)}, state)

            self.assertTrue(success)
            self.assertFalse(go_path.exists())


if __name__ == "__main__":
    unittest.main()
