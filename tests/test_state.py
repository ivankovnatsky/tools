import json
import os
import tempfile
import unittest

from tools.state import (
    STATE_VERSION,
    find_state_file,
    load_json,
    migrate_state_schema,
    save_json,
)


class StateSchemaTest(unittest.TestCase):
    def test_legacy_state_is_released_not_reinterpreted(self):
        # Pre-v2 sections recorded the desired config, not what we installed.
        # Treating them as ownership would uninstall hand-installed packages
        # on the first deploy after the upgrade.
        state = {
            "brew": {"brews": ["jq"], "casks": ["kitty"], "taps": ["a/b"], "masApps": {"X": 1}},
            "flatpak": {"packages": ["org.example.App"], "remotes": ["flathub"]},
            "ollamaModels": {"installed": ["llama3:latest"]},
        }

        migrate_state_schema(state)

        self.assertEqual(state["brew"]["brews"], [])
        self.assertEqual(state["brew"]["casks"], [])
        self.assertEqual(state["brew"]["taps"], [])
        self.assertEqual(state["brew"]["masApps"], {})
        self.assertEqual(state["flatpak"]["packages"], [])
        self.assertEqual(state["flatpak"]["remotes"], [])
        self.assertEqual(state["ollamaModels"]["installed"], [])
        self.assertEqual(state["version"], STATE_VERSION)

    def test_current_state_is_untouched(self):
        state = {"version": STATE_VERSION, "flatpak": {"packages": ["a"], "remotes": []}}

        migrate_state_schema(state)

        self.assertEqual(state["flatpak"]["packages"], ["a"])

    def test_newer_state_is_refused(self):
        # Downgrading silently would hand stale semantics deletion authority.
        with self.assertRaises(RuntimeError):
            migrate_state_schema({"version": STATE_VERSION + 1})

    def test_migration_is_idempotent(self):
        state = {"flatpak": {"packages": ["a"], "remotes": []}}
        migrate_state_schema(state)
        state["flatpak"]["packages"] = ["b"]

        migrate_state_schema(state)

        self.assertEqual(state["flatpak"]["packages"], ["b"])

    def test_unrelated_sections_survive_migration(self):
        state = {"npm": {"packages": {"x": {"installed": True}}}}

        migrate_state_schema(state)

        self.assertEqual(state["npm"]["packages"], {"x": {"installed": True}})


class StateWriteTest(unittest.TestCase):
    def test_save_is_atomic_and_leaves_no_temp(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "state.json")

            save_json(path, {"version": STATE_VERSION, "flatpak": {"packages": ["a"]}})

            self.assertEqual(load_json(path)["flatpak"]["packages"], ["a"])
            self.assertFalse(os.path.exists(f"{path}.tmp"))

    def test_failed_write_leaves_previous_state_intact(self):
        # The state file is the only record of what we installed; a crash
        # mid-write must not truncate it.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            save_json(path, {"version": STATE_VERSION, "flatpak": {"packages": ["a"]}})

            class Unserializable:
                pass

            with self.assertRaises(TypeError):
                save_json(path, {"bad": Unserializable()})

            with open(path) as f:
                self.assertEqual(json.load(f)["flatpak"]["packages"], ["a"])

    def test_save_json_accepts_bare_relative_path(self):
        # `stateFile: state.json` has an empty dirname; makedirs("") raised
        # FileNotFoundError.
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            try:
                save_json("state.json", {"version": STATE_VERSION})
                self.assertTrue(os.path.isfile("state.json"))
            finally:
                os.chdir(cwd)

    def test_load_json_is_read_only_for_dir_squatter(self):
        # diff reads state; it must not repair (rmdir) anything on disk.
        with tempfile.TemporaryDirectory() as d:
            squatter = os.path.join(d, "state.json")
            os.makedirs(squatter)

            self.assertEqual(load_json(squatter), {})
            self.assertTrue(os.path.isdir(squatter))

    def test_find_state_file_prefers_existing_current_path(self):
        with tempfile.TemporaryDirectory() as d:
            current = os.path.join(d, "tools", "state.json")
            os.makedirs(os.path.dirname(current))
            with open(current, "w") as f:
                json.dump({}, f)

            self.assertEqual(find_state_file(current), current)

    def test_find_state_file_locates_legacy_sibling_without_copying(self):
        with tempfile.TemporaryDirectory() as d:
            legacy = os.path.join(d, "manual-packages", "state.json")
            os.makedirs(os.path.dirname(legacy))
            with open(legacy, "w") as f:
                json.dump({}, f)
            current = os.path.join(d, "tools", "state.json")

            self.assertEqual(find_state_file(current), legacy)
            # Read-only: nothing was migrated.
            self.assertFalse(os.path.exists(current))


if __name__ == "__main__":
    unittest.main()
