import os
import pwd
import tempfile
import unittest
from unittest import mock

from tools import cli, diff, privilege
from tools.config import TOP_LEVEL_KEYS
from tools.user import users

FISH = "/opt/homebrew/bin/fish"
MINI = {"ivan": {"uid": 502, "shell": FISH}}


def account(name="ivan", uid=502, shell="/bin/zsh"):
    return pwd.struct_passwd((name, "*", uid, 20, "", f"/Users/{name}", shell))


def getpwnam_of(*accounts):
    by_name = {a.pw_name: a for a in accounts}

    def getpwnam(name):
        return by_name[name]

    return getpwnam


class ParseTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(users.parse(MINI), (MINI, []))
        self.assertEqual(users.parse({"a": {"uid": 0}}), ({"a": {"uid": 0}}, []))

    def test_errors_hold_section(self):
        for raw in (
            [],
            {"ivan": {}},
            {"ivan": {"uid": "502"}},
            {"ivan": {"uid": True}},
            {"ivan": {"uid": -1}},
            {"ivan": {"shell": "fish"}},
            {"ivan": {"home": "/x"}},
            {"-x": {"uid": 1}},
            {"a/b": {"uid": 1}},
            {"ok": {"uid": 1}, "bad": None},
        ):
            spec, errors = users.parse(raw)
            self.assertEqual(spec, {}, raw)
            self.assertTrue(errors, raw)


class PlanTest(unittest.TestCase):
    def _plan(self, spec, *accounts, shells=(FISH,), shell_error=None, local=502):
        return users.plan(
            spec,
            getpwnam_of(*accounts),
            shells=lambda: list(shells),
            shell_error=lambda _path, _pw: shell_error,
            local=lambda _: local,
        )

    def test_shell_differs(self):
        todo, warnings, errors = self._plan(MINI, account())
        self.assertEqual(todo, [(f"  ~ ivan: login shell /bin/zsh -> {FISH}", "ivan", FISH)])
        self.assertEqual((warnings, errors), ([], []))

    def test_in_sync(self):
        self.assertEqual(self._plan(MINI, account(shell=FISH)), ([], [], []))

    def test_uid_mismatch_reports_and_skips_shell(self):
        todo, warnings, errors = self._plan(MINI, account(uid=501))
        self.assertEqual(todo, [])
        self.assertEqual(errors, [])
        self.assertIn("uid is 501, config says 502", warnings[0])

    def test_missing_user(self):
        todo, _, errors = self._plan(MINI)
        self.assertEqual(todo, [])
        self.assertIn("no such user", errors[0])

    def test_shell_not_in_etc_shells(self):
        todo, _, errors = self._plan(MINI, account(), shells=("/bin/zsh",))
        self.assertEqual(todo, [])
        self.assertIn("not listed in /etc/shells", errors[0])

    def test_shell_not_executable(self):
        todo, _, errors = self._plan(MINI, account(), shell_error="is not executable by ivan")
        self.assertEqual(todo, [])
        self.assertIn("is not executable by ivan", errors[0])

    def test_shell_not_installed_warns_only(self):
        todo, warnings, errors = self._plan(MINI, account(), shell_error="missing")
        self.assertEqual((todo, errors), ([], []))
        self.assertIn("not installed yet", warnings[0])

    def test_shell_error_checks_target_user(self):
        with tempfile.TemporaryDirectory() as d:
            shell = os.path.join(d, "sh")
            with open(shell, "w"):
                pass
            os.chmod(shell, 0o700)
            me = os.getuid()
            owner = account(uid=me, shell=shell)
            other = account("bob", uid=me + 4242, shell=shell)
            self.assertIsNone(users._shell_error(shell, owner))
            self.assertIn("not executable by bob", users._shell_error(shell, other))
            self.assertEqual(users._shell_error(os.path.join(d, "none"), owner), "missing")

    def test_shell_error_checks_symlink_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            hidden = os.path.join(d, "hidden")
            os.mkdir(hidden, 0o700)
            os.chmod(d, 0o755)
            link = os.path.join(hidden, "sh")
            os.symlink("/bin/sh", link)
            other = account("bob", uid=os.getuid() + 4242, shell=link)
            self.assertIsNone(users._shell_error("/bin/sh", other))
            self.assertIn(f"({hidden})", users._shell_error(link, other))

    def test_non_local_with_missing_shell_fails(self):
        todo, warnings, errors = self._plan(MINI, account(), shell_error="missing", local=None)
        self.assertEqual((todo, warnings), ([], []))
        self.assertIn("not a local account", errors[0])

    def test_not_local_account(self):
        todo, _, errors = self._plan(MINI, account(), local=None)
        self.assertEqual(todo, [])
        self.assertIn("not a local account", errors[0])

    def test_local_uid_reads_local_node(self):
        calls = []

        def run(cmd):
            calls.append(cmd)
            return 0, "UniqueID: 502\n", ""

        self.assertEqual(users.local_uid("ivan", run), 502)
        self.assertEqual(calls, [["/usr/bin/dscl", ".", "-read", "/Users/ivan", "UniqueID"]])
        self.assertIsNone(users.local_uid("ivan", lambda cmd: (56, "", "eDSRecordNotFound")))

    def test_uid_only(self):
        self.assertEqual(self._plan({"ivan": {"uid": 502}}, account()), ([], [], []))

    def test_read_shells_skips_comments(self):
        with mock.patch("builtins.open", mock.mock_open(read_data="# list\n/bin/zsh\n\n" + FISH)):
            self.assertEqual(users.read_shells(), ["/bin/zsh", FISH])


class InstallTest(unittest.TestCase):
    def setUp(self):
        for patch in (
            mock.patch.object(users.sys, "platform", "darwin"),
            mock.patch.object(users, "read_shells", return_value=[FISH]),
            mock.patch.object(users, "_shell_error", return_value=None),
            mock.patch.object(users, "local_uid", return_value=502),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def _install(self, raw, *accounts, root=True, rc=0):
        commands = []

        def run(cmd, env=None, cwd=None):
            commands.append(cmd)
            return rc, "", "boom" if rc else ""

        ok = users.install_users(raw, root=root, getpwnam=getpwnam_of(*accounts), run=run)
        return ok, commands

    def test_root_sets_shell_with_dscl(self):
        ok, commands = self._install(MINI, account())
        self.assertTrue(ok)
        self.assertEqual(
            commands, [["/usr/bin/dscl", ".", "-create", "/Users/ivan", "UserShell", FISH]]
        )

    def test_dscl_failure_fails(self):
        ok, commands = self._install(MINI, account(), rc=1)
        self.assertFalse(ok)
        self.assertEqual(len(commands), 1)

    def test_uid_mismatch_changes_nothing_and_passes(self):
        self.assertEqual(self._install(MINI, account(uid=501)), (True, []))

    def test_missing_user_fails(self):
        self.assertEqual(self._install(MINI), (False, []))

    def test_invalid_config_fails(self):
        self.assertEqual(self._install({"ivan": {"uid": "x"}}, account()), (False, []))

    def test_user_only_notes(self):
        with mock.patch.object(users, "log") as log:
            self.assertEqual(self._install(MINI, account(), root=False), (True, []))
        self.assertIn("sudo tools deploy --scope users", log.call_args_list[0].args[0])

    def test_user_under_sudo_is_silent(self):
        with (
            mock.patch.object(privilege, "UNDER_SUDO", True),
            mock.patch.object(users, "log") as log,
        ):
            self.assertEqual(self._install(MINI, account(), root=False), (True, []))
        log.assert_not_called()

    def test_not_macos(self):
        with mock.patch.object(users.sys, "platform", "linux"):
            self.assertEqual(self._install(MINI, account()), (True, []))

    def test_diff(self):
        getpwnam = getpwnam_of(account(), account("bob", 600))
        raw = {**MINI, "bob": {"uid": 601}, "eve": {"uid": 1}}
        rows = users.diff_users(raw, root=True, getpwnam=getpwnam)
        self.assertEqual(
            rows,
            [
                "  ! eve: no such user (tools does not create users)",
                "  ? bob: uid is 600, config says 601; tools never changes uids,"
                " skipping this user",
                f"  ~ ivan: login shell /bin/zsh -> {FISH}",
            ],
        )
        with mock.patch.object(users, "log"):
            self.assertEqual(users.diff_users(raw, root=False, getpwnam=getpwnam), [])


class WiringTest(unittest.TestCase):
    def _deploy(self, config, root):
        with (
            mock.patch.object(cli, "load_json", return_value={}),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(cli, "save_json"),
            mock.patch.object(cli, "state_lock"),
            mock.patch.object(cli, "install_users", return_value=True) as inst,
        ):
            self.assertTrue(cli._deploy(config, "/tmp", root=root))
        return inst

    def test_known_root_section(self):
        self.assertIn("users", TOP_LEVEL_KEYS)
        self.assertIn("users", privilege.ROOT_SECTIONS)

    def test_deploy_skips_absent_section(self):
        self._deploy({}, root=True).assert_not_called()
        self._deploy({}, root=False).assert_not_called()

    def test_deploy_runs_declared_section(self):
        self._deploy({"users": MINI}, root=True).assert_called_once_with(MINI)
        self._deploy({"users": MINI}, root=False).assert_called_once_with(MINI)

    def test_root_diff_lists_section(self):
        with mock.patch.object(diff, "diff_users", return_value=["  ~ row"]) as d:
            sections = diff._root_sections({"users": MINI}, "/tmp", (), {})
        d.assert_called_once_with(MINI)
        self.assertIn(("users", ["  ~ row"]), sections)


if __name__ == "__main__":
    unittest.main()
