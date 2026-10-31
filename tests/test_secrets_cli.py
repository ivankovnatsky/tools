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
