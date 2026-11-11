import contextlib
import errno
import io
import json
import os
import stat
import tempfile
import unittest
from unittest import mock

import ssh_keys

from tools import fsutil
from tools.placeholders import PlaceholderError
from tools.user import secrets_flow, sops

SECRET_VALUE = "hunter2-very-secret"


def backups(path):
    d, base = os.path.split(path)
    return sorted(os.path.join(d, n) for n in os.listdir(d) if n == base + ".tools")


def appearing(content):
    """Patch fsutil.atomic_write so the target appears just before an exclusive create."""
    real = fsutil.atomic_write

    def write(path, *args, **kwargs):
        if kwargs.get("exclusive") and not os.path.lexists(path):
            with open(path, "w") as f:
                f.write(content)
        return real(path, *args, **kwargs)

    return mock.patch.object(fsutil, "atomic_write", side_effect=write)


def aside(path, reason):
    return f"{reason}; old file moved aside to {path}.tools"


class FakeSops:
    """Stands in for the sops binary: {file: decrypted document}."""

    def __init__(self, docs, fail=()):
        self.docs = docs
        self.fail = set(fail)
        self.calls = []

    def __call__(self, cmd, env):
        self.calls.append((list(cmd), dict(env)))
        path = cmd[-1]
        if path in self.fail:
            return 128, b"", "Failed to get the data key required to decrypt the SOPS file.\n"
        doc = self.docs[path]
        return 0, doc if isinstance(doc, bytes) else json.dumps(doc).encode(), ""


class SopsTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.secrets_dir = os.path.join(self.tmp, "state", "secrets")
        self.main = os.path.join(self.tmp, "cfg", "secrets.yaml")
        self.other = os.path.join(self.tmp, "cfg", "other.yaml")
        self.whole = os.path.join(self.tmp, "cfg", "ssh.bin")
        for p in (self.main, self.other, self.whole):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write("ENC[...]")
        self.fake = FakeSops(
            {
                self.main: {
                    "externalDomain": "example.test",
                    "nextDNS": {"IPs": ["192.0.2.1", "192.0.2.2"]},
                    "token": SECRET_VALUE,
                    "port": 5432,
                    "flag": True,
                },
                self.other: {"cloudflare": {"apiToken": "cf-" + SECRET_VALUE}},
                self.whole: b"-----BEGIN KEY-----\nabc\n",
            }
        )
        self.state = {}
        self.report = secrets_flow.RunReport()

    def tearDown(self):
        self._tmp.cleanup()

    def config(self, secrets, **extra):
        return {
            "secretsDir": self.secrets_dir,
            "defaultSopsFile": self.main,
            "age": {"keyFile": os.path.join(self.tmp, "age.txt")},
            "binary": "/fake/sops",
            "secrets": secrets,
            **extra,
        }

    def decryptor(self, raw):
        return sops.Decryptor(
            sops.parse_sops(raw).settings,
            runner=self.fake,
            which=lambda n: f"/fake/bin/{n}",
            env={},
        )

    def deploy(self, raw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = secrets_flow.deploy_sops(raw, self.state, self.report, self.decryptor(raw))
        self.assertNotIn(SECRET_VALUE, out.getvalue())
        return ok, out.getvalue()

    def diff(self, raw):
        rows = secrets_flow.diff_sops(raw, self.state, self.decryptor(raw))
        self.assertNotIn(SECRET_VALUE, "\n".join(rows))
        return rows

    def read(self, path):
        with open(path) as f:
            return f.read()

    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)


class ParseTest(unittest.TestCase):
    def test_defaults_and_errors(self):
        p = sops.parse_sops(
            {
                "secretsDir": "/s",
                "defaultSopsFile": "/f.yaml",
                "secrets": {
                    "a": {"key": "x/y"},
                    "b": {"key": "x", "mode": 400},
                    "c": {"generate": {"length": 4}},
                    "d": {"key": "x", "path": "/s/a"},
                    "e/../f": {},
                    "g": {"key": "x", "bogus": 1},
                },
            }
        )
        self.assertEqual([s.name for s in p.secrets], ["a"])
        self.assertEqual(p.secrets[0].path, "/s/a")
        self.assertEqual(p.secrets[0].mode, 0o400)
        self.assertEqual(p.secrets[0].key, ("x", "y"))
        self.assertEqual(len(p.errors), 5)

    def test_path_fields_type_checked(self):
        base = {"secretsDir": "/s", "defaultSopsFile": "/f"}
        for spec in ({"path": 0}, {"path": ""}, {"sopsFile": 0}, {"sopsFile": ["x"]}):
            p = sops.parse_sops({**base, "secrets": {"a": {"key": "k", **spec}}})
            self.assertEqual(p.secrets, [], spec)
            self.assertIn("must be a non-empty path string", p.errors[0])

    def test_invalid_settings_drop_every_secret(self):
        for extra in (
            {"secretsDir": 0},
            {"defaultSopsFile": 1},
            {"binary": ""},
            {"age": {"keyFile": 5}},
        ):
            raw = {"secretsDir": "/s", "defaultSopsFile": "/f", "secrets": {"a": {"key": "k"}}}
            p = sops.parse_sops({**raw, **extra})
            self.assertEqual(p.secrets, [], extra)
            self.assertIn("non-empty path string", p.errors[-1])

    def test_relative_secrets_dir_is_a_settings_error(self):
        raw = {"secretsDir": "rel/secrets", "defaultSopsFile": "/f", "secrets": {"a": {"key": "k"}}}
        p = sops.parse_sops(raw)
        self.assertEqual(p.secrets, [])
        self.assertEqual(p.secrets_dir()[0], None)
        self.assertIn("sops.secretsDir must be an absolute path", p.secrets_dir()[1])

    def test_secrets_dir_kwargs_need_declared_secrets(self):
        with_secret = {"secretsDir": "/s", "defaultSopsFile": "/f", "secrets": {"a": {"key": "k"}}}
        self.assertEqual(
            sops.secrets_dir_kwargs(with_secret), {"secrets_dir": "/s", "secrets_dir_error": None}
        )
        for raw in (None, {}, {"secretsDir": "/s"}):
            kw = sops.secrets_dir_kwargs(raw)
            self.assertEqual(kw["secrets_dir"], None, raw)
            self.assertEqual(kw["secrets_dir_error"], "no sops secrets are configured", raw)
        outside = {**with_secret, "secrets": {"a": {"key": "k", "path": "/x/custom/a"}}}
        self.assertEqual(
            sops.secrets_dir_kwargs(outside),
            {"secrets_dir": None, "secrets_dir_error": "no sops secret has its path inside /s"},
        )
        inside = {**with_secret, "secrets": {"a": {"key": "k", "path": "/s/sub/a"}}}
        self.assertEqual(sops.secrets_dir_kwargs(inside)["secrets_dir"], "/s")
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real")
            os.makedirs(real)
            alias = os.path.join(tmp, "alias")
            os.symlink(real, alias)
            secret = {"a": {"key": "k", "path": f"{real}/a"}}
            aliased = {**with_secret, "secretsDir": alias, "secrets": secret}
            self.assertEqual(sops.secrets_dir_kwargs(aliased)["secrets_dir"], alias)
        kw = sops.secrets_dir_kwargs({**with_secret, "secretsDir": "rel"})
        self.assertEqual(kw["secrets_dir"], None)
        self.assertIn("absolute path", kw["secrets_dir_error"])

    def test_missing_sops_file_is_error(self):
        p = sops.parse_sops({"secrets": {"a": {"key": "k"}}})
        self.assertIn("no sopsFile", p.errors[0])

    def test_only_missing_or_null_is_absent(self):
        base = {"secretsDir": "/s", "defaultSopsFile": "/f"}
        p = sops.parse_sops({**base, "age": None, "secrets": None})
        self.assertEqual((p.secrets, p.errors), ([], []))
        p = sops.parse_sops({**base, "secrets": {"a": {"key": "k", "restartUnits": None}}})
        self.assertEqual((p.secrets[0].notify, p.errors), ((), []))
        for age in ([], "", 0, False):
            p = sops.parse_sops({**base, "age": age, "secrets": {"a": {"key": "k"}}})
            self.assertEqual(p.secrets, [], age)
            self.assertIn("age must be a mapping", p.errors[-1])
        for secrets in ([], "", 0, False):
            p = sops.parse_sops({**base, "secrets": secrets})
            self.assertIn("secrets must be a mapping", p.errors[-1], secrets)
        for notify in ("", 0, False, {}):
            p = sops.parse_sops({**base, "secrets": {"a": {"key": "k", "restartUnits": notify}}})
            self.assertEqual(p.secrets, [], notify)
            self.assertIn("restartUnits must be a list of strings", p.errors[-1])

    def test_any_present_section_has_work(self):
        for raw in ({"secrets": []}, {"age": "bad"}, {}, {"secrets": {}}):
            self.assertTrue(sops.has_work(raw, {}), raw)
        self.assertFalse(sops.has_work(None, {}))
        rows = secrets_flow.diff_sops({"secrets": []}, {})
        self.assertIn("secrets must be a mapping", rows[0])
        rows = secrets_flow.diff_sops({"age": "bad"}, {})
        self.assertIn("age must be a mapping", rows[0])

    def test_owner_and_group_type_checked(self):
        base = {"secretsDir": "/s", "defaultSopsFile": "/f"}
        for key, bad in (("owner", False), ("group", True), ("owner", -1), ("owner", "")):
            p = sops.parse_sops({**base, "secrets": {"a": {"key": "k", key: bad}}})
            self.assertEqual(p.secrets, [], (key, bad))
            self.assertIn(f"{key} must be a user/group name or a non-negative id", p.errors[0])
        for bad in (False, True, -1):
            with self.assertRaises(ValueError):
                fsutil.resolve_uid(bad)
            with self.assertRaises(ValueError):
                fsutil.resolve_gid(bad)
        self.assertEqual(fsutil.resolve_uid(0), 0)
        self.assertEqual(fsutil.resolve_gid("0"), 0)

    def test_key_name_format_and_ssh_keys_follow_sops_nix(self):
        base = {"secretsDir": "/s", "defaultSopsFile": "/f"}
        p = sops.parse_sops(
            {
                **base,
                "secrets": {
                    "aws/region": {},
                    "plain": {},
                    "whole": {"key": ""},
                    "bin": {"format": "binary"},
                    "env": {"format": "dotenv", "key": "TOKEN"},
                },
            }
        )
        self.assertEqual(p.errors, [])
        by = {s.name: s for s in p.secrets}
        self.assertEqual(by["aws/region"].key, ("aws", "region"))
        self.assertEqual(by["aws/region"].path, "/s/aws/region")
        self.assertEqual(by["plain"].key, ("plain",))
        self.assertIsNone(by["whole"].key)
        self.assertIsNone(by["bin"].key)
        self.assertEqual((by["env"].format, by["plain"].format), ("dotenv", "yaml"))
        p = sops.parse_sops({**base, "defaultSopsFormat": "json", "secrets": {"a": {}}})
        self.assertEqual(p.secrets[0].format, "json")
        for bad in (
            {"secrets": {"a": {"format": "toml"}}},
            {"secrets": {"a": {"format": "binary", "key": "x"}}},
            {"secrets": {"a/../b": {}}},
            {"secrets": {"/a": {}}},
            {"defaultSopsFormat": "xml"},
        ):
            p = sops.parse_sops({**base, **bad})
            self.assertEqual(p.secrets, [], bad)
            self.assertTrue(p.errors, bad)
        p = sops.parse_sops({**base, "age": {"sshKeyPaths": ["/a", "~/b"]}})
        self.assertEqual(p.errors, [])
        self.assertEqual(p.settings.age_ssh_key_files, ("/a", os.path.expanduser("~/b")))
        for bad in ("/a", ["/a", ""], [1]):
            p = sops.parse_sops({**base, "age": {"sshKeyPaths": bad}})
            self.assertIn("sshKeyPaths", p.errors[0], bad)
        p = sops.parse_sops({**base, "age": {"sshKeyFile": "/a"}})
        self.assertIn("sshKeyPaths", p.errors[0])

    def test_nested_secret_paths_rejected(self):
        base = {"secretsDir": "/s", "defaultSopsFile": "/f"}
        p = sops.parse_sops({**base, "secrets": {"a": {}, "a/b": {}}})
        self.assertEqual([s.name for s in p.secrets], ["a"])
        self.assertIn("nests with secret a", p.errors[0])
        p = sops.parse_sops({**base, "secrets": {"x": {"path": "/s/d/f"}, "y": {"path": "/s/d"}}})
        self.assertIn("nests with secret x", p.errors[0])

    def test_lookup_reports_invalid_config_not_unknown_secret(self):
        parsed = sops.parse_sops({"age": [], "secrets": {"a": {"key": "k", "sopsFile": "/f"}}})
        lookup = sops.make_lookup(parsed)
        with self.assertRaisesRegex(PlaceholderError, "sops config is invalid: age must be"):
            lookup("a")
        with self.assertRaises(KeyError):
            sops.make_lookup(sops.parse_sops({}))("a")


class AtomicWriteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.path = os.path.join(self.tmp, "s")

    def tearDown(self):
        self._tmp.cleanup()

    def no_hardlinks(self, err=errno.EPERM):
        def fail(src, dst):
            raise OSError(err, os.strerror(err))

        return mock.patch.object(fsutil.os, "link", side_effect=fail)

    def test_exclusive_falls_back_without_hardlinks(self):
        for err in (errno.EPERM, errno.ENOTSUP, errno.EMLINK):
            with self.no_hardlinks(err):
                fsutil.atomic_write(self.path, b"v", 0o400, exclusive=True)
            with open(self.path, "rb") as f:
                self.assertEqual(f.read(), b"v")
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o400)
            self.assertEqual(os.listdir(self.tmp), ["s"])
            os.remove(self.path)

    def test_exclusive_fallback_still_refuses_existing(self):
        with open(self.path, "w") as f:
            f.write("old")
        with self.no_hardlinks(), self.assertRaises(FileExistsError):
            fsutil.atomic_write(self.path, b"new", 0o400, exclusive=True)
        with open(self.path) as f:
            self.assertEqual(f.read(), "old")
        self.assertEqual(os.listdir(self.tmp), ["s"])

    def test_other_link_errors_propagate(self):
        with self.no_hardlinks(errno.EACCES), self.assertRaises(PermissionError):
            fsutil.atomic_write(self.path, b"v", 0o400, exclusive=True)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_apply_attrs_refuses_symlink(self):
        pointee = os.path.join(self.tmp, "p")
        with open(pointee, "w") as f:
            f.write("v")
        os.chmod(pointee, 0o644)
        os.symlink(pointee, self.path)
        with self.assertRaises(OSError):
            fsutil.apply_attrs(self.path, 0o400)
        self.assertEqual(stat.S_IMODE(os.stat(pointee).st_mode), 0o644)
        fsutil.apply_attrs(pointee, 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(pointee).st_mode), 0o600)

    def test_read_current_does_not_follow_symlink(self):
        pointee = os.path.join(self.tmp, "p")
        with open(pointee, "w") as f:
            f.write("v")
        os.symlink(pointee, self.path)
        current = fsutil.read_current(self.path)
        self.assertTrue(current.exists)
        self.assertIsNone(current.content)
        self.assertIn("symlink", current.error)
        self.assertNotEqual(fsutil.fingerprint(self.path), fsutil.fingerprint(pointee))

    def test_secret_at_unmanaged_symlink_moved_aside(self):
        pointee = os.path.join(self.tmp, "p")
        with open(pointee, "w") as f:
            f.write("v")
        os.chmod(pointee, 0o644)
        os.symlink(pointee, self.path)
        raw = {
            "binary": "/fake/sops",
            "secrets": {"s": {"key": "k", "sopsFile": pointee, "path": self.path}},
        }
        parsed = sops.parse_sops(raw)
        runner = lambda cmd, env: (0, b'{"k": "v"}', "")  # noqa: E731
        p = sops.plan(parsed, {}, sops.Decryptor(parsed.settings, runner=runner, env={}))
        self.assertEqual((p.changes[0].action, p.changes[0].aside), ("replace", "a symlink"))
        installed = {}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(sops.apply(p, parsed.settings, installed).success)
        self.assertIn(self.path, installed)
        self.assertEqual(stat.S_IMODE(os.stat(pointee).st_mode), 0o644)
        with open(pointee) as f:
            self.assertEqual(f.read(), "v")
        self.assertFalse(os.path.islink(self.path))
        [backup] = backups(self.path)
        self.assertEqual(os.readlink(backup), pointee)

    def test_generate_works_without_hardlinks(self):
        raw = {"secretsDir": self.tmp, "secrets": {"jwt": {"generate": {"length": 16}}}}
        parsed = sops.parse_sops(raw)
        p = sops.plan(parsed, {}, sops.Decryptor(parsed.settings))
        out = io.StringIO()
        with self.no_hardlinks(), contextlib.redirect_stdout(out):
            self.assertTrue(sops.apply(p, parsed.settings, {}).success)
        with open(os.path.join(self.tmp, "jwt")) as f:
            self.assertEqual(len(f.read()), 16)

    def test_secret_paths_predictable(self):
        raw = {
            "secretsDir": "/s",
            "defaultSopsFile": "/f",
            "secrets": {"a": {}, "b": {"path": "/x"}},
        }
        self.assertEqual(sops.secret_paths(raw), {"a": "/s/a", "b": "/x"})


class DecryptTest(SopsTestBase):
    def test_nested_keys_list_index_whole_file_and_multiple_files(self):
        raw = self.config(
            {
                "domain": {"key": "externalDomain"},
                "ip1": {"key": "nextDNS/IPs/1"},
                "cf": {"key": "cloudflare/apiToken", "sopsFile": self.other},
                "ssh": {
                    "key": "",
                    "sopsFile": self.whole,
                    "path": os.path.join(self.tmp, "ssh", "key"),
                },
            }
        )
        self.assertEqual(len(self.diff(raw)), 4)
        self.fake.calls = []
        ok, _ = self.deploy(raw)
        self.assertTrue(ok)
        d = self.secrets_dir
        self.assertEqual(self.read(os.path.join(d, "domain")), "example.test")
        self.assertEqual(self.read(os.path.join(d, "ip1")), "192.0.2.2")
        self.assertEqual(self.read(os.path.join(d, "cf")), "cf-" + SECRET_VALUE)
        self.assertEqual(
            self.read(os.path.join(self.tmp, "ssh", "key")), "-----BEGIN KEY-----\nabc\n"
        )
        # One structured decrypt per file, plus one whole-file decrypt.
        self.assertEqual(len(self.fake.calls), 3)

    def test_non_string_values_are_errors(self):
        raw = self.config({"port": {"key": "port"}, "flag": {"key": "flag"}})
        rows = self.diff(raw)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIn("is not a string", row)
        ok, _ = self.deploy(raw)
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(os.path.join(self.secrets_dir, "port")))

    def test_age_key_and_binary_passed_to_sops(self):
        ssh_key = os.path.join(self.tmp, "id")
        with open(ssh_key, "w") as f:
            f.write("k")
        raw = self.config(
            {"t": {"key": "token"}}, age={"sshKeyPaths": [ssh_key], "keyFile": "/k/age"}
        )
        self.deploy(raw)
        cmd, env = [(c, e) for c, e in self.fake.calls if c[0] == "/fake/sops"][0]
        self.assertEqual(
            cmd[:6], ["/fake/sops", "--decrypt", "--input-type", "yaml", "--output-type", "json"]
        )
        self.assertEqual(env["SOPS_AGE_KEY_FILE"], "/k/age")
        self.assertEqual(env["SOPS_AGE_SSH_PRIVATE_KEY_FILE"], ssh_key)

    def test_missing_binary_is_clear_error(self):
        raw = self.config({"t": {"key": "token"}})
        del raw["binary"]
        d = sops.Decryptor(sops.parse_sops(raw).settings, runner=self.fake, which=lambda _: None)
        rows = secrets_flow.diff_sops(raw, self.state, d)
        self.assertIn("sops binary not found on PATH", rows[0])

    def test_modes_and_private_dirs(self):
        raw = self.config({"t": {"key": "token"}, "u": {"key": "token", "mode": "0600"}})
        self.deploy(raw)
        self.assertEqual(self.mode(os.path.join(self.secrets_dir, "t")), 0o400)
        self.assertEqual(self.mode(os.path.join(self.secrets_dir, "u")), 0o600)
        self.assertEqual(self.mode(self.secrets_dir), 0o751)
        umask = os.umask(0)
        os.umask(umask)
        self.assertEqual(self.mode(os.path.dirname(self.secrets_dir)), 0o777 & ~umask)

    def test_existing_shared_secrets_dir_left_alone_with_warning(self):
        shared = os.path.join(self.tmp, "shared")
        os.makedirs(shared)
        os.chmod(shared, 0o755)
        raw = self.config({"t": {"key": "token"}}, secretsDir=shared)
        _, out = self.deploy(raw)
        self.assertEqual(self.mode(shared), 0o755)
        self.assertIn("group-writable or world-readable", out)
        self.assertEqual(self.mode(os.path.join(shared, "t")), 0o400)

    def test_private_existing_dir_no_warning(self):
        os.makedirs(self.secrets_dir, mode=0o700)
        os.chmod(self.secrets_dir, 0o700)
        _, out = self.deploy(self.config({"t": {"key": "token"}}))
        self.assertNotIn("accessible", out)

    def test_unchanged_not_rewritten_and_diff_clean(self):
        raw = self.config({"t": {"key": "token"}})
        self.deploy(raw)
        path = os.path.join(self.secrets_dir, "t")
        before = os.stat(path)
        self.assertEqual(self.diff(raw), [])
        self.deploy(raw)
        after = os.stat(path)
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertEqual(self.report.changed["sops"], [])

    def test_value_change_rewrites_and_reports_without_value(self):
        raw = self.config({"t": {"key": "token", "restartUnits": ["launchd:org.example.app"]}})
        self.deploy(raw)
        self.report = secrets_flow.RunReport()
        self.fake.docs[self.main]["token"] = SECRET_VALUE + "-rotated"
        rows = self.diff(raw)
        path = os.path.join(self.secrets_dir, "t")
        self.assertEqual(
            rows, [f"  ~ update {path} (secret)", "    notify: launchd:org.example.app"]
        )
        ok, out = self.deploy(raw)
        self.assertIn(f"Updated: {path} (secret)", out)
        self.assertEqual(self.read(path), SECRET_VALUE + "-rotated")
        self.assertEqual(self.report.notify, [("launchd:org.example.app", "sops/t")])

    def test_mode_drift_is_chmod(self):
        raw = self.config({"t": {"key": "token"}})
        self.deploy(raw)
        path = os.path.join(self.secrets_dir, "t")
        os.chmod(path, 0o644)
        self.assertEqual(self.diff(raw), [f"  ~ chmod {path} -> 0o400"])
        self.deploy(raw)
        self.assertEqual(self.mode(path), 0o400)

    def test_one_failure_does_not_abort_others(self):
        self.fake.fail.add(self.other)
        raw = self.config(
            {
                "cf": {"key": "cloudflare/apiToken", "sopsFile": self.other},
                "missing": {"key": "nope/here"},
                "t": {"key": "token"},
            }
        )
        ok, out = self.deploy(raw)
        self.assertFalse(ok)
        self.assertIn("Failed to get the data key", out)
        self.assertIn("key nope not found", out)
        self.assertTrue(os.path.exists(os.path.join(self.secrets_dir, "t")))
        self.assertEqual(sorted(self.report.failed_secrets), ["cf", "missing"])

    def test_removed_secret_deleted_only_if_tracked(self):
        raw = self.config({"t": {"key": "token"}})
        self.deploy(raw)
        path = os.path.join(self.secrets_dir, "t")
        stray = os.path.join(self.secrets_dir, "not-ours")
        with open(stray, "w") as f:
            f.write("x")
        empty = self.config({})
        self.assertEqual(self.diff(empty), [f"  - remove {path}"])
        self.deploy(empty)
        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.exists(stray))
        self.assertEqual(self.state["sops"]["installed"], {})

    def test_modified_secret_kept_on_removal(self):
        raw = self.config({"t": {"key": "token"}})
        self.deploy(raw)
        path = os.path.join(self.secrets_dir, "t")
        os.chmod(path, 0o600)
        with open(path, "w") as f:
            f.write("hand edited, longer content")
        _, out = self.deploy(self.config({}))
        self.assertTrue(os.path.exists(path))
        self.assertIn("modified since tools wrote it", out)

    def test_removal_held_on_config_error(self):
        raw = self.config({"t": {"key": "token"}})
        self.deploy(raw)
        self.deploy(self.config({"bad": {"key": "x", "mode": 1}}))
        self.assertTrue(os.path.exists(os.path.join(self.secrets_dir, "t")))

    def test_state_has_no_value_or_hash(self):
        self.deploy(self.config({"t": {"key": "token"}}))
        record = self.state["sops"]["installed"][os.path.join(self.secrets_dir, "t")]
        self.assertEqual(set(record), {"name", "dev", "ino", "mtime_ns"})

    def test_existing_identical_file_is_adopted(self):
        os.makedirs(self.secrets_dir)
        path = os.path.join(self.secrets_dir, "t")
        with open(path, "w") as f:
            f.write(SECRET_VALUE)
        os.chmod(path, 0o400)
        raw = self.config({"t": {"key": "token"}})
        self.assertEqual(self.diff(raw), [])
        self.deploy(raw)
        self.assertIn(path, self.state["sops"]["installed"])


class NoDoubleExpansionTest(unittest.TestCase):
    def test_paths_not_env_expanded(self):
        from unittest import mock

        with mock.patch.dict(os.environ, {"X": "expanded"}):
            p = sops.parse_sops(
                {
                    "secretsDir": "/s/$X",
                    "defaultSopsFile": "/f/${X}.yaml",
                    "secrets": {"a": {"key": "k", "path": "/p/$X"}, "b": {"key": "k"}},
                }
            )
        self.assertEqual(p.errors, [])
        self.assertEqual(p.secrets[0].path, "/p/$X")
        self.assertEqual(p.secrets[0].sops_file, "/f/${X}.yaml")
        self.assertEqual(p.secrets[1].path, "/s/$X/b")


class OwnershipTest(SopsTestBase):
    def test_sops_and_files_claiming_one_path_both_refuse(self):
        from tools import diff as diff_mod
        from tools.user import files

        target = os.path.join(self.secrets_dir, "t")
        src = os.path.join(self.tmp, "cfg", "plain.txt")
        with open(src, "w") as f:
            f.write("not a secret")
        files_entries = [{"source": "plain.txt", "target": target}]
        raw = self.config({"t": {"key": "token"}})
        conflicts = secrets_flow.ownership_conflicts(
            files_entries=files_entries,
            sops_raw=raw,
            state=self.state,
            config_dir=os.path.join(self.tmp, "cfg"),
        )
        self.assertEqual(conflicts, {target: "claimed by files and sops"})

        rows = secrets_flow.diff_sops(raw, self.state, self.decryptor(raw), conflicts)
        self.assertIn("claimed by files and sops", rows[0])
        file_rows = diff_mod._diff_files(
            files_entries, os.path.join(self.tmp, "cfg"), {}, conflicts
        )
        self.assertIn("refusing", file_rows[0])

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok_sops = secrets_flow.deploy_sops(
                raw, self.state, self.report, self.decryptor(raw), conflicts
            )
            ok_files = files.install_files(
                files_entries, os.path.join(self.tmp, "cfg"), self.state, conflicts
            )
        self.assertFalse(ok_sops)
        self.assertFalse(ok_files)
        self.assertFalse(os.path.exists(target))

    def test_state_only_claim_frees_itself(self):
        from tools.user import files

        target = os.path.join(self.secrets_dir, "t")
        os.makedirs(self.secrets_dir)
        with open(target, "w") as f:
            f.write("old")
        import hashlib

        self.state["files"] = {target: {"hash": hashlib.sha256(b"old").hexdigest()}}
        raw = self.config({"t": {"key": "token"}})
        conflicts = secrets_flow.ownership_conflicts(sops_raw=raw, state=self.state)
        self.assertIn(target, conflicts)
        # files no longer declares it, so it removes its copy...
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(files.install_files([], self.tmp, self.state, conflicts))
        self.assertFalse(os.path.exists(target))
        # ...and the next run has a single owner.
        self.assertEqual(secrets_flow.ownership_conflicts(sops_raw=raw, state=self.state), {})

    def test_stale_secret_under_managed_link_not_removed(self):
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        link = os.path.join(self.tmp, "linked")
        os.symlink(repo, link)
        path = os.path.join(link, "tok")
        with open(path, "w") as f:
            f.write("v")
        self.state["sops"] = {"installed": {path: {"name": "tok", **fsutil.fingerprint(path)}}}
        self.state["links"] = {"installed": {link: {"source": repo}}}
        raw = self.config({})
        conflicts = secrets_flow.ownership_conflicts(sops_raw=raw, state=self.state)
        rows = secrets_flow.diff_sops(raw, self.state, self.decryptor(raw), conflicts)
        self.assertTrue(rows[0].startswith(f"  ~ keep {path} (is under"), rows)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = secrets_flow.deploy_sops(
                raw, self.state, self.report, self.decryptor(raw), conflicts
            )
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(os.path.join(repo, "tok")))
        self.assertIn(path, self.state["sops"]["installed"])

    def test_untracked_existing_file_moved_aside(self):
        path = os.path.join(self.tmp, "ssh", "config")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("Host handwritten\n")
        os.chmod(path, 0o600)
        raw = self.config({"tok": {"key": "token", "path": path}})
        why = aside(path, "not tracked by tools")
        self.assertEqual(self.diff(raw), [f"  ~ replace {path} (secret; {why})"])
        ok, out = self.deploy(raw)
        self.assertTrue(ok)
        [backup] = backups(path)
        self.assertIn(f"Moved aside (not tracked by tools): {path} -> {backup}", out)
        self.assertIn(f"Replaced: {path}", out)
        self.assertNotIn("handwritten", out)
        self.assertEqual(self.read(path), SECRET_VALUE)
        self.assertEqual(self.read(backup), "Host handwritten\n")
        self.assertEqual(self.mode(backup), 0o600)
        self.assertEqual(self.mode(path), 0o400)
        self.assertEqual(self.diff(raw), [])
        self.assertEqual(backups(path), [backup])

    def test_tracked_unchanged_file_updated_without_backup(self):
        path = os.path.join(self.tmp, "tok")
        raw = self.config({"tok": {"key": "token", "path": path}})
        self.assertTrue(self.deploy(raw)[0])
        self.fake.docs[self.main]["token"] = "rotated"
        self.assertEqual(self.diff(raw), [f"  ~ update {path} (secret)"])
        self.assertTrue(self.deploy(raw)[0])
        self.assertEqual(self.read(path), "rotated")
        self.assertEqual(backups(path), [])

    def test_tracked_hand_edited_file_moved_aside(self):
        path = os.path.join(self.tmp, "tok")
        raw = self.config({"tok": {"key": "token", "path": path}})
        self.assertTrue(self.deploy(raw)[0])
        os.chmod(path, 0o600)
        with open(path, "w") as f:
            f.write("hand-edited")
        os.chmod(path, 0o400)
        why = aside(path, "modified since tools wrote it")
        self.assertEqual(self.diff(raw), [f"  ~ replace {path} (secret; {why})"])
        self.assertTrue(self.deploy(raw)[0])
        self.assertEqual(self.read(path), SECRET_VALUE)
        [backup] = backups(path)
        self.assertEqual(self.read(backup), "hand-edited")
        self.assertEqual(self.mode(backup), 0o400)

    def test_second_drift_refused_until_backup_removed(self):
        path = os.path.join(self.tmp, "tok")
        other = os.path.join(self.tmp, "other")
        with open(path, "w") as f:
            f.write("one")
        raw = self.config(
            {"tok": {"key": "token", "path": path}, "dom": {"key": "externalDomain", "path": other}}
        )
        self.assertTrue(self.deploy(raw)[0])
        [backup] = backups(path)
        os.chmod(path, 0o600)
        with open(path, "w") as f:
            f.write("two")
        rows = self.diff(raw)
        self.assertIn(f"  ! tok: {path} changed again; an earlier backup {backup} exists", rows[0])
        ok, _ = self.deploy(raw)
        self.assertFalse(ok)
        self.assertEqual(self.read(path), "two")
        self.assertEqual(backups(path), [backup])
        self.assertEqual(self.read(other), "example.test")
        os.remove(backup)
        self.assertTrue(self.deploy(raw)[0])
        self.assertEqual(self.read(path), SECRET_VALUE)
        self.assertEqual(len(backups(path)), 1)

    def test_target_appearing_after_plan_is_moved_aside(self):
        path = os.path.join(self.tmp, "tok")
        raw = self.config({"tok": {"key": "token", "path": path}})
        with appearing("raced"):
            self.assertTrue(self.deploy(raw)[0])
        self.assertEqual(self.read(path), SECRET_VALUE)
        [backup] = backups(path)
        self.assertEqual(self.read(backup), "raced")

    def test_target_appearing_with_earlier_backup_is_an_error(self):
        path = os.path.join(self.tmp, "tok")
        with open(path + ".tools", "w") as f:
            f.write("old")
        raw = self.config({"tok": {"key": "token", "path": path}})
        with appearing("raced"):
            ok, out = self.deploy(raw)
        self.assertFalse(ok)
        self.assertIn("changed again", out)
        self.assertEqual(self.read(path), "raced")

    def test_generated_target_appearing_after_plan_is_adopted(self):
        path = os.path.join(self.tmp, "gen")
        raw = self.config({"tok": {"generate": {}, "path": path}})
        with appearing("raced"):
            self.assertTrue(self.deploy(raw)[0])
        self.assertEqual(self.read(path), "raced")
        self.assertEqual(self.mode(path), 0o400)
        self.assertTrue(self.state["sops"]["installed"][path]["generated"])
        self.assertEqual(backups(path), [])

    def test_force_key_is_an_error(self):
        p = sops.parse_sops({"secrets": {"t": {"key": "k", "force": True}}})
        self.assertIn("tools now always moves the old file aside", p.errors[0])

    def test_respelled_path_keeps_secret_and_single_entry(self):
        real = os.path.join(self.tmp, "Sec")
        os.makedirs(real)
        alias = os.path.join(self.tmp, "alias")
        os.symlink(real, alias)
        pairs = [(os.path.join(real, "a"), os.path.join(alias, "a"))]
        if fsutil._case_insensitive(real):
            pairs.append((os.path.join(real, "b"), os.path.join(self.tmp, "sec", "B")))
        for first, second in pairs:
            self.state = {}
            self.deploy(self.config({"t": {"key": "token", "path": first}}))
            raw = self.config({"t": {"key": "token", "path": second}})
            self.assertEqual(self.diff(raw), [], (first, second))
            ok, out = self.deploy(raw)
            self.assertTrue(ok, out)
            self.assertEqual(self.read(first), SECRET_VALUE)
            self.assertEqual(list(self.state["sops"]["installed"]), [second])


class GenerateTest(SopsTestBase):
    def test_generated_once_never_regenerated(self):
        raw = self.config({"jwt": {"generate": {"length": 48, "charset": "hex"}}})
        path = os.path.join(self.secrets_dir, "jwt")
        self.assertEqual(self.diff(raw), [f"  + generate {path} (secret)"])
        self.deploy(raw)
        value = self.read(path)
        self.assertEqual(len(value), 48)
        self.assertTrue(set(value) <= set("0123456789abcdef"))
        self.assertEqual(self.mode(path), 0o400)
        self.deploy(raw)
        self.assertEqual(self.read(path), value)
        self.assertEqual(self.diff(raw), [])
        self.assertEqual(self.fake.calls, [])

    def test_symlink_at_generated_path_moved_aside(self):
        pointee = os.path.join(self.tmp, "pointee")
        with open(pointee, "w") as f:
            f.write("theirs")
        path = os.path.join(self.tmp, "gen", "tok")
        os.makedirs(os.path.dirname(path))
        os.symlink(pointee, path)
        raw = self.config({"tok": {"generate": {}, "path": path}})
        self.assertEqual(
            self.diff(raw), [f"  + generate {path} (secret; {aside(path, 'a symlink')})"]
        )
        ok, _ = self.deploy(raw)
        self.assertTrue(ok)
        self.assertFalse(os.path.islink(path))
        self.assertEqual(len(self.read(path)), 64)
        [backup] = backups(path)
        self.assertEqual(os.readlink(backup), pointee)

    def test_existing_untracked_value_adopted(self):
        path = os.path.join(self.tmp, "forgejo", "internal_token")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("pre-existing")
        os.chmod(path, 0o644)
        ok, _ = self.deploy(self.config({"tok": {"generate": {}, "path": path}}))
        self.assertTrue(ok)
        self.assertEqual(self.read(path), "pre-existing")
        self.assertEqual(backups(path), [])
        self.assertEqual(self.mode(path), 0o400)
        self.assertTrue(self.state["sops"]["installed"][path]["generated"])

    def test_lookup_never_follows_symlink_at_generated_path(self):
        pointee = os.path.join(self.tmp, "elsewhere")
        with open(pointee, "w") as f:
            f.write("not-a-secret-target")
        os.makedirs(self.secrets_dir, exist_ok=True)
        os.symlink(pointee, os.path.join(self.secrets_dir, "jwt"))
        parsed = sops.parse_sops(self.config({"jwt": {"generate": {}}}))
        lookup = sops.make_lookup(parsed, decryptor=self.decryptor(self.config({})))
        with self.assertRaisesRegex(sops.SecretUnavailable, "symlink"):
            lookup("jwt")

    def test_dropped_generated_secret_is_kept(self):
        raw = self.config({"jwt": {"generate": {}}})
        self.deploy(raw)
        path = os.path.join(self.secrets_dir, "jwt")
        _, out = self.deploy(self.config({}))
        self.assertTrue(os.path.exists(path))
        self.assertIn("generated secret; kept on disk", out)
        self.assertEqual(self.state["sops"]["installed"], {})

    def test_generated_mode_drift_is_fixed_without_rewriting(self):
        raw = self.config({"jwt": {"generate": {}, "mode": "0400"}})
        self.deploy(raw)
        path = os.path.join(self.secrets_dir, "jwt")
        value = self.read(path)
        os.chmod(path, 0o644)
        self.assertEqual(self.diff(raw), [f"  ~ chmod {path} -> 0o400"])
        self.deploy(raw)
        self.assertEqual(self.mode(path), 0o400)
        self.assertEqual(self.read(path), value)

    def test_custom_chars(self):
        self.deploy(self.config({"p": {"generate": {"length": 16, "chars": "ab"}}}))
        self.assertTrue(set(self.read(os.path.join(self.secrets_dir, "p"))) <= {"a", "b"})


class FixesTest(SopsTestBase):
    def test_tracked_secret_switched_to_generate_records_generated(self):
        raw = self.config({"t": {"key": "token"}})
        self.deploy(raw)
        path = os.path.join(self.secrets_dir, "t")
        self.deploy(self.config({"t": {"generate": {}}}))
        self.assertTrue(self.state["sops"]["installed"][path]["generated"])
        _, out = self.deploy(self.config({}))
        self.assertTrue(os.path.exists(path))
        self.assertIn("generated secret; kept on disk", out)

    def test_whole_file_decrypt_passes_declared_output_type(self):
        for fmt in ("yaml", "json", "dotenv", "ini"):
            self.fake.calls = []
            self.fake.docs[self.whole] = b"whole-" + fmt.encode()
            raw = self.config(
                {"w": {"key": "", "format": fmt, "sopsFile": self.whole}},
            )
            self.state = {}
            ok, _ = self.deploy(raw)
            self.assertTrue(ok, fmt)
            cmd, _env = self.fake.calls[0]
            self.assertEqual(
                cmd[1:6], ["--decrypt", "--input-type", fmt, "--output-type", fmt], fmt
            )
            self.assertEqual(self.read(os.path.join(self.secrets_dir, "w")), "whole-" + fmt)
            os.remove(os.path.join(self.secrets_dir, "w"))
        self.fake.calls = []
        self.state = {}
        self.deploy(self.config({"b": {"format": "binary", "sopsFile": self.whole}}))
        cmd, _env = self.fake.calls[0]
        self.assertEqual(
            cmd[1:6], ["--decrypt", "--input-type", "binary", "--output-type", "binary"]
        )

    def write_ssh_key(self, name="id_ed25519", data=None):
        key = os.path.join(self.tmp, name)
        with open(key, "wb") as f:
            f.write(ssh_keys.openssh_key() if data is None else data)
        return key

    def test_ssh_key_converted_in_process(self):
        key = self.write_ssh_key()
        raw = self.config({"t": {"key": "token"}}, age={"sshKeyPaths": [key]})
        ok, out = self.deploy(raw)
        self.assertTrue(ok, out)
        self.assertEqual([c[0] for c, _e in self.fake.calls], ["/fake/sops"])
        sops_env = self.fake.calls[0][1]
        self.assertEqual(sops_env["SOPS_AGE_SSH_PRIVATE_KEY_FILE"], key)
        self.assertEqual(sops_env["SOPS_AGE_KEY"], ssh_keys.AGE_KEY)
        self.assertNotIn(ssh_keys.AGE_KEY, out)
        self.assertEqual(os.listdir(self.tmp).count("age.txt"), 0)

    def test_ssh_key_conversion_keeps_env_key_and_key_file(self):
        key = self.write_ssh_key()
        raw = self.config({"t": {"key": "token"}}, age={"sshKeyPaths": [key], "keyFile": "/k/age"})
        d = sops.Decryptor(
            sops.parse_sops(raw).settings,
            runner=self.fake,
            which=lambda n: f"/fake/bin/{n}",
            env={"SOPS_AGE_KEY": "AGE-SECRET-KEY-1EXISTING"},
        )
        d.value(sops.parse_sops(raw).secrets[0])
        env = [e for c, e in self.fake.calls if c[0] == "/fake/sops"][0]
        self.assertEqual(env["SOPS_AGE_KEY"], "AGE-SECRET-KEY-1EXISTING\n" + ssh_keys.AGE_KEY)
        self.assertEqual(env["SOPS_AGE_KEY_FILE"], "/k/age")

    def test_unconvertible_ssh_key_skipped_with_warning(self):
        bad = self.write_ssh_key(
            "id_locked", ssh_keys.openssh_key(cipher=b"aes256-ctr", kdf=b"bcrypt")
        )
        good = self.write_ssh_key()
        raw = self.config({"t": {"key": "token"}}, age={"sshKeyPaths": [bad, good]})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rows = secrets_flow.diff_sops(raw, self.state, self.decryptor(raw))
        self.assertEqual(rows, [f"  + decrypt {os.path.join(self.secrets_dir, 't')} (secret)"])
        self.assertEqual(out.getvalue().count(f"cannot convert ssh key {bad}"), 1)
        self.assertIn("passphrase-protected", out.getvalue())
        env = self.fake.calls[0][1]
        self.assertEqual(env["SOPS_AGE_SSH_PRIVATE_KEY_FILE"], bad)
        self.assertEqual(env["SOPS_AGE_KEY"], ssh_keys.AGE_KEY)

    def test_no_convertible_ssh_key_leaves_sops_age_key_unset(self):
        key = self.write_ssh_key(data=b"k")
        raw = self.config({"t": {"key": "token"}}, age={"sshKeyPaths": [key]})
        _ok, out = self.deploy(raw)
        self.assertIn(f"cannot convert ssh key {key}", out)
        env = self.fake.calls[0][1]
        self.assertEqual(env["SOPS_AGE_SSH_PRIVATE_KEY_FILE"], key)
        self.assertNotIn("SOPS_AGE_KEY", env)

    def test_ssh_to_age_binary_is_unknown_key(self):
        p = sops.parse_sops(self.config({}, sshToAgeBinary="/opt/bin/ssh-to-age"))
        self.assertIn("sshToAgeBinary", p.errors[0])

    def test_first_existing_ssh_key_passed_natively(self):
        key = os.path.join(self.tmp, "id_ed25519")
        with open(key, "w") as f:
            f.write("k")
        missing = os.path.join(self.tmp, "nope")
        raw = self.config({"t": {"key": "token"}}, age={"sshKeyPaths": [missing, key]})
        self.deploy(raw)
        env = [e for c, e in self.fake.calls if c[0] == "/fake/sops"][0]
        self.assertEqual(env["SOPS_AGE_SSH_PRIVATE_KEY_FILE"], key)

    @unittest.skipIf(os.geteuid() == 0, "root can read a mode-000 file")
    def test_unreadable_ssh_key_skipped_for_next_readable_one(self):
        bad = self.write_ssh_key("id_unreadable")
        good = self.write_ssh_key()
        os.chmod(bad, 0)
        raw = self.config({"t": {"key": "token"}}, age={"sshKeyPaths": [bad, good]})
        ok, out = self.deploy(raw)
        self.assertTrue(ok, out)
        self.assertIn(f"skipping ssh key {bad}", out)
        env = [e for c, e in self.fake.calls if c[0] == "/fake/sops"][0]
        self.assertEqual(env["SOPS_AGE_SSH_PRIVATE_KEY_FILE"], good)
        self.assertEqual(env["SOPS_AGE_KEY"], ssh_keys.AGE_KEY)

    def test_null_ssh_key_path_is_config_error(self):
        raw = self.config({"t": {"key": "token"}}, age={"sshKeyPaths": [None]})
        p = sops.parse_sops(raw)
        self.assertIn("sshKeyPaths", p.errors[0])
        rows = self.diff(raw)
        self.assertIn("sshKeyPaths", rows[0])
        ok, _ = self.deploy(raw)
        self.assertFalse(ok)

    def test_lookup_of_generated_secret_checks_identity(self):
        a = os.path.join(self.tmp, "a")
        b = os.path.join(self.tmp, "b")
        os.makedirs(a)
        os.makedirs(b)
        link = os.path.join(self.tmp, "link")
        os.symlink(a, link)
        path = os.path.join(link, "jwt")
        raw = self.config({"jwt": {"generate": {}, "path": path}})
        self.deploy(raw)
        parsed = sops.parse_sops(raw)
        installed = self.state["sops"]["installed"]
        lookup = sops.make_lookup(parsed, decryptor=self.decryptor(raw), installed=installed)
        self.assertEqual(len(lookup("jwt")), 64)
        os.remove(link)
        os.symlink(b, link)
        with open(os.path.join(b, "jwt"), "w") as f:
            f.write("hand-written")
        with self.assertRaisesRegex(sops.SecretUnavailable, "not the file tools"):
            lookup("jwt")

    def test_repointed_parent_symlink_is_untracked(self):
        a = os.path.join(self.tmp, "a")
        b = os.path.join(self.tmp, "b")
        os.makedirs(a)
        os.makedirs(b)
        link = os.path.join(self.tmp, "link")
        os.symlink(a, link)
        path = os.path.join(link, "tok")
        raw = self.config({"tok": {"key": "token", "path": path}})
        self.deploy(raw)
        self.assertEqual(self.read(os.path.join(a, "tok")), SECRET_VALUE)
        self.assertEqual(self.state["sops"]["installed"][path]["dev"], os.lstat(path).st_dev)
        os.remove(link)
        os.symlink(b, link)
        with open(os.path.join(b, "tok"), "w") as f:
            f.write("hand-written")
        rows = self.diff(raw)
        why = aside(path, "not tracked by tools")
        self.assertEqual(rows, [f"  ~ replace {path} (secret; {why})"])
        ok, _ = self.deploy(raw)
        self.assertTrue(ok)
        self.assertEqual(self.read(os.path.join(b, "tok")), SECRET_VALUE)
        [backup] = backups(os.path.join(b, "tok"))
        self.assertEqual(self.read(backup), "hand-written")

    def test_removed_same_inode_other_file_is_not_removed(self):
        raw = self.config({"t": {"key": "token"}})
        self.deploy(raw)
        path = os.path.join(self.secrets_dir, "t")
        record = self.state["sops"]["installed"][path]
        record["dev"] = record["dev"] + 1
        record["mtime_ns"] = record["mtime_ns"] + 1
        rows = self.diff(self.config({}))
        self.assertTrue(rows[0].startswith(f"  ~ forget {path}"), rows)
        self.deploy(self.config({}))
        self.assertTrue(os.path.exists(path))

    def test_changed_device_number_keeps_tracking(self):
        raw = self.config({"t": {"key": "token"}, "jwt": {"generate": {}}})
        self.deploy(raw)
        installed = self.state["sops"]["installed"]
        real_dev = {}
        for path, record in installed.items():
            real_dev[path] = record["dev"]
            record["dev"] = record["dev"] + 1
        self.assertEqual(self.diff(raw), [])
        ok, out = self.deploy(raw)
        self.assertTrue(ok, out)
        for path, record in self.state["sops"]["installed"].items():
            self.assertEqual(record["dev"], real_dev[path])
        self.fake.docs[self.main]["token"] = SECRET_VALUE + "-new"
        for record in self.state["sops"]["installed"].values():
            record["dev"] = record["dev"] + 1
        self.assertEqual(
            self.diff(raw), [f"  ~ update {os.path.join(self.secrets_dir, 't')} (secret)"]
        )
        self.assertEqual(
            self.diff(self.config({})),
            [f"  - remove {os.path.join(self.secrets_dir, 't')}"]
            + [
                f"  ~ forget {os.path.join(self.secrets_dir, 'jwt')} "
                "(generated secret; kept on disk, delete by hand if unwanted)"
            ],
        )

    def test_held_removal_is_shown_in_diff(self):
        raw = self.config({"t": {"key": "token"}})
        self.deploy(raw)
        path = os.path.join(self.secrets_dir, "t")
        rows = self.diff(self.config({"bad": {"key": "x", "mode": 1}}))
        self.assertIn(f"  ~ hold {path} (would remove; held until config errors are fixed)", rows)


if __name__ == "__main__":
    unittest.main()
