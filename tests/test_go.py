import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
            self.assertEqual(state["go"]["packages"], {})

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
            self.assertEqual(state["go"]["packages"], {})

    def test_state_drop_succeeds_when_gobin_unresolvable(self):
        # Dropping a package never needs $GOBIN (Go has no uninstall), so an
        # unresolvable install dir must not turn a pure state-drop into failure.
        state = {"go": {"packages": {"rclone": {"installed": True}}}}
        with mock.patch("tools.user.go._resolve_go_bin", return_value=None):
            success = install_go_packages({}, {}, state)
        self.assertTrue(success)
        self.assertEqual(state["go"]["packages"], {})


class GoUpdateTest(unittest.TestCase):
    def test_unpinned_packages_reinstall_only_with_update(self):
        paths = {"goBin": "/gobin"}
        packages = {
            "hey": {"source": "github.com/rakyll/hey"},
            "pinned": {"source": "example.com/pinned", "version": "v1.0.0"},
        }
        installs = []

        def fake_run(cmd, env=None, cwd=None):
            installs.append(cmd[1:])
            return 0, "", ""

        for update in (False, True):
            state = {"go": {"context": "/gobin|", "packages": {}}}
            for pkg, info in packages.items():
                state["go"]["packages"][pkg] = {"installed": True, "version": "latest", **info}
            with (
                mock.patch("tools.user.go.run_command", fake_run),
                mock.patch("tools.user.go.shutil.which", return_value="/go"),
            ):
                self.assertTrue(install_go_packages(packages, paths, state, update))
        self.assertEqual(installs, [["install", "github.com/rakyll/hey@latest"]])


if __name__ == "__main__":
    unittest.main()


class GoRestartUnitsTest(unittest.TestCase):
    def test_install_owes_restart_and_unchanged_does_not(self):
        packages = {"gatus": {"source": "github.com/x/gatus", "version": "v1",
                              "restartUnits": ["launchd:org.example.gatus"]}}  # fmt: skip
        paths = {"goBin": "/gobin"}
        state = {}
        with (
            mock.patch("tools.user.go.shutil.which", return_value="/go"),
            mock.patch("tools.user.go.run_command", return_value=(0, "", "")),
        ):
            self.assertTrue(install_go_packages(packages, paths, state))
            owed = state["restartUnits"]["launchd:org.example.gatus"]
            self.assertEqual((owed["sources"], owed["seq"]), (["go/gatus"], 1))
            self.assertTrue(install_go_packages(packages, paths, state))
        self.assertEqual(state["restartUnits"]["launchd:org.example.gatus"]["seq"], 1)

    def test_failed_install_owes_nothing(self):
        packages = {"gatus": {"restartUnits": ["launchd:org.example.gatus"]}}
        state = {}
        with (
            mock.patch("tools.user.go.shutil.which", return_value="/go"),
            mock.patch("tools.user.go.run_command", return_value=(1, "", "err")),
        ):
            self.assertFalse(install_go_packages(packages, {"goBin": "/gobin"}, state))
        self.assertNotIn("restartUnits", state)
