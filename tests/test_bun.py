import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
