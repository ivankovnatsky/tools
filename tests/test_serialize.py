import json
import tomllib
import unittest

import yaml

from tools.serialize import SerializeError, serialize


class SerializeTest(unittest.TestCase):
    def test_json_and_yaml_round_trip_any_mapping(self):
        data = {"rpc-port": 9091, "list": [1, "a"], "nested": {"ok": True}, "none": None}
        self.assertEqual(json.loads(serialize("json", data)), data)
        self.assertEqual(yaml.safe_load(serialize("yaml", data)), data)
        self.assertTrue(serialize("json", data).endswith("}\n"))

    def test_yaml_keeps_key_order(self):
        self.assertEqual(serialize("yaml", {"b": 1, "a": 2}), "b: 1\na: 2\n")

    def test_toml_round_trips(self):
        data = {
            "title": 'x "q" \\ é',
            "n": 1,
            "f": 1.5,
            "b": False,
            "arr": [1, 2],
            "inline": [{"a": 1}, {"a": 2}],
            "server": {"host": "h", "nested": {"deep": "d"}, "empty": {}},
            "dotted.key": {"x": 1},
            "upstream": [{"name": "a", "opts": {"tls": True}}, {"name": "b"}],
        }
        self.assertEqual(tomllib.loads(serialize("toml", data)), data)

    def test_toml_rejects_null(self):
        with self.assertRaisesRegex(SerializeError, "null"):
            serialize("toml", {"a": None})

    def test_ini_sectionless_head_then_sections(self):
        data = {
            "APP_NAME": "Forge",
            "RUN_MODE": "prod",
            "server": {"HTTP_PORT": 3000, "OFFLINE": True},
            "repository.upload": {"ENABLED": False, "EMPTY": None},
        }
        self.assertEqual(
            serialize("ini", data),
            "APP_NAME = Forge\nRUN_MODE = prod\n\n[server]\nHTTP_PORT = 3000\nOFFLINE = true\n"
            "\n[repository.upload]\nENABLED = false\nEMPTY = \n",
        )

    def test_ini_rejects_deep_nesting_and_lists(self):
        with self.assertRaisesRegex(SerializeError, "nested too deep"):
            serialize("ini", {"a": {"b": {"c": 1}}})
        with self.assertRaisesRegex(SerializeError, "scalar"):
            serialize("ini", {"a": {"b": [1]}})

    def test_env(self):
        self.assertEqual(serialize("env", {"KEY": "v", "N": 1}), "KEY='v'\nN='1'\n")
        self.assertEqual(serialize("env", {"K": "v'q"}, quote=False), "K=v'q\n")
        self.assertEqual(serialize("env", {"K": "it's"}), "K='it'\\''s'\n")
        with self.assertRaisesRegex(SerializeError, "invalid variable name"):
            serialize("env", {"bad-key": "v"})
        with self.assertRaisesRegex(SerializeError, "newline"):
            serialize("env", {"K": "a\nb"})

    def test_env_round_trips_through_sh(self):
        import os
        import subprocess
        import tempfile

        values = {
            "Q": 'it\'s "quoted"',
            "B": "back\\slash",
            "D": "$HOME ${HOME} $(echo no) `echo no`",
            "H": "a # not a comment",
            "W": "  padded  ",
            "E": "",
            "U": "ünïcode",
        }
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "app.env")
            with open(path, "w") as f:
                f.write(serialize("env", values))
            for key, expected in values.items():
                out = subprocess.run(
                    ["/bin/sh", "-c", f'. "$1"; printf %s "${key}"', "sh", path],
                    capture_output=True,
                    check=True,
                    env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"},
                )
                self.assertEqual(out.stdout.decode(), expected, key)

    def test_raw_requires_text(self):
        self.assertEqual(serialize("raw", "x\n"), "x\n")
        with self.assertRaises(SerializeError):
            serialize("raw", {"a": 1})

    def test_unknown_format(self):
        with self.assertRaisesRegex(SerializeError, "unknown format"):
            serialize("xml", {})


if __name__ == "__main__":
    unittest.main()
