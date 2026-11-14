import unittest

from tools.util import version_changed


class VersionChangedTest(unittest.TestCase):
    def _state(self, entry):
        return {"go": {"packages": {"tool": entry}}}

    def test_unchanged_when_version_and_source_match(self):
        state = self._state({"version": "latest", "source": "example.com/tool"})
        pkg = {"version": "latest", "source": "example.com/tool"}
        self.assertFalse(version_changed("tool", pkg, state, "go"))

    def test_changed_when_version_differs(self):
        state = self._state({"version": "1.0.0"})
        pkg = {"version": "2.0.0"}
        self.assertTrue(version_changed("tool", pkg, state, "go"))

    def test_adding_commit_pin_reinstalls(self):
        state = self._state({"version": "latest"})
        pkg = {"version": "latest", "commit": "abc123"}
        self.assertTrue(version_changed("tool", pkg, state, "go"))

    def test_removing_commit_pin_reinstalls(self):
        # Dropping a pin (stored "abc123" -> declared "") must reinstall at the
        # plain version rather than being treated as unchanged.
        state = self._state({"version": "latest", "commit": "abc123"})
        pkg = {"version": "latest"}
        self.assertTrue(version_changed("tool", pkg, state, "go"))

    def test_no_commit_on_either_side_is_unchanged(self):
        state = self._state({"version": "latest"})
        pkg = {"version": "latest"}
        self.assertFalse(version_changed("tool", pkg, state, "go"))


if __name__ == "__main__":
    unittest.main()


class RunCommandTest(unittest.TestCase):
    def test_missing_binary_is_exit_127_not_an_exception(self):
        from tools.util import run_command

        rc, out, err = run_command(["/nonexistent/tools-test/npm", "-v"])
        self.assertEqual((rc, out), (127, ""))
        self.assertEqual(err, "/nonexistent/tools-test/npm: No such file or directory")


class PackageEntriesTest(unittest.TestCase):
    def test_bare_key_means_defaults(self):
        from tools.util import package_entries

        self.assertEqual(package_entries("uv", {"ruff": None}), ({"ruff": {}}, []))

    def test_malformed_entry_fails_each_section_before_running_anything(self):
        from unittest import mock

        from tools.user import bun, cargo, go, npm, pi, uv

        def fake_run(cmd, env=None, cwd=None):
            raise AssertionError(f"ran {cmd}")

        calls = {
            "bun": lambda p: bun.install_bun_packages(p, {"bun": "/b", "nodejs": "/n"}, {}, {}),
            "npm": lambda p: npm.install_npm_packages(p, {"nodejs": "/n", "npmBin": "/nb"}, {}, {}),
            "uv": lambda p: uv.install_uv_packages(
                p, {"uv": "/u", "uvBin": "/ub", "uvToolDir": "/ut"}, {}
            ),
            "go": lambda p: go.install_go_packages(p, {}, {}),
            "cargo": lambda p: cargo.install_cargo_packages(p, {}, {}),
            "pi": lambda p: pi.install_pi_packages(p, {}, {}),
        }
        modules = {"bun": bun, "npm": npm, "uv": uv, "go": go, "cargo": cargo, "pi": pi}
        for name, call in calls.items():
            with self.subTest(section=name):
                with mock.patch.object(modules[name], "run_command", fake_run):
                    self.assertFalse(call({"tool": "1.0"}))
