import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from click.testing import CliRunner

from tools import cli, runmode


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


USER = SimpleNamespace(pw_name="me", pw_uid=501, pw_dir="/Users/me")
ROOT_PW = SimpleNamespace(pw_name="toor", pw_uid=0, pw_dir="/var/root")
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
            with self.assertRaises(runmode.RootModeError) as ctx:
                runmode.invoker(0, environ, _getpwnam)
            self.assertIn("must not run as root directly", str(ctx.exception))
            self.assertIn(error, str(ctx.exception))
            return None
        return runmode.invoker(0, environ, _getpwnam)

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
                self.assertEqual(inv, runmode.Invoker(True, 501, "/Users/me", "me"))

    def test_normal_user_is_not_root_mode(self):
        self.assertFalse(runmode.invoker(501, {"SUDO_USER": "root"}).root)

    def test_act_as_points_home_at_the_user(self):
        with mock.patch.dict(os.environ, {"HOME": "/var/root", "USER": "root"}):
            runmode.act_as(runmode.Invoker(True, 501, "/Users/me", "me"))
            self.assertEqual(os.path.expanduser("~/x"), "/Users/me/x")
            self.assertEqual((os.environ["USER"], os.environ["LOGNAME"]), ("me", "me"))

    def test_act_as_drops_user_controlled_env(self):
        unsafe = {k: "/Users/me/x" for k in (*runmode.UNSAFE_ENV, "DYLD_INSERT_LIBRARIES")}
        with (
            mock.patch.dict(os.environ, unsafe),
            mock.patch.object(tempfile, "tempdir", "/Users/me/tmp"),
        ):
            runmode.act_as(runmode.Invoker(True, 501, "/Users/me", "me"))
            self.assertEqual([k for k in unsafe if k in os.environ], [])
            self.assertIsNone(tempfile.tempdir)
            self.assertFalse(tempfile.gettempdir().startswith("/Users/me"))
        with mock.patch.dict(os.environ, {"TMPDIR": "/Users/me/t"}):
            runmode.act_as(runmode.Invoker(False, 501, "/Users/me", "me"))
            self.assertEqual(os.environ["TMPDIR"], "/Users/me/t")

    def test_under_home_ignores_case_and_follows_symlinks(self):
        self.assertTrue(runmode.under_home("/users/ME/Library/x", "/Users/me"))
        self.assertTrue(runmode.under_home("/Users/me/x", "/USERS/ME"))
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(home)
            link = os.path.join(tmp, "link")
            os.symlink(home, link)
            self.assertTrue(runmode.under_home(os.path.join(link, "x"), home))
            self.assertTrue(runmode.under_home(os.path.join(home, "x"), link))

    def test_under_home(self):
        self.assertTrue(runmode.under_home("/Users/me/Library/x", "/Users/me"))
        self.assertTrue(runmode.under_home("/Users/me", "/Users/me"))
        self.assertFalse(runmode.under_home("/Users/meow/x", "/Users/me"))
        self.assertFalse(runmode.under_home("/Library/LaunchDaemons/x", "/Users/me"))


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


class RootModeDeployTest(RootInvocation, unittest.TestCase):
    SUDO = {"SUDO_USER": "me", "SUDO_UID": "501"}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        self.user_state = os.path.join(self.tmp, "user", "state.json")
        os.makedirs(os.path.dirname(self.user_state))
        with open(self.user_state, "w") as f:
            json.dump({"version": 2, "launchd": {"services": {}}}, f)
        self.root_state = os.path.join(self.tmp, "root", "state.json")
        self.config = {
            "stateFile": self.user_state,
            "launchd": {"daemons": {"d": {"command": "x"}}},
            "defaults": {"system": {"/Library/Preferences/x": {"k": 1}}},
            "brew": {"brews": ["a"]},
            "dock": {"apps": []},
        }
        self.homes = []

    def load(self, _paths):
        self.homes.append(os.environ["HOME"])
        return self.config

    def run_cli(self, args):
        with (
            mock.patch.object(runmode, "ROOT_STATE_FILE", self.root_state),
            mock.patch.object(cli, "_load_merged_config", side_effect=self.load),
            mock.patch.object(cli, "install_launchd", return_value=True) as launchd,
            mock.patch.object(cli, "install_defaults", return_value=True) as defaults,
            mock.patch.object(cli, "install_brew_packages", return_value=True) as brew,
            mock.patch.object(cli, "install_dock", return_value=True) as dock,
            mock.patch.object(cli, "install_mgmt", return_value=True) as mgmt,
            mock.patch.object(cli, "migrate_state_file") as migrate,
            mock.patch.object(cli, "run_restarts"),
        ):
            result = self.invoke(args, self.SUDO)
        return result, SimpleNamespace(
            launchd=launchd, defaults=defaults, brew=brew, dock=dock, mgmt=mgmt, migrate=migrate
        )

    def test_runs_only_root_sections_against_the_root_state(self):
        with open(self.user_state) as f:
            before = f.read()
        result, calls = self.run_cli(["deploy", "--approve"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Running as root: only launchd, defaults", result.output)
        calls.launchd.assert_called_once()
        calls.defaults.assert_called_once()
        for name in ("brew", "dock", "mgmt", "migrate"):
            getattr(calls, name).assert_not_called()
        self.assertEqual(self.homes, ["/Users/me"])
        self.assertTrue(os.path.exists(self.root_state))
        with open(self.user_state) as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(os.listdir(os.path.dirname(self.user_state)), ["state.json"])

    def test_explicit_root_scope_runs_only_that_section(self):
        result, calls = self.run_cli(["deploy", "--approve", "--scope", "launchd"])
        self.assertEqual(result.exit_code, 0, result.output)
        calls.launchd.assert_called_once()
        calls.defaults.assert_not_called()

    def test_user_only_scope_is_an_error(self):
        for section in ("brew", "dock", "files"):
            with self.subTest(section):
                result, calls = self.run_cli(["deploy", "--approve", "--scope", section])
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIn(f"{section} is user-only; run without sudo", result.output)
                calls.launchd.assert_not_called()
                self.assertFalse(os.path.exists(self.root_state))

    def test_diff_reads_only_the_root_state(self):
        with (
            mock.patch.object(runmode, "ROOT_STATE_FILE", self.root_state),
            mock.patch.object(cli, "_load_merged_config", side_effect=self.load),
            mock.patch("tools.diff.diff_launchd", return_value=[]) as diff_launchd,
            mock.patch("tools.diff.diff_defaults", return_value=[]) as diff_defaults,
            mock.patch("tools.diff.diff_brew") as diff_brew,
            mock.patch("tools.state.load_json", return_value={}) as load_json,
        ):
            result = self.invoke(["diff"], self.SUDO)
        self.assertEqual(result.exit_code, 0, result.output)
        load_json.assert_called_once_with(self.root_state)
        diff_launchd.assert_called_once()
        diff_defaults.assert_called_once()
        diff_brew.assert_not_called()


if __name__ == "__main__":
    unittest.main()
