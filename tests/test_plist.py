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

    def test_dropped_key_gets_its_prior_value_back(self):
        self.write({"General": {"showMenuBarItem": True, "other": 1}})
        state = {}
        self.assertTrue(plist.install_plist(self.raw, state))
        self.assertEqual(self.read()[0]["General"]["showMenuBarItem"], False)
        self.assertTrue(plist.has_work(None, state))
        self.assertEqual(
            plist.diff_plist(None, state),
            [f"  - restore {self.path} General:showMenuBarItem -> true"],
        )
        self.assertTrue(plist.install_plist(None, state))
        self.assertEqual(self.read()[0], {"General": {"showMenuBarItem": True, "other": 1}})
        self.assertEqual(state, {})
        self.assertFalse(plist.has_work(None, state))

    def test_dropped_key_that_was_absent_is_deleted(self):
        self.write({"General": {"other": 1}})
        state = {}
        self.assertTrue(plist.install_plist(self.raw, state))
        other = {self.path: {"General:other": 1}}
        self.assertEqual(
            plist.diff_plist(other, state), [f"  - delete {self.path} General:showMenuBarItem"]
        )
        self.assertTrue(plist.install_plist(other, state))
        self.assertEqual(self.read()[0], {"General": {"other": 1}})
        self.assertEqual(state, {})

    def test_key_changed_by_hand_is_left_and_forgotten(self):
        self.write({"General": {"showMenuBarItem": True}})
        state = {}
        self.assertTrue(plist.install_plist(self.raw, state))
        self.write({"General": {"showMenuBarItem": "mine"}})
        self.assertEqual(
            plist.diff_plist(None, state),
            [f"  ~ forget {self.path} General:showMenuBarItem (changed by hand; left as is)"],
        )
        self.assertTrue(plist.install_plist(None, state))
        self.assertEqual(self.read()[0], {"General": {"showMenuBarItem": "mine"}})
        self.assertEqual(state, {})

    def test_gone_file_is_forgotten_not_created(self):
        self.write({"General": {"showMenuBarItem": True}})
        state = {}
        self.assertTrue(plist.install_plist(self.raw, state))
        os.remove(self.path)
        self.assertTrue(plist.install_plist(None, state))
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(state, {})

    def test_value_change_keeps_first_prior(self):
        self.write({"General": {"showMenuBarItem": True}})
        state = {}
        self.assertTrue(plist.install_plist(self.raw, state))
        self.assertTrue(plist.install_plist({self.path: {"General:showMenuBarItem": 5}}, state))
        self.assertTrue(plist.install_plist(None, state))
        self.assertEqual(self.read()[0], {"General": {"showMenuBarItem": True}})

    def test_hand_set_new_value_is_adopted_before_removal(self):
        self.write({"General": {"showMenuBarItem": True}})
        state = {}
        self.assertTrue(plist.install_plist(self.raw, state))
        self.write({"General": {"showMenuBarItem": 5}})
        self.assertTrue(plist.install_plist({self.path: {"General:showMenuBarItem": 5}}, state))
        self.assertTrue(plist.install_plist(None, state))
        self.assertEqual(self.read()[0], {"General": {"showMenuBarItem": True}})

    def test_unreadable_file_keeps_state(self):
        self.write({"General": {"showMenuBarItem": True}})
        state = {}
        self.assertTrue(plist.install_plist(self.raw, state))
        with open(self.path, "w") as f:
            f.write("not a plist")
        self.assertFalse(plist.install_plist(None, state))
        self.assertTrue(plist.has_work(None, state))

    def test_failed_write_does_not_record_ownership(self):
        self.write({"General": {"showMenuBarItem": True}})
        state = {}
        with mock.patch.object(plist.fsutil, "atomic_write", side_effect=OSError("boom")):
            self.assertFalse(plist.install_plist(self.raw, state))
        self.assertEqual(state, {})

    def test_parent_key_moving_to_child_keeps_new_value(self):
        self.write({"other": 1})
        state = {}
        self.assertTrue(plist.install_plist({self.path: {"A": {"B": 1}}}, state))
        child = {self.path: {"A:B": 2}}
        self.assertEqual(
            plist.diff_plist(child, state),
            [f"  + write {self.path} A:B = 2", f"  - delete {self.path} A"],
        )
        self.assertTrue(plist.install_plist(child, state))
        self.assertEqual(self.read()[0], {"other": 1, "A": {"B": 2}})
        self.assertEqual(plist.diff_plist(child, state), [])
        self.assertTrue(plist.install_plist(None, state))
        self.assertEqual(self.read()[0], {"other": 1, "A": {}})

    def test_child_key_moving_to_parent_keeps_new_value(self):
        self.write({"A": {"C": 3}})
        state = {}
        self.assertTrue(plist.install_plist({self.path: {"A:B": 1}}, state))
        parent = {self.path: {"A": {"B": 2}}}
        self.assertTrue(plist.install_plist(parent, state))
        self.assertEqual(self.read()[0], {"A": {"B": 2}})
        self.assertEqual(plist.diff_plist(parent, state), [])
        self.assertTrue(plist.install_plist(None, state))
        self.assertEqual(self.read()[0], {"A": {"C": 3}})

    def test_prior_xml_cannot_hold_is_kept_and_restored(self):
        self.write({"General": {"showMenuBarItem": plistlib.UID(7)}, "n": {"k": None}})
        state = {}
        raw = {self.path: {"General:showMenuBarItem": False, "n": 1}}
        self.assertTrue(plist.install_plist(raw, state))
        self.assertEqual(self.read()[0], {"General": {"showMenuBarItem": False}, "n": 1})
        self.assertTrue(plist.install_plist(None, state))
        self.assertEqual(
            self.read()[0], {"General": {"showMenuBarItem": plistlib.UID(7)}, "n": {"k": None}}
        )
        self.assertEqual(state, {})

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

    def _deploy(self, config, state):
        with (
            mock.patch.object(cli, "load_json", return_value=state),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(cli, "save_json"),
            mock.patch.object(cli, "state_lock"),
            mock.patch.object(cli, "install_plist", return_value=True) as inst,
        ):
            self.assertTrue(cli._deploy(config, "/tmp"))
        return inst

    def test_deploy_runs_declared_section(self):
        raw = {"/tmp/x.plist": {"k": 1}}
        inst = self._deploy({"plist": raw}, {})
        inst.assert_called_once()
        self.assertEqual(inst.call_args.args[0], raw)
        self.assertIsInstance(inst.call_args.args[1], dict)

    def test_deploy_runs_on_state_alone(self):
        state = {"plist": {"managed": {"/tmp/x.plist": {"k": {"prior": None}}}}}
        inst = self._deploy({}, state)
        inst.assert_called_once()
        self.assertIsNone(inst.call_args.args[0])
        self._deploy({}, {}).assert_not_called()


if __name__ == "__main__":
    unittest.main()
