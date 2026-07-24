import unittest
from unittest import mock

from tools.user import brew


class BrewBootstrapTest(unittest.TestCase):
    """The installer must be fetched first, then handed to bash as the -c
    script body. Passing the literal `$(curl ...)` makes bash substitute and
    word-split the script, then execute its first token ("#!/bin/bash") as a
    command name — rc 127, every time."""

    def _bootstrap(self, fake_run):
        with (
            mock.patch.object(brew.sys, "platform", "darwin"),
            mock.patch.object(brew, "system_bin", side_effect=lambda n: f"/usr/bin/{n}"),
            mock.patch.object(brew, "run_command", fake_run),
            mock.patch.object(brew, "_brew_bin", return_value="/opt/homebrew/bin/brew"),
        ):
            return brew._bootstrap_brew()

    def test_fetches_script_then_runs_body(self):
        script = "#!/bin/bash\necho install\n"
        calls = []

        def fake_run(cmd, env=None, cwd=None):
            calls.append(cmd)
            if cmd[0] == "/usr/bin/curl":
                return 0, script, ""
            return 0, "", ""

        result = self._bootstrap(fake_run)

        self.assertEqual(result, "/opt/homebrew/bin/brew")
        self.assertEqual(calls[0][:2], ["/usr/bin/curl", "-fsSL"])
        self.assertEqual(calls[1][:2], ["/usr/bin/bash", "-c"])
        # bash receives the script body, never an unexpanded substitution.
        self.assertEqual(calls[1][2], script)
        self.assertNotIn("$(", calls[1][2])

    def test_failed_download_never_runs_bash(self):
        calls = []

        def fake_run(cmd, env=None, cwd=None):
            calls.append(cmd)
            return 1, "", "curl: (6) no host"

        result = self._bootstrap(fake_run)

        self.assertIsNone(result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "/usr/bin/curl")

    def test_empty_download_never_runs_bash(self):
        # rc 0 with an empty body must not hand bash an empty script and
        # report success.
        def fake_run(cmd, env=None, cwd=None):
            return 0, "", ""

        result = self._bootstrap(fake_run)

        self.assertIsNone(result)


class BrewReconcileTest(unittest.TestCase):
    def _reconcile(self, config, state, fake_run):
        with (
            mock.patch.object(brew.sys, "platform", "darwin"),
            mock.patch.object(brew, "_brew_bin", return_value="/brew"),
            mock.patch.object(brew, "run_command", fake_run),
        ):
            return brew.install_brew_packages(config, state)

    def test_successful_install_records_ownership(self):
        calls = []

        def fake_run(cmd, env=None, cwd=None):
            calls.append(cmd)
            return 0, "", ""

        state = {}
        result = self._reconcile({"brews": ["b", "a"]}, state, fake_run)

        self.assertTrue(result)
        self.assertIn(["/brew", "install", "a", "b"], calls)
        self.assertEqual(state["brew"]["brews"], ["a", "b"])

    def test_partial_batch_install_records_what_landed(self):
        # brew installs targets in order; an earlier one can land before a
        # later failure. Those must not stay unowned forever.
        def fake_run(cmd, env=None, cwd=None):
            if cmd[1] == "install":
                return 1, "", "boom"
            if cmd[1] == "list":
                return 0, "a\n", ""
            return 0, "", ""

        state = {}
        result = self._reconcile({"brews": ["a", "b"]}, state, fake_run)

        self.assertFalse(result)
        self.assertEqual(state["brew"]["brews"], ["a"])

    def test_failed_uninstall_with_unqueryable_brew_keeps_ownership(self):
        # "Cannot query" must not read as "nothing installed": the package is
        # still on disk, so dropping ownership would orphan it forever.
        def fake_run(cmd, env=None, cwd=None):
            if cmd[1] in ("uninstall", "list"):
                return 1, "", "boom"
            return 0, "", ""

        state = {"version": 2, "brew": {"brews": ["x"], "casks": [], "taps": [], "masApps": {}}}
        result = self._reconcile({"brews": []}, state, fake_run)

        self.assertFalse(result)
        self.assertEqual(state["brew"]["brews"], ["x"])

    def test_successful_uninstall_releases_ownership(self):
        def fake_run(cmd, env=None, cwd=None):
            return 0, "", ""

        state = {"version": 2, "brew": {"brews": ["x"], "casks": [], "taps": [], "masApps": {}}}
        result = self._reconcile({"brews": []}, state, fake_run)

        self.assertTrue(result)
        self.assertEqual(state["brew"]["brews"], [])

    def test_mas_rename_under_same_id_updates_state(self):
        # Same app id under a new name produces no install/remove work, but
        # state must adopt the new name instead of keeping the stale key.
        state = {
            "version": 2,
            "brew": {"brews": [], "casks": [], "taps": [], "masApps": {"Old": 1}},
        }

        def fake_run(cmd, env=None, cwd=None):
            raise AssertionError("no brew command should run")

        result = self._reconcile({"masApps": {"New": 1}}, state, fake_run)

        self.assertTrue(result)
        self.assertEqual(state["brew"]["masApps"], {"New": 1})

    def test_non_darwin_is_a_noop(self):
        with mock.patch.object(brew.sys, "platform", "linux"):
            self.assertTrue(brew.install_brew_packages({"brews": ["a"]}, {}))


if __name__ == "__main__":
    unittest.main()
