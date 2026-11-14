import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from click.testing import CliRunner
from test_brew import FakeBrew, _state
from test_fetch import URL, FakeOpener
from test_flatpak import FakeFlatpak
from test_ollama_models import FakeOllama

from tools import cli
from tools import diff as diff_mod
from tools.user import brew, fetch, flatpak, git_repos, npm, ollama_models, skills


def _quiet(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **k)


class UpdatePreviewTest(unittest.TestCase):
    """`deploy --update` work shows in the preview, so the prompt appears."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = os.path.realpath(self._tmp.name)

    def test_unpinned_packages_upgrade_only_with_update(self):
        cases = {
            "bun": lambda p, s, u: diff_mod._diff_bun(p, {}, s, {}, u),
            "npm": lambda p, s, u: diff_mod._diff_npm(p, {}, s, {}, u),
            "uv": lambda p, s, u: diff_mod._diff_uv(p, {}, s, u),
            "go": lambda p, s, u: diff_mod._diff_go(p, {}, s, u),
            "cargo": lambda p, s, u: diff_mod._diff_cargo(p, {}, s, u),
        }
        for section, run in cases.items():
            with self.subTest(section):
                packages = {"a": {}, "pinned": {"version": "1.0"}}
                state = {
                    section: {
                        "packages": {"a": {}, "pinned": {"version": "1.0"}},
                        **({"context": "|", "root": os.path.expanduser("~/.cargo")}),
                    }
                }
                with mock.patch.dict(os.environ, {"CARGO_INSTALL_ROOT": "", "CARGO_HOME": ""}):
                    self.assertEqual(run(packages, state, False), [])
                    self.assertEqual(run(packages, state, True), ["  ~ upgrade a"])

    def test_pull_of_tracked_checkout(self):
        dest = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(dest, ".git"))
        state = {"gitRepos": {"installed": [dest]}}
        self.assertEqual(diff_mod._diff_git_repos({dest: "u"}, state), [])
        self.assertEqual(diff_mod._diff_git_repos({dest: "u"}, state, True), [f"  ~ pull {dest}"])

    def test_files_on_update_hook(self):
        src = os.path.join(self.tmp, "a.conf")
        with open(src, "w") as f:
            f.write("x\n")
        target = os.path.join(self.tmp, "out", "a.conf")
        entries = [{"source": "a.conf", "target": target, "onUpdate": "true"}]
        state: dict = {}
        from tools.user import files

        _quiet(files.install_files, entries, self.tmp, state)
        self.assertEqual(diff_mod._diff_files(entries, self.tmp, state), [])
        self.assertEqual(
            diff_mod._diff_files(entries, self.tmp, state, update=True),
            ["  ~ run onUpdate: true"],
        )

    def test_fetch_refetch(self):
        target = os.path.join(self.tmp, "f")
        raw = {target: {"url": URL}}
        state: dict = {}
        _quiet(fetch.install_fetch, raw, state, None, False, FakeOpener())
        self.assertEqual(fetch.diff_fetch(raw, state), [])
        self.assertEqual(
            fetch.diff_fetch(raw, state, None, True), [f"  ~ fetch {target} <- {URL} (--update)"]
        )

    def test_skills_refetch(self):
        source = {"url": "https://example.com/s.git"}
        cfg = {"cacheDir": self.tmp, "sources": {"s": source}, "enable": ["s/x"]}
        os.makedirs(os.path.join(self.tmp, "s", ".git"))
        state = {"skills": {"sources": {"s": skills._source_fingerprint(source)}}}
        self.assertNotIn("  ~ refetch s (--update)", skills.diff_skills(cfg, self.tmp, state))
        self.assertIn("  ~ refetch s (--update)", skills.diff_skills(cfg, self.tmp, state, True))

    def test_flatpak_update(self):
        fake = FakeFlatpak(installed=["app"])
        state = {"flatpak": {"packages": ["app"], "remotes": []}}
        with (
            mock.patch.object(flatpak, "_find_flatpak", return_value="flatpak"),
            mock.patch.object(flatpak, "run_command", fake.run),
        ):
            self.assertEqual(flatpak.diff_flatpak({"packages": ["app"]}, state), [])
            self.assertEqual(
                flatpak.diff_flatpak({"packages": ["app"]}, state, True), ["  ~ update app"]
            )

    def test_ollama_refresh(self):
        fake = FakeOllama(installed=["m:latest"])
        state = {"ollamaModels": {"installed": ["m:latest"]}}
        with (
            mock.patch.object(ollama_models, "_find_ollama", return_value="ollama"),
            mock.patch.object(ollama_models, "run_command", fake.run),
        ):
            self.assertEqual(ollama_models.diff_ollama_models({"models": ["m"]}, state), [])
            self.assertEqual(
                ollama_models.diff_ollama_models({"models": ["m"]}, state, True),
                ["  ~ pull m:latest (--update)"],
            )

    def test_brew_upgrades(self):
        config = {"brews": ["a"], "casks": ["c"], "masApps": {"App": 1}}
        state = _state(brews=["a"], casks=["c"], masApps={"App": 1})
        with (
            mock.patch.object(brew.sys, "platform", "darwin"),
            mock.patch.object(brew, "_brew_bin", return_value="/brew"),
            mock.patch.object(brew, "_mas_bin_from_env", return_value="/mas"),
            mock.patch.object(brew, "run_command", FakeBrew()),
        ):
            self.assertEqual(brew.diff_brew(config, state), [])
            self.assertEqual(
                dict(brew.diff_brew(config, state, True)),
                {
                    "brews": ["    ~ upgrade a"],
                    "casks": ["    ~ upgrade c"],
                    "masApps": ["    ~ upgrade App (1)"],
                },
            )

    def test_deploy_update_previews_with_update(self):
        seen = []
        with (
            mock.patch.object(cli, "_load_merged_config", return_value={}),
            mock.patch.object(
                cli, "show_diff", side_effect=lambda *a, **k: seen.append(k["update"]) or False
            ),
            mock.patch.object(cli.click, "prompt", return_value="no") as prompted,
            mock.patch.object(cli, "_deploy", return_value=True),
        ):
            result = CliRunner().invoke(cli.main, ["deploy", "--update"])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(seen, [True])
        prompted.assert_called_once()


class BrewPreviewTest(unittest.TestCase):
    def test_bootstrap_is_shown(self):
        with (
            mock.patch.object(brew.sys, "platform", "darwin"),
            mock.patch.object(brew, "_brew_bin", return_value=None),
            mock.patch.object(brew, "_nix_managed_error", return_value=None),
        ):
            rows = dict(brew.diff_brew({"brews": ["a"]}, {}))
        self.assertEqual(rows["homebrew"], ["    + install Homebrew (brew not found)"])

    def test_autoremove_after_removal_is_shown(self):
        with (
            mock.patch.object(brew.sys, "platform", "darwin"),
            mock.patch.object(brew, "_brew_bin", return_value="/brew"),
            mock.patch.object(brew, "_nix_managed_error", return_value=None),
        ):
            rows = dict(brew.diff_brew({}, _state(brews=["old"])))
        self.assertEqual(
            rows["brews"], ["    - remove old", "    - autoremove dependencies nothing else needs"]
        )


class LocationChangeTest(unittest.TestCase):
    def test_npm_and_bun_prefix_change_releases_tracked(self):
        for section, key, run in (
            ("npm", "npmBin", lambda p, paths, s: diff_mod._diff_npm(p, paths, s, {})),
            ("bun", "bunBin", lambda p, paths, s: diff_mod._diff_bun(p, paths, s, {})),
        ):
            with self.subTest(section):
                state = {section: {"prefix": "/old", "packages": {"a": {}, "b": {}}}}
                self.assertEqual(
                    run({"a": {}}, {key: "/new"}, state),
                    [
                        f"  ~ {section} prefix changed (/old -> /new); releasing tracked packages",
                        "  ~ forget b (left in the old location)",
                        "  + install a",
                    ],
                )

    def test_uv_context_change_releases_tracked(self):
        paths = {"uvBin": "/b", "uvToolDir": "/t"}
        state = {"uv": {"context": "/x|/y", "packages": {"a": {}, "b": {}}}}
        self.assertEqual(
            diff_mod._diff_uv({"a": {}}, paths, state),
            [
                "  ~ uv tool dir changed (/x|/y -> /b|/t); releasing tracked packages",
                "  ~ forget b (left in the old location)",
                "  + install a",
            ],
        )

    def test_go_and_cargo_context_change_reinstall(self):
        state = {"go": {"context": "/old|", "packages": {"a": {}}}}
        self.assertEqual(
            diff_mod._diff_go({"a": {}}, {"goBin": "/new"}, state),
            ["  ~ reinstall a (go install dir changed)"],
        )
        state = {"cargo": {"context": "/old|", "packages": {"a": {}, "b": {}}}}
        self.assertEqual(
            diff_mod._diff_cargo({"a": {}}, {"cargoRoot": "/new"}, state),
            [
                "  ~ reinstall a (cargo install dir changed)",
                "  ~ forget b (cargo install dir changed; binary left behind)",
            ],
        )


class RemovalRowsTest(unittest.TestCase):
    def test_go_forgets_unknown_and_shared_paths(self):
        state = {
            "go": {
                "packages": {
                    "keep": {"path": "/b/x"},
                    "shared": {"path": "/b/x"},
                    "unknown": {},
                    "gone": {"path": "/b/y"},
                }
            }
        }
        self.assertEqual(
            diff_mod._diff_go({"keep": {}}, {}, state),
            [
                "  - remove gone",
                "  ~ forget shared (binary /b/x still in use)",
                "  ~ forget unknown (install path unknown, binary not removed)",
            ],
        )

    def test_cargo_forgets_crate_missing_from_record(self):
        with tempfile.TemporaryDirectory() as root:
            paths = {"cargoRoot": root}
            state = {
                "cargo": {
                    "context": f"{root}|",
                    "root": root,
                    "packages": {"g": {"source": "https://x/r", "commit": "abc"}, "p": {}},
                }
            }
            self.assertEqual(
                diff_mod._diff_cargo({}, paths, state),
                ["  ~ forget g (not in cargo's install record)", "  - remove p"],
            )

    def test_invalid_restart_units_show_the_error_not_an_install(self):
        bad = {"a": {"restartUnits": "launchd:x"}}
        for section, run in (
            ("bun", lambda: diff_mod._diff_bun(bad, {}, {}, {})),
            ("npm", lambda: diff_mod._diff_npm(bad, {}, {}, {})),
            ("uv", lambda: diff_mod._diff_uv(bad, {}, {})),
            ("go", lambda: diff_mod._diff_go(bad, {}, {})),
            ("cargo", lambda: diff_mod._diff_cargo(bad, {}, {})),
        ):
            with self.subTest(section):
                self.assertEqual(
                    run(), [f"  ! {section}.packages.a: restartUnits must be a list of strings"]
                )

    def test_non_mapping_entry_deploys_nothing_in_its_section(self):
        from tools.user import bun, cargo, go, pi, uv

        calls = []
        record = lambda *a, **k: calls.append(a) or (0, "", "")  # noqa: E731
        state = {"npm": {"packages": {"old": {}}}}
        for module, run in (
            (npm, lambda p: npm.install_npm_packages(p, {}, state, {"configFile": "x"})),
            (bun, lambda p: bun.install_bun_packages(p, {}, state, {"configFile": "x"})),
            (uv, lambda p: uv.install_uv_packages(p, {}, state)),
            (go, lambda p: go.install_go_packages(p, {}, state)),
            (cargo, lambda p: cargo.install_cargo_packages(p, {}, state)),
            (pi, lambda p: pi.install_pi_packages(p, {}, state)),
        ):
            with self.subTest(module.__name__), mock.patch.object(module, "run_command", record):
                self.assertFalse(_quiet(run, {"ruff": "1.0", "ok": {}}))
        self.assertEqual(calls, [])
        self.assertEqual(state, {"npm": {"packages": {"old": {}}}})

    def test_non_mapping_package_entry_is_an_error_row(self):
        for section in ("npm", "bun", "uv", "go", "cargo", "pi"):
            with self.subTest(section):
                config = {section: {"packages": {"ruff": "1.0"}}, "stateFile": "/nonexistent"}
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    clean = diff_mod.show_diff(config, "/tmp", (section,))
                self.assertFalse(clean)
                self.assertIn(
                    f"! {section}.packages.ruff must be a mapping, got str", out.getvalue()
                )


class NpmPreviewTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        self.paths = {"npmBin": os.path.join(self.tmp, "bin"), "nodejs": "/node"}

    def test_config_file_is_previewed(self):
        with mock.patch.dict(os.environ, {"HOME": self.tmp}):
            self.assertEqual(
                diff_mod._diff_npm({}, {}, {}, {"configFile": "x"}), ["  + create ~/.npmrc"]
            )

    def test_config_file_without_packages_or_paths_deploys(self):
        state: dict = {}
        with mock.patch.dict(os.environ, {"HOME": self.tmp}):
            self.assertTrue(_quiet(npm.install_npm_packages, {}, {}, state, {"configFile": "x"}))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, ".npmrc")))

    def test_post_install_and_subpackages_are_previewed(self):
        os.makedirs(os.path.join(self.tmp, "lib", "node_modules", "a"))
        info = {"postInstall": "make", "subpackages": {"sp": {"version": "1"}}}
        state = {"npm": {"prefix": self.paths["npmBin"], "packages": {"a": {}}}}
        self.assertEqual(
            diff_mod._diff_npm({"a": info}, self.paths, state, {}),
            ["  ~ run postInstall for a: make", "  + install subpackages for a: sp@1"],
        )
        self.assertEqual(
            diff_mod._diff_npm({"b": info}, self.paths, state, {}),
            [
                "  - remove a",
                "  + install b",
                "  ~ run postInstall for b: make",
                "  + install subpackages for b: sp@1",
            ],
        )


class GitReposTest(unittest.TestCase):
    def test_deleted_tracked_checkout_is_cloned_again(self):
        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "repo")
            calls = []
            with (
                mock.patch.object(git_repos, "system_bin", return_value="git"),
                mock.patch.object(
                    git_repos,
                    "run_command",
                    side_effect=lambda cmd, env: calls.append(cmd) or (0, "", ""),
                ),
            ):
                state = {"gitRepos": {"installed": [dest]}}
                self.assertEqual(
                    diff_mod._diff_git_repos({dest: "u"}, state), [f"  + clone {dest}"]
                )
                self.assertTrue(_quiet(git_repos.install_git_repos, {dest: "u"}, state))
            self.assertEqual(calls, [["git", "clone", "u", dest]])

    def test_tracked_non_checkout_is_released_not_removed(self):
        with tempfile.TemporaryDirectory() as d:
            state = {"gitRepos": {"installed": [d]}}
            self.assertEqual(
                diff_mod._diff_git_repos({}, state),
                [f"  ~ release {d} (not a git checkout, left in place)"],
            )
            os.makedirs(os.path.join(d, ".git"))
            with mock.patch.object(git_repos, "_has_local_work", return_value=False):
                self.assertEqual(
                    diff_mod._diff_git_repos({}, state), [f"  - remove {d} (deletes the checkout)"]
                )

    def test_release_and_keep_rows_match_deploy(self):
        with tempfile.TemporaryDirectory() as d:
            plain, unreadable = os.path.join(d, "plain"), os.path.join(d, "unreadable")
            os.makedirs(plain)
            os.makedirs(os.path.join(unreadable, ".git"))
            state = {"gitRepos": {"installed": [plain, unreadable]}}
            # git fails on the checkout: deploy keeps it, and so does the preview.
            failing = lambda cmd, env=None, cwd=None: (128, "", "fatal")  # noqa: E731
            with (
                mock.patch.object(git_repos, "run_command", side_effect=failing),
                mock.patch.object(git_repos, "system_bin", return_value="git"),
                mock.patch("tools.system_paths.system_bin", return_value="git"),
            ):
                rows = diff_mod._diff_git_repos({}, state)
                self.assertTrue(_quiet(git_repos.install_git_repos, {}, state))
            self.assertEqual(
                rows,
                [
                    f"  ~ release {plain} (not a git checkout, left in place)",
                    f"  ~ keep {unreadable} (uncommitted or unpushed work, or git cannot "
                    "read it; not removed)",
                ],
            )
            self.assertEqual(state["gitRepos"]["installed"], [unreadable])
            self.assertTrue(os.path.isdir(plain) and os.path.isdir(unreadable))


if __name__ == "__main__":
    unittest.main()
