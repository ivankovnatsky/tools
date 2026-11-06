import json
import unittest
from unittest import mock

from tools.user import brew


class BrewBootstrapTest(unittest.TestCase):
    """The installer must be fetched first, then handed to bash as the -c
    script body. Passing the literal `$(curl ...)` makes bash substitute and
    word-split the script, then execute its first token ("#!/bin/bash") as a
    command name — rc 127, every time."""

    def _bootstrap(self, fake_run):
        with (
            mock.patch.object(brew.sys, "platform", "darwin"),
            mock.patch.object(brew, "system_bin", side_effect=lambda n: f"/usr/bin/{n}"),
            mock.patch.object(brew, "run_command", fake_run),
            mock.patch.object(brew, "_brew_bin", return_value="/opt/homebrew/bin/brew"),
        ):
            return brew._bootstrap_brew()

    def test_fetches_script_then_runs_body(self):
        script = "#!/bin/bash\necho install\n"
        calls = []

        def fake_run(cmd, env=None, cwd=None):
            calls.append(cmd)
            if cmd[0] == "/usr/bin/curl":
                return 0, script, ""
            return 0, "", ""

        result = self._bootstrap(fake_run)

        self.assertEqual(result, "/opt/homebrew/bin/brew")
        self.assertEqual(calls[0][:2], ["/usr/bin/curl", "-fsSL"])
        self.assertEqual(calls[1][:2], ["/usr/bin/bash", "-c"])
        # bash receives the script body, never an unexpanded substitution.
        self.assertEqual(calls[1][2], script)
        self.assertNotIn("$(", calls[1][2])

    def test_failed_download_never_runs_bash(self):
        calls = []

        def fake_run(cmd, env=None, cwd=None):
            calls.append(cmd)
            return 1, "", "curl: (6) no host"

        result = self._bootstrap(fake_run)

        self.assertIsNone(result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "/usr/bin/curl")

    def test_empty_download_never_runs_bash(self):
        # rc 0 with an empty body must not hand bash an empty script and
        # report success.
        def fake_run(cmd, env=None, cwd=None):
            return 0, "", ""

        result = self._bootstrap(fake_run)

        self.assertIsNone(result)


class BrewReconcileTest(unittest.TestCase):
    def _reconcile(self, config, state, fake_run):
        with (
            mock.patch.object(brew.sys, "platform", "darwin"),
            mock.patch.object(brew, "_brew_bin", return_value="/brew"),
            mock.patch.object(brew, "run_command", fake_run),
        ):
            return brew.install_brew_packages(config, state)

    def test_successful_install_records_ownership(self):
        calls = []

        def fake_run(cmd, env=None, cwd=None):
            calls.append(cmd)
            return 0, "", ""

        state = {}
        result = self._reconcile({"brews": ["b", "a"]}, state, fake_run)

        self.assertTrue(result)
        self.assertIn(["/brew", "install", "a", "b"], calls)
        self.assertEqual(state["brew"]["brews"], ["a", "b"])

    def test_partial_batch_install_records_what_landed(self):
        # brew installs targets in order; an earlier one can land before a
        # later failure. Those must not stay unowned forever.
        def fake_run(cmd, env=None, cwd=None):
            if cmd[1] == "install":
                return 1, "", "boom"
            if cmd[1] == "list":
                return 0, "a\n", ""
            return 0, "", ""

        state = {}
        result = self._reconcile({"brews": ["a", "b"]}, state, fake_run)

        self.assertFalse(result)
        self.assertEqual(state["brew"]["brews"], ["a"])

    def test_failed_uninstall_with_unqueryable_brew_keeps_ownership(self):
        # "Cannot query" must not read as "nothing installed": the package is
        # still on disk, so dropping ownership would orphan it forever.
        def fake_run(cmd, env=None, cwd=None):
            if cmd[1] in ("uninstall", "list"):
                return 1, "", "boom"
            return 0, "", ""

        state = {"version": 2, "brew": {"brews": ["x"], "casks": [], "taps": [], "masApps": {}}}
        result = self._reconcile({"brews": []}, state, fake_run)

        self.assertFalse(result)
        self.assertEqual(state["brew"]["brews"], ["x"])

    def test_successful_uninstall_releases_ownership(self):
        def fake_run(cmd, env=None, cwd=None):
            return 0, "", ""

        state = {"version": 2, "brew": {"brews": ["x"], "casks": [], "taps": [], "masApps": {}}}
        result = self._reconcile({"brews": []}, state, fake_run)

        self.assertTrue(result)
        self.assertEqual(state["brew"]["brews"], [])

    def test_mas_rename_under_same_id_updates_state(self):
        # Same app id under a new name produces no install/remove work, but
        # state must adopt the new name instead of keeping the stale key.
        state = {
            "version": 2,
            "brew": {"brews": [], "casks": [], "taps": [], "masApps": {"Old": 1}},
        }

        def fake_run(cmd, env=None, cwd=None):
            raise AssertionError("no brew command should run")

        result = self._reconcile({"masApps": {"New": 1}}, state, fake_run)

        self.assertTrue(result)
        self.assertEqual(state["brew"]["masApps"], {"New": 1})

    def test_non_darwin_is_a_noop(self):
        with mock.patch.object(brew.sys, "platform", "linux"):
            self.assertTrue(brew.install_brew_packages({"brews": ["a"]}, {}))


class BrewParseTest(unittest.TestCase):
    def test_unknown_keys_are_errors(self):
        _, errors = brew.parse_brew({"brews": [], "whalebrews": ["x"]})
        self.assertTrue(any("whalebrews" in e for e in errors))
        _, errors = brew.parse_brew({"onActivation": {"upgrade": True}})
        self.assertTrue(any("onActivation" in e and "upgrade" in e for e in errors))
        _, errors = brew.parse_brew({"caskArgs": {"appdir": "~/Apps"}})
        self.assertTrue(any("caskArgs" in e for e in errors))

    def test_bad_values_are_errors(self):
        for raw in (
            {"onActivation": {"cleanup": "none"}},
            {"onActivation": {"autoUpdate": "no"}},
            {"brews": "jq"},
            {"masApps": {"App": "abc"}},
            {"trust": {"taps": "a/b"}},
        ):
            _, errors = brew.parse_brew(raw)
            self.assertTrue(errors, raw)

    def test_valid_spec(self):
        spec, errors = brew.parse_brew(
            {
                "taps": ["user/homebrew-tap"],
                "onActivation": {"autoUpdate": False, "cleanup": "zap"},
                "trust": {"taps": ["user/homebrew-tap"]},
            }
        )
        self.assertEqual(errors, [])
        self.assertEqual(spec.taps, {"user/tap"})
        self.assertEqual(spec.trusted_taps, {"user/tap"})
        self.assertEqual(spec.cleanup, "zap")
        self.assertIs(spec.auto_update, False)


class FakeBrew:
    """Stateful brew stand-in for cleanup tests.

    `uninstall` refuses while an installed formula or cask still depends on the
    target, reports "No such keg" for missing ones, and, like Homebrew, runs its
    own autoremove afterwards unless HOMEBREW_NO_AUTOREMOVE is set.
    """

    def __init__(self, formulae=None, casks=None, taps=(), available=None):
        # formulae: name -> {"deps": [...], "on_request": bool, "tap": str, "aliases": [...]}
        self.formulae = {
            n: {"deps": [], "on_request": True, "aliases": [], **v}
            for n, v in (formulae or {}).items()
        }
        # casks: token -> {"tap": str, "formula": [...], "cask": [...]}
        self.casks = {n: {"formula": [], "cask": [], **v} for n, v in (casks or {}).items()}
        self.taps = list(taps)
        # available: not-installed packages brew can resolve -> same shape as above
        self.available = available or {}
        self.calls = []
        self.envs = []
        self.fail = set()

    def _short(self, name):
        return name.rsplit("/", 1)[-1]

    def _formula_json(self, name, f):
        return {
            "name": self._short(name),
            "full_name": name,
            "tap": f.get("tap", "homebrew/core"),
            "aliases": f["aliases"],
            "oldnames": [],
            "installed": [
                {
                    "installed_on_request": f["on_request"],
                    "runtime_dependencies": [{"full_name": d} for d in f["deps"]],
                }
            ],
        }

    def _cask_json(self, token, c):
        tap = c.get("tap", "homebrew/cask")
        return {
            "token": self._short(token),
            "full_token": token,
            "tap": tap,
            "old_tokens": [],
            "depends_on": {"formula": c["formula"], "cask": c["cask"]},
        }

    def _users(self, name):
        short = self._short(name)
        hits = [n for n, f in self.formulae.items() if short in map(self._short, f["deps"])]
        for n, c in self.casks.items():
            if short in map(self._short, c["formula"] + c["cask"]):
                hits.append(n)
        return hits

    def _find(self, table, name):
        for n, v in table.items():
            if self._short(name) in {self._short(n), *v.get("aliases", [])}:
                return n
        return None

    def _autoremove(self):
        while True:
            orphans = [
                n for n, f in self.formulae.items() if not f["on_request"] and not self._users(n)
            ]
            if not orphans:
                return
            for n in orphans:
                del self.formulae[n]

    def _deps_closure(self, names):
        out, queue = set(), list(names)
        while queue:
            info = self.available.get(queue.pop(), {})
            for dep in info.get("deps", []) + info.get("formula", []):
                if dep not in out:
                    out.add(dep)
                    queue.append(dep)
        return out

    def __call__(self, cmd, env=None, cwd=None):
        self.calls.append(cmd)
        self.envs.append(env or {})
        args = cmd[1:]
        if args[0] in self.fail or tuple(args) in self.fail:
            return 1, "", "boom"
        if args == ["leaves"]:
            used = {self._short(d) for f in self.formulae.values() for d in f["deps"]}
            used |= {self._short(d) for c in self.casks.values() for d in c["formula"]}
            return 0, "\n".join(n for n in self.formulae if self._short(n) not in used), ""
        if args == ["tap"]:
            return 0, "\n".join(self.taps), ""
        if args == ["info", "--json=v2", "--installed"]:
            data = {
                "formulae": [self._formula_json(n, f) for n, f in self.formulae.items()],
                "casks": [self._cask_json(n, c) for n, c in self.casks.items()],
            }
            return 0, json.dumps(data), ""
        if args[:3] == ["info", "--json=v2", "--cask"]:
            casks = [
                {
                    "token": n,
                    "depends_on": {
                        "formula": self.available.get(n, {}).get("formula", []),
                        "cask": self.available.get(n, {}).get("cask", []),
                    },
                }
                for n in args[3:]
            ]
            return 0, json.dumps({"casks": casks}), ""
        if args[0] == "deps":
            names = [a for a in args[1:] if not a.startswith("--")]
            if "--cask" in args:
                # Like brew: cask->cask requirements are not listed.
                deps = {d for n in names for d in self.available.get(n, {}).get("formula", [])}
                deps |= self._deps_closure(deps)
            else:
                deps = self._deps_closure(names)
            return 0, "\n".join(sorted(deps)), ""
        if args[0] == "tap":
            self.taps.append(args[1])
            return 0, "", ""
        if args[0] == "install":
            names = [a for a in args[1:] if not a.startswith("--")]
            for n in names:
                info = self.available.get(n, {})
                if "--cask" in args:
                    self.casks[n] = {
                        "formula": info.get("formula", []),
                        "cask": info.get("cask", []),
                    }
                else:
                    for dep in self._deps_closure([n]):
                        self.formulae.setdefault(
                            dep, {"deps": [], "on_request": False, "aliases": []}
                        )
                    self.formulae[n] = {
                        "deps": info.get("deps", []),
                        "on_request": True,
                        "aliases": [],
                    }
            return 0, "", ""
        if args[0] == "uninstall":
            is_cask = "--cask" in args
            table = self.casks if is_cask else self.formulae
            for name in [a for a in args[1:] if not a.startswith("--")]:
                found = self._find(table, name)
                if not found:
                    return 1, "", f"No such keg: {name}"
                users = [u for u in self._users(found) if u != found]
                if users:
                    return 1, "", f"Refusing to uninstall {name}: required by {users}"
                del table[found]
            if not (env or {}).get("HOMEBREW_NO_AUTOREMOVE"):
                self._autoremove()
            return 0, "", ""
        if args[0] == "list":
            table = self.casks if "--cask" in args else self.formulae
            return 0, "\n".join(table), ""
        return 0, "", ""


def _state(**kw):
    base = {"brews": [], "casks": [], "taps": [], "masApps": {}}
    return {"version": 2, "brew": {**base, **kw}}


class BrewOptionsTest(unittest.TestCase):
    def _run(self, config, state, fake=None):
        fake = fake or FakeBrew()
        with (
            mock.patch.object(brew.sys, "platform", "darwin"),
            mock.patch.object(brew, "_brew_bin", return_value="/brew"),
            mock.patch.object(brew, "run_command", fake),
        ):
            result = brew.install_brew_packages(config, state)
        return result, fake

    def _removed(self, fake):
        return [c[1:] for c in fake.calls if c[:2] == ["/brew", "uninstall"]]

    def test_invalid_config_runs_nothing(self):
        state = _state(brews=["x"])
        result, fake = self._run({"brews": [], "bogus": 1}, state)
        self.assertFalse(result)
        self.assertEqual(fake.calls, [])
        self.assertEqual(state["brew"]["brews"], ["x"])

    def test_auto_update_false_disables_brew_auto_update(self):
        _, fake = self._run({"brews": ["a"], "onActivation": {"autoUpdate": False}}, {})
        self.assertEqual(fake.calls, [["/brew", "install", "a"]])
        self.assertEqual(fake.envs[0]["HOMEBREW_NO_AUTO_UPDATE"], "1")

    def test_auto_update_true_clears_inherited_opt_out(self):
        with mock.patch.dict(brew.os.environ, {"HOMEBREW_NO_AUTO_UPDATE": "1"}):
            _, fake = self._run({"brews": ["a"], "onActivation": {"autoUpdate": True}}, {})
        self.assertNotIn("HOMEBREW_NO_AUTO_UPDATE", fake.envs[0])

    def test_installs_never_upgrade_without_update(self):
        _, fake = self._run({"brews": ["a"]}, {})
        self.assertEqual(fake.envs[0]["HOMEBREW_NO_INSTALL_UPGRADE"], "1")
        self.assertEqual(fake.envs[0]["HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK"], "1")

    def test_tracked_packages_are_upgraded_only_with_update(self):
        config = {"brews": ["a", "new"], "casks": ["c"]}
        _, fake = self._run(config, _state(brews=["a"], casks=["c"]))
        self.assertEqual(fake.calls, [["/brew", "install", "new"]])

        with mock.patch.object(brew, "run_command", FakeBrew()) as fake:
            with (
                mock.patch.object(brew.sys, "platform", "darwin"),
                mock.patch.object(brew, "_brew_bin", return_value="/brew"),
            ):
                state = _state(brews=["a"], casks=["c"])
                self.assertTrue(brew.install_brew_packages(config, state, update=True))
        self.assertEqual(
            fake.calls,
            [
                ["/brew", "upgrade", "--formula", "a"],
                ["/brew", "upgrade", "--cask", "c"],
                ["/brew", "install", "new"],
            ],
        )
        self.assertNotIn("HOMEBREW_NO_INSTALL_UPGRADE", fake.envs[0])

    def test_without_cleanup_unmanaged_items_are_never_queried(self):
        result, fake = self._run({"brews": ["a"]}, _state(brews=["a"]))
        self.assertTrue(result)
        self.assertEqual(fake.calls, [])

    def test_without_cleanup_owned_removals_stay_one_batch(self):
        fake = FakeBrew(formulae={"app": {"deps": ["lib"]}, "lib": {}})
        result, fake = self._run({}, _state(brews=["app", "lib"]), fake)
        self.assertTrue(result)
        self.assertEqual(self._removed(fake), [["uninstall", "app", "lib"]])
        self.assertIn(["/brew", "autoremove"], fake.calls)

    def test_zap_cleanup_removes_unmanaged_items(self):
        fake = FakeBrew(
            formulae={
                "kubernetes-cli": {"aliases": ["kubectl"]},
                "stray": {},
                "owned": {},
                "x/y/tapped": {"tap": "x/y"},
            },
            casks={"kitty": {}, "junk": {}, "m/tap/muxy": {"tap": "m/tap"}},
            taps=["homebrew/core", "homebrew/cask", "homebrew/bundle", "x/y", "z/w", "m/tap"],
        )
        config = {
            "brews": ["kubectl", "x/y/tapped"],
            "casks": ["kitty", "m/tap/muxy"],
            "onActivation": {"cleanup": "zap"},
        }
        result, fake = self._run(config, _state(brews=["owned"]), fake)

        self.assertTrue(result)
        self.assertEqual(
            self._removed(fake),
            [
                ["uninstall", "--cask", "--zap", "junk"],
                ["uninstall", "owned"],
                ["uninstall", "stray"],
            ],
        )
        # Built-in taps are never untapped; x/y and m/tap back configured packages.
        self.assertEqual([c[2] for c in fake.calls if c[1] == "untap"], ["z/w"])
        self.assertNotIn(["/brew", "autoremove"], fake.calls)

    def test_cleanup_removes_dependents_before_dependencies(self):
        # stray -> lib -> base: brew refuses lib while stray is installed, and its
        # own autoremove would delete lib and base before tools gets to them.
        fake = FakeBrew(
            formulae={
                "stray": {"deps": ["lib"]},
                "lib": {"deps": ["base"], "on_request": False},
                "base": {"on_request": False},
                "kept": {"deps": ["base2"]},
                "base2": {"on_request": False},
            }
        )
        config = {"brews": ["kept"], "onActivation": {"cleanup": "uninstall"}}
        result, fake = self._run(config, _state(brews=["kept"]), fake)
        self.assertTrue(result)
        self.assertEqual(
            self._removed(fake),
            [["uninstall", "stray"], ["uninstall", "lib"], ["uninstall", "base"]],
        )
        uninstall_envs = [e for c, e in zip(fake.calls, fake.envs) if c[1] == "uninstall"]
        self.assertTrue(all(e.get("HOMEBREW_NO_AUTOREMOVE") == "1" for e in uninstall_envs))
        self.assertEqual(set(fake.formulae), {"kept", "base2"})

    def test_owned_removals_under_cleanup_are_ordered_too(self):
        fake = FakeBrew(formulae={"app": {"deps": ["lib"]}, "lib": {}})
        result, fake = self._run(
            {"onActivation": {"cleanup": "zap"}}, _state(brews=["lib", "app"]), fake
        )
        self.assertTrue(result)
        self.assertEqual(self._removed(fake), [["uninstall", "app"], ["uninstall", "lib"]])

    def test_dependencies_of_kept_casks_are_never_targeted(self):
        fake = FakeBrew(
            formulae={"helper": {}},
            casks={"app": {"formula": ["helper"], "cask": ["runtime"]}, "runtime": {}},
        )
        config = {"casks": ["app"], "onActivation": {"cleanup": "zap"}}
        _, fake = self._run(config, _state(casks=["app"]), fake)
        self.assertEqual(self._removed(fake), [])

    def test_dependencies_of_pending_installs_are_never_targeted(self):
        fake = FakeBrew(formulae={"libfoo": {}}, available={"newtool": {"deps": ["libfoo"]}})
        config = {"brews": ["newtool"], "onActivation": {"cleanup": "zap"}}
        result, fake = self._run(config, {}, fake)
        self.assertTrue(result)
        self.assertIn(["/brew", "install", "newtool"], fake.calls)
        self.assertEqual(self._removed(fake), [])
        self.assertIn("libfoo", fake.formulae)

    def test_cask_requirements_of_pending_casks_are_never_targeted(self):
        fake = FakeBrew(casks={"runtime": {}}, available={"app": {"cask": ["runtime"]}})
        config = {"casks": ["app"], "onActivation": {"cleanup": "zap"}}
        _, fake = self._run(config, {}, fake)
        self.assertEqual(self._removed(fake), [])

    def test_cleanup_waits_for_new_taps(self):
        fake = FakeBrew(formulae={"stray": {}}, available={"u/t/tool": {"deps": ["stray"]}})
        config = {"taps": ["u/t"], "brews": ["u/t/tool"], "onActivation": {"cleanup": "zap"}}
        result, fake = self._run(config, {}, fake)
        self.assertTrue(result)
        self.assertIn(["/brew", "tap", "u/t"], fake.calls)
        self.assertFalse([c for c in fake.calls if c[1] == "deps"])
        self.assertEqual(self._removed(fake), [])

    def test_mas_is_kept_while_mas_apps_are_configured(self):
        fake = FakeBrew(formulae={"mas": {}})
        config = {"masApps": {"Pages": 1}, "onActivation": {"cleanup": "zap"}}
        with mock.patch.object(brew, "_mas_bin_from_env", return_value="/mas"):
            _, fake = self._run(config, _state(masApps={"Pages": 1}), fake)
        self.assertEqual(self._removed(fake), [])

    def test_owned_removals_run_when_cleanup_is_deferred(self):
        fake = FakeBrew(formulae={"old": {}, "stray": {}}, casks={"gone": {}})
        config = {"taps": ["u/t"], "onActivation": {"cleanup": "zap"}}
        state = _state(brews=["old"], casks=["gone"])
        result, fake = self._run(config, state, fake)
        self.assertTrue(result)
        self.assertEqual(
            self._removed(fake), [["uninstall", "old"], ["uninstall", "--cask", "--zap", "gone"]]
        )
        self.assertIn("stray", fake.formulae)
        self.assertEqual((state["brew"]["brews"], state["brew"]["casks"]), ([], []))

    def test_owned_removals_run_when_cleanup_query_fails(self):
        fake = FakeBrew(formulae={"old": {}})
        fake.fail.add("info")
        state = _state(brews=["old"])
        result, fake = self._run({"onActivation": {"cleanup": "uninstall"}}, state, fake)
        self.assertFalse(result)
        self.assertEqual(self._removed(fake), [["uninstall", "old"]])
        self.assertEqual(state["brew"]["brews"], [])

    def test_cask_requirements_resolve_transitively(self):
        # app (pending) -> mid (pending) -> runtime (installed cask) and libx (installed leaf).
        fake = FakeBrew(
            formulae={"libx": {}},
            casks={"runtime": {}},
            available={"app": {"cask": ["mid"]}, "mid": {"cask": ["runtime"], "formula": ["libx"]}},
        )
        config = {"casks": ["app"], "onActivation": {"cleanup": "zap"}}
        _, fake = self._run(config, {}, fake)
        self.assertEqual(self._removed(fake), [])

    def test_mas_is_kept_while_mas_apps_are_being_removed(self):
        fake = FakeBrew(formulae={"mas": {}})
        with mock.patch.object(brew, "_mas_bin_from_env", return_value="/mas"):
            _, fake = self._run(
                {"onActivation": {"cleanup": "zap"}}, _state(masApps={"Pages": 1}), fake
            )
        self.assertEqual(self._removed(fake), [])
        self.assertIn(["/mas", "uninstall", "1"], fake.calls)

    def test_missing_mas_with_pending_work_fails(self):
        state = {}
        with mock.patch.object(brew, "_mas_bin_from_env", return_value=None):
            result, _ = self._run({"masApps": {"Pages": 1}}, state)
        self.assertFalse(result)
        self.assertEqual(state["brew"]["masApps"], {})

    def test_dropped_core_tap_is_forgotten_not_untapped(self):
        state = _state(taps=["homebrew/core", "u/t"])
        result, fake = self._run({"taps": ["u/t"]}, state)
        self.assertTrue(result)
        self.assertEqual(fake.calls, [])
        self.assertEqual(state["brew"]["taps"], ["u/t"])
        self.assertEqual(
            brew._plan(brew.parse_brew({})[0], _state(taps=["homebrew/core"])["brew"])[
                "taps_remove"
            ],
            [],
        )

    def test_one_failed_removal_does_not_block_others(self):
        fake = FakeBrew(formulae={"a": {}, "b": {}})
        fake.fail.add(("uninstall", "a"))
        result, fake = self._run({"onActivation": {"cleanup": "zap"}}, {}, fake)
        self.assertFalse(result)
        self.assertIn(["/brew", "uninstall", "b"], fake.calls)

    def test_trusted_only_tap_is_kept(self):
        fake = FakeBrew(taps=["u/t"])
        config = {"trust": {"taps": ["u/homebrew-t"]}, "onActivation": {"cleanup": "zap"}}
        _, fake = self._run(config, _state(trustedTaps=["u/t"]), fake)
        self.assertFalse([c for c in fake.calls if c[1] in ("untap", "untrust")])

    def test_tap_of_full_entry_is_normalized_and_kept(self):
        fake = FakeBrew(casks={"u/t/app": {"tap": "u/t"}}, taps=["u/t"])
        config = {"casks": ["u/homebrew-t/app"], "onActivation": {"cleanup": "zap"}}
        _, fake = self._run(config, _state(casks=["u/homebrew-t/app"]), fake)
        self.assertFalse([c for c in fake.calls if c[1] == "untap"])

    def test_core_tap_is_never_untapped(self):
        _, fake = self._run({"taps": []}, _state(taps=["homebrew/core"]))
        self.assertFalse([c for c in fake.calls if c[1] == "untap"])

    def test_failed_tap_is_not_trusted(self):
        fake = FakeBrew()
        fake.fail.add("tap")
        state = {}
        result, fake = self._run({"taps": ["u/t"], "trust": {"taps": ["u/t"]}}, state, fake)
        self.assertFalse(result)
        self.assertNotIn(["/brew", "trust", "--tap", "u/t"], fake.calls)
        self.assertEqual(state["brew"]["trustedTaps"], [])

    def test_uninstall_cleanup_does_not_zap(self):
        fake = FakeBrew(casks={"junk": {}})
        _, fake = self._run({"onActivation": {"cleanup": "uninstall"}}, {}, fake)
        self.assertEqual(self._removed(fake), [["uninstall", "--cask", "junk"]])

    def test_zap_applies_to_owned_cask_removals(self):
        fake = FakeBrew(casks={"old": {}})
        _, fake = self._run({"onActivation": {"cleanup": "zap"}}, _state(casks=["old"]), fake)
        self.assertEqual(self._removed(fake), [["uninstall", "--cask", "--zap", "old"]])

    def test_cleanup_with_nothing_unmanaged_only_queries(self):
        fake = FakeBrew(formulae={"a": {}}, taps=["homebrew/core"])
        config = {"brews": ["a"], "onActivation": {"cleanup": "zap"}}
        result, fake = self._run(config, _state(brews=["a"]), fake)
        self.assertTrue(result)
        self.assertEqual({c[1] for c in fake.calls}, {"leaves", "tap", "info"})

    def test_failed_cleanup_query_removes_nothing_but_still_installs(self):
        fake = FakeBrew(formulae={"stray": {}})
        fake.fail.add("leaves")
        result, fake = self._run({"brews": ["a"], "onActivation": {"cleanup": "zap"}}, {}, fake)
        self.assertFalse(result)
        self.assertIn(["/brew", "install", "a"], fake.calls)
        self.assertEqual(self._removed(fake), [])

    def test_trust_taps_before_install_and_untrust_on_removal(self):
        state = {}
        config = {"taps": ["u/homebrew-t"], "trust": {"taps": ["u/homebrew-t"]}, "brews": ["u/t/f"]}
        _, fake = self._run(config, state)
        self.assertEqual(
            fake.calls[:3],
            [
                ["/brew", "tap", "u/t"],
                ["/brew", "trust", "--tap", "u/t"],
                ["/brew", "install", "u/t/f"],
            ],
        )
        self.assertEqual(state["brew"]["trustedTaps"], ["u/t"])

        _, fake = self._run({"taps": ["u/t"], "brews": ["u/t/f"]}, state)
        self.assertEqual(fake.calls, [["/brew", "untrust", "--tap", "u/t"]])
        self.assertEqual(state["brew"]["trustedTaps"], [])


class BrewDiffTest(unittest.TestCase):
    def _diff(self, config, state, fake):
        def read_only(cmd, env=None, cwd=None):
            if cmd[1] in ("install", "uninstall", "untap", "trust", "untrust", "autoremove") or (
                cmd[1] == "tap" and len(cmd) > 2
            ):
                raise AssertionError(f"diff ran a mutating command: {cmd}")
            return fake(cmd, env, cwd)

        with (
            mock.patch.object(brew.sys, "platform", "darwin"),
            mock.patch.object(brew, "_brew_bin", return_value="/brew"),
            mock.patch.object(brew, "run_command", read_only),
        ):
            return dict(brew.diff_brew(config, state))

    def test_cleanup_rows_are_marked_unowned(self):
        fake = FakeBrew(
            formulae={"stray": {"deps": ["dep"]}, "dep": {"on_request": False}},
            casks={"junk": {}},
            taps=["homebrew/core", "z/w"],
        )
        rows = self._diff({"onActivation": {"cleanup": "zap"}}, {}, fake)
        self.assertEqual(
            rows["brews"],
            [
                "    - remove stray (not installed by tools)",
                "    - remove dep (dependency nothing else needs)",
            ],
        )
        self.assertEqual(rows["casks"], ["    - zap junk (not installed by tools)"])
        self.assertEqual(rows["taps"], ["    - untap z/w (not installed by tools)"])

    def test_deferred_cleanup_is_shown(self):
        fake = FakeBrew(formulae={"stray": {}})
        rows = self._diff({"taps": ["u/t"], "onActivation": {"cleanup": "zap"}}, {}, fake)
        self.assertEqual(rows["cleanup"], ["    ? cleanup deferred until tapped: u/t"])
        self.assertNotIn("brews", rows)

    def test_deferred_cleanup_still_lists_owned_removals(self):
        fake = FakeBrew(formulae={"old": {}})
        rows = self._diff(
            {"taps": ["u/t"], "onActivation": {"cleanup": "zap"}}, _state(brews=["old"]), fake
        )
        self.assertEqual(rows["brews"], ["    - remove old"])

    def test_errors_are_shown(self):
        rows = self._diff({"onActivation": {"cleanup": "all"}}, {}, FakeBrew())
        self.assertIn("errors", rows)


if __name__ == "__main__":
    unittest.main()
