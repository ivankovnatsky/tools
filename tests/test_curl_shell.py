import unittest
from unittest import mock

from tools.user import curl_shell


class FakeProc:
    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        # Real pipes: the code closes stdout and reads stderr on failure.
        self.stdout = mock.MagicMock()
        self.stderr = mock.MagicMock()
        self.stderr.read.return_value = stderr

    def communicate(self, *a, **kw):
        return b"", b""

    def wait(self):
        return self.returncode


class CurlShellTest(unittest.TestCase):
    def _run(self, scripts, state, curl_rc=0, shell_rc=0):
        def fake_popen(cmd, **kw):
            if "curl" in cmd[0]:
                return FakeProc(curl_rc)
            return FakeProc(shell_rc)

        with (
            mock.patch.object(curl_shell, "system_bin", return_value="/usr/bin/curl"),
            mock.patch.object(curl_shell, "system_dir", return_value="/usr/bin"),
            mock.patch.object(curl_shell, "system_dir_optional", return_value=None),
            mock.patch.object(curl_shell.subprocess, "Popen", side_effect=fake_popen),
        ):
            return curl_shell.install_curl_shell_scripts(scripts, state)

    def test_successful_install_is_tracked(self):
        state = {}

        self.assertTrue(self._run({"https://example/x.sh": "bash"}, state))

        self.assertIn("https://example/x.sh", state["curlShell"]["installed"])

    def test_download_failure_is_not_success(self):
        # A failed deploy must not exit 0, or CI and activation both lie.
        state = {}

        self.assertFalse(self._run({"https://example/x.sh": "bash"}, state, curl_rc=1))

        self.assertEqual(state.get("curlShell", {}).get("installed", []), [])

    def test_script_failure_is_not_success(self):
        state = {}

        self.assertFalse(self._run({"https://example/x.sh": "bash"}, state, shell_rc=1))

    def test_already_installed_is_a_noop(self):
        state = {"curlShell": {"installed": ["https://example/x.sh"]}}

        self.assertTrue(self._run({"https://example/x.sh": "bash"}, state))

    def test_dropped_url_is_released_from_state(self):
        # There is no uninstall for curl|sh, so the only thing to reconcile
        # is the record — otherwise re-adding it later would never re-run.
        state = {"curlShell": {"installed": ["https://example/gone.sh"]}}

        self._run({}, state)

        self.assertEqual(state["curlShell"]["installed"], [])


if __name__ == "__main__":
    unittest.main()
