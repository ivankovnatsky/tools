import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.user import npm as npm_mod
from tools.user.npm import install_npm_packages


class NpmNoOpStateTest(unittest.TestCase):
    def test_noop_sync_sheds_legacy_binary_field(self):
        # A package already installed at the declared version needs no
        # install/removal, so npm is never shelled out to. State should still
        # be rewritten to drop a legacy "binary" field carried from old runs.
        with tempfile.TemporaryDirectory() as temp_dir:
            # Point npmBin at a dir that doesn't exist so postInstall/subpackage
            # probes are skipped and no external command runs.
            paths = {
                "nodejs": str(Path(temp_dir) / "nonexistent"),
                "npmBin": str(Path(temp_dir) / "nonexistent" / "bin"),
            }
            state = {
                "npm": {
                    "packages": {
                        "@steipete/summarize": {
                            "installed": True,
                            "version": "latest",
                            "subpackages": {},
                            "postInstall": "",
                            "binary": "summarize",
                        }
                    }
                }
            }
            packages = {"@steipete/summarize": {}}

            result = install_npm_packages(packages, paths, state, {})

            self.assertTrue(result)
            entry = state["npm"]["packages"]["@steipete/summarize"]
            self.assertNotIn("binary", entry)
            self.assertTrue(entry["installed"])
            self.assertEqual(entry["version"], "latest")


class NpmPartialFailureTest(unittest.TestCase):
    def test_install_failure_still_records_removals(self):
        # `npm uninstall -g` already succeeded; an early return on the later
        # install failure would keep the removed package in state and put the
        # next runs in a permanent retry loop.
        def fake_run(cmd, env=None, cwd=None):
            if cmd[1] == "uninstall":
                return 0, "", ""
            return 1, "", "boom"

        paths = {"nodejs": "/n", "npmBin": "/nonexistent/bin"}
        state = {
            "npm": {
                "packages": {
                    "old": {"installed": True, "version": "latest"},
                }
            }
        }

        with mock.patch.object(npm_mod, "run_command", fake_run):
            result = install_npm_packages({"new": {}}, paths, state, {})

        self.assertFalse(result)
        self.assertNotIn("old", state["npm"]["packages"])
        self.assertNotIn("new", state["npm"]["packages"])

    def test_missing_paths_fail_cleanly(self):
        state = {"npm": {"packages": {"x": {"installed": True}}}}

        result = install_npm_packages({}, {}, state, {})

        self.assertFalse(result)


class NpmUpdateTest(unittest.TestCase):
    def test_unpinned_packages_reinstall_only_with_update(self):
        paths = {"nodejs": "/n", "npmBin": "/nonexistent/bin"}
        packages = {"loose": {}, "pinned": {"version": "1.0.0"}}
        installs = []

        def fake_run(cmd, env=None, cwd=None):
            installs.append(cmd[3:])
            return 0, "", ""

        for update in (False, True):
            state = {"npm": {"packages": {}, "prefix": "/nonexistent/bin"}}
            for pkg, info in packages.items():
                state["npm"]["packages"][pkg] = {"installed": True, "version": "latest", **info}
            with mock.patch.object(npm_mod, "run_command", fake_run):
                self.assertTrue(install_npm_packages(packages, paths, state, {}, update))
        self.assertEqual(installs, [["loose"]])


if __name__ == "__main__":
    unittest.main()


class NpmMissingRuntimeTest(unittest.TestCase):
    def test_missing_npm_fails_the_section_without_raising(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = {"nodejs": str(Path(temp_dir) / "none"), "npmBin": f"{temp_dir}/bin"}
            state = {}
            with mock.patch.object(npm_mod, "log") as log:
                self.assertFalse(install_npm_packages({"a": {}}, paths, state, {}))
            self.assertIn("none/npm: No such file or directory", str(log.call_args_list))
            self.assertNotIn("a", state.get("npm", {}).get("packages", {}))


class NpmRestartUnitsTest(unittest.TestCase):
    UNIT = "launchd:org.example.svc"
    PATHS = {"nodejs": "/nonexistent/node", "npmBin": "/nonexistent/npm/bin"}

    def owed(self, state):
        return state.get("restartUnits", {}).get(self.UNIT)

    def run_install(self, state, packages, returncode=0, update=False):
        with mock.patch("tools.user.npm.run_command", return_value=(returncode, "", "err")) as run:
            ok = install_npm_packages(packages, self.PATHS, state, {}, update)
        return ok, run

    def test_install_owes_and_unchanged_does_not(self):
        state = {}
        packages = {"svc": {"restartUnits": [self.UNIT]}, "other": {}}
        self.assertTrue(self.run_install(state, packages)[0])
        self.assertEqual((self.owed(state)["sources"], self.owed(state)["seq"]), (["npm/svc"], 1))
        self.assertNotIn("restartUnits", state["npm"]["packages"]["svc"])
        ok, run = self.run_install(state, packages)
        self.assertTrue(ok)
        run.assert_not_called()
        self.assertEqual(self.owed(state)["seq"], 1)

    def test_update_reinstall_owes(self):
        state = {}
        packages = {"svc": {"restartUnits": [self.UNIT]}}
        self.run_install(state, packages)
        self.run_install(state, packages, update=True)
        self.assertEqual(self.owed(state)["seq"], 2)

    def test_pinned_package_on_update_owes_nothing(self):
        state = {}
        packages = {"svc": {"version": "1.0.0", "restartUnits": [self.UNIT]}}
        self.run_install(state, packages)
        self.run_install(state, packages, update=True)
        self.assertEqual(self.owed(state)["seq"], 1)

    def test_failed_install_owes_nothing(self):
        state = {}
        packages = {"svc": {"restartUnits": [self.UNIT]}}
        self.assertFalse(self.run_install(state, packages, returncode=1)[0])
        self.assertIsNone(self.owed(state))

    def test_invalid_restart_units_is_an_error_and_skips_only_that_entry(self):
        state = {}
        packages = {"bad": {"restartUnits": "launchd:x"}, "good": {}}
        ok, run = self.run_install(state, packages)
        self.assertFalse(ok)
        self.assertNotIn("bad", run.call_args[0][0])
        self.assertIn("good", run.call_args[0][0])
        self.assertNotIn("restartUnits", state)
        self.assertEqual(set(state["npm"]["packages"]), {"good"})
