import os
import plistlib
import tempfile
import unittest
from unittest import mock

import trash_fake

from tools import privilege
from tools.config import TOP_LEVEL_KEYS
from tools.user import nix_darwin

setUpModule = trash_fake.start
tearDownModule = trash_fake.stop


def write_job(directory, label, args):
    path = os.path.join(directory, f"{label}.plist")
    with open(path, "wb") as f:
        plistlib.dump({"Label": label, "ProgramArguments": args}, f)
    return path


class ParseTest(unittest.TestCase):
    def test_section_known(self):
        self.assertIn("nixDarwin", TOP_LEVEL_KEYS)
        self.assertIn("nixDarwin", privilege.ROOT_SECTIONS)

    def test_parse(self):
        self.assertEqual(nix_darwin.parse(None), (False, []))
        self.assertEqual(nix_darwin.parse({}), (False, []))
        self.assertEqual(nix_darwin.parse({"takeOver": True}), (True, []))
        for raw in ([], {"takeOver": "yes"}, {"other": 1}):
            take, errors = nix_darwin.parse(raw)
            self.assertFalse(take)
            self.assertTrue(errors)


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.etc = os.path.join(root, "etc")
        self.jobs = os.path.join(root, "LaunchDaemons")
        os.makedirs(os.path.join(self.etc, "ssh", "ssh_config.d"))
        os.makedirs(self.jobs)
        os.symlink("/nix/store/abc-etc/etc", os.path.join(self.etc, "static"))
        os.symlink("/etc/static/zshrc", os.path.join(self.etc, "zshrc"))
        with open(os.path.join(self.etc, "zshrc" + nix_darwin.BEFORE), "w") as f:
            f.write("stock\n")
        os.symlink(
            "/etc/static/ssh/ssh_config.d/100-nix-darwin.conf",
            os.path.join(self.etc, "ssh", "ssh_config.d", "100-nix-darwin.conf"),
        )
        os.symlink("/opt/homebrew/etc/other", os.path.join(self.etc, "other"))
        self.nix = write_job(
            self.jobs, "org.nixos.activate-system", ["/bin/sh", "-c", "exec /nix/store/x-start"]
        )
        write_job(self.jobs, "systems.determinate.nix-daemon", ["/usr/local/bin/determinate-nixd"])
        write_job(self.jobs, "com.example.ours", ["/nix/store/y/bin/ours"])

    def plan(self, root=True, state=None):
        return nix_darwin.plan(root, state, etc=self.etc, daemons=self.jobs, agents=self.jobs)

    def test_root_plan(self):
        state = {"launchd": {"services": {"com.example.ours": {}}}}
        self.assertEqual(
            self.plan(state=state),
            [
                ("job", self.nix, "org.nixos.activate-system"),
                (
                    "remove",
                    os.path.join(self.etc, "ssh", "ssh_config.d", "100-nix-darwin.conf"),
                    None,
                ),
                (
                    "restore",
                    os.path.join(self.etc, "zshrc"),
                    os.path.join(self.etc, "zshrc" + nix_darwin.BEFORE),
                ),
                ("remove", os.path.join(self.etc, "static"), None),
            ],
        )

    def test_user_plan_only_jobs(self):
        kinds = {kind for kind, _, _ in self.plan(root=False)}
        self.assertEqual(kinds, {"job"})

    def test_apply_restores_and_trashes(self):
        with mock.patch.object(nix_darwin, "run_command", return_value=(3, "", "not loaded")):
            for action in self.plan(state={"launchd": {"services": {"com.example.ours": {}}}}):
                self.assertIsNone(nix_darwin._do(action, root=True))
        zshrc = os.path.join(self.etc, "zshrc")
        self.assertFalse(os.path.islink(zshrc))
        with open(zshrc) as f:
            self.assertEqual(f.read(), "stock\n")
        self.assertFalse(os.path.lexists(os.path.join(self.etc, "static")))
        self.assertFalse(os.path.exists(self.nix))
        self.assertTrue(os.path.islink(os.path.join(self.etc, "other")))
        self.assertEqual(self.plan(state={"launchd": {"services": {"com.example.ours": {}}}}), [])

    def test_symlink_backup_restored(self):
        link = os.path.join(self.etc, "ssh", "ssh_config.d", "100-nix-darwin.conf")
        os.symlink("/opt/homebrew/etc/orig.conf", link + nix_darwin.BEFORE)
        action = ("restore", link, link + nix_darwin.BEFORE)
        self.assertIn(action, self.plan())
        self.assertIsNone(nix_darwin._do(action, root=True))
        self.assertEqual(os.readlink(link), "/opt/homebrew/etc/orig.conf")
        self.assertFalse(os.path.lexists(link + nix_darwin.BEFORE))

    def test_failed_restore_keeps_link(self):
        link = os.path.join(self.etc, "zshrc")
        action = ("restore", link, link + nix_darwin.BEFORE)
        with mock.patch.object(nix_darwin.os, "rename", side_effect=OSError(13, "denied")):
            self.assertIsNotNone(nix_darwin._do(action, root=True))
        self.assertEqual(os.readlink(link), "/etc/static/zshrc")

    def test_bootout_failure_keeps_plist(self):
        action = ("job", self.nix, "org.nixos.activate-system")
        with mock.patch.object(nix_darwin, "run_command", return_value=(5, "", "boom")):
            self.assertIn("bootout", nix_darwin._do(action, root=True))
        self.assertTrue(os.path.exists(self.nix))

    def test_off_by_default(self):
        self.assertEqual(nix_darwin.diff_nix_darwin({}, root=True), [])
        self.assertTrue(nix_darwin.install_nix_darwin({"takeOver": False}, root=True))


class OnlyTest(unittest.TestCase):
    """--only sets launchd records aside; takeover must still see them as owned."""

    def test_root_deploy_gets_full_state(self):
        from tools import cli

        full = {"launchd": {"services": {"com.example.ours": {}}}}
        with mock.patch.object(cli, "install_nix_darwin", return_value=True) as inst:
            cli._run_root_sections(
                {"nixDarwin": {"takeOver": True}}, "/tmp", {}, {"nixDarwin"}, full_state=full
            )
        self.assertIs(inst.call_args.args[1], full)

    def test_root_diff_gets_full_state(self):
        from tools import diff

        full = {"launchd": {"services": {"com.example.ours": {}}}}
        with mock.patch.object(diff, "diff_nix_darwin", return_value=[]) as d:
            diff._root_sections(
                {"nixDarwin": {"takeOver": True}}, "/tmp", ("nixDarwin",), {}, full_state=full
            )
        self.assertIs(d.call_args.args[1], full)


if __name__ == "__main__":
    unittest.main()
