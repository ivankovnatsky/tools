import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.user import pi as pi_mod
from tools.user.pi import install_pi_packages, resolve_pi_cli


class PiCliResolutionTest(unittest.TestCase):
    def test_explicit_pi_cli_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pi_bin = Path(temp_dir) / "pi"
            pi_bin.touch()
            paths = {"piCli": str(pi_bin)}
            self.assertEqual(resolve_pi_cli(paths), str(pi_bin))

    def test_npm_bin_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            npm_bin = Path(temp_dir) / "bin"
            npm_bin.mkdir()
            pi_bin = npm_bin / "pi"
            pi_bin.touch()
            paths = {"npmBin": str(npm_bin)}
            self.assertEqual(resolve_pi_cli(paths), str(pi_bin))


class PiInstallTest(unittest.TestCase):
    def test_install_new_package(self):
        commands_run = []

        def fake_run(cmd, env=None, cwd=None):
            commands_run.append(cmd)
            return 0, "", ""

        with tempfile.TemporaryDirectory() as temp_dir:
            pi_bin = Path(temp_dir) / "pi"
            pi_bin.touch()
            paths = {"piCli": str(pi_bin)}
            state = {}
            packages = {"npm:pi-antigravity": {}}

            with mock.patch.object(pi_mod, "run_command", fake_run):
                result = install_pi_packages(packages, paths, state)

            self.assertTrue(result)
            self.assertEqual(commands_run, [[str(pi_bin), "install", "npm:pi-antigravity"]])
            self.assertIn("npm:pi-antigravity", state["pi"]["packages"])
            self.assertTrue(state["pi"]["packages"]["npm:pi-antigravity"]["installed"])

    def test_remove_unwanted_package(self):
        commands_run = []

        def fake_run(cmd, env=None, cwd=None):
            commands_run.append(cmd)
            return 0, "", ""

        with tempfile.TemporaryDirectory() as temp_dir:
            pi_bin = Path(temp_dir) / "pi"
            pi_bin.touch()
            paths = {"piCli": str(pi_bin)}
            state = {
                "pi": {
                    "packages": {
                        "npm:old-pkg": {"installed": True, "version": "latest", "source": ""},
                    }
                }
            }
            packages = {}

            with mock.patch.object(pi_mod, "run_command", fake_run):
                result = install_pi_packages(packages, paths, state)

            self.assertTrue(result)
            self.assertEqual(commands_run, [[str(pi_bin), "remove", "npm:old-pkg"]])
            self.assertNotIn("npm:old-pkg", state["pi"]["packages"])

    def test_version_changed_reinstalls(self):
        commands_run = []

        def fake_run(cmd, env=None, cwd=None):
            commands_run.append(cmd)
            return 0, "", ""

        with tempfile.TemporaryDirectory() as temp_dir:
            pi_bin = Path(temp_dir) / "pi"
            pi_bin.touch()
            paths = {"piCli": str(pi_bin)}
            state = {
                "pi": {
                    "packages": {
                        "npm:pkg": {"installed": True, "version": "1.0.0", "source": ""},
                    }
                }
            }
            packages = {"npm:pkg": {"version": "2.0.0"}}

            with mock.patch.object(pi_mod, "run_command", fake_run):
                result = install_pi_packages(packages, paths, state)

            self.assertTrue(result)
            self.assertEqual(commands_run, [[str(pi_bin), "install", "npm:pkg@2.0.0"]])
            self.assertEqual(state["pi"]["packages"]["npm:pkg"]["version"], "2.0.0")

    def test_missing_pi_cli_fails_cleanly(self):
        paths = {"piCli": "/nonexistent/pi"}
        state = {}
        with mock.patch("shutil.which", return_value=None):
            result = install_pi_packages({"npm:pkg": {}}, paths, state)
        self.assertFalse(result)

    def test_noop_sync_updates_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pi_bin = Path(temp_dir) / "pi"
            pi_bin.touch()
            paths = {"piCli": str(pi_bin)}
            state = {
                "pi": {
                    "packages": {
                        "npm:pi-antigravity": {
                            "installed": True,
                            "version": "latest",
                            "source": "",
                        }
                    }
                }
            }
            packages = {"npm:pi-antigravity": {}}
            # No commands run because package is already installed and unchanged
            result = install_pi_packages(packages, paths, state)
            self.assertTrue(result)
            self.assertTrue(state["pi"]["packages"]["npm:pi-antigravity"]["installed"])


if __name__ == "__main__":
    unittest.main()
