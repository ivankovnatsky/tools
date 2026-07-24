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


if __name__ == "__main__":
    unittest.main()
