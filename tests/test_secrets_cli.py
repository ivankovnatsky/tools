import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tools import cli, diff
from tools.user import secrets_flow, sops


class SecretsGatingTest(unittest.TestCase):
    def _deploy(self, config, state):
        calls = []
        with (
            mock.patch.object(cli, "load_json", return_value=state),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(cli, "save_json"),
            mock.patch.object(
                secrets_flow,
                "deploy_sops",
                side_effect=lambda *a, **k: calls.append("sops") or True,
            ),
            mock.patch.object(
                secrets_flow,
                "deploy_templates",
                side_effect=lambda *a, **k: calls.append("templates") or True,
            ),
        ):
            self.assertTrue(cli._deploy(config, "/tmp"))
        return calls

    def test_absent_sections_are_noops(self):
        self.assertEqual(self._deploy({}, {}), [])

    def test_sops_runs_before_templates(self):
        config = {"sops": {"secrets": {"a": {}}}, "templates": {"t": {}}}
        self.assertEqual(self._deploy(config, {}), ["sops", "templates"])

    def test_runs_when_only_state_remains(self):
        state = {"sops": {"installed": {"/x": {}}}, "templates": {"installed": {"/y": {}}}}
        self.assertEqual(self._deploy({}, state), ["sops", "templates"])

    def test_non_mapping_sections_report_errors_without_crashing(self):
        for config in ({"sops": ["x"]}, {"sops": "x"}, {"templates": ["x"]}):
            with (
                mock.patch.object(cli, "load_json", return_value={}),
                mock.patch.object(cli, "migrate_state_file"),
                mock.patch.object(cli, "save_json"),
            ):
                self.assertFalse(cli._deploy(config, "/tmp"), config)
            with mock.patch("tools.state.find_state_file", return_value="/nonexistent"):
                self.assertFalse(diff.show_diff(config, "/tmp"), config)

    def test_malformed_files_entry_confined_to_files(self):
        config = {"sops": {"secrets": {"a": {}}}}
        for files in (["not-a-mapping"], [3, None], {"a": 1}, "x"):
            calls = []
            with (
                mock.patch.object(cli, "load_json", return_value={}),
                mock.patch.object(cli, "migrate_state_file"),
                mock.patch.object(cli, "save_json"),
                mock.patch.object(
                    secrets_flow,
                    "deploy_sops",
                    side_effect=lambda *a, **k: calls.append("sops") or True,
                ),
            ):
                self.assertTrue(cli._deploy({**config, "files": files}, "/tmp", ("sops",)))
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertFalse(cli._deploy({**config, "files": files}, "/tmp", ("files",)))
            self.assertEqual(calls, ["sops"], files)
            self.assertIn("files:", out.getvalue())
            with (
                mock.patch("tools.state.find_state_file", return_value="/nonexistent"),
                mock.patch.object(secrets_flow, "diff_sops", return_value=[]),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                diff.show_diff({**config, "files": files}, "/tmp", ("sops",))

    def test_deploy_shares_one_decryptor_with_templates(self):
        seen = {}
        with (
            mock.patch.object(cli, "load_json", return_value={}),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(cli, "save_json"),
            mock.patch.object(
                secrets_flow,
                "deploy_sops",
                side_effect=lambda raw, state, report, dec, **k: bool(seen.setdefault("s", dec)),
            ),
            mock.patch.object(
                secrets_flow,
                "deploy_templates",
                side_effect=lambda *a: bool(seen.setdefault("t", a[5])),
            ),
        ):
            cli._deploy({"sops": {"secrets": {"a": {}}}, "templates": {"t": {}}}, "/tmp")
        self.assertIsInstance(seen["s"], sops.Decryptor)
        self.assertIs(seen["s"], seen["t"])

    def test_secrets_materialize_before_every_consumer(self):
        calls = []

        def rec(name):
            return lambda *a, **k: calls.append(name) or True

        config = {
            "sops": {"secrets": {"a": {}}},
            "templates": {"t": {}},
            "mcp": {"servers": {"x": {}}},
            "npm": {"packages": {"p": {}}},
            "files": [],
        }
        with (
            mock.patch.object(cli, "load_json", return_value={}),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(cli, "save_json"),
            mock.patch.object(secrets_flow, "deploy_sops", side_effect=rec("sops")),
            mock.patch.object(secrets_flow, "deploy_templates", side_effect=rec("templates")),
            mock.patch.object(cli, "install_mcp_servers", side_effect=rec("mcp")),
            mock.patch.object(cli, "install_npm_packages", side_effect=rec("npm")),
            mock.patch.object(cli, "install_files", side_effect=rec("files")),
        ):
            cli._deploy(config, "/tmp")
        self.assertEqual(calls[:2], ["sops", "templates"])
        self.assertEqual(set(calls[2:]), {"mcp", "npm", "files"})

    def test_diff_skips_sops_when_absent(self):
        with mock.patch.object(sops, "Decryptor") as dec:
            with mock.patch("tools.state.find_state_file", return_value="/nonexistent"):
                self.assertTrue(diff.show_diff({}, "/tmp"))
        dec.assert_not_called()


class GitReposFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.repo = os.path.join(self.tmp, "src", "repo")
        sops_file = os.path.join(self.tmp, "secrets.yaml")
        with open(sops_file, "w") as f:
            f.write("ENC")
        self.secret = os.path.join(self.repo, ".env")
        self.rendered = os.path.join(self.repo, "conf.json")
        self.config = {
            "gitRepos": {self.repo: "https://example.invalid/r.git"},
            "sops": {
                "binary": "/fake/sops",
                "secretsDir": os.path.join(self.tmp, "secrets"),
                "defaultSopsFile": sops_file,
                "secrets": {"tok": {"key": "token", "path": self.secret}},
            },
            "templates": {"c": {"path": self.rendered, "format": "json", "content": {"a": 1}}},
        }
        self.git = []

    def tearDown(self):
        self._tmp.cleanup()

    def fake_git(self, cmd, env=None, cwd=None):
        self.git.append(cmd)
        if cmd[1] == "clone":
            os.makedirs(os.path.join(cmd[-1], ".git"))
        return 0, "", ""

    def patches(self):
        real = sops.Decryptor

        def decryptor(settings):
            return real(settings, runner=lambda c, e: (0, b'{"token": "tok-value"}', ""), env={})

        from tools.user import git_repos

        return [
            mock.patch.object(sops, "Decryptor", decryptor),
            mock.patch.object(git_repos, "system_bin", return_value="git"),
            mock.patch.object(git_repos, "system_dir", return_value="/usr/bin"),
            mock.patch.object(git_repos, "run_command", self.fake_git),
        ]

    def run_sections(self, active):
        state = {}
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            for p in self.patches():
                stack.enter_context(p)
            stack.enter_context(contextlib.redirect_stdout(out))
            ok = cli._run_sections(self.config, self.tmp, state, {}, set(active))
        return ok, out.getvalue(), state

    def show_diff(self, scope=()):
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            for p in self.patches():
                stack.enter_context(p)
            stack.enter_context(
                mock.patch("tools.state.find_state_file", return_value="/nonexistent")
            )
            stack.enter_context(contextlib.redirect_stdout(out))
            diff.show_diff({**self.config, "stateFile": "/nonexistent"}, self.tmp, scope)
        return out.getvalue()


class GitReposDestinationTest(GitReposFixture):
    def test_repo_cloned_before_secrets_and_templates_are_written(self):
        out = self.show_diff()
        self.assertIn(f"+ clone {self.repo}", out)
        self.assertIn(f"+ decrypt {self.secret}", out)
        self.assertIn(f"+ render {self.rendered}", out)
        ok, out, state = self.run_sections(diff.ALL_SECTIONS)
        self.assertTrue(ok, out)
        self.assertEqual([c[1] for c in self.git], ["clone"])
        self.assertNotIn("Not a git repo", out)
        self.assertEqual(state["gitRepos"]["installed"], [self.repo])
        with open(self.secret) as f:
            self.assertEqual(f.read(), "tok-value")
        self.assertTrue(os.path.exists(self.rendered))

    def test_paths_in_uncloned_repo_refused_when_gitrepos_not_run(self):
        out = self.show_diff(("sops", "templates"))
        self.assertIn("not cloned yet", out)
        ok, out, _state = self.run_sections(("sops", "templates"))
        self.assertFalse(ok)
        self.assertIn(f"inside gitRepos checkout {self.repo}, which is not cloned yet", out)
        self.assertFalse(os.path.exists(self.repo))

    def test_paths_in_non_git_destination_refused(self):
        os.makedirs(self.repo)
        out = self.show_diff()
        self.assertIn(f"! not a git repo: {self.repo}", out)
        self.assertIn("which is not a git checkout", out)
        ok, out, _state = self.run_sections(diff.ALL_SECTIONS)
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(self.secret))
        self.assertFalse(os.path.exists(self.rendered))


class GitReposSplitOrderTest(GitReposDestinationTest):
    def setUp(self):
        super().setUp()
        self.cred = os.path.join(self.tmp, "home", ".netrc")
        self.config["sops"]["secrets"]["cred"] = {"key": "token", "path": self.cred}
        self.config["templates"]["ssh"] = {
            "path": os.path.join(self.tmp, "home", "ssh_config"),
            "content": "Host x",
        }
        self.seen_at_clone = None

    def fake_git(self, cmd, env=None, cwd=None):
        if cmd[1] == "clone":
            self.seen_at_clone = (
                os.path.exists(self.cred),
                os.path.exists(os.path.join(self.tmp, "home", "ssh_config")),
                os.path.exists(self.secret),
            )
        return super().fake_git(cmd, env, cwd)

    def test_credentials_outside_repos_written_before_clone(self):
        ok, out, _state = self.run_sections(diff.ALL_SECTIONS)
        self.assertTrue(ok, out)
        self.assertEqual(self.seen_at_clone, (True, True, False))
        self.assertTrue(os.path.exists(self.secret))
        self.assertTrue(os.path.exists(self.rendered))

    def test_diff_follows_deploy_order(self):
        out = self.show_diff()
        cred = out.index(f"+ decrypt {self.cred}")
        clone = out.index(f"+ clone {self.repo}")
        inside = out.index(f"+ decrypt {self.secret}")
        self.assertLess(cred, clone)
        self.assertLess(clone, inside)
        self.assertLess(clone, out.index(f"+ render {self.rendered}"))

    def test_clone_bringing_parent_symlink_into_collision_is_refused(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        with open(os.path.join(self.tmp, "src.txt"), "w") as f:
            f.write("plain")
        target = os.path.join(outside, "tok")
        self.config["files"] = [{"source": "src.txt", "target": target}]
        self.config["sops"]["secrets"]["tok"]["path"] = os.path.join(self.repo, "alias", "tok")
        orig = self.fake_git

        def clone_with_link(cmd, env=None, cwd=None):
            rc = orig(cmd, env, cwd)
            if cmd[1] == "clone":
                os.symlink(outside, os.path.join(cmd[-1], "alias"))
            return rc

        self.fake_git = clone_with_link
        ok, out, _state = self.run_sections(diff.ALL_SECTIONS)
        self.assertFalse(ok)
        self.assertIn("claimed by files and sops", out)
        self.assertFalse(os.path.exists(target))


class TemplateUsingInRepoSecretTest(GitReposDestinationTest):
    def setUp(self):
        super().setUp()
        self.out = os.path.join(self.tmp, "home", "app.env")
        self.config["sops"]["secrets"]["jwt"] = {
            "generate": {},
            "path": os.path.join(self.repo, "jwt"),
        }
        self.config["templates"]["app"] = {
            "path": self.out,
            "content": "JWT=${sops:jwt}\nTOK=${sops:tok}\n",
        }

    def test_template_waits_for_in_repo_secret(self):
        out = self.show_diff()
        self.assertLess(out.index(f"+ clone {self.repo}"), out.index(f"render {self.out}"))
        ok, out, _state = self.run_sections(diff.ALL_SECTIONS)
        self.assertTrue(ok, out)
        with open(self.out) as f:
            self.assertIn("TOK=tok-value", f.read())

    def test_refused_in_repo_secret_blocks_outside_template(self):
        self.config["templates"]["app"]["content"] = "TOK=${sops:tok}\n"
        ok, out, _state = self.run_sections(("sops", "templates"))
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(self.out))


class SopsFileInRepoTest(GitReposFixture):
    def setUp(self):
        super().setUp()
        self.enc = os.path.join(self.repo, "secrets.yaml")
        self.key = os.path.join(self.tmp, "home", "api-key")
        self.out = os.path.join(self.tmp, "home", "app.env")
        self.config["sops"]["secrets"] = {
            "api": {"key": "token", "sopsFile": self.enc, "path": self.key}
        }
        self.config["templates"] = {"app": {"path": self.out, "content": "K=${sops:api}\n"}}

    def fake_git(self, cmd, env=None, cwd=None):
        rc = super().fake_git(cmd, env, cwd)
        if cmd[1] == "clone":
            with open(self.enc, "w") as f:
                f.write("ENC")
        return rc

    def test_secret_from_repo_sops_file_decrypted_after_clone(self):
        out = self.show_diff()
        clone = out.index(f"+ clone {self.repo}")
        self.assertLess(clone, out.index(f"{self.key}"))
        self.assertLess(clone, out.index(f"render {self.out}"))
        ok, out, _state = self.run_sections(diff.ALL_SECTIONS)
        self.assertTrue(ok, out)
        with open(self.key) as f:
            self.assertEqual(f.read(), "tok-value")
        with open(self.out) as f:
            self.assertEqual(f.read(), "K=tok-value\n")


class NestedReposOwnershipTest(unittest.TestCase):
    def test_nested_destination_refused_in_any_config_order(self):
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            outer = os.path.join(d, "outer")
            inner = os.path.join(outer, "inner")
            os.makedirs(os.path.join(outer, ".git"))
            path = os.path.join(inner, "x")
            for repos in ({outer: "u", inner: "u"}, {inner: "u", outer: "u"}):
                c = secrets_flow.ownership_conflicts(git_repos_raw=repos)
                self.assertIn("not cloned yet", c.get(path) or "", list(repos))


@unittest.skipUnless(
    shutil.which("sops") and shutil.which("age-keygen"), "sops and age-keygen not on PATH"
)
class RealSopsTest(unittest.TestCase):
    """End to end with a throwaway age key; never touches a real key or file."""

    def test_decrypt_with_generated_key(self):
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            key = os.path.join(d, "age.txt")
            subprocess.run(["age-keygen", "-o", key], check=True, capture_output=True)
            with open(key) as f:
                recipient = next(
                    line.split(": ", 1)[1].strip() for line in f if "public key" in line
                )
            plain = os.path.join(d, "plain.yaml")
            with open(plain, "w") as f:
                f.write("app:\n  token: throwaway-value\nlist: [a, b]\n")
            enc = os.path.join(d, "secrets.yaml")
            env = {"PATH": os.environ.get("PATH", "")}
            result = subprocess.run(
                ["sops", "--encrypt", "--age", recipient, plain],
                check=True,
                capture_output=True,
                env=env,
            )
            with open(enc, "wb") as f:
                f.write(result.stdout)

            raw = {
                "secretsDir": os.path.join(d, "out"),
                "defaultSopsFile": enc,
                "age": {"keyFile": key},
                "secrets": {"tok": {"key": "app/token"}, "b": {"key": "list/1"}},
            }
            parsed = sops.parse_sops(raw)
            p = sops.plan(parsed, {}, sops.Decryptor(parsed.settings, env=env))
            self.assertTrue(sops.apply(p, parsed.settings, {}).success)
            with open(os.path.join(d, "out", "tok")) as f:
                self.assertEqual(f.read(), "throwaway-value")
            with open(os.path.join(d, "out", "b")) as f:
                self.assertEqual(f.read(), "b")


if __name__ == "__main__":
    unittest.main()
