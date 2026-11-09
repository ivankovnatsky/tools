import fcntl
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from tools import cli
from tools import state as state_mod
from tools.state import (
    STATE_VERSION,
    find_state_file,
    load_json,
    lock_path,
    migrate_state_schema,
    save_json,
    state_lock,
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
            self.assertEqual(os.listdir(os.path.dirname(path)), ["state.json"])

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
            self.assertEqual(os.listdir(d), ["state.json"])

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


class StateLockTest(unittest.TestCase):
    def test_concurrent_deploys_keep_each_others_records(self):
        with tempfile.TemporaryDirectory() as d:
            state_file = os.path.join(d, "state.json")
            save_json(state_file, {"version": STATE_VERSION})
            first_inside = threading.Event()

            def section(config, _dir, state, _paths, _active, persist, *_a):
                state.setdefault("records", []).append(config["name"])
                if config["name"] == "a":
                    first_inside.set()
                    # Without the lock, b reads the state now and drops "a".
                    time.sleep(0.3)
                persist()
                return True

            def run(name):
                cli._deploy({"stateFile": state_file, "name": name}, d)

            with (
                mock.patch.object(cli, "_run_sections", side_effect=section),
                mock.patch.object(cli, "migrate_state_file"),
                mock.patch.object(state_mod, "log"),
            ):
                a = threading.Thread(target=run, args=("a",))
                a.start()
                self.assertTrue(first_inside.wait(5))
                b = threading.Thread(target=run, args=("b",))
                b.start()
                a.join(5)
                b.join(5)

            self.assertEqual(sorted(load_json(state_file)["records"]), ["a", "b"])

    def test_waits_with_message_instead_of_failing(self):
        with tempfile.TemporaryDirectory() as d:
            state_file = os.path.join(d, "state.json")
            holder = os.open(lock_path(state_file), os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(holder, fcntl.LOCK_EX)
            threading.Timer(0.2, os.close, args=(holder,)).start()
            with mock.patch.object(state_mod, "log") as log:
                with state_lock(state_file):
                    pass
            self.assertIn("Waiting for another tools run", log.call_args[0][0])

    def test_lock_file_is_private(self):
        with tempfile.TemporaryDirectory() as d:
            state_file = os.path.join(d, "sub", "state.json")
            with state_lock(state_file):
                pass
            self.assertEqual(os.stat(lock_path(state_file)).st_mode & 0o777, 0o600)

    def test_lock_released_on_exception(self):
        with tempfile.TemporaryDirectory() as d:
            state_file = os.path.join(d, "state.json")
            with self.assertRaises(RuntimeError):
                with state_lock(state_file):
                    raise RuntimeError("boom")
            fd = os.open(lock_path(state_file), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)

    def test_deploy_releases_lock_when_a_section_crashes(self):
        with tempfile.TemporaryDirectory() as d:
            state_file = os.path.join(d, "state.json")
            with (
                mock.patch.object(cli, "_run_sections", side_effect=RuntimeError("boom")),
                mock.patch.object(cli, "migrate_state_file"),
            ):
                with self.assertRaises(RuntimeError):
                    cli._deploy({"stateFile": state_file}, d)
            fd = os.open(lock_path(state_file), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)

    def test_save_json_temp_files_are_unique(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            seen = []
            real_replace = os.replace

            def replace(src, dst):
                seen.append(src)
                self.assertEqual(os.stat(src).st_mode & 0o777, 0o600)
                real_replace(src, dst)

            with mock.patch.object(state_mod.os, "replace", side_effect=replace):
                save_json(path, {"a": 1})
                save_json(path, {"a": 2})

            self.assertEqual(len(set(seen)), 2)
            for tmp in seen:
                self.assertEqual(os.path.dirname(tmp), d)
                self.assertTrue(os.path.basename(tmp).startswith("state.json."))
            self.assertEqual(os.listdir(d), ["state.json"])


if __name__ == "__main__":
    unittest.main()
