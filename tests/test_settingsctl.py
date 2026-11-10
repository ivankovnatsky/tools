import unittest
from unittest import mock

from tools import cli
from tools.config import TOP_LEVEL_KEYS
from tools.user import settingsctl

MINI = {
    "fullDiskAccess": ["kitty", "Terminal"],
    "accessibility": ["Hammerspoon"],
    "loginItems": ["Hammerspoon", "LosslessSwitcher"],
}


class SettingsctlTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(settingsctl.sys, "platform", "darwin")
        patch.start()
        self.addCleanup(patch.stop)

    def _install(self, raw, rc=0):
        commands = []

        def run(cmd, env=None, cwd=None):
            commands.append(cmd)
            return rc, "", ""

        with mock.patch.object(settingsctl, "run_command", run):
            ok = settingsctl.install_settingsctl(raw, which=lambda _: "/bin/settings")
        return ok, commands

    def test_runs_each_declared_list(self):
        ok, commands = self._install(MINI)
        self.assertTrue(ok)
        self.assertEqual(
            commands,
            [
                ["/bin/settings", "fulldiskaccess", "--enable", "kitty,Terminal"],
                ["/bin/settings", "accessibility", "set", "Hammerspoon"],
                ["/bin/settings", "login", "set", "Hammerspoon,LosslessSwitcher"],
            ],
        )

    def test_failure_fails_deploy(self):
        ok, commands = self._install({"accessibility": ["A"]}, rc=1)
        self.assertFalse(ok)
        self.assertEqual(len(commands), 1)

    def test_diff_shows_commands(self):
        self.assertEqual(
            settingsctl.diff_settingsctl(MINI),
            [
                "  ~ settings fulldiskaccess --enable 'kitty,Terminal'",
                "  ~ settings accessibility set 'Hammerspoon'",
                "  ~ settings login set 'Hammerspoon,LosslessSwitcher'",
            ],
        )

    def test_invalid_config_runs_nothing(self):
        raw = {"accessibility": ["A,B"], "camera": []}
        self.assertEqual(len(settingsctl.diff_settingsctl(raw)), 2)
        ok, commands = self._install(raw)
        self.assertFalse(ok)
        self.assertEqual(commands, [])

    def test_empty_list_is_rejected(self):
        for key in settingsctl.KEYS:
            with self.subTest(key=key):
                raw = {**MINI, key: []}
                self.assertEqual(len(settingsctl.diff_settingsctl(raw)), 1)
                ok, commands = self._install(raw)
                self.assertFalse(ok)
                self.assertEqual(commands, [])

    def test_missing_binary(self):
        self.assertFalse(settingsctl.install_settingsctl(MINI, which=lambda _: None))


class WiringTest(unittest.TestCase):
    def _deploy(self, config):
        with (
            mock.patch.object(cli, "load_json", return_value={}),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(cli, "save_json"),
            mock.patch.object(cli, "state_lock"),
            mock.patch.object(cli, "install_settingsctl", return_value=True) as inst,
        ):
            self.assertTrue(cli._deploy(config, "/tmp"))
        return inst

    def test_known_key(self):
        self.assertIn("settingsctl", TOP_LEVEL_KEYS)

    def test_deploy_skips_absent_section(self):
        self._deploy({}).assert_not_called()

    def test_deploy_runs_declared_section(self):
        self._deploy({"settingsctl": MINI}).assert_called_once_with(MINI)


if __name__ == "__main__":
    unittest.main()
