import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.diff import _diff_cargo
from tools.user.cargo import _cargo_env, install_cargo_packages


class CargoRemovalTest(unittest.TestCase):
    def _run(self, packages, paths, state, result=(0, "", ""), cargo="/cargo"):
        with (
            mock.patch("tools.user.cargo.run_command", return_value=result) as run,
            mock.patch("tools.user.cargo.shutil.which", return_value=cargo),
        ):
            success = install_cargo_packages(packages, paths, state)
        return success, [
            (c.args[0][1:], c.kwargs["env"].get("CARGO_HOME")) for c in run.call_args_list
        ]

    def test_dropping_package_runs_cargo_uninstall(self):
        state = {
            "cargo": {
                "packages": {
                    "genpass": {"installed": True, "version": "0.5.1"},
                    "ripgrep": {"installed": True, "version": "14.1.1"},
                }
            }
        }

        success, calls = self._run({"ripgrep": {"version": "14.1.1"}}, {"cargoHome": "/c"}, state)

        self.assertTrue(success)
        self.assertEqual(calls, [(["uninstall", "genpass"], "/c")])
        self.assertEqual(set(state["cargo"]["packages"]), {"ripgrep"})

    def test_dropping_one_crate_of_a_shared_repo_uninstalls_that_crate(self):
        src = "https://example.com/x.git"
        with tempfile.TemporaryDirectory() as root:
            record = {
                "installs": {
                    f"crate-a 0.1.0 (git+{src}?rev=abc#abcdef)": {},
                    f"crate-b 0.1.0 (git+{src}?rev=abc#abcdef)": {},
                }
            }
            Path(root, ".crates2.json").write_text(json.dumps(record))
            entry = {"installed": True, "version": "latest", "source": src, "commit": "abc"}
            state = {
                "cargo": {
                    "context": f"{root}|",
                    "root": root,
                    "packages": {"crate-a": dict(entry), "crate-b": dict(entry)},
                }
            }
            keep = {"crate-a": {"source": src, "commit": "abc"}}
            success, calls = self._run(keep, {"cargoRoot": root}, state)

        self.assertTrue(success)
        self.assertEqual([c[0] for c in calls], [["uninstall", "crate-b"]])
        self.assertEqual(set(state["cargo"]["packages"]), {"crate-a"})

    def test_uninstall_name_matches_install(self):
        with tempfile.TemporaryDirectory() as root:
            record = {
                "installs": {
                    "other 1.0.0 (registry+https://github.com/rust-lang/crates.io-index)": {},
                    "repo-crate 0.1.0 (git+https://example.com/x.git?rev=abc#abcdef)": {},
                }
            }
            Path(root, ".crates2.json").write_text(json.dumps(record))
            state = {
                "cargo": {
                    "packages": {
                        "alias": {"installed": True, "source": "real-crate"},
                        "fromgit": {
                            "installed": True,
                            "source": "https://example.com/x.git",
                            "commit": "abc",
                        },
                        "gone": {
                            "installed": True,
                            "source": "https://example.com/y.git",
                            "commit": "def",
                        },
                    }
                }
            }

            success, calls = self._run({}, {"cargoRoot": root}, state)

        self.assertTrue(success)
        self.assertEqual(
            [c[0] for c in calls], [["uninstall", "real-crate"], ["uninstall", "repo-crate"]]
        )
        self.assertEqual(state["cargo"]["packages"], {})

    def test_already_uninstalled_is_dropped(self):
        state = {"cargo": {"packages": {"genpass": {"installed": True}}}}
        err = "error: package ID specification `genpass` did not match any packages"

        success, _ = self._run({}, {}, state, result=(101, "", err))

        self.assertTrue(success)
        self.assertEqual(state["cargo"]["packages"], {})

    def test_failed_uninstall_stays_tracked(self):
        state = {"cargo": {"packages": {"genpass": {"installed": True}}}}

        success, _ = self._run({}, {}, state, result=(101, "", "error: permission denied"))

        self.assertFalse(success)
        self.assertIn("genpass", state["cargo"]["packages"])

    def test_without_cargo_removal_warns_and_stays_tracked(self):
        state = {"cargo": {"packages": {"genpass": {"installed": True}}}}

        success, calls = self._run({}, {}, state, cargo=None)

        self.assertTrue(success)
        self.assertEqual(calls, [])
        self.assertIn("genpass", state["cargo"]["packages"])

    def test_install_dir_change_forgets_without_uninstall(self):
        state = {"cargo": {"context": "|/old", "packages": {"genpass": {"installed": True}}}}

        success, calls = self._run({}, {"cargoHome": "/new"}, state)

        self.assertTrue(success)
        self.assertEqual(calls, [])
        self.assertEqual(state["cargo"]["packages"], {})


class CargoRootTest(unittest.TestCase):
    def test_env_root_change_forgets_without_uninstall(self):
        state = {
            "cargo": {
                "context": "|",
                "root": "/old",
                "packages": {"genpass": {"installed": True}},
            }
        }
        with (
            mock.patch.dict("os.environ", {"CARGO_HOME": "/new"}, clear=False),
            mock.patch("tools.user.cargo.run_command") as run,
            mock.patch("tools.user.cargo.shutil.which", return_value="/cargo"),
        ):
            self.assertTrue(install_cargo_packages({}, {}, state))
        run.assert_not_called()
        self.assertEqual(state["cargo"]["packages"], {})
        self.assertEqual(state["cargo"]["root"], "/new")

    def test_git_crate_without_source_uses_key(self):
        with tempfile.TemporaryDirectory() as root:
            key = "https://example.com/x.git"
            record = {"installs": {f"xcrate 0.1.0 (git+{key}?rev=abc#abcdef)": {}}}
            Path(root, ".crates2.json").write_text(json.dumps(record))
            state = {"cargo": {"packages": {key: {"installed": True, "commit": "abc"}}}}
            with (
                mock.patch("tools.user.cargo.run_command", return_value=(0, "", "")) as run,
                mock.patch("tools.user.cargo.shutil.which", return_value="/cargo"),
            ):
                self.assertTrue(install_cargo_packages({}, {"cargoRoot": root}, state))
        self.assertEqual(run.call_args.args[0][1:], ["uninstall", "xcrate"])


class CargoEnvTest(unittest.TestCase):
    def test_configured_home_overrides_inherited_install_root(self):
        with mock.patch.dict("os.environ", {"CARGO_INSTALL_ROOT": "/elsewhere"}):
            env = _cargo_env({"cargoHome": "/ch"})
        self.assertEqual(env["CARGO_INSTALL_ROOT"], "/ch")


class CargoRecordTest(unittest.TestCase):
    def test_unreadable_record_keeps_package_tracked(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, ".crates2.json").write_text("{not json")
            entry = {"installed": True, "source": "https://example.com/x.git", "commit": "abc"}
            state = {"cargo": {"packages": {"fromgit": entry}}}
            with (
                mock.patch("tools.user.cargo.run_command") as run,
                mock.patch("tools.user.cargo.shutil.which", return_value="/cargo"),
            ):
                self.assertFalse(install_cargo_packages({}, {"cargoRoot": root}, state))
        run.assert_not_called()
        self.assertIn("fromgit", state["cargo"]["packages"])


class CargoReinstallTest(unittest.TestCase):
    def test_new_revision_of_same_repo_is_not_uninstalled_first(self):
        with tempfile.TemporaryDirectory() as root:
            src = "https://example.com/x.git"
            record = {"installs": {f"xcrate 0.1.0 (git+{src}?rev=old#oldfull)": {}}}
            Path(root, ".crates2.json").write_text(json.dumps(record))
            old = {"installed": True, "version": "latest", "source": src, "commit": "old"}
            state = {"cargo": {"context": f"{root}|", "root": root, "packages": {"x": old}}}
            with (
                mock.patch("tools.user.cargo.run_command", return_value=(0, "", "")) as run,
                mock.patch("tools.user.cargo.shutil.which", return_value="/cargo"),
            ):
                ok = install_cargo_packages(
                    {"x": {"source": src, "commit": "new"}}, {"cargoRoot": root}, state
                )
        self.assertTrue(ok)
        self.assertEqual(
            [c.args[0][1:] for c in run.call_args_list],
            [["install", "--git", src, "--rev", "new", "x"]],
        )

    def test_failed_install_after_replacing_crate_is_retried(self):
        packages = {"tool": {"source": "new-crate"}}
        state = {
            "cargo": {
                "context": "|/c",
                "packages": {
                    "tool": {"installed": True, "version": "latest", "source": "old-crate"}
                },
            }
        }

        def fake_run(cmd, env=None, cwd=None):
            return (0, "", "") if cmd[1] == "uninstall" else (101, "", "boom")

        with (
            mock.patch("tools.user.cargo.run_command", fake_run),
            mock.patch("tools.user.cargo.shutil.which", return_value="/cargo"),
        ):
            self.assertFalse(install_cargo_packages(packages, {"cargoHome": "/c"}, state))
        self.assertNotIn("tool", state["cargo"]["packages"])

    def test_changed_crate_uninstalls_previous_one(self):
        packages = {"tool": {"source": "new-crate"}}
        state = {
            "cargo": {
                "context": "|/c",
                "packages": {
                    "tool": {"installed": True, "version": "latest", "source": "old-crate"}
                },
            }
        }
        with (
            mock.patch("tools.user.cargo.run_command", return_value=(0, "", "")) as run,
            mock.patch("tools.user.cargo.shutil.which", return_value="/cargo"),
        ):
            self.assertTrue(install_cargo_packages(packages, {"cargoHome": "/c"}, state))
        self.assertEqual(
            [c.args[0][1:] for c in run.call_args_list],
            [["uninstall", "old-crate"], ["install", "new-crate"]],
        )
        self.assertEqual(state["cargo"]["packages"]["tool"]["source"], "new-crate")

    def test_failed_uninstall_of_previous_crate_keeps_old_record(self):
        packages = {"tool": {"source": "new-crate"}}
        old = {"installed": True, "version": "latest", "source": "old-crate"}
        state = {"cargo": {"context": "|/c", "packages": {"tool": dict(old)}}}
        with (
            mock.patch("tools.user.cargo.run_command", return_value=(101, "", "denied")) as run,
            mock.patch("tools.user.cargo.shutil.which", return_value="/cargo"),
        ):
            self.assertFalse(install_cargo_packages(packages, {"cargoHome": "/c"}, state))
        self.assertEqual([c.args[0][1:] for c in run.call_args_list], [["uninstall", "old-crate"]])
        self.assertEqual(state["cargo"]["packages"]["tool"], old)

    def test_failed_reinstall_after_dir_change_is_retried(self):
        packages = {"genpass": {"version": "0.5.1"}}
        state = {
            "cargo": {
                "context": "|/old",
                "packages": {"genpass": {"installed": True, "version": "0.5.1", "source": ""}},
            }
        }
        paths = {"cargoHome": "/new"}
        with (
            mock.patch("tools.user.cargo.run_command", return_value=(101, "", "boom")),
            mock.patch("tools.user.cargo.shutil.which", return_value="/cargo"),
        ):
            self.assertFalse(install_cargo_packages(packages, paths, state))
        self.assertEqual(state["cargo"]["context"], "|/new")
        self.assertNotIn("genpass", state["cargo"]["packages"])

        with (
            mock.patch("tools.user.cargo.run_command", return_value=(0, "", "")) as run,
            mock.patch("tools.user.cargo.shutil.which", return_value="/cargo"),
        ):
            self.assertTrue(install_cargo_packages(packages, paths, state))
        run.assert_called_once()
        self.assertIn("genpass", state["cargo"]["packages"])


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
                (
                    ["install", "--git", "https://example.com/x.git", "--rev", "abc", "fromgit"],
                    "/cargo",
                ),
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


class CargoDiffTest(unittest.TestCase):
    def test_removal_preview(self):
        state = {"cargo": {"packages": {"genpass": {"installed": True}}}}
        self.assertEqual(_diff_cargo({}, {}, state), ["  - remove genpass"])


if __name__ == "__main__":
    unittest.main()


class CargoRestartUnitsTest(unittest.TestCase):
    UNIT = "launchd:org.example.svc"
    PACKAGES = {"svc": {"restartUnits": [UNIT]}}

    def owed(self, state):
        return state.get("restartUnits", {}).get(self.UNIT)

    def run_install(self, state, returncode=0, update=False, packages=None):
        with (
            mock.patch("tools.user.cargo.shutil.which", return_value="/cargo"),
            mock.patch("tools.user.cargo.run_command", return_value=(returncode, "", "err")) as run,
        ):
            ok = install_cargo_packages(packages or self.PACKAGES, {}, state, update)
        return ok, run

    def test_install_owes_and_unchanged_does_not(self):
        state = {}
        self.assertTrue(self.run_install(state)[0])
        self.assertEqual((self.owed(state)["sources"], self.owed(state)["seq"]), (["cargo/svc"], 1))
        self.assertNotIn("restartUnits", state["cargo"]["packages"]["svc"])
        self.assertTrue(self.run_install(state)[0])
        self.assertEqual(self.owed(state)["seq"], 1)

    def test_update_reinstall_owes(self):
        state = {}
        self.run_install(state)
        self.run_install(state, update=True)
        self.assertEqual(self.owed(state)["seq"], 2)

    def test_failed_install_owes_nothing(self):
        state = {}
        self.assertFalse(self.run_install(state, returncode=1)[0])
        self.assertIsNone(self.owed(state))

    def test_invalid_restart_units_is_an_error(self):
        state = {}
        ok, run = self.run_install(state, packages={"svc": {"restartUnits": "launchd:x"}})
        self.assertFalse(ok)
        run.assert_not_called()
        self.assertNotIn("restartUnits", state)
        self.assertNotIn("svc", state["cargo"].get("packages", {}))
