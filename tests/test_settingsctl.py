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


class SettingsctlCase(unittest.TestCase):
    def setUp(self):
        self.applied = {}
        self.after = {}
        self.after_failure = False
        self.login_items = ""
        for patch in (
            mock.patch.object(settingsctl.sys, "platform", "darwin"),
            mock.patch.object(settingsctl, "_read_state", self.applied.get),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def fake_run(self, cmd, env=None, cwd=None):
        self.commands.append(cmd)
        if cmd[1:] == ["login", "list"]:
            return 0, self.login_items, ""
        rc = next((rc for verb, rc in self.fail.items() if verb in cmd), self.rc)
        if rc == 0 or self.after_failure:
            self.applied.update(self.after.pop(cmd[1], {}))
        if "login_items" in self.after:
            self.login_items = self.after.pop("login_items")
        return rc, "", ""

    def _install(self, raw, rc=0, state=None, fail=None):
        self.commands, self.rc, self.fail = [], rc, fail or {}
        with mock.patch.object(settingsctl, "run_command", self.fake_run):
            ok = settingsctl.install_settingsctl(raw, which=lambda _: "/bin/settings", state=state)
        return ok, [c for c in self.commands if c[1:] != ["login", "list"]]

    def diff(self, raw, which=lambda _: "/bin/settings", state=None):
        self.commands, self.rc, self.fail = [], 0, {}
        with mock.patch.object(settingsctl, "run_command", self.fake_run):
            return settingsctl.diff_settingsctl(
                raw, which=which, executable=lambda _: False, state=state
            )


class SettingsctlTest(SettingsctlCase):
    def test_runs_each_declared_list(self):
        ok, commands = self._install(MINI)
        self.assertTrue(ok)
        self.assertEqual(
            commands,
            [
                ["/bin/settings", "fulldiskaccess", "set", "kitty,Terminal"],
                ["/bin/settings", "accessibility", "set", "Hammerspoon"],
                ["/bin/settings", "login", "set", "Hammerspoon,LosslessSwitcher"],
            ],
        )

    def test_failure_fails_deploy(self):
        ok, commands = self._install({"accessibility": ["A"]}, rc=1)
        self.assertFalse(ok)
        self.assertEqual(len(commands), 1)

    def test_diff_shows_commands(self):
        self.applied["fda-enabled"] = "Terminal,determinate-nixd,kitty"
        self.login_items = "Hammerspoon\n"
        self.assertEqual(
            self.diff(MINI),
            [
                "  ~ settings fulldiskaccess --enable 'kitty,Terminal'"
                " (now 'Terminal,determinate-nixd,kitty') (leaves 'determinate-nixd' alone)",
                "  ~ settings accessibility set 'Hammerspoon'",
                "  ~ settings login set 'Hammerspoon,LosslessSwitcher' (now 'Hammerspoon')",
            ],
        )

    def test_applied_lists_are_clean(self):
        self.applied["fda-enabled"] = "Terminal,kitty"
        self.applied["accessibility-enabled"] = "Hammerspoon"
        self.login_items = "LosslessSwitcher\nHammerspoon\n"
        self.assertEqual(self.diff(MINI), [])
        ok, commands = self._install(MINI)
        self.assertTrue(ok)
        self.assertEqual(commands, [])

    def test_app_paths_compare_by_name(self):
        self.applied["accessibility-enabled"] = "Muxy"
        self.login_items = "Home App\n"
        raw = {
            "accessibility": ["Muxy=/Applications/Muxy.app"],
            "loginItems": ["/Users/me/Applications/Home App.app"],
        }
        self.assertEqual(self.diff(raw), [])

    def test_no_login_items_reads_as_empty(self):
        self.login_items = "No login items found.\n"
        self.assertEqual(
            self.diff({"loginItems": ["A"]}), ["  ~ settings login set 'A' (now none)"]
        )

    def test_missing_cli_is_reported_for_any_user_key(self):
        rows = self.diff({"accessibility": ["A"]}, which=lambda _: None)
        self.assertIn("`settings` not found", rows[0])

    def test_invalid_config_runs_nothing(self):
        raw = {"accessibility": ["A,B"], "camera": []}
        self.assertEqual(len(settingsctl.diff_settingsctl(raw)), 2)
        ok, commands = self._install(raw)
        self.assertFalse(ok)
        self.assertEqual(commands, [])

    def test_empty_list_is_rejected(self):
        for key in set(settingsctl.KEYS) - set(settingsctl.LIST_KEYS):
            with self.subTest(key=key):
                raw = {**MINI, key: []}
                self.assertEqual(len(settingsctl.diff_settingsctl(raw)), 1)
                ok, commands = self._install(raw)
                self.assertFalse(ok)
                self.assertEqual(commands, [])

    def test_missing_binary(self):
        self.assertFalse(settingsctl.install_settingsctl(MINI, which=lambda _: None))


S = "/bin/settings"


class ListRecordTest(SettingsctlCase):
    """state.json records what tools granted; recorded names that leave config are revoked."""

    def assert_preview_matches(self, raw, state, commands):
        """Rows from diff are exactly the commands deploy ran, in order."""
        rows = self.diff(raw, state=state)
        shown = [r.split(" (now ")[0].split(" (leaves ")[0] for r in rows]
        ran = [
            "  {} settings {}".format(
                "-" if c[2] in ("revoke", "remove") else "~",
                " ".join(c[1:-1]) + " " + repr(c[-1])
                if c[1:3] != ["login", "remove"]
                else settingsctl._shown(c[1:]),
            )
            for c in commands
        ]
        self.assertEqual(shown, ran)

    def test_records_granted_names(self):
        state = {}
        ok, _ = self._install(MINI, state=state)
        self.assertTrue(ok)
        self.assertEqual(
            state["settingsctl"],
            {
                "fullDiskAccess": ["Terminal", "kitty"],
                "accessibility": ["Hammerspoon"],
                "loginItems": ["Hammerspoon", "LosslessSwitcher"],
            },
        )

    def test_adopts_already_applied_lists(self):
        self.applied["fda-enabled"] = "Terminal,kitty"
        self.applied["accessibility-enabled"] = "Hammerspoon"
        self.login_items = "LosslessSwitcher\nHammerspoon\n"
        state = {}
        ok, commands = self._install(MINI, state=state)
        self.assertTrue(ok)
        self.assertEqual(commands, [])
        self.assertEqual(state["settingsctl"]["accessibility"], ["Hammerspoon"])

    def test_adoption_does_not_import_settingsctl_state(self):
        self.applied["accessibility-enabled"] = "Old,Hammerspoon"
        state = {}
        ok, commands = self._install({"accessibility": ["Hammerspoon"]}, state=state)
        self.assertTrue(ok)
        self.assertEqual(commands, [[S, "accessibility", "enable", "Hammerspoon"]])
        self.assertEqual(state["settingsctl"], {"accessibility": ["Hammerspoon"]})

    def removed_state(self):
        return {
            "settingsctl": {
                "fullDiskAccess": ["kitty"],
                "accessibility": ["A", "B"],
                "loginItems": ["L1", "L2"],
            }
        }

    def test_removed_keys_revoke_only_recorded_names(self):
        self.applied["fda-enabled"] = "kitty,other"
        self.login_items = "L1\nHandAdded\n"
        expected = [
            [S, "fulldiskaccess", "revoke", "kitty"],
            [S, "accessibility", "revoke", "A,B"],
            [S, "login", "remove", "L1"],
        ]
        for raw in (None, {}, {"fullDiskAccess": [], "accessibility": [], "loginItems": []}):
            with self.subTest(raw=raw):
                self.assertTrue(settingsctl.has_work(raw, self.removed_state()))
                self.assert_preview_matches(raw, self.removed_state(), expected)
                state = self.removed_state()
                ok, commands = self._install(raw, state=state)
                self.assertTrue(ok)
                self.assertEqual(commands, expected)
                self.assertNotIn("settingsctl", state)
                self.assertFalse(settingsctl.has_work(None, state))

    def test_failed_revoke_stays_recorded(self):
        self.login_items = "L1\nL2\n"
        state = self.removed_state()
        ok, _ = self._install(None, state=state, fail={"accessibility": 1, "L2": 1})
        self.assertFalse(ok)
        self.assertEqual(state["settingsctl"], {"accessibility": ["A", "B"], "loginItems": ["L2"]})
        self.assertEqual(
            self.diff(None, state=state),
            ["  - settings accessibility revoke 'A,B'", "  - settings login remove L2"],
        )

    def test_shrunk_list_revokes_only_dropped_names(self):
        state = {"settingsctl": {"accessibility": ["A", "B"], "fullDiskAccess": ["k", "t"]}}
        self.applied["accessibility-enabled"] = "A,B"
        self.applied["fda-enabled"] = "k"
        raw = {"accessibility": ["A"], "fullDiskAccess": ["k"]}
        expected = [[S, "fulldiskaccess", "revoke", "t"], [S, "accessibility", "set", "A"]]
        self.assert_preview_matches(raw, state, expected)
        ok, commands = self._install(raw, state=state)
        self.assertTrue(ok)
        self.assertEqual(commands, expected)
        self.assertEqual(state["settingsctl"], {"accessibility": ["A"], "fullDiskAccess": ["k"]})

    def test_unrecorded_settingsctl_names_are_left_alone(self):
        state = {"settingsctl": {"accessibility": ["A", "C"]}}
        self.applied["accessibility-enabled"] = "A,C,X"
        raw = {"accessibility": ["A", "B=/Applications/B.app"]}
        expected = [
            [S, "accessibility", "enable", "A,B=/Applications/B.app"],
            [S, "accessibility", "revoke", "C"],
        ]
        self.assert_preview_matches(raw, state, expected)
        ok, commands = self._install(raw, state=state)
        self.assertTrue(ok)
        self.assertEqual(commands, expected)
        self.assertEqual(state["settingsctl"], {"accessibility": ["A", "B"]})

    def test_partial_set_records_what_was_granted(self):
        self.applied["fda-enabled"] = "old"
        self.after = {"fulldiskaccess": {"fda-enabled": "A,old"}}
        self.after_failure = True
        state = {"settingsctl": {"fullDiskAccess": ["old", "Z"]}}
        ok, commands = self._install({"fullDiskAccess": ["A", "B", "Z"]}, rc=1, state=state)
        self.assertFalse(ok)
        self.assertEqual(commands, [[S, "fulldiskaccess", "set", "A,B,Z"]])
        self.assertEqual(state["settingsctl"], {"fullDiskAccess": ["A", "Z", "old"]})

    def test_failed_enable_records_its_targets(self):
        self.applied["accessibility-enabled"] = "X"
        state = {}
        ok, _ = self._install({"accessibility": ["A"]}, rc=1, state=state)
        self.assertFalse(ok)
        self.assertEqual(state["settingsctl"], {"accessibility": ["A"]})

    def test_login_set_drops_recorded_items_it_removed(self):
        self.login_items = "L1\nL2\n"
        state = {"settingsctl": {"loginItems": ["L1", "L2"]}}
        ok, commands = self._install({"loginItems": ["L1"]}, state=state)
        self.assertTrue(ok)
        self.assertEqual(commands, [[S, "login", "set", "L1"]])
        self.assertEqual(state["settingsctl"], {"loginItems": ["L1"]})

    def test_failed_login_set_records_from_current_items(self):
        self.login_items = "L2\n"
        self.after = {"login_items": "L2\nN1\n"}
        state = {"settingsctl": {"loginItems": ["L2"]}}
        ok, _ = self._install({"loginItems": ["N1", "N2"]}, rc=1, state=state)
        self.assertFalse(ok)
        self.assertEqual(state["settingsctl"], {"loginItems": ["L2", "N1"]})

    def test_missing_cli_keeps_records(self):
        state = self.removed_state()
        self.assertFalse(settingsctl.install_settingsctl(None, which=lambda _: None, state=state))
        self.assertEqual(state, self.removed_state())
        self.assertIn("`settings` not found", self.diff(None, which=lambda _: None, state=state)[0])

    def test_root_ignores_records(self):
        state = self.removed_state()
        ok = settingsctl.install_settingsctl(
            {"timeZone": "UTC"}, root=True, which=lambda _: None, state=state
        )
        self.assertFalse(ok)
        self.assertEqual(state, self.removed_state())


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
        inst = self._deploy({"settingsctl": MINI})
        inst.assert_called_once()
        self.assertEqual(inst.call_args.args, (MINI, None))
        self.assertIn("state", inst.call_args.kwargs)

    def test_deploy_runs_removed_section_with_records(self):
        state = {"version": 2, "settingsctl": {"accessibility": ["A"]}}
        with (
            mock.patch.object(cli, "load_json", return_value=state),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(cli, "save_json"),
            mock.patch.object(cli, "state_lock"),
            mock.patch.object(cli, "install_settingsctl", return_value=True) as inst,
        ):
            self.assertTrue(cli._deploy({}, "/tmp"))
        inst.assert_called_once()
        self.assertIsNone(inst.call_args.args[0])

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
