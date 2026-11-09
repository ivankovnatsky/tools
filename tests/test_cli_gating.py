import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from click.testing import CliRunner

from tools import cli, privilege


class DeployGatingTest(unittest.TestCase):
    """bun/npm index paths['bun'] and paths['nodejs'] directly, so calling
    them with nothing configured raised KeyError on a machine whose diff
    reported clean. Every section must be skippable the same way."""

    def _deploy(self, config, state, scope=()):
        with (
            mock.patch.object(cli, "load_json", return_value=state),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(cli, "save_json"),
        ):
            return cli._deploy(config, "/tmp", scope)

    def test_update_flag_reaches_scoped_sections_only(self):
        config = {"gitRepos": {"/r": "u"}, "brew": {"brews": ["a"]}}
        runner = CliRunner()
        with (
            mock.patch.object(cli, "_load_merged_config", return_value=config),
            mock.patch.object(cli, "load_json", return_value={}),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(cli, "save_json"),
            mock.patch.object(cli, "install_git_repos", return_value=True) as git,
            mock.patch.object(cli, "install_brew_packages", return_value=True) as brew,
        ):
            result = runner.invoke(
                cli.main, ["deploy", "--approve", "--scope", "gitRepos", "--update"]
            )
            self.assertEqual(result.exit_code, 0, result.output)
            git.assert_called_once_with({"/r": "u"}, mock.ANY, True)
            brew.assert_not_called()

            runner.invoke(cli.main, ["deploy", "--approve", "--scope", "gitRepos"])
            self.assertEqual(git.call_args.args[2], False)

    def test_update_flag_reaches_files(self):
        config = {"files": [{"dir": "d"}]}
        runner = CliRunner()
        with (
            mock.patch.object(cli, "_load_merged_config", return_value=config),
            mock.patch.object(cli, "load_json", return_value={}),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(cli, "save_json"),
            mock.patch.object(cli, "install_files", return_value=True) as inst,
        ):
            for args, update in (
                (["--scope", "files", "--update"], True),
                (["--scope", "files"], False),
            ):
                inst.reset_mock()
                result = runner.invoke(cli.main, ["deploy", "--approve", *args])
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(inst.call_args.args[5], update)
            inst.reset_mock()
            runner.invoke(cli.main, ["deploy", "--approve", "--scope", "brew", "--update"])
            inst.assert_not_called()

    def test_empty_config_does_not_crash(self):
        # No paths at all: the pre-fix code raised KeyError: 'bun'.
        self.assertTrue(self._deploy({}, {}))

    def test_bun_runs_when_state_has_packages(self):
        state = {"bun": {"packages": {"x": {"installed": True}}}}
        with mock.patch.object(cli, "install_bun_packages", return_value=True) as bun:
            self._deploy({}, state)

        bun.assert_called_once()

    def test_bun_skipped_when_nothing_configured_or_tracked(self):
        with mock.patch.object(cli, "install_bun_packages", return_value=True) as bun:
            self._deploy({}, {})

        bun.assert_not_called()

    def test_npm_skipped_when_nothing_configured_or_tracked(self):
        with mock.patch.object(cli, "install_npm_packages", return_value=True) as npm:
            self._deploy({}, {})

        npm.assert_not_called()

    def test_pi_runs_when_state_has_packages(self):
        state = {"pi": {"packages": {"x": {"installed": True}}}}
        with mock.patch.object(cli, "install_pi_packages", return_value=True) as pi_mock:
            self._deploy({}, state)

        pi_mock.assert_called_once()

    def test_pi_skipped_when_nothing_configured_or_tracked(self):
        with mock.patch.object(cli, "install_pi_packages", return_value=True) as pi_mock:
            self._deploy({}, {})

        pi_mock.assert_not_called()

    def test_git_repos_runs_when_only_state_remains(self):
        # Dropping the last entry from config must still reconcile, or the
        # state is orphaned and cleanup never happens.
        state = {"gitRepos": {"installed": ["~/repo"]}}
        with mock.patch.object(cli, "install_git_repos", return_value=True) as repos:
            self._deploy({}, state)

        repos.assert_called_once()

    def test_curl_shell_runs_when_only_state_remains(self):
        state = {"curlShell": {"installed": ["https://example/x.sh"]}}
        with mock.patch.object(cli, "install_curl_shell_scripts", return_value=True) as curl:
            self._deploy({}, state)

        curl.assert_called_once()

    def test_launchd_and_mgmt_get_sops_secrets_dir(self):
        config = {
            "sops": {
                "secretsDir": "/state/secrets",
                "secrets": {"t": {"key": "a", "sopsFile": "/f"}},
            },
            "launchd": {"services": {"s": {"command": "x"}}},
            "mgmt": {"nextdns": {}},
        }
        with (
            mock.patch.object(cli, "install_launchd", return_value=True) as ld,
            mock.patch.object(cli, "install_mgmt", return_value=True) as svc,
        ):
            self._deploy(config, {}, ("launchd", "mgmt"))

        for call in (ld, svc):
            self.assertEqual(call.call_args.kwargs["secrets_dir"], "/state/secrets")

    def test_invalid_sops_reaches_launchd_and_mgmt_as_none(self):
        config = {
            "sops": {"secretsDir": "rel", "secrets": {"t": {"key": "a", "sopsFile": "/f"}}},
            "launchd": {"services": {"s": {"command": "x"}}},
            "mgmt": {"nextdns": {}},
        }
        with (
            mock.patch.object(cli, "install_launchd", return_value=True) as ld,
            mock.patch.object(cli, "install_mgmt", return_value=True) as svc,
        ):
            self._deploy(config, {}, ("launchd", "mgmt"))

        for call in (ld, svc):
            self.assertIsNone(call.call_args.kwargs["secrets_dir"])
            self.assertIn("absolute path", call.call_args.kwargs["secrets_dir_error"])

    def test_show_diff_passes_secrets_dir(self):
        import tempfile

        from tools import diff

        sops_raw = {
            "secretsDir": "/state/secrets",
            "secrets": {"t": {"key": "a", "sopsFile": "/f"}},
        }
        with tempfile.TemporaryDirectory() as d:
            config = {
                "stateFile": f"{d}/state.json",
                "sops": sops_raw,
                "launchd": {"services": {"s": {"command": "x"}}},
                "mgmt": {"nextdns": {}},
            }
            with (
                mock.patch.object(diff, "diff_launchd", return_value=[]) as ld,
                mock.patch.object(diff, "diff_mgmt", return_value=[]) as svc,
            ):
                diff.show_diff(config, d, ("launchd", "mgmt"))

        for call in (ld, svc):
            self.assertEqual(call.call_args.kwargs["secrets_dir"], "/state/secrets")

    def test_section_failure_propagates(self):
        state = {"gitRepos": {"installed": ["~/repo"]}}
        with mock.patch.object(cli, "install_git_repos", return_value=False):
            self.assertFalse(self._deploy({}, state))


class SkillsGatingTest(unittest.TestCase):
    def _deploy(self, config, state):
        with (
            mock.patch.object(cli, "load_json", return_value=state),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(cli, "save_json"),
            mock.patch.object(cli, "install_skills", return_value=True) as install,
        ):
            cli._deploy(config, "/tmp")
        return install

    def test_skills_skipped_when_nothing_configured_or_tracked(self):
        self._deploy({}, {}).assert_not_called()

    def test_skills_run_when_only_state_remains(self):
        state = {"skills": {"installed": {"/x/pdf": {"skill": "a/pdf", "hash": "h"}}}}
        self._deploy({}, state).assert_called_once()


class DeployCleanDiffTest(unittest.TestCase):
    """A clean diff means nothing approval-worthy — not nothing to do.
    Non-destructive maintenance (git pulls) must run on every deploy, not
    only under --approve."""

    def test_clean_diff_still_deploys_without_prompting(self):
        with (
            mock.patch.object(cli, "_load_merged_config", return_value={}),
            mock.patch.object(cli, "show_diff", return_value=True),
            mock.patch.object(cli, "_deploy", return_value=True) as deploy,
            mock.patch.object(cli.click, "prompt") as prompt,
        ):
            result = CliRunner().invoke(cli.main, ["deploy"])

        self.assertEqual(result.exit_code, 0)
        deploy.assert_called_once()
        prompt.assert_not_called()

    def test_dirty_diff_still_requires_approval(self):
        with (
            mock.patch.object(cli, "_load_merged_config", return_value={}),
            mock.patch.object(cli, "show_diff", return_value=False),
            mock.patch.object(cli, "_deploy", return_value=True) as deploy,
            mock.patch.object(cli.click, "prompt", return_value="no"),
        ):
            result = CliRunner().invoke(cli.main, ["deploy"])

        self.assertEqual(result.exit_code, 1)
        deploy.assert_not_called()


USER = SimpleNamespace(pw_name="me", pw_uid=501, pw_gid=20, pw_dir="/Users/me", pw_shell="/bin/zsh")
ROOT_PW = SimpleNamespace(
    pw_name="toor", pw_uid=0, pw_gid=0, pw_dir="/var/root", pw_shell="/bin/sh"
)
SUBCOMMANDS = (
    ["deploy", "--approve"],
    ["apply", "--approve"],
    ["diff"],
    ["plan"],
    ["status"],
    ["watch", "--config", "."],
    ["reconcile"],
)


def _getpwnam(name):
    return {"me": USER, "toor": ROOT_PW}[name]


class RootInvocation:
    """Run the CLI as euid 0 with the given sudo environment."""

    def invoke(self, args, sudo_env, **patches):
        env = {k: v for k, v in os.environ.items() if k not in ("SUDO_USER", "SUDO_UID")}
        env.update(sudo_env)
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch("os.geteuid", return_value=0),
            mock.patch("pwd.getpwnam", side_effect=_getpwnam),
        ):
            return CliRunner().invoke(cli.main, args)


class InvokerTest(unittest.TestCase):
    def check(self, environ, error=None):
        if error is not None:
            with self.assertRaises(privilege.SudoUserError) as ctx:
                privilege.invoker(0, environ, _getpwnam)
            self.assertIn("must not run as root directly", str(ctx.exception))
            self.assertIn(error, str(ctx.exception))
            return None
        return privilege.invoker(0, environ, _getpwnam)

    def test_root_without_a_sudo_user_is_refused(self):
        for environ in ({}, {"SUDO_USER": ""}, {"SUDO_USER": "root"}):
            with self.subTest(environ=environ):
                self.check(environ, "")

    def test_unknown_or_root_sudo_user_is_refused(self):
        self.check({"SUDO_USER": "ghost"}, "'ghost' is not a known user")
        self.check({"SUDO_USER": "toor"}, "")

    def test_mismatched_sudo_uid_is_refused(self):
        self.check({"SUDO_USER": "me", "SUDO_UID": "0"}, "SUDO_UID 0 is not me's uid 501")
        self.check({"SUDO_USER": "me", "SUDO_UID": "502"}, "SUDO_UID 502")

    def test_valid_sudo_acts_for_that_user(self):
        for environ in ({"SUDO_USER": "me"}, {"SUDO_USER": "me", "SUDO_UID": "501"}):
            with self.subTest(environ=environ):
                inv = self.check(environ)
                self.assertEqual(
                    inv, privilege.Invoker(True, 501, "/Users/me", "me", 20, "/bin/zsh")
                )

    def test_normal_user_is_not_root(self):
        self.assertFalse(privilege.invoker(501, {"SUDO_USER": "root"}).root)

    def test_act_as_points_home_at_the_user(self):
        with mock.patch.dict(os.environ, {"HOME": "/var/root", "USER": "root"}):
            privilege.act_as(privilege.Invoker(True, 501, "/Users/me", "me"))
            self.assertEqual(os.path.expanduser("~/x"), "/Users/me/x")
            self.assertEqual((os.environ["USER"], os.environ["LOGNAME"]), ("me", "me"))

    def test_act_as_drops_user_controlled_env(self):
        unsafe = {k: "/Users/me/x" for k in (*privilege.UNSAFE_ENV, "DYLD_INSERT_LIBRARIES")}
        with (
            mock.patch.dict(os.environ, unsafe),
            mock.patch.object(tempfile, "tempdir", "/Users/me/tmp"),
        ):
            privilege.act_as(privilege.Invoker(True, 501, "/Users/me", "me"))
            self.assertEqual([k for k in unsafe if k in os.environ], [])
            self.assertIsNone(tempfile.tempdir)
            self.assertFalse(tempfile.gettempdir().startswith("/Users/me"))
        with mock.patch.dict(os.environ, {"TMPDIR": "/Users/me/t"}):
            privilege.act_as(privilege.Invoker(False, 501, "/Users/me", "me"))
            self.assertEqual(os.environ["TMPDIR"], "/Users/me/t")

    def test_under_home_ignores_case_and_follows_symlinks(self):
        self.assertTrue(privilege.under_home("/users/ME/Library/x", "/Users/me"))
        self.assertTrue(privilege.under_home("/Users/me/x", "/USERS/ME"))
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(home)
            link = os.path.join(tmp, "link")
            os.symlink(home, link)
            self.assertTrue(privilege.under_home(os.path.join(link, "x"), home))
            self.assertTrue(privilege.under_home(os.path.join(home, "x"), link))

    def test_under_home(self):
        self.assertTrue(privilege.under_home("/Users/me/Library/x", "/Users/me"))
        self.assertTrue(privilege.under_home("/Users/me", "/Users/me"))
        self.assertFalse(privilege.under_home("/Users/meow/x", "/Users/me"))
        self.assertFalse(privilege.under_home("/Library/LaunchDaemons/x", "/Users/me"))


class UserDeploySystemKeysTest(unittest.TestCase):
    def test_pending_system_defaults_alone_do_not_prompt(self):
        from fake_defaults import FakeDefaults

        from tools.user import defaults

        fake = FakeDefaults()
        config = {
            "stateFile": "/nonexistent/state.json",
            "defaults": {"system": {"/Library/Preferences/com.example.app": {"k": 1}}},
        }
        with (
            mock.patch.object(cli, "_load_merged_config", return_value=config),
            mock.patch.object(defaults.sys, "platform", "darwin"),
            mock.patch.object(defaults.DefaultsCLI, "for_host", return_value=fake.cli()),
            mock.patch.object(cli, "load_json", return_value={}),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(cli, "save_json"),
            mock.patch("tools.state.load_json", return_value={}),
            mock.patch.object(cli.click, "prompt") as prompt,
        ):
            result = CliRunner().invoke(cli.main, ["deploy"])
        self.assertEqual(result.exit_code, 0, result.output)
        prompt.assert_not_called()
        self.assertIn("system key(s) differ; run sudo tools deploy --scope defaults", result.output)
        self.assertEqual(fake.writes(), [])


class RootRefusalTest(RootInvocation, unittest.TestCase):
    """Every subcommand refuses root unless it came through sudo from a normal user."""

    BAD = {
        "plain root": {},
        "empty SUDO_USER": {"SUDO_USER": ""},
        "SUDO_USER=root": {"SUDO_USER": "root"},
        "unknown SUDO_USER": {"SUDO_USER": "ghost"},
        "SUDO_USER resolving to uid 0": {"SUDO_USER": "toor"},
        "mismatched SUDO_UID": {"SUDO_USER": "me", "SUDO_UID": "0"},
    }

    def test_every_subcommand_refuses(self):
        for case, sudo_env in self.BAD.items():
            for args in SUBCOMMANDS:
                with self.subTest(case=case, cmd=args[0]):
                    with mock.patch.object(cli, "_load_merged_config") as load:
                        result = self.invoke(args, sudo_env)
                    self.assertNotEqual(result.exit_code, 0, result.output)
                    self.assertIn("tools must not run as root directly", result.output)
                    load.assert_not_called()

    def test_watch_and_reconcile_refuse_even_through_sudo(self):
        for args in (["watch", "--config", "."], ["reconcile"]):
            with self.subTest(cmd=args[0]):
                result = self.invoke(args, {"SUDO_USER": "me", "SUDO_UID": "501"})
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIn(f"{args[0]} does not run as root", result.output)


class FakeOps:
    """os as seen by a root process: setuid away from 0 is permanent."""

    def __init__(self, sticky=True):
        self.calls = []
        self.uid = self.gid = 0
        self.sticky = sticky

    def initgroups(self, login, gid):
        self.calls.append(("initgroups", login, gid))

    def setgroups(self, groups):
        self.calls.append(("setgroups", groups))

    def setgid(self, gid):
        self.calls.append(("setgid", gid))
        self.gid = gid

    def setuid(self, uid):
        self.calls.append(("setuid", uid))
        if self.uid != 0 and self.sticky:
            raise PermissionError("Operation not permitted")
        self.uid = uid

    def getuid(self):
        return self.uid

    geteuid = getuid

    def getgid(self):
        return self.gid

    getegid = getgid


ME = privilege.Invoker(True, 501, "/Users/me", "me", 20, "/bin/zsh")


class DropPrivilegesTest(unittest.TestCase):
    def test_groups_then_gid_then_uid_and_root_is_unreachable(self):
        ops = FakeOps()
        privilege.drop_privileges(ME, ops)
        self.assertEqual(
            ops.calls,
            [("initgroups", "me", 20), ("setgid", 20), ("setuid", 501), ("setuid", 0)],
        )
        self.assertEqual((ops.uid, ops.gid), (501, 20))

    def test_regaining_root_is_an_error_and_fn_never_runs(self):
        ran = []
        with mock.patch.dict(os.environ, {}), mock.patch.object(privilege, "UNDER_SUDO", False):
            rc = privilege.as_user(ME, lambda: ran.append(1) or 0, FakeOps(sticky=False))
        self.assertEqual((rc, ran), (1, []))

    def test_no_login_sets_only_the_primary_group(self):
        ops = FakeOps()
        privilege.drop_privileges(privilege.Invoker(True, 501, "/h", None, 20), ops)
        self.assertEqual(ops.calls[0], ("setgroups", [20]))

    def test_user_environ(self):
        environ = {
            "HOME": "/var/root",
            "USER": "root",
            "SHELL": "/bin/sh",
            "TMPDIR": "/x",
            "SUDO_USER": "me",
            "SUDO_COMMAND": "tools deploy",
            "PATH": "/usr/bin:/custom",
            "KEEP": "1",
        }
        with mock.patch.object(privilege, "user_path", return_value="P") as path:
            privilege.user_environ(ME, environ)
        path.assert_called_once_with(ME, "/usr/bin:/custom")
        self.assertEqual(
            environ,
            {
                "HOME": "/Users/me",
                "USER": "me",
                "LOGNAME": "me",
                "SHELL": "/bin/zsh",
                "PATH": "P",
                "KEEP": "1",
            },
        )

    def test_user_path_keeps_sudo_path_and_adds_known_dirs(self):
        exists = {"/Users/me/.local/bin", "/opt/homebrew/bin", "/usr/local/bin"}
        path = privilege.user_path(ME, "/usr/bin:/opt/homebrew/bin:/x", exists.__contains__)
        self.assertEqual(
            path.split(":"),
            [
                "/Users/me/.local/bin",
                "/usr/local/bin",
                "/usr/bin",
                "/opt/homebrew/bin",
                "/x",
                "/bin",
                "/usr/sbin",
                "/sbin",
            ],
        )
        self.assertEqual(
            privilege.user_path(ME, None, lambda _d: False), "/usr/bin:/bin:/usr/sbin:/sbin"
        )


class SudoDeployTest(RootInvocation, unittest.TestCase):
    SUDO = {"SUDO_USER": "me", "SUDO_UID": "501"}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        self.user_state = os.path.join(self.tmp, "user", "state.json")
        os.makedirs(os.path.dirname(self.user_state))
        self.root_state = os.path.join(self.tmp, "root", "state.json")
        self.config = {
            "stateFile": self.user_state,
            "launchd": {"daemons": {"d": {"command": "x"}}, "agents": {"a": {"command": "y"}}},
            "defaults": {"system": {"/Library/Preferences/x": {"k": 1}}, "user": {"x": {"k": 1}}},
            "brew": {"brews": ["a"]},
            "dock": {"apps": []},
        }
        self.events = []
        self.ops = []
        self.results = {}

    def load(self, _paths):
        self.events.append(("load", os.geteuid(), os.environ["HOME"]))
        return self.config

    def fake_run_as_user(self, inv, fn):
        """run_as_user without the fork: the real drop sequence on fake ids, then fn with
        euid 501."""
        ops = FakeOps()
        self.ops.append(ops)
        self.events.append(("drop", inv.uid))
        with (
            mock.patch("os.geteuid", side_effect=ops.geteuid),
            mock.patch("os.getuid", side_effect=ops.getuid),
            mock.patch.object(privilege, "UNDER_SUDO", False),
        ):
            return privilege.as_user(inv, fn, ops)

    def step(self, name):
        def run(*_a, **_k):
            self.events.append((name, os.geteuid()))
            return self.results.get((name, os.geteuid()), True)

        return run

    def run_cli(self, args, prompt="yes", show_diff=None):
        names = ("install_launchd", "install_defaults", "install_brew_packages", "install_dock")
        patches = [mock.patch.object(cli, n, side_effect=self.step(n)) for n in names]
        if show_diff is not None:
            patches.append(mock.patch.object(cli, "show_diff", side_effect=show_diff))
        with (
            mock.patch.object(privilege, "ROOT_STATE_FILE", self.root_state),
            mock.patch.object(privilege, "run_as_user", side_effect=self.fake_run_as_user),
            mock.patch.object(cli, "_load_merged_config", side_effect=self.load),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(cli, "run_restarts"),
            mock.patch.object(cli.click, "prompt", return_value=prompt) as prompted,
        ):
            for p in patches:
                p.start()
            try:
                result = self.invoke(args, self.SUDO)
            finally:
                for p in patches:
                    p.stop()
        self.prompted = prompted
        return result

    def steps(self):
        return [e for e in self.events if e[0] not in ("load",)]

    def test_system_sections_as_root_then_everything_else_as_the_user(self):
        result = self.run_cli(["deploy", "--approve"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            self.steps(),
            [
                ("install_launchd", 0),
                ("install_defaults", 0),
                ("drop", 501),
                ("install_brew_packages", 501),
                ("install_launchd", 501),
                ("install_defaults", 501),
                ("install_dock", 501),
            ],
        )
        self.assertEqual(
            self.ops[0].calls[:3], [("initgroups", "me", 20), ("setgid", 20), ("setuid", 501)]
        )
        self.assertTrue(os.path.exists(self.root_state))
        self.assertTrue(os.path.exists(self.user_state))
        self.assertEqual({e[2] for e in self.events if e[0] == "load"}, {"/Users/me"})

    def test_user_sections_never_run_as_root(self):
        for args in (["deploy", "--approve"], ["deploy", "--approve", "--scope", "brew"]):
            with self.subTest(args=args):
                self.events.clear()
                self.run_cli(args)
                user_steps = [
                    e for e in self.steps() if e[0] in ("install_brew_packages", "install_dock")
                ]
                self.assertTrue(user_steps)
                self.assertTrue(all(uid == 501 for _n, uid in user_steps), user_steps)

    def test_one_prompt_covers_both_diffs(self):
        seen = []

        def show_diff(_cfg, _dir, scope, root=False):
            seen.append((root, os.geteuid(), tuple(scope)))
            return False

        result = self.run_cli(["deploy"], show_diff=show_diff)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(seen, [(True, 0, ("launchd", "defaults")), (False, 501, ())])
        self.prompted.assert_called_once()
        self.assertIn(("install_brew_packages", 501), self.events)

    def test_only_user_changes_still_prompt_and_no_means_nothing_runs(self):
        result = self.run_cli(
            ["deploy"], prompt="no", show_diff=lambda _c, _d, _s, root=False: root
        )
        self.assertEqual(result.exit_code, 1, result.output)
        self.prompted.assert_called_once()
        self.assertEqual(self.steps(), [("drop", 501)])

    def test_clean_diffs_do_not_prompt(self):
        result = self.run_cli(["deploy"], show_diff=lambda *_a, **_k: True)
        self.assertEqual(result.exit_code, 0, result.output)
        self.prompted.assert_not_called()

    def test_scope_restricts_both_phases(self):
        result = self.run_cli(["deploy", "--approve", "--scope", "brew"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.steps(), [("drop", 501), ("install_brew_packages", 501)])
        self.assertFalse(os.path.exists(self.root_state))

        self.events.clear()
        result = self.run_cli(["deploy", "--approve", "--scope", "launchd"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            self.steps(), [("install_launchd", 0), ("drop", 501), ("install_launchd", 501)]
        )

    def test_failed_system_phase_stops_before_the_user_phase(self):
        self.results[("install_launchd", 0)] = False
        result = self.run_cli(["deploy", "--approve"])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("System sections failed; not running the rest as me", result.output)
        self.assertNotIn(("drop", 501), self.events)

    def test_failed_user_phase_fails_the_run(self):
        self.results[("install_brew_packages", 501)] = False
        result = self.run_cli(["deploy", "--approve"])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn(("install_dock", 501), self.events)

    def test_failed_user_diff_deploys_nothing(self):
        def show_diff(_c, _d, _s, root=False):
            if not root:
                raise SystemExit(1)
            return False

        result = self.run_cli(["deploy"], show_diff=show_diff)
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Diff as me failed; nothing deployed", result.output)
        self.prompted.assert_not_called()
        self.assertEqual(self.steps(), [("drop", 501)])

    def test_diff_runs_both_phases(self):
        seen = []

        def show_diff(_cfg, _dir, scope, root=False):
            seen.append((root, os.geteuid()))
            return not root

        result = self.run_cli(["diff"], show_diff=show_diff)
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(seen, [(True, 0), (False, 501)])
        seen.clear()
        result = self.run_cli(["diff"], show_diff=lambda *_a, **_k: seen.append(1) or True)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(seen), 2)


if __name__ == "__main__":
    unittest.main()
