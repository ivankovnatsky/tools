import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.user.cargo import install_cargo_packages


class CargoRemovalTest(unittest.TestCase):
    def test_dropping_last_package_leaves_binary_and_clears_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cargo_home = Path(temp_dir) / ".cargo"
            cargo_bin = cargo_home / "bin"
            cargo_bin.mkdir(parents=True)
            (cargo_bin / "genpass").touch()
            (cargo_home / "registry" / "cache").mkdir(parents=True)
            state = {"cargo": {"packages": {"genpass": {"installed": True}}}}

            success = install_cargo_packages({}, {"cargoHome": str(cargo_home)}, state)

            self.assertTrue(success)
            self.assertTrue((cargo_bin / "genpass").exists())
            # The registry cache is never touched.
            self.assertTrue((cargo_home / "registry" / "cache").exists())
            self.assertEqual(state["cargo"]["packages"], {})

    def test_dropping_one_of_several_keeps_the_others_in_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cargo_home = Path(temp_dir) / ".cargo"
            cargo_bin = cargo_home / "bin"
            cargo_bin.mkdir(parents=True)
            (cargo_bin / "genpass").touch()
            (cargo_bin / "ripgrep").touch()
            # `ripgrep` is already installed at the declared version, so no
            # `cargo install` is shelled out to.
            ripgrep = {"version": "14.1.1"}
            state = {
                "cargo": {
                    "packages": {
                        "genpass": {"installed": True},
                        "ripgrep": {**ripgrep, "installed": True},
                    }
                }
            }

            success = install_cargo_packages(
                {"ripgrep": ripgrep}, {"cargoHome": str(cargo_home)}, state
            )

            self.assertTrue(success)
            self.assertTrue((cargo_bin / "genpass").exists())
            self.assertTrue((cargo_bin / "ripgrep").exists())
            self.assertEqual(set(state["cargo"]["packages"]), {"ripgrep"})

    def test_unmanaged_binary_is_left_alone(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cargo_home = Path(temp_dir) / ".cargo"
            cargo_bin = cargo_home / "bin"
            cargo_bin.mkdir(parents=True)
            (cargo_bin / "genpass").touch()
            (cargo_bin / "other").touch()
            state = {"cargo": {"packages": {"genpass": {"installed": True}}}}

            success = install_cargo_packages({}, {"cargoHome": str(cargo_home)}, state)

            self.assertTrue(success)
            self.assertTrue((cargo_bin / "genpass").exists())
            self.assertTrue((cargo_bin / "other").exists())
            self.assertEqual(state["cargo"]["packages"], {})

    def test_state_drop_succeeds_without_cargo(self):
        state = {"cargo": {"packages": {"genpass": {"installed": True}}}}
        with mock.patch("tools.user.cargo.shutil.which", return_value=None):
            success = install_cargo_packages({}, {}, state)
        self.assertTrue(success)
        self.assertEqual(state["cargo"]["packages"], {})


class CargoInstallTest(unittest.TestCase):
    def test_install_args_and_env(self):
        paths = {"cargoHome": "/cargo"}
        packages = {
            "genpass": {"version": "0.5.1"},
            "latest": {},
            "fromgit": {"source": "https://example.com/x.git", "commit": "abc"},
        }
        calls = []

        def fake_run(cmd, env=None, cwd=None):
            calls.append((cmd[1:], env.get("CARGO_HOME")))
            return 0, "", ""

        state = {}
        with (
            mock.patch("tools.user.cargo.run_command", fake_run),
            mock.patch("tools.user.cargo.shutil.which", return_value="/cargo-bin"),
        ):
            self.assertTrue(install_cargo_packages(packages, paths, state))
        self.assertEqual(
            calls,
            [
                (["install", "genpass", "--version", "0.5.1"], "/cargo"),
                (["install", "latest"], "/cargo"),
                (["install", "--git", "https://example.com/x.git", "--rev", "abc"], "/cargo"),
            ],
        )
        self.assertEqual(state["cargo"]["context"], "|/cargo")
        self.assertEqual(
            state["cargo"]["packages"]["genpass"],
            {"installed": True, "version": "0.5.1", "source": ""},
        )


class CargoUpdateTest(unittest.TestCase):
    def test_unpinned_packages_reinstall_only_with_update(self):
        paths = {"cargoHome": "/cargo"}
        packages = {
            "genpass": {},
            "pinned": {"version": "1.0.0"},
        }
        installs = []

        def fake_run(cmd, env=None, cwd=None):
            installs.append(cmd[1:])
            return 0, "", ""

        for update in (False, True):
            state = {"cargo": {"context": "|/cargo", "packages": {}}}
            for pkg, info in packages.items():
                state["cargo"]["packages"][pkg] = {"installed": True, "version": "latest", **info}
            with (
                mock.patch("tools.user.cargo.run_command", fake_run),
                mock.patch("tools.user.cargo.shutil.which", return_value="/cargo"),
            ):
                self.assertTrue(install_cargo_packages(packages, paths, state, update))
        self.assertEqual(installs, [["install", "genpass"]])


if __name__ == "__main__":
    unittest.main()
