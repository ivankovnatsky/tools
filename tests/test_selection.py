import copy
import os
import shutil
import tempfile
import unittest
from unittest import mock

from click.testing import CliRunner
from fake_defaults import FakeDefaults
from test_launchd import LaunchdTestBase
from test_system_files import SystemFilesBase, _quiet, _write

from tools import cli, selection
from tools import diff as diff_mod
from tools.state import load_json, save_json
from tools.user import brew as brew_mod
from tools.user import defaults
from tools.user import files as files_mod
from tools.user.defaults import parse_defaults


def ok(*_a, **_k):
    return 0, "", ""


class ParseTest(unittest.TestCase):
    def test_parse_and_combine(self):
        only = selection.parse(["brew:jq", "brew:homebrew/cask/x", "files:~/a:b"])
        self.assertEqual(only, {"brew": {"jq", "homebrew/cask/x"}, "files": {"~/a:b"}})
        self.assertEqual(selection.combine((), only), ("brew", "files"))
        self.assertEqual(selection.combine(("uv",), only), ("uv", "brew", "files"))
        self.assertEqual(selection.combine((), {}), ())

    def test_trust_only_tap(self):
        config = {"brew": {"trust": {"taps": ["u/t"]}}}
        self.assertEqual(selection.unmatched({"brew": frozenset({"u/t"})}, config, "/tmp"), [])

    def test_errors(self):
        for value, message in (
            ("brew", "expected SECTION:NAME"),
            ("brew:", "expected SECTION:NAME"),
            ("nope:x", "unknown section"),
            ("mcp:x", "cannot be narrowed"),
            ("gitRepos:~/r", "cannot be narrowed"),
        ):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(selection.SelectionError, message),
            ):
                selection.parse([value])
        with self.assertRaisesRegex(selection.SelectionError, "both"):
            selection.combine(("brew",), {"brew": frozenset({"jq"})})

    def test_unmatched_names(self):
        config = {
            "brew": {
                "brews": ["jq"],
                "casks": ["c"],
                "taps": ["u/homebrew-t"],
                "masApps": {"A": 1},
            },
            "uv": {"packages": {"ruff": {}}},
            "npm": {"packages": {"pnpm": {}}},
            "templates": {"t": {"path": "/tmp/selection-t", "content": ""}},
            "launchd": {
                "labelPrefix": "org.x",
                "agents": {"web": {"command": "w"}},
                "daemons": {"db": {"command": "d"}},
            },
            "defaults": {"user": {"-g": {"k": 1}}, "system": {"/Library/Preferences/p": {}}},
            "files": [{"target": "/tmp/selection-dir", "directory": True}],
        }
        found = {
            "brew": {"jq", "c", "u/t", "u/homebrew-t", "A"},
            "uv": {"ruff"},
            "npm": {"pnpm"},
            "templates": {"t"},
            "launchd": {"web", "org.x.db"},
            "defaults": {"NSGlobalDomain", "-g", "/Library/Preferences/p"},
            "files": {"/tmp/selection-dir", "/tmp/selection-dir/"},
        }
        only = {s: frozenset(names | {"missing"}) for s, names in found.items()}
        errors = selection.unmatched(only, config, "/tmp")
        self.assertEqual(
            sorted(errors),
            sorted(f"--only {s}:missing: no such item in the {s} config" for s in found),
        )


class BrewMappingEntriesTest(unittest.TestCase):
    CONFIG = {
        "brew": {
            "brews": ["jq", {"name": "forgejo", "restartUnits": ["launchd:org.x.forgejo"]}],
            "casks": [{"name": "app"}],
        }
    }

    def test_mapping_entries_are_matched_by_name(self):
        only = {"brew": frozenset({"forgejo", "app", "missing"})}
        self.assertEqual(
            selection.unmatched(only, self.CONFIG, "/tmp"),
            ["--only brew:missing: no such item in the brew config"],
        )

    def test_narrowed_config_keeps_the_mapping(self):
        view = selection.View({"brew": frozenset({"forgejo"})}, self.CONFIG, "/tmp")
        self.assertEqual(view.config["brew"]["brews"], [self.CONFIG["brew"]["brews"][1]])
        self.assertEqual(view.config["brew"]["casks"], [])


class FullSectionErrorsTest(unittest.TestCase):
    """A narrowed section is refused while the whole section has config errors."""

    def assert_refused(self, section, raw, name, message):
        errors = selection.unmatched({section: frozenset({name})}, {section: raw}, "/tmp")
        self.assertTrue(errors)
        self.assertTrue(all(e.startswith(f"--only {section}: fix") for e in errors), errors)
        self.assertIn(message, " ".join(errors))

    def test_templates_sharing_a_path(self):
        t = {"path": "/tmp/selection-shared", "content": ""}
        self.assert_refused("templates", {"a": t, "b": t}, "b", "already used by a")

    def test_launchd_sharing_a_label(self):
        raw = {
            "agents": {
                "a": {"command": "x", "label": "org.x.same"},
                "b": {"command": "y", "label": "org.x.same"},
            }
        }
        self.assert_refused("launchd", raw, "b", "already used by")

    def test_launchd_sharing_a_log_path(self):
        raw = {
            "labelPrefix": "org.x",
            "agents": {
                "a": {"command": "x", "stdoutPath": "/tmp/selection-shared.log"},
                "b": {"command": "y", "stdoutPath": "/tmp/selection-shared.log"},
            },
        }
        self.assert_refused("launchd", raw, "a", "is shared")

    def test_files_dir_that_does_not_resolve(self):
        self.assert_refused("files", [{"dir": "missing"}], "missing", "not found: missing")


class DeployTest(unittest.TestCase):
    """Real reconcilers through cli._deploy: unselected records are never removed or changed."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.state_file = os.path.join(self.tmp, "state.json")
        for patcher in (
            mock.patch.object(cli, "migrate_state_file"),
            # Keep the host's Homebrew prefix (and any nix-homebrew marker) out.
            mock.patch.object(brew_mod, "_brew_prefixes", return_value=[self.tmp]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def deploy(self, config, only=None, update=False, scope=None):
        config = {"stateFile": self.state_file, **config}
        only = only or {}
        scope = selection.combine((), only) if scope is None else scope
        return cli._deploy(config, self.tmp, scope, update, only=only)

    def diff(self, config, only):
        config = {"stateFile": self.state_file, **config}
        return diff_mod.show_diff(config, self.tmp, selection.combine((), only), only=only)

    def state(self):
        return load_json(self.state_file)

    def test_uv_never_removes_unselected_and_updates_only_selected(self):
        paths = {"uv": "/uv", "uvBin": "/b", "uvToolDir": "/t"}
        save_json(
            self.state_file,
            {
                "version": 2,
                "uv": {
                    "context": "/b|/t",
                    "packages": {
                        "kept": {"installed": True, "version": "latest", "source": "", "x": 1},
                        "gone": {"installed": True, "version": "latest", "source": ""},
                    },
                },
            },
        )
        before = self.state()["uv"]["packages"]
        config = {"paths": paths, "uv": {"packages": {"kept": {}, "new": {}}}}
        only = {"uv": frozenset({"new"})}
        with mock.patch("tools.diff.log") as dlog:
            self.assertFalse(self.diff(config, only))
        rows = " ".join(str(c.args[0]) for c in dlog.call_args_list)
        self.assertIn("+ install new", rows)
        self.assertNotIn("gone", rows)
        with mock.patch("tools.user.uv.run_command", side_effect=ok) as run:
            self.assertTrue(self.deploy(config, only, update=True))
        cmds = [c.args[0][1:] for c in run.call_args_list]
        self.assertEqual(cmds, [["tool", "install", "new"]])
        packages = self.state()["uv"]["packages"]
        self.assertEqual(packages["kept"], before["kept"])
        self.assertEqual(packages["gone"], before["gone"])
        self.assertIn("new", packages)

        # --update upgrades the selected package only.
        with mock.patch("tools.user.uv.run_command", side_effect=ok) as run:
            self.assertTrue(self.deploy(config, only, update=True))
        self.assertEqual([c.args[0][1:] for c in run.call_args_list], [["tool", "upgrade", "new"]])

    def test_uv_relocated_tool_dir_refuses(self):
        save_json(
            self.state_file,
            {"version": 2, "uv": {"context": "/old|/t", "packages": {"gone": {}}}},
        )
        config = {
            "paths": {"uv": "/uv", "uvBin": "/b", "uvToolDir": "/t"},
            "uv": {"packages": {"new": {}}},
        }
        only = {"uv": frozenset({"new"})}
        with mock.patch("tools.user.uv.run_command", side_effect=ok) as run:
            self.assertFalse(self.deploy(config, only))
        run.assert_not_called()
        self.assertEqual(self.state()["uv"]["packages"], {"gone": {}})

    def test_npm_never_removes_unselected(self):
        save_json(
            self.state_file,
            {"version": 2, "npm": {"prefix": "/n/bin", "packages": {"gone": {"v": 1}}}},
        )
        config = {
            "paths": {"nodejs": "/node", "npmBin": "/n/bin"},
            "npm": {"configFile": "x=1", "packages": {"new": {}}},
        }
        with (
            mock.patch.dict(os.environ, {"HOME": self.tmp}),
            mock.patch("tools.user.npm.run_command", side_effect=ok) as run,
        ):
            self.assertTrue(self.deploy(config, {"npm": frozenset({"new"})}))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, ".npmrc")))
        self.assertEqual([c.args[0][1:] for c in run.call_args_list], [["install", "-g", "new"]])
        self.assertEqual(self.state()["npm"]["packages"]["gone"], {"v": 1})

    def test_brew_never_removes_or_cleans_up_unselected(self):
        prev = {
            "brews": ["gone", "kept"],
            "casks": ["oldcask"],
            "taps": ["u/t"],
            "masApps": {"Old": 1},
            "trustedTaps": ["u/t"],
        }
        save_json(self.state_file, {"version": 2, "brew": copy.deepcopy(prev)})
        config = {
            "brew": {
                "brews": ["kept", "new"],
                "onActivation": {"cleanup": "zap"},
            }
        }
        only = {"brew": frozenset({"new"})}
        with (
            mock.patch.object(brew_mod.sys, "platform", "darwin"),
            mock.patch.object(brew_mod, "_brew_bin", return_value="/brew"),
            mock.patch.object(brew_mod, "run_command", side_effect=ok) as run,
            mock.patch("tools.diff.log") as dlog,
        ):
            self.assertFalse(self.diff(config, only))
            self.assertTrue(self.deploy(config, only, update=True))
        rows = " ".join(str(c.args[0]) for c in dlog.call_args_list)
        self.assertIn("+ install new", rows)
        self.assertNotIn("remove", rows)
        self.assertEqual([c.args[0][1:] for c in run.call_args_list], [["install", "new"]])
        state = self.state()["brew"]
        for key, value in prev.items():
            if key != "brews":
                self.assertEqual(state[key], value)
        self.assertEqual(state["brews"], ["gone", "kept", "new"])

        # A narrowed upgrade leaves installed dependents (unselected) alone.
        with (
            mock.patch.object(brew_mod.sys, "platform", "darwin"),
            mock.patch.object(brew_mod, "_brew_bin", return_value="/brew"),
            mock.patch.object(brew_mod, "run_command", side_effect=ok) as run,
        ):
            self.assertTrue(self.deploy(config, only, update=True))
        upgrade = [c for c in run.call_args_list if c.args[0][1] == "upgrade"]
        self.assertEqual([c.args[0][1:] for c in upgrade], [["upgrade", "--formula", "new"]])
        self.assertEqual(upgrade[0].args[1]["HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK"], "1")

    def test_brew_app_store_id_change_refuses_without_uninstall(self):
        save_json(self.state_file, {"version": 2, "brew": {"masApps": {"App": 1}}})
        config = {"brew": {"masApps": {"App": 2}}}
        only = {"brew": frozenset({"App"})}
        self.assertTrue(
            selection.state_errors(only, config, self.tmp, self.state())[0].startswith("brew: App")
        )
        with (
            mock.patch.object(brew_mod.sys, "platform", "darwin"),
            mock.patch.object(brew_mod, "_brew_bin", return_value="/brew"),
            mock.patch.object(brew_mod, "run_command", side_effect=ok) as run,
        ):
            self.assertFalse(self.deploy(config, only))
            with self.assertRaises(SystemExit):
                self.diff(config, only)
        run.assert_not_called()
        self.assertEqual(self.state()["brew"], {"masApps": {"App": 1}})

    def test_files_never_removes_unselected(self):
        src = os.path.join(self.tmp, "src")
        os.makedirs(os.path.join(src, "tree"))
        for name in ("a", "gone", "b", "tree/x", "tree/y"):
            with open(os.path.join(src, name), "w") as f:
                f.write(name)
        out = os.path.join(self.tmp, "out")

        def entry(name):
            return {"source": f"src/{name}", "target": f"{out}/{name}"}

        self.assertTrue(self.deploy({"files": [entry("a"), entry("gone")]}, scope=("files",)))
        before = self.state()["files"]
        with open(f"{out}/a", "w") as f:
            f.write("changed by hand")
        config = {"files": [entry("a"), entry("b")]}
        only = {"files": frozenset({f"{out}/b"})}
        with mock.patch("tools.diff.log") as dlog:
            self.assertFalse(self.diff(config, only))
        rows = " ".join(str(c.args[0]) for c in dlog.call_args_list)
        self.assertIn(f"+ create {out}/b", rows)
        self.assertNotIn("gone", rows)
        self.assertNotIn(f"{out}/a", rows)

        self.assertTrue(self.deploy(config, only))
        self.assertTrue(os.path.exists(f"{out}/gone"))
        with open(f"{out}/a") as f:
            self.assertEqual(f.read(), "changed by hand")
        files = self.state()["files"]
        self.assertEqual(files[f"{out}/gone"], before[f"{out}/gone"])
        self.assertEqual(files[f"{out}/a"], before[f"{out}/a"])
        self.assertIn(f"{out}/b", files)

    def test_files_one_file_from_a_dir_entry(self):
        src = os.path.join(self.tmp, "home")
        os.makedirs(src)
        for name in ("x", "y"):
            with open(os.path.join(src, name), "w") as f:
                f.write(name)
        home = os.path.join(self.tmp, "fakehome")
        os.makedirs(home)
        with mock.patch.dict(os.environ, {"HOME": home}):
            config = {"files": [{"dir": "home", "mode": "0600"}]}
            self.assertTrue(self.deploy(config, {"files": frozenset({"~/x"})}))
            self.assertTrue(os.path.exists(os.path.join(home, "x")))
            self.assertFalse(os.path.exists(os.path.join(home, "y")))
            self.assertEqual(os.stat(os.path.join(home, "x")).st_mode & 0o777, 0o600)

    def test_files_keep_full_run_precedence(self):
        os.makedirs(os.path.join(self.tmp, "home"))
        for name, text in (("home/x", "tree"), ("override", "explicit")):
            with open(os.path.join(self.tmp, name), "w") as f:
                f.write(text)
        home = os.path.join(self.tmp, "fakehome")
        os.makedirs(home)
        config = {
            "files": [
                {"source": "override", "target": "~/x", "secrets": True},
                {"dir": "home"},
            ]
        }
        with mock.patch.dict(os.environ, {"HOME": home}):
            for name in ("~/x", "home"):
                with self.subTest(name=name):
                    view = selection.View({"files": frozenset({name})}, config, self.tmp)
                    self.assertEqual(
                        view.config["files"],
                        [
                            {
                                "target": os.path.join(home, "x"),
                                "source": os.path.join(self.tmp, "override"),
                                "secrets": True,
                            }
                        ],
                    )

    def test_files_keep_restart_units(self):
        with open(os.path.join(self.tmp, "s"), "w") as f:
            f.write("s")
        target = os.path.join(self.tmp, "o")
        config = {"files": [{"source": "s", "target": target, "restartUnits": ["launchd:a.b"]}]}
        view = selection.View({"files": frozenset({target})}, config, self.tmp)
        self.assertEqual(view.config["files"][0]["restartUnits"], ["launchd:a.b"])

    def test_shared_hook_never_runs_for_unselected_paths(self):
        src = os.path.join(self.tmp, "s")
        with open(src, "w") as f:
            f.write("s")
        marker = os.path.join(self.tmp, "ran")
        hook = f"touch {marker}"
        o1, o2 = f"{self.tmp}/o1", f"{self.tmp}/o2"
        config = {
            "files": [
                {"source": "s", "target": o1, "onChange": hook},
                {"source": "s", "target": o2, "onChange": hook},
            ]
        }
        only = {"files": frozenset({o1})}
        self.assertTrue(self.deploy(config, only))
        os.remove(marker)
        state = self.state()
        state["onChange"] = {"files": {hook: [o2]}}
        save_json(self.state_file, state)
        self.assertTrue(self.deploy(config, only))
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(self.state()["onChange"], {"files": {hook: [o2]}})

    def test_templates_never_removes_unselected(self):
        out = os.path.join(self.tmp, "t")
        os.makedirs(out)

        def tmpl(name, content="v"):
            return {"path": f"{out}/{name}", "content": content}

        full = {"templates": {"a": tmpl("a"), "gone": tmpl("gone")}}
        self.assertTrue(self.deploy(full, scope=("templates",)))
        before = self.state()["templates"]["installed"]
        config = {"templates": {"a": tmpl("a", "edited"), "new": tmpl("new")}}
        self.assertTrue(self.deploy(config, {"templates": frozenset({"new"})}))
        self.assertTrue(os.path.exists(f"{out}/gone"))
        with open(f"{out}/a") as f:
            self.assertEqual(f.read(), "v")
        installed = self.state()["templates"]["installed"]
        self.assertEqual(installed[f"{out}/gone"], before[f"{out}/gone"])
        self.assertEqual(installed[f"{out}/a"], before[f"{out}/a"])
        self.assertIn(f"{out}/new", installed)

    def test_pending_hooks_of_unselected_entries_survive(self):
        src = os.path.join(self.tmp, "s")
        with open(src, "w") as f:
            f.write("s")
        marker = os.path.join(self.tmp, "ran")
        hook = f"touch {marker}"
        save_json(
            self.state_file,
            {"version": 2, "onChange": {"files": {hook: ["/elsewhere"]}}},
        )
        config = {
            "files": [
                {"source": "s", "target": f"{self.tmp}/o1", "onChange": "true"},
                {"source": "s", "target": f"{self.tmp}/o2", "onChange": hook},
            ]
        }
        self.assertTrue(self.deploy(config, {"files": frozenset({f"{self.tmp}/o1"})}))
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(self.state()["onChange"], {"files": {hook: ["/elsewhere"]}})

    def test_persist_saves_set_aside_records(self):
        state = {"uv": {"packages": {"a": {}, "b": {}}}}
        view = selection.View({"uv": frozenset({"a"})}, {"uv": {"packages": {"a": {}}}}, "/")
        view.hide(state)
        self.assertEqual(state["uv"]["packages"], {"a": {}})
        self.assertEqual(view.merged(state)["uv"]["packages"], {"a": {}, "b": {}})
        view.restore(state)
        self.assertEqual(state["uv"]["packages"], {"a": {}, "b": {}})

    def test_malformed_state_refuses_without_changes(self):
        save_json(self.state_file, {"version": 2, "files": ["not", "a", "mapping"]})
        with mock.patch.object(cli, "install_files") as inst:
            self.assertFalse(self.deploy({"files": []}, {"files": frozenset({"/x"})}))
        inst.assert_not_called()
        self.assertEqual(self.state()["files"], ["not", "a", "mapping"])


class SystemFilesTest(SystemFilesBase):
    """`--only files:` names root-owned files too: root deploys them, the user run leaves
    them alone, as a full run does."""

    def narrowed(self, names, entries, state, run):
        config = {"files": entries}
        self.assertEqual(
            selection.unmatched({"files": frozenset(names)}, config, self.config_dir), []
        )
        view = selection.View({"files": frozenset(names)}, config, self.config_dir)
        view.hide(state)
        try:
            return _quiet(run, view.config)[0]
        finally:
            view.restore(state)

    def test_root_file_in_both_phases(self):
        gone = os.path.join(self.etc, "gone")
        _write(gone, "old\n")
        root_state = {"files": {gone: files_mod.record_for(gone, "", False)}}
        before = copy.deepcopy(root_state["files"][gone])
        user_target = os.path.join(self.home, "u")
        entries = self.entries + [{"source": "etc/shells", "target": user_target}]
        with self.as_root():
            ok = self.narrowed(
                {self.target},
                entries,
                root_state,
                lambda c: cli._run_root_sections(c, self.config_dir, root_state, {"files"}),
            )
        self.assertTrue(ok)
        self.assertEqual(_read_file(self.target), "/bin/sh\n")
        self.assertTrue(os.path.exists(gone))
        self.assertEqual(root_state["files"][gone], before)

        os.remove(self.target)
        user_state = {}
        ok = self.narrowed(
            {self.target},
            entries,
            user_state,
            lambda c: files_mod.install_files(c["files"], self.config_dir, user_state),
        )
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(self.target))
        self.assertFalse(os.path.exists(user_target))
        self.assertEqual(user_state.get("files"), {})

    def test_root_entry_errors_refuse(self):
        entries = [{"source": "etc/missing", "target": os.path.join(self.etc, "m")}]
        errors = selection.unmatched(
            {"files": frozenset({os.path.join(self.etc, "m")})}, {"files": entries}, self.config_dir
        )
        self.assertTrue(errors)
        self.assertIn("source not found: etc/missing", " ".join(errors))

    def test_skipped_entry_stays_skipped(self):
        target = os.path.join(self.home, "s")
        _write(target, "hand\n")
        entries = [{"source": "etc/shells", "target": target, "skip": True}]
        state = {"files": {target: {"hash": "kept"}}}
        ok = self.narrowed(
            {target},
            entries,
            state,
            lambda c: files_mod.install_files(c["files"], self.config_dir, state),
        )
        self.assertTrue(ok)
        self.assertEqual(_read_file(target), "hand\n")
        self.assertEqual(state["files"], {target: {"hash": "kept"}})

    def test_backup_false_survives_narrowing(self):
        target = os.path.join(self.home, "b")
        _write(target, "hand\n")
        _write(target + ".tools", "earlier\n")
        entries = [{"source": "etc/shells", "target": target, "backup": False}]
        state = {}
        ok = self.narrowed(
            {target},
            entries,
            state,
            lambda c: files_mod.install_files(c["files"], self.config_dir, state),
        )
        self.assertTrue(ok)
        self.assertNotEqual(_read_file(target), "hand\n")
        self.assertEqual(_read_file(target + ".tools"), "earlier\n")


def _read_file(path):
    with open(path) as f:
        return f.read()


class LaunchdTest(LaunchdTestBase):
    def narrowed(self, raw, names, run):
        view = selection.View({"launchd": frozenset(names)}, {"launchd": raw}, "/")
        view.hide(self.state)
        try:
            return run(view.config["launchd"])
        finally:
            view.restore(self.state)

    def test_never_boots_out_or_forgets_unselected(self):
        self.assertTrue(self.deploy(self.cfg({"web": {"command": "a"}, "old": {"command": "b"}})))
        before = copy.deepcopy(self.state["launchd"]["services"])
        raw = self.cfg({"web": {"command": "changed"}, "new": {"command": "c"}})
        self.assertEqual(
            self.narrowed(raw, {"new"}, self.diff),
            [f"  + create user/org.example.new -> {self.agent_path('new')}"],
        )
        self.fake.calls.clear()
        self.assertTrue(self.narrowed(raw, {"org.example.new"}, self.deploy))
        touched = " ".join(" ".join(c) for c in self.fake.calls)
        self.assertNotIn("org.example.old", touched)
        self.assertNotIn("org.example.web", touched)
        self.assertTrue(os.path.exists(self.agent_path("old")))
        with open(self.agent_path("web"), "rb") as f:
            self.assertNotIn(b"changed", f.read())
        services = self.state["launchd"]["services"]
        self.assertEqual(services["org.example.old"], before["org.example.old"])
        self.assertEqual(services["org.example.web"], before["org.example.web"])
        self.assertIn("org.example.new", services)


class DefaultsTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDefaults()
        self.state = {}
        home = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home)
        os.makedirs(os.path.join(home, "Library", "Preferences", "ByHost"))
        patcher = mock.patch.dict(os.environ, {"HOME": home})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_defaults(self, raw, names=None, preview=False):
        view = None
        if names:
            view = selection.View({"defaults": frozenset(names)}, {"defaults": raw}, "/")
            view.hide(self.state)
            raw = view.config["defaults"]
        try:
            spec, errors = parse_defaults(raw)
            if preview:
                return defaults.preview_defaults(spec, errors, self.state, self.fake.cli())
            restarts = []
            ok = defaults.apply_defaults(spec, errors, self.state, self.fake.cli(), restarts)
            return ok, restarts
        finally:
            if view:
                view.restore(self.state)

    def test_never_restores_or_restarts_unselected(self):
        full = {
            "user": {"kept": {"k": 1}, "gone": {"k": 1}},
            "restart": {"kept": "Dock", "gone": "Finder", "new": "SystemUIServer"},
        }
        self.assertTrue(self.run_defaults(full)[0])
        before = copy.deepcopy(self.state["defaults"])
        raw = {
            "user": {"kept": {"k": 2}, "new": {"k": 1}},
            "restart": {"kept": "Dock", "new": "SystemUIServer"},
        }
        self.assertEqual(
            self.run_defaults(raw, {"new"}, preview=True),
            ["  + write new k = 1", "  ~ restart SystemUIServer"],
        )
        self.fake.calls.clear()
        ok, restarts = self.run_defaults(raw, {"new"})
        self.assertTrue(ok)
        self.assertEqual(restarts, ["SystemUIServer"])
        self.assertEqual(
            [v for v in self.fake.verbs() if v[0] in ("write", "delete")], [("write", "new", "k")]
        )
        managed = self.state["defaults"]["managed"]["user"]
        self.assertEqual(managed["kept"], before["managed"]["user"]["kept"])
        self.assertEqual(managed["gone"], before["managed"]["user"]["gone"])
        self.assertIn("new", managed)
        restart = self.state["defaults"]["restart"]["user"]
        self.assertEqual(restart["gone"], before["restart"]["user"]["gone"])
        self.assertEqual(restart["kept"], before["restart"]["user"]["kept"])

    def test_restart_dropped_from_a_selected_domain_stays_dropped(self):
        self.assertTrue(self.run_defaults({"user": {"d": {"k": 1}}, "restart": {"d": "Dock"}})[0])
        self.assertTrue(self.run_defaults({"user": {"d": {"k": 2}}}, {"d"})[0])
        self.assertNotIn("d", (self.state["defaults"]["restart"].get("user") or {}))


class CliTest(unittest.TestCase):
    def invoke(self, args, config):
        with (
            mock.patch.object(cli, "_load_merged_config", return_value=config),
            mock.patch.object(cli, "_read_state", return_value={}),
            mock.patch.object(cli, "_deploy", return_value=True) as dep,
            mock.patch.object(cli, "show_diff", return_value=True) as diff,
        ):
            result = CliRunner().invoke(cli.main, args)
        return result, dep, diff

    def test_only_narrows_scope_and_reaches_deploy_and_diff(self):
        config = {"uv": {"packages": {"a": {}}}}
        result, dep, diff = self.invoke(["deploy", "--only", "uv:a"], config)
        self.assertEqual(result.exit_code, 0, result.output)
        only = {"uv": frozenset({"a"})}
        self.assertEqual(diff.call_args.args[2], ("uv",))
        self.assertEqual(diff.call_args.kwargs["only"], only)
        self.assertEqual(dep.call_args.args[2], ("uv",))
        self.assertEqual(dep.call_args.kwargs["only"], only)

        result, _dep, diff = self.invoke(["diff", "--scope", "brew", "--only", "uv:a"], config)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(diff.call_args.args[2], ("brew", "uv"))

    def test_bad_only_is_an_error_and_runs_nothing(self):
        config = {"uv": {"packages": {"a": {}}}}
        for args, message in (
            (["deploy", "--approve", "--only", "uv:b"], "uv:b: no such item"),
            (["diff", "--only", "mcp:x"], "cannot be narrowed"),
            (["diff", "--only", "zz:x"], "unknown section"),
            (["deploy", "--scope", "uv", "--only", "uv:a"], "pick one"),
            (["watch", "--config", "/", "--only", "uv"], "expected SECTION:NAME"),
        ):
            with self.subTest(args=args):
                result, dep, diff = self.invoke(args, config)
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIn(message, result.output)
                dep.assert_not_called()
                diff.assert_not_called()


if __name__ == "__main__":
    unittest.main()
