import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from fake_defaults import FakeDefaults

from tools import cli as cli_mod
from tools import fsutil
from tools.user import defaults
from tools.user.defaults import DefaultsKey, parse_defaults, same


class SameTest(unittest.TestCase):
    def test_types_are_strict(self):
        self.assertFalse(same(True, 1))
        self.assertFalse(same(1, True))
        self.assertFalse(same(64, 64.0))
        self.assertFalse(same("1", 1))
        self.assertTrue(same(64, 64))
        self.assertTrue(same(1000.0, 1000.0))

    def test_nested(self):
        a = {"x": [1, {"y": True}], "z": "s"}
        self.assertTrue(same(a, {"x": [1, {"y": True}], "z": "s"}))
        self.assertFalse(same(a, {"x": [1, {"y": 1}], "z": "s"}))
        self.assertFalse(same([1, 2], [2, 1]))
        self.assertFalse(same({"a": 1}, {"a": 1, "b": 2}))


class ParseTest(unittest.TestCase):
    def test_scopes_and_restart(self):
        spec, errors = parse_defaults(
            {
                "user": {"com.apple.dock": {"tilesize": 64}},
                "currentHost": {"com.apple.controlcenter": {"Weather": 2}},
                "system": {"/Library/Preferences/com.example.app.plist": {"On": True}},
                "restart": {"com.apple.dock": "Dock", "com.apple.finder": ["Finder"]},
            }
        )
        self.assertEqual(errors, [])
        self.assertEqual(
            [k.id for k in spec.keys],
            [
                ("user", "com.apple.dock", "tilesize"),
                ("currentHost", "com.apple.controlcenter", "Weather"),
                ("system", "/Library/Preferences/com.example.app", "On"),
            ],
        )
        self.assertEqual(spec.restart, {"com.apple.dock": ["Dock"], "com.apple.finder": ["Finder"]})

    def test_errors(self):
        _, errors = parse_defaults(
            {
                "user": {"d": {"nul": [None], 1: "x", "big": 2**70, "dt": object()}},
                "system": {"relative.domain": {"k": 1}},
                "bogus": {},
                "restart": {"d": 3},
            }
        )
        text = "\n".join(errors)
        for needle in ("null", "non-empty string", "out of plist range", "unsupported", "absolute"):
            self.assertIn(needle, text)
        self.assertIn("unknown keys ['bogus']", text)
        self.assertIn("restart", text)

    def test_xml_unsafe_strings_hold_the_section(self):
        for value in ("a\x1bb", {"k\x1b": 1}, ["ok", "\x00"]):
            spec, errors = parse_defaults({"user": {"d": {"good": 1, "bad": value}}})
            self.assertIn("not plist-safe", "\n".join(errors), value)

    def test_path_domains_are_canonical(self):
        tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        prefs = os.path.join(tmp, "prefs")
        spec, errors = parse_defaults(
            {"user": {f"{tmp}/./prefs/": {"a": 1}, f"{tmp}/p.plist": {"c": 1}}}
        )
        self.assertEqual(errors, [])
        self.assertEqual([k.domain for k in spec.keys], [prefs, os.path.join(tmp, "p")])
        for dup in (f"{tmp}/prefs/", f"{tmp}/./prefs", f"{tmp}/prefs.plist", f"{tmp}//prefs"):
            _, errors = parse_defaults({"user": {prefs: {"k": 1}, dup: {"k": 2}}})
            self.assertIn("declared more than once", "\n".join(errors), dup)

    def test_same_plist_spelled_differently_is_one_domain(self):
        tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        os.makedirs(os.path.join(tmp, "Real"))
        open(os.path.join(tmp, "Real", "com.example.App.plist"), "w").close()
        os.symlink(os.path.join(tmp, "Real"), os.path.join(tmp, "link"))
        real = os.path.join(tmp, "Real", "com.example.App")
        spellings = [os.path.join(tmp, "link", "com.example.App")]
        if fsutil._case_insensitive(tmp):
            spellings += [os.path.join(tmp, "real", "COM.example.app.plist")]
        for other in spellings:
            self.assertEqual(defaults.canonical_domain(other), real, other)
            _, errors = defaults.identify(
                *parse_defaults({"user": {real: {"k": 1}, other: {"k": 2}}})
            )
            self.assertIn("declared more than once", "\n".join(errors), other)

    def test_parse_does_not_touch_the_filesystem(self):
        def boom(*args, **kwargs):
            raise AssertionError("parse touched the filesystem")

        raw = {
            "user": {
                "/Volumes/Data/Prefs/com.Example.App.plist": {"k": 1},
                "~/Library/Preferences/ByHost/com.apple.controlcenter": {"W": 2},
                "~/Library/Preferences/com.apple.finder.plist": {"k": 1},
            },
            "system": {"/Library/Preferences/com.example.sys": {"k": 1}},
            "restart": {"/Volumes/Data/Prefs/com.Example.App": "Finder"},
        }
        with (
            mock.patch.object(os, "listdir", boom),
            mock.patch.object(os, "stat", boom),
            mock.patch.object(os, "lstat", boom),
            mock.patch.object(os.path, "realpath", boom),
            mock.patch.object(os.path, "exists", boom),
            mock.patch.object(os.path, "isdir", boom),
        ):
            spec, errors = parse_defaults(raw)
        self.assertEqual(errors, [])
        self.assertEqual(
            [k.domain for k in spec.keys],
            [
                "/Volumes/Data/Prefs/com.Example.App",
                os.path.expanduser("~/Library/Preferences/ByHost/com.apple.controlcenter"),
                "com.apple.finder",
                "/Library/Preferences/com.example.sys",
            ],
        )

    def test_dock_keys_are_reserved_through_symlinked_spellings(self):
        tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        prefs = os.path.join(os.path.expanduser("~"), "Library", "Preferences")
        link = os.path.join(tmp, "prefs")
        os.symlink(prefs, link)
        raw = {"user": {os.path.join(link, "com.apple.dock"): {"persistent-apps": []}}}
        spec, errors = parse_defaults(raw)
        if os.path.isdir(prefs):
            _, errors = defaults.identify(spec, errors)
            self.assertIn("managed by the dock section", "\n".join(errors))

    def test_home_preferences_path_is_the_domain_name(self):
        for path in (
            "~/Library/Preferences/com.apple.finder",
            "~/Library/Preferences/com.apple.finder.plist",
            os.path.expanduser("~/Library/./Preferences/com.apple.finder"),
        ):
            self.assertEqual(defaults.canonical_domain(path), "com.apple.finder", path)
            _, errors = parse_defaults({"user": {"com.apple.finder": {"k": 1}, path: {"k": 2}}})
            self.assertIn("declared more than once", "\n".join(errors), path)
        for name in ("-g", ".GlobalPreferences", "~/Library/Preferences/.GlobalPreferences"):
            self.assertEqual(defaults.canonical_domain(name), "NSGlobalDomain", name)
        with mock.patch.dict(os.environ, {"HOME": "/homeless-shelter"}):
            self.assertEqual(
                defaults.canonical_domain("~/Library/Preferences/.GlobalPreferences"),
                "NSGlobalDomain",
            )
            self.assertEqual(
                defaults.canonical_domain("~/Library/Preferences/ByHost/com.apple.controlcenter"),
                "/homeless-shelter/Library/Preferences/ByHost/com.apple.controlcenter",
            )

    def test_dock_guard_uses_canonical_domain(self):
        for domain in ("~/Library/Preferences/com.apple.dock.plist", "com.apple.dock"):
            _, errors = parse_defaults({"user": {domain: {"persistent-apps": []}}})
            self.assertIn("managed by the dock section", errors[0], domain)

    def test_null_value_is_unmanaged(self):
        spec, errors = parse_defaults({"user": {"d": {"k": None, "j": 1}}})
        self.assertEqual(errors, [])
        self.assertEqual([k.key for k in spec.keys], ["j"])

    def test_dock_keys_are_reserved(self):
        _, errors = parse_defaults({"user": {"com.apple.dock": {"persistent-apps": []}}})
        self.assertIn("managed by the dock section", errors[0])

    def test_dock_keys_are_reserved_via_path_domains(self):
        prefs = os.path.join(os.path.expanduser("~"), "Library", "Preferences")
        for scope, domain in (
            ("user", "~/Library/Preferences/com.apple.dock.plist"),
            ("user", os.path.join(prefs, "com.apple.dock")),
            ("user", "~/Library/Preferences/ByHost/com.apple.dock.0A1B2C3D-0000-1111.plist"),
            ("currentHost", "com.apple.dock"),
            ("currentHost", "~/Library/Preferences/ByHost/com.apple.dock.0A1B2C3D-4E5F.plist"),
        ):
            spec, errors = parse_defaults({scope: {domain: {"persistent-others": []}}})
            self.assertEqual(spec.keys, [], domain)
            self.assertIn("managed by the dock section", errors[0], domain)

    def test_lookalike_dock_domains_are_allowed(self):
        for scope, domain in (
            ("user", "com.apple.dock.extra"),
            ("user", "/tmp/Library/Preferences/com.apple.dock"),
            ("system", "/Library/Preferences/com.apple.dock"),
        ):
            _, errors = parse_defaults({scope: {domain: {"persistent-apps": []}}})
            self.assertEqual(errors, [], domain)

    def test_invalid_restart_holds_all_keys(self):
        for restart in ({"com.apple.dock": 3}, "Dock", {"": "Dock"}):
            spec, errors = parse_defaults(
                {"user": {"com.apple.dock": {"tilesize": 64}}, "restart": restart}
            )
            self.assertTrue(errors, restart)
            self.assertEqual(spec.keys, [], restart)

    def test_falsy_non_mappings_are_errors(self):
        for raw in (False, [], "", 0):
            self.assertTrue(parse_defaults(raw)[1], raw)
        for bad in ([], False, "", 0):
            for key in ("user", "currentHost", "system", "restart"):
                spec, errors = parse_defaults(
                    {"user": {"com.example.app": {"k": 1}}, key: bad}
                    if key != "user"
                    else {"currentHost": {"com.example.app": {"k": 1}}, key: bad}
                )
                self.assertTrue(errors, (key, bad))
                self.assertEqual(spec.keys, [], (key, bad))

    def test_null_scopes_are_absent(self):
        spec, errors = parse_defaults({"user": None, "restart": None, "system": {"/L/x": {"k": 1}}})
        self.assertEqual(errors, [])
        self.assertEqual(len(spec.keys), 1)

    def test_none_is_empty(self):
        self.assertEqual(parse_defaults(None)[0].keys, [])


class DefaultsTestBase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDefaults()
        self.state = {}
        self.root = False
        home = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home)
        os.makedirs(os.path.join(home, "Library", "Preferences", "ByHost"))
        patcher = mock.patch.dict(os.environ, {"HOME": home})
        patcher.start()
        self.addCleanup(patcher.stop)

    def deploy(self, raw):
        spec, errors = parse_defaults(raw)
        return defaults.apply_defaults(spec, errors, self.state, self.fake.cli(self.root))

    def diff(self, raw, **kwargs):
        spec, errors = parse_defaults(raw)
        return defaults.preview_defaults(
            spec, errors, self.state, self.fake.cli(self.root, **kwargs)
        )

    def managed(self):
        return self.state["defaults"]["managed"]


class WriteTest(DefaultsTestBase):
    def test_typed_values_round_trip_and_are_idempotent(self):
        raw = {
            "user": {
                "com.example.app": {
                    "b": True,
                    "i": 3,
                    "f": 1000.0,
                    "s": "RegularAutomatic",
                    "a": [{"InputSourceKind": "Keyboard Layout", "KeyboardLayout ID": -2354}],
                    "d": {"NSUserKeyEquivalents": {"Move focus": "~`"}},
                }
            }
        }
        self.assertTrue(self.deploy(raw))
        stored = self.fake.domain("com.example.app")
        self.assertIs(stored["b"], True)
        self.assertIsInstance(stored["f"], float)
        self.assertIsInstance(stored["i"], int)
        self.assertEqual(stored["d"], {"NSUserKeyEquivalents": {"Move focus": "~`"}})

        self.fake.calls.clear()
        self.assertEqual(self.diff(raw), [])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.fake.writes(), [])

    def test_value_is_written_as_xml_plist(self):
        self.deploy({"user": {"com.example.app": {"b": False}}})
        write = next(c for c in self.fake.calls if c[1] == "write")
        self.assertEqual(write[:4], ["/usr/bin/defaults", "write", "com.example.app", "b"])
        self.assertIn("<false/>", write[4])

    def test_type_mismatch_rewrites(self):
        self.fake.domain("com.apple.dock")["tilesize"] = 64.0
        raw = {"user": {"com.apple.dock": {"tilesize": 64}}}

        self.assertEqual(self.diff(raw), ["  ~ write com.apple.dock tilesize: 64.0 -> 64"])
        self.deploy(raw)
        self.assertIsInstance(self.fake.domain("com.apple.dock")["tilesize"], int)

    def test_bool_vs_int_rewrites(self):
        self.fake.domain("com.example.app")["k"] = 1
        self.assertEqual(
            self.diff({"user": {"com.example.app": {"k": True}}}),
            ["  ~ write com.example.app k: 1 -> true"],
        )

    def test_equal_unmanaged_key_is_not_written_or_tracked(self):
        self.fake.domain("NSGlobalDomain")["KeyRepeat"] = 120
        self.assertTrue(self.deploy({"user": {"NSGlobalDomain": {"KeyRepeat": 120}}}))
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.managed(), {})

    def test_current_host_uses_flag(self):
        raw = {"currentHost": {"com.apple.controlcenter": {"Weather": 2}}}
        self.assertEqual(
            self.diff(raw), ["  + write currentHost:com.apple.controlcenter Weather = 2"]
        )
        self.deploy(raw)
        write = next(c for c in self.fake.calls if "write" in c)
        self.assertEqual(write[:3], ["/usr/bin/defaults", "-currentHost", "write"])
        self.assertEqual(self.fake.domain("com.apple.controlcenter", "currentHost"), {"Weather": 2})
        self.assertEqual(self.fake.domain("com.apple.controlcenter"), {})

    def test_byhost_path_matches_nix_darwin(self):
        path = os.path.expanduser("~/Library/Preferences/ByHost/com.apple.controlcenter")
        self.deploy({"user": {"~/Library/Preferences/ByHost/com.apple.controlcenter": {"W": 2}}})
        write = next(c for c in self.fake.calls if "write" in c)
        self.assertEqual(write[:4], ["/usr/bin/defaults", "write", path, "W"])


class PathDomainCaseTest(DefaultsTestBase):
    def setUp(self):
        super().setUp()
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        os.makedirs(os.path.join(self.tmp, "Real"))
        patcher = mock.patch.object(fsutil, "_case_insensitive", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def written_domains(self):
        return [c[2] for c in self.fake.calls if c[1] == "write"]

    def test_new_domain_keeps_declared_case(self):
        domain = os.path.join(self.tmp, "New", "Prefs", "com.Example.App")
        raw = {"user": {domain + ".plist": {"k": 1}}}
        self.assertEqual(self.diff(raw), [f"  + write {domain} k = 1"])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.written_domains(), [domain])
        ident = defaults.domain_identity(domain)
        self.assertEqual(ident, domain.casefold())
        self.assertEqual(list(self.managed()["user"]), [ident])
        self.assertEqual(self.managed()["user"][ident]["k"]["target"], domain)
        self.fake.calls.clear()
        self.assertEqual(self.diff(raw), [])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.fake.writes(), [])

    def test_new_domain_under_existing_dir_keeps_declared_case(self):
        domain = os.path.join(self.tmp, "Real", "Sub", "com.Example.App")
        self.assertTrue(self.deploy({"user": {domain: {"k": 1}}}))
        self.assertEqual(self.written_domains(), [domain])

    def test_case_variants_of_a_new_domain_are_one_domain(self):
        a = os.path.join(self.tmp, "New", "com.Example.App")
        b = os.path.join(self.tmp, "new", "COM.example.app.plist")
        raw = {"user": {a: {"k": 1}, b: {"k": 2}}}
        self.assertFalse(self.deploy(raw))
        self.assertEqual(self.fake.writes(), [])
        self.assertIn("declared more than once", "\n".join(self.diff(raw)))

    def test_respelled_domain_keeps_its_state(self):
        old = os.path.join(self.tmp, "New", "com.Example.App")
        new = os.path.join(self.tmp, "new", "com.example.APP")
        self.fake.domain(old)["k"] = 0
        self.deploy({"user": {old: {"k": 1}}})
        self.fake.store[("user", new)] = self.fake.store.pop(("user", old))
        self.fake.calls.clear()
        raw = {"user": {new: {"k": 1}}}
        self.assertEqual(self.diff(raw), [])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(list(self.managed()["user"]), [defaults.domain_identity(old)])
        self.assertEqual(self.managed()["user"][defaults.domain_identity(new)]["k"]["target"], new)
        self.assertTrue(self.deploy({}))
        self.assertEqual(self.fake.writes(), [("write", new, "k")])
        self.assertEqual(self.fake.domain(new), {"k": 0})

    def test_restart_matches_another_spelling(self):
        domain = os.path.join(self.tmp, "New", "com.Example.App")
        raw = {
            "user": {domain: {"k": 1}},
            "restart": {os.path.join(self.tmp, "new", "com.example.app"): "Finder"},
        }
        self.assertTrue(self.deploy(raw))
        self.assertIn(("killall", "Finder"), self.fake.writes())


class SymlinkDomainTest(DefaultsTestBase):
    def test_state_under_resolved_spelling_follows_declared_spelling(self):
        tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        os.makedirs(os.path.join(tmp, "Real"))
        os.symlink(os.path.join(tmp, "Real"), os.path.join(tmp, "link"))
        resolved = os.path.join(tmp, "Real", "com.example.app")
        declared = os.path.join(tmp, "link", "com.example.app")
        self.state = {
            "defaults": {
                "managed": {
                    "user": {
                        resolved: {
                            "k": {
                                "prior": defaults.to_xml(0),
                                "written": defaults.to_xml(1),
                                "confirmed": defaults.to_xml(1),
                            }
                        }
                    }
                }
            }
        }
        self.fake.domain(declared)["k"] = 1
        raw = {"user": {declared: {"k": 1}}}
        self.assertEqual(self.diff(raw), [])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.fake.writes(), [])
        ident = defaults.domain_identity(declared)
        self.assertEqual(ident, defaults.domain_identity(resolved))
        self.assertEqual(list(self.managed()["user"]), [ident])
        self.assertEqual(self.managed()["user"][ident]["k"]["target"], resolved)


class FoldingDefaults(FakeDefaults):
    """`defaults` on a case-insensitive volume: case variants are one plist."""

    def domain(self, domain, scope="user"):
        return self.store.setdefault((scope, domain.casefold()), {})


class AliasDefaults(FakeDefaults):
    """Path domains through a symlinked parent are one plist."""

    def domain(self, domain, scope="user"):
        if os.path.isabs(domain):
            domain = os.path.realpath(domain)
        return self.store.setdefault((scope, domain), {})


class AliasIdentityTest(DefaultsTestBase):
    def setUp(self):
        super().setUp()
        self.fake = AliasDefaults()
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        os.makedirs(os.path.join(self.tmp, "Real"))
        os.symlink(os.path.join(self.tmp, "Real"), os.path.join(self.tmp, "link"))
        self.real = os.path.join(self.tmp, "Real", "com.example.app")
        self.link = os.path.join(self.tmp, "link", "com.example.app")
        self.fake.running |= {"OldProc", "NewProc"}

    def kills(self):
        return [v[1] for v in self.fake.writes() if v[0] == "killall"]

    def test_restart_alias_for_removed_domain_wins_over_recorded(self):
        self.fake.domain(self.real)["k"] = 0
        self.deploy({"user": {self.real: {"k": 1}}, "restart": {self.real: "OldProc"}})
        self.fake.calls.clear()
        raw = {"restart": {self.link: "NewProc"}}
        self.assertEqual(self.diff(raw)[-1], "  ~ restart NewProc")
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.kills(), ["NewProc"])

    def test_restart_alias_for_removed_domain_is_saved(self):
        self.fake.domain(self.real)["k"] = 0
        self.deploy({"user": {self.real: {"k": 1}}, "restart": {self.real: "OldProc"}})
        self.fake.fail["write"] = "boom"
        self.assertFalse(self.deploy({"restart": {self.link: "NewProc"}}))
        self.assertEqual(
            self.state["defaults"]["restart"],
            {"user": {defaults.domain_identity(self.real): ["NewProc"]}},
        )

    def test_two_spellings_of_one_plist_are_a_config_error(self):
        self.fake.domain(self.real).update(x=0, y=0)
        raw = {"user": {self.real: {"x": 1}, self.link: {"y": 2}}}
        self.assertFalse(self.deploy(raw))
        self.assertEqual(self.fake.writes(), [])
        self.assertIn("declared more than once", "\n".join(self.diff(raw)))

    def test_restart_applies_to_every_spelling_scope_of_a_plist(self):
        raw = {
            "user": {self.real: {"k": 1}},
            "currentHost": {self.link: {"k": 1}},
            "restart": {self.real: "Finder"},
        }
        self.deploy(raw)
        self.fake.domain(self.link, "currentHost")["k"] = 5
        self.fake.calls.clear()
        self.assertEqual(self.diff(raw)[-1], "  ~ restart Finder")
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.fake.writes(), [("write", self.link, "k"), ("killall", "Finder")])


class HomeNameCaseTest(DefaultsTestBase):
    def setUp(self):
        super().setUp()
        self.fake = FoldingDefaults()
        home = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home)
        os.makedirs(os.path.join(home, "Library", "Preferences"))
        for patcher in (
            mock.patch.dict(os.environ, {"HOME": home}),
            mock.patch.object(fsutil, "_case_insensitive", return_value=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def respelled(self, old, new):
        self.fake.domain(defaults.normalize_domain(old))["k"] = 0
        self.assertTrue(self.deploy({"user": {old: {"k": 1}}}))
        self.fake.calls.clear()
        raw = {"user": {new: {"k": 1}}}
        self.assertEqual(self.diff(raw), [])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.fake.writes(), [])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.fake.domain(defaults.normalize_domain(new)), {"k": 1})
        self.assertTrue(self.deploy({}))
        self.assertEqual(self.fake.domain(defaults.normalize_domain(new)), {"k": 0})

    def test_bare_name_case_change_keeps_state(self):
        self.respelled("com.example.App", "com.example.app")

    def test_home_path_case_change_keeps_state(self):
        self.respelled(
            "~/Library/Preferences/com.example.app", "~/Library/Preferences/COM.EXAMPLE.APP"
        )

    def test_case_variants_of_a_bare_name_are_one_domain(self):
        _, errors = defaults.identify(
            *parse_defaults({"user": {"com.example.App": {"k": 1}, "com.example.app": {"j": 1}}})
        )
        self.assertIn("declared more than once", "\n".join(errors))


class GlobalAliasDefaults(FakeDefaults):
    """`.GlobalPreferences` in any case is NSGlobalDomain's plist."""

    def domain(self, domain, scope="user"):
        if domain.casefold() == ".globalpreferences":
            domain = "NSGlobalDomain"
        return self.store.setdefault((scope, domain), {})


class HomePrefsTestBase(DefaultsTestBase):
    def make_home(self, prefs_is_link=False):
        root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        home = os.path.join(root, "home")
        os.makedirs(os.path.join(home, "Library"))
        prefs = os.path.join(home, "Library", "Preferences")
        if prefs_is_link:
            os.makedirs(os.path.join(root, "real"))
            os.symlink(os.path.join(root, "real"), prefs)
        else:
            os.makedirs(prefs)
        patcher = mock.patch.dict(os.environ, {"HOME": home})
        patcher.start()
        self.addCleanup(patcher.stop)
        return root

    def tracked(self, domain, prior=0, value=1):
        self.state = {
            "defaults": {
                "managed": {
                    "user": {
                        domain: {
                            "k": {
                                "prior": defaults.to_xml(prior),
                                "written": defaults.to_xml(value),
                                "confirmed": defaults.to_xml(value),
                            }
                        }
                    }
                }
            }
        }


class SymlinkedPrefsTest(HomePrefsTestBase):
    def test_bare_name_matches_state_under_resolved_prefs(self):
        root = self.make_home(prefs_is_link=True)
        resolved = os.path.join(root, "real", "com.foo")
        self.tracked(resolved)
        self.fake.domain("com.foo")["k"] = 1
        raw = {"user": {"com.foo": {"k": 1}}}
        self.assertEqual(self.diff(raw), [])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(list(self.managed()["user"]), ["com.foo"])

    def test_bare_name_and_resolved_path_are_one_plist(self):
        root = self.make_home(prefs_is_link=True)
        raw = {"user": {"com.foo": {"a": 1}, os.path.join(root, "real", "com.foo"): {"b": 1}}}
        _, errors = defaults.identify(*parse_defaults(raw))
        self.assertIn("declared more than once", "\n".join(errors))


class GlobalPreferencesCaseTest(HomePrefsTestBase):
    def setUp(self):
        super().setUp()
        self.fake = GlobalAliasDefaults()
        self.make_home()
        patcher = mock.patch.object(fsutil, "_case_insensitive", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_case_variant_matches_global_domain_state(self):
        for spelling in ("~/Library/Preferences/.globalpreferences.plist", ".GLOBALPREFERENCES"):
            self.fake = GlobalAliasDefaults()
            self.tracked("NSGlobalDomain")
            self.fake.domain("NSGlobalDomain")["k"] = 1
            raw = {"user": {spelling: {"k": 1}}}
            self.assertEqual(self.diff(raw), [], spelling)
            self.assertTrue(self.deploy(raw), spelling)
            self.assertEqual(self.fake.writes(), [], spelling)
            self.assertEqual(self.fake.domain("NSGlobalDomain"), {"k": 1}, spelling)

    def test_case_variant_and_global_domain_are_one_plist(self):
        raw = {"user": {"-g": {"a": 1}, "~/Library/Preferences/.GLOBALPREFERENCES": {"b": 1}}}
        _, errors = defaults.identify(*parse_defaults(raw))
        self.assertIn("declared more than once", "\n".join(errors))


class RepointedSymlinkTest(DefaultsTestBase):
    def setUp(self):
        super().setUp()
        self.fake = AliasDefaults()
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        for d in ("A", "B"):
            os.makedirs(os.path.join(self.tmp, d))
        self.link = os.path.join(self.tmp, "link")
        os.symlink(os.path.join(self.tmp, "A"), self.link)
        self.a = os.path.join(self.tmp, "A", "x")
        self.b = os.path.join(self.tmp, "B", "x")
        self.declared = os.path.join(self.link, "x")

    def repoint(self):
        os.unlink(self.link)
        os.symlink(os.path.join(self.tmp, "B"), self.link)

    def test_repointed_link_is_a_new_plist(self):
        self.fake.domain(self.a)["k"] = 10
        raw = {"user": {self.declared: {"k": 1}}}
        self.assertTrue(self.deploy(raw))
        self.repoint()
        self.fake.domain(self.b)["k"] = 20

        self.assertEqual(
            self.diff(raw),
            [f"  ~ write {self.declared} k: 20 -> 1", f"  - restore {self.a} k -> 10"],
        )
        self.fake.calls.clear()
        self.assertTrue(self.deploy(raw))
        self.assertEqual(
            self.fake.writes(), [("write", self.declared, "k"), ("write", self.a, "k")]
        )
        self.assertEqual(self.fake.domain(self.a), {"k": 10})
        self.assertEqual(self.fake.domain(self.b), {"k": 1})
        self.assertEqual(list(self.managed()["user"]), [defaults.domain_identity(self.b)])

        self.assertTrue(self.deploy({}))
        self.assertEqual(self.fake.domain(self.b), {"k": 20})
        self.assertEqual(self.fake.domain(self.a), {"k": 10})
        self.assertEqual(self.managed(), {})

    def test_repointed_link_without_prior_deletes_old_plist_key(self):
        raw = {"user": {self.declared: {"k": 1}}}
        self.assertTrue(self.deploy(raw))
        self.repoint()
        self.assertEqual(
            self.diff(raw), [f"  + write {self.declared} k = 1", f"  - delete {self.a} k"]
        )
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.fake.domain(self.a), {})
        self.assertEqual(self.fake.domain(self.b), {"k": 1})


class ScopeIdentityTest(DefaultsTestBase):
    def setUp(self):
        super().setUp()
        self.fake = AliasDefaults()
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.link = os.path.join(self.tmp, "prefs")
        os.symlink(defaults._home_prefs(), self.link)

    def test_current_host_bare_name_and_prefs_path_are_different_plists(self):
        path = os.path.join(self.link, "com.foo")
        raw = {"currentHost": {"com.foo": {"k": 1}, path: {"k": 2}}}
        spec, errors = defaults.identify(*parse_defaults(raw))
        self.assertEqual(errors, [])
        self.assertNotEqual(spec.keys[0].identity, spec.keys[1].identity)
        self.assertTrue(self.deploy(raw))
        self.assertEqual(len(self.managed()["currentHost"]), 2)
        self.fake.calls.clear()
        self.assertTrue(self.deploy({"currentHost": {"com.foo": {"k": 1}}}))
        resolved = os.path.join(defaults._home_prefs(), "com.foo")
        self.assertEqual(self.fake.writes(), [("delete", resolved, "k")])
        self.assertEqual(self.fake.domain(path, "currentHost"), {})
        self.assertEqual(self.fake.domain("com.foo", "currentHost"), {"k": 1})

    def test_user_bare_name_and_prefs_path_are_one_plist(self):
        raw = {"user": {"com.foo": {"k": 1}, os.path.join(self.link, "com.foo"): {"j": 2}}}
        _, errors = defaults.identify(*parse_defaults(raw))
        self.assertIn("declared more than once", "\n".join(errors))

    def test_current_host_byhost_path_and_bare_name_are_one_plist(self):
        byhost = os.path.join(self.link, "ByHost", "com.foo")
        raw = {"currentHost": {"com.foo": {"k": 1}, byhost: {"j": 2}}}
        _, errors = defaults.identify(*parse_defaults(raw))
        self.assertIn("declared more than once", "\n".join(errors))


class LegacyStateTest(DefaultsTestBase):
    def test_state_keyed_by_spelling_with_flat_restart_map_migrates(self):
        self.state = {
            "defaults": {
                "managed": {
                    "user": {
                        "com.apple.dock": {
                            "tilesize": {
                                "prior": defaults.to_xml(48),
                                "written": defaults.to_xml(64),
                                "confirmed": defaults.to_xml(64),
                            }
                        }
                    }
                },
                "restart": {"com.apple.dock": ["Dock"]},
            }
        }
        self.fake.domain("com.apple.dock")["tilesize"] = 64
        self.assertEqual(
            self.diff({}), ["  - restore com.apple.dock tilesize -> 48", "  ~ restart Dock"]
        )
        self.assertTrue(self.deploy({}))
        self.assertEqual(self.fake.domain("com.apple.dock"), {"tilesize": 48})
        self.assertIn(("killall", "Dock"), self.fake.writes())
        self.assertEqual(self.managed(), {})
        self.assertEqual(self.state["defaults"]["restart"], {})

    def test_legacy_key_still_declared_is_kept(self):
        self.state = {
            "defaults": {
                "managed": {
                    "user": {
                        "com.apple.dock": {
                            "tilesize": {
                                "prior": defaults.to_xml(48),
                                "written": defaults.to_xml(64),
                                "confirmed": defaults.to_xml(64),
                            }
                        }
                    }
                },
                "restart": {"com.apple.dock": ["Dock"]},
            }
        }
        self.fake.domain("com.apple.dock")["tilesize"] = 64
        raw = {"user": {"com.apple.dock": {"tilesize": 64}}}
        self.assertEqual(self.diff(raw), [])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.fake.writes(), [])
        entry = self.managed()["user"]["com.apple.dock"]["tilesize"]
        self.assertEqual(defaults.from_xml(entry["prior"]), 48)
        self.assertEqual(entry["target"], "com.apple.dock")


class SystemScopeTest(DefaultsTestBase):
    DOMAIN = "/Library/Preferences/com.example.app"
    RAW = {"system": {DOMAIN: {"Mode": "Automatic"}}}

    def test_root_writes_system_keys_directly(self):
        self.root = True
        self.assertEqual(
            self.diff(self.RAW), [f'  + write system:{self.DOMAIN} Mode = "Automatic"']
        )
        self.assertTrue(self.deploy(self.RAW))
        write = next(c for c in self.fake.calls if "write" in c)
        self.assertEqual(write[0], "/usr/bin/defaults")
        self.assertEqual(self.fake.domain(self.DOMAIN, "system"), {"Mode": "Automatic"})
        self.assertEqual([list(keys) for keys in self.managed()["system"].values()], [["Mode"]])

    def test_user_previews_system_keys_but_never_writes_them(self):
        raw = {**self.RAW, "user": {"com.example.app": {"k": 1}}}
        with (
            mock.patch.object(defaults, "log") as log,
            mock.patch.object(defaults.sys, "platform", "darwin"),
        ):
            self.assertEqual(self.diff(raw), ["  + write com.example.app k = 1"])
            self.assertEqual(self.diff(self.RAW), [])
            self.assertTrue(defaults.install_defaults(raw, self.state, self.fake.cli()))
        notes = [c.args[0] for c in log.call_args_list]
        self.assertIn(
            "defaults: 1 system key(s) differ; run sudo tools deploy --scope defaults", notes
        )
        self.assertIn(
            "defaults: 1 system key(s) not managed as user; run sudo tools deploy --scope defaults",
            notes,
        )
        self.assertEqual(self.fake.domain(self.DOMAIN, "system"), {})
        self.assertEqual(list(self.managed()), ["user"])

    def test_root_never_writes_user_preferences(self):
        self.root = True
        raw = {**self.RAW, "user": {"com.example.app": {"k": 1}}}
        self.assertEqual(self.diff(raw), [f'  + write system:{self.DOMAIN} Mode = "Automatic"'])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.fake.domain("com.example.app"), {})
        self.assertEqual(list(self.managed()), ["system"])
        with self.assertRaises(defaults.DefaultsError):
            self.fake.cli(True).write("user", "com.example.app", "k", 1)
        with self.assertRaises(defaults.DefaultsError):
            self.fake.cli(False).write("system", self.DOMAIN, "k", 1)
        self.assertFalse(any("write" in c and "com.example.app" in c for c in self.fake.calls))

    def test_root_refuses_system_domains_under_the_users_home(self):
        self.root = True
        home = os.path.expanduser("~")
        link = home + "-link"
        os.symlink(home, link)
        self.addCleanup(os.unlink, link)
        inside = [
            os.path.join(home, "Prefs", "evil"),
            os.path.join(home.upper(), "Prefs", "evil"),
            os.path.join(link, "evil"),
        ]
        for domain in inside:
            with self.subTest(domain=domain):
                self.fake.calls.clear()
                self.state = {}
                raw = {"system": {domain: {"k": 1}, self.DOMAIN: {"Mode": "Automatic"}}}
                self.assertIn(f"  ! system:{domain} {defaults.HOME_ERROR}", self.diff(raw))
                self.assertFalse(self.deploy(raw))
                self.assertFalse(any(domain in c for c in self.fake.calls))
                self.assertEqual(self.fake.domain(self.DOMAIN, "system"), {"Mode": "Automatic"})
        with self.assertRaises(defaults.DefaultsError):
            self.fake.cli(True).write("system", inside[1], "k", 1)

    def test_root_refuses_a_plist_symlinked_into_the_home(self):
        self.root = True
        shared = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, shared)
        victim = os.path.join(os.path.expanduser("~"), "victim.plist")
        open(victim, "w").close()
        domain = os.path.join(shared, "foo")
        os.symlink(victim, domain + ".plist")
        raw = {"system": {domain: {"k": 1}, self.DOMAIN: {"Mode": "Automatic"}}}
        self.assertIn(f"  ! system:{domain} {defaults.HOME_ERROR}", self.diff(raw))
        self.assertFalse(self.deploy(raw))
        self.assertFalse(any(domain in c for c in self.fake.calls))
        self.assertEqual(self.fake.domain(self.DOMAIN, "system"), {"Mode": "Automatic"})
        for call in (
            lambda cli: cli.write("system", domain, "k", 1),
            lambda cli: cli.delete("system", domain, "k"),
        ):
            with self.assertRaises(defaults.DefaultsError):
                call(self.fake.cli(True))

    def root_view(self, writable=(), user_owned=()):
        """stat/lstat as on a real host: root-only except the given paths."""

        def view(real):
            def st(path):
                r = real(path)
                mode = r.st_mode | 0o002 if path in writable else r.st_mode & ~0o022
                return SimpleNamespace(st_uid=1000 if path in user_owned else 0, st_mode=mode)

            return st

        return {"stat": view(os.stat), "lstat": view(os.lstat)}

    def deploy_with(self, raw, **kwargs):
        spec, errors = parse_defaults(raw)
        return defaults.apply_defaults(spec, errors, self.state, self.fake.cli(True, **kwargs))

    def shared_dir(self):
        shared = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, shared)
        return shared

    def assert_refused(self, domain, why, **kwargs):
        self.fake.calls.clear()
        self.state = {}
        raw = {"system": {domain: {"k": 1}, self.DOMAIN: {"Mode": "Automatic"}}}
        self.assertIn(f"  ! system:{domain} {why}", self.diff(raw, **kwargs))
        self.assertFalse(self.deploy_with(raw, **kwargs))
        self.assertFalse(any(domain in c for c in self.fake.calls))
        self.assertEqual(self.fake.domain(self.DOMAIN, "system"), {"Mode": "Automatic"})
        for call in (
            lambda cli: cli.write("system", domain, "k", 1),
            lambda cli: cli.delete("system", domain, "k"),
        ):
            with self.assertRaisesRegex(defaults.DefaultsError, "symlink|not root-only"):
                call(self.fake.cli(True, **kwargs))

    def test_root_refuses_system_domains_in_a_user_owned_dir(self):
        self.root = True
        shared = self.shared_dir()
        self.assert_refused(
            os.path.join(shared, "foo"),
            f"{shared} is not root-only (root-owned, not group/world-writable)",
        )
        self.assert_refused(
            os.path.join(shared, "missing", "foo"),
            f"{shared} is not root-only (root-owned, not group/world-writable)",
        )

    def test_root_refuses_system_domains_in_a_world_writable_dir(self):
        self.root = True
        shared = self.shared_dir()
        sticky = os.path.join(shared, "tmp")
        os.makedirs(sticky)
        domain = os.path.join(sticky, "foo")
        self.assert_refused(
            domain,
            f"{sticky} is not root-only (root-owned, not group/world-writable)",
            **self.root_view(writable={sticky}),
        )

    def test_root_refuses_a_symlinked_plist_outside_the_home(self):
        self.root = True
        shared = self.shared_dir()
        target = os.path.join(shared, "target.plist")
        open(target, "w").close()
        domain = os.path.join(shared, "foo")
        os.symlink(target, domain + ".plist")
        self.assert_refused(domain, f"{domain}.plist is a symlink", **self.root_view())

    def test_root_writes_system_domains_in_root_only_dirs(self):
        self.root = True
        domain = os.path.join(self.shared_dir(), "foo")
        self.assertTrue(self.deploy_with({"system": {domain: {"k": 1}}}, **self.root_view()))
        self.assertIn(("write", domain, "k"), self.fake.writes())

    def test_tracked_domain_whose_dir_turned_user_owned_is_not_restored(self):
        self.root = True
        shared = self.shared_dir()
        domain = os.path.join(shared, "foo")
        self.assertTrue(self.deploy_with({"system": {domain: {"k": 1}}}, **self.root_view()))
        self.fake.calls.clear()
        view = self.root_view(user_owned={shared})
        self.assertFalse(self.deploy_with({}, **view))
        self.assertFalse(any("delete" in c for c in self.fake.calls))
        [keys] = self.managed()["system"].values()
        self.assertIn("k", keys)

    def test_root_never_restores_a_tracked_domain_under_the_home(self):
        self.root = True
        domain = os.path.join(os.path.expanduser("~"), "evil")
        entry = {"prior": None, "written": defaults.to_xml(1), "target": domain}
        self.state = {"defaults": {"managed": {"system": {domain: {"k": entry}}}}}
        self.assertIn(f"  ! system:{domain} {defaults.HOME_ERROR}", self.diff({}))
        self.assertFalse(self.deploy({}))
        self.assertFalse(any("delete" in c for c in self.fake.calls))
        self.assertIn("k", self.managed()["system"][domain])

    def test_state_from_the_other_kind_of_run_is_never_restored(self):
        self.root = True
        self.deploy(self.RAW)
        self.root = False
        self.assertEqual(self.diff({}), [])
        self.assertTrue(self.deploy({}))
        self.assertEqual(len(self.managed()["system"]), 1)
        self.assertEqual(self.fake.domain(self.DOMAIN, "system"), {"Mode": "Automatic"})

    def test_unreadable_plist_is_reported(self):
        with mock.patch.object(defaults, "log") as log:
            changes = self.diff(self.RAW, exists=lambda p: True, readable=lambda p: False)
        self.assertEqual(changes, [])
        self.assertIn("is not readable", log.call_args_list[0].args[0])
        self.root = True
        changes = self.diff(self.RAW, exists=lambda p: True, readable=lambda p: False)
        self.assertEqual(len(changes), 1)
        self.assertIn("is not readable", changes[0])
        spec, errors = parse_defaults(self.RAW)
        cli = self.fake.cli(True, exists=lambda p: True, readable=lambda p: False)
        self.assertFalse(defaults.apply_defaults(spec, errors, self.state, cli))
        self.assertFalse(any("export" in c for c in self.fake.calls))


class RestoreTest(DefaultsTestBase):
    RAW = {"user": {"com.apple.dock": {"tilesize": 64}}}

    def test_prior_value_restored_on_removal(self):
        self.fake.domain("com.apple.dock")["tilesize"] = 48
        self.deploy(self.RAW)
        self.assertEqual(self.fake.domain("com.apple.dock")["tilesize"], 64)
        self.assertIn(
            "<integer>48</integer>", self.managed()["user"]["com.apple.dock"]["tilesize"]["prior"]
        )

        self.assertEqual(self.diff({}), ["  - restore com.apple.dock tilesize -> 48"])
        self.assertTrue(self.deploy({}))
        self.assertEqual(self.fake.domain("com.apple.dock")["tilesize"], 48)
        self.assertEqual(self.managed(), {})

    def test_absent_prior_deletes_key_on_removal(self):
        self.deploy({**self.RAW, "restart": {"com.apple.dock": "Dock"}})
        self.assertIsNone(self.managed()["user"]["com.apple.dock"]["tilesize"]["prior"])

        self.assertEqual(self.diff({}), ["  - delete com.apple.dock tilesize", "  ~ restart Dock"])
        self.fake.calls.clear()
        self.assertTrue(self.deploy({}))
        self.assertEqual(
            self.fake.writes(), [("delete", "com.apple.dock", "tilesize"), ("killall", "Dock")]
        )
        self.assertNotIn("tilesize", self.fake.domain("com.apple.dock"))
        self.assertEqual(self.managed(), {})

    def test_absent_prior_hand_changed_is_left(self):
        self.deploy(self.RAW)
        self.fake.domain("com.apple.dock")["tilesize"] = 99
        self.assertEqual(
            self.diff({}), ["  ~ forget com.apple.dock tilesize (changed by hand; left as is)"]
        )
        self.fake.calls.clear()
        self.assertTrue(self.deploy({}))
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.fake.domain("com.apple.dock")["tilesize"], 99)
        self.assertEqual(self.managed(), {})

    def test_absent_prior_delete_failure_is_retried(self):
        self.deploy(self.RAW)
        self.fake.fail["delete"] = "boom"
        self.assertFalse(self.deploy({}))
        self.assertIn("tilesize", self.managed()["user"]["com.apple.dock"])
        del self.fake.fail["delete"]
        self.assertTrue(self.deploy({}))
        self.assertNotIn("tilesize", self.fake.domain("com.apple.dock"))
        self.assertEqual(self.managed(), {})

    def test_absent_prior_current_host_and_system_delete(self):
        self.deploy({"currentHost": {"com.example.app": {"k": 1}}})
        self.root = True
        self.deploy({"system": {"/Library/Preferences/com.example.sys": {"k": 1}}})
        self.assertEqual(
            self.diff({}), ["  - delete system:/Library/Preferences/com.example.sys k"]
        )
        self.root = False
        self.assertEqual(self.diff({}), ["  - delete currentHost:com.example.app k"])
        self.fake.calls.clear()
        self.assertTrue(self.deploy({}))
        self.root = True
        self.assertTrue(self.deploy({}))
        self.assertIn(
            ["/usr/bin/defaults", "-currentHost", "delete", "com.example.app", "k"],
            self.fake.calls,
        )
        self.assertIn(
            ["/usr/bin/defaults", "delete", "/Library/Preferences/com.example.sys", "k"],
            self.fake.calls,
        )
        self.assertEqual(self.fake.domain("com.example.app", "currentHost"), {})
        self.assertEqual(self.fake.domain("/Library/Preferences/com.example.sys", "system"), {})

    def test_diff_shows_every_drift_in_declared_keys(self):
        self.fake.domain("com.apple.dock").update(tilesize=48, autohide=False)
        self.deploy({"user": {"com.apple.dock": {"tilesize": 64, "autohide": True, "new": 1}}})
        self.fake.domain("com.apple.dock")["autohide"] = False
        raw = {"user": {"com.apple.dock": {"autohide": True, "extra": "x"}}}
        self.assertEqual(
            self.diff(raw),
            [
                "  ~ write com.apple.dock autohide: false -> true",
                '  + write com.apple.dock extra = "x"',
                "  - restore com.apple.dock tilesize -> 48",
                "  - delete com.apple.dock new",
            ],
        )

    def test_prior_kept_across_value_changes(self):
        self.fake.domain("com.apple.dock")["tilesize"] = 48
        self.deploy(self.RAW)
        self.deploy({"user": {"com.apple.dock": {"tilesize": 32}}})
        self.deploy({})
        self.assertEqual(self.fake.domain("com.apple.dock")["tilesize"], 48)

    def test_hand_changed_value_is_released_not_restored(self):
        self.fake.domain("com.apple.dock")["tilesize"] = 48
        self.deploy(self.RAW)
        self.fake.domain("com.apple.dock")["tilesize"] = 99

        self.assertIn("changed by hand; left as is", self.diff({})[0])
        self.deploy({})
        self.assertEqual(self.fake.domain("com.apple.dock")["tilesize"], 99)
        self.assertEqual(self.managed(), {})

    def test_key_deleted_by_hand_is_forgotten(self):
        self.fake.domain("com.apple.dock")["tilesize"] = 48
        self.deploy(self.RAW)
        del self.fake.domain("com.apple.dock")["tilesize"]
        self.fake.calls.clear()
        self.assertTrue(self.deploy({}))
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.managed(), {})

    def test_untracked_keys_are_never_touched(self):
        self.fake.domain("com.apple.dock")["autohide"] = True
        self.deploy(self.RAW)
        self.deploy({})
        self.assertIs(self.fake.domain("com.apple.dock")["autohide"], True)

    def test_removals_held_while_config_has_errors(self):
        self.deploy(self.RAW)
        bad = {"user": {"com.apple.dock": {"other": [None]}}}
        self.assertFalse(self.deploy(bad))
        self.assertEqual(self.fake.domain("com.apple.dock")["tilesize"], 64)
        self.assertIn("tilesize", self.managed()["user"]["com.apple.dock"])

    def test_falsy_invalid_scope_holds_removals(self):
        self.deploy(self.RAW)
        for raw in ({"user": []}, {"user": False}, False, [], {"restart": []}):
            self.fake.calls.clear()
            self.assertFalse(self.deploy(raw), raw)
            self.assertEqual(self.fake.writes(), [], raw)
            self.assertEqual(self.fake.domain("com.apple.dock")["tilesize"], 64)
            self.assertIn("tilesize", self.managed()["user"]["com.apple.dock"])

    def test_every_error_kind_holds_whole_section(self):
        dock_raw = {
            "user": {"com.apple.dock": {"tilesize": 64}},
            "restart": {"com.apple.dock": "Dock"},
        }
        change = {"com.apple.finder": {"ShowPathbar": True}}
        cases = {
            "unknown key": {"user": change, "restrat": {"com.apple.finder": "Finder"}},
            "unknown key case": {"user": change, "Restart": {}},
            "wrong scope type": {"user": change, "system": []},
            "nested null": {"user": {**change, "com.example.app": {"k": [None]}}},
            "control char": {"user": {**change, "com.example.app": {"k": "a\x1b"}}},
            "bad key": {"user": {**change, "com.example.app": {1: "x"}}},
            "relative system domain": {"user": change, "system": {"x": {"k": 1}}},
            "reserved dock key": {"user": {**change, "com.apple.dock": {"persistent-apps": []}}},
            "bad restart value": {"user": change, "restart": {"com.apple.finder": 1}},
            "restart not mapping": {"user": change, "restart": "Finder"},
        }
        for name, raw in cases.items():
            self.fake = FakeDefaults()
            self.fake.domain("com.apple.dock")["tilesize"] = 48
            self.state = {}
            self.deploy(dock_raw)
            self.fake.calls.clear()

            self.assertFalse(self.deploy(raw), name)
            self.assertEqual(self.fake.writes(), [], name)
            self.assertTrue(all(c.startswith("  !") for c in self.diff(raw)), name)
            self.assertEqual(self.fake.domain("com.apple.dock")["tilesize"], 64, name)
            self.assertNotIn("ShowPathbar", self.fake.domain("com.apple.finder"), name)

    def test_restored_prior_can_be_complex(self):
        prior = [{"k": b"\x00\x01", "n": 1.5}]
        self.fake.domain("com.example.app")["arr"] = prior
        self.deploy({"user": {"com.example.app": {"arr": []}}})
        json.dumps(self.state)  # state stays JSON-serializable
        self.deploy({})
        self.assertEqual(self.fake.domain("com.example.app")["arr"], prior)


class RestartTest(DefaultsTestBase):
    RAW = {"user": {"com.apple.dock": {"tilesize": 64}}, "restart": {"com.apple.dock": "Dock"}}

    def test_restart_only_when_changed(self):
        self.assertEqual(self.diff(self.RAW)[-1], "  ~ restart Dock")
        self.deploy(self.RAW)
        self.assertIn(("killall", "Dock"), self.fake.writes())

        self.fake.calls.clear()
        self.assertEqual(self.diff(self.RAW), [])
        self.deploy(self.RAW)
        self.assertEqual(self.fake.writes(), [])

    def test_restart_once_per_process(self):
        raw = {
            "user": {"com.apple.dock": {"a": 1, "b": 2}, "com.apple.finder": {"c": 3}},
            "restart": {"com.apple.dock": "Dock", "com.apple.finder": ["Finder", "Dock"]},
        }
        self.deploy(raw)
        kills = [v for v in self.fake.writes() if v[0] == "killall"]
        self.assertEqual(kills, [("killall", "Dock"), ("killall", "Finder")])

    def test_restore_restarts_from_recorded_map(self):
        self.fake.domain("com.apple.dock")["tilesize"] = 48
        self.deploy(self.RAW)
        self.fake.calls.clear()
        self.deploy({})
        self.assertIn(("killall", "Dock"), self.fake.writes())
        self.assertEqual(self.state["defaults"]["restart"], {})

    def test_path_restart_keys_are_normalized(self):
        raw = {
            "user": {"com.example.app": {"k": 1}},
            "system": {"/Library/Preferences/com.example.sys": {"k": 1}},
            "restart": {
                "~/Library/Preferences/com.example.app.plist": "Finder",
                "/Library/Preferences/com.example.sys.plist": "Dock",
            },
        }
        spec, errors = parse_defaults(raw)
        self.assertEqual(errors, [])
        self.assertEqual(
            spec.restart,
            {"com.example.app": ["Finder"], "/Library/Preferences/com.example.sys": ["Dock"]},
        )
        self.deploy(raw)
        self.root = True
        self.deploy(raw)
        kills = [v for v in self.fake.writes() if v[0] == "killall"]
        self.assertEqual(kills, [("killall", "Finder"), ("killall", "Dock")])

    def test_restart_entries_for_one_domain_merge(self):
        raw = {
            "user": {"com.example.app": {"k": 1}},
            "restart": {
                "com.example.app": ["Finder", "Dock"],
                "~/Library/Preferences/com.example.app.plist": ["SystemUIServer", "Finder"],
            },
        }
        spec, errors = parse_defaults(raw)
        self.assertEqual(errors, [])
        self.assertEqual(spec.restart, {"com.example.app": ["Finder", "Dock", "SystemUIServer"]})
        self.fake.running.add("SystemUIServer")
        self.deploy(raw)
        kills = [v[1] for v in self.fake.writes() if v[0] == "killall"]
        self.assertEqual(kills, ["Finder", "Dock", "SystemUIServer"])

    def test_invalid_restart_writes_and_kills_nothing(self):
        self.deploy(self.RAW)
        self.fake.calls.clear()
        bad = {"user": {"com.apple.dock": {"tilesize": 32}}, "restart": {"com.apple.dock": 0}}
        self.assertFalse(self.deploy(bad))
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.fake.domain("com.apple.dock")["tilesize"], 64)
        self.assertIn("tilesize", self.managed()["user"]["com.apple.dock"])

    def test_dropping_domain_from_restart_stops_restarting(self):
        self.deploy(self.RAW)
        self.fake.calls.clear()
        raw = {"user": {"com.apple.dock": {"tilesize": 32}}}
        self.assertNotIn("  ~ restart Dock", self.diff(raw))
        self.deploy(raw)
        self.assertNotIn(("killall", "Dock"), self.fake.writes())
        self.assertEqual(self.state["defaults"]["restart"], {})

    def test_partial_restore_in_desired_domain_uses_current_restart(self):
        raw = {"user": {"com.apple.dock": {"tilesize": 64, "autohide": True}}}
        self.deploy({**raw, "restart": {"com.apple.dock": "Dock"}})
        self.fake.calls.clear()
        self.deploy({"user": {"com.apple.dock": {"tilesize": 64}}})
        self.assertNotIn(("killall", "Dock"), self.fake.writes())

    def test_current_restart_entry_wins_for_removed_domain(self):
        self.fake.domain("com.apple.dock")["tilesize"] = 48
        self.deploy(self.RAW)
        self.fake.calls.clear()
        raw = {"restart": {"com.apple.dock": "Finder"}}
        self.assertEqual(self.diff(raw)[-1], "  ~ restart Finder")
        self.deploy(raw)
        kills = [v for v in self.fake.writes() if v[0] == "killall"]
        self.assertEqual(kills, [("killall", "Finder")])

    def test_current_restart_entry_is_saved_for_removed_domain(self):
        self.fake.domain("com.apple.dock")["tilesize"] = 48
        self.deploy(self.RAW)
        self.fake.fail["write"] = "boom"
        self.assertFalse(self.deploy({"restart": {"com.apple.dock": "Finder"}}))
        self.assertEqual(
            self.state["defaults"]["restart"], {"user": {"com.apple.dock": ["Finder"]}}
        )

    def test_process_not_running_is_a_warning(self):
        self.fake.running = set()
        self.assertTrue(self.deploy(self.RAW))

    def test_user_mode_restart_is_plain_killall(self):
        self.fake.cli().restart("Dock")
        self.assertEqual(self.fake.calls, [["/usr/bin/killall", "Dock"]])

    def test_root_restarts_only_the_invoking_users_process(self):
        self.fake.cli(True, login="me").restart("Dock")
        self.assertEqual(self.fake.calls, [["/usr/bin/killall", "-u", "me", "Dock"]])
        self.fake.calls.clear()
        warning = self.fake.cli(True, login=None).restart("Dock")
        self.assertIn("no invoking user", warning)
        self.assertEqual(self.fake.calls, [])

    def test_root_deploy_restart_is_scoped_to_the_user(self):
        self.root = True
        raw = {"system": {"/Library/Preferences/com.example.sys": {"k": 1}},
               "restart": {"/Library/Preferences/com.example.sys": "Dock"}}  # fmt: skip
        self.assertTrue(self.deploy(raw))
        kills = [c for c in self.fake.calls if c[0] == "/usr/bin/killall"]
        self.assertEqual(kills, [["/usr/bin/killall", "-u", "me", "Dock"]])


class FailureTest(DefaultsTestBase):
    def test_write_failure_drops_fresh_entry(self):
        self.fake.fail["write"] = "boom"
        self.assertFalse(self.deploy({"user": {"com.example.app": {"k": 1}}}))
        self.assertEqual(self.managed(), {})

    def test_export_failure_skips_domain_only(self):
        orig = self.fake.run

        def run(cmd, env=None, cwd=None):
            if "export" in cmd and "com.broken" in cmd:
                return 1, "", "cfprefsd unavailable"
            return orig(cmd, env, cwd)

        self.fake.run = run
        raw = {"user": {"com.broken": {"k": 1}, "com.example.app": {"k": 2}}}
        self.assertFalse(self.deploy(raw))
        self.assertEqual(self.fake.domain("com.example.app"), {"k": 2})
        self.assertIn("? cannot read user:com.broken", self.diff(raw)[0])

    RAW = {"user": {"com.apple.dock": {"tilesize": 64}}, "restart": {"com.apple.dock": "Dock"}}

    def slot(self):
        return self.managed()["user"]["com.apple.dock"]["tilesize"]

    def crash_on_write(self, lands, exc=None):
        self.fake.domain("com.apple.dock")["tilesize"] = 32
        undo = self.fake.raise_on("write", lands, exc or OSError("timed out"))
        self.assertFalse(self.deploy(self.RAW))
        undo()
        self.assertEqual(defaults.from_xml(self.slot()["written"]), 64)
        self.fake.calls.clear()

    def test_written_is_persisted_before_the_write(self):
        self.fake.domain("com.apple.dock")["tilesize"] = 32
        seen = []
        spec, errors = parse_defaults(self.RAW)
        defaults.apply_defaults(
            spec,
            errors,
            self.state,
            self.fake.cli(),
            persist=lambda: seen.append((json.dumps(self.state), list(self.fake.calls))),
        )
        snapshot, calls = seen[0]
        self.assertIn("<integer>64</integer>", snapshot)
        self.assertFalse(any("write" in c for c in calls))

    def test_crash_before_write_lands_forgets_on_removal(self):
        self.crash_on_write(lands=False)
        self.assertTrue(self.deploy({}))
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.fake.domain("com.apple.dock"), {"tilesize": 32})
        self.assertEqual(self.managed(), {})

    def test_crash_after_write_landed_restores_on_removal(self):
        self.crash_on_write(lands=True)
        self.assertEqual(self.diff({})[0], "  - restore com.apple.dock tilesize -> 32")
        self.assertTrue(self.deploy({}))
        self.assertEqual(self.fake.domain("com.apple.dock"), {"tilesize": 32})
        self.assertEqual(self.managed(), {})

    def test_crash_queues_restart_since_write_may_have_landed(self):
        self.fake.domain("com.apple.dock")["tilesize"] = 32
        self.fake.raise_on("write", lands=False)
        self.assertFalse(self.deploy(self.RAW))
        self.assertIn(("killall", "Dock"), self.fake.writes())

    def test_defaults_error_reverts_written(self):
        self.fake.domain("com.apple.dock")["tilesize"] = 32
        self.deploy(self.RAW)
        self.fake.fail["write"] = "boom"
        self.fake.calls.clear()
        self.assertFalse(self.deploy({**self.RAW, "user": {"com.apple.dock": {"tilesize": 128}}}))
        self.assertNotIn(("killall", "Dock"), self.fake.writes())
        self.assertEqual(defaults.from_xml(self.slot()["written"]), 64)

    def test_restore_raises_then_retries(self):
        self.fake.domain("com.apple.dock")["tilesize"] = 32
        self.deploy(self.RAW)
        undo = self.fake.raise_on("write", lands=False)
        self.fake.calls.clear()
        self.assertFalse(self.deploy({"restart": self.RAW["restart"]}))
        undo()
        self.assertIn(("killall", "Dock"), self.fake.writes())
        self.assertEqual(defaults.from_xml(self.slot()["written"]), 64)
        self.assertTrue(self.deploy({}))
        self.assertEqual(self.fake.domain("com.apple.dock"), {"tilesize": 32})
        self.assertEqual(self.managed(), {})

    def test_interrupt_propagates_and_keeps_written(self):
        self.fake.domain("com.apple.dock")["tilesize"] = 32
        self.fake.raise_on("write", lands=True, exc=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.deploy(self.RAW)
        self.assertEqual(defaults.from_xml(self.slot()["written"]), 64)
        self.assertIn(("killall", "Dock"), self.fake.writes())

    def test_crash_on_change_from_own_value_still_restores(self):
        self.fake.domain("com.apple.dock")["tilesize"] = 32
        self.deploy(self.RAW)
        undo = self.fake.raise_on("write", lands=False)
        self.assertFalse(self.deploy({**self.RAW, "user": {"com.apple.dock": {"tilesize": 80}}}))
        undo()
        self.assertEqual(defaults.from_xml(self.slot()["written"]), 80)
        self.assertEqual(defaults.from_xml(self.slot()["confirmed"]), 64)
        self.assertTrue(self.deploy({}))
        self.assertEqual(self.fake.domain("com.apple.dock"), {"tilesize": 32})
        self.assertEqual(self.managed(), {})

    def test_double_crash_still_restores_prior(self):
        self.crash_on_write(lands=True)
        undo = self.fake.raise_on("write", lands=False)
        self.assertFalse(self.deploy({**self.RAW, "user": {"com.apple.dock": {"tilesize": 80}}}))
        undo()
        self.assertEqual(defaults.from_xml(self.slot()["confirmed"]), 64)
        self.assertTrue(self.deploy({}))
        self.assertEqual(self.fake.domain("com.apple.dock"), {"tilesize": 32})
        self.assertEqual(self.managed(), {})

    def test_landed_crash_is_confirmed_on_next_deploy(self):
        self.crash_on_write(lands=True)
        self.assertTrue(self.deploy(self.RAW))
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.slot()["confirmed"], self.slot()["written"])

    def test_readding_key_keeps_original_prior(self):
        self.crash_on_write(lands=True)
        self.fake.domain("com.apple.dock")["tilesize"] = 99
        self.assertTrue(self.deploy({"user": {"com.apple.dock": {"tilesize": 50}}}))
        self.assertEqual(defaults.from_xml(self.slot()["prior"]), 32)

    def test_garbage_export_is_an_error(self):
        self.fake.run = lambda cmd, env=None, cwd=None: (0, "not a plist", "")
        self.assertFalse(self.deploy({"user": {"d": {"k": 1}}}))


class AdapterTest(unittest.TestCase):
    def test_non_darwin_skips(self):
        with mock.patch.object(defaults.sys, "platform", "linux"):
            self.assertTrue(defaults.install_defaults({"user": {"d": {"k": 1}}}, {}))
            self.assertEqual(defaults.diff_defaults({"user": {"d": {"k": 1}}}, {}), [])

    def test_missing_binary(self):
        with (
            mock.patch.object(defaults.sys, "platform", "darwin"),
            mock.patch.object(defaults, "system_bin_optional", return_value=None),
        ):
            self.assertFalse(defaults.install_defaults({"user": {"d": {"k": 1}}}, {}))
            self.assertIn("not found", defaults.diff_defaults({"user": {"d": {"k": 1}}}, {})[0])

    def test_resource_api_needs_no_config_dict(self):
        fake = FakeDefaults()
        spec = defaults.DefaultsSpec(keys=[DefaultsKey("user", "com.example.app", "k", 1)])
        self.assertTrue(defaults.apply_defaults(spec, [], {}, fake.cli()))
        self.assertEqual(fake.domain("com.example.app"), {"k": 1})


class CliGatingTest(unittest.TestCase):
    def _deploy(self, config, state):
        with (
            mock.patch.object(cli_mod, "load_json", return_value=state),
            mock.patch.object(cli_mod, "migrate_state_file"),
            mock.patch.object(cli_mod, "save_json"),
            mock.patch.object(cli_mod, "state_lock"),
            mock.patch.object(cli_mod, "install_defaults", return_value=True) as d,
            mock.patch.object(cli_mod, "install_dock", return_value=True) as k,
        ):
            cli_mod._deploy(config, "/tmp")
        return d, k

    def test_falsy_sections_still_reach_validation(self):
        d, k = self._deploy({"defaults": False, "dock": []}, {})
        self.assertIs(d.call_args.args[0], False)
        self.assertEqual(k.call_args.args[0], [])

    def test_absent_sections_are_noops(self):
        d, k = self._deploy({}, {})
        d.assert_not_called()
        k.assert_not_called()

    def test_run_when_only_state_remains(self):
        state = {
            "defaults": {"managed": {"user": {"d": {"k": {"prior": None, "written": "x"}}}}},
            "dock": {"sections": {"persistent-apps": {"prior": None, "written": []}}},
        }
        d, k = self._deploy({}, state)
        d.assert_called_once()
        k.assert_called_once()
        self.assertTrue(callable(d.call_args.kwargs["persist"]))
        self.assertTrue(callable(k.call_args.kwargs["persist"]))

    def test_diff_with_tmp_state_file(self):
        from tools.diff import show_diff

        fake = FakeDefaults()
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "stateFile": os.path.join(tmp, "state.json"),
                "defaults": {"user": {"com.example.app": {"k": 1}}},
            }
            with (
                mock.patch.object(defaults.sys, "platform", "darwin"),
                mock.patch.object(defaults.DefaultsCLI, "for_host", return_value=fake.cli()),
            ):
                self.assertFalse(show_diff(config, tmp, ("defaults",)))
            self.assertFalse(os.path.exists(config["stateFile"]))
        self.assertEqual(fake.writes(), [])


if __name__ == "__main__":
    unittest.main()
