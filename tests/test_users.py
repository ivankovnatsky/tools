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


def account(name="ivan", uid=502, shell="/bin/zsh", gid=20):
    return pwd.struct_passwd((name, "*", uid, gid, "", f"/Users/{name}", shell))


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
            # The Nix sandbox root is 0750 group nixbld; share our gid to get past it.
            other = account("bob", uid=os.getuid() + 4242, shell=link, gid=os.getgid())
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
                "  ! eve: no such user (set create: true to create it)",
                "  ? bob: uid is 600, config says 601; tools never changes uids,"
                " skipping this user",
                f"  ~ ivan: login shell /bin/zsh -> {FISH}",
            ],
        )
        with mock.patch.object(users, "log"):
            self.assertEqual(users.diff_users(raw, root=False, getpwnam=getpwnam), [])


GUEST = {"guest": {"uid": 600, "create": True}}
GUEST_REC = {"uid": 600, "home": "/Users/guest", "deleteHome": False}
INVOKER = privilege.Invoker(True, 502, "/Users/ivan", "ivan")


def created_state(**records):
    return {"users": {"created": records}}


def no_uid(uid):
    raise KeyError(uid)


class CreateParseTest(unittest.TestCase):
    def test_valid(self):
        raw = {"guest": {"uid": 600, "create": True, "deleteHome": True}}
        self.assertEqual(users.parse(raw), (raw, []))

    def test_errors(self):
        for raw in (
            {"guest": {"create": True}},
            {"guest": {"uid": 500, "create": True}},
            {"guest": {"uid": 600, "create": "yes"}},
            {"guest": {"uid": 600, "deleteHome": True}},
            {"guest": {"uid": 600, "create": True, "deleteHome": 1}},
            {"a": {"uid": 600, "create": True}, "b": {"uid": 600, "create": True}},
        ):
            spec, errors = users.parse(raw)
            self.assertEqual(spec, {}, raw)
            self.assertTrue(errors, raw)


class PlanCreateTest(unittest.TestCase):
    def _plan(self, spec, *accounts, taken=(), local=None, home_exists=False, shells=(FISH,)):
        by_uid = {a.pw_uid: a for a in accounts + tuple(taken)}

        def getpwuid(uid):
            return by_uid[uid]

        return users.plan_create(
            spec,
            getpwnam_of(*accounts),
            getpwuid,
            shells=lambda: list(shells) + ["/bin/zsh"],
            local=lambda _: local,
            exists=lambda _: home_exists,
        )

    def test_missing_account_is_created(self):
        todo, errors = self._plan(GUEST)
        self.assertEqual(errors, [])
        self.assertEqual(
            todo,
            [
                (
                    "  + create account guest (uid 600, shell /bin/zsh, home /Users/guest,"
                    " no password)",
                    "guest",
                    600,
                    "/bin/zsh",
                )
            ],
        )

    def test_existing_account_is_not_created(self):
        self.assertEqual(self._plan(GUEST, account("guest", 600)), ([], []))

    def test_without_create_nothing(self):
        self.assertEqual(self._plan({"guest": {"uid": 600}}), ([], []))

    def test_uid_taken(self):
        todo, errors = self._plan(GUEST, taken=(account("bob", 600),))
        self.assertEqual(todo, [])
        self.assertIn("uid 600 is taken by bob", errors[0])

    def test_name_taken_locally(self):
        todo, errors = self._plan(GUEST, local=600)
        self.assertEqual(todo, [])
        self.assertIn("name is taken", errors[0])

    def test_home_exists(self):
        todo, errors = self._plan(GUEST, home_exists=True)
        self.assertEqual(todo, [])
        self.assertIn("/Users/guest already exists", errors[0])

    def test_shell_not_listed(self):
        todo, errors = self._plan({"guest": {**GUEST["guest"], "shell": "/bin/nope"}})
        self.assertEqual(todo, [])
        self.assertIn("not listed", errors[0])


class PlanDeleteTest(unittest.TestCase):
    def _plan(self, spec, state, *accounts, local=600, protected=None):
        return users.plan_delete(
            spec,
            state,
            getpwnam_of(*accounts),
            local=lambda _: local,
            protected=protected or (lambda _n, _u: None),
        )

    def test_left_config_is_deleted_keeping_home(self):
        todo, warnings, errors = self._plan(
            {}, created_state(guest=GUEST_REC), account("guest", 600)
        )
        self.assertEqual((warnings, errors), ([], []))
        self.assertEqual(
            todo,
            [("  - delete account guest (uid 600), keep home /Users/guest", "guest", GUEST_REC)],
        )

    def test_delete_home_row(self):
        rec = {**GUEST_REC, "deleteHome": True}
        todo, _, _ = self._plan({}, created_state(guest=rec), account("guest", 600))
        self.assertIn("remove home /Users/guest", todo[0][0])

    def test_still_in_config_is_kept(self):
        state = created_state(guest=GUEST_REC)
        self.assertEqual(self._plan(GUEST, state, account("guest", 600)), ([], [], []))

    def test_unrecorded_account_is_never_deleted(self):
        self.assertEqual(self._plan({}, {}, account("guest", 600)), ([], [], []))
        self.assertEqual(self._plan({}, None, account("guest", 600)), ([], [], []))

    def test_gone_account_is_forgotten(self):
        todo, _, _ = self._plan({}, created_state(guest=GUEST_REC))
        self.assertEqual(todo, [("  - forget guest (account already gone)", "guest", None)])

    def test_uid_mismatch_warns_and_keeps(self):
        todo, warnings, errors = self._plan(
            {}, created_state(guest=GUEST_REC), account("guest", 700)
        )
        self.assertEqual((todo, errors), ([], []))
        self.assertIn("uid is 700", warnings[0])

    def test_not_local_is_kept(self):
        todo, _, errors = self._plan(
            {}, created_state(guest=GUEST_REC), account("guest", 600), local=None
        )
        self.assertEqual(todo, [])
        self.assertIn("not a local account", errors[0])

    def test_malformed_record_is_kept(self):
        for rec in ("x", {"uid": "600", "home": "/Users/guest"}, {"uid": 600, "home": "/"}):
            todo, _, errors = self._plan({}, created_state(guest=rec), account("guest", 600))
            self.assertEqual(todo, [], rec)
            self.assertIn("malformed", errors[0])

    def test_protected_is_kept(self):
        todo, _, errors = self._plan(
            {},
            created_state(guest=GUEST_REC),
            account("guest", 600),
            protected=lambda _n, _u: "is the current user",
        )
        self.assertEqual(todo, [])
        self.assertIn("is the current user; never deleted", errors[0])


class ProtectedTest(unittest.TestCase):
    def _why(self, name, uid, getuid=0, invoker=INVOKER):
        side = invoker if isinstance(invoker, Exception) else None
        with (
            mock.patch.object(users.os, "getuid", return_value=getuid),
            mock.patch.object(users.privilege, "invoker", return_value=invoker, side_effect=side),
        ):
            return users._protected(name, uid)

    def test_cases(self):
        self.assertIn("root", self._why("root", 600))
        self.assertIn("root", self._why("guest", 0))
        self.assertIn("below 501", self._why("guest", 500))
        self.assertIn("current user", self._why("guest", 600, getuid=600))
        self.assertIn("SUDO_USER", self._why("ivan", 600))
        self.assertIn("SUDO_USER", self._why("other", 502))
        self.assertIn("SUDO_USER", self._why("guest", 600, invoker=privilege.SudoUserError("x")))
        self.assertIsNone(self._why("guest", 600))


class CreateDeleteInstallTest(unittest.TestCase):
    def setUp(self):
        for patch in (
            mock.patch.object(users.sys, "platform", "darwin"),
            mock.patch.object(users, "read_shells", return_value=[FISH, "/bin/zsh"]),
            mock.patch.object(users, "_shell_error", return_value=None),
            mock.patch.object(users, "local_uid", side_effect=lambda n: self.local.get(n)),
            mock.patch.object(users.os.path, "lexists", return_value=False),
            mock.patch.object(users.os, "getuid", return_value=0),
            mock.patch.object(users.privilege, "invoker", return_value=INVOKER),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.local = {"ivan": 502}

    def _install(self, raw, state, *accounts, fail_on=None):
        commands = []

        def run(cmd, env=None, cwd=None):
            commands.append(cmd)
            return (1, "", "boom") if cmd == fail_on else (0, "", "")

        ok = users.install_users(
            raw, state, root=True, getpwnam=getpwnam_of(*accounts), getpwuid=no_uid, run=run
        )
        return ok, commands

    def test_creates_disabled_account_and_records_it(self):
        state = {}
        raw = {"guest": {**GUEST["guest"], "deleteHome": True}}
        ok, commands = self._install(raw, state, account())
        self.assertTrue(ok)
        rec = ["/usr/bin/dscl", ".", "-create", "/Users/guest"]
        self.assertEqual(
            commands,
            [
                rec,
                rec + ["Password", "*"],
                rec + ["UniqueID", "600"],
                rec + ["PrimaryGroupID", "20"],
                rec + ["UserShell", "/bin/zsh"],
                rec + ["NFSHomeDirectory", "/Users/guest"],
                ["/usr/sbin/createhomedir", "-c", "-u", "guest"],
            ],
        )
        self.assertEqual(state, created_state(guest={**GUEST_REC, "deleteHome": True}))

    def test_failed_create_records_and_fails(self):
        state = {}
        rec = ["/usr/bin/dscl", ".", "-create", "/Users/guest"]
        ok, commands = self._install(GUEST, state, fail_on=rec + ["UniqueID", "600"])
        self.assertFalse(ok)
        self.assertEqual(len(commands), 3)
        self.assertEqual(state, created_state(guest=GUEST_REC))

    def test_failed_first_step_records_nothing(self):
        state = {}
        ok, _ = self._install(
            GUEST, state, fail_on=["/usr/bin/dscl", ".", "-create", "/Users/guest"]
        )
        self.assertFalse(ok)
        self.assertEqual(state, {"users": {"created": {}}})

    def test_refuses_create_when_home_exists(self):
        state = {}
        with mock.patch.object(users.os.path, "lexists", return_value=True):
            ok, commands = self._install(GUEST, state)
        self.assertEqual((ok, commands), (False, []))
        self.assertEqual(created(state), {})

    def test_create_needs_state(self):
        ok, commands = self._install(GUEST, None)
        self.assertEqual((ok, commands), (False, []))

    def test_deletes_recorded_account_keeping_home(self):
        self.local["guest"] = 600
        state = created_state(guest=dict(GUEST_REC))
        ok, commands = self._install({}, state, account("guest", 600))
        self.assertTrue(ok)
        self.assertEqual(commands, [["/usr/bin/dscl", ".", "-delete", "/Users/guest"]])
        self.assertEqual(created(state), {})

    def test_deletes_home_when_asked(self):
        self.local["guest"] = 600
        state = created_state(guest={**GUEST_REC, "deleteHome": True})
        st = os.stat_result((0o40755, 0, 0, 0, 600, 20, 0, 0, 0, 0))
        with mock.patch.object(users.os, "lstat", return_value=st):
            ok, commands = self._install({}, state, account("guest", 600))
        self.assertTrue(ok)
        self.assertEqual(commands[-1], ["/bin/rm", "-rf", "--", "/Users/guest"])

    def test_keeps_home_not_owned_by_account(self):
        self.local["guest"] = 600
        state = created_state(guest={**GUEST_REC, "deleteHome": True})
        st = os.stat_result((0o40755, 0, 0, 0, 502, 20, 0, 0, 0, 0))
        with mock.patch.object(users.os, "lstat", return_value=st):
            ok, commands = self._install({}, state, account("guest", 600))
        self.assertFalse(ok)
        self.assertEqual(commands, [["/usr/bin/dscl", ".", "-delete", "/Users/guest"]])

    def test_never_deletes_invoker_even_if_recorded(self):
        self.local["ivan"] = 502
        state = created_state(ivan={"uid": 502, "home": "/Users/ivan", "deleteHome": True})
        ok, commands = self._install({}, state, account())
        self.assertEqual((ok, commands), (False, []))
        self.assertIn("ivan", created(state))

    def test_never_deletes_unrecorded(self):
        self.local["guest"] = 600
        ok, commands = self._install({}, {}, account("guest", 600))
        self.assertEqual((ok, commands), (True, []))

    def test_delete_home_flag_follows_config(self):
        state = created_state(guest=dict(GUEST_REC))
        raw = {"guest": {**GUEST["guest"], "deleteHome": True}}
        ok, commands = self._install(raw, state, account("guest", 600))
        self.assertEqual((ok, commands), (True, []))
        self.assertTrue(state["users"]["created"]["guest"]["deleteHome"])

    def test_dscl_delete_failure_keeps_record(self):
        self.local["guest"] = 600
        state = created_state(guest=dict(GUEST_REC))
        ok, _ = self._install(
            {},
            state,
            account("guest", 600),
            fail_on=["/usr/bin/dscl", ".", "-delete", "/Users/guest"],
        )
        self.assertFalse(ok)
        self.assertIn("guest", created(state))

    def test_diff_rows(self):
        self.local["old"] = 601
        state = created_state(old={"uid": 601, "home": "/Users/old", "deleteHome": False})
        rows = users.diff_users(
            GUEST, state, root=True, getpwnam=getpwnam_of(account("old", 601)), getpwuid=no_uid
        )
        self.assertEqual(
            rows,
            [
                "  - delete account old (uid 601), keep home /Users/old",
                "  + create account guest (uid 600, shell /bin/zsh, home /Users/guest,"
                " no password)",
            ],
        )

    def test_has_work_with_records_only(self):
        self.assertTrue(users.has_work(None, created_state(guest=GUEST_REC)))
        self.assertFalse(users.has_work(None, {}))


created = users.created


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
        self._deploy({"users": MINI}, root=True).assert_called_once_with(MINI, mock.ANY)
        self._deploy({"users": MINI}, root=False).assert_called_once_with(MINI)

    def test_root_deploy_runs_with_records_only(self):
        state = created_state(guest=GUEST_REC)
        with (
            mock.patch.object(cli, "load_json", return_value={"root": state}),
            mock.patch.object(cli, "migrate_state_schema", side_effect=lambda s: s),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(cli, "save_json"),
            mock.patch.object(cli, "state_lock"),
            mock.patch.object(cli, "install_users", return_value=True) as inst,
        ):
            self.assertTrue(cli._deploy({}, "/tmp", root=True))
        inst.assert_called_once_with(None, state)

    def test_root_diff_lists_section(self):
        with mock.patch.object(diff, "diff_users", return_value=["  ~ row"]) as d:
            sections = diff._root_sections({"users": MINI}, "/tmp", (), {})
        d.assert_called_once_with(MINI, mock.ANY)
        self.assertIn(("users", ["  ~ row"]), sections)


if __name__ == "__main__":
    unittest.main()
