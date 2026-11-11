import os
import tempfile
import unittest
from unittest import mock

from tools import config as config_mod
from tools.config import (
    SECTIONS,
    TOP_LEVEL_KEYS,
    ConfigError,
    deep_merge,
    load_config,
    load_config_dir,
)
from tools.diff import ALL_SECTIONS


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


class DeepMergeTest(unittest.TestCase):
    def test_lists_replace_and_dicts_recurse(self):
        base = {"a": {"x": 1, "y": 2}, "l": [1, 2]}
        overlay = {"a": {"y": 3}, "l": [9]}

        self.assertEqual(deep_merge(base, overlay), {"a": {"x": 1, "y": 3}, "l": [9]})


class EmptyDesiredStateTest(unittest.TestCase):
    """An empty config now means 'remove everything tracked', so every path
    that could produce one by accident has to fail loudly instead."""

    def test_dir_without_machines_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ConfigError):
                load_config_dir(d)

    def test_missing_machine_config_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "machines", "other-host.json"), "{}")

            with mock.patch("tools.config._get_hostname", return_value="thishost"):
                with self.assertRaises(ConfigError):
                    load_config_dir(d)

    def test_present_machine_config_loads(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "machines", "thishost.json"), '{"npm": {"packages": {}}}')

            with mock.patch("tools.config._get_hostname", return_value="thishost"):
                loaded = load_config_dir(d)

            self.assertIn("npm", loaded)

    def test_root_files_beside_machines_are_not_read(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "machines", "thishost.json"), '{"npm": {"packages": {}}}')
            _write(os.path.join(d, "treefmt.toml"), '[formatter.nix]\ncommand = "nixfmt"\n')

            with mock.patch("tools.config._get_hostname", return_value="thishost"):
                loaded = load_config_dir(d)

            self.assertEqual(loaded, {"npm": {"packages": {}}})

    def test_dir_files_without_machines_are_not_read(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "a.json"), '{"npm": {"packages": {}}}')

            with self.assertRaisesRegex(ConfigError, "machines/"):
                load_config_dir(d)


class PinHostnameTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, config_mod, "_PINNED_HOSTNAME", None)

    def test_pinned_machine_survives_a_hostname_change(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "machines", "old.json"), '{"npm": {"packages": {}}}')
            _write(os.path.join(d, "machines", "new.json"), '{"uv": {"packages": {}}}')
            with mock.patch.object(config_mod.socket, "gethostname", return_value="old.local"):
                self.assertEqual(config_mod.pin_hostname(), "old")
            with mock.patch.object(config_mod.socket, "gethostname", return_value="new"):
                self.assertIn("npm", load_config_dir(d))


class IncludeTest(unittest.TestCase):
    def test_includes_are_merged_under_the_including_file(self):
        with tempfile.TemporaryDirectory() as d:
            _write(
                os.path.join(d, "base.json"),
                '{"npm": {"packages": {"a": {}}}, "stateFile": "/base"}',
            )
            _write(os.path.join(d, "top.json"), '{"include": ["base.json"], "stateFile": "/top"}')

            loaded = load_config(os.path.join(d, "top.json"))

            self.assertEqual(loaded["stateFile"], "/top")
            self.assertIn("a", loaded["npm"]["packages"])

    def test_missing_include_raises(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "top.json"), '{"include": ["nope.json"]}')

            with self.assertRaises(FileNotFoundError):
                load_config(os.path.join(d, "top.json"))

    def test_circular_include_raises(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "a.json"), '{"include": ["b.json"]}')
            _write(os.path.join(d, "b.json"), '{"include": ["a.json"]}')

            with self.assertRaises(ValueError):
                load_config(os.path.join(d, "a.json"))


class TopLevelKeysTest(unittest.TestCase):
    def test_every_section_and_setting_is_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.json")
            keys = sorted(TOP_LEVEL_KEYS - {"include"})
            _write(path, "{" + ", ".join(f'"{k}": null' for k in keys) + "}")

            self.assertEqual(sorted(load_config(path)), keys)

    def test_allowed_keys_follow_the_section_registry(self):
        self.assertIs(ALL_SECTIONS, SECTIONS)
        self.assertLessEqual(set(SECTIONS), TOP_LEVEL_KEYS)

    def test_unknown_key_names_key_file_and_valid_keys(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.yaml")
            _write(path, "npm: {}\nbogus: 1\n")

            with self.assertRaises(ConfigError) as cm:
                load_config(path)

        msg = str(cm.exception)
        self.assertIn("'bogus'", msg)
        self.assertIn(path, msg)
        self.assertIn("valid keys: ", msg)
        self.assertIn("mgmt", msg)
        self.assertIn("stateFile", msg)

    def test_renamed_services_key_hints_mgmt(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.yaml")
            _write(path, "services: {}\n")

            with self.assertRaisesRegex(ConfigError, "use 'mgmt' instead of 'services'"):
                load_config(path)

    def test_unknown_key_in_included_file_names_that_file(self):
        with tempfile.TemporaryDirectory() as d:
            inc = os.path.join(d, "shared", "base.yaml")
            _write(inc, "services: {}\n")
            _write(os.path.join(d, "machines", "h.yaml"), "include: [../shared/base.yaml]\n")

            with mock.patch("tools.config._get_hostname", return_value="h"):
                with self.assertRaises(ConfigError) as cm:
                    load_config_dir(d)

        self.assertIn(inc, str(cm.exception))

    def test_unknown_key_in_host_file_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "machines", "h.json"), '{"npm": {}, "variables": {}}')

            with mock.patch("tools.config._get_hostname", return_value="h"):
                with self.assertRaisesRegex(ConfigError, "'variables'"):
                    load_config_dir(d)

    def test_cli_exits_on_unknown_key(self):
        from tools import cli

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.yaml")
            _write(path, "services: {}\n")

            with mock.patch.object(cli, "log") as log, self.assertRaises(SystemExit) as cm:
                cli._load_merged_config([path])

        self.assertEqual(cm.exception.code, 1)
        self.assertIn("'services'", log.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
