import os
import plistlib
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from tools import cli
from tools.config import TOP_LEVEL_KEYS
from tools.user import plist


class PlistTest(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "config.plist")
        self.raw = {self.path: {"General:showMenuBarItem": False}}

    def write(self, data, fmt=plistlib.FMT_BINARY, mode=0o600):
        with open(self.path, "wb") as f:
            plistlib.dump(data, f, fmt=fmt)
        os.chmod(self.path, mode)

    def read(self):
        with open(self.path, "rb") as f:
            raw = f.read()
        return plistlib.loads(raw), raw.startswith(b"bplist")

    def test_sets_only_the_key_and_keeps_format_and_mode(self):
        self.write({"General": {"showMenuBarItem": True, "other": 1}, "License": {"k": "v"}})
        self.assertEqual(
            plist.diff_plist(self.raw),
            [f"  ~ write {self.path} General:showMenuBarItem: true -> false"],
        )
        self.assertTrue(plist.install_plist(self.raw))
        data, binary = self.read()
        self.assertEqual(
            data, {"General": {"showMenuBarItem": False, "other": 1}, "License": {"k": "v"}}
        )
        self.assertTrue(binary)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(plist.diff_plist(self.raw), [])

    def test_clean_file_is_not_rewritten(self):
        self.write({"General": {"showMenuBarItem": False}}, fmt=plistlib.FMT_XML)
        before = os.stat(self.path).st_ino
        self.assertTrue(plist.install_plist(self.raw))
        self.assertEqual(os.stat(self.path).st_ino, before)

    def test_types_are_strict(self):
        self.write({"General": {"showMenuBarItem": 0}})
        self.assertEqual(len(plist.diff_plist(self.raw)), 1)

    def test_missing_parents_are_created(self):
        self.write({}, fmt=plistlib.FMT_XML)
        self.assertEqual(
            plist.diff_plist(self.raw), [f"  + write {self.path} General:showMenuBarItem = false"]
        )
        self.assertTrue(plist.install_plist(self.raw))
        self.assertEqual(self.read(), ({"General": {"showMenuBarItem": False}}, False))

    def test_missing_file_is_skipped_not_created(self):
        rows = plist.diff_plist(self.raw)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].startswith("  = skip "))
        self.assertTrue(plist.install_plist(self.raw))
        self.assertFalse(os.path.exists(self.path))

    def test_non_dict_parent_is_an_error(self):
        self.write({"General": "x"})
        self.assertTrue(plist.diff_plist(self.raw)[0].startswith("  ? "))
        self.assertFalse(plist.install_plist(self.raw))
        self.assertEqual(self.read()[0], {"General": "x"})

    def test_unreadable_plist_is_an_error(self):
        for body in ("not a plist", "<?xml version='1.0'?><plist><dict><key>a"):
            with self.subTest(body=body):
                with open(self.path, "w") as f:
                    f.write(body)
                self.assertTrue(plist.diff_plist(self.raw)[0].startswith("  ? "))
                self.assertFalse(plist.install_plist(self.raw))

    def test_parse_errors_hold_the_section(self):
        for raw in (
            {"relative.plist": {"k": 1}},
            {self.path: {"a::b": 1}},
            {self.path: {"k": None}},
            {self.path: {}},
            {self.path: {"General": {}, "General:showMenuBarItem": False}},
            {self.path: {"General:showMenuBarItem": False, "General": {"x": 1}}},
            [],
        ):
            with self.subTest(raw=raw):
                spec, errors = plist.parse(raw)
                self.assertEqual(spec, {})
                self.assertTrue(errors)

    def test_sibling_key_paths_are_fine(self):
        spec, errors = plist.parse({self.path: {"General:a": 1, "General:ab": 2, "Generalx": 3}})
        self.assertEqual(errors, [])
        self.assertEqual(len(spec[self.path]), 3)

    def test_tilde_expands(self):
        with mock.patch.dict(os.environ, {"HOME": self.dir}):
            spec, errors = plist.parse({"~/config.plist": {"k": 1}})
        self.assertEqual(errors, [])
        self.assertEqual(list(spec), [self.path])


class WiringTest(unittest.TestCase):
    def test_known_user_section(self):
        from tools import privilege

        self.assertIn("plist", TOP_LEVEL_KEYS)
        self.assertNotIn("plist", privilege.ROOT_SECTIONS)

    def test_deploy_runs_declared_section(self):
        raw = {"/tmp/x.plist": {"k": 1}}
        with (
            mock.patch.object(cli, "load_json", return_value={}),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(cli, "save_json"),
            mock.patch.object(cli, "state_lock"),
            mock.patch.object(cli, "install_plist", return_value=True) as inst,
        ):
            self.assertTrue(cli._deploy({"plist": raw}, "/tmp"))
        inst.assert_called_once_with(raw)


if __name__ == "__main__":
    unittest.main()
