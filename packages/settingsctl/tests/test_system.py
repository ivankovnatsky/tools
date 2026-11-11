"""Tests for the hostname, timezone, sleep, remotelogin and defaultbrowser subcommands.

Run from the repo root: python -m unittest discover -s packages/settingsctl/tests
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import click  # noqa: E402
from click.testing import CliRunner  # noqa: E402

import defaultbrowser  # noqa: E402
import hostname  # noqa: E402
import remotelogin  # noqa: E402
import settings  # noqa: E402
import sleep  # noqa: E402
import timezone  # noqa: E402

MODULES = (hostname, timezone, sleep, remotelogin, defaultbrowser)


class FakeMac:
    def __init__(self):
        self.names = {"HostName": "mac", "LocalHostName": "mac", "ComputerName": "My Mac"}
        self.pm = {"sleep": "0", "displaysleep": "10", "disksleep": "0"}
        self.sshd = {"enabled": True, "loaded": True}
        self.browsers = ["safari", "firefox"]
        self.browser = "safari"
        self.calls = []

    def run(self, cmd):
        self.calls.append(list(cmd))
        name, args = os.path.basename(cmd[0]), cmd[1:]
        if name == "scutil":
            if args[0] == "--get":
                value = self.names.get(args[1])
                return (0, value + "\n", "") if value else (1, "", f"{args[1]}: not set")
            self.names[args[1]] = args[2]
            return 0, "", ""
        if name == "systemsetup":
            self.zone_link(args[1])
            return 0, "", ""
        if name == "pmset":
            if args == ["-g", "custom"]:
                body = "".join(f" {k} {v}\n" for k, v in self.pm.items())
                return 0, "AC Power:\n Sleep On Power Button 1\n" + body, ""
            for key, value in zip(args[1::2], args[2::2]):
                self.pm[key] = value
            return 0, "", ""
        if name == "launchctl":
            if args == ["print-disabled", "system"]:
                state = "enabled" if self.sshd["enabled"] else "disabled"
                return 0, f'disabled services = {{\n\t"{remotelogin.SSHD}" => {state}\n}}\n', ""
            if args[0] == "print":
                return (0, "", "") if self.sshd["loaded"] else (113, "", "not found")
            key = {"enable": "enabled", "disable": "enabled", "bootstrap": "loaded"}
            key["bootout"] = "loaded"
            self.sshd[key[args[0]]] = args[0] in ("enable", "bootstrap")
            return 0, "", ""
        if name == "defaultbrowser":
            if not args:
                out = "".join(
                    ("* " if b == self.browser else "  ") + b + "\n" for b in self.browsers
                )
                return 0, out, ""
            self.browser = args[0]
            return 0, "", ""
        raise AssertionError(f"unexpected {cmd}")

    def writes(self):
        reads = (["--get"], ["-g", "custom"], ["print-disabled", "system"], ["print"])
        return [
            [os.path.basename(c[0]), *c[1:]]
            for c in self.calls
            if not any(c[1 : 1 + len(r)] == r for r in reads) and c[1:]
        ]


class SubcommandTestBase(unittest.TestCase):
    def setUp(self):
        self.mac = FakeMac()
        self.root = True
        self.zones = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.zones))
        for zone, data in (
            ("Europe/Zaporozhye", b"zone"),
            ("Europe/Uzhgorod", b"zone"),
            ("Europe/Berlin", b"b"),
        ):
            path = os.path.join(self.zones, zone)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        self.link = os.path.join(self.zones, "localtime")
        self.mac.zone_link = self.set_zone
        self.set_zone("Europe/Zaporozhye")
        patches = [
            mock.patch.object(timezone, "ZONEINFO", self.zones),
            mock.patch.object(timezone, "LOCALTIME", self.link),
            mock.patch.object(
                defaultbrowser, "defaultbrowser_cli", lambda: "/opt/homebrew/bin/defaultbrowser"
            ),
        ]
        for mod in MODULES:
            patches.append(mock.patch.object(mod, "run_cmd", self.mac.run))
            patches.append(mock.patch.object(mod, "is_macos", lambda: True))
        patches.append(mock.patch("common.is_root", lambda: self.root))
        patches.append(mock.patch.object(defaultbrowser, "is_root", lambda: self.root))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def set_zone(self, zone):
        if os.path.lexists(self.link):
            os.unlink(self.link)
        os.symlink(f"/var/db/timezone/zoneinfo/{zone}", self.link)

    def cli(self, *args):
        result = CliRunner().invoke(settings.cli, list(args))
        return result.exit_code, result.output


class HostnameTest(SubcommandTestBase):
    def test_get_json(self):
        del self.mac.names["ComputerName"]
        rc, out = self.cli("hostname", "get", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(
            json.loads(out), {"hostName": "mac", "localHostName": "mac", "computerName": None}
        )

    def test_set_only_what_differs(self):
        rc, _ = self.cli("hostname", "set", "--host-name", "mac", "--local-host-name", "new")
        self.assertEqual(rc, 0)
        self.assertEqual(self.mac.writes(), [["scutil", "--set", "LocalHostName", "new"]])

    def test_set_needs_root_only_on_drift(self):
        self.root = False
        self.assertEqual(self.cli("hostname", "set", "--host-name", "mac")[0], 0)
        rc, out = self.cli("hostname", "set", "--host-name", "other")
        self.assertEqual(rc, 1)
        self.assertIn("needs root", out)
        self.assertEqual(self.mac.writes(), [])

    def test_invalid_names(self):
        for args in (["--host-name", "bad name"], ["--local-host-name", "a.b"], []):
            with self.subTest(args=args):
                self.assertEqual(self.cli("hostname", "set", *args)[0], 1)
        self.assertEqual(self.mac.writes(), [])


class TimezoneTest(SubcommandTestBase):
    def test_get_matches_linked_names(self):
        rc, out = self.cli("timezone", "get", "Europe/Uzhgorod", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {"timeZone": "Europe/Zaporozhye", "matches": True})
        rc, out = self.cli("timezone", "get", "Europe/Berlin", "--json")
        self.assertEqual(json.loads(out)["matches"], False)

    def test_set_same_zone_is_a_no_op(self):
        self.assertEqual(self.cli("timezone", "set", "Europe/Uzhgorod")[0], 0)
        self.assertEqual(self.mac.writes(), [])

    def test_set(self):
        self.assertEqual(self.cli("timezone", "set", "Europe/Berlin")[0], 0)
        self.assertEqual(self.mac.writes(), [["systemsetup", "-settimezone", "Europe/Berlin"]])

    def test_set_that_does_not_stick_fails(self):
        self.mac.zone_link = lambda _zone: None
        self.assertEqual(self.cli("timezone", "set", "Europe/Berlin")[0], 1)

    def test_unknown_zone(self):
        for zone in ("Mars/Base", "../etc/passwd"):
            with self.subTest(zone=zone):
                self.assertEqual(self.cli("timezone", "get", zone)[0], 1)

    def test_set_needs_root(self):
        self.root = False
        rc, out = self.cli("timezone", "set", "Europe/Berlin")
        self.assertEqual(rc, 1)
        self.assertIn("needs root", out)


class SleepTest(SubcommandTestBase):
    def test_get_json(self):
        rc, out = self.cli("sleep", "get", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(
            json.loads(out),
            {"sources": {"AC Power": {"computer": "never", "display": 10, "disk": "never"}}},
        )

    def test_set_only_drift(self):
        rc, _ = self.cli("sleep", "set", "--computer", "never", "--display", "5")
        self.assertEqual(rc, 0)
        self.assertEqual(self.mac.writes(), [["pmset", "-a", "displaysleep", "5"]])

    def test_bad_values(self):
        for value in ("0", "181", "x"):
            with self.subTest(value=value):
                self.assertEqual(self.cli("sleep", "set", "--display", value)[0], 1)

    def test_mixed_sources(self):
        text = "AC Power:\n sleep 0\nBattery Power:\n sleep 1\n"
        self.assertEqual(
            sleep.sleep_drift(sleep.sleep_parse_pmset(text), {"computer": "never"}),
            {"computer": "never"},
        )


class RemoteLoginTest(SubcommandTestBase):
    def test_get_json(self):
        rc, out = self.cli("remotelogin", "get", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {"remoteLogin": True, "enabled": True, "loaded": True})

    def test_on(self):
        self.mac.sshd = {"enabled": False, "loaded": False}
        self.assertEqual(self.cli("remotelogin", "set", "on")[0], 0)
        self.assertEqual(
            self.mac.writes(),
            [
                ["launchctl", "enable", f"system/{remotelogin.SSHD}"],
                ["launchctl", "bootstrap", "system", remotelogin.SSHD_PLIST],
            ],
        )

    def test_off_also_disables_an_unloaded_sshd(self):
        self.mac.sshd["loaded"] = False
        self.assertEqual(self.cli("remotelogin", "set", "off")[0], 0)
        self.assertEqual(
            self.mac.writes(), [["launchctl", "disable", f"system/{remotelogin.SSHD}"]]
        )

    def test_already_on(self):
        self.root = False
        self.assertEqual(self.cli("remotelogin", "set", "on")[0], 0)
        self.assertEqual(self.mac.writes(), [])


class DefaultBrowserTest(SubcommandTestBase):
    def setUp(self):
        super().setUp()
        self.root = False

    def test_get_json(self):
        rc, out = self.cli("defaultbrowser", "get", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(
            json.loads(out), {"defaultBrowser": "safari", "browsers": ["safari", "firefox"]}
        )

    def test_set(self):
        self.assertEqual(self.cli("defaultbrowser", "set", "firefox")[0], 0)
        self.assertEqual(self.mac.writes(), [["defaultbrowser", "firefox"]])
        self.mac.calls.clear()
        self.assertEqual(self.cli("defaultbrowser", "set", "firefox")[0], 0)
        self.assertEqual(self.mac.writes(), [])

    def test_unknown_browser(self):
        self.assertEqual(self.cli("defaultbrowser", "set", "chrome")[0], 1)
        self.assertEqual(self.mac.writes(), [])

    def test_refuses_root(self):
        self.root = True
        self.assertEqual(self.cli("defaultbrowser", "get")[0], 1)


class ExistingInvocationsTest(unittest.TestCase):
    """Invocations nix-config makes must keep parsing (only parsed, never run)."""

    INVOCATIONS = (
        ["scaling", "--init"],
        ["appearance", "--init"],
        ["location", "--init"],
        ["windows", "quit", "--wait", "10", "Mac Mouse Fix"],
        ["accessibility", "set", "Hammerspoon,Mac Mouse Fix Helper,Muxy,Terminal,kitty"],
        ["fulldiskaccess", "--enable", "bash,kitty,Terminal"],
        ["login", "set", "Hammerspoon,Mac Mouse Fix,LosslessSwitcher"],
        ["daemon", "run", "--autovolume", "--autovolume-idle", "1800"]
        + ["--autovolume-threshold", "6", "--check-interval", "300", "--verbose"],
    )

    def test_parse(self):
        for args in self.INVOCATIONS:
            with self.subTest(args=args):
                group, ctx, rest = settings.cli, None, list(args)
                ctx = group.make_context("settings", rest)
                while True:
                    cmd = group.get_command(ctx, rest[0])
                    self.assertIsNotNone(cmd, rest[0])
                    ctx = cmd.make_context(rest[0], rest[1:], parent=ctx)
                    if not isinstance(cmd, click.Group):
                        break
                    group, rest = cmd, rest[1:]


if __name__ == "__main__":
    unittest.main()
