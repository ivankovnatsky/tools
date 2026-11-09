import os
import unittest
from unittest import mock

from test_dock import DockTestBase

from tools import cli as cli_mod
from tools import diff as diff_mod
from tools.user import defaults, dock

DOCK_KEY = {"user": {"com.apple.dock": {"tilesize": 64}}, "restart": {"com.apple.dock": "Dock"}}


class SharedRestartTest(DockTestBase):
    def setUp(self):
        super().setUp()
        patches = [
            mock.patch.object(defaults.sys, "platform", "darwin"),
            mock.patch.object(defaults.DefaultsCLI, "for_host", side_effect=self._cli),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _cli(self):
        return self.fake.cli()

    def run_deploy(self, config):
        return cli_mod._run_sections(config, self.tmp, self.state, {}, set(config))

    def run_diff(self, config):
        config = {"stateFile": os.path.join(self.tmp, "state.json"), **config}
        rows = []
        titles = []
        real_log = diff_mod.log

        def log(msg, color=None):
            titles.append(msg.strip())
            real_log(msg, color)

        with (
            mock.patch.object(diff_mod, "_emit_change", rows.append),
            mock.patch.object(diff_mod, "log", log),
        ):
            diff_mod.show_diff(config, self.tmp, tuple(k for k in config if k != "stateFile"))
        return rows, titles

    def mutations(self):
        return self.fake.writes()

    def test_both_sections_kill_dock_once_after_all_writes(self):
        config = {
            "defaults": {
                "user": {"com.apple.dock": {"tilesize": 64}, "com.example.app": {"k": 1}},
                "restart": {"com.apple.dock": "Dock"},
            },
            "dock": {"apps": [self.mail], "restart": True},
        }
        self.assertTrue(self.run_deploy(config))
        self.assertEqual(
            self.mutations(),
            [
                ("write", "com.apple.dock", "tilesize"),
                ("write", "com.example.app", "k"),
                ("write", "com.apple.dock", "persistent-apps"),
                ("killall", "Dock"),
            ],
        )

    def test_only_defaults(self):
        self.assertTrue(self.run_deploy({"defaults": DOCK_KEY}))
        self.assertEqual(
            self.mutations(), [("write", "com.apple.dock", "tilesize"), ("killall", "Dock")]
        )

    def test_only_dock(self):
        self.assertTrue(self.run_deploy({"dock": {"apps": [self.mail], "restart": True}}))
        self.assertEqual(
            self.mutations(), [("write", "com.apple.dock", "persistent-apps"), ("killall", "Dock")]
        )

    def test_dock_restart_false_does_not_suppress_defaults_restart(self):
        config = {"defaults": DOCK_KEY, "dock": {"apps": [self.mail], "restart": False}}
        self.assertTrue(self.run_deploy(config))
        self.assertEqual(
            self.mutations(),
            [
                ("write", "com.apple.dock", "tilesize"),
                ("write", "com.apple.dock", "persistent-apps"),
                ("killall", "Dock"),
            ],
        )

    def test_dock_restart_false_alone_kills_nothing(self):
        self.run_deploy({"defaults": {}, "dock": {"apps": [self.mail], "restart": False}})
        self.assertNotIn(("killall", "Dock"), self.mutations())

    def test_held_section_queues_nothing(self):
        config = {
            "defaults": {**DOCK_KEY, "restrat": {}},
            "dock": {"apps": [self.mail], "restart": True},
        }
        self.assertFalse(self.run_deploy(config))
        self.assertEqual(
            self.mutations(), [("write", "com.apple.dock", "persistent-apps"), ("killall", "Dock")]
        )

        self.fake.calls.clear()
        config = {"defaults": DOCK_KEY, "dock": {"apps": [self.safari], "restart": "no"}}
        self.assertFalse(self.run_deploy(config))
        self.assertEqual(
            self.mutations(), [("write", "com.apple.dock", "tilesize"), ("killall", "Dock")]
        )

    def test_both_held_kills_nothing(self):
        config = {"defaults": {**DOCK_KEY, "restrat": {}}, "dock": {"apps": [self.mail], "x": 1}}
        self.assertFalse(self.run_deploy(config))
        self.assertEqual(self.mutations(), [])

    def test_failed_write_skips_only_its_restart(self):
        config = {
            "defaults": {**DOCK_KEY, "restart": {"com.apple.dock": ["Dock", "Finder"]}},
            "dock": {"apps": [self.mail], "restart": True},
        }
        real = self.fake.run

        def run(cmd, env=None, cwd=None):
            if cmd[1:3] == ["write", "com.apple.dock"] and cmd[3] == "tilesize":
                self.fake.calls.append(list(cmd))
                return 1, "", "boom"
            return real(cmd, env, cwd)

        self.fake.run = run
        self.assertFalse(self.run_deploy(config))
        kills = [v for v in self.mutations() if v[0] == "killall"]
        self.assertEqual(kills, [("killall", "Dock")])
        self.assertEqual(self.mutations()[-1], ("killall", "Dock"))

    def test_dock_build_crash_still_restarts_for_defaults_write(self):
        config = {"defaults": DOCK_KEY, "dock": {"apps": [self.mail], "restart": True}}
        with mock.patch.object(dock, "build_tile", side_effect=RuntimeError("bad tile")):
            self.assertFalse(self.run_deploy(config))
        self.assertEqual(
            self.mutations(), [("write", "com.apple.dock", "tilesize"), ("killall", "Dock")]
        )
        self.assertNotIn("persistent-apps", self.state.get("dock", {}).get("sections", {}))

    def _crash_on_second_write(self):
        real = self.fake.run
        writes = []

        def run(cmd, env=None, cwd=None):
            if cmd[1:2] == ["write"]:
                writes.append(cmd)
                if len(writes) == 2:
                    raise RuntimeError("defaults crashed")
            return real(cmd, env, cwd)

        self.fake.run = run

    def test_crashed_write_still_queues_its_restart(self):
        config = {
            "defaults": {
                "user": {"com.apple.dock": {"tilesize": 64}, "com.apple.finder": {"k": 1}},
                "restart": {"com.apple.dock": "Dock", "com.apple.finder": "Finder"},
            },
            "dock": {"apps": [self.mail], "restart": True},
        }
        self._crash_on_second_write()
        self.assertFalse(self.run_deploy(config))
        self.assertEqual(
            self.mutations(),
            [
                ("write", "com.apple.dock", "tilesize"),
                ("write", "com.apple.dock", "persistent-apps"),
                ("killall", "Dock"),
                ("killall", "Finder"),
            ],
        )

    def test_section_crash_still_runs_queued_restarts(self):
        with mock.patch.object(cli_mod, "install_dock", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.run_deploy(
                    {"defaults": DOCK_KEY, "dock": {"apps": [self.mail], "restart": True}}
                )
        self.assertEqual(
            self.mutations(), [("write", "com.apple.dock", "tilesize"), ("killall", "Dock")]
        )

    def test_interrupt_runs_queued_restarts_and_propagates(self):
        config = {
            "defaults": {
                "user": {"com.apple.dock": {"tilesize": 64}, "com.apple.finder": {"k": 1}},
                "restart": {"com.apple.dock": "Dock", "com.apple.finder": "Finder"},
            },
            "dock": {"apps": [self.mail], "restart": True},
        }
        real = self.fake.run

        def run(cmd, env=None, cwd=None):
            if "com.apple.finder" in cmd and "write" in cmd:
                raise KeyboardInterrupt
            return real(cmd, env, cwd)

        self.fake.run = run
        with self.assertRaises(KeyboardInterrupt):
            self.run_deploy(config)
        self.assertEqual(
            self.mutations(),
            [("write", "com.apple.dock", "tilesize"), ("killall", "Dock"), ("killall", "Finder")],
        )
        slot = self.state["defaults"]["managed"]["user"]["com.apple.finder"]["k"]
        self.assertEqual(defaults.from_xml(slot["written"]), 1)

    def test_standalone_apply_restarts_after_crashed_write(self):
        self._crash_on_second_write()
        spec, errors = defaults.parse_defaults(
            {**DOCK_KEY, "user": {"com.apple.dock": {"tilesize": 64}, "x.y": {"k": 1}}}
        )
        self.assertFalse(defaults.apply_defaults(spec, errors, self.state, self.fake.cli()))
        self.assertEqual([v for v in self.mutations() if v[0] == "killall"], [("killall", "Dock")])

    def test_diff_shows_restart_once_and_kills_nothing(self):
        config = {"defaults": DOCK_KEY, "dock": {"apps": [self.mail], "restart": True}}
        rows, titles = self.run_diff(config)
        self.assertEqual([r for r in rows if "restart" in r], ["  ~ restart Dock"])
        self.assertEqual(titles[-1], "restart:")
        self.assertEqual(self.mutations(), [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "state.json")))


if __name__ == "__main__":
    unittest.main()
