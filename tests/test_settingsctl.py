import json
import unittest
from unittest import mock

from tools import cli, privilege
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


class DriftKeysTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(settingsctl.sys, "platform", "darwin")
        patch.start()
        self.addCleanup(patch.stop)
        self.state_exists = True
        self.commands = []

    def run_command(self, cmd, env=None, cwd=None):
        self.commands.append(cmd[1:])
        return 0, "", ""

    def _call(self, fn, raw):
        with mock.patch.object(settingsctl, "run_command", self.run_command):
            return fn(raw, which=lambda _: "/bin/settings", exists=lambda _: self.state_exists)

    def diff(self, raw):
        return self._call(settingsctl.diff_settingsctl, raw)

    def install(self, raw):
        return self._call(settingsctl.install_settingsctl, raw)

    def test_clean_host_runs_nothing(self):
        raw = {"appearanceInit": True}
        self.assertEqual(self.diff(raw), [])
        self.assertTrue(self.install(raw))
        self.assertEqual(self.commands, [])

    def test_appearance_init_only_without_state_file(self):
        self.state_exists = False
        self.assertEqual(self.diff({"appearanceInit": True}), ["  ~ settings appearance --init"])
        self.assertTrue(self.install({"appearanceInit": True}))
        self.assertEqual(self.commands, [["appearance", "--init"]])

    def test_appearance_init_false_does_nothing(self):
        self.state_exists = False
        self.assertEqual(self.diff({"appearanceInit": False}), [])

    def test_bool_keys_are_typed(self):
        self.assertEqual(
            self.diff({"appearanceInit": "yes"}), ["  ! appearanceInit: must be true or false"]
        )
        self.assertFalse(self.install({"appearanceInit": 1}))
        self.assertEqual(self.commands, [])


HOST = {
    "hostName": "Ivans-Mac-mini",
    "localHostName": "Ivans-Mac-mini",
    "timeZone": "Europe/Uzhgorod",
    "sleep": {"computer": "never", "display": 10, "disk": "never"},
    "remoteLogin": True,
}


class FakeSettings:
    """`settings ... get --json` answers and the set commands it receives."""

    def __init__(self):
        self.names = {"hostName": "Ivans-Mac-mini", "localHostName": "Ivans-Mac-mini"}
        self.names["computerName"] = "Mac"
        self.zone, self.matches = "Etc/Linked", True
        self.sleep = {"AC Power": {"computer": "never", "display": 10, "disk": "never"}}
        self.sshd = {"remoteLogin": True, "enabled": True, "loaded": True}
        self.browser = {"defaultBrowser": "firefox", "browsers": ["safari", "firefox"]}
        self.calls = []
        self.fail = set()

    def run(self, cmd, env=None, cwd=None):
        args = cmd[1:]
        self.calls.append(args)
        if args[0] in self.fail:
            return 1, "", "boom"
        if args[-1] == "--json" and args[1] == "get":
            data = {
                "hostname": self.names,
                "timezone": {"timeZone": self.zone, "matches": self.matches},
                "sleep": {"sources": self.sleep},
                "remotelogin": self.sshd,
                "defaultbrowser": self.browser,
            }[args[0]]
            return 0, json.dumps(data), ""
        return 0, "", ""

    def sets(self):
        return [c for c in self.calls if c[1:2] == ["set"]]


class HostKeysTest(unittest.TestCase):
    def setUp(self):
        for patch in (
            mock.patch.object(settingsctl.sys, "platform", "darwin"),
            mock.patch.object(privilege, "UNDER_SUDO", False),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.fake = FakeSettings()

    def _kw(self, root, which="/bin/settings"):
        return {
            "which": lambda _: which,
            "run": self.fake.run,
            "root": root,
            "exists": lambda _: True,
            "executable": lambda p: p == "/uv/settings",
            "paths": {"uvBin": "/uv"},
        }

    def diff(self, raw, root=True, **kw):
        return settingsctl.diff_settingsctl(raw, **{**self._kw(root), **kw})

    def install(self, raw, root=True, **kw):
        return settingsctl.install_settingsctl(raw, **{**self._kw(root), **kw})

    def test_clean_host_sets_nothing(self):
        raw = {**HOST, "defaultBrowser": "firefox"}
        self.assertEqual(self.diff(raw), [])
        self.assertTrue(self.install(raw))
        self.assertTrue(self.install(raw, root=False))
        self.assertEqual(self.fake.sets(), [])

    def test_root_drift(self):
        self.fake.names["hostName"] = "old"
        self.fake.matches, self.fake.zone = False, "Europe/Berlin"
        self.fake.sleep["AC Power"]["display"] = 5
        self.fake.sshd = {"remoteLogin": False, "enabled": True, "loaded": False}
        self.assertEqual(
            self.diff(HOST),
            [
                "  ~ settings hostname set --host-name Ivans-Mac-mini (now hostName 'old')",
                "  ~ settings timezone set Europe/Uzhgorod (now 'Europe/Berlin')",
                "  ~ settings sleep set --display 10 (now display 5)",
                "  ~ settings remotelogin set on (now enabled on, loaded off)",
            ],
        )
        self.assertTrue(self.install(HOST))
        self.assertEqual(
            self.fake.sets(),
            [
                ["hostname", "set", "--host-name", "Ivans-Mac-mini"],
                ["timezone", "set", "Europe/Uzhgorod"],
                ["sleep", "set", "--display", "10"],
                ["remotelogin", "set", "on"],
            ],
        )

    def test_remote_login_off_needs_disabled_and_unloaded(self):
        self.fake.sshd = {"remoteLogin": False, "enabled": False, "loaded": True}
        self.assertTrue(self.install({"remoteLogin": False}))
        self.assertEqual(self.fake.sets(), [["remotelogin", "set", "off"]])

    def test_user_previews_root_keys_but_never_sets_them(self):
        self.fake.names["hostName"] = "old"
        self.assertEqual(self.diff(HOST, root=False), [])
        self.assertTrue(self.install(HOST, root=False))
        self.assertEqual(self.fake.sets(), [])

    def test_root_never_sets_user_keys(self):
        self.fake.browser["defaultBrowser"] = "safari"
        raw = {**HOST, "defaultBrowser": "firefox", "loginItems": ["A"]}
        self.assertEqual(self.diff(raw), [])
        self.assertTrue(self.install(raw))
        self.assertEqual(self.fake.calls[-1][:2], ["remotelogin", "get"])
        self.assertEqual(self.fake.sets(), [])

    def test_browser_drift_as_user(self):
        self.fake.browser["defaultBrowser"] = "safari"
        raw = {"defaultBrowser": "firefox"}
        self.assertEqual(
            self.diff(raw, root=False),
            ["  ~ settings defaultbrowser set firefox (now 'safari')"],
        )
        self.assertTrue(self.install(raw, root=False))
        self.assertEqual(self.fake.sets(), [["defaultbrowser", "set", "firefox"]])

    def test_uninstalled_browser_is_an_error(self):
        raw = {"defaultBrowser": "chrome"}
        self.assertEqual(len(self.diff(raw, root=False)), 1)
        self.assertFalse(self.install(raw, root=False))
        self.assertEqual(self.fake.sets(), [])

    def test_read_failure_fails_root_phase(self):
        self.fake.fail.add("timezone")
        self.assertTrue(self.diff({"timeZone": "Etc/Linked"})[0].startswith("  ? timeZone"))
        self.assertFalse(self.install({"timeZone": "Etc/Linked"}))

    def test_root_never_uses_its_own_path(self):
        kw = {"which": lambda _: "/root/bin/settings", "paths": None}
        rows = self.diff(HOST, **kw)
        self.assertEqual(len(rows), 1)
        self.assertIn("`settings` not found", rows[0])
        self.assertFalse(self.install(HOST, **kw))
        self.assertEqual(self.fake.calls, [])

    def test_root_finds_settings_in_uv_bin(self):
        self.assertTrue(self.install(HOST))
        self.assertTrue(all(c for c in self.fake.calls))

    def test_explicit_path(self):
        self.assertTrue(self.install({**HOST, "path": "/uv/settings"}, paths=None))
        self.assertFalse(self.install({**HOST, "path": "/missing/settings"}))
        self.assertEqual(settingsctl.parse({"path": "relative"})[0], {})

    def test_invalid_values_hold_the_section(self):
        for key, value in (
            ("sleep", {"computer": 0}),
            ("sleep", {"harddisk": "never"}),
            ("remoteLogin", "yes"),
            ("hostName", "-x"),
            ("timeZone", ""),
        ):
            with self.subTest(key=key, value=value):
                spec, errors = settingsctl.parse({**HOST, key: value})
                self.assertEqual(spec, {})
                self.assertEqual(len(errors), 1, errors)


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
        self._deploy({"settingsctl": MINI}).assert_called_once_with(MINI, None)

    def test_root_phase_runs_only_with_root_keys(self):
        with (
            mock.patch.object(cli, "load_json", return_value={}),
            mock.patch.object(cli, "save_json"),
            mock.patch.object(cli, "state_lock"),
            mock.patch.object(cli, "install_settingsctl", return_value=True) as inst,
        ):
            self.assertTrue(cli._deploy({"settingsctl": MINI}, "/tmp", root=True))
            inst.assert_not_called()
            raw = {**MINI, "timeZone": "Etc/Linked"}
            paths = {"uvBin": "/x"}
            self.assertTrue(cli._deploy({"settingsctl": raw, "paths": paths}, "/tmp", root=True))
            inst.assert_called_once_with(raw, paths)

    def test_root_section(self):
        self.assertIn("settingsctl", privilege.ROOT_SECTIONS)


if __name__ == "__main__":
    unittest.main()
