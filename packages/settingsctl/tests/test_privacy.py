"""Tests for the Full Disk Access and Accessibility list syncs (no UI is driven).

Run from the repo root: python -m unittest discover -s packages/settingsctl/tests
"""

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from click.testing import CliRunner  # noqa: E402

import accessibility_state  # noqa: E402
import appearance  # noqa: E402
import common  # noqa: E402
import fulldiskaccess  # noqa: E402
import login  # noqa: E402
import privacy_pane  # noqa: E402
import settings  # noqa: E402


class FakePane:
    """App rows of one privacy pane: {name: enabled}; None means unreadable."""

    def __init__(self, rows):
        self.rows = rows
        self.sticks = True
        self.sessions = []

    def set_listed(self, _url, apps, on=True):
        self.sessions.append((sorted(apps), on))
        if self.rows is None:
            return None
        verb = "enabled" if on else "disabled"
        out = {}
        for app in apps:
            if app not in self.rows:
                continue
            if self.rows[app] == on:
                out[app] = f"already {verb}"
            elif self.sticks:
                self.rows[app] = on
                out[app] = verb
            else:
                out[app] = "unchanged"
        return out


class PaneTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, common, "NO_STATE", False)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.pane = FakePane({"kitty": False, "Terminal": True, "old": True})
        for patch in (
            mock.patch.object(privacy_pane, "set_listed", self.pane.set_listed),
            mock.patch.object(fulldiskaccess, "_get_state_dir", lambda: Path(self.dir.name)),
            mock.patch.object(accessibility_state, "_get_state_dir", lambda: Path(self.dir.name)),
            mock.patch.object(accessibility_state, "list_apps", self.list_apps),
            mock.patch.object(accessibility_state, "quit_app", lambda _: True),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def list_apps(self, _url):
        if self.pane.rows is None:
            return None
        return [{"name": n, "enabled": v} for n, v in self.pane.rows.items()]

    def state(self, name):
        path = Path(self.dir.name) / name
        return path.read_text() if path.exists() else None

    def cli(self, *args):
        with mock.patch.object(fulldiskaccess, "is_macos", lambda: True):
            return CliRunner().invoke(settings.cli, list(args)).exit_code

    def test_ensure_enabled_is_true_only_when_all_enabled(self):
        apps = {"kitty": None, "Terminal": None}
        self.assertTrue(privacy_pane.ensure_enabled("u", "FDA", apps))
        self.assertFalse(privacy_pane.ensure_enabled("u", "FDA", {"kitty": None, "nope": None}))
        self.pane.rows["kitty"] = False
        self.pane.sticks = False
        self.assertFalse(privacy_pane.ensure_enabled("u", "FDA", apps))
        self.pane.rows = None
        self.assertFalse(privacy_pane.ensure_enabled("u", "FDA", apps))

    def test_set_turns_off_dropped_apps_and_records_state(self):
        (Path(self.dir.name) / "fda-enabled").write_text("Terminal,old")
        self.assertEqual(self.cli("fulldiskaccess", "set", "kitty,Terminal"), 0)
        self.assertEqual(self.pane.rows, {"kitty": True, "Terminal": True, "old": False})
        self.assertEqual(self.state("fda-enabled"), "Terminal,kitty")

    def test_set_skips_when_state_matches(self):
        (Path(self.dir.name) / "fda-enabled").write_text("Terminal,kitty")
        self.assertEqual(self.cli("fulldiskaccess", "set", "kitty,Terminal"), 0)
        self.assertEqual(self.pane.sessions, [])

    def test_missing_app_exits_non_zero(self):
        self.assertEqual(self.cli("fulldiskaccess", "--enable", "kitty,nope"), 1)
        self.assertIsNone(self.state("fda-enabled"))
        self.assertEqual(self.cli("fulldiskaccess", "set", "kitty,nope"), 1)
        self.assertEqual(self.state("fda-enabled"), "kitty")

    def test_partial_set_records_apps_it_turned_on(self):
        (Path(self.dir.name) / "fda-enabled").write_text("Terminal")
        self.pane.rows["Terminal"] = True
        self.assertFalse(fulldiskaccess.fda_set({"kitty": None, "nope": None}))
        self.assertEqual(self.pane.rows["Terminal"], False)
        self.assertEqual(self.state("fda-enabled"), "kitty")
        self.assertTrue(fulldiskaccess.fda_set({"Terminal": None}))
        self.assertEqual(self.pane.rows, {"kitty": False, "Terminal": True, "old": True})
        self.assertEqual(self.state("fda-enabled"), "Terminal")

    def test_partial_set_with_previous_subset_still_retries(self):
        (Path(self.dir.name) / "fda-enabled").write_text("Terminal")
        self.assertFalse(fulldiskaccess.fda_set({"Terminal": None, "kitty": None, "nope": None}))
        self.assertEqual(self.state("fda-enabled"), "Terminal,kitty")
        self.assertFalse(fulldiskaccess.fda_state_matches(["Terminal", "kitty", "nope"]))

    def test_unreadable_pane_exits_non_zero(self):
        self.pane.rows = None
        self.assertEqual(self.cli("fulldiskaccess", "set", "kitty"), 1)
        self.assertIsNone(self.state("fda-enabled"))

    def test_failed_turn_off_keeps_old_state(self):
        (Path(self.dir.name) / "fda-enabled").write_text("Terminal,old")
        self.pane.sticks = False
        self.assertFalse(fulldiskaccess.fda_set({"Terminal": None}))
        self.assertEqual(self.state("fda-enabled"), "Terminal,old")

    def test_accessibility_partial_set_records_apps_it_turned_on(self):
        (Path(self.dir.name) / "accessibility-enabled").write_text("Terminal")
        self.assertFalse(
            accessibility_state.accessibility_set({"Terminal": None, "kitty": None, "nope": None})
        )
        self.assertEqual(self.state("accessibility-enabled"), "Terminal,kitty")

    def test_accessibility_set_fails_when_list_unreadable(self):
        (Path(self.dir.name) / "accessibility-enabled").write_text("old")
        self.pane.rows = None
        self.assertFalse(accessibility_state.accessibility_set({"kitty": None}))
        self.assertEqual(self.state("accessibility-enabled"), "old")

    def test_accessibility_set_records_state_on_success(self):
        with mock.patch.object(accessibility_state, "accessibility_remove") as remove:
            remove.side_effect = lambda app: self.pane.rows.pop(app, None) is not None
            (Path(self.dir.name) / "accessibility-enabled").write_text("old")
            self.assertTrue(accessibility_state.accessibility_set({"kitty": None}))
        remove.assert_called_once_with("old")
        self.assertEqual(self.state("accessibility-enabled"), "kitty")
        self.assertFalse(accessibility_state.accessibility_set({"kitty": None, "nope": None}))
        self.assertEqual(self.state("accessibility-enabled"), "kitty")
        self.pane.rows["kitty"] = False
        self.pane.sticks = False
        (Path(self.dir.name) / "accessibility-enabled").write_text("x")
        self.assertFalse(accessibility_state.accessibility_enable({"kitty": None}))
        self.assertEqual(self.state("accessibility-enabled"), "x")

    def test_fda_revoke_turns_off_only_listed_apps(self):
        (Path(self.dir.name) / "fda-enabled").write_text("Terminal,old")
        self.assertEqual(self.cli("fulldiskaccess", "revoke", "old,gone"), 0)
        self.assertEqual(self.pane.rows, {"kitty": False, "Terminal": True, "old": False})
        self.assertEqual(self.state("fda-enabled"), "Terminal")

    def test_fda_revoke_failure_keeps_state(self):
        (Path(self.dir.name) / "fda-enabled").write_text("Terminal,old")
        self.pane.sticks = False
        self.assertEqual(self.cli("fulldiskaccess", "revoke", "old"), 1)
        self.assertEqual(self.state("fda-enabled"), "Terminal,old")
        self.assertEqual(self.cli("fulldiskaccess", "revoke", ""), 1)

    def test_accessibility_revoke_removes_only_listed_apps(self):
        (Path(self.dir.name) / "accessibility-enabled").write_text("Terminal,old")
        with mock.patch.object(accessibility_state, "accessibility_remove") as remove:
            remove.side_effect = lambda app: self.pane.rows.pop(app, None) is not None
            self.assertTrue(accessibility_state.accessibility_revoke(["old", "gone"]))
        remove.assert_called_once_with("old")
        self.assertEqual(self.pane.rows, {"kitty": False, "Terminal": True})
        self.assertEqual(self.state("accessibility-enabled"), "Terminal")

    def test_accessibility_revoke_failures(self):
        (Path(self.dir.name) / "accessibility-enabled").write_text("old")
        with mock.patch.object(accessibility_state, "accessibility_remove", return_value=False):
            self.assertFalse(accessibility_state.accessibility_revoke(["old"]))
        self.assertEqual(self.state("accessibility-enabled"), "old")
        self.pane.rows = None
        self.assertFalse(accessibility_state.accessibility_revoke(["old"]))

    def test_accessibility_confirmed_removal_needs_no_second_pane_read(self):
        (Path(self.dir.name) / "accessibility-enabled").write_text("Terminal,old")
        reads = [self.list_apps("u")]
        with (
            mock.patch.object(accessibility_state, "list_apps", lambda _u: reads.pop(0)),
            mock.patch.object(accessibility_state, "accessibility_remove") as remove,
        ):
            remove.side_effect = lambda app: self.pane.rows.pop(app, None) is not None
            self.assertTrue(accessibility_state.accessibility_revoke(["old"]))
        self.assertEqual(reads, [])
        self.assertEqual(self.state("accessibility-enabled"), "Terminal")

    def test_accessibility_set_failed_removal_keeps_the_grant(self):
        (Path(self.dir.name) / "accessibility-enabled").write_text("Terminal,old")
        with mock.patch.object(accessibility_state, "accessibility_remove", return_value=False):
            self.assertFalse(accessibility_state.accessibility_set({"Terminal": None}))
        self.assertEqual(self.state("accessibility-enabled"), "Terminal,old")


class RemoveAppTest(unittest.TestCase):
    """remove_app drives osascript (mocked): the script and how its output is read."""

    def run_remove(self, stdout, returncode=0):
        calls = []

        def run(cmd, **_kw):
            calls.append(cmd)
            return mock.Mock(returncode=returncode, stdout=stdout, stderr="")

        with mock.patch.object(privacy_pane.subprocess, "run", run):
            result = privacy_pane.remove_app("url", "old")
        script = next(c[2] for c in calls if "select r" in c[2])
        return result, script

    def test_clicks_the_button_labelled_remove_never_by_position(self):
        result, script = self.run_remove("removed\n")
        self.assertTrue(result)
        self.assertNotIn("click btn", script)
        self.assertNotIn("click button 1", script)
        self.assertIn("description of btn", script)
        self.assertIn('labels contains "Remove"', script)
        self.assertIn('if removeBtn is missing value then return "no remove button: "', script)
        self.assertIn('return "still listed"', script)
        self.assertIn('count of windows of process "SecurityAgent"', script)

    def test_missing_remove_button_reports_the_labels_seen(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            result, _ = self.run_remove("no remove button: [add/+] [] \n")
        self.assertFalse(result)
        self.assertIn("buttons: [add/+] []", err.getvalue())

    def test_auth_wait_ends_once_the_click_took_effect(self):
        # A SecurityAgent window left open must not stall a click that worked:
        # the done check runs before the prompt check on every poll.
        _, script = self.run_remove("removed\n")
        exit_done = script.index("if clickDone then exit repeat")
        self.assertLess(script.index("then set clickDone to false"), exit_done)
        self.assertLess(exit_done, script.index('process "SecurityAgent"'))
        calls = []

        def run(cmd, **_kw):
            calls.append(cmd)
            return mock.Mock(returncode=0, stdout="a:enabled\n", stderr="")

        with mock.patch.object(privacy_pane.subprocess, "run", run):
            privacy_pane.set_listed("url", ["a", "b"])
        script = next(c[2] for c in calls if "click el" in c[2])
        self.assertIn("set clickDone to (value of el is 1)", script)
        exit_done = script.index("if clickDone then exit repeat")
        self.assertLess(exit_done, script.index('process "SecurityAgent"'))

    def test_only_a_confirmed_removal_counts(self):
        for out in ("no remove button: \n", "still listed\n", "not found\n", ""):
            self.assertFalse(self.run_remove(out)[0], out)
        self.assertFalse(self.run_remove("removed\n", returncode=1)[0])


class NoStateTest(unittest.TestCase):
    """`settings --no-state` acts on the live pane only and never touches the state dir."""

    def setUp(self):
        self.addCleanup(setattr, common, "NO_STATE", False)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.state = Path(self.dir.name) / ".local" / "state" / "settings"
        self.pane = FakePane({"kitty": False, "Terminal": True, "old": True})
        for patch in (
            mock.patch.object(common, "_user_home", lambda: Path(self.dir.name)),
            mock.patch.object(privacy_pane, "set_listed", self.pane.set_listed),
            mock.patch.object(accessibility_state, "list_apps", self.list_apps),
            mock.patch.object(accessibility_state, "quit_app", lambda _: True),
            mock.patch.object(fulldiskaccess, "is_macos", lambda: True),
            mock.patch("accessibility.is_macos", lambda: True),
            mock.patch.object(login, "is_macos", lambda: True),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def list_apps(self, _url):
        if self.pane.rows is None:
            return None
        return [{"name": n, "enabled": v} for n, v in self.pane.rows.items()]

    def cli(self, *args):
        return CliRunner().invoke(settings.cli, ["--no-state", *args])

    def write_state(self, name, text):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / name).write_text(text)

    def test_state_dir_refuses_under_no_state(self):
        common.NO_STATE = True
        with self.assertRaises(RuntimeError):
            common._get_state_dir()

    def test_enable_ignores_matching_state_and_writes_none(self):
        self.write_state("fda-enabled", "kitty")
        self.write_state("accessibility-enabled", "kitty")
        self.assertEqual(self.cli("fulldiskaccess", "--enable", "kitty").exit_code, 0)
        self.assertEqual(self.cli("accessibility", "enable", "kitty").exit_code, 0)
        self.assertEqual(self.pane.sessions, [(["kitty"], True), (["kitty"], True)])
        self.assertEqual((self.state / "fda-enabled").read_text(), "kitty")
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), [
            "accessibility-enabled", "fda-enabled",
        ])

    def test_enable_failure_exits_non_zero_without_state(self):
        self.assertEqual(self.cli("fulldiskaccess", "--enable", "kitty,nope").exit_code, 1)
        self.pane.rows = None
        self.assertEqual(self.cli("accessibility", "enable", "kitty").exit_code, 1)
        self.assertFalse(self.state.exists())

    def test_set_is_refused(self):
        self.assertEqual(self.cli("fulldiskaccess", "set", "kitty").exit_code, 2)
        self.assertEqual(self.cli("accessibility", "set", "kitty").exit_code, 2)
        self.assertEqual(self.pane.sessions, [])
        self.assertFalse(self.state.exists())

    def test_revoke_acts_on_the_pane_only(self):
        self.assertEqual(self.cli("fulldiskaccess", "revoke", "old,gone").exit_code, 0)
        self.assertEqual(self.pane.rows["old"], False)
        with mock.patch.object(accessibility_state, "accessibility_remove") as remove:
            remove.side_effect = lambda app: self.pane.rows.pop(app, None) is not None
            self.assertEqual(self.cli("accessibility", "revoke", "Terminal").exit_code, 0)
            remove.return_value = False
            remove.side_effect = None
            self.assertEqual(self.cli("accessibility", "revoke", "kitty").exit_code, 1)
        self.assertNotIn("Terminal", self.pane.rows)
        self.assertFalse(self.state.exists())

    def test_list_json_and_unreadable(self):
        with mock.patch.object(fulldiskaccess, "list_apps", self.list_apps):
            result = self.cli("fulldiskaccess", "list", "--json")
            self.assertEqual(result.exit_code, 0)
            items = __import__("json").loads(result.output)["items"]
            self.assertIn({"name": "kitty", "enabled": False}, items)
            self.pane.rows = None
            self.assertEqual(self.cli("fulldiskaccess", "list", "--json").exit_code, 1)

    def test_login_add_ignores_state_and_fails_when_unreadable(self):
        self.write_state("login-items", "A")
        items = []

        def osascript(cmd, **_kw):
            script = cmd[-1]
            if "get the name" in script:
                return mock.Mock(returncode=0, stdout=", ".join(items), stderr="")
            items.append("A")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with (
            mock.patch.object(login.subprocess, "run", osascript),
            mock.patch.object(login.Path, "exists", lambda _p: True),
        ):
            self.assertEqual(self.cli("login", "add", "A").exit_code, 0)
        self.assertEqual(items, ["A"])
        self.assertEqual((self.state / "login-items").read_text(), "A")
        failed = mock.Mock(returncode=1, stdout="", stderr="boom")
        with mock.patch.object(login.subprocess, "run", return_value=failed):
            self.assertEqual(self.cli("login", "add", "B").exit_code, 1)
            self.assertEqual(self.cli("login", "remove", "B").exit_code, 1)

    def test_appearance_init_always_applies_and_writes_nothing(self):
        (self.state / "appearance").mkdir(parents=True)
        (self.state / "appearance" / "last-run").write_text("2026-01-01")
        with (
            mock.patch.object(appearance, "is_macos", lambda: True),
            mock.patch.object(appearance, "appearance_set_dark_mode") as dark,
        ):
            self.assertEqual(self.cli("appearance", "--init").exit_code, 0)
        dark.assert_called_once_with(True)
        self.assertEqual((self.state / "appearance" / "last-run").read_text(), "2026-01-01")


class UserHomeTest(unittest.TestCase):
    def home(self, euid, sudo_user):
        users = {"ivan": "/Users/ivan", "root": "/var/root"}
        env = {"SUDO_USER": sudo_user} if sudo_user else {}
        uid_home = "/var/root" if euid == 0 else "/Users/ivan"
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(common.os, "geteuid", return_value=euid),
            mock.patch("pwd.getpwnam", lambda n: mock.Mock(pw_dir=users[n])),
            mock.patch("pwd.getpwuid", lambda _uid: mock.Mock(pw_dir=uid_home)),
        ):
            return str(common._user_home())

    def test_root_under_sudo_uses_the_invoking_user(self):
        self.assertEqual(self.home(0, "ivan"), "/Users/ivan")

    def test_user_with_sudo_user_root_uses_own_home(self):
        self.assertEqual(self.home(501, "root"), "/Users/ivan")

    def test_root_without_a_real_sudo_user_uses_own_home(self):
        self.assertEqual(self.home(0, "root"), "/var/root")
        self.assertEqual(self.home(0, None), "/var/root")

    def test_user_ignores_sudo_user(self):
        self.assertEqual(self.home(501, "someone"), "/Users/ivan")


if __name__ == "__main__":
    unittest.main()


class LoginListTest(unittest.TestCase):
    def cli(self, osascript_rc, stdout=""):
        import login

        done = mock.Mock(returncode=osascript_rc, stdout=stdout, stderr="boom")
        with (
            mock.patch.object(login, "is_macos", lambda: True),
            mock.patch.object(login.subprocess, "run", return_value=done),
        ):
            return CliRunner().invoke(settings.cli, ["login", "list"])

    def test_unreadable_login_items_fail_instead_of_looking_empty(self):
        result = self.cli(1)
        self.assertEqual(result.exit_code, 1)
        self.assertNotIn("No login items found.", result.output)

    def test_empty_and_listed_login_items(self):
        self.assertIn("No login items found.", self.cli(0, "\n").output)
        self.assertEqual(self.cli(0, "A, B\n").output.split(), ["A", "B"])
