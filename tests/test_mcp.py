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
        if verb in self.failing:
            return 1, "", "boom"
        if verb == "list":
            rows = "\n".join(
                f"{name}: https://example/{name} (HTTP)" for name in sorted(self.installed)
            )
            return 0, rows, ""
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

    def test_failed_removal_keeps_server_tracked(self):
        # The server is still registered; dropping it from state would mean
        # it is never removed and never shown in a diff again.
        fake = FakeClaude(installed=["ours"], failing=["remove"])
        state = {"mcp": {"servers": {"ours": {"installed": True}}}}

        self._reconcile({}, state, fake)

        self.assertIn("ours", state["mcp"]["servers"])

    def test_removal_uses_tracked_scope(self):
        # A server registered with -s local can never be removed by -s user.
        fake = FakeClaude(installed=["ours"])
        state = {"mcp": {"servers": {"ours": {"installed": True, "scope": "local"}}}}

        self._reconcile({}, state, fake)

        remove_cmds = [c for c in fake.commands if len(c) > 2 and c[2] == "remove"]
        self.assertEqual(remove_cmds[0][4:6], ["-s", "local"])

    def test_failed_list_aborts_without_forgetting_servers(self):
        # An empty desired config plus a transient listing failure must not
        # wipe every tracked server from state.
        fake = FakeClaude(installed=["ours"], failing=["list"])
        state = {"mcp": {"servers": {"ours": {"installed": True}}}}

        self.assertFalse(self._reconcile({}, state, fake))

        self.assertIn("ours", state["mcp"]["servers"])
        self.assertEqual(fake.removed(), [])

    def test_changed_args_are_re_registered(self):
        # args are registration inputs: editing them must not be silent.
        fake = FakeClaude(installed=["srv"])
        cfg = {"srv": {"scope": "user", "transport": "stdio", "args": ["--new"]}}
        state = {
            "mcp": {
                "servers": {
                    "srv": {
                        "installed": True,
                        "scope": "user",
                        "transport": "stdio",
                        "url": None,
                        "command": None,
                        "args": ["--old"],
                    }
                }
            }
        }

        self._reconcile(cfg, state, fake)

        self.assertEqual(fake.removed(), ["srv"])

    def test_changed_url_is_re_registered(self):
        # Identity is the name, so `claude mcp list` cannot reveal a changed
        # url/transport — without a fingerprint, editing config does nothing.
        fake = FakeClaude(installed=["srv"])
        state = {
            "mcp": {
                "servers": {
                    "srv": {
                        "installed": True,
                        "scope": "user",
                        "transport": "http",
                        "url": "https://old/srv",
                        "command": None,
                    }
                }
            }
        }

        self._reconcile(_server("srv"), state, fake)

        self.assertEqual(fake.removed(), ["srv"])
        self.assertTrue(any(c[2] == "add" for c in fake.commands))

    def test_unchanged_server_is_left_alone(self):
        fake = FakeClaude(installed=["srv"])
        cfg = _server("srv")
        state = {
            "mcp": {
                "servers": {
                    "srv": dict(installed=True, **cfg["srv"]),
                }
            }
        }

        self._reconcile(cfg, state, fake)

        self.assertEqual(fake.removed(), [])

    def test_diff_previews_re_registration(self):
        fake = FakeClaude(installed=["srv"])
        state = {
            "mcp": {
                "servers": {
                    "srv": {
                        "installed": True,
                        "scope": "user",
                        "transport": "http",
                        "url": "https://old/srv",
                        "command": None,
                    }
                }
            }
        }

        changes = self._diff(_server("srv"), state, fake)

        self.assertIn("  ~ re-register srv", changes)

    def test_successful_run_reports_success(self):
        fake = FakeClaude()
        state = {}

        self.assertTrue(self._reconcile(_server("newsrv"), state, fake))

        self.assertIn("newsrv", state["mcp"]["servers"])


if __name__ == "__main__":
    unittest.main()
