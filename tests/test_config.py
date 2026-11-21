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

    def test_append_lists_extends_only_managed_lists(self):
        base = {"brew": {"brews": ["a", "b"]}, "defaults": {"user": {"d": {"k": [1, 2]}}}}
        overlay = {"brew": {"brews": ["b", "c"]}, "defaults": {"user": {"d": {"k": [3]}}}}

        merged = deep_merge(base, overlay, append_lists=True)

        self.assertEqual(merged["brew"]["brews"], ["a", "b", "c"])
        self.assertEqual(merged["defaults"]["user"]["d"]["k"], [3])

    def test_append_lists_dedupes_identical_mappings(self):
        shared = {"source": "s", "target": "~/t"}
        base = {"files": [shared]}
        overlay = {"files": [dict(shared), {"source": "s", "target": "~/t", "mode": "0600"}]}

        merged = deep_merge(base, overlay, append_lists=True)

        self.assertEqual(merged["files"], [shared, overlay["files"][1]])


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


class DuplicateKeyTest(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.yaml")
            _write(path, text)
            return path, config_mod._load_raw(path)

    def assertRejected(self, text, key, line, first):
        with self.assertRaises(ConfigError) as cm:
            self.load(text)
        msg = str(cm.exception)
        self.assertIn("c.yaml: ", msg)
        self.assertIn(f"duplicate key {key!r} on line {line} (first defined on line {first})", msg)

    def test_top_level_duplicate_rejected(self):
        self.assertRejected("d: 1\nd: 2\n", "d", 2, 1)

    def test_nested_duplicate_rejected(self):
        text = (
            "launchd:\n"
            "  caddy.service:\n"
            "    command: a\n"
            "  other: {command: b}\n"
            "  caddy.service:\n"
            "    command: c\n"
        )
        self.assertRejected(text, "caddy.service", 5, 2)

    def test_duplicate_in_flow_mapping_inside_list_rejected(self):
        self.assertRejected("files:\n  - {source: a, source: b}\n", "source", 2, 2)

    def test_keys_equal_after_resolution_rejected(self):
        self.assertRejected("yes: 1\ntrue: 2\n", True, 2, 1)

    def test_merge_keys_still_override(self):
        text = (
            "vars:\n"
            "  base: &b {a: 1, b: 2}\n"
            "  other: &o {a: 9, c: 3}\n"
            "  one:\n"
            "    <<: *b\n"
            "    a: 3\n"
            "  many:\n"
            "    <<: [*b, *o]\n"
        )
        _path, data = self.load(text)
        self.assertEqual(data["vars"]["one"], {"a": 3, "b": 2})
        self.assertEqual(data["vars"]["many"], {"a": 1, "b": 2, "c": 3})

    def test_nested_merge_reached_through_alias_first_still_loads(self):
        text = "vars:\n  c: &c {a: 0}\n  deep: {deeper: {b: &b {<<: *c, a: 1}}}\n  use: {<<: *b}\n"
        _path, data = self.load(text)
        self.assertEqual(data["vars"]["use"], {"a": 1})

    def test_aliased_key_repeating_its_anchor_rejected(self):
        self.assertRejected("- {&k a: 1, *k : 2}\n", "a", 1, 1)

    def test_duplicate_inside_inline_merge_source_rejected(self):
        self.assertRejected("svc: {<<: {command: a, command: b}}\n", "command", 1, 1)

    def test_value_key_still_loads(self):
        _path, data = self.load("vars: {=: value}\n")
        self.assertEqual(data, {"vars": {"=": "value"}})

    def test_distinct_keys_load(self):
        _path, data = self.load("a: 1\nb: {a: 2}\n'1': x\n1: y\n")
        self.assertEqual(data, {"a": 1, "b": {"a": 2}, "1": "x", 1: "y"})

    def test_load_config_surfaces_duplicate_in_include(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "inc.yaml"), "dock:\n  apps: [A]\ndock:\n  apps: [B]\n")
            _write(os.path.join(d, "top.yaml"), "include: [inc.yaml]\n")
            with self.assertRaisesRegex(ConfigError, r"inc\.yaml: duplicate key 'dock'"):
                load_config(os.path.join(d, "top.yaml"))


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

    def test_included_managed_lists_append_in_include_order(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "a.yaml"), "dock:\n  apps: [A, B]\n")
            _write(
                os.path.join(d, "b.yaml"),
                "dock:\n  apps: [B, C]\ndefaults:\n  user:\n    d:\n      k: [0]\n",
            )
            _write(
                os.path.join(d, "top.yaml"),
                "include: [a.yaml, b.yaml]\ndock:\n  apps: [D, A]\n"
                "defaults:\n  user:\n    d:\n      k: [1]\n",
            )

            loaded = load_config(os.path.join(d, "top.yaml"))

            self.assertEqual(loaded["dock"]["apps"], ["A", "B", "B", "C", "D", "A"])
            self.assertEqual(loaded["defaults"]["user"]["d"]["k"], [1])

    def test_dock_spacers_repeat_across_includes(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "inc.yaml"), "dock:\n  apps: [A, spacer, B, {spacer: small}]\n")
            _write(
                os.path.join(d, "top.yaml"),
                "include: [inc.yaml]\ndock:\n  apps: [spacer, C, {spacer: small}]\n",
            )

            loaded = load_config(os.path.join(d, "top.yaml"))

            self.assertEqual(
                loaded["dock"]["apps"],
                ["A", "spacer", "B", {"spacer": "small"}, "spacer", "C", {"spacer": "small"}],
            )

    def test_host_repeating_an_earlier_include_entry_wins(self):
        a = "- {source: a, target: ~/t}\n"
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "i1.yaml"), "files:\n  " + a)
            _write(os.path.join(d, "i2.yaml"), "files:\n  - {source: b, target: ~/t}\n")
            _write(os.path.join(d, "top.yaml"), "include: [i1.yaml, i2.yaml]\nfiles:\n  " + a)

            loaded = load_config(os.path.join(d, "top.yaml"))

            self.assertEqual(
                loaded["files"],
                [{"source": "b", "target": "~/t"}, {"source": "a", "target": "~/t"}],
            )

    def test_host_link_replaces_an_included_link_on_the_same_target(self):
        with tempfile.TemporaryDirectory() as d:
            _write(
                os.path.join(d, "inc.yaml"),
                "links:\n  - {source: ~/a, target: ~/t}\n  - {source: ~/k, target: ~/u}\n",
            )
            _write(
                os.path.join(d, "top.yaml"),
                "include: [inc.yaml]\nlinks:\n  - {source: ~/b, target: ~/t}\n",
            )

            loaded = load_config(os.path.join(d, "top.yaml"))

            self.assertEqual(
                loaded["links"],
                [{"source": "~/k", "target": "~/u"}, {"source": "~/b", "target": "~/t"}],
            )

    def test_null_managed_list_over_an_include_stays_null(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "a.yaml"), "brew:\n  casks: [x]\n")
            _write(os.path.join(d, "top.yaml"), "include: [a.yaml]\nbrew:\n  casks:\n")

            loaded = load_config(os.path.join(d, "top.yaml"))

            self.assertIsNone(loaded["brew"]["casks"])

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

    def test_yaml_syntax_error_is_a_config_error(self):
        from tools import cli

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.yaml")
            _write(path, "files: [\n")

            with self.assertRaisesRegex(ConfigError, path):
                load_config(path)
            with mock.patch.object(cli, "log") as log, self.assertRaises(SystemExit):
                cli._load_merged_config([path])

        self.assertIn(f"Config error in {path}", log.call_args[0][0])

    def test_included_non_mapping_is_a_config_error(self):
        with tempfile.TemporaryDirectory() as d:
            inc = os.path.join(d, "inc.yaml")
            _write(inc, "- a\n- b\n")
            host = os.path.join(d, "h.yaml")
            _write(host, "include: [inc.yaml]\n")

            with self.assertRaisesRegex(ConfigError, f"{inc}: top level must be a mapping"):
                load_config(host)


class NullSectionErrorsTest(unittest.TestCase):
    def test_absent_and_explicit_empty_are_fine(self):
        self.assertEqual(config_mod.null_section_errors({}), {})
        config = {
            "uv": {"packages": {}},
            "files": [],
            "brew": {"casks": [], "trust": {"taps": []}},
            "mgmt": {"arr": {}},
            "dock": {"apps": []},
        }
        self.assertEqual(config_mod.null_section_errors(config), {})

    def test_yaml_bare_keys_are_errors(self):
        import yaml

        config = yaml.safe_load(
            "uv:\n  packages:\nfiles:\nbrew:\n  trust:\n    taps:\nmgmt:\n  arr:\n"
            "sops:\n  secrets:\n    a:\n"
        )
        held = config_mod.null_section_errors(config)
        self.assertEqual(
            held,
            {
                "uv": ["uv.packages must be a mapping, got NoneType (use {} for none)"],
                "files": ["files must be a list, got NoneType (use [] for none)"],
                "brew": ["brew.trust.taps must be a list, got NoneType (use [] for none)"],
                "mgmt": ["mgmt.arr must be a mapping, got NoneType (use {} for none)"],
            },
        )

    def test_null_values_that_mean_defaults_are_not_errors(self):
        # A bare package or secret name installs with defaults; a null defaults
        # value is unmanaged.
        config = {
            "uv": {"packages": {"ruff": None}},
            "sops": {"secrets": {"a": None}},
            "defaults": {"user": {"d": {"k": None}}},
        }
        self.assertEqual(config_mod.null_section_errors(config), {})

    def test_every_section_but_nix_darwin_is_checked(self):
        checked = set(config_mod._MANAGED_KEYS)
        self.assertEqual(set(SECTIONS) - checked, {"nixDarwin"})
        for section in checked:
            self.assertIn(section, config_mod.null_section_errors({section: None}))


if __name__ == "__main__":
    unittest.main()
