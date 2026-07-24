import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.user import bun as bun_mod
from tools.user.bun import install_bun_packages


class BunNoOpStateTest(unittest.TestCase):
    def test_noop_sync_sheds_legacy_binary_field(self):
        # Already-installed at the declared version: no install/removal, so bun
        # is never invoked, but state should still shed a legacy "binary" field.
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = {
                "bun": str(Path(temp_dir) / "nonexistent"),
                "nodejs": str(Path(temp_dir) / "nonexistent"),
            }
            state = {
                "bun": {
                    "packages": {
                        "some-tool": {
                            "installed": True,
                            "version": "latest",
                            "binary": "some-tool",
                        }
                    }
                }
            }
            packages = {"some-tool": {}}

            result = install_bun_packages(packages, paths, state, {})

            self.assertTrue(result)
            entry = state["bun"]["packages"]["some-tool"]
            self.assertNotIn("binary", entry)
            self.assertTrue(entry["installed"])


class BunPartialFailureTest(unittest.TestCase):
    def test_install_failure_still_records_removals(self):
        # The removal above already mutated the system; an early return would
        # leave the removed package in state and the next run would retry
        # `bun remove -g` on something already gone — forever.
        def fake_run(cmd, env=None, cwd=None):
            if cmd[1] == "remove":
                return 0, "", ""
            return 1, "", "boom"

        paths = {"bun": "/b", "nodejs": "/n", "bunBin": "/bin"}
        state = {"bun": {"packages": {"old": {"installed": True, "version": "latest"}}}}

        with mock.patch.object(bun_mod, "run_command", fake_run):
            result = install_bun_packages({"new": {}}, paths, state, {})

        self.assertFalse(result)
        self.assertNotIn("old", state["bun"]["packages"])
        # The failed install must not be recorded as installed either.
        self.assertNotIn("new", state["bun"]["packages"])

    def test_missing_paths_fail_cleanly(self):
        # A machine with tracked packages but no paths must get a clear
        # failure, not a KeyError traceback mid-deploy.
        state = {"bun": {"packages": {"x": {"installed": True}}}}

        result = install_bun_packages({}, {}, state, {})

        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
