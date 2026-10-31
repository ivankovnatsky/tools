import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from tools import fsutil
from tools.user import secrets_flow, templates
from tools.user.sops import Decryptor, parse_sops

SECRET = "s3cr3t-password-value"


class FakeSops:
    def __init__(self, doc):
        self.doc = doc

    def __call__(self, cmd, env):
        return 0, json.dumps(self.doc).encode(), ""


class TemplatesTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.sops_file = os.path.join(self.tmp, "secrets.yaml")
        with open(self.sops_file, "w") as f:
            f.write("ENC")
        self.secrets_dir = os.path.join(self.tmp, "secrets")
        self.sops_raw = {
            "binary": "/fake/sops",
            "secretsDir": self.secrets_dir,
            "defaultSopsFile": self.sops_file,
            "secrets": {"password": {"key": "password"}, "jwt": {"generate": {}}},
        }
        self.fake = FakeSops({"password": SECRET})
        self.state = {}
        self.report = secrets_flow.RunReport()
        self.target = os.path.join(self.tmp, "out", "app.json")

    def tearDown(self):
        self._tmp.cleanup()

    def decryptor(self):
        return Decryptor(parse_sops(self.sops_raw).settings, runner=self.fake, env={})

    def write_secret(self, name, value):
        os.makedirs(self.secrets_dir, exist_ok=True)
        with open(os.path.join(self.secrets_dir, name), "w") as f:
            f.write(value)

    def tpl(self, content, fmt="json", **extra):
        return {"app": {"path": self.target, "format": fmt, "content": content, **extra}}

    def deploy(self, raw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = secrets_flow.deploy_templates(
                raw, self.sops_raw, self.state, self.report, decryptor=self.decryptor()
            )
        self.assertNotIn(SECRET, out.getvalue())
        return ok, out.getvalue()

    def diff(self, raw):
        rows = secrets_flow.diff_templates(raw, self.sops_raw, self.state, self.decryptor())
        self.assertNotIn(SECRET, "\n".join(rows))
        return rows

    def read(self):
        with open(self.target) as f:
            return f.read()

    def mode(self):
        return stat.S_IMODE(os.stat(self.target).st_mode)


class RenderTest(TemplatesTestBase):
    def test_structured_render_and_default_mode(self):
        raw = self.tpl({"rpc-port": 9091, "name": "x"})
        self.assertEqual(self.diff(raw), [f"  + render {self.target}"])
        ok, _ = self.deploy(raw)
        self.assertTrue(ok)
        self.assertEqual(json.loads(self.read()), {"rpc-port": 9091, "name": "x"})
        self.assertEqual(self.mode(), 0o400)
        self.assertEqual(self.diff(raw), [])

    def test_render_accepts_any_python_mapping(self):
        from collections import OrderedDict

        t = templates.Template("t", "/x", "toml", OrderedDict(a={"b": 1}))
        self.assertEqual(templates.render(t, None), (b"[a]\nb = 1\n", False))

    def test_secret_template_masked_and_private(self):
        self.write_secret("password", SECRET)
        raw = self.tpl({"rpc-password": "${sops:password}"})
        self.assertEqual(self.diff(raw), [f"  + render {self.target} (secret)"])
        self.deploy(raw)
        self.assertEqual(json.loads(self.read()), {"rpc-password": SECRET})
        self.assertEqual(self.mode(), 0o400)
        self.assertEqual(self.mode() & 0o077, 0)

        self.fake.doc["password"] = SECRET + "-new"
        rows = self.diff(raw)
        self.assertEqual(rows, [f"  ~ update {self.target} (secret, diff suppressed)"])

    def test_diff_and_deploy_render_the_same_decrypted_value(self):
        # A stale on-disk secret file must not make deploy disagree with diff.
        self.write_secret("password", "stale")
        raw = self.tpl({"p": "${sops:password}"})
        self.assertEqual(self.diff(raw), [f"  + render {self.target} (secret)"])
        ok, _ = self.deploy(raw)
        self.assertTrue(ok)
        self.assertEqual(json.loads(self.read()), {"p": SECRET})
        self.assertEqual(self.diff(raw), [])

        self.fake.doc["password"] = SECRET + "-new"
        self.assertEqual(self.diff(raw), [f"  ~ update {self.target} (secret, diff suppressed)"])
        self.deploy(raw)
        self.assertEqual(json.loads(self.read()), {"p": SECRET + "-new"})
        self.assertEqual(self.diff(raw), [])

    def test_pending_generated_secret(self):
        raw = self.tpl({"j": "${sops:jwt}"})
        self.assertEqual(self.diff(raw)[0][:10], "  ~ render")

    def test_failed_secret_blocks_template_even_if_stale_file_exists(self):
        self.write_secret("password", "stale")
        self.report.failed_secrets = ("password",)
        ok, out = self.deploy(self.tpl({"p": "${sops:password}"}))
        self.assertFalse(ok)
        self.assertIn("failed to decrypt this run", out)

    def test_non_secret_update_shows_diff(self):
        self.deploy(self.tpl({"a": 1}))
        with mock.patch("tools.util._try_delta", return_value=None):
            rows = self.diff(self.tpl({"a": 2}))
        self.assertEqual(rows[0], f"  ~ update {self.target}")
        self.assertIn('+  "a": 2', rows[1])
        ok, out = self.deploy(self.tpl({"a": 2}))
        self.assertIn(f"Updated: {self.target}", out)
        self.assertEqual(self.report.changed["templates"], ["app"])

    def test_literal_secret_in_new_content_masks_diff(self):
        self.deploy(self.tpl("plain\n", fmt="raw"))
        pem = "-----BEGIN PRIVATE KEY-----\nMIIfakekeymaterial\n-----END PRIVATE KEY-----\n"
        rows = self.diff(self.tpl(pem, fmt="raw"))
        self.assertEqual(rows, [f"  ~ update {self.target} (secret, diff suppressed)"])
        _, out = self.deploy(self.tpl(pem, fmt="raw"))
        self.assertNotIn("MIIfakekeymaterial", out)

    def test_secret_flag_and_previous_secret_mask_diff(self):
        self.deploy(self.tpl({"a": 1}, secrets=True))
        self.assertEqual(self.mode(), 0o400)
        rows = self.diff(self.tpl({"a": 2}))
        self.assertEqual(rows, [f"  ~ update {self.target} (secret, diff suppressed)"])

    def test_explicit_mode_and_chmod_drift(self):
        raw = self.tpl({"a": 1}, mode="0640")
        self.deploy(raw)
        self.assertEqual(self.mode(), 0o640)
        os.chmod(self.target, 0o644)
        self.assertEqual(self.diff(raw), [f"  ~ chmod {self.target} -> 0o640"])

    def test_raw_with_escape_and_secret(self):
        self.write_secret("password", SECRET)
        raw = self.tpl("user:${sops:password}\nliteral $${HOME}\n", fmt="raw")
        self.deploy(raw)
        self.assertEqual(self.read(), f"user:{SECRET}\nliteral ${{HOME}}\n")

    def test_ini_and_env_formats(self):
        self.write_secret("password", SECRET)
        self.deploy(self.tpl({"APP": "x", "db": {"PASSWD": "${sops:password}"}}, fmt="ini"))
        self.assertEqual(self.read(), f"APP = x\n\n[db]\nPASSWD = {SECRET}\n")
        self.deploy(self.tpl({"KEY": "${sops:password}"}, fmt="env"))
        self.assertEqual(self.read(), f"KEY='{SECRET}'\n")
        self.deploy(self.tpl({"KEY": "${sops:password}"}, fmt="env", quote=False))
        self.assertEqual(self.read(), f"KEY={SECRET}\n")

    def test_unresolved_var_is_error_not_empty(self):
        ok, out = self.deploy(self.tpl({"a": "${leftover}"}))
        self.assertFalse(ok)
        self.assertIn("undefined variable 'leftover'", out)
        self.assertFalse(os.path.exists(self.target))

    def test_unknown_secret_is_error(self):
        rows = self.diff(self.tpl({"a": "${sops:nope}"}))
        self.assertIn("no sops secret named 'nope'", rows[0])

    def test_notify_reported_on_change_only(self):
        raw = self.tpl({"a": 1}, restartUnits=["launchd:org.example.app"])
        self.assertIn("    notify: launchd:org.example.app", self.diff(raw))
        self.deploy(raw)
        self.assertEqual(self.report.notify, [("launchd:org.example.app", "templates/app")])
        self.report = secrets_flow.RunReport()
        self.deploy(raw)
        self.assertEqual(self.report.notify, [])

    def conflicts(self, raw, files_entries=None):
        return secrets_flow.ownership_conflicts(
            files_entries=files_entries,
            sops_raw=self.sops_raw,
            templates_raw=raw,
            state=self.state,
            config_dir=self.tmp,
        )

    def test_conflict_with_tracked_files_and_sops_paths(self):
        self.state["files"] = {self.target: {"hash": "h"}}
        raw = self.tpl({"a": 1})
        rows = secrets_flow.diff_templates(
            raw, self.sops_raw, self.state, self.decryptor(), self.conflicts(raw)
        )
        self.assertIn("claimed by files and templates", rows[0])
        self.state = {}
        secret_path = os.path.join(self.secrets_dir, "password")
        raw = {"x": {"path": secret_path, "format": "raw", "content": ""}}
        rows = secrets_flow.diff_templates(
            raw, self.sops_raw, self.state, self.decryptor(), self.conflicts(raw)
        )
        self.assertIn("claimed by sops and templates", rows[0])

    def test_conflict_with_current_files_config_after_normalization(self):
        src = os.path.join(self.tmp, "src.txt")
        with open(src, "w") as f:
            f.write("x")
        same = os.path.join(os.path.dirname(self.target), ".", "app.json")
        raw = self.tpl({"a": 1})
        conflicts = self.conflicts(raw, [{"source": "src.txt", "target": same}])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = secrets_flow.deploy_templates(
                raw, self.sops_raw, self.state, self.report, conflicts
            )
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(self.target))

    def test_files_targets_from_config(self):
        src = os.path.join(self.tmp, "src.txt")
        with open(src, "w") as f:
            f.write("x")
        targets = secrets_flow.files_targets(
            [{"source": "src.txt", "target": self.target}], self.tmp
        )
        self.assertEqual(targets, [self.target])

    def test_quote_only_for_env(self):
        _, errors = templates.parse_templates(
            {"a": {"path": "/x", "format": "json", "content": {}, "quote": False}}
        )
        self.assertIn("only applies to format env", errors[0])

    def test_target_not_env_expanded(self):
        with mock.patch.dict(os.environ, {"X": "expanded"}):
            tmpls, errors = templates.parse_templates({"a": {"path": "/t/$X", "content": ""}})
        self.assertEqual(tmpls[0].target, "/t/$X")

    def test_parse_errors(self):
        tmpls, errors = templates.parse_templates(
            {
                "a": {"path": "rel/path", "content": ""},
                "b": {"path": "/x", "format": "xml", "content": ""},
                "c": {"path": "/x", "format": "ini", "content": "text"},
                "d": {"path": "/y", "content": "", "mode": 644},
                "e": {"path": "/z"},
                "g": {"path": 0, "content": ""},
                "f": {"path": "/w", "content": "", "extra": 1},
            }
        )
        self.assertEqual(tmpls, [])
        self.assertEqual(len(errors), 7)

    def test_chmod_keeps_secret_flag(self):
        raw = self.tpl({"a": 1}, secrets=True, mode="0600")
        self.deploy(raw)
        self.assertTrue(self.state["templates"]["installed"][self.target]["secret"])
        os.chmod(self.target, 0o644)
        self.deploy(self.tpl({"a": 1}, mode="0600"))
        self.assertEqual(self.mode(), 0o600)
        self.assertTrue(self.state["templates"]["installed"][self.target]["secret"])
        rows = self.diff(self.tpl({"a": 2}, mode="0600"))
        self.assertEqual(rows, [f"  ~ update {self.target} (secret, diff suppressed)"])

    def test_json_and_yaml_content_must_be_structured(self):
        for fmt in ("json", "yaml"):
            for bad in ("text", 1, True, None):
                _, errors = templates.parse_templates(
                    {"a": {"path": "/x", "format": fmt, "content": bad}}
                )
                self.assertIn(f"{fmt} content must be a mapping or a list", errors[0])
            tmpls, errors = templates.parse_templates(
                {"a": {"path": "/x", "format": fmt, "content": [1]}}
            )
            self.assertEqual((len(tmpls), errors), (1, []))

    def test_default_path_and_mode_follow_sops_nix(self):
        tmpls, errors = templates.parse_templates({"app.yaml": {"content": "x"}}, "/s")
        self.assertEqual(errors, [])
        self.assertEqual(tmpls[0].target, "/s/rendered/app.yaml")
        _, errors = templates.parse_templates({"a/b": {"content": "x"}}, "/s")
        self.assertIn("set path", errors[0])
        _, errors = templates.parse_templates({"a": {"content": "x"}})
        self.assertIn("path is required", errors[0])
        _, errors = templates.parse_templates({"a": {"target": "/x", "content": "x"}}, "/s")
        self.assertIn("unknown keys ['target']", errors[0])

    def test_duplicate_paths_via_alias_or_case_rejected(self):
        real = os.path.join(self.tmp, "real")
        os.makedirs(real)
        os.symlink(real, os.path.join(self.tmp, "alias"))
        _, errors = templates.parse_templates(
            {
                "one": {"path": f"{real}/app.conf", "content": "1"},
                "two": {"path": f"{self.tmp}/alias/app.conf", "content": "2"},
            }
        )
        self.assertIn("already used by one", errors[0])
        if not fsutil._case_insensitive(real):
            self.skipTest("case-sensitive volume")
        _, errors = templates.parse_templates(
            {
                "one": {"path": f"{real}/app.conf", "content": "1"},
                "three": {"path": f"{real}/APP.conf", "content": "3"},
            }
        )
        self.assertIn("already used by one", errors[0])

    def test_invalid_sops_settings_never_fall_back_to_default_dir(self):
        sops_raw = {"secretsDir": os.path.join(self.tmp, "mine"), "age": {"keyFile": 5}}
        raw = {"app.ini": {"content": "x"}}
        rows = secrets_flow.diff_templates(raw, sops_raw, self.state, self.decryptor())
        self.assertIn("default needs a valid sops section", rows[0])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = secrets_flow.deploy_templates(
                raw, sops_raw, self.state, self.report, decryptor=self.decryptor()
            )
        self.assertFalse(ok)
        self.assertEqual(self.state["templates"]["installed"], {})
        explicit = {"t": {"path": self.target, "content": "x"}}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(
                secrets_flow.deploy_templates(
                    explicit, sops_raw, self.state, self.report, decryptor=self.decryptor()
                )
            )

    def test_default_path_dirs_are_private_and_kept_private(self):
        raw = {"plain": {"content": "x\n", "mode": "0644"}}
        self.deploy(raw)
        rendered = os.path.join(self.secrets_dir, "rendered")
        for d in (self.secrets_dir, rendered):
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o751, d)
        self.assertIn(rendered, self.state["sops"]["createdDirs"])
        os.chmod(rendered, 0o755)
        self.deploy(raw)
        self.assertEqual(stat.S_IMODE(os.stat(rendered).st_mode), 0o751)

    def test_secret_named_like_rendered_dir_conflicts(self):
        self.sops_raw["secrets"]["rendered"] = {"key": "password"}
        raw = {"app.ini": {"content": "x"}}
        c = self.conflicts(raw)
        rendered = os.path.join(self.secrets_dir, "rendered")
        self.assertIn("a parent of", c[rendered])
        self.assertIn("inside", c[os.path.join(rendered, "app.ini")])

    def test_notify_only_null_is_absent(self):
        tmpls, errors = templates.parse_templates(
            {"a": {"path": "/x", "content": "", "restartUnits": None}}
        )
        self.assertEqual((tmpls[0].notify, errors), ((), []))
        for bad in ("", 0, False, {}):
            _, errors = templates.parse_templates(
                {"a": {"path": "/x", "content": "", "restartUnits": bad}}
            )
            self.assertIn("restartUnits must be a list of strings", errors[0])

    def test_invalid_sops_config_reported_instead_of_unknown_secret(self):
        self.sops_raw["age"] = []
        rows = self.diff(self.tpl({"p": "${sops:password}"}))
        self.assertIn("sops config is invalid", rows[0])
        self.assertIn("age must be a mapping", rows[0])
        self.assertNotIn("no sops secret named", rows[0])

    def test_unmanaged_symlink_target_refused_not_followed(self):
        pointee = os.path.join(self.tmp, "elsewhere.json")
        raw = self.tpl({"a": 1})
        rendered, _ = templates.render(templates.parse_templates(raw)[0][0], None)
        with open(pointee, "wb") as f:
            f.write(rendered)
        os.chmod(pointee, 0o640)
        os.makedirs(os.path.dirname(self.target))
        os.symlink(pointee, self.target)
        rows = self.diff(raw)
        self.assertIn("is a symlink", rows[0])
        ok, out = self.deploy(raw)
        self.assertFalse(ok)
        self.assertTrue(os.path.islink(self.target))
        self.assertEqual(stat.S_IMODE(os.stat(pointee).st_mode), 0o640)
        self.assertNotIn(self.target, self.state["templates"]["installed"])

    def test_exact_claim_matched_through_unmanaged_alias(self):
        real_dir = os.path.join(self.tmp, "config")
        os.makedirs(real_dir)
        alias = os.path.join(self.tmp, "cfg-alias")
        os.symlink(real_dir, alias)
        src = os.path.join(self.tmp, "src.txt")
        with open(src, "w") as f:
            f.write("x")
        real_target = os.path.join(real_dir, "app.ini")
        raw = {"t": {"path": real_target, "format": "raw", "content": "x"}}
        conflicts = self.conflicts(raw, [{"source": "src.txt", "target": f"{alias}/app.ini"}])
        self.assertEqual(conflicts.get(real_target), "claimed by files and templates")
        self.assertEqual(conflicts.get(f"{alias}/app.ini"), "claimed by files and templates")
        ok, _ = self.deploy_conflicted(raw, conflicts)
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(real_target))

    def deploy_conflicted(self, raw, conflicts):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = secrets_flow.deploy_templates(
                raw, self.sops_raw, self.state, self.report, conflicts, self.decryptor()
            )
        return ok, out.getvalue()

    def test_diff_marks_templates_of_failed_secrets_pending(self):
        secret_path = os.path.join(self.secrets_dir, "password")
        src = os.path.join(self.tmp, "src.txt")
        with open(src, "w") as f:
            f.write("x")
        raw = self.tpl({"p": "${sops:password}"})
        conflicts = self.conflicts(raw, [{"source": "src.txt", "target": secret_path}])
        dec = self.decryptor()
        sops_plan = secrets_flow.plan_sops(self.sops_raw, self.state, dec, conflicts)
        failed = secrets_flow.failed_secrets(sops_plan)
        self.assertEqual(failed, ("password",))
        rows = secrets_flow.diff_templates(raw, self.sops_raw, self.state, dec, conflicts, failed)
        self.assertTrue(rows[0].startswith(f"  ~ render {self.target} (pending"), rows)
        ok, out = self.deploy_conflicted_with_failed(raw, conflicts, failed)
        self.assertFalse(ok)
        self.assertIn("failed to decrypt this run", out)

    def deploy_conflicted_with_failed(self, raw, conflicts, failed):
        self.report.failed_secrets = failed
        return self.deploy_conflicted(raw, conflicts)

    def test_render_error_holds_removals(self):
        self.deploy(self.tpl({"a": 1}))
        raw = {"b": {"path": os.path.join(self.tmp, "b"), "format": "raw", "content": "${x}"}}
        rows = self.diff(raw)
        self.assertFalse(any(r.startswith("  - remove") for r in rows), rows)
        ok, _ = self.deploy(raw)
        self.assertFalse(ok)
        self.assertTrue(os.path.exists(self.target))
        self.assertIn(self.target, self.state["templates"]["installed"])

    def test_owner_and_group_type_checked(self):
        for key, bad in (("owner", False), ("group", True), ("owner", -1), ("group", -5)):
            tmpls, errors = templates.parse_templates(
                {"a": {"path": "/x", "content": "", key: bad}}
            )
            self.assertEqual(tmpls, [], (key, bad))
            self.assertIn(f"{key} must be a user/group name or a non-negative id", errors[0])

    def test_untracked_existing_target_content_never_shown(self):
        os.makedirs(os.path.dirname(self.target))
        with open(self.target, "w") as f:
            f.write("INTERNAL_TOKEN = handwrittenvalue\n")
        raw = self.tpl({"a": 1})
        rows = self.diff(raw)
        self.assertIn("exists and is not tracked by tools; set force: true", rows[0])
        self.assertNotIn("handwrittenvalue", "\n".join(rows))
        ok, out = self.deploy(raw)
        self.assertFalse(ok)
        self.assertNotIn("handwrittenvalue", out)
        self.assertIn("handwrittenvalue", self.read())

        raw = self.tpl({"a": 1}, force=True)
        rows = self.diff(raw)
        self.assertEqual(
            rows, [f"  ~ replace {self.target} (not tracked by tools; content not shown)"]
        )
        ok, out = self.deploy(raw)
        self.assertTrue(ok)
        self.assertNotIn("handwrittenvalue", out)
        self.assertIn("Replaced untracked", out)
        with mock.patch("tools.util._try_delta", return_value=None):
            rows = self.diff(self.tpl({"a": 2}, force=True))
        self.assertEqual(rows[0], f"  ~ update {self.target}")

    def respellings(self):
        """(declared, respelled) pairs naming the same file."""
        real = os.path.join(self.tmp, "Conf")
        os.makedirs(real, exist_ok=True)
        alias = os.path.join(self.tmp, "alias")
        if not os.path.lexists(alias):
            os.symlink(real, alias)
        pairs = [(os.path.join(real, "a.ini"), os.path.join(alias, "a.ini"))]
        if fsutil._case_insensitive(real):
            pairs.append((os.path.join(real, "b.ini"), os.path.join(self.tmp, "conf", "B.INI")))
        return pairs

    def test_respelled_path_keeps_file_and_single_entry(self):
        for first, second in self.respellings():
            self.state = {}
            self.deploy({"t": {"path": first, "content": "v"}})
            self.assertEqual(
                self.diff({"t": {"path": second, "content": "v"}}), [], (first, second)
            )
            ok, out = self.deploy({"t": {"path": second, "content": "v"}})
            self.assertTrue(ok, out)
            self.assertTrue(os.path.exists(first), (first, second))
            self.assertEqual(list(self.state["templates"]["installed"]), [second])


class LinkOwnershipTest(TemplatesTestBase):
    def setUp(self):
        super().setUp()
        self.checkout = os.path.join(self.tmp, "src", "checkout")
        os.makedirs(self.checkout)
        self.link = os.path.join(self.tmp, "home", ".claude", "skills", "jira")
        os.makedirs(os.path.dirname(self.link))
        os.symlink(self.checkout, self.link)
        self.links_raw = [{"source": self.checkout, "target": self.link}]

    def ownership(self, raw, links_raw=None, skills_raw=None):
        return secrets_flow.ownership_conflicts(
            sops_raw=self.sops_raw,
            templates_raw=raw,
            links_raw=self.links_raw if links_raw is None else links_raw,
            skills_raw=skills_raw,
            state=self.state,
            config_dir=self.tmp,
        )

    def deploy_with(self, raw, ownership):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = secrets_flow.deploy_templates(
                raw, self.sops_raw, self.state, self.report, ownership, self.decryptor()
            )
        return ok, out.getvalue()

    def test_template_onto_managed_link_refused(self):
        raw = {"t": {"path": self.link, "format": "raw", "content": "x"}}
        ok, out = self.deploy_with(raw, self.ownership(raw))
        self.assertFalse(ok)
        self.assertIn("a symlink managed by links", out)
        self.assertTrue(os.path.islink(self.link))

    def test_secret_through_managed_link_refused(self):
        target = os.path.join(self.link, "nested", "token.env")
        raw = {"t": {"path": target, "format": "raw", "content": "${sops:password}"}}
        ok, out = self.deploy_with(raw, self.ownership(raw))
        self.assertFalse(ok)
        self.assertIn(f"under {self.link}, a symlink managed by links", out)
        self.assertEqual(os.listdir(self.checkout), [])

    def test_resolved_parent_traversing_managed_link_refused(self):
        alias = os.path.join(self.tmp, "alias")
        os.symlink(os.path.dirname(self.link), alias)
        target = os.path.join(alias, "jira", "token.env")
        raw = {"t": {"path": target, "format": "raw", "content": "x"}}
        rows = secrets_flow.diff_templates(
            raw, self.sops_raw, self.state, self.decryptor(), self.ownership(raw)
        )
        self.assertIn("a symlink managed by links", rows[0])

    def test_tracked_link_in_state_refused_without_config(self):
        self.state["links"] = {"installed": {self.link: {"source": self.checkout}}}
        raw = {"t": {"path": os.path.join(self.link, "x"), "format": "raw", "content": "x"}}
        ownership = self.ownership(raw, links_raw=[])
        self.assertIn("managed by links", ownership.get(raw["t"]["path"]))
        self.assertEqual(dict(ownership), {})

    def test_sops_secret_inside_skill_dir_refused(self):
        self.state["skills"] = {
            "installed": {os.path.join(self.tmp, "agents", "pdf"): {"hash": "h"}},
            "links": {os.path.join(self.tmp, "home", "pdf"): {"source": "/s"}},
        }
        ownership = self.ownership({}, links_raw=[])
        inside = os.path.join(self.tmp, "agents", "pdf", "secret")
        self.assertIn("a directory managed by skills", ownership[inside])
        self.assertIn("managed by skills", ownership[os.path.join(self.tmp, "home", "pdf")])
        self.assertNotIn(os.path.join(self.tmp, "agents", "other"), ownership)

    def test_symlinked_parent_matched_by_real_path(self):
        dotfiles = os.path.join(self.tmp, "dotfiles")
        os.makedirs(os.path.join(dotfiles, "skills"))
        home_claude = os.path.join(self.tmp, "home2", ".claude")
        os.makedirs(os.path.dirname(home_claude))
        os.symlink(dotfiles, home_claude)
        managed = os.path.join(home_claude, "skills", "jira")
        os.symlink(self.checkout, managed)
        ownership = self.ownership({}, links_raw=[{"source": self.checkout, "target": managed}])
        real = os.path.join(dotfiles, "skills", "jira")
        self.assertIn("a symlink managed by links", ownership[real])
        self.assertIn("a symlink managed by links", ownership[os.path.join(real, "x")])
        self.assertIn("a symlink managed by links", ownership[os.path.join(managed, "x")])

    def test_unresolved_tmp_alias_matches_both_ways(self):
        alias_root = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(alias_root))
        alias = os.path.join(alias_root, "real")
        os.makedirs(alias)
        os.symlink(alias, os.path.join(alias_root, "via"))
        cases = [
            (os.path.join(alias_root, "via", "l"), os.path.join(alias_root, "real", "l")),
            (os.path.join(alias_root, "real", "l"), os.path.join(alias_root, "via", "l")),
        ]
        if os.path.realpath(alias_root) != alias_root:  # macOS /var -> /private/var
            unaliased = os.path.realpath(alias_root)
            cases.append(
                (os.path.join(alias_root, "real", "l"), os.path.join(unaliased, "real", "l"))
            )
            cases.append(
                (os.path.join(unaliased, "real", "l"), os.path.join(alias_root, "real", "l"))
            )
        for declared, queried in cases:
            ownership = self.ownership(
                {}, links_raw=[{"source": self.checkout, "target": declared}]
            )
            self.assertIn("managed by links", ownership.get(queried) or "", (declared, queried))
            self.assertIn("managed by links", ownership.get(os.path.join(queried, "f")) or "")
            skills_state = {"skills": {"installed": {declared: {"hash": "h"}}}}
            self.state = skills_state
            tree = self.ownership({}, links_raw=[])
            self.assertIn("managed by skills", tree.get(os.path.join(queried, "f")) or "")
            self.state = {}

    def test_alias_into_subdirectory_of_managed_tree_or_link_refused(self):
        sub = os.path.join(self.checkout, "sub")
        os.makedirs(sub)
        alias = os.path.join(self.tmp, "alias")
        os.symlink(os.path.join(self.link, "sub"), alias)
        ownership = self.ownership({})
        self.assertIn("the target of a symlink managed by links", ownership[f"{alias}/x"])
        self.assertIn("the target of a symlink managed by links", ownership[f"{sub}/x"])

        pdf = os.path.join(self.tmp, "agents", "pdf")
        os.makedirs(os.path.join(pdf, "sub"))
        self.state["skills"] = {"installed": {pdf: {"hash": "h"}}}
        alias2 = os.path.join(self.tmp, "alias2")
        os.symlink(os.path.join(pdf, "sub"), alias2)
        ownership = self.ownership({}, links_raw=[])
        self.assertIn("a directory managed by skills", ownership[f"{alias2}/x"])

    def test_uncloned_git_skill_paths_protected(self):
        skills_raw = {
            "cacheDir": os.path.join(self.tmp, "cache"),
            "sources": {"remote": {"url": "https://example.test/skills.git"}},
            "targets": [os.path.join(self.tmp, "agents")],
            "linkTargets": [os.path.join(self.tmp, "claude")],
            "enable": ["remote/pdf", {"skill": "remote/docx", "as": "word"}, "remote/*"],
        }
        ownership = self.ownership({}, links_raw=[], skills_raw=skills_raw)
        for path in ("agents/pdf/SKILL.md", "agents/word/x", "cache/remote/x"):
            self.assertIn("managed by skills", ownership[os.path.join(self.tmp, path)], path)
        self.assertIn(
            "a symlink managed by skills", ownership[os.path.join(self.tmp, "claude", "pdf")]
        )
        self.assertNotIn(os.path.join(self.tmp, "agents", "other"), ownership)

    def test_stale_template_under_managed_link_not_removed(self):
        target = os.path.join(self.link, "app.ini")
        with open(target, "w") as f:
            f.write("x")
        from tools import fsutil

        self.state["templates"] = {
            "installed": {target: {"name": "t", **fsutil.fingerprint(target)}}
        }
        self.state["links"] = {"installed": {self.link: {"source": self.checkout}}}
        ownership = self.ownership({}, links_raw=[])
        rows = secrets_flow.diff_templates(
            {}, self.sops_raw, self.state, self.decryptor(), ownership
        )
        self.assertTrue(rows[0].startswith(f"  ~ keep {target} (is under"), rows)
        ok, out = self.deploy_with({}, ownership)
        self.assertTrue(ok)
        self.assertIn("not removing", out)
        self.assertTrue(os.path.exists(os.path.join(self.checkout, "app.ini")))
        self.assertIn(target, self.state["templates"]["installed"])

    def test_link_matched_case_insensitively(self):
        if not fsutil._case_insensitive(self.tmp):
            self.skipTest("case-sensitive volume")
        upper = self.link.replace("/home/", "/HOME/")
        raw = {"t": {"path": os.path.join(upper, "x"), "format": "raw", "content": "x"}}
        self.assertIn("a symlink managed by links", self.ownership(raw)[raw["t"]["path"]])

    def test_unrelated_target_still_written(self):
        raw = self.tpl({"a": 1})
        ok, _ = self.deploy_with(raw, self.ownership(raw))
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(self.target))


class RemovalTest(TemplatesTestBase):
    def test_removed_template_deleted_if_unmodified(self):
        self.deploy(self.tpl({"a": 1}))
        self.assertEqual(self.diff({}), [f"  - remove {self.target}"])
        self.deploy({})
        self.assertFalse(os.path.exists(self.target))
        self.assertEqual(self.state["templates"]["installed"], {})

    def test_modified_template_kept(self):
        self.deploy(self.tpl({"a": 1}))
        os.chmod(self.target, 0o600)
        with open(self.target, "w") as f:
            f.write("hand edited, different length\n")
        _, out = self.deploy({})
        self.assertTrue(os.path.exists(self.target))
        self.assertIn("modified since tools wrote it", out)

    def test_removal_held_on_parse_error(self):
        self.deploy(self.tpl({"a": 1}))
        self.deploy({"bad": {"path": "relative", "content": ""}})
        self.assertTrue(os.path.exists(self.target))


class FixesTest(TemplatesTestBase):
    def test_unchanged_template_turning_secret_records_it(self):
        self.deploy(self.tpl({"a": "plain-old-value"}))
        self.assertFalse(self.state["templates"]["installed"][self.target]["secret"])
        self.deploy(self.tpl({"a": "plain-old-value"}, secrets=True))
        self.assertTrue(self.state["templates"]["installed"][self.target]["secret"])
        rows = self.diff(self.tpl({"a": "plain-new-value"}))
        self.assertEqual(rows, [f"  ~ update {self.target} (secret, diff suppressed)"])
        ok, out = self.deploy(self.tpl({"a": "plain-new-value"}))
        self.assertTrue(ok)
        self.assertNotIn("plain-old-value", out)

    def test_repointed_parent_symlink_is_untracked(self):
        a = os.path.join(self.tmp, "a")
        b = os.path.join(self.tmp, "b")
        os.makedirs(a)
        os.makedirs(b)
        link = os.path.join(self.tmp, "link")
        os.symlink(a, link)
        self.target = os.path.join(link, "app.json")
        self.deploy(self.tpl({"a": 1}))
        os.remove(link)
        os.symlink(b, link)
        with open(os.path.join(b, "app.json"), "w") as f:
            f.write("handwritten-content-here\n")
        rows = self.diff(self.tpl({"a": 2}))
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("exists and is not tracked by tools; set force: true", rows[0])
        self.assertNotIn("handwritten-content-here", "\n".join(rows))
        ok, out = self.deploy(self.tpl({"a": 2}))
        self.assertFalse(ok)
        self.assertNotIn("handwritten-content-here", out)
        with open(os.path.join(b, "app.json")) as f:
            self.assertEqual(f.read(), "handwritten-content-here\n")
        rows = self.diff(self.tpl({"a": 2}, force=True))
        self.assertEqual(
            rows, [f"  ~ replace {self.target} (not tracked by tools; content not shown)"]
        )

    def test_generated_secret_behind_repointed_symlink_not_rendered(self):
        a = os.path.join(self.tmp, "a")
        b = os.path.join(self.tmp, "b")
        os.makedirs(a)
        os.makedirs(b)
        link = os.path.join(self.tmp, "link")
        os.symlink(a, link)
        self.sops_raw["secrets"]["jwt"] = {"generate": {}, "path": os.path.join(link, "jwt")}
        with contextlib.redirect_stdout(io.StringIO()):
            secrets_flow.deploy_sops(self.sops_raw, self.state, self.report, self.decryptor())
        raw = self.tpl({"jwt": "${sops:jwt}"})
        ok, _ = self.deploy(raw)
        self.assertTrue(ok)
        os.remove(link)
        os.symlink(b, link)
        with open(os.path.join(b, "jwt"), "w") as f:
            f.write("hand-written-value")
        self.assertIn("pending", self.diff(self.tpl({"jwt": "${sops:jwt}", "x": 1}))[0])
        ok, _ = self.deploy(self.tpl({"jwt": "${sops:jwt}", "x": 1}))
        self.assertFalse(ok)
        self.assertNotIn("hand-written-value", self.read())

    def test_split_plan_holds_removals_for_invalid_unselected_entry(self):
        self.deploy(self.tpl({"a": 1}))
        stale = self.target
        good = os.path.join(self.tmp, "repo", "good")
        raw = {
            "good": {"path": good, "content": "x"},
            "bad": {"path": os.path.join(self.tmp, "bad"), "content": "${sops:x}"},
        }
        tmpls, errors = templates.parse_templates(raw)
        installed = self.state["templates"]["installed"]
        early = templates.plan(
            tmpls, errors, installed, None, select=lambda p: p != good, tail=False
        )
        self.assertTrue(early.errors)
        self.assertEqual((early.remove, early.forget), ([], []))
        late = templates.plan(tmpls, errors, installed, None, select=lambda p: p == good)
        self.assertEqual(late.errors, [])
        self.assertTrue(late.held)
        self.assertIn(
            f"  ~ hold {stale} (would remove; held until config errors are fixed)",
            templates.describe(late),
        )
        with contextlib.redirect_stdout(io.StringIO()):
            templates.apply(late, dict(installed))
        self.assertTrue(os.path.exists(stale))

    def test_changed_device_number_keeps_tracking(self):
        self.deploy(self.tpl({"a": 1}))
        record = self.state["templates"]["installed"][self.target]
        dev = record["dev"]
        record["dev"] = dev + 1
        self.assertEqual(self.diff(self.tpl({"a": 1})), [])
        self.deploy(self.tpl({"a": 1}))
        self.assertEqual(self.state["templates"]["installed"][self.target]["dev"], dev)
        self.state["templates"]["installed"][self.target]["dev"] = dev + 1
        rows = self.diff(self.tpl({"a": 2}))
        self.assertTrue(rows[0].startswith(f"  ~ update {self.target}"), rows)
        self.assertNotIn("not tracked", rows[0])

    def test_held_removal_is_shown_in_diff(self):
        self.deploy(self.tpl({"a": 1}))
        rows = self.diff({"bad": {"path": "rel", "content": ""}})
        self.assertIn(
            f"  ~ hold {self.target} (would remove; held until config errors are fixed)", rows
        )


if __name__ == "__main__":
    unittest.main()
