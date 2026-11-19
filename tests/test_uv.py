import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import cli
from tools.diff import _diff_uv, show_diff
from tools.user import files
from tools.user.uv import _uv_commit_spec, install_uv_packages

REPO = "git+https://github.com/ivankovnatsky/tools"
PATHS = {"uv": "/uv", "uvBin": "/bin", "uvToolDir": "/tooldir"}


class UvCommitSpecTest(unittest.TestCase):
    def test_plain_source(self):
        self.assertEqual(_uv_commit_spec(REPO, "abc"), f"{REPO}@abc")

    def test_ref_goes_before_fragment(self):
        self.assertEqual(
            _uv_commit_spec(f"{REPO}#subdirectory=packages/gh-pr", "abc"),
            f"{REPO}@abc#subdirectory=packages/gh-pr",
        )

    def test_at_in_path_is_kept(self):
        self.assertEqual(
            _uv_commit_spec("git+https://h/o@x/r#subdirectory=p", "abc"),
            "git+https://h/o@x/r@abc#subdirectory=p",
        )

    def test_local_path_with_at(self):
        self.assertEqual(_uv_commit_spec("/Users/a@b/pkg", "abc"), "/Users/a@b/pkg@abc")

    def test_ssh_userinfo_is_kept(self):
        self.assertEqual(
            _uv_commit_spec("git+ssh://git@github.com/o/r#subdirectory=x", "abc"),
            "git+ssh://git@github.com/o/r@abc#subdirectory=x",
        )


class UvInstallTest(unittest.TestCase):
    def test_installs_fragment_source_with_ref_before_fragment(self):
        source = f"{REPO}#subdirectory=packages/gh-pr"
        packages = {"gh-pr": {"source": source, "commit": "abc"}}
        state = {}
        with mock.patch("tools.user.uv.run_command", return_value=(0, "", "")) as run:
            self.assertTrue(install_uv_packages(packages, PATHS, state))
        cmd = run.call_args[0][0]
        self.assertEqual(
            cmd, ["/uv/uv", "tool", "install", f"{REPO}@abc#subdirectory=packages/gh-pr"]
        )
        self.assertEqual(state["uv"]["packages"]["gh-pr"]["source"], source)
        self.assertEqual(state["uv"]["packages"]["gh-pr"]["commit"], "abc")

    def test_unchanged_pinned_package_is_not_reinstalled(self):
        source = f"{REPO}#subdirectory=packages/gh-pr"
        packages = {"gh-pr": {"source": source, "commit": "abc"}}
        state = {
            "uv": {
                "context": "/bin|/tooldir",
                "packages": {
                    "gh-pr": {
                        "installed": True,
                        "version": "latest",
                        "source": source,
                        "commit": "abc",
                    }
                },
            }
        }
        with mock.patch("tools.user.uv.run_command") as run:
            self.assertTrue(install_uv_packages(packages, PATHS, state))
        run.assert_not_called()


class UvBinaryTest(unittest.TestCase):
    def test_missing_declared_binary_is_force_reinstalled(self):
        with tempfile.TemporaryDirectory() as bin_dir:
            paths = {**PATHS, "uvBin": bin_dir}
            (Path(bin_dir) / "ruff").touch()
            packages = {"ruff": {"binary": "ruff"}, "osxphotos": {"binary": "osxphotos"}}
            state = {"uv": {"context": f"{bin_dir}|/tooldir", "packages": {}}}
            for pkg in packages:
                state["uv"]["packages"][pkg] = {
                    "installed": True,
                    "version": "latest",
                    "source": "",
                }
            with mock.patch("tools.user.uv.run_command", return_value=(0, "", "")) as run:
                self.assertTrue(install_uv_packages(packages, paths, state))
            self.assertEqual(
                [c.args[0] for c in run.call_args_list],
                [["/uv/uv", "tool", "install", "osxphotos", "--force"]],
            )


class UvUpdateTest(unittest.TestCase):
    def test_unpinned_packages_upgrade_only_with_update(self):
        packages = {"loose": {}, "pinned": {"version": "1.0.0"}}
        for update, expected in ((False, []), (True, [["/uv/uv", "tool", "upgrade", "loose"]])):
            state = {"uv": {"context": "/bin|/tooldir", "packages": {}}}
            for pkg, info in packages.items():
                state["uv"]["packages"][pkg] = {
                    "installed": True,
                    "version": "latest",
                    "source": "",
                    **info,
                }
            with mock.patch("tools.user.uv.run_command", return_value=(0, "", "")) as run:
                self.assertTrue(install_uv_packages(packages, PATHS, state, update))
            self.assertEqual([c.args[0] for c in run.call_args_list], expected)


class UvDiffTest(unittest.TestCase):
    def test_missing_binary_preview(self):
        with tempfile.TemporaryDirectory() as bin_dir:
            (Path(bin_dir) / "ruff").touch()
            packages = {"ruff": {"binary": "ruff"}, "osxphotos": {"binary": "osxphotos"}}
            state = {"uv": {"packages": {p: {"installed": True} for p in packages}}}

            changes = _diff_uv(packages, {"uvBin": bin_dir}, state)

        self.assertEqual(changes, ["  ~ reinstall osxphotos (binary osxphotos missing)"])


class UvRestartUnitsTest(unittest.TestCase):
    UNITS = {"restartUnits": ["launchd:org.example.svc"]}

    def owed(self, state):
        return state.get("restartUnits", {}).get("launchd:org.example.svc")

    def test_install_owes_restart(self):
        state = {}
        with mock.patch("tools.user.uv.run_command", return_value=(0, "", "")):
            self.assertTrue(install_uv_packages({"svc": dict(self.UNITS)}, PATHS, state))
        self.assertEqual(self.owed(state)["sources"], ["uv/svc"])
        self.assertNotIn("restartUnits", state["uv"]["packages"]["svc"])

    def test_failed_install_owes_nothing(self):
        state = {}
        with mock.patch("tools.user.uv.run_command", return_value=(1, "", "err")):
            self.assertFalse(install_uv_packages({"svc": dict(self.UNITS)}, PATHS, state))
        self.assertIsNone(self.owed(state))

    def test_unchanged_owes_nothing_and_upgrade_owes_restart(self):
        state = {"uv": {"packages": {"svc": {"installed": True, "version": "latest"}},
                        "context": "/bin|/tooldir"}}  # fmt: skip
        with mock.patch("tools.user.uv.run_command", return_value=(0, "", "")):
            self.assertTrue(install_uv_packages({"svc": dict(self.UNITS)}, PATHS, state))
            self.assertIsNone(self.owed(state))
            self.assertTrue(install_uv_packages({"svc": dict(self.UNITS)}, PATHS, state, True))
        self.assertEqual(self.owed(state)["sources"], ["uv/svc"])

    def test_invalid_restart_units_is_an_error(self):
        state = {}
        with mock.patch("tools.user.uv.run_command", return_value=(0, "", "")) as run:
            self.assertFalse(
                install_uv_packages({"svc": {"restartUnits": "launchd:x"}}, PATHS, state)
            )
        run.assert_not_called()
        self.assertNotIn("restartUnits", state)


class UvFilesTakeoverTest(unittest.TestCase):
    """uv takes over an undeclared, unmodified `files` copy in uvBin."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bin = self._tmp.name
        self.paths = {**PATHS, "uvBin": self.bin}
        self.target = str(Path(self.bin) / "claude-statusline")
        Path(self.target).write_text("#!/bin/sh\n")
        self.state = {"files": {self.target: files.record_for(self.target, "", False)}}
        self.packages = {"claude-statusline": {"binary": "claude-statusline"}}

    def tearDown(self):
        self._tmp.cleanup()

    def deploy(self, declared=(), trash=None, rc=0, conflicts=None):
        def fake_trash(path):
            Path(path).unlink()
            return True

        with (
            mock.patch("tools.user.files.trash", side_effect=trash or fake_trash) as t,
            mock.patch("tools.user.uv.run_command", return_value=(rc, "", "")) as run,
        ):
            ok = install_uv_packages(
                self.packages,
                self.paths,
                self.state,
                False,
                files.Declared(list(declared), [], conflicts),
            )
        return ok, t, [c.args[0] for c in run.call_args_list]

    def test_undeclared_unmodified_copy_is_trashed_and_installed(self):
        ok, trash, cmds = self.deploy()
        self.assertTrue(ok)
        trash.assert_called_once_with(self.target)
        self.assertEqual(self.state["files"], {})
        self.assertEqual(cmds, [["/uv/uv", "tool", "install", "claude-statusline"]])

    def test_still_declared_is_left_alone(self):
        ok, trash, cmds = self.deploy(declared=[self.target], rc=2)
        self.assertFalse(ok)
        trash.assert_not_called()
        self.assertIn(self.target, self.state["files"])
        self.assertEqual(cmds, [["/uv/uv", "tool", "install", "claude-statusline"]])

    def test_edited_copy_is_left_alone(self):
        Path(self.target).write_text("#!/bin/sh\necho mine\n")
        _, trash, cmds = self.deploy(rc=2)
        trash.assert_not_called()
        self.assertEqual(len(cmds), 1)

    def test_foreign_file_is_left_alone(self):
        self.state = {}
        _, trash, cmds = self.deploy(rc=2)
        trash.assert_not_called()
        self.assertEqual(cmds, [["/uv/uv", "tool", "install", "claude-statusline"]])

    def test_path_links_manage_is_left_alone(self):
        conflicts = mock.Mock(**{"managed_reason.return_value": "inside a links target"})
        _, trash, cmds = self.deploy(rc=2, conflicts=conflicts)
        trash.assert_not_called()
        self.assertIn(self.target, self.state["files"])
        conflicts.managed_reason.assert_any_call(self.target)

    def test_same_file_as_declared_path_is_left_alone(self):
        alias = str(Path(self.bin) / "alias")
        os.link(self.target, alias)
        _, trash, _ = self.deploy(declared=[alias], rc=2)
        trash.assert_not_called()

    def test_failed_trash_skips_install_and_keeps_record(self):
        ok, _, cmds = self.deploy(trash=OSError("nope"))
        self.assertFalse(ok)
        self.assertEqual(cmds, [])
        self.assertIn(self.target, self.state["files"])
        self.assertNotIn("claude-statusline", self.state["uv"].get("packages", {}))

    def test_without_files_declared_nothing_is_taken_over(self):
        with (
            mock.patch("tools.user.files.trash") as trash,
            mock.patch("tools.user.uv.run_command", return_value=(2, "", "")),
        ):
            install_uv_packages(self.packages, self.paths, self.state)
        trash.assert_not_called()

    def test_preview_row(self):
        changes = _diff_uv(
            self.packages, self.paths, self.state, files_declared=files.Declared([], [])
        )
        self.assertEqual(changes[0], f"  ~ take over {self.target} from files (old copy to Trash)")
        self.assertEqual(_diff_uv(self.packages, self.paths, self.state)[:1], changes[1:2])

    def test_preview_lists_takeover_once(self):
        state_file = Path(self._tmp.name) / "state.json"
        state_file.write_text(json.dumps(self.state))
        config = {
            "stateFile": str(state_file),
            "paths": self.paths,
            "uv": {"packages": self.packages},
            "files": [],
        }
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            show_diff(config, self._tmp.name, ("uv", "files"))
        self.assertIn(f"take over {self.target}", out.getvalue())
        self.assertNotIn(f"- remove {self.target}", out.getvalue())


class UvCliFilesDeclaredTest(unittest.TestCase):
    def run_sections(self, view):
        config = {"uv": {"packages": {"x": {}}}, "files": []}
        with mock.patch.object(cli, "install_uv_packages", return_value=True) as install:
            cli._run_sections(config, "/nonexistent", {}, {}, {"uv"}, view=view)
        return install.call_args.args[4]

    def test_full_run_passes_declared_targets(self):
        self.assertEqual(self.run_sections(None).targets, [])

    def test_only_run_skips_takeover(self):
        self.assertIsNone(self.run_sections(mock.Mock(**{"merged.return_value": {}})))


class UvToolDirMovedTest(unittest.TestCase):
    def test_entry_point_from_old_tool_dir_is_forced(self):
        with tempfile.TemporaryDirectory() as root:
            bin_dir, old, new = (str(Path(root) / d) for d in ("bin", "old", "new"))
            Path(bin_dir).mkdir()
            (Path(old) / "ruff" / "bin").mkdir(parents=True)
            (Path(old) / "ruff" / "bin" / "ruff").touch()
            (Path(bin_dir) / "ruff").symlink_to(Path(old) / "ruff" / "bin" / "ruff")
            (Path(bin_dir) / "foreign").touch()
            packages = {"ruff": {"binary": "ruff"}, "foreign": {"binary": "foreign"}}
            state = {
                "uv": {
                    "context": f"{bin_dir}|{old}",
                    "packages": {p: {"installed": True, "version": "latest"} for p in packages},
                }
            }
            paths = {"uv": "/uv", "uvBin": bin_dir, "uvToolDir": new}
            with mock.patch("tools.user.uv.run_command", return_value=(0, "", "")) as run:
                self.assertTrue(install_uv_packages(packages, paths, state))
        self.assertEqual(
            [c.args[0] for c in run.call_args_list],
            [
                ["/uv/uv", "tool", "install", "ruff", "--force"],
                ["/uv/uv", "tool", "install", "foreign"],
            ],
        )
        self.assertEqual(state["uv"]["context"], f"{bin_dir}|{new}")

    def test_failed_install_keeps_force_for_next_run(self):
        with tempfile.TemporaryDirectory() as root:
            bin_dir, old, new = (str(Path(root) / d) for d in ("bin", "old", "new"))
            Path(bin_dir).mkdir()
            (Path(old) / "ruff" / "bin").mkdir(parents=True)
            (Path(old) / "ruff" / "bin" / "ruff").touch()
            (Path(bin_dir) / "ruff").symlink_to(Path(old) / "ruff" / "bin" / "ruff")
            packages = {"ruff": {"binary": "ruff"}}
            state = {"uv": {"context": f"{bin_dir}|{old}", "packages": {"ruff": {}}}}
            paths = {"uv": "/uv", "uvBin": bin_dir, "uvToolDir": new}
            force = ["/uv/uv", "tool", "install", "ruff", "--force"]
            with mock.patch("tools.user.uv.run_command", return_value=(1, "", "x")) as run:
                self.assertFalse(install_uv_packages(packages, paths, state))
            self.assertEqual(run.call_args.args[0], force)
            self.assertEqual(state["uv"]["oldToolDirs"], [old])
            (Path(new) / "ruff" / "bin").mkdir(parents=True)
            (Path(new) / "ruff" / "bin" / "ruff").touch()

            def install(cmd, env):
                (Path(bin_dir) / "ruff").unlink()
                (Path(bin_dir) / "ruff").symlink_to(Path(new) / "ruff" / "bin" / "ruff")
                return 0, "", ""

            with mock.patch("tools.user.uv.run_command", side_effect=install) as run:
                self.assertTrue(install_uv_packages(packages, paths, state))
            self.assertEqual(run.call_args.args[0], force)
            self.assertNotIn("oldToolDirs", state["uv"])

    def test_bin_only_move_does_not_force_current_tool_dir(self):
        with tempfile.TemporaryDirectory() as root:
            old_bin, bin_dir, tools = (str(Path(root) / d) for d in ("old-bin", "bin", "tools"))
            Path(bin_dir).mkdir()
            (Path(tools) / "ruff" / "bin").mkdir(parents=True)
            (Path(tools) / "ruff" / "bin" / "ruff").touch()
            (Path(bin_dir) / "ruff").symlink_to(Path(tools) / "ruff" / "bin" / "ruff")
            packages = {"ruff": {"binary": "ruff"}}
            state = {
                "uv": {
                    "context": f"{old_bin}|{tools}",
                    "oldToolDirs": [tools],
                    "packages": {"ruff": {"installed": True, "version": "latest"}},
                }
            }
            paths = {"uv": "/uv", "uvBin": bin_dir, "uvToolDir": tools}
            with mock.patch("tools.user.uv.run_command", return_value=(0, "", "")) as run:
                self.assertTrue(install_uv_packages(packages, paths, state))
        self.assertEqual(run.call_args.args[0], ["/uv/uv", "tool", "install", "ruff"])
        self.assertNotIn("oldToolDirs", state["uv"])


if __name__ == "__main__":
    unittest.main()
