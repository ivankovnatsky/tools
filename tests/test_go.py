import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.diff import _diff_go
from tools.user.go import _go_binary, _go_env, install_go_packages


class GoBinaryNameTest(unittest.TestCase):
    def test_major_version_suffix(self):
        cases = {
            "github.com/TwiN/gatus/v5": "gatus",
            "example.com/tool/v10": "tool",
            "example.com/tool/v1": "v1",
            "example.com/tool/v0": "v0",
            "example.com/tool/v02": "v02",
            "github.com/d-kuro/gwq/cmd/gwq": "gwq",
        }
        for source, want in cases.items():
            self.assertEqual(_go_binary("k", {"source": source}), want, source)
        self.assertEqual(_go_binary("k", {"source": "x/v5", "binary": "b"}), "b")


class GoEnvTest(unittest.TestCase):
    def test_configured_gopath_overrides_inherited_gobin(self):
        with mock.patch.dict("os.environ", {"GOBIN": "/elsewhere"}):
            env = _go_env({"goPath": "/gp"})
        self.assertEqual(env["GOBIN"], "/gp/bin")


class GoRemovalTest(unittest.TestCase):
    def test_dropping_last_package_removes_binary_and_clears_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            go_path = Path(temp_dir) / ".go"
            go_bin = go_path / "bin"
            go_bin.mkdir(parents=True)
            (go_bin / "rclone").touch()
            (go_path / "pkg" / "mod" / "example").mkdir(parents=True)
            state = {"go": {"packages": {"rclone": {"installed": True}}}}

            success = install_go_packages({}, {"goPath": str(go_path), "goBin": str(go_bin)}, state)

            self.assertTrue(success)
            self.assertFalse((go_bin / "rclone").exists())
            # The module cache is never touched.
            self.assertTrue((go_path / "pkg" / "mod" / "example").exists())
            self.assertEqual(state["go"]["packages"], {})

    def test_removal_deletes_recorded_path_even_when_env_dir_moved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_bin = Path(temp_dir) / "old"
            new_bin = Path(temp_dir) / "new"
            old_bin.mkdir()
            new_bin.mkdir()
            (old_bin / "gatus").touch()
            (new_bin / "gatus").touch()
            state = {
                "go": {
                    "context": "|",
                    "packages": {"gatus": {"installed": True, "path": str(old_bin / "gatus")}},
                }
            }

            with mock.patch("tools.user.go._resolve_go_bin", return_value=str(new_bin)):
                self.assertTrue(install_go_packages({}, {}, state))

            self.assertFalse((old_bin / "gatus").exists())
            self.assertTrue((new_bin / "gatus").exists())

    def test_legacy_record_with_env_dir_is_only_forgotten(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            (Path(temp_dir) / "rclone").touch()
            state = {"go": {"context": "|", "packages": {"rclone": {"installed": True}}}}

            with mock.patch("tools.user.go._resolve_go_bin", return_value=temp_dir):
                self.assertTrue(install_go_packages({}, {}, state))

            self.assertTrue((Path(temp_dir) / "rclone").exists())
            self.assertEqual(state["go"]["packages"], {})

    def test_removal_without_recorded_binary_derives_go_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            go_bin = Path(temp_dir) / "bin"
            go_bin.mkdir()
            (go_bin / "gatus").touch()
            state = {
                "go": {
                    "packages": {"gatus": {"installed": True, "source": "github.com/TwiN/gatus/v5"}}
                }
            }

            self.assertTrue(install_go_packages({}, {"goBin": str(go_bin)}, state))

            self.assertFalse((go_bin / "gatus").exists())

    def test_dropping_one_of_several_keeps_the_others(self):
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
            state = {"go": {"packages": {"rclone": {"installed": True}}}}

            success = install_go_packages({}, {"goPath": str(go_path), "goBin": str(go_bin)}, state)

            self.assertTrue(success)
            self.assertFalse((go_bin / "rclone").exists())
            self.assertTrue((go_bin / "other").exists())
            self.assertEqual(state["go"]["packages"], {})

    def test_already_missing_binary_is_dropped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = {"go": {"packages": {"rclone": {"installed": True}}}}
            self.assertTrue(install_go_packages({}, {"goBin": temp_dir}, state))
            self.assertEqual(state["go"]["packages"], {})

    def test_failed_removal_stays_tracked(self):
        state = {"go": {"packages": {"rclone": {"installed": True}}}}
        with mock.patch("tools.user.go.os.remove", side_effect=PermissionError("denied")):
            success = install_go_packages({}, {"goBin": "/gobin"}, state)
        self.assertFalse(success)
        self.assertIn("rclone", state["go"]["packages"])

    def test_install_dir_change_forgets_without_deleting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            go_bin = Path(temp_dir) / "new"
            go_bin.mkdir()
            # Same name in the new dir is not ours to delete.
            (go_bin / "rclone").touch()
            state = {"go": {"context": "/old|", "packages": {"rclone": {"installed": True}}}}

            self.assertTrue(install_go_packages({}, {"goBin": str(go_bin)}, state))

            self.assertTrue((go_bin / "rclone").exists())
            self.assertEqual(state["go"]["packages"], {})

    def test_state_drop_succeeds_when_gobin_unresolvable(self):
        state = {"go": {"packages": {"rclone": {"installed": True}}}}
        with mock.patch("tools.user.go._resolve_go_bin", return_value=None):
            success = install_go_packages({}, {}, state)
        self.assertTrue(success)
        self.assertEqual(state["go"]["packages"], {})


class GoReinstallTest(unittest.TestCase):
    def test_failed_reinstall_after_dir_change_is_retried(self):
        packages = {"hey": {"source": "github.com/rakyll/hey"}}
        state = {
            "go": {
                "context": "/old|",
                "packages": {"hey": {"installed": True, "version": "latest", **packages["hey"]}},
            }
        }
        paths = {"goBin": "/new"}
        with (
            mock.patch("tools.user.go.run_command", return_value=(1, "", "boom")),
            mock.patch("tools.user.go.shutil.which", return_value="/go"),
        ):
            self.assertFalse(install_go_packages(packages, paths, state))
        self.assertEqual(state["go"]["context"], "/new|")
        self.assertNotIn("hey", state["go"]["packages"])

        with (
            mock.patch("tools.user.go.run_command", return_value=(0, "", "")) as run,
            mock.patch("tools.user.go.shutil.which", return_value="/go"),
        ):
            self.assertTrue(install_go_packages(packages, paths, state))
        run.assert_called_once()
        self.assertEqual(state["go"]["packages"]["hey"]["path"], "/new/hey")

    def test_changed_binary_name_removes_previous_binary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            (Path(temp_dir) / "old").touch()
            packages = {"tool": {"source": "example.com/new"}}
            state = {
                "go": {
                    "context": f"{temp_dir}|",
                    "packages": {
                        "tool": {
                            "installed": True,
                            "version": "latest",
                            "source": "example.com/old",
                            "path": str(Path(temp_dir) / "old"),
                        }
                    },
                }
            }
            with (
                mock.patch("tools.user.go.run_command", return_value=(0, "", "")),
                mock.patch("tools.user.go.shutil.which", return_value="/go"),
            ):
                self.assertTrue(install_go_packages(packages, {"goBin": temp_dir}, state))
            self.assertFalse((Path(temp_dir) / "old").exists())
            self.assertEqual(state["go"]["packages"]["tool"]["path"], str(Path(temp_dir) / "new"))

    def test_failed_install_after_deleting_previous_binary_is_retried(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            (Path(temp_dir) / "old").touch()
            old = {
                "installed": True,
                "version": "latest",
                "source": "example.com/old",
                "path": str(Path(temp_dir) / "old"),
            }
            state = {"go": {"context": f"{temp_dir}|", "packages": {"tool": old}}}
            with (
                mock.patch("tools.user.go.run_command", return_value=(1, "", "boom")),
                mock.patch("tools.user.go.shutil.which", return_value="/go"),
            ):
                ok = install_go_packages(
                    {"tool": {"source": "example.com/new"}}, {"goBin": temp_dir}, state
                )
            self.assertFalse(ok)
            self.assertFalse((Path(temp_dir) / "old").exists())
            self.assertNotIn("tool", state["go"]["packages"])

    def test_failed_delete_of_previous_binary_keeps_old_record(self):
        old = {
            "installed": True,
            "version": "latest",
            "source": "example.com/old",
            "path": "/g/old",
        }
        state = {"go": {"context": "/g|", "packages": {"tool": dict(old)}}}
        with (
            mock.patch("tools.user.go.os.remove", side_effect=PermissionError("denied")),
            mock.patch("tools.user.go.run_command") as run,
            mock.patch("tools.user.go.shutil.which", return_value="/go"),
        ):
            ok = install_go_packages(
                {"tool": {"source": "example.com/new"}}, {"goBin": "/g"}, state
            )
        self.assertFalse(ok)
        run.assert_not_called()
        self.assertEqual(state["go"]["packages"]["tool"], old)

    def test_missing_declared_binary_is_reinstalled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = {"goBin": temp_dir}
            context = f"{temp_dir}|"
            packages = {
                "gwq": {"source": "github.com/d-kuro/gwq/cmd/gwq", "binary": "gwq"},
                "hey": {"source": "github.com/rakyll/hey", "binary": "hey"},
            }
            (Path(temp_dir) / "hey").touch()
            state = {"go": {"context": context, "packages": {}}}
            for pkg, info in packages.items():
                state["go"]["packages"][pkg] = {"installed": True, "version": "latest", **info}
            with (
                mock.patch("tools.user.go.run_command", return_value=(0, "", "")) as run,
                mock.patch("tools.user.go.shutil.which", return_value="/go"),
            ):
                self.assertTrue(install_go_packages(packages, paths, state))
            self.assertEqual(
                [c.args[0][1:] for c in run.call_args_list],
                [["install", "github.com/d-kuro/gwq/cmd/gwq@latest"]],
            )


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


class GoDiffTest(unittest.TestCase):
    def test_removal_and_missing_binary_preview(self):
        with tempfile.TemporaryDirectory() as go_bin:
            (Path(go_bin) / "hey").touch()
            packages = {
                "gwq": {"source": "github.com/d-kuro/gwq/cmd/gwq", "binary": "gwq"},
                "hey": {"source": "github.com/rakyll/hey", "binary": "hey"},
            }
            state = {"go": {"packages": {"old": {"installed": True}}}}
            for pkg, info in packages.items():
                state["go"]["packages"][pkg] = {"installed": True, "version": "latest", **info}

            changes = _diff_go(packages, {"goBin": go_bin}, state)

        self.assertEqual(changes, ["  ~ reinstall gwq (binary gwq missing)", "  - remove old"])


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
