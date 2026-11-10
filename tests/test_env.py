import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tmp_home import setUpModule, tearDownModule  # noqa: F401

from tools import cli
from tools.config import ConfigError, resolve_config
from tools.user import env, secrets_flow


class FakeLaunchctl:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        verb, name = args[0], args[1]
        if verb == "getenv":
            return 0, self.values.get(name, "")
        if verb == "setenv":
            self.values[name] = args[2]
        elif verb == "unsetenv":
            self.values.pop(name, None)
        return 0, ""

    def writes(self):
        return [c for c in self.calls if c[0] != "getenv"]


class EnvTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.dir = os.path.join(self.tmp, "state", "env")
        self.state = {}

    def tearDown(self):
        self._tmp.cleanup()

    def raw(self, **kw):
        return {"dir": self.dir, **kw}

    def path(self, shell):
        return os.path.join(self.dir, f"env.{shell}")

    def read(self, shell):
        with open(self.path(shell)) as f:
            return f.read()

    def deploy(self, raw, **kw):
        return env.install_env(raw, self.state, **kw)

    def diff(self, raw, **kw):
        return env.diff_env(raw, self.state, **kw)


class ParseTest(unittest.TestCase):
    def errors(self, raw):
        return env.parse(raw)[1]

    def test_defaults(self):
        spec, errors = env.parse({"vars": {"EDITOR": "nvim", "N": 3}})
        self.assertEqual(errors, [])
        self.assertEqual(spec.vars, {"EDITOR": "nvim", "N": "3"})
        self.assertEqual(spec.shells, ["sh", "fish", "nu"])
        self.assertEqual(spec.dir, os.path.expanduser(env.DEFAULT_DIR))
        self.assertFalse(spec.launchd)

    def test_expands_leading_tilde_only(self):
        with mock.patch.dict(os.environ, {"HOME": "/h"}):
            spec, _ = env.parse(
                {"vars": {"A": "~/x", "B": "a~/x", "C": "$HOME/x"}, "path": ["~/bin", "~/bin/"]}
            )
        self.assertEqual(spec.vars, {"A": "/h/x", "B": "a~/x", "C": "$HOME/x"})
        self.assertEqual(spec.path, ["/h/bin"])

    def test_rejections(self):
        cases = {
            "bad name": {"vars": {"1X": "a"}},
            "reserved": {"vars": {"PATH": "/bin"}},
            "bool": {"vars": {"X": True}},
            "list": {"vars": {"X": ["a"]}},
            "control": {"vars": {"X": "a\nb"}},
            "secret": {"vars": {"X": "@TOKEN@"}},
            "pem": {"vars": {"X": "-----BEGIN PRIVATE KEY-----"}},
            "relative path": {"path": ["bin"]},
            "colon path": {"path": ["/a:b"]},
            "unknown key": {"variables": {}},
            "unknown shell": {"shells": ["csh"]},
            "launchd type": {"launchd": "yes"},
            "not a mapping": ["x"],
        }
        for label, raw in cases.items():
            with self.subTest(label):
                self.assertTrue(self.errors(raw))

    def test_sops_placeholders_are_rejected_by_the_loader(self):
        config = {"sops": {"secrets": {}}, "env": {"vars": {"TOKEN": "${sops:token}"}}}
        with self.assertRaises(ConfigError):
            resolve_config(config, env={})

    def test_vars_and_env_placeholders_resolve(self):
        config = {
            "vars": {"externalStoragePath": "/Volumes/X"},
            "env": {
                "vars": {"HF_HOME": "${externalStoragePath}/.cache/huggingface", "U": "${env:U}"}
            },
        }
        resolved = resolve_config(config, env={"U": "me"})["env"]["vars"]
        self.assertEqual(resolved, {"HF_HOME": "/Volumes/X/.cache/huggingface", "U": "me"})


class RenderTest(unittest.TestCase):
    def spec(self, **kw):
        base = {"vars": {"A": 'it\'s "q" $HOME \\ `x`'}, "path": ["/p/one", "/p/two"]}
        return env.parse({**base, **kw})[0]

    def test_sh_quotes_literally(self):
        text = env.render(self.spec(), "sh").decode()
        self.assertIn("export A='it'\\''s \"q\" $HOME \\ `x`'\n", text)
        self.assertLess(text.index("prepend '/p/two'"), text.index("prepend '/p/one'"))

    def test_fish_quotes_literally(self):
        text = env.render(self.spec(), "fish").decode()
        self.assertIn("set -gx A 'it\\'s \"q\" $HOME \\\\ `x`'\n", text)

    def test_nu_quotes_literally(self):
        text = env.render(self.spec(), "nu").decode()
        self.assertIn('"A": "it\'s \\"q\\" $HOME \\\\ `x`"', text)

    def test_empty_spec_renders_header_only(self):
        spec = env.parse({})[0]
        for shell in env.SHELLS:
            self.assertEqual(env.render(spec, shell).decode().count("\n"), 1)


VALUE = 'it\'s "q" $HOME \\ `x` ~ *'


class ShellExecutionTest(unittest.TestCase):
    """Source each snippet twice in the real shell; skipped where it is missing."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.spec = env.parse({"vars": {"A": VALUE}, "path": ["/p/one", "/p/two b"]})[0]
        self.start = "/usr/bin:/p/two b:/bin"
        self.want = "/p/one:/p/two b:/usr/bin:/bin"

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, shell):
        path = os.path.join(self.tmp, f"env.{shell}")
        with open(path, "wb") as f:
            f.write(env.render(self.spec, shell))
        return path

    def run_shell(self, argv, script):
        exe = shutil.which(argv[0])
        if not exe:
            self.skipTest(f"{argv[0]} not installed")
        out = subprocess.run(
            [exe, *argv[1:], script],
            capture_output=True,
            text=True,
            env={"PATH": self.start, "HOME": self.tmp},
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.split("\n")

    def check_posix(self, shell, *flags):
        f = self.write("sh")
        lines = self.run_shell(
            [shell, *flags, "-c"], f'. "{f}"; . "{f}"; printf "%s\\n%s\\n" "$A" "$PATH"'
        )
        self.assertEqual(lines[:2], [VALUE, self.want])

    def test_sh(self):
        self.check_posix("sh")

    def test_bash(self):
        self.check_posix("bash")

    def test_zsh(self):
        self.check_posix("zsh", "-f")

    def test_dash(self):
        self.check_posix("dash")

    def test_fish(self):
        f = self.write("fish")
        lines = self.run_shell(
            ["fish", "--no-config", "-c"],
            f"source '{f}'; source '{f}'; printf '%s\\n' $A (string join : $PATH)",
        )
        self.assertEqual(lines[:2], [VALUE, self.want])

    def test_nu(self):
        f = self.write("nu")
        script = f'source "{f}"\nsource "{f}"\nprint $env.A\nprint ($env.PATH | str join ":")'
        lines = self.run_shell(["nu", "--no-config-file", "-c"], script)
        self.assertEqual(lines[:2], [VALUE, self.want])


class DeployTest(EnvTestBase):
    def test_writes_snippets_tracks_state_and_is_idempotent(self):
        raw = self.raw(vars={"EDITOR": "nvim"}, path=["/opt/bin"])
        self.assertEqual(
            self.diff(raw),
            [
                "  + EDITOR=nvim",
                "  ~ path: /opt/bin",
                f"  + write {self.path('sh')}",
                f"  + write {self.path('fish')}",
                f"  + write {self.path('nu')}",
            ],
        )
        self.assertTrue(self.deploy(raw))

        self.assertIn("export EDITOR='nvim'", self.read("sh"))
        self.assertEqual(oct(os.stat(self.path("sh")).st_mode & 0o777), "0o644")
        section = self.state["env"]
        self.assertEqual(sorted(section["files"]), sorted(self.path(s) for s in env.SHELLS))
        self.assertEqual(section["vars"], {"EDITOR": "nvim"})
        self.assertEqual(section["path"], ["/opt/bin"])
        self.assertEqual(self.diff(raw), [])

    def test_changed_and_removed_vars(self):
        self.deploy(self.raw(vars={"EDITOR": "vi", "OLD": "x"}))
        raw = self.raw(vars={"EDITOR": "nvim"})
        rows = self.diff(raw)
        self.assertEqual(rows[:2], ["  ~ EDITOR=nvim (was vi)", "  - OLD"])
        self.assertIn(f"  ~ update {self.path('sh')}", rows)

        self.assertTrue(self.deploy(raw))
        self.assertNotIn("OLD", self.read("sh"))
        self.assertNotIn("OLD", self.read("fish"))
        self.assertNotIn("OLD", self.read("nu"))
        self.assertEqual(self.state["env"]["vars"], {"EDITOR": "nvim"})

    def test_dropped_shell_removes_its_file(self):
        self.deploy(self.raw(vars={"A": "1"}))
        raw = self.raw(vars={"A": "1"}, shells=["sh"])
        self.assertEqual(
            self.diff(raw), [f"  - remove {self.path('fish')}", f"  - remove {self.path('nu')}"]
        )
        self.assertTrue(self.deploy(raw))
        self.assertFalse(os.path.exists(self.path("fish")))
        self.assertEqual(list(self.state["env"]["files"]), [self.path("sh")])

    def test_dropped_section_removes_everything(self):
        self.deploy(self.raw(vars={"A": "1"}))
        self.assertTrue(env.has_work(None, self.state))
        rows = self.diff(None)
        self.assertIn("  - A", rows)
        self.assertTrue(self.deploy(None))
        for shell in env.SHELLS:
            self.assertFalse(os.path.exists(self.path(shell)))
        self.assertEqual(self.state["env"]["files"], {})
        self.assertNotIn("vars", self.state["env"])
        self.assertFalse(env.has_work(None, self.state))

    def test_hand_edited_file_is_released_not_removed(self):
        self.deploy(self.raw(vars={"A": "1"}, shells=["sh"]))
        with open(self.path("sh"), "a") as f:
            f.write("# mine\n")
        rows = self.diff(None)
        self.assertIn(
            f"  ~ forget {self.path('sh')} (changed since tools wrote it; left in place)", rows
        )
        self.assertTrue(self.deploy(None))
        self.assertTrue(os.path.exists(self.path("sh")))
        self.assertEqual(self.state["env"]["files"], {})

    def backups(self, shell):
        d, base = os.path.split(self.path(shell))
        return sorted(os.path.join(d, n) for n in os.listdir(d) if n == base + ".tools")

    def test_hand_edited_tracked_file_is_moved_aside_and_rewritten(self):
        raw = self.raw(vars={"A": "1"}, shells=["sh"])
        self.deploy(raw)
        with open(self.path("sh"), "w") as f:
            f.write("export A=2\n")
        target = self.path("sh")
        self.assertEqual(
            self.diff(raw),
            [
                f"  ~ replace {target} (changed since tools wrote it; old file moved aside "
                f"to {target}.tools)"
            ],
        )
        self.assertTrue(self.deploy(raw))
        self.assertIn("export A='1'", self.read("sh"))
        [backup] = self.backups("sh")
        with open(backup) as f:
            self.assertEqual(f.read(), "export A=2\n")

    def test_second_drift_refused_until_backup_removed(self):
        raw = self.raw(vars={"A": "1"}, shells=["sh", "fish"])
        self.deploy(raw)
        for _ in range(2):
            with open(self.path("sh"), "w") as f:
                f.write("export A=2\n")
            self.deploy(raw)
        [backup] = self.backups("sh")
        self.assertIn("export A=2", self.read("sh"))
        self.assertIn(f"an earlier backup {backup} exists", "\n".join(self.diff(raw)))
        self.assertFalse(self.deploy(raw))
        os.remove(backup)
        self.assertTrue(self.deploy(raw))
        self.assertIn("export A='1'", self.read("sh"))
        self.assertEqual(len(self.backups("sh")), 1)

    def test_target_appearing_after_plan_is_moved_aside(self):
        from tools import fsutil

        real = fsutil.atomic_write

        def write(path, *args, **kwargs):
            if kwargs.get("exclusive") and not os.path.lexists(path):
                with open(path, "w") as f:
                    f.write("raced\n")
            return real(path, *args, **kwargs)

        with mock.patch.object(fsutil, "atomic_write", side_effect=write):
            self.assertTrue(self.deploy(self.raw(vars={"A": "1"}, shells=["sh"])))
        self.assertIn("export A='1'", self.read("sh"))
        [backup] = self.backups("sh")
        with open(backup) as f:
            self.assertEqual(f.read(), "raced\n")

    def test_tracked_file_updated_without_backup(self):
        self.deploy(self.raw(vars={"A": "1"}, shells=["sh"]))
        self.assertTrue(self.deploy(self.raw(vars={"A": "2"}, shells=["sh"])))
        self.assertIn("export A='2'", self.read("sh"))
        self.assertEqual(self.backups("sh"), [])

    def test_untracked_file_is_replaced_and_identical_one_adopted(self):
        raw = self.raw(vars={"A": "1"}, shells=["sh", "fish"])
        os.makedirs(self.dir)
        with open(self.path("sh"), "w") as f:
            f.write("foreign\n")
        spec = env.parse(raw)[0]
        with open(self.path("fish"), "wb") as f:
            f.write(env.render(spec, "fish"))
        rows = self.diff(raw)
        target = self.path("sh")
        self.assertIn(
            f"  ~ replace {target} (not tracked by tools; old file moved aside to {target}.tools)",
            rows,
        )
        self.assertNotIn(self.path("fish"), "".join(rows))
        self.assertTrue(self.deploy(raw))
        self.assertEqual(sorted(self.state["env"]["files"]), [self.path("fish"), self.path("sh")])
        [backup] = self.backups("sh")
        with open(backup) as f:
            self.assertEqual(f.read(), "foreign\n")
        self.assertEqual(self.backups("fish"), [])

    def test_config_error_changes_nothing(self):
        self.deploy(self.raw(vars={"A": "1"}))
        raw = self.raw(vars={"PATH": "/x"})
        self.assertTrue(self.diff(raw)[0].startswith("  ! env.vars.PATH"))
        self.assertFalse(self.deploy(raw))
        self.assertTrue(os.path.exists(self.path("sh")))
        self.assertEqual(self.state["env"]["vars"], {"A": "1"})

    def test_files_section_claiming_a_snippet_blocks_both(self):
        source = os.path.join(self.tmp, "src")
        with open(source, "w") as f:
            f.write("x\n")
        raw = self.raw(vars={"A": "1"}, shells=["sh"])
        conflicts = secrets_flow.ownership_conflicts(
            files_entries=[{"source": source, "target": self.path("sh")}],
            env_raw=raw,
            config_dir=self.tmp,
        )
        self.assertIn("claimed by env and files", conflicts.get(self.path("sh")))
        self.assertEqual(
            self.diff(raw, conflicts=conflicts),
            [f"  ! skip {self.path('sh')}: claimed by env and files"],
        )
        self.assertFalse(self.deploy(raw, conflicts=conflicts))
        self.assertFalse(os.path.exists(self.path("sh")))


class LaunchdTest(EnvTestBase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(env, "launchd_available", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_setenv_then_unsetenv_on_removal(self):
        fake = FakeLaunchctl()
        raw = self.raw(vars={"A": "1", "B": "2"}, shells=[], launchd=True)
        self.assertEqual(
            self.diff(raw, run=fake),
            ["  + A=1", "  + B=2", "  + launchctl setenv A=1", "  + launchctl setenv B=2"],
        )
        self.assertEqual(fake.writes(), [])
        self.assertTrue(self.deploy(raw, run=fake))
        self.assertEqual(fake.values, {"A": "1", "B": "2"})
        self.assertEqual(self.state["env"]["launchd"], {"A": "1", "B": "2"})
        self.assertEqual(self.diff(raw, run=fake), [])

        raw = self.raw(vars={"A": "1"}, shells=[], launchd=True)
        self.assertEqual(self.diff(raw, run=fake), ["  - B", "  - launchctl unsetenv B"])
        self.assertTrue(self.deploy(raw, run=fake))
        self.assertEqual(fake.values, {"A": "1"})
        self.assertEqual(self.state["env"]["launchd"], {"A": "1"})

    def test_lost_after_reboot_is_set_again(self):
        fake = FakeLaunchctl()
        raw = self.raw(vars={"A": "1"}, shells=[], launchd=True)
        self.deploy(raw, run=fake)
        fake.values.clear()
        self.assertEqual(self.diff(raw, run=fake), ["  + launchctl setenv A=1"])

    def test_turning_launchd_off_unsets_only_untouched_values(self):
        fake = FakeLaunchctl()
        self.deploy(self.raw(vars={"A": "1", "B": "2"}, shells=[], launchd=True), run=fake)
        fake.values["B"] = "someone else"
        raw = self.raw(vars={"A": "1", "B": "2"}, shells=[])
        self.assertEqual(
            self.diff(raw, run=fake),
            [
                "  - launchctl unsetenv A",
                "  ~ forget launchd B (changed since tools set it; left in place)",
            ],
        )
        self.assertTrue(self.deploy(raw, run=fake))
        self.assertEqual(fake.values, {"B": "someone else"})
        self.assertEqual(self.state["env"]["launchd"], {})

    def test_off_by_default(self):
        fake = FakeLaunchctl()
        self.deploy(self.raw(vars={"A": "1"}, shells=[]), run=fake)
        self.assertEqual(fake.calls, [])

    def test_unavailable_platform_warns(self):
        fake = FakeLaunchctl()
        with mock.patch.object(env, "launchd_available", return_value=False):
            raw = self.raw(vars={"A": "1"}, shells=[], launchd=True)
            self.assertIn("  ! launchd: launchctl is not available here; skipped", self.diff(raw))
            self.assertTrue(self.deploy(raw, run=fake))
        self.assertEqual(fake.calls, [])


class ExportProcessTest(unittest.TestCase):
    def test_exports_vars_and_moves_path_entries_to_front(self):
        environ = {"PATH": "/usr/bin:/opt/b:/bin"}
        exported = {}
        raw = {"vars": {"UV_PYTHON_INSTALL_DIR": "/x/py"}, "path": ["/opt/a", "/opt/b"]}
        self.assertTrue(env.export_process(raw, environ, exported))
        self.assertEqual(environ["UV_PYTHON_INSTALL_DIR"], "/x/py")
        self.assertEqual(environ["PATH"], "/opt/a:/opt/b:/usr/bin:/bin")
        env.export_process(raw, environ, exported)
        self.assertEqual(environ["PATH"], "/opt/a:/opt/b:/usr/bin:/bin")

    def test_drops_vars_it_exported_earlier(self):
        environ, exported = {}, {}
        env.export_process({"vars": {"A": "1", "B": "2"}}, environ, exported)
        environ["B"] = "changed"
        env.export_process({"vars": {}}, environ, exported)
        self.assertEqual(environ, {"B": "changed"})
        self.assertEqual(exported, {})

    def test_invalid_config_exports_nothing(self):
        environ = {}
        self.assertFalse(env.export_process({"vars": {"PATH": "/x"}}, environ, {}))
        self.assertEqual(environ, {})


class CliTest(unittest.TestCase):
    def _deploy(self, config, state, scope=()):
        with (
            mock.patch.object(cli, "load_json", return_value=state),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(cli, "save_json"),
            mock.patch.object(cli, "state_lock"),
            mock.patch.object(cli, "install_env", return_value=True) as install,
            mock.patch.object(cli, "env_export_process") as export,
        ):
            cli._deploy(config, "/tmp", scope)
        return install, export

    def test_skipped_when_nothing_configured_or_tracked(self):
        install, _ = self._deploy({}, {}, ("env",))
        install.assert_not_called()

    def test_runs_when_only_state_remains(self):
        install, _ = self._deploy({}, {"env": {"files": {"/x/env.sh": {"hash": "h"}}}}, ("env",))
        install.assert_called_once()

    def test_process_export_ignores_scope(self):
        config = {"env": {"vars": {"A": "1"}}}
        install, export = self._deploy(config, {}, ("uv",))
        install.assert_not_called()
        export.assert_called_once_with(config["env"])


if __name__ == "__main__":
    unittest.main()
