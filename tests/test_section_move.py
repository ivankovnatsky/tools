import contextlib
import io
import os
import tempfile
import unittest

from tools import cli
from tools.user import secrets_flow
from tools.user.files import _file_hash, _record


class SectionMoveTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.target = os.path.join(self.tmp, "out", "app.json")
        os.makedirs(os.path.dirname(self.target))
        self.src = os.path.join(self.tmp, "src.json")
        with open(self.src, "w") as f:
            f.write('{"a": 1}\n')

    def tearDown(self):
        self._tmp.cleanup()

    def write_target(self, text='{"a": 1}\n'):
        with open(self.target, "w") as f:
            f.write(text)

    def files_cfg(self):
        return [{"source": "src.json", "target": self.target}]

    def tmpl_cfg(self):
        return {"app": {"path": self.target, "format": "json", "content": {"a": 2}, "mode": "0644"}}

    def move(self, state, files=None, templates=None, sops_raw=None):
        return secrets_flow.move_between_sections(
            state,
            files_entries=files,
            templates_raw=templates,
            sops_raw=sops_raw,
            config_dir=self.tmp,
        )

    def test_files_to_templates_moves_unmodified_record(self):
        self.write_target()
        state = {"files": {self.target: _record(self.target, _file_hash(self.target))}}
        moved = self.move(state, templates=self.tmpl_cfg())
        self.assertEqual(moved, [f"{self.target} (files -> templates)"])
        self.assertNotIn(self.target, state["files"])
        record = state["templates"]["installed"][self.target]
        self.assertEqual(record["name"], "app")
        self.assertFalse(record["secret"])
        self.assertIn("mtime_ns", record)

    def test_templates_to_files_moves_with_content_hash(self):
        self.write_target()
        from tools import fsutil

        rec = {"name": "app", "secret": False, **fsutil.fingerprint(self.target)}
        state = {"templates": {"installed": {self.target: rec}}}
        moved = self.move(state, files=self.files_cfg())
        self.assertEqual(moved, [f"{self.target} (templates -> files)"])
        self.assertEqual(state["files"][self.target]["hash"], _file_hash(self.target))
        self.assertNotIn(self.target, state["templates"]["installed"])

    def test_edited_file_keeps_its_record(self):
        self.write_target()
        state = {"files": {self.target: _record(self.target, _file_hash(self.target))}}
        self.write_target('{"edited": true}\n')
        self.assertEqual(self.move(state, templates=self.tmpl_cfg()), [])
        self.assertIn(self.target, state["files"])

    def test_missing_file_drops_record(self):
        state = {"files": {self.target: {"hash": "h"}}}
        moved = self.move(state, templates=self.tmpl_cfg())
        self.assertEqual(moved, [f"{self.target} (files -> templates)"])
        self.assertEqual(state["files"], {})
        self.assertEqual(state["templates"]["installed"], {})

    def test_declared_by_two_sections_is_not_moved(self):
        self.write_target()
        state = {"files": {self.target: _record(self.target, _file_hash(self.target))}}
        self.assertEqual(self.move(state, files=self.files_cfg(), templates=self.tmpl_cfg()), [])
        self.assertIn(self.target, state["files"])

    def test_config_errors_skip_moves(self):
        self.write_target()
        state = {"files": {self.target: _record(self.target, _file_hash(self.target))}}
        bad = {**self.tmpl_cfg(), "broken": "not a mapping"}
        self.assertEqual(self.move(state, templates=bad), [])
        self.assertIn(self.target, state["files"])

    def test_deploy_switches_files_to_templates_in_one_run(self):
        self.write_target()
        state = {"files": {self.target: _record(self.target, _file_hash(self.target))}}
        config = {"templates": self.tmpl_cfg()}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = cli._run_sections(config, self.tmp, state, {}, {"files", "templates"})
        self.assertTrue(ok, out.getvalue())
        self.assertIn("Moved:", out.getvalue())
        with open(self.target) as f:
            self.assertIn('"a": 2', f.read())
        self.assertNotIn(self.target, state.get("files", {}))
        self.assertIn(self.target, state["templates"]["installed"])


if __name__ == "__main__":
    unittest.main()
