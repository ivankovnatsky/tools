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
