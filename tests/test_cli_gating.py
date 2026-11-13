import contextlib
import io
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from click.testing import CliRunner

from tools import cli, privilege
from tools.state import save_json


class DeployGatingTest(unittest.TestCase):
    """bun/npm index paths['bun'] and paths['nodejs'] directly, so calling
    them with nothing configured raised KeyError on a machine whose diff
    reported clean. Every section must be skippable the same way."""

    def _deploy(self, config, state, scope=()):
        with (
            mock.patch.object(cli, "load_json", return_value=state),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(cli, "save_json"),
            mock.patch.object(cli, "state_lock"),
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
            mock.patch.object(cli, "state_lock"),
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
            mock.patch.object(cli, "state_lock"),
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


class RestartUnitsWiringTest(unittest.TestCase):
    def _deploy(self, config, state, daemons=None):
        with (
            mock.patch.object(cli, "env_export_process"),
            mock.patch.object(cli, "load_json", return_value=state),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(cli, "save_json"),
            mock.patch.object(cli, "state_lock"),
        ):
            return cli._deploy(config, "/tmp", ("launchd",), daemons=daemons)

    def test_done_and_stale_units_are_settled(self):
        state = {"restartUnits": {}}
        for target, source in (("a", "templates/a"), ("b", "sops/b"), ("gone", "templates/g")):
            state["restartUnits"]["launchd:" + target] = {"sources": [source], "seq": 1}
        queue = []

        def fake_install(raw, st, **kw):
            self.assertEqual(kw["units"], {"a": ["templates/a"], "b": ["sops/b"],
                                           "gone": ["templates/g"]})  # fmt: skip
            kw["daemons"].append("b")
            kw["done"].add("a")
            return True

        with (
            mock.patch.object(cli, "install_launchd", side_effect=fake_install),
            mock.patch.object(cli, "stale_units", return_value={"gone"}) as stale,
        ):
            self.assertTrue(self._deploy({}, state, queue))
        self.assertEqual(stale.call_args.args[1], {"b": ["sops/b"], "gone": ["templates/g"]})
        self.assertEqual(state["restartUnits"], {"launchd:b": {"sources": ["sops/b"], "seq": 1}})
        self.assertEqual(queue, [["b", 1]])

    def test_fetch_write_owes_restart_in_same_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "bin", "agent")
            config = {
                "fetch": {
                    target: {
                        "url": "https://example.com/agent",
                        "restartUnits": ["launchd:org.example.agent"],
                    }
                }
            }
            state: dict = {}
            seen = []

            def fake_install(raw, st, **kw):
                seen.append(kw["units"])
                kw["done"].add("org.example.agent")
                return True

            with (
                mock.patch.object(cli, "env_export_process"),
                mock.patch.object(cli, "load_json", return_value=state),
                mock.patch.object(cli, "migrate_state_file"),
                mock.patch.object(cli, "save_json"),
                mock.patch.object(cli, "state_lock"),
                mock.patch.object(cli, "install_launchd", side_effect=fake_install),
                mock.patch("tools.user.fetch._open", return_value=io.BytesIO(b"bin")),
                mock.patch("sys.stdout", io.StringIO()),
            ):
                self.assertTrue(cli._deploy(config, tmp, ("fetch", "launchd")))
            self.assertEqual(seen, [{"org.example.agent": [f"fetch/{target}"]}])
            self.assertNotIn("restartUnits", state)

    def test_no_launchd_work_and_no_units_skips_launchd(self):
        with mock.patch.object(cli, "install_launchd") as inst:
            self.assertTrue(self._deploy({}, {}))
        inst.assert_not_called()

    def test_root_settles_only_restarted_daemons_in_user_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = os.path.join(tmp, "state.json")
            entry = {"sources": ["t"], "seq": 2}
            save_json(state_file, {"restartUnits": {"launchd:a": entry, "launchd:b": entry,
                                                    "launchd:c": entry}})  # fmt: skip
            inv = SimpleNamespace(login="me")
            config = {"stateFile": state_file}

            def restart(raw, labels, state, **kw):
                self.assertEqual(labels, ["a", "b", "c"])
                kw["done"].update({"a", "c"})
                return False

            with (
                mock.patch.object(cli, "_load_merged_config", return_value=config),
                mock.patch.object(cli, "restart_daemons", side_effect=restart),
                mock.patch.object(cli.privilege, "run_as_user", side_effect=lambda _inv, fn: fn()),
            ):
                # c was owed again (seq 2) after it was queued at seq 1.
                queued = [["a", 2], ["b", 2], ["c", 1]]
                self.assertFalse(cli._restart_daemons(inv, ["."], queued))
            self.assertEqual(
                cli.load_json(state_file)["restartUnits"],
                {"launchd:b": entry, "launchd:c": entry},
            )

    def read(self, payload: bytes):
        r, w = os.pipe()
        os.write(w, payload)
        os.close(w)
        out, daemons = [], []
        try:
            with mock.patch.object(cli, "log"):
                cli._read_restarts(r, out, daemons)
        finally:
            os.close(r)
        return out, daemons

    def test_restart_payload_carries_processes_and_daemons(self):
        self.assertEqual(
            self.read(b'{"processes": ["Dock"], "daemons": [["org.example.dns", 3]]}'),
            (["Dock"], [["org.example.dns", 3]]),
        )
        self.assertEqual(self.read(b""), ([], []))

    def test_restart_payload_with_bad_label_or_shape_is_dropped(self):
        for bad in (
            b'{"processes": [], "daemons": [["../x", 1]]}',
            b'{"processes": [], "daemons": [["org.example.dns", "1"]]}',
            b'{"processes": [], "daemons": ["org.example.dns"]}',
            b'{"processes": [], "daemons": "org.example.dns"}',
            b'{"processes": ["-x"], "daemons": []}',
            b'["Dock"]',
            b"nope",
        ):
            self.assertEqual(self.read(bad), ([], []), bad)


class SkillsGatingTest(unittest.TestCase):
    def _deploy(self, config, state):
        with (
            mock.patch.object(cli, "load_json", return_value=state),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(cli, "save_json"),
            mock.patch.object(cli, "state_lock"),
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
            mock.patch("tools.config._PINNED_HOSTNAME", None),
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
            mock.patch.object(cli, "state_lock"),
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
        self.queues = {}

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
        def run(*_a, **k):
            self.events.append((name, os.geteuid()))
            if name in self.queues and "restarts" in k:
                k["restarts"].extend(self.queues[name])
            return self.results.get((name, os.geteuid()), True)

        return run

    def run_cli(self, args, prompt="yes", show_diff=None):
        names = ("install_launchd", "install_defaults", "install_brew_packages", "install_dock")
        patches = [mock.patch.object(cli, n, side_effect=self.step(n)) for n in names]
        if show_diff is not None:
            patches.append(mock.patch.object(cli, "show_diff", side_effect=show_diff))
        with (
            mock.patch.object(privilege, "run_as_user", side_effect=self.fake_run_as_user),
            mock.patch.object(cli, "_load_merged_config", side_effect=self.load),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(
                cli, "run_restarts", side_effect=lambda r: self.events.append(("restart", r))
            ),
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
        return [e for e in self.events if e[0] not in ("load", "restart")]

    def test_machine_is_pinned_before_either_phase_loads_config(self):
        with mock.patch.object(
            cli, "pin_hostname", side_effect=lambda: self.events.append(("pin",))
        ):
            result = self.run_cli(["deploy", "--approve"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.events[0], ("pin",))

    def test_user_sections_then_system_sections_as_root(self):
        result = self.run_cli(["deploy", "--approve"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            self.steps(),
            [
                ("drop", 501),
                ("install_brew_packages", 501),
                ("install_brew_packages", 501),
                ("install_launchd", 501),
                ("install_defaults", 501),
                ("install_dock", 501),
                ("install_launchd", 0),
                ("install_defaults", 0),
                ("drop", 501),
            ],
        )
        self.assertEqual(
            self.ops[0].calls[:3], [("initgroups", "me", 20), ("setgid", 20), ("setuid", 501)]
        )
        self.assertIn("root", cli.load_json(self.user_state))
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

        def show_diff(_cfg, _dir, scope, root=False, only=None):
            seen.append((root, os.geteuid(), tuple(scope)))
            return False

        result = self.run_cli(["deploy"], show_diff=show_diff)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(seen, [(True, 0, privilege.ROOT_SECTIONS), (False, 501, ())])
        self.prompted.assert_called_once()
        self.assertIn(("install_brew_packages", 501), self.events)

    def test_only_user_changes_still_prompt_and_no_means_nothing_runs(self):
        result = self.run_cli(
            ["deploy"], prompt="no", show_diff=lambda _c, _d, _s, root=False, only=None: root
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
        self.assertEqual(
            self.steps(),
            [("drop", 501), ("install_brew_packages", 501), ("install_brew_packages", 501)],
        )
        self.assertNotIn("root", cli.load_json(self.user_state))

        self.events.clear()
        result = self.run_cli(["deploy", "--approve", "--scope", "launchd"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            self.steps(),
            [("drop", 501), ("install_launchd", 501), ("install_launchd", 0), ("drop", 501)],
        )

    def test_only_narrows_both_phases(self):
        self.config["launchd"]["labelPrefix"] = "org.x"
        self.config["launchd"]["daemons"]["other"] = {"command": "z"}
        seen = []
        step = self.step

        def recording(name):
            run = step(name)

            def wrapped(raw, *a, **k):
                seen.append((name, os.geteuid(), raw))
                return run(raw, *a, **k)

            return wrapped

        with mock.patch.object(self, "step", side_effect=recording):
            result = self.run_cli(
                ["deploy", "--approve", "--only", "launchd:d", "--only", "brew:a"]
            )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            [(n, uid) for n, uid, _raw in seen],
            [
                ("install_brew_packages", 501),
                ("install_brew_packages", 501),
                ("install_launchd", 501),
                ("install_launchd", 0),
            ],
        )
        for name, _uid, raw in seen:
            if name == "install_launchd":
                self.assertEqual(raw["daemons"], {"d": {"command": "x"}})
                self.assertEqual(raw["agents"], {})

        self.events.clear()
        result = self.run_cli(["deploy", "--approve", "--only", "launchd:nope"])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("launchd:nope: no such item", result.output)
        self.assertEqual(self.steps(), [])

    def test_only_user_state_failure_stops_before_the_root_phase(self):
        self.config["launchd"]["labelPrefix"] = "org.x"
        save_json(self.user_state, {"version": 2, "launchd": {"services": ["broken"]}})
        for args in (["deploy", "--approve"], ["deploy"]):
            with self.subTest(args=args):
                self.events.clear()
                result = self.run_cli([*args, "--only", "launchd:d"])
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIn("state for launchd is malformed", result.output)
                self.assertEqual(self.steps(), [("drop", 501)])
                self.prompted.assert_not_called()

    def run_with_mgmt(self, args):
        self.config["mgmt"] = {"x": {}}
        with (
            mock.patch.object(cli, "mgmt_has_work", return_value=True),
            mock.patch.object(cli, "install_mgmt", side_effect=self.step("install_mgmt")),
        ):
            return self.run_cli(args)

    def test_mgmt_runs_after_root_started_daemons(self):
        result = self.run_with_mgmt(["deploy", "--approve"])
        self.assertEqual(result.exit_code, 0, result.output)
        steps = self.steps()
        self.assertEqual(
            # The first drop saves root's records into the user's state.
            steps[-4:],
            [("install_defaults", 0), ("drop", 501), ("drop", 501), ("install_mgmt", 501)],
        )
        self.assertEqual(steps.count(("install_mgmt", 501)), 1)

        self.events.clear()
        result = self.run_with_mgmt(["deploy", "--approve", "--scope", "mgmt"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.steps(), [("drop", 501), ("install_mgmt", 501)])

    def test_failed_system_phase_skips_mgmt(self):
        self.results[("install_launchd", 0)] = False
        result = self.run_with_mgmt(["deploy", "--approve"])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("System sections failed; not running mgmt", result.output)
        self.assertNotIn(("install_mgmt", 501), self.events)

    def test_failed_user_phase_stops_before_the_system_phase(self):
        self.results[("install_brew_packages", 501)] = False
        result = self.run_cli(["deploy", "--approve"])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Deploy as me failed; not running the later phases", result.output)
        self.assertIn(("install_dock", 501), self.events)
        self.assertFalse([e for e in self.steps() if e[1] == 0])

    def test_failed_system_phase_fails_the_run(self):
        self.results[("install_launchd", 0)] = False
        result = self.run_cli(["deploy", "--approve"])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn(("install_dock", 501), self.events)
        self.assertIn(("install_defaults", 0), self.events)

    def test_restarts_run_once_after_both_phases(self):
        self.queues = {"install_defaults": ["Dock"], "install_dock": ["Dock", "SystemUIServer"]}
        result = self.run_cli(["deploy", "--approve"])
        self.assertEqual(result.exit_code, 0, result.output)
        restarts = [e for e in self.events if e[0] == "restart"]
        self.assertEqual(restarts, [("restart", ["Dock", "SystemUIServer"])])
        self.assertEqual(self.events[-1], restarts[0])

    def test_restarts_run_when_either_phase_fails(self):
        self.queues = {"install_defaults": ["Dock"]}
        cases = {
            "user phase fails": ("install_brew_packages", 501),
            "system phase fails": ("install_launchd", 0),
        }
        for label, failing in cases.items():
            with self.subTest(label):
                self.events.clear()
                self.results = {failing: False}
                result = self.run_cli(["deploy", "--approve"])
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertEqual(self.events[-1], ("restart", ["Dock"]))
                self.assertEqual(len([e for e in self.events if e[0] == "restart"]), 1)

    def test_child_restart_queue_with_bad_names_is_ignored(self):
        self.queues = {"install_defaults": ["Dock"], "install_dock": ["Finder", "-uroot"]}
        result = self.run_cli(["deploy", "--approve"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.events[-1], ("restart", ["Dock"]))
        self.assertIn("invalid process names", result.output)

    def test_oversized_child_restart_queue_neither_blocks_nor_skips_restarts(self):
        names = [f"p{i:05d}" + "x" * 90 for i in range(1000)]
        self.queues = {"install_defaults": ["Dock"], "install_dock": names}
        result = self.run_cli(["deploy", "--approve"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.events[-1], ("restart", ["Dock"]))
        self.assertIn("too large", result.output)

    def test_interrupt_during_user_phase_still_runs_both_queues(self):
        self.queues = {"install_defaults": ["Dock"], "install_dock": ["Finder"]}
        run_as_user = self.fake_run_as_user

        def interrupted(inv, fn):
            run_as_user(inv, fn)
            raise KeyboardInterrupt

        with mock.patch.object(self, "fake_run_as_user", side_effect=interrupted):
            result = self.run_cli(["deploy", "--approve"])
        self.assertNotEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.events[-1], ("restart", ["Dock", "Finder"]))

    def test_crashed_user_child_skips_the_system_phase(self):
        root_load = self.load

        def load(paths):
            if os.geteuid() != 0:
                raise RuntimeError("boom")
            return root_load(paths)

        with mock.patch.object(self, "load", side_effect=load):
            result = self.run_cli(["deploy", "--approve"])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(self.steps(), [("drop", 501)])
        self.assertEqual(self.events[-1], ("restart", []))

    def test_failed_user_diff_deploys_nothing(self):
        def show_diff(_c, _d, _s, root=False, only=None):
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

        def show_diff(_cfg, _dir, scope, root=False, only=None):
            seen.append((root, os.geteuid()))
            return not root

        result = self.run_cli(["diff"], show_diff=show_diff)
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(seen, [(True, 0), (False, 501)])
        seen.clear()
        result = self.run_cli(["diff"], show_diff=lambda *_a, **_k: seen.append(1) or True)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(seen), 2)


class DeployOrderTest(unittest.TestCase):
    """A fresh host converges in one run: runtimes before what runs them, user packages
    before the root phase that starts daemons on them."""

    def test_runtimes_install_before_what_runs_them(self):
        calls = []

        def rec(name):
            return lambda *a, **k: calls.append(name) or True

        config = {
            "brew": {"brews": ["node"]},
            "curlShell": {"https://example.com/install.sh": "bash"},
            "gitRepos": {"/r": "u"},
            "bun": {"packages": {"a": {}}},
            "npm": {"packages": {"a": {}}},
            "uv": {"packages": {"a": {}}},
            "go": {"packages": {"a": {}}},
            "cargo": {"packages": {"a": {}}},
            "pi": {"packages": {"a": {}}},
            "mcp": {"servers": {"a": {}}},
            "launchd": {"agents": {"a": {"command": "a"}}},
        }
        patches = {
            "env_export_process": None,
            "load_json": {},
            "migrate_state_file": None,
            "save_json": None,
            "state_lock": mock.MagicMock(),
            "install_files": True,
            "install_ollama_models": True,
            "install_flatpak_packages": True,
            "stale_units": set(),
        }
        recorded = {
            "install_brew_packages": "brew",
            "install_curl_shell_scripts": "curlShell",
            "install_git_repos": "gitRepos",
            "install_bun_packages": "bun",
            "install_npm_packages": "npm",
            "install_uv_packages": "uv",
            "install_go_packages": "go",
            "install_cargo_packages": "cargo",
            "install_pi_packages": "pi",
            "install_mcp_servers": "mcp",
            "install_launchd": "launchd",
        }
        with contextlib.ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(cli, name, return_value=value))
            for name, label in recorded.items():
                stack.enter_context(mock.patch.object(cli, name, side_effect=rec(label)))
            stack.enter_context(mock.patch.object(cli.sops_section, "has_work", return_value=True))
            stack.enter_context(
                mock.patch.object(cli.secrets_flow, "deploy_sops", side_effect=rec("sops"))
            )
            self.assertTrue(cli._deploy(config, "/tmp"))
        self.assertEqual(
            calls,
            ["brew", "sops", "gitRepos", "curlShell", "bun", "npm", "uv", "go", "cargo", "pi",
             "mcp", "brew", "launchd"],
        )  # fmt: skip


if __name__ == "__main__":
    unittest.main()
