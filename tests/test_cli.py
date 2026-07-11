import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.cli import _deploy
from tools.diff import show_diff


class GoCleanupRetryTest(unittest.TestCase):
    def test_pending_cleanup_runs_with_no_configured_packages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "state.json"
            state_file.write_text(json.dumps({"go": {"packages": {}, "cleanupPending": True}}))
            config = {
                "stateFile": str(state_file),
                "paths": {"goPath": str(Path(temp_dir) / ".go")},
                "go": {"packages": {}},
            }

            with patch("tools.cli.install_go_packages", return_value=True) as install:
                success = _deploy(config, temp_dir, ("go",))

            self.assertTrue(success)
            install.assert_called_once()

    def test_pending_cleanup_is_reported_as_a_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "state.json"
            state_file.write_text(json.dumps({"go": {"packages": {}, "cleanupPending": True}}))
            config = {
                "stateFile": str(state_file),
                "paths": {"goPath": str(Path(temp_dir) / ".go")},
                "go": {"packages": {}},
            }

            self.assertFalse(show_diff(config, temp_dir, ("go",)))


if __name__ == "__main__":
    unittest.main()
