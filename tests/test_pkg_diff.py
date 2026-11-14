import unittest

from tools.diff import _diff_bun, _diff_cargo, _diff_go, _diff_npm, _diff_uv

UNIT = "launchd:org.example.svc"


class PackageNotifyRowsTest(unittest.TestCase):
    def test_install_and_update_rows_list_restart_units(self):
        packages = {
            "new": {"restartUnits": [UNIT]},
            "bumped": {"version": "2", "restartUnits": [UNIT]},
        }
        for name, diff, extra in (
            ("bun", _diff_bun, ({},)),
            ("npm", _diff_npm, ({},)),
            ("cargo", _diff_cargo, ()),
            ("uv", _diff_uv, ()),
            ("go", _diff_go, ()),
        ):
            state = {name: {"packages": {"bumped": {"installed": True, "version": "1"}}}}
            with self.subTest(name):
                rows = diff(packages, {}, state, *extra)
                self.assertEqual(
                    rows,
                    [
                        "  + install new",
                        f"    notify: {UNIT}",
                        "  ~ update bumped",
                        f"    notify: {UNIT}",
                    ],
                )

    def test_unchanged_package_has_no_rows(self):
        state = {"npm": {"packages": {"a": {"installed": True, "version": "latest"}}}}
        self.assertEqual(_diff_npm({"a": {"restartUnits": [UNIT]}}, {}, state, {}), [])
