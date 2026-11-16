import json
import tempfile
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
S = "/bin/settings"
PANES = {"fullDiskAccess": "fulldiskaccess", "accessibility": "accessibility"}


class FakeCli:
    """Live panes ({name: enabled}) and login items behind `settings --no-state ...`."""

    def __init__(self):
        self.panes = {"fulldiskaccess": {}, "accessibility": {}}
        self.login = []
        self.unreadable = set()
        self.stuck = set()
        self.calls = []
        self.envs = []

    def run(self, cmd, env=None, cwd=None):
        assert cmd[0] == S and cmd[1] == "--no-state", cmd
        args = cmd[2:]
        self.calls.append(args)
        self.envs.append(env)
        verb = args[0]
        if args[1:2] == ["list"]:
            if verb in self.unreadable:
                return 1, "", "could not read"
            if verb == "login":
                return 0, "\n".join(self.login) or "No login items found.", ""
            rows = [{"name": n, "enabled": on} for n, on in self.panes[verb].items()]
            return 0, json.dumps({"items": rows}), ""
        assert "set" not in args[1:2], args
        if verb == "appearance":
            return (1 if "--init" in self.stuck else 0), "", ""
        ok = True
        names = args[-1].split(",")
        for name in names:
            plain = name.partition("=")[0]
            if verb == "login":
                plain = plain.rsplit("/", 1)[-1].removesuffix(".app")
            if plain in self.stuck:
                ok = False
                continue
            if verb == "login":
                if args[1] == "add" and plain not in self.login:
                    self.login.append(plain)
                if args[1] == "remove" and plain in self.login:
                    self.login.remove(plain)
            elif args[1] == "revoke":
                if verb == "accessibility":
                    self.panes[verb].pop(plain, None)
                elif plain in self.panes[verb]:
                    self.panes[verb][plain] = False
            else:
                self.panes[verb][plain] = True
        return (0 if ok else 1), "", ""

    def changes(self):
        return [c for c in self.calls if c[1:2] != ["list"]]


class SettingsctlCase(unittest.TestCase):
    def setUp(self):
        for patch in (
            mock.patch.object(settingsctl.sys, "platform", "darwin"),
            mock.patch.object(settingsctl, "_legacy_appearance_marker", lambda: "/nonexistent"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.fake = FakeCli()

    def grant(self, key, *names, on=True):
        for name in names:
            self.fake.panes[PANES[key]][name] = on

    def _install(self, raw, state=None):
        self.fake.calls = []
        ok = settingsctl.install_settingsctl(
            raw, which=lambda _: S, run=self.fake.run, root=False, state=state
        )
        return ok, self.fake.changes()

    def diff(self, raw, which=lambda _: S, state=None):
        self.fake.calls = []
        rows = settingsctl.diff_settingsctl(
            raw, which=which, run=self.fake.run, root=False, executable=lambda _: False, state=state
        )
        self.assertEqual(self.fake.changes(), [])
        return rows

    def assert_preview_matches(self, raw, state, commands):
        """Rows from diff are exactly the commands deploy ran, in order."""
        rows = self.diff(raw, state=state)
        ran = [
            "  {} settings {}".format(
                "-" if c[1] in ("revoke", "remove") else "+" if c[0] != "appearance" else "~",
                settingsctl._shown(c),
            )
            for c in commands
        ]
        self.assertEqual(rows, ran)


class SettingsctlTest(SettingsctlCase):
    def test_enables_and_adds_each_declared_list(self):
        expected = [
            ["fulldiskaccess", "--enable", "kitty,Terminal"],
            ["accessibility", "enable", "Hammerspoon"],
            ["login", "add", "Hammerspoon"],
            ["login", "add", "LosslessSwitcher"],
        ]
        self.assert_preview_matches(MINI, {}, expected)
        ok, commands = self._install(MINI)
        self.assertTrue(ok)
        self.assertEqual(commands, expected)

    def test_failure_fails_deploy(self):
        self.fake.stuck.add("A")
        ok, commands = self._install({"accessibility": ["A"]})
        self.assertFalse(ok)
        self.assertEqual(commands, [["accessibility", "enable", "A"]])

    def test_only_what_is_not_live_runs(self):
        self.grant("fullDiskAccess", "Terminal", "determinate-nixd")
        self.grant("fullDiskAccess", "kitty", on=False)
        self.fake.login = ["Hammerspoon", "HandAdded"]
        self.assertEqual(
            self.diff(MINI),
            [
                "  + settings fulldiskaccess --enable kitty",
                "  + settings accessibility enable Hammerspoon",
                "  + settings login add LosslessSwitcher",
            ],
        )

    def test_recorded_lists_skip_pane_reads(self):
        state = {
            "settingsctl": {
                "fullDiskAccess": ["Terminal", "kitty"],
                "accessibility": ["Hammerspoon"],
                "loginItems": ["Hammerspoon", "LosslessSwitcher"],
            }
        }
        self.fake.login = ["LosslessSwitcher", "Hammerspoon"]
        self.assertEqual(self.diff(MINI, state=state), [])
        ok, commands = self._install(MINI, state=state)
        self.assertTrue(ok)
        self.assertEqual(commands, [])
        self.assertEqual(self.fake.calls, [["login", "list"]])

    def test_app_paths_compare_by_name(self):
        self.grant("accessibility", "Muxy")
        self.fake.login = ["Home App"]
        raw = {
            "accessibility": ["Muxy=/Applications/Muxy.app"],
            "loginItems": ["/Users/me/Applications/Home App.app"],
        }
        self.assertEqual(self.diff(raw), [])

    def test_enable_passes_app_paths(self):
        self.assertEqual(
            self.diff({"accessibility": ["Muxy=/Applications/Muxy.app"]}),
            ["  + settings accessibility enable Muxy=/Applications/Muxy.app"],
        )

    def test_no_login_items_reads_as_empty(self):
        self.assertEqual(self.diff({"loginItems": ["A"]}), ["  + settings login add A"])

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

    def test_user_runs_get_home_from_passwd_without_sudo_vars(self):
        entry = mock.Mock(pw_dir="/Users/me")
        sudo = {k: "root" for k in settingsctl.SUDO_VARS}
        with (
            mock.patch.dict(settingsctl.os.environ, {"HOME": "/var/root", "KEEP": "1", **sudo}),
            mock.patch.object(settingsctl.pwd, "getpwuid", return_value=entry),
        ):
            self.assertTrue(self._install({"accessibility": ["A"]})[0])
            self.diff({"loginItems": ["A"]})
        self.assertGreaterEqual(len(self.fake.envs), 2)
        for env in self.fake.envs:
            self.assertEqual(env["HOME"], "/Users/me")
            self.assertEqual(env["KEEP"], "1")
            self.assertFalse(set(settingsctl.SUDO_VARS) & set(env))

    def test_root_runs_keep_their_env(self):
        envs = []

        def run(cmd, env=None, cwd=None):
            envs.append(env)
            return 0, json.dumps({"timeZone": "UTC", "matches": True}), ""

        self.assertTrue(
            settingsctl.install_settingsctl(
                {"timeZone": "UTC"},
                run=run,
                root=True,
                executable=lambda _: True,
                paths={"uvBin": "/bin"},
            )
        )
        self.assertEqual(envs, [None])


class ListRecordTest(SettingsctlCase):
    """state.json records what tools granted; recorded names that leave config are revoked."""

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

    def test_adopts_live_grants(self):
        self.grant("fullDiskAccess", "Terminal", "kitty")
        self.grant("accessibility", "Hammerspoon", "Other")
        self.fake.login = ["LosslessSwitcher", "Hammerspoon"]
        state = {}
        ok, commands = self._install(MINI, state=state)
        self.assertTrue(ok)
        self.assertEqual(commands, [])
        self.assertEqual(state["settingsctl"]["accessibility"], ["Hammerspoon"])

    def removed_state(self):
        return {
            "settingsctl": {
                "fullDiskAccess": ["kitty"],
                "accessibility": ["A", "B"],
                "loginItems": ["L1", "L2"],
            }
        }

    def test_removed_keys_revoke_only_recorded_names(self):
        expected = [
            ["fulldiskaccess", "revoke", "kitty"],
            ["accessibility", "revoke", "A,B"],
            ["login", "remove", "L1"],
        ]
        for raw in (None, {}, {"fullDiskAccess": [], "accessibility": [], "loginItems": []}):
            with self.subTest(raw=raw):
                self.grant("fullDiskAccess", "kitty", "other")
                self.grant("accessibility", "A", "B", "Hand")
                self.fake.login = ["L1", "HandAdded"]
                self.assertTrue(settingsctl.has_work(raw, self.removed_state()))
                self.assert_preview_matches(raw, self.removed_state(), expected)
                state = self.removed_state()
                ok, commands = self._install(raw, state=state)
                self.assertTrue(ok)
                self.assertEqual(commands, expected)
                self.assertNotIn("settingsctl", state)
                self.assertFalse(settingsctl.has_work(None, state))
                self.assertEqual(self.fake.panes["fulldiskaccess"]["other"], True)
                self.assertIn("Hand", self.fake.panes["accessibility"])
                self.assertEqual(self.fake.login, ["HandAdded"])

    def test_failed_revoke_stays_recorded(self):
        self.grant("fullDiskAccess", "kitty")
        self.grant("accessibility", "A", "B")
        self.fake.login = ["L1", "L2"]
        self.fake.stuck |= {"kitty", "B", "L2"}
        state = self.removed_state()
        ok, _ = self._install(None, state=state)
        self.assertFalse(ok)
        self.assertEqual(
            state["settingsctl"],
            {"fullDiskAccess": ["kitty"], "accessibility": ["B"], "loginItems": ["L2"]},
        )
        self.assertEqual(
            self.diff(None, state=state),
            [
                "  - settings fulldiskaccess revoke kitty",
                "  - settings accessibility revoke B",
                "  - settings login remove L2",
            ],
        )

    def test_failed_revoke_with_unreadable_pane_stays_recorded(self):
        self.fake.unreadable.add("accessibility")
        self.fake.stuck.add("B")
        state = {"settingsctl": {"accessibility": ["A", "B"]}}
        ok, _ = self._install(None, state=state)
        self.assertFalse(ok)
        self.assertEqual(state["settingsctl"], {"accessibility": ["A", "B"]})

    def test_shrunk_list_revokes_only_dropped_names(self):
        state = {"settingsctl": {"accessibility": ["A", "B"], "fullDiskAccess": ["k", "t"]}}
        self.grant("accessibility", "A", "B", "X")
        self.grant("fullDiskAccess", "k", "t")
        raw = {"accessibility": ["A"], "fullDiskAccess": ["k"]}
        expected = [["fulldiskaccess", "revoke", "t"], ["accessibility", "revoke", "B"]]
        self.assert_preview_matches(raw, state, expected)
        ok, commands = self._install(raw, state=state)
        self.assertTrue(ok)
        self.assertEqual(commands, expected)
        self.assertEqual(state["settingsctl"], {"accessibility": ["A"], "fullDiskAccess": ["k"]})
        self.assertIn("X", self.fake.panes["accessibility"])

    def test_new_names_enable_and_dropped_revoke(self):
        state = {"settingsctl": {"accessibility": ["A", "C"]}}
        self.grant("accessibility", "A", "C", "X")
        raw = {"accessibility": ["A", "B=/Applications/B.app"]}
        expected = [
            ["accessibility", "enable", "B=/Applications/B.app"],
            ["accessibility", "revoke", "C"],
        ]
        self.assert_preview_matches(raw, state, expected)
        ok, commands = self._install(raw, state=state)
        self.assertTrue(ok)
        self.assertEqual(commands, expected)
        self.assertEqual(state["settingsctl"], {"accessibility": ["A", "B"]})

    def test_partial_enable_records_live_grants(self):
        self.grant("fullDiskAccess", "old", "Z")
        self.fake.stuck.add("B")
        state = {"settingsctl": {"fullDiskAccess": ["old", "Z"]}}
        ok, commands = self._install({"fullDiskAccess": ["A", "B", "Z"]}, state=state)
        self.assertFalse(ok)
        self.assertEqual(
            commands,
            [["fulldiskaccess", "--enable", "A,B"], ["fulldiskaccess", "revoke", "old"]],
        )
        self.assertEqual(state["settingsctl"], {"fullDiskAccess": ["A", "Z"]})

    def test_failed_enable_with_unreadable_pane_records_its_targets(self):
        self.fake.unreadable.add("accessibility")
        self.fake.stuck.add("A")
        state = {}
        ok, commands = self._install({"accessibility": ["A"]}, state=state)
        self.assertFalse(ok)
        self.assertEqual(commands, [["accessibility", "enable", "A"]])
        self.assertEqual(state["settingsctl"], {"accessibility": ["A"]})

    def test_failed_login_add_records_from_current_items(self):
        self.fake.login = ["L2"]
        self.fake.stuck.add("N2")
        state = {"settingsctl": {"loginItems": ["L2"]}}
        ok, _ = self._install({"loginItems": ["N1", "N2"]}, state=state)
        self.assertFalse(ok)
        self.assertEqual(state["settingsctl"], {"loginItems": ["N1"]})
        self.assertEqual(self.fake.login, ["N1"])

    def test_login_items_already_gone_are_forgotten(self):
        self.fake.login = ["L1"]
        state = {"settingsctl": {"loginItems": ["L1", "L2"]}}
        ok, commands = self._install({"loginItems": ["L1"]}, state=state)
        self.assertTrue(ok)
        self.assertEqual(commands, [])
        self.assertEqual(state["settingsctl"], {"loginItems": ["L1"]})

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


class AppearanceInitTest(SettingsctlCase):
    def test_runs_once_and_records(self):
        raw = {"appearanceInit": True}
        state = {}
        self.assert_preview_matches(raw, state, [["appearance", "--init"]])
        ok, commands = self._install(raw, state=state)
        self.assertTrue(ok)
        self.assertEqual(commands, [["appearance", "--init"]])
        self.assertEqual(state, {"settingsctl": {"appearanceInit": True}})
        self.assertEqual(self.diff(raw, state=state), [])
        self.assertEqual(self._install(raw, state=state), (True, []))

    def test_failure_is_not_recorded(self):
        self.fake.stuck.add("--init")
        state = {}
        ok, _ = self._install({"appearanceInit": True}, state=state)
        self.assertFalse(ok)
        self.assertEqual(state, {})

    def test_false_does_nothing(self):
        self.assertEqual(self.diff({"appearanceInit": False}), [])

    def test_legacy_marker_counts_as_already_inited(self):
        with tempfile.NamedTemporaryFile() as marker:
            with mock.patch.object(settingsctl, "_legacy_appearance_marker", lambda: marker.name):
                raw = {"appearanceInit": True}
                state = {}
                self.assertEqual(self.diff(raw, state=state), [])
                ok, commands = self._install(raw, state=state)
        self.assertTrue(ok)
        self.assertEqual(commands, [])
        self.assertEqual(state, {"settingsctl": {"appearanceInit": True}})

    def test_leaving_config_forgets_the_record(self):
        state = {"settingsctl": {"appearanceInit": True}}
        self.assertTrue(settingsctl.has_work(None, state))
        self.assertEqual(self._install(None, state=state), (True, []))
        self.assertEqual(state, {})

    def test_bool_keys_are_typed(self):
        self.assertEqual(
            self.diff({"appearanceInit": "yes"}), ["  ! appearanceInit: must be true or false"]
        )
        ok, commands = self._install({"appearanceInit": 1})
        self.assertFalse(ok)
        self.assertEqual(commands, [])


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
        assert cmd[1] == "--no-state", cmd
        args = cmd[2:]
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


class OldCliTest(unittest.TestCase):
    def test_cli_without_no_state_gets_a_clear_error(self):
        def run(cmd, env=None):
            return 2, "", "Error: No such option: --no-state"

        rc, _, stderr = settingsctl.Runner("/old/settings", run).call("login", "list")
        self.assertEqual(rc, 2)
        self.assertIn("too old for --no-state", stderr)
