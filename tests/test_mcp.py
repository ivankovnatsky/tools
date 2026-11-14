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

    def test_hand_registered_server_under_configured_name_is_adopted(self):
        fake = FakeClaude(installed=["manual", "other"])
        state = {"mcp": {"servers": {}}}

        self.assertEqual(
            self._diff(_server("manual"), state, fake),
            [
                "  ~ re-register manual (registered outside tools; adopted if it is in scope "
                "user, otherwise left alone)"
            ],
        )
        self.assertTrue(self._reconcile(_server("manual"), state, fake))
        self.assertEqual(fake.removed(), ["manual"])
        self.assertIn("manual", fake.installed)
        self.assertEqual(state["mcp"]["servers"]["manual"]["url"], "https://example/manual")
        self.assertEqual(self._diff(_server("manual"), state, fake), [])

        changed = {"manual": {**_server("manual")["manual"], "url": "https://example/new"}}
        self.assertEqual(self._diff(changed, state, fake), ["  ~ re-register manual"])

        self.assertEqual(self._diff({}, state, fake), ["  - remove manual"])
        self._reconcile({}, state, fake)
        self.assertNotIn("manual", fake.installed)
        self.assertIn("other", fake.installed)

    def _other_scope(self, listed: bool) -> FakeClaude:
        """`manual` lives in another scope: removing it from the configured one fails,
        and adding it again reports it exists."""
        fake = FakeClaude(installed=["manual"] if listed else [])

        def run(cmd, env=None, cwd=None):
            verb = cmd[2] if len(cmd) > 2 else ""
            if verb in ("remove", "add"):
                fake.commands.append(cmd)
                err = "No user-scoped MCP server" if verb == "remove" else "already exists"
                return 1, "", f"{err} named manual"
            return FakeClaude.run(fake, cmd, env, cwd)

        fake.run = run
        return fake

    def test_server_in_another_scope_is_left_alone_not_retried(self):
        for listed in (True, False):
            with self.subTest(listed=listed):
                fake = self._other_scope(listed)
                state = {"mcp": {"servers": {}}}
                self.assertTrue(self._reconcile(_server("manual"), state, fake))
                self.assertNotIn("manual", state["mcp"]["servers"])
                self.assertEqual(state["mcp"]["foreign"], {"manual": "user"})
                adds = [c for c in fake.commands if c[2] == "add"]
                self.assertEqual(len(adds), 0 if listed else 1)

                # Remembered: no more attempts, and the preview has nothing to apply.
                fake.commands.clear()
                self.assertTrue(self._reconcile(_server("manual"), state, fake))
                self.assertEqual([c for c in fake.commands if c[2] != "list"], [])
                rows = self._diff(_server("manual"), state, fake)
                self.assertEqual(
                    rows,
                    ["  = skip manual (registered outside tools, not in scope user; left alone)"],
                )
                self.assertTrue(diff_mod._print_sections([("mcp", rows)]))

                # A different configured scope tries again.
                other = {"manual": {**_server("manual")["manual"], "scope": "local"}}
                self.assertEqual(len(self._diff(other, state, fake)), 1)
                self.assertNotIn("= skip", self._diff(other, state, fake)[0])

    def test_already_exists_on_add_adopts_in_the_configured_scope(self):
        fake = FakeClaude()
        adds = []

        def run(cmd, env=None, cwd=None):
            if len(cmd) > 2 and cmd[2] == "add":
                adds.append(cmd)
                if len(adds) == 1:
                    return 1, "", "MCP server manual already exists in user config"
            return FakeClaude.run(fake, cmd, env, cwd)

        fake.run = run
        state = {"mcp": {"servers": {}}}
        self.assertTrue(self._reconcile(_server("manual"), state, fake))
        self.assertEqual(fake.removed(), ["manual"])
        self.assertEqual(len(adds), 2)
        self.assertEqual(state["mcp"]["servers"]["manual"]["scope"], "user")
        self.assertNotIn("foreign", state["mcp"])

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


class McpRuntimePlaceholderTest(unittest.TestCase):
    _reconcile = McpOwnershipTest._reconcile

    def test_bearer_env_header_reaches_claude_unresolved(self):
        from tools.config import resolve_config

        config = {
            "mcp": {
                "servers": {
                    "x": {
                        "scope": "user",
                        "transport": "http",
                        "url": "https://example/x",
                        "headers": ["Authorization: Bearer ${API_KEY}"],
                    }
                }
            }
        }
        servers = resolve_config(config, env={"API_KEY": "real-key"})["mcp"]["servers"]
        state, fake = {}, FakeClaude()
        self.assertTrue(self._reconcile(servers, state, fake))
        add = next(c for c in fake.commands if c[2] == "add")
        self.assertIn("Authorization: Bearer ${API_KEY}", add)
        self.assertNotIn("real-key", repr(fake.commands))
        self.assertNotIn("real-key", repr(state))
        self.assertIn("${API_KEY}", repr(state))


if __name__ == "__main__":
    unittest.main()
