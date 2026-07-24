import unittest
from unittest import mock

from tools import diff as diff_mod
from tools.user import mcp


class FakeClaude:
    """Stand-in for the claude CLI, recording the commands it is given."""

    def __init__(self, installed=(), failing=()):
        self.installed = set(installed)
        # Verbs whose invocation should return non-zero.
        self.failing = set(failing)
        self.commands = []

    def run(self, cmd, env=None, cwd=None):
        self.commands.append(cmd)
        verb = cmd[2] if len(cmd) > 2 else ""
        if verb == "list":
            rows = "\n".join(
                f"{name}: https://example/{name} (HTTP)" for name in sorted(self.installed)
            )
            return 0, rows, ""
        if verb in self.failing:
            return 1, "", "boom"
        if verb == "remove":
            self.installed.discard(cmd[3])
            return 0, "", ""
        if verb == "add":
            self.installed.add(cmd[-2] if cmd[-1].startswith("http") else cmd[-1])
            return 0, "", ""
        return 0, "", ""

    def removed(self):
        return [c[3] for c in self.commands if len(c) > 3 and c[2] == "remove"]


def _server(name):
    return {name: {"scope": "user", "transport": "http", "url": f"https://example/{name}"}}


class McpOwnershipTest(unittest.TestCase):
    def _reconcile(self, servers, state, fake):
        with (
            mock.patch.object(mcp, "resolve_claude_cli", return_value="/bin/claude"),
            mock.patch.object(mcp.os.path, "exists", return_value=True),
            mock.patch.object(mcp, "run_command", fake.run),
        ):
            return mcp.install_mcp_servers(servers, {}, state)

    def _diff(self, servers, state, fake):
        with (
            mock.patch("tools.user.mcp.resolve_claude_cli", return_value="/bin/claude"),
            mock.patch.object(mcp.os.path, "exists", return_value=True),
            mock.patch.object(mcp, "run_command", fake.run),
        ):
            return diff_mod._diff_mcp(servers, {}, state)

    def test_hand_registered_server_is_never_removed(self):
        # Registered outside this tool: absent from config and absent from
        # state, so it is not ours to delete.
        fake = FakeClaude(installed=["manual"])
        state = {"mcp": {"servers": {}}}

        self._reconcile({}, state, fake)

        self.assertIn("manual", fake.installed)
        self.assertEqual(fake.removed(), [])

    def test_tracked_server_dropped_from_config_is_removed(self):
        fake = FakeClaude(installed=["ours"])
        state = {"mcp": {"servers": {"ours": {"installed": True}}}}

        self._reconcile({}, state, fake)

        self.assertNotIn("ours", fake.installed)
        self.assertEqual(fake.removed(), ["ours"])

    def test_diff_never_previews_removing_an_untracked_server(self):
        fake = FakeClaude(installed=["manual"])
        state = {"mcp": {"servers": {}}}

        changes = self._diff({}, state, fake)

        self.assertEqual(changes, [])

    def test_diff_previews_tracked_removal(self):
        fake = FakeClaude(installed=["ours"])
        state = {"mcp": {"servers": {"ours": {"installed": True}}}}

        changes = self._diff({}, state, fake)

        self.assertIn("  - remove ours", changes)

    def test_diff_skips_cli_entirely_when_nothing_desired_or_tracked(self):
        fake = FakeClaude(installed=["manual"])

        changes = self._diff({}, {}, fake)

        self.assertEqual(changes, [])
        self.assertEqual(fake.commands, [])

    def test_failed_install_is_not_recorded_as_installed(self):
        # Recording it would hide the failure from the next run's diff.
        fake = FakeClaude(failing=["add"])
        state = {}

        self.assertFalse(self._reconcile(_server("newsrv"), state, fake))

        self.assertNotIn("newsrv", state.get("mcp", {}).get("servers", {}))

    def test_failed_removal_propagates(self):
        fake = FakeClaude(installed=["ours"], failing=["remove"])
        state = {"mcp": {"servers": {"ours": {"installed": True}}}}

        self.assertFalse(self._reconcile({}, state, fake))

    def test_successful_run_reports_success(self):
        fake = FakeClaude()
        state = {}

        self.assertTrue(self._reconcile(_server("newsrv"), state, fake))

        self.assertIn("newsrv", state["mcp"]["servers"])


if __name__ == "__main__":
    unittest.main()
