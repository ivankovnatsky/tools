import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from click.testing import CliRunner

from tools import cli, watch


class FakeTime:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def run_loop(scans, *, debounce=3.0, poll=1.0, run=None, initial_run=False):
    """Drive watch_loop over a scripted list of snapshots; returns run() calls."""
    calls = []
    t = FakeTime()
    it = iter(scans)
    last = {"value": None}
    exhausted = {"value": False}

    def scan():
        try:
            last["value"] = next(it)
        except StopIteration:
            exhausted["value"] = True
        return last["value"]

    def record(files):
        calls.append((t.now, files))
        if run:
            run(files)

    with redirect_stdout(io.StringIO()):
        watch.watch_loop(
            scan,
            record,
            poll=poll,
            debounce=debounce,
            clock=t.clock,
            sleep=t.sleep,
            should_stop=lambda: exhausted["value"],
            initial_run=initial_run,
        )
    return calls


class ChangedPathsTest(unittest.TestCase):
    def test_added_removed_modified(self):
        old = {"a": (1, 1), "b": (1, 1), "c": (1, 1)}
        new = {"a": (1, 1), "b": (2, 1), "d": (1, 1)}
        self.assertEqual(watch.changed_paths(old, new), ["b", "c", "d"])

    def test_identical(self):
        self.assertEqual(watch.changed_paths({"a": (1, 1)}, {"a": (1, 1)}), [])


class SnapshotTest(unittest.TestCase):
    def test_walks_dirs_skips_git_and_accepts_files(self):
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            os.makedirs(os.path.join(d, "cfg", "machines"))
            os.makedirs(os.path.join(d, "cfg", ".git"))
            for rel in ("cfg/a.yaml", "cfg/machines/host.yaml", "cfg/.git/HEAD", "extra.yaml"):
                with open(os.path.join(d, rel), "w") as f:
                    f.write("x")
            snap = watch.snapshot([os.path.join(d, "cfg"), os.path.join(d, "extra.yaml")])
            names = sorted(os.path.relpath(p, d) for p in snap)
        self.assertEqual(names, ["cfg/a.yaml", "cfg/machines/host.yaml", "extra.yaml"])

    def test_watches_dot_paths_but_skips_git_and_tools_temp_files(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "dotfiles", ".config", "app"))
            os.makedirs(os.path.join(d, ".git"))
            for rel in (
                "a.yaml",
                ".sops.yaml",
                ".DS_Store",
                "dotfiles/.config/app/rc",
                ".git/HEAD",
                ".tools-abc123",
                "dotfiles/.tools-def456",
            ):
                with open(os.path.join(d, rel), "w") as f:
                    f.write("x")
            snap = watch.snapshot([d])
            names = sorted(os.path.relpath(p, os.path.realpath(d)) for p in snap)
        self.assertEqual(names, [".DS_Store", ".sops.yaml", "a.yaml", "dotfiles/.config/app/rc"])

    def _tree(self, d, rels, ignore=None):
        for rel in rels:
            os.makedirs(os.path.join(d, os.path.dirname(rel)), exist_ok=True)
            with open(os.path.join(d, rel), "w") as f:
                f.write("x")
        if ignore is not None:
            with open(os.path.join(d, watch.IGNORE_FILE), "w") as f:
                f.write(ignore)

    def _names(self, d):
        return sorted(os.path.relpath(p, os.path.realpath(d)) for p in watch.snapshot([d]))

    def test_honours_ignore_patterns_from_file(self):
        rels = (
            "a.yaml",
            "README.md",
            "dotfiles/notes.md",
            ".direnv/env",
            "dotfiles/.direnv/env",
            "dotfiles/.config/rc",
            "dotfiles/rc.sync-conflict-20260101-abc",
            "result-bin/x",
            "dotfiles/.config/.DS_Store",
        )
        with tempfile.TemporaryDirectory() as d:
            self._tree(
                d,
                rels,
                '{"ignore_patterns": ["*.md", ".direnv/", "*.sync-conflict-*",'
                ' "result*", ".DS_Store"]}',
            )
            names = self._names(d)
        self.assertEqual(names, ["a.yaml", "dotfiles/.config/rc"])

    def test_missing_ignore_file_watches_everything_but_builtins(self):
        with tempfile.TemporaryDirectory() as d:
            self._tree(d, ("a.yaml", ".direnv/env", "__pycache__/x.pyc", ".git/HEAD"))
            names = self._names(d)
        self.assertEqual(names, [".direnv/env", "__pycache__/x.pyc", "a.yaml"])

    def test_invalid_ignore_file_warns_and_uses_none(self):
        with tempfile.TemporaryDirectory() as d:
            self._tree(d, ("a.md",), "{not json")
            out = io.StringIO()
            with redirect_stdout(out):
                names = self._names(d)
                self._names(d)
        self.assertEqual(names, ["a.md"])
        self.assertEqual(out.getvalue().count("Ignoring invalid"), 1)

    def test_ignore_file_is_reread_when_it_changes(self):
        with tempfile.TemporaryDirectory() as d:
            self._tree(d, ("a.md", "b.yaml"), '{"ignore_patterns": []}')
            self.assertIn("a.md", self._names(d))
            with open(os.path.join(d, watch.IGNORE_FILE), "w") as f:
                f.write('{"ignore_patterns": ["*.md", "x"]}')
            self.assertNotIn("a.md", self._names(d))

    def test_change_under_dot_dir_triggers_and_git_does_not(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "dotfiles", ".config"))
            os.makedirs(os.path.join(d, ".git"))
            rc = os.path.join(d, "dotfiles", ".config", "rc")
            head = os.path.join(d, ".git", "HEAD")
            for path in (rc, head):
                with open(path, "w") as f:
                    f.write("x")
            before = watch.snapshot([d])
            with open(head, "w") as f:
                f.write("yy")
            self.assertEqual(watch.changed_paths(before, watch.snapshot([d])), [])
            with open(rc, "w") as f:
                f.write("yy")
            changed = watch.changed_paths(before, watch.snapshot([d]))
        self.assertEqual(changed, [os.path.realpath(rc)])

    def test_is_excluded_matches_path_and_dot_suffixed_siblings(self):
        exclude = {"/s/state.json"}
        self.assertTrue(watch.is_excluded("/s/state.json", exclude))
        self.assertTrue(watch.is_excluded("/s/state.json.tmp", exclude))
        self.assertFalse(watch.is_excluded("/s/state.jsonx", exclude))

    def test_follows_symlinked_dirs_and_survives_loops(self):
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            os.makedirs(os.path.join(d, "real"))
            os.makedirs(os.path.join(d, "cfg"))
            with open(os.path.join(d, "real", "host.yaml"), "w") as f:
                f.write("x")
            os.symlink(os.path.join(d, "real"), os.path.join(d, "cfg", "machines"))
            os.symlink(os.path.join(d, "cfg"), os.path.join(d, "cfg", "loop"))
            snap = watch.snapshot([os.path.join(d, "cfg")])
            names = sorted(os.path.relpath(p, d) for p in snap)
        self.assertEqual(names, ["cfg/machines/host.yaml"])

    def test_repointed_symlink_with_same_size_and_mtime_is_a_change(self):
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            for name in ("one", "two"):
                with open(os.path.join(d, name), "w") as f:
                    f.write("x")
                os.utime(os.path.join(d, name), ns=(1_000_000_000, 1_000_000_000))
            os.makedirs(os.path.join(d, "cfg"))
            link = os.path.join(d, "cfg", "a.yaml")
            os.symlink(os.path.join(d, "one"), link)
            before = watch.snapshot([os.path.join(d, "cfg")])
            os.remove(link)
            os.symlink(os.path.join(d, "two"), link)
            after = watch.snapshot([os.path.join(d, "cfg")])
        self.assertEqual(watch.changed_paths(before, after), [link])

    def test_mode_change_is_a_change(self):
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            path = os.path.join(d, "run.sh")
            with open(path, "w") as f:
                f.write("x")
            os.chmod(path, 0o644)
            before = watch.snapshot([d])
            os.chmod(path, 0o755)
            os.utime(path, ns=(before[path][1], before[path][1]))
            after = watch.snapshot([d])
        self.assertEqual(watch.changed_paths(before, after), [path])

    def test_dangling_symlink_is_recorded_and_repointing_it_is_a_change(self):
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            link = os.path.join(d, "a.yaml")
            os.symlink(os.path.join(d, "missing-one"), link)
            before = watch.snapshot([d])
            os.remove(link)
            os.symlink(os.path.join(d, "missing-two"), link)
            after = watch.snapshot([d])
        self.assertIn(link, before)
        self.assertEqual(watch.changed_paths(before, after), [link])

    def test_missing_path_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            self.assertEqual(watch.snapshot([os.path.join(d, "nope")]), {})


class WatchLoopTest(unittest.TestCase):
    A = {"f": (1, 1)}
    B = {"f": (2, 1)}
    C = {"f": (3, 1)}

    def test_no_changes_never_runs(self):
        self.assertEqual(run_loop([self.A] * 10), [])

    def test_runs_once_after_debounce(self):
        calls = run_loop([self.A, self.B] + [self.B] * 6, debounce=3.0)
        self.assertEqual(calls, [(4.0, ["f"])])

    def test_burst_is_coalesced_and_extends_deadline(self):
        # Changes at t=1 and t=2: one run, 3s after the last change.
        calls = run_loop([self.A, self.B, self.C] + [self.C] * 6, debounce=3.0)
        self.assertEqual(calls, [(5.0, ["f"])])

    def test_collects_every_changed_file(self):
        scans = [{}, {"a": (1, 1)}, {"a": (1, 1), "b": (1, 1)}] + [{"a": (1, 1), "b": (1, 1)}] * 5
        calls = run_loop(scans, debounce=2.0)
        self.assertEqual([files for _, files in calls], [["a", "b"]])

    def test_change_during_run_triggers_follow_up(self):
        scans = [self.A, self.B] + [self.B] * 3 + [self.C] * 5
        calls = run_loop(scans, debounce=2.0)
        self.assertEqual(len(calls), 2)

    def test_initial_run_happens_after_baseline(self):
        # The file changes while the initial run is in progress (before the
        # first poll): the baseline predates it, so a follow-up run happens.
        scans = [self.A] + [self.B] * 5
        calls = run_loop(scans, debounce=2.0, initial_run=True)
        self.assertEqual([files for _, files in calls], [[], ["f"]])

    def test_ignored_changes_never_run(self):
        scans = [{"s": (1, 1)}] + [{"s": (2, 1)}] * 5
        calls = []
        with redirect_stdout(io.StringIO()):
            t = FakeTime()
            it = iter(scans)
            watch.watch_loop(
                lambda: next(it, scans[-1]),
                calls.append,
                poll=1.0,
                debounce=1.0,
                clock=t.clock,
                sleep=t.sleep,
                should_stop=lambda: t.now > 8,
                initial_run=False,
                ignore=lambda p: p == "s",
            )
        self.assertEqual(calls, [])

    def test_run_failure_keeps_watching(self):
        def boom(files):
            raise RuntimeError("deploy blew up")

        scans = [self.A, self.B] + [self.B] * 3 + [self.C] * 5
        calls = run_loop(scans, debounce=2.0, run=boom)
        self.assertEqual(len(calls), 2)


class WatchRunTest(unittest.TestCase):
    def _run(self, approve, *, deploy=True, diff=True, load=None):
        load = load or mock.Mock(return_value={})
        with (
            mock.patch.object(cli, "_load_merged_config", load),
            mock.patch.object(cli, "_deploy", return_value=deploy) as deploy_mock,
            mock.patch.object(cli, "show_diff", return_value=diff) as diff_mock,
            redirect_stdout(io.StringIO()) as out,
        ):
            cli._watch_run(["/cfg"], ("brew",), approve, ["/cfg/a.yaml"])
        return deploy_mock, diff_mock, out.getvalue()

    def test_approve_deploys_without_prompt(self):
        deploy_mock, diff_mock, out = self._run(True)
        deploy_mock.assert_called_once_with({}, "/", ("brew",))
        diff_mock.assert_not_called()
        self.assertIn("Deploy successful", out)

    def test_without_approve_only_diffs(self):
        deploy_mock, diff_mock, out = self._run(False, diff=False)
        deploy_mock.assert_not_called()
        diff_mock.assert_called_once()
        self.assertIn("not deploying without --approve", out)

    def test_failed_deploy_is_reported(self):
        _, _, out = self._run(True, deploy=False)
        self.assertIn("Deploy failed", out)

    def test_config_error_does_not_exit(self):
        deploy_mock, _, out = self._run(True, load=mock.Mock(side_effect=SystemExit(1)))
        deploy_mock.assert_not_called()
        self.assertIn("Config failed to load", out)


class SourcePathsTest(unittest.TestCase):
    def test_sections_sources(self):
        config = {
            "files": [
                {"dir": "dot"},
                {"source": "s/a", "target": "~/a"},
                {"target": "~/d", "directory": True},
            ],
            "sops": {"secrets": {"k": {"sopsFile": "/c/secrets/k.yaml", "key": "a", "path": "/x"}}},
            "skills": {
                "sources": {"local": {"path": "skills", "subdir": "sub"}, "r": {"url": "u"}}
            },
            "mgmt": {"nextdns": {"profiles": {"p": {"profileFile": "tpl/p.json"}}}},
        }
        paths = cli._source_paths(config, "/c")
        for expected in ("/c/dot", "/c/s/a", "/c/secrets/k.yaml", "/c/skills/sub", "/c/tpl/p.json"):
            self.assertIn(expected, paths)
        self.assertNotIn("/c/~/d", paths)

    def test_mgmt_collects_only_path_fields(self):
        config = {
            "mgmt": {
                "nextdns": {
                    "profiles": {
                        "p": {
                            "profileFile": "tpl/p.json",
                            "apiKeyFile": "/s/key",
                            "profileIdDir": "ids",
                            "vars": {"a": ".", "b": "tpl"},
                            "varsFiles": {"c": "v/c"},
                        }
                    }
                },
                "syncthing": {
                    "configDir": "st",
                    "baseUrl": "http://@h@",
                    "baseUrlFiles": {"h": "h"},
                },
                "forgejo": {"workPath": "work", "forgejoBin": "bin/forgejo"},
            }
        }
        paths = cli._source_paths(config, "/c")
        self.assertEqual(
            sorted(paths),
            ["/c/h", "/c/ids", "/c/st", "/c/tpl/p.json", "/c/v/c", "/s/key"],
        )

    def test_links_sources(self):
        config = {"links": [{"source": "/c/l/src", "target": "~/l"}, {"bad": 1}]}
        self.assertIn("/c/l/src", cli._source_paths(config, "/c"))


class IsWatchedTest(unittest.TestCase):
    def test_file_and_dir_entries(self):
        watched = {"/c/machines/h.yaml", "/c/dot"}
        self.assertTrue(watch.is_watched("/c/machines/h.yaml", watched))
        self.assertTrue(watch.is_watched("/c/dot/a/b", watched))
        self.assertFalse(watch.is_watched("/c/dotfiles/a", watched))
        self.assertFalse(watch.is_watched("/c/machines/other.yaml", watched))


class TmuxTargetTest(unittest.TestCase):
    def test_window_is_matched_exactly(self):
        self.assertEqual(watch.tmux_target("mini"), "=mini:=tools-watch")


class WatchCommandTest(unittest.TestCase):
    def test_config_is_required(self):
        result = CliRunner().invoke(cli.main, ["watch", "--approve"])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("--config", result.output)

    def test_tmux_validates_paths_first(self):
        with mock.patch.object(cli, "run_in_tmux") as tmux:
            result = CliRunner().invoke(
                cli.main, ["watch", "--config", "/nonexistent/tools-cfg", "--tmux"]
            )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("does not exist", result.output)
        tmux.assert_not_called()

    def test_missing_config_path_exits(self):
        result = CliRunner().invoke(cli.main, ["watch", "--config", "/nonexistent/tools-cfg"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("does not exist", result.output)

    def _watch(self, d, deploy=None, polls=8):
        """Run `tools watch --approve` on `d` with a fake clock for `polls` polls."""
        t = FakeTime()
        real_loop = cli.watch_loop

        def loop(scan, run, **kwargs):
            kwargs.update(clock=t.clock, sleep=t.sleep, should_stop=lambda: t.now >= polls)
            real_loop(scan, run, **kwargs)

        with (
            mock.patch.object(cli, "watch_loop", side_effect=loop),
            mock.patch.object(cli, "_deploy", side_effect=deploy or (lambda *a, **k: True)) as dep,
            mock.patch("tools.config._get_hostname", return_value="h"),
        ):
            result = CliRunner().invoke(
                cli.main, ["watch", "--config", d, "--approve", "--debounce", "1"]
            )
        return result, dep

    def _state_config(self, d):
        os.makedirs(os.path.join(d, "state"))
        state = os.path.join(d, "state", "state.json")
        os.makedirs(os.path.join(d, "machines"))
        with open(os.path.join(d, "machines", "h.yaml"), "w") as f:
            f.write(f"stateFile: {state}\n")
        return state

    def test_state_writes_under_watched_dir_do_not_retrigger(self):
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            state = self._state_config(d)

            def deploy(*_, **__):
                for path in (state, state + ".tmp", os.path.join(d, "state", "mgmt.key")):
                    with open(path, "a") as f:
                        f.write("x")
                return True

            result, dep = self._watch(d, deploy)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(dep.call_count, 1, result.output)

    def test_config_edit_during_run_still_retriggers(self):
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            state = self._state_config(d)
            edits = []

            def deploy(*_, **__):
                with open(state, "a") as f:
                    f.write("x")
                if not edits:
                    edits.append(1)
                    with open(os.path.join(d, "machines", "h.yaml"), "a") as f:
                        f.write("vars: {a: 1}\n")
                return True

            result, dep = self._watch(d, deploy)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(dep.call_count, 2, result.output)

    def test_malformed_yaml_keeps_watching(self):
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            os.makedirs(os.path.join(d, "machines"))
            with open(os.path.join(d, "machines", "h.yaml"), "w") as f:
                f.write("a: [\n")
            result, dep = self._watch(d)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIsNone(result.exception)
        self.assertIn("Config failed to load", result.output)
        dep.assert_not_called()

    def _host_config(self, d):
        """Host h with an include, a template, a `dir:` source and a file source."""
        for sub in ("machines/h", "machines/other", "dot/sub", "tpl", "src", "state"):
            os.makedirs(os.path.join(d, sub))
        files = {
            "machines/h.yaml": (
                f"include: [h/extra.yaml]\nstateFile: {d}/state/state.json\n"
                "files:\n  - dir: dot\n  - source: src/a.conf\n    target: ~/a.conf\n"
            ),
            "machines/h/extra.yaml": (
                "templates:\n  t:\n    path: /tmp/tools-watch-test-t\n    file: tpl/t.json\n"
            ),
            "machines/other.yaml": "vars: {a: 1}\n",
            "dot/sub/x": "x",
            "tpl/t.json": "{}",
            "src/a.conf": "a",
            "src/unused.conf": "u",
        }
        for rel, text in files.items():
            with open(os.path.join(d, rel), "w") as f:
                f.write(text)

    def _count_runs_after_edit(self, rel, *, create=False):
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            self._host_config(d)
            edits = []

            def deploy(*_, **__):
                if not edits:
                    edits.append(1)
                    with open(os.path.join(d, rel), "w" if create else "a") as f:
                        f.write("\n")
                return True

            result, dep = self._watch(d, deploy)
        self.assertEqual(result.exit_code, 0, result.output)
        return dep.call_count

    def test_host_file_include_and_sources_trigger(self):
        for rel, create in (
            ("machines/h.yaml", False),
            ("machines/h/extra.yaml", False),
            ("tpl/t.json", False),
            ("src/a.conf", False),
            ("dot/sub/x", False),
            ("dot/sub/new", True),
        ):
            with self.subTest(rel=rel):
                self.assertEqual(self._count_runs_after_edit(rel, create=create), 2)

    def test_other_files_do_not_trigger(self):
        for rel, create in (
            ("machines/other.yaml", False),
            ("src/unused.conf", False),
            ("new.yaml", True),
        ):
            with self.subTest(rel=rel):
                self.assertEqual(self._count_runs_after_edit(rel, create=create), 1)

    def _count_runs_after(self, setup, action):
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            self._host_config(d)
            setup(d)
            edits = []

            def deploy(*_):
                if not edits:
                    edits.append(1)
                    action(d)
                return True

            result, dep = self._watch(d, deploy)
        self.assertEqual(result.exit_code, 0, result.output)
        return dep.call_count

    @staticmethod
    def _repoint(link, target):
        os.remove(link)
        os.symlink(target, link)

    def test_repointed_symlinked_host_file_triggers(self):
        def setup(d):
            host = os.path.join(d, "machines", "h.yaml")
            os.rename(host, os.path.join(d, "machines", "h-a.yaml"))
            with open(os.path.join(d, "machines", "h-b.yaml"), "w") as f:
                f.write(f"stateFile: {d}/state/state.json\n")
            os.symlink("h-a.yaml", host)

        def action(d):
            self._repoint(os.path.join(d, "machines", "h.yaml"), "h-b.yaml")

        self.assertEqual(self._count_runs_after(setup, action), 2)

    def test_repointed_symlinked_source_triggers(self):
        def setup(d):
            with open(os.path.join(d, "machines", "h.yaml"), "a") as f:
                f.write("  - source: src/link.conf\n    target: ~/l.conf\n")
            os.symlink("a.conf", os.path.join(d, "src", "link.conf"))

        def action(d):
            self._repoint(os.path.join(d, "src", "link.conf"), "unused.conf")

        self.assertEqual(self._count_runs_after(setup, action), 2)

    def test_new_preferred_host_file_triggers(self):
        def action(d):
            with open(os.path.join(d, "machines", "h.json"), "w") as f:
                f.write(f'{{"stateFile": "{d}/state/state.json"}}')

        self.assertEqual(self._count_runs_after(lambda d: None, action), 2)

    def test_watched_set_follows_config_edits(self):
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            self._host_config(d)
            step = []

            def deploy(*_, **__):
                step.append(1)
                if len(step) == 1:
                    with open(os.path.join(d, "machines", "h.yaml"), "a") as f:
                        f.write("  - source: src/unused.conf\n    target: ~/u.conf\n")
                elif len(step) == 2:
                    with open(os.path.join(d, "src", "unused.conf"), "a") as f:
                        f.write("x")
                return True

            result, dep = self._watch(d, deploy, polls=12)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(dep.call_count, 3, result.output)

    def test_failed_load_watches_everything(self):
        watched = {"/old"}
        with (
            mock.patch.object(cli, "_load_merged_config", side_effect=SystemExit(1)),
            redirect_stdout(io.StringIO()),
        ):
            cli._watch_run(["/cfg"], (), False, [], None, watched)
        self.assertEqual(watched, set())

    def test_watch_run_refreshes_exclusions(self):
        excluded = {"/old"}
        with (
            mock.patch.object(cli, "_load_merged_config", return_value={"stateFile": "/s/st.json"}),
            mock.patch.object(cli, "show_diff", return_value=True),
            redirect_stdout(io.StringIO()),
        ):
            cli._watch_run(["/cfg"], (), False, [], excluded)
        self.assertNotIn("/old", excluded)
        self.assertIn(os.path.realpath("/s/st.json"), excluded)


if __name__ == "__main__":
    unittest.main()
