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
    def _run(self, scripts, state, curl_rc=0, shell_rc=0, uninstall_rc=0):
        self.uninstalls = []

        def fake_popen(cmd, **kw):
            if "curl" in cmd[0]:
                return FakeProc(curl_rc)
            return FakeProc(shell_rc)

        def fake_run(cmd, env=None, cwd=None):
            self.uninstalls.append(cmd)
            return uninstall_rc, "", "boom" if uninstall_rc else ""

        with (
            mock.patch.object(curl_shell, "system_bin", return_value="/usr/bin/curl"),
            mock.patch.object(curl_shell, "system_dir", return_value="/usr/bin"),
            mock.patch.object(curl_shell, "system_dir_optional", return_value=None),
            mock.patch.object(curl_shell.subprocess, "Popen", side_effect=fake_popen),
            mock.patch.object(curl_shell, "run_command", side_effect=fake_run),
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
        self.assertEqual(self.uninstalls, [])

    def test_install_records_uninstall_command(self):
        state = {}
        scripts = {"https://example/x.sh": {"shell": "bash", "uninstall": "rm -rf ~/.x"}}

        self.assertTrue(self._run(scripts, state))

        self.assertEqual(state["curlShell"]["installed"], ["https://example/x.sh"])
        self.assertEqual(state["curlShell"]["uninstall"], {"https://example/x.sh": "rm -rf ~/.x"})

    def test_failed_install_records_no_uninstall(self):
        state = {}
        scripts = {"https://example/x.sh": {"shell": "bash", "uninstall": "rm -rf ~/.x"}}

        self.assertFalse(self._run(scripts, state, shell_rc=1))

        self.assertNotIn("uninstall", state.get("curlShell", {}))

    def test_uninstall_added_to_installed_entry_is_recorded(self):
        # The config entry is gone by removal time, so an uninstall added later
        # must reach state without a reinstall.
        state = {"curlShell": {"installed": ["https://example/x.sh"]}}
        scripts = {"https://example/x.sh": {"shell": "bash", "uninstall": "rm ~/.x"}}

        self.assertTrue(self._run(scripts, state))

        self.assertEqual(state["curlShell"]["uninstall"], {"https://example/x.sh": "rm ~/.x"})

    def test_uninstall_dropped_from_installed_entry_is_cleared(self):
        state = {
            "curlShell": {
                "installed": ["https://example/x.sh"],
                "uninstall": {"https://example/x.sh": "rm ~/.x"},
            }
        }

        self.assertTrue(self._run({"https://example/x.sh": "bash"}, state))

        self.assertNotIn("uninstall", state["curlShell"])

    def test_dropped_url_runs_recorded_uninstall(self):
        state = {
            "curlShell": {
                "installed": ["https://example/gone.sh"],
                "uninstall": {"https://example/gone.sh": "rm -rf ~/.gone"},
            }
        }

        self.assertTrue(self._run({}, state))

        self.assertEqual(self.uninstalls, [["/bin/sh", "-c", "rm -rf ~/.gone"]])
        self.assertEqual(state["curlShell"]["installed"], [])
        self.assertNotIn("uninstall", state["curlShell"])

    def test_failed_uninstall_stays_tracked_for_retry(self):
        record = {
            "installed": ["https://example/gone.sh"],
            "uninstall": {"https://example/gone.sh": "rm -rf ~/.gone"},
        }
        state = {"curlShell": dict(record)}

        self.assertFalse(self._run({}, state, uninstall_rc=1))

        self.assertEqual(state["curlShell"], record)

    def test_malformed_entry_refuses_section(self):
        # A typo must not read as "entry removed" and run its uninstall.
        state = {
            "curlShell": {
                "installed": ["https://example/x.sh"],
                "uninstall": {"https://example/x.sh": "rm ~/.x"},
            }
        }
        scripts = {"https://example/x.sh": {"shel": "bash"}}

        self.assertFalse(self._run(scripts, state))

        self.assertEqual(self.uninstalls, [])
        self.assertEqual(state["curlShell"]["installed"], ["https://example/x.sh"])

    def test_unknown_shell_refuses_section(self):
        state = {"curlShell": {"installed": ["https://example/old.sh"]}}
        popen = mock.Mock(side_effect=FileNotFoundError(2, "No such file", "nosuchshell"))
        with (
            mock.patch.object(curl_shell, "system_bin", return_value="/usr/bin/curl"),
            mock.patch.object(curl_shell, "system_dir", return_value="/usr/bin"),
            mock.patch.object(curl_shell, "system_dir_optional", return_value=None),
            mock.patch.object(curl_shell.shutil, "which", return_value=None),
            mock.patch.object(curl_shell.subprocess, "Popen", popen),
        ):
            result = curl_shell.install_curl_shell_scripts(
                {"https://example/x.sh": "nosuchshell"}, state
            )

        self.assertFalse(result)
        popen.assert_not_called()
        self.assertEqual(state["curlShell"]["installed"], ["https://example/old.sh"])

    def test_non_mapping_section_refuses_and_keeps_state(self):
        for raw in ([], False, "x"):
            with self.subTest(raw=raw):
                record = {
                    "installed": ["https://example/x.sh"],
                    "uninstall": {"https://example/x.sh": "rm ~/.x"},
                }
                state = {"curlShell": dict(record)}

                self.assertFalse(self._run(raw, state))

                self.assertEqual(self.uninstalls, [])
                self.assertEqual(state["curlShell"], record)


class CurlShellDiffTest(unittest.TestCase):
    def test_rows(self):
        state = {
            "curlShell": {
                "installed": ["https://example/a.sh", "https://example/b.sh"],
                "uninstall": {"https://example/a.sh": "rm ~/.a"},
            }
        }

        rows = curl_shell.diff_curl_shell({"https://example/new.sh": "bash"}, state)

        self.assertEqual(
            rows,
            [
                "  + install https://example/new.sh",
                "  - uninstall https://example/a.sh (runs: rm ~/.a)",
                "  ~ forget https://example/b.sh (no uninstall; releases tracking)",
            ],
        )

    def test_malformed_entry_shows_only_errors(self):
        state = {"curlShell": {"installed": ["https://example/a.sh"]}}

        rows = curl_shell.diff_curl_shell({"https://example/a.sh": {"uninstall": "x"}}, state)

        self.assertEqual(
            rows, ["  ! curlShell.https://example/a.sh: shell must be a non-empty string"]
        )


if __name__ == "__main__":
    unittest.main()
