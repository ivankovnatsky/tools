import unittest
from unittest import mock

from tools.user import flatpak


class FakeFlatpak:
    """Stand-in for the flatpak CLI, recording the commands it is given."""

    def __init__(self, remotes=(), installed=()):
        self.remotes = set(remotes)
        self.installed = set(installed)
        self.commands = []

    def run(self, cmd, env=None, cwd=None):
        self.commands.append(cmd)
        verb = cmd[2]
        if verb == "remotes":
            return 0, "\n".join(sorted(self.remotes)), ""
        if verb == "list":
            return 0, "\n".join(sorted(self.installed)), ""
        if verb == "remote-add":
            self.remotes.add(cmd[-2])
            return 0, "", ""
        if verb == "remote-delete":
            self.remotes.discard(cmd[-1])
            return 0, "", ""
        if verb == "install":
            self.installed.add(cmd[-1])
            return 0, "", ""
        if verb == "uninstall":
            self.installed.discard(cmd[-1])
            return 0, "", ""
        raise AssertionError(f"unexpected flatpak verb: {verb}")


class FlatpakTest(unittest.TestCase):
    def _reconcile(self, config, state, fake):
        with (
            mock.patch.object(flatpak, "_find_flatpak", return_value="flatpak"),
            mock.patch.object(flatpak, "run_command", fake.run),
        ):
            return flatpak.install_flatpak_packages(config, state)

    def _diff(self, config, state, fake):
        with (
            mock.patch.object(flatpak, "_find_flatpak", return_value="flatpak"),
            mock.patch.object(flatpak, "run_command", fake.run),
        ):
            return flatpak.diff_flatpak(config, state)

    def test_adds_remote_and_installs_app(self):
        fake = FakeFlatpak()
        state = {}
        config = {
            "remotes": {"flathub": "https://example/flathub.flatpakrepo"},
            "packages": ["com.bitwarden.desktop"],
        }

        self.assertTrue(self._reconcile(config, state, fake))

        self.assertEqual(fake.remotes, {"flathub"})
        self.assertEqual(fake.installed, {"com.bitwarden.desktop"})
        self.assertEqual(state["flatpak"]["packages"], ["com.bitwarden.desktop"])
        self.assertEqual(state["flatpak"]["remotes"], ["flathub"])

    def test_every_command_is_user_scoped(self):
        fake = FakeFlatpak(remotes=["flathub"], installed=["org.example.Old"])
        config = {
            "remotes": {"flathub": "https://example/flathub.flatpakrepo", "other": "https://x"},
            "packages": ["org.example.App"],
            "removeUntracked": True,
        }
        state = {"flatpak": {"packages": ["org.example.Old"], "remotes": ["flathub"]}}

        self._reconcile(config, state, fake)

        self.assertTrue(fake.commands)
        self.assertTrue(all(cmd[1] == "--user" for cmd in fake.commands))
        self.assertFalse(any("--system" in cmd for cmd in fake.commands))

    def test_per_app_remote_is_passed_through(self):
        fake = FakeFlatpak(remotes=["flathub"])
        config = {"packages": {"org.example.App": {"remote": "flathub"}}}
        self._reconcile(config, {}, fake)

        install = next(cmd for cmd in fake.commands if cmd[2] == "install")
        self.assertEqual(install[-2:], ["flathub", "org.example.App"])

    def test_untracked_app_removed_only_when_enabled(self):
        state = {"flatpak": {"packages": ["org.example.Old"], "remotes": []}}
        fake = FakeFlatpak(installed=["org.example.Old"])

        self._reconcile({"packages": []}, dict(state), fake)
        self.assertEqual(fake.installed, {"org.example.Old"})

        fake = FakeFlatpak(installed=["org.example.Old"])
        self._reconcile({"packages": [], "removeUntracked": True}, dict(state), fake)
        self.assertEqual(fake.installed, set())

    def test_unmanaged_app_is_left_alone(self):
        # Installed by hand, never in state — removeUntracked must not touch it.
        fake = FakeFlatpak(installed=["org.example.Manual"])
        state = {"flatpak": {"packages": [], "remotes": []}}

        self._reconcile({"packages": [], "removeUntracked": True}, state, fake)

        self.assertEqual(fake.installed, {"org.example.Manual"})

    def test_missing_cli_is_not_a_failure(self):
        state = {}
        with mock.patch.object(flatpak, "_find_flatpak", return_value=None):
            self.assertTrue(flatpak.install_flatpak_packages({"packages": ["a"]}, state))
            self.assertEqual(
                flatpak.diff_flatpak({"packages": ["a"]}, state), ["  ? flatpak CLI not found"]
            )

    def test_diff_reports_adoption_of_preinstalled_app(self):
        fake = FakeFlatpak(remotes=["flathub"], installed=["org.example.App"])
        changes = self._diff({"packages": ["org.example.App"]}, {}, fake)

        self.assertEqual(changes, ["  ~ adopt org.example.App"])

    def test_diff_is_empty_when_in_sync(self):
        fake = FakeFlatpak(remotes=["flathub"], installed=["org.example.App"])
        state = {"flatpak": {"packages": ["org.example.App"], "remotes": ["flathub"]}}
        config = {
            "remotes": {"flathub": "https://example/flathub.flatpakrepo"},
            "packages": ["org.example.App"],
        }

        self.assertEqual(self._diff(config, state, fake), [])

    def test_nothing_configured_does_nothing(self):
        fake = FakeFlatpak()
        state = {}

        self.assertTrue(self._reconcile({}, state, fake))

        self.assertEqual(fake.commands, [])
        self.assertEqual(state, {})


if __name__ == "__main__":
    unittest.main()
