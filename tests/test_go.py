import tempfile
import unittest
from pathlib import Path

from tools.user.go import install_go_packages


class GoRemovalTest(unittest.TestCase):
    def test_dropping_last_package_leaves_binary_and_clears_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            go_path = Path(temp_dir) / ".go"
            go_bin = go_path / "bin"
            go_bin.mkdir(parents=True)
            (go_bin / "rclone").touch()
            (go_path / "pkg" / "mod" / "example").mkdir(parents=True)
            state = {"go": {"packages": {"rclone": {"installed": True}}}}

            success = install_go_packages({}, {"goPath": str(go_path), "goBin": str(go_bin)}, state)

            self.assertTrue(success)
            # Go has no uninstall; the binary is intentionally left in $GOBIN.
            self.assertTrue((go_bin / "rclone").exists())
            # The module cache is never touched.
            self.assertTrue((go_path / "pkg" / "mod" / "example").exists())
            # State stops tracking the dropped package.
            self.assertEqual(state["go"], {"packages": {}})

    def test_dropping_one_of_several_keeps_the_others_in_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            go_path = Path(temp_dir) / ".go"
            go_bin = go_path / "bin"
            go_bin.mkdir(parents=True)
            (go_bin / "rclone").touch()
            (go_bin / "hey").touch()
            # `hey` is already installed at the declared version, so no
            # `go install` is shelled out to.
            hey = {"source": "github.com/rakyll/hey", "version": "latest"}
            state = {
                "go": {
                    "packages": {
                        "rclone": {"installed": True},
                        "hey": {**hey, "installed": True},
                    }
                }
            }
            packages = {"hey": hey}

            success = install_go_packages(
                packages, {"goPath": str(go_path), "goBin": str(go_bin)}, state
            )

            self.assertTrue(success)
            # Both binaries stay on disk; only state is reconciled.
            self.assertTrue((go_bin / "rclone").exists())
            self.assertTrue((go_bin / "hey").exists())
            self.assertEqual(set(state["go"]["packages"]), {"hey"})

    def test_unmanaged_binary_is_left_alone(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            go_path = Path(temp_dir) / ".go"
            go_bin = go_path / "bin"
            go_bin.mkdir(parents=True)
            (go_bin / "rclone").touch()
            (go_bin / "other").touch()
            state = {"go": {"packages": {"rclone": {"installed": True}}}}

            success = install_go_packages({}, {"goPath": str(go_path), "goBin": str(go_bin)}, state)

            self.assertTrue(success)
            # Nothing on disk is removed, managed or not.
            self.assertTrue((go_bin / "rclone").exists())
            self.assertTrue((go_bin / "other").exists())
            self.assertEqual(state["go"], {"packages": {}})


if __name__ == "__main__":
    unittest.main()
