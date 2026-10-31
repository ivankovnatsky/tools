import json
import os
import tempfile
import unittest
from unittest import mock

from fake_defaults import FakeDefaults

from tools.user import dock
from tools.user.dock import FS, parse_dock


class DockTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.mail = self.mk("System/Applications/Mail.app")
        self.safari_real = self.mk("Cryptexes/Applications/Safari.app")
        self.safari = os.path.join(self.tmp, "Applications", "Safari.app")
        os.makedirs(os.path.dirname(self.safari))
        os.symlink(self.safari_real, self.safari)
        self.term = self.mk("Applications/Terminal.app")
        self.downloads = self.mk("home/Downloads")
        self.notes = os.path.join(self.tmp, "home", "notes.txt")
        open(self.notes, "w").close()
        self.fake = FakeDefaults()
        self.state = {}
        self.fs = FS()

    def tearDown(self):
        self._tmp.cleanup()

    def mk(self, rel):
        path = os.path.join(self.tmp, rel)
        os.makedirs(path)
        return path

    def deploy(self, raw):
        spec, errors = parse_dock(raw)
        return dock.apply_dock(spec, errors, self.state, self.fake.cli(), self.fs)

    def diff(self, raw):
        spec, errors = parse_dock(raw)
        return dock.preview_dock(spec, errors, self.state, self.fake.cli(False), self.fs)

    def live(self, key="persistent-apps"):
        return self.fake.domain("com.apple.dock").get(key)

    def dock_rewrites(self, tiles):
        """What Dock does after a restart: resolve paths and add its own bookkeeping."""
        out = []
        for i, tile in enumerate(tiles):
            tile = json.loads(json.dumps(tile))
            fd = tile["tile-data"].get("file-data")
            if fd:
                path = os.path.realpath(dock._tile_path(tile))
                fd["_CFURLString"] = dock._file_url(path, os.path.isdir(path))
                tile["tile-data"]["book"] = b"bookmark"
                tile["tile-data"]["bundle-identifier"] = f"com.example.{i}"
            tile["GUID"] = 1000 + i
            out.append(tile)
        return out


class ParseTest(unittest.TestCase):
    def test_entry_forms(self):
        spec, errors = parse_dock(
            {
                "apps": ["/A.app", "spacer", "small-spacer", {"spacer": "flexible"}],
                "others": [
                    {"path": "~/Downloads", "view": "grid", "display": "folder", "sort": "name"},
                    {"url": "https://example.com", "label": "Example"},
                ],
            }
        )
        self.assertEqual(errors, [])
        apps = spec.sections["persistent-apps"]
        self.assertEqual([i.spacer for i in apps[1:]], ["regular", "small", "flexible"])
        folder = spec.sections["persistent-others"][0]
        self.assertTrue(os.path.isabs(folder.path))
        self.assertEqual((folder.view, folder.display, folder.sort), ("grid", "folder", "name"))

    def test_parse_does_not_touch_the_filesystem(self):
        def boom(*args, **kwargs):
            raise AssertionError("parse touched the filesystem")

        raw = {
            "apps": ["/Applications/Safari.app", "spacer"],
            "others": [{"path": "~/Downloads", "view": "grid"}, {"url": "https://example.com"}],
        }
        with (
            mock.patch.object(os, "listdir", boom),
            mock.patch.object(os, "stat", boom),
            mock.patch.object(os, "lstat", boom),
            mock.patch.object(os.path, "realpath", boom),
            mock.patch.object(os.path, "exists", boom),
            mock.patch.object(os.path, "isdir", boom),
        ):
            spec, errors = parse_dock(raw)
        self.assertEqual(errors, [])
        self.assertEqual(spec.sections["persistent-apps"][0].path, "/Applications/Safari.app")

    def test_errors(self):
        _, errors = parse_dock(
            {
                "apps": [
                    "relative.app",
                    "/Users/x/Downloads",
                    {"path": "/A.app", "view": "grid"},
                    {"url": "https://example.com"},
                    {"spacer": "huge"},
                    42,
                ],
                "others": [{"path": "/x", "sort": "random"}, {"path": "/x", "bogus": 1}],
                "restart": "yes",
                "extra": [],
            }
        )
        text = "\n".join(errors)
        for needle in (
            "absolute",
            "only .app bundles",
            "only applies to folders",
            "URLs belong in others",
            "spacer must be one of",
            "must be a path",
            "sort must be one of",
            "unknown keys ['bogus']",
            "restart must be",
            "unknown keys ['extra']",
        ):
            self.assertIn(needle, text)

    def test_non_string_options_are_config_errors(self):
        for entry in (
            {"path": "/x", "view": ["grid"]},
            {"path": "/x", "sort": {"a": 1}},
            {"spacer": []},
            {"url": "https://e.com", "label": "a\x1b"},
            "/Users/x/bad\x1bdir",
        ):
            spec, errors = parse_dock({"others": [entry]})
            self.assertTrue(errors, entry)
            self.assertEqual(spec.sections, {}, entry)

    def test_null_section_is_unmanaged(self):
        spec, errors = parse_dock({"apps": None, "others": []})
        self.assertEqual(errors, [])
        self.assertEqual(spec.sections, {"persistent-others": []})

    def test_any_error_empties_spec(self):
        spec, errors = parse_dock({"apps": ["/A.app", "typo"], "others": []})
        self.assertEqual(len(errors), 1)
        self.assertEqual(spec.sections, {})

    def test_undeclared_sections_are_unmanaged(self):
        spec, _ = parse_dock({"apps": []})
        self.assertEqual(list(spec.sections), ["persistent-apps"])


class LayoutTest(DockTestBase):
    def test_writes_ordered_tiles_and_restarts_once(self):
        raw = {
            "apps": [self.mail, "spacer", self.safari, {"spacer": "small"}, self.term],
            "others": [{"path": self.downloads, "view": "grid", "sort": "dateadded"}, self.notes],
            "restart": True,
        }
        changes = self.diff(raw)
        self.assertIn("  ~ rewrite persistent-apps (5 items)", changes)
        self.assertIn(f"      + {self.mail}", changes)
        self.assertEqual(changes[-1], "  ~ restart Dock")

        self.assertTrue(self.deploy(raw))
        apps = self.live()
        self.assertEqual(
            [t["tile-type"] for t in apps],
            ["file-tile", "spacer-tile", "file-tile", "small-spacer-tile", "file-tile"],
        )
        self.assertEqual(apps[0]["tile-data"]["file-data"]["_CFURLStringType"], 0)
        self.assertEqual(apps[0]["tile-data"]["file-data"]["_CFURLString"], self.mail)
        self.assertEqual(apps[0]["tile-data"]["file-type"], 41)
        others = self.live("persistent-others")
        self.assertEqual(others[0]["tile-type"], "directory-tile")
        self.assertEqual(others[0]["tile-data"]["showas"], 2)
        self.assertEqual(others[0]["tile-data"]["arrangement"], 2)
        self.assertEqual(others[0]["tile-data"]["displayas"], 1)
        self.assertEqual(others[1]["tile-type"], "file-tile")
        self.assertEqual(
            [v for v in self.fake.writes() if v[0] == "killall"], [("killall", "Dock")]
        )

    def test_dock_bookkeeping_does_not_force_rewrite(self):
        raw = {"apps": [self.safari, "flexible-spacer", self.mail]}
        self.deploy(raw)
        dom = self.fake.domain("com.apple.dock")
        dom["persistent-apps"] = self.dock_rewrites(dom["persistent-apps"])

        self.fake.calls.clear()
        self.assertEqual(self.diff(raw), [])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.fake.writes(), [])

    def test_reorder_is_a_rewrite(self):
        self.deploy({"apps": [self.mail, self.safari]})
        changes = self.diff({"apps": [self.safari, self.mail]})
        self.assertEqual(changes[0], "  ~ rewrite persistent-apps (2 items)")
        self.assertTrue(any(c.startswith("      - ") for c in changes))

    def test_folder_option_change_is_a_rewrite(self):
        self.deploy({"others": [{"path": self.downloads, "view": "grid"}]})
        changes = self.diff({"others": [{"path": self.downloads, "view": "list"}]})
        self.assertEqual(changes[1], f"      ~ {self.downloads} (view=list)")

    def test_unspecified_folder_options_are_not_compared(self):
        self.deploy({"others": [self.downloads]})
        dom = self.fake.domain("com.apple.dock")
        dom["persistent-others"][0]["tile-data"].update(showas=3, arrangement=1, displayas=1)
        self.assertEqual(self.diff({"others": [self.downloads]}), [])

    def test_missing_path_is_skipped_rest_applies(self):
        missing = os.path.join(self.tmp, "Applications", "Gone.app")
        raw = {"apps": [self.mail, missing, self.term]}
        self.assertIn(f"  ! skip {missing} in persistent-apps: not found", self.diff(raw))

        self.assertTrue(self.deploy(raw))
        self.assertEqual(len(self.live()), 2)

        self.fake.calls.clear()
        self.deploy(raw)
        self.assertEqual(self.fake.writes(), [])

        os.makedirs(missing)
        self.deploy(raw)
        self.assertEqual(len(self.live()), 3)

    def test_all_paths_missing_leaves_live_section_untouched(self):
        live = [{"tile-type": "keep"}]
        self.fake.domain("com.apple.dock")["persistent-apps"] = live
        gone = [os.path.join(self.tmp, "Applications", f"Gone{i}.app") for i in range(2)]
        raw = {"apps": [gone[0], "spacer", gone[1]]}

        changes = self.diff(raw)
        self.assertIn("  ! leave persistent-apps untouched: none of its paths exist", changes)
        self.assertNotIn("  ~ restart Dock", changes)
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.live(), live)

    def test_missing_path_keeps_its_live_tile_in_place(self):
        self.deploy({"apps": [self.mail, self.term]})
        live_term = self.live()[1]
        os.rmdir(self.term)
        raw = {"apps": [self.mail, self.term, self.safari]}

        self.assertIn(
            f"  ! keep {self.term} in persistent-apps: not found, live tile stays", self.diff(raw)
        )
        self.assertTrue(self.deploy(raw))
        apps = self.live()
        self.assertEqual(len(apps), 3)
        self.assertEqual(apps[1], live_term)

        self.fake.calls.clear()
        self.deploy(raw)
        self.assertEqual(self.fake.writes(), [])

    def test_matching_untracked_layout_is_left_alone(self):
        self.fake.domain("com.apple.dock")["persistent-apps"] = [
            dock.build_tile(dock.DockItem("path", path=self.mail), self.fs)
        ]
        self.assertTrue(self.deploy({"apps": [self.mail]}))
        self.assertEqual(self.fake.writes(), [])
        self.assertNotIn("dock", self.state)

    def test_unknown_live_tiles_force_rewrite(self):
        self.fake.domain("com.apple.dock")["persistent-apps"] = [{"tile-type": "weird-tile"}]
        self.assertEqual(self.diff({"apps": []})[0], "  ~ rewrite persistent-apps (0 items)")

    def test_folder_options_on_a_file_are_ignored(self):
        raw = {"others": [{"path": self.notes, "view": "grid", "sort": "name"}]}
        self.assertTrue(self.deploy(raw))
        tile = self.live("persistent-others")[0]
        self.assertEqual(tile["tile-type"], "file-tile")
        self.assertNotIn("showas", tile["tile-data"])

        self.fake.calls.clear()
        self.assertEqual(self.diff(raw), [])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.fake.writes(), [])

    def test_root_path_matches_live_tile(self):
        raw = {"others": ["/"]}
        self.deploy(raw)
        self.assertEqual(self.live("persistent-others")[0]["tile-type"], "directory-tile")
        self.fake.calls.clear()
        self.assertEqual(self.diff(raw), [])
        self.deploy(raw)
        self.assertEqual(self.fake.writes(), [])

    def test_url_tile(self):
        raw = {"others": [{"url": "https://example.com", "label": "Example"}]}
        self.deploy(raw)
        tile = self.live("persistent-others")[0]
        self.assertEqual(tile["tile-type"], "url-tile")
        self.assertEqual(self.diff(raw), [])

    def test_bad_entry_does_not_rewrite_live_section(self):
        self.deploy({"apps": [self.mail, self.term]})
        self.fake.calls.clear()
        raw = {"apps": [self.mail, "Termnal.app"]}

        self.assertNotIn("  ~ rewrite persistent-apps (1 items)", self.diff(raw))
        self.assertFalse(self.deploy(raw))
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(len(self.live()), 2)

    def test_null_section_does_not_wipe_untracked_dock(self):
        self.fake.domain("com.apple.dock")["persistent-apps"] = [{"tile-type": "keep"}]
        self.assertEqual(self.diff({"apps": None}), [])
        self.assertTrue(self.deploy({"apps": None}))
        self.assertEqual(self.live(), [{"tile-type": "keep"}])

    def test_explicit_empty_list_empties_section(self):
        self.deploy({"apps": [self.mail]})
        self.deploy({"apps": []})
        self.assertEqual(self.live(), [])

    def test_invalid_restart_holds_whole_dock(self):
        self.deploy({"apps": [self.mail]})
        self.fake.calls.clear()
        for restart in ("false", "no-restart", 0):
            raw = {"apps": [self.safari], "others": [self.downloads], "restart": restart}
            changes = self.diff(raw)
            self.assertIn("restart must be true or false", changes[0])
            self.assertNotIn("  ~ restart Dock", changes)
            self.assertFalse(self.deploy(raw))
            self.assertEqual(self.fake.writes(), [])
        self.assertEqual(len(self.live()), 1)
        self.assertIn("persistent-apps", self.state["dock"]["sections"])

    def test_falsy_invalid_config_holds_restore(self):
        self.deploy({"apps": [self.mail]})
        for raw in ([], False, "", 0):
            self.fake.calls.clear()
            self.assertFalse(self.deploy(raw), raw)
            self.assertEqual(self.fake.writes(), [], raw)
            self.assertIn("persistent-apps", self.state["dock"]["sections"])

    def test_url_label_types(self):
        _, errors = parse_dock({"others": [{"url": "https://example.com", "label": 0}]})
        self.assertIn("label must be a string", errors[0])
        spec, errors = parse_dock({"others": [{"url": "https://example.com", "label": None}]})
        self.assertEqual(errors, [])
        self.assertEqual(spec.sections["persistent-others"][0].label, "")

    def test_every_error_kind_holds_whole_dock(self):
        others = [self.downloads]
        cases = {
            "unknown key": {"others": others, "restrat": False},
            "unknown key plural": {"others": others, "restarts": False},
            "unknown key case": {"others": others, "Restart": False},
            "section not list": {"others": others, "apps": {"x": 1}},
            "bad entry": {"others": others, "apps": ["Termnal.app"]},
            "bad restart": {"others": others, "restart": "false"},
            "bad label": {"others": [*others, {"url": "https://example.com", "label": 0}]},
            "bad option": {"others": [{"path": self.downloads, "view": "tiles"}]},
        }
        for name, raw in cases.items():
            self.fake = FakeDefaults()
            self.state = {}
            self.deploy({"apps": [self.mail]})
            self.fake.calls.clear()

            self.assertFalse(self.deploy(raw), name)
            self.assertEqual(self.fake.writes(), [], name)
            self.assertTrue(all(c.startswith("  !") for c in self.diff(raw)), name)
            self.assertEqual(len(self.live()), 1, name)
            self.assertIsNone(self.live("persistent-others"), name)

    def test_no_restart_by_default(self):
        self.assertNotIn("  ~ restart Dock", self.diff({"apps": [self.mail]}))
        self.deploy({"apps": [self.mail]})
        self.assertEqual([v for v in self.fake.writes() if v[0] == "killall"], [])

    def test_restart_false_wins_when_sections_dropped(self):
        prior = [dock.build_tile(dock.DockItem("path", path=self.term), self.fs)]
        self.fake.domain("com.apple.dock")["persistent-apps"] = prior
        self.deploy({"apps": [self.mail], "restart": True})
        self.fake.calls.clear()
        raw = {"restart": False}
        self.assertNotIn("  ~ restart Dock", self.diff(raw))
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.live(), prior)
        self.assertEqual([v for v in self.fake.writes() if v[0] == "killall"], [])

    def test_folder_defaults_match_dockutil(self):
        self.deploy({"others": [self.downloads]})
        data = self.live("persistent-others")[0]["tile-data"]
        self.assertEqual((data["showas"], data["displayas"], data["arrangement"]), (0, 1, 3))
        self.assertEqual(data["directory"], 1)

    def test_applications_paths_resolve_like_dockutil(self):
        apps = os.path.join(self.tmp, "Applications") + "/"
        cryptex = os.path.join(self.tmp, "Cryptexes") + "/"
        system = os.path.join(self.tmp, "sys")
        os.makedirs(os.path.join(system + apps, "Notes.app"))
        notes = os.path.join(apps, "Notes.app")
        with (
            mock.patch.object(dock, "APPLICATIONS", apps),
            mock.patch.object(dock, "CRYPTEX_APPS", cryptex),
            mock.patch.object(dock, "SYSTEM_ROOT", system),
        ):
            self.assertTrue(self.deploy({"apps": [self.safari, notes]}))
            paths = [t["tile-data"]["file-data"]["_CFURLString"] for t in self.live()]
            self.assertEqual(paths, [self.safari_real, system + notes])
            self.fake.calls.clear()
            self.assertTrue(self.deploy({"apps": [self.safari, notes]}))
            self.assertEqual(self.fake.writes(), [])

    def test_only_declared_section_is_written(self):
        self.fake.domain("com.apple.dock")["persistent-others"] = [{"tile-type": "keep"}]
        self.deploy({"apps": [self.mail]})
        self.assertEqual(self.live("persistent-others"), [{"tile-type": "keep"}])


class RestoreTest(DockTestBase):
    def prior_layout(self):
        tiles = [dock.build_tile(dock.DockItem("path", path=self.term), self.fs)]
        tiles[0]["GUID"] = 7
        self.fake.domain("com.apple.dock")["persistent-apps"] = tiles
        return tiles

    def test_prior_layout_restored_when_section_removed(self):
        prior = self.prior_layout()
        self.deploy({"apps": [self.mail], "restart": True})
        json.dumps(self.state)

        self.assertEqual(self.diff({}), ["  - restore prior persistent-apps", "  ~ restart Dock"])
        self.fake.calls.clear()
        self.assertTrue(self.deploy({}))
        self.assertEqual(self.live(), prior)
        self.assertIn(("killall", "Dock"), self.fake.writes())
        self.assertNotIn("dock", self.state)

    def test_absent_prior_deletes_section(self):
        self.deploy({"apps": [self.mail], "restart": True})
        self.assertEqual(self.diff({}), ["  - delete persistent-apps", "  ~ restart Dock"])
        self.fake.calls.clear()
        self.assertTrue(self.deploy({}))
        self.assertEqual(
            self.fake.writes(),
            [("delete", "com.apple.dock", "persistent-apps"), ("killall", "Dock")],
        )
        self.assertIsNone(self.live())
        self.assertNotIn("dock", self.state)

    def test_absent_prior_rearranged_by_hand_is_left(self):
        self.deploy({"apps": [self.mail, self.term]})
        dom = self.fake.domain("com.apple.dock")
        dom["persistent-apps"] = list(reversed(dom["persistent-apps"]))
        self.assertEqual(
            self.diff({}), ["  ~ forget persistent-apps (rearranged by hand; left as is)"]
        )
        self.fake.calls.clear()
        self.assertTrue(self.deploy({}))
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(len(self.live()), 2)
        self.assertNotIn("dock", self.state)

    def test_absent_prior_delete_failure_is_retried(self):
        self.deploy({"apps": [self.mail]})
        self.fake.fail["delete"] = "boom"
        self.assertFalse(self.deploy({}))
        self.assertIn("persistent-apps", self.state["dock"]["sections"])
        del self.fake.fail["delete"]
        self.assertTrue(self.deploy({}))
        self.assertIsNone(self.live())
        self.assertNotIn("dock", self.state)

    def test_removed_item_is_removed_from_section(self):
        self.deploy({"apps": [self.mail, self.term]})
        self.assertEqual(
            self.diff({"apps": [self.mail]}),
            ["  ~ rewrite persistent-apps (1 items)", f"      - {self.term}"],
        )
        self.deploy({"apps": [self.mail]})
        self.assertEqual([dock._tile_path(t) for t in self.live()], [self.mail])

    def test_hand_added_tile_in_declared_section_shows_in_diff(self):
        raw = {"apps": [self.mail]}
        self.deploy(raw)
        self.live().append(dock.build_tile(dock.DockItem("path", path=self.term), self.fs))
        self.assertEqual(
            self.diff(raw), ["  ~ rewrite persistent-apps (1 items)", f"      - {self.term}"]
        )
        self.deploy(raw)
        self.assertEqual([dock._tile_path(t) for t in self.live()], [self.mail])

    def test_hand_rearranged_layout_is_released(self):
        self.prior_layout()
        self.deploy({"apps": [self.mail, self.term]})
        dom = self.fake.domain("com.apple.dock")
        dom["persistent-apps"] = list(reversed(dom["persistent-apps"]))

        self.assertIn("rearranged by hand; left as is", self.diff({})[0])
        self.fake.calls.clear()
        self.deploy({})
        self.assertEqual(self.fake.writes(), [])
        self.assertNotIn("dock", self.state)

    def test_dock_rewritten_layout_still_counts_as_ours(self):
        prior = self.prior_layout()
        self.deploy({"apps": [self.safari]})
        dom = self.fake.domain("com.apple.dock")
        dom["persistent-apps"] = self.dock_rewrites(dom["persistent-apps"])
        self.deploy({})
        self.assertEqual(self.live(), prior)

    def test_prior_survives_layout_changes(self):
        prior = self.prior_layout()
        self.deploy({"apps": [self.mail]})
        self.deploy({"apps": [self.safari]})
        self.deploy({})
        self.assertEqual(self.live(), prior)

    def test_restore_held_while_config_has_errors(self):
        self.deploy({"apps": [self.mail]})
        self.assertFalse(self.deploy({"others": "nope"}))
        self.assertIn("persistent-apps", self.state["dock"]["sections"])


class FailureTest(DockTestBase):
    def test_read_failure(self):
        self.fake.fail["export"] = "boom"
        self.assertFalse(self.deploy({"apps": [self.mail]}))
        self.assertIn("cannot read Dock layout", self.diff({"apps": [self.mail]})[0])

    def test_write_failure_keeps_no_state_and_skips_restart(self):
        self.fake.fail["write"] = "boom"
        self.assertFalse(self.deploy({"apps": [self.mail]}))
        self.assertNotIn("dock", self.state)
        self.assertFalse(any(c[0] == "/usr/bin/killall" for c in self.fake.calls))

    def prior_layout(self):
        tiles = [dock.build_tile(dock.DockItem("path", path=self.term), self.fs)]
        self.fake.domain("com.apple.dock")["persistent-apps"] = tiles
        return json.loads(json.dumps(tiles))

    def section(self):
        return self.state["dock"]["sections"]["persistent-apps"]

    def recorded(self, field, key="persistent-apps"):
        tiles = dock._recorded(self.state["dock"]["sections"][key][field])
        return [dock._tile_path(t) for t in tiles]

    def recorded_data(self, field, key):
        return [t["tile-data"] for t in dock._recorded(self.state["dock"]["sections"][key][field])]

    def crash_on_write(self, lands, exc=None):
        prior = self.prior_layout()
        undo = self.fake.raise_on("write", lands, exc or OSError("timed out"))
        self.assertFalse(self.deploy({"apps": [self.mail], "restart": True}))
        undo()
        self.assertEqual(self.recorded("written")[0], self.mail)
        self.fake.calls.clear()
        return prior

    def test_crash_before_write_lands_forgets_on_removal(self):
        prior = self.crash_on_write(lands=False)
        self.assertTrue(self.deploy({}))
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.live(), prior)
        self.assertNotIn("dock", self.state)

    def test_crash_after_write_landed_restores_on_removal(self):
        prior = self.crash_on_write(lands=True)
        self.assertIn("  - restore prior persistent-apps", self.diff({}))
        self.assertTrue(self.deploy({}))
        self.assertEqual(self.live(), prior)
        self.assertNotIn("dock", self.state)

    def test_crash_queues_restart(self):
        self.prior_layout()
        self.fake.raise_on("write", lands=False)
        self.assertFalse(self.deploy({"apps": [self.mail], "restart": True}))
        self.assertIn(("killall", "Dock"), self.fake.writes())

    def test_restore_raises_then_retries(self):
        prior = self.prior_layout()
        self.deploy({"apps": [self.mail], "restart": True})
        undo = self.fake.raise_on("write", lands=False)
        self.fake.calls.clear()
        self.assertFalse(self.deploy({}))
        undo()
        self.assertIn(("killall", "Dock"), self.fake.writes())
        self.assertIn("persistent-apps", self.state["dock"]["sections"])
        self.assertTrue(self.deploy({}))
        self.assertEqual(self.live(), prior)
        self.assertNotIn("dock", self.state)

    def test_restore_landed_matches_prior_after_dock_relaunch(self):
        self.prior_layout()
        self.deploy({"apps": [self.mail]})
        undo = self.fake.raise_on("write", lands=True)
        self.assertFalse(self.deploy({}))
        undo()
        dom = self.fake.domain("com.apple.dock")
        dom["persistent-apps"] = self.dock_rewrites(dom["persistent-apps"])
        self.fake.calls.clear()
        self.assertTrue(self.deploy({}))
        self.assertEqual(self.fake.writes(), [])
        self.assertNotIn("dock", self.state)

    def test_folder_option_change_is_restored(self):
        prior = [dock.build_tile(dock.DockItem("path", path=self.downloads), self.fs)]
        self.fake.domain("com.apple.dock")["persistent-others"] = prior
        self.deploy({"others": [{"path": self.downloads, "view": "grid"}]})
        self.assertEqual(self.live("persistent-others")[0]["tile-data"]["showas"], 2)
        self.assertIn("  - restore prior persistent-others", self.diff({}))
        self.assertTrue(self.deploy({}))
        self.assertEqual(self.live("persistent-others")[0]["tile-data"]["showas"], 0)
        self.assertNotIn("dock", self.state)

    def test_dock_ui_option_change_is_left_on_removal(self):
        prior = [dock.build_tile(dock.DockItem("path", path=self.notes), self.fs)]
        self.fake.domain("com.apple.dock")["persistent-others"] = prior
        self.deploy({"others": [self.downloads]})
        data = self.recorded_data("written", "persistent-others")[0]
        self.assertEqual((data["showas"], data["displayas"], data["arrangement"]), (0, 1, 3))
        self.live("persistent-others")[0]["tile-data"]["showas"] = 2
        self.fake.calls.clear()
        self.assertTrue(self.deploy({}))
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.live("persistent-others")[0]["tile-data"]["showas"], 2)
        self.assertNotIn("dock", self.state)

    def test_unset_options_not_enforced_or_adopted(self):
        self.deploy({"others": [self.downloads]})
        self.live("persistent-others")[0]["tile-data"]["showas"] = 2
        self.fake.calls.clear()
        self.assertTrue(self.deploy({"others": [self.downloads]}))
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.recorded_data("written", "persistent-others")[0]["showas"], 0)

    def test_double_crash_still_restores_prior(self):
        prior = self.crash_on_write(lands=True)
        undo = self.fake.raise_on("write", lands=False)
        self.assertFalse(self.deploy({"apps": [self.safari]}))
        undo()
        self.assertEqual(self.recorded("confirmed")[0], self.mail)
        self.assertTrue(self.deploy({}))
        self.assertEqual(self.live(), prior)
        self.assertNotIn("dock", self.state)

    def test_hand_deleted_after_empty_section_is_left(self):
        self.prior_layout()
        self.deploy({"apps": []})
        self.assertEqual(self.live(), [])
        del self.fake.domain("com.apple.dock")["persistent-apps"]
        self.fake.calls.clear()
        self.assertTrue(self.deploy({}))
        self.assertEqual(self.fake.writes(), [])
        self.assertIsNone(self.live())
        self.assertNotIn("dock", self.state)

    def test_hand_renamed_url_tile_is_left(self):
        url = {"url": "https://example.com"}
        prior = [dock.build_tile(dock.DockItem("path", path=self.notes), self.fs)]
        self.fake.domain("com.apple.dock")["persistent-others"] = prior
        self.deploy({"others": [url]})
        self.live("persistent-others")[0]["tile-data"]["label"] = "Renamed"
        self.fake.calls.clear()
        self.assertTrue(self.deploy({"others": [url]}))
        self.assertEqual(self.fake.writes(), [])
        self.assertTrue(self.deploy({}))
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.live("persistent-others")[0]["tile-data"]["label"], "Renamed")

    def test_crash_on_change_from_own_layout_still_restores(self):
        prior = self.prior_layout()
        self.deploy({"apps": [self.mail]})
        undo = self.fake.raise_on("write", lands=False)
        self.assertFalse(self.deploy({"apps": [self.safari]}))
        undo()
        self.assertEqual(self.recorded("written")[0], self.safari)
        self.assertEqual(self.recorded("confirmed")[0], self.mail)
        self.assertTrue(self.deploy({}))
        self.assertEqual(self.live(), prior)
        self.assertNotIn("dock", self.state)

    def test_hand_deleted_layout_is_left_alone(self):
        self.prior_layout()
        self.deploy({"apps": [self.mail]})
        del self.fake.domain("com.apple.dock")["persistent-apps"]
        self.fake.calls.clear()
        self.assertTrue(self.deploy({}))
        self.assertEqual(self.fake.writes(), [])
        self.assertIsNone(self.live())
        self.assertNotIn("dock", self.state)

    def test_interrupt_propagates_and_keeps_written(self):
        self.prior_layout()
        self.fake.raise_on("write", lands=True, exc=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.deploy({"apps": [self.mail], "restart": True})
        self.assertEqual(self.recorded("written")[0], self.mail)
        self.assertIn(("killall", "Dock"), self.fake.writes())


class AdapterTest(unittest.TestCase):
    def test_non_darwin_skips(self):
        with mock.patch.object(dock.sys, "platform", "linux"):
            self.assertTrue(dock.install_dock({"apps": ["/A.app"]}, {}))
            self.assertEqual(dock.diff_dock({"apps": ["/A.app"]}, {}), [])

    def test_has_work(self):
        self.assertFalse(dock.has_work(None, {}))
        self.assertTrue(dock.has_work([], {}))
        self.assertTrue(dock.has_work({}, {"dock": {"sections": {"persistent-apps": {}}}}))


if __name__ == "__main__":
    unittest.main()
