import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
