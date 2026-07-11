import tempfile
import unittest
from pathlib import Path

from tools.user.go import install_go_packages


class GoRemovalTest(unittest.TestCase):
    def test_removing_last_package_keeps_module_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            go_path = Path(temp_dir) / ".go"
            go_bin = go_path / "bin"
            go_bin.mkdir(parents=True)
            (go_bin / "rclone").touch()
            (go_path / "pkg" / "mod" / "example").mkdir(parents=True)
            state = {"go": {"packages": {"rclone": {"binary": "rclone", "installed": True}}}}

            success = install_go_packages({}, {"goPath": str(go_path), "goBin": str(go_bin)}, state)

            self.assertTrue(success)
            self.assertFalse((go_bin / "rclone").exists())
            # $GOPATH is shared with the user's own Go work, and Go records no
            # per-package ownership of modules. Only the binary is ours to remove.
            self.assertTrue((go_path / "pkg" / "mod" / "example").exists())
            self.assertEqual(state["go"], {"packages": {}})

    def test_removing_one_of_several_keeps_the_others(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            go_path = Path(temp_dir) / ".go"
            go_bin = go_path / "bin"
            go_bin.mkdir(parents=True)
            (go_bin / "rclone").touch()
            (go_bin / "hey").touch()
            # `hey` is already installed at the declared version, so this
            # exercises removal alone — no `go install` is shelled out to.
            hey = {"source": "github.com/rakyll/hey", "binary": "hey", "version": "latest"}
            state = {
                "go": {
                    "packages": {
                        "rclone": {"binary": "rclone", "installed": True},
                        "hey": {**hey, "installed": True},
                    }
                }
            }
            packages = {"hey": hey}

            success = install_go_packages(
                packages, {"goPath": str(go_path), "goBin": str(go_bin)}, state
            )

            self.assertTrue(success)
            self.assertFalse((go_bin / "rclone").exists())
            self.assertTrue((go_bin / "hey").exists())
            self.assertEqual(set(state["go"]["packages"]), {"hey"})

    def test_unmanaged_binary_is_left_alone(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            go_path = Path(temp_dir) / ".go"
            go_bin = go_path / "bin"
            go_bin.mkdir(parents=True)
            (go_bin / "rclone").touch()
            (go_bin / "other").touch()
            state = {"go": {"packages": {"rclone": {"binary": "rclone", "installed": True}}}}

            success = install_go_packages({}, {"goPath": str(go_path), "goBin": str(go_bin)}, state)

            self.assertTrue(success)
            self.assertFalse((go_bin / "rclone").exists())
            self.assertTrue((go_bin / "other").exists())


if __name__ == "__main__":
    unittest.main()
