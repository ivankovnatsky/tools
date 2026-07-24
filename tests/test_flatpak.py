import unittest
from unittest import mock

from tools.user import flatpak


class FakeFlatpak:
    """Stand-in for the flatpak CLI, recording the commands it is given."""

    def __init__(self, remotes=(), installed=(), origins=()):
        self.remotes = set(remotes)
        self.installed = set(installed)
        # Remotes that still have refs installed from them (apps or runtimes).
        self.origins = set(origins)
        self.commands = []

    def run(self, cmd, env=None, cwd=None):
        self.commands.append(cmd)
        verb = cmd[2]
        if verb == "remotes":
            return 0, "\n".join(sorted(self.remotes)), ""
        if verb == "list":
            if "--columns=origin" in cmd:
                return 0, "\n".join(sorted(self.origins)), ""
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

    def test_tracked_app_dropped_from_config_is_removed(self):
        state = {"flatpak": {"packages": ["org.example.Old"], "remotes": []}}
        fake = FakeFlatpak(installed=["org.example.Old"])

        self._reconcile({"packages": []}, dict(state), fake)

        self.assertEqual(fake.installed, set())

    def test_stale_tracked_entry_forces_a_diff(self):
        # Tracked but gone from the system and from config: without a diff
        # line deploy short-circuits and state keeps claiming it.
        state = {"flatpak": {"packages": ["org.example.Gone"], "remotes": ["stale"]}}
        fake = FakeFlatpak()

        changes = self._diff({"packages": []}, state, fake)

        self.assertIn("  ~ forget org.example.Gone", changes)
        self.assertIn("  ~ forget remote stale", changes)

    def test_reinstalled_forgotten_app_is_not_deleted(self):
        # Full cycle: app vanishes out-of-band, deploy converges state, then
        # the user installs it by hand — it must survive the next deploy.
        state = {"flatpak": {"packages": ["org.example.Gone"], "remotes": []}}
        self._reconcile({"packages": []}, state, FakeFlatpak())
        self.assertEqual(state["flatpak"]["packages"], [])

        fake = FakeFlatpak(installed=["org.example.Gone"])
        self._reconcile({"packages": []}, state, fake)

        self.assertEqual(fake.installed, {"org.example.Gone"})

    def test_unmanaged_app_is_left_alone(self):
        # Installed by hand, never in state — cleanup must not touch it.
        fake = FakeFlatpak(installed=["org.example.Manual"])
        state = {"flatpak": {"packages": [], "remotes": []}}

        self._reconcile({"packages": []}, state, fake)

        self.assertEqual(fake.installed, {"org.example.Manual"})

    def test_remote_still_in_use_is_not_deleted(self):
        # flatpak refuses to delete a remote with refs installed, and those refs
        # can be hand-installed apps. Skipping keeps it tracked for a later run
        # instead of pinning success=False forever.
        state = {"flatpak": {"packages": [], "remotes": ["flathub"]}}
        fake = FakeFlatpak(
            remotes=["flathub"], installed=["org.example.Manual"], origins=["flathub"]
        )

        self.assertTrue(self._reconcile({"remotes": {}, "packages": []}, state, fake))

        self.assertIn("flathub", fake.remotes)
        self.assertFalse(any(c[2] == "remote-delete" for c in fake.commands))
        self.assertEqual(state["flatpak"]["remotes"], ["flathub"])

    def test_in_use_remote_is_not_previewed_for_deletion(self):
        # Preview must match apply, or the diff never clears.
        state = {"flatpak": {"packages": [], "remotes": ["flathub"]}}
        fake = FakeFlatpak(
            remotes=["flathub"], installed=["org.example.Manual"], origins=["flathub"]
        )

        changes = self._diff({"remotes": {}, "packages": []}, state, fake)

        self.assertNotIn("  - remote-delete flathub", changes)

    def test_unused_remote_is_deleted(self):
        state = {"flatpak": {"packages": [], "remotes": ["flathub"]}}
        fake = FakeFlatpak(remotes=["flathub"])

        self._reconcile({"remotes": {}, "packages": []}, state, fake)

        self.assertNotIn("flathub", fake.remotes)
        self.assertEqual(state["flatpak"]["remotes"], [])

    def test_diff_and_deploy_agree(self):
        # Parity: empty diff means deploy has nothing to do; non-empty diff
        # means it does something. Preview/apply mismatch was the single most
        # common bug class in this module.
        scenarios = [
            ({"packages": ["a"]}, {}, [], []),
            ({"packages": []}, {"flatpak": {"packages": ["a"], "remotes": []}}, ["a"], []),
            ({"packages": []}, {"flatpak": {"packages": ["a"], "remotes": []}}, [], []),
            ({"packages": ["a"]}, {"flatpak": {"packages": ["a"], "remotes": []}}, ["a"], []),
            ({"packages": []}, {"flatpak": {"packages": [], "remotes": ["r"]}}, [], ["r"]),
            ({"packages": []}, {"flatpak": {"packages": [], "remotes": ["r"]}}, [], []),
        ]
        for config, state, installed, origins in scenarios:
            with self.subTest(config=config, state=state, installed=installed):
                remotes = ["r"] if state.get("flatpak", {}).get("remotes") else []
                changes = self._diff(
                    config,
                    dict(state),
                    FakeFlatpak(remotes=remotes, installed=installed, origins=origins),
                )
                fake = FakeFlatpak(remotes=remotes, installed=installed, origins=origins)
                mutable = {k: dict(v) for k, v in state.items()}
                self._reconcile(config, mutable, fake)
                acted = any(
                    c[2] in ("install", "uninstall", "remote-add", "remote-delete")
                    for c in fake.commands
                )
                state_moved = mutable.get("flatpak") != state.get("flatpak")
                self.assertEqual(bool(changes), acted or state_moved)

    def test_missing_cli_is_not_a_failure(self):
        state = {}
        with mock.patch.object(flatpak, "_find_flatpak", return_value=None):
            self.assertTrue(flatpak.install_flatpak_packages({"packages": ["a"]}, state))
            self.assertEqual(
                flatpak.diff_flatpak({"packages": ["a"]}, state), ["  ? flatpak CLI not found"]
            )

    def test_preinstalled_app_is_not_adopted(self):
        # Desired but installed by hand: we did not put it there, so it never
        # enters state and config can never remove it.
        fake = FakeFlatpak(remotes=["flathub"], installed=["org.example.App"])
        state = {}

        self.assertEqual(self._diff({"packages": ["org.example.App"]}, {}, fake), [])
        self._reconcile({"packages": ["org.example.App"]}, state, fake)

        self.assertEqual(state["flatpak"]["packages"], [])
        self.assertIn("org.example.App", fake.installed)

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
