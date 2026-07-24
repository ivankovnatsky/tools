import os
import tempfile
import unittest
from unittest import mock

from tools.config import ConfigError, deep_merge, load_config, load_config_dir


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

    def test_empty_flat_dir_is_refused(self):
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

    def test_flat_dir_with_config_loads(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "a.json"), '{"npm": {"packages": {}}}')

            self.assertIn("npm", load_config_dir(d))


class IncludeTest(unittest.TestCase):
    def test_includes_are_merged_under_the_including_file(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "base.json"), '{"npm": {"packages": {"a": {}}}, "x": 1}')
            _write(os.path.join(d, "top.json"), '{"include": ["base.json"], "x": 2}')

            loaded = load_config(os.path.join(d, "top.json"))

            self.assertEqual(loaded["x"], 2)
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


if __name__ == "__main__":
    unittest.main()
