import os
import tempfile
import unittest
from unittest import mock

from tools import cli
from tools.user import links


class LinksTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # realpath: macOS tmp lives under a symlinked /var.
        self.tmp = os.path.realpath(self._tmp.name)
        self.src = os.path.join(self.tmp, "agents", "skills", "jira")
        self.src2 = os.path.join(self.tmp, "agents", "skills", "jira2")
        os.makedirs(self.src)
        os.makedirs(self.src2)
        self.target = os.path.join(self.tmp, "home", ".claude", "skills", "jira")
        self.state = {}

    def tearDown(self):
        self._tmp.cleanup()

    def entry(self, source=None, target=None):
        return {"source": source or self.src, "target": target or self.target}

    def deploy(self, entries):
        return links.install_links(entries, self.state)

    def diff(self, entries):
        return links.diff_links(entries, self.state)

    def installed(self):
        return self.state["links"]["installed"]


class LinkTest(LinksTestBase):
    def test_creates_absolute_link_with_parents_and_is_idempotent(self):
        entries = [self.entry()]
        self.assertEqual(self.diff(entries), [f"  + link {self.target} -> {self.src}"])

        self.assertTrue(self.deploy(entries))

        self.assertEqual(os.readlink(self.target), self.src)
        self.assertEqual(self.installed(), {self.target: {"source": self.src}})
        self.assertEqual(self.diff(entries), [])

    def test_expands_tilde_and_vars(self):
        with mock.patch.dict(os.environ, {"HOME": self.tmp, "SRC": self.src}):
            entries = [{"source": "$SRC", "target": "~/home/.claude/skills/jira"}]
            self.assertTrue(self.deploy(entries))

        self.assertEqual(os.readlink(self.target), self.src)

    def test_source_change_repoints_tracked_link(self):
        self.deploy([self.entry()])
        entries = [self.entry(source=self.src2)]

        self.assertEqual(self.diff(entries), [f"  ~ relink {self.target} -> {self.src2}"])
        self.assertTrue(self.deploy(entries))

        self.assertEqual(os.readlink(self.target), self.src2)
        self.assertEqual(self.installed()[self.target]["source"], self.src2)

    def test_dropped_link_is_removed(self):
        self.deploy([self.entry()])

        self.assertEqual(self.diff([]), [f"  - remove {self.target}"])
        self.assertTrue(links.has_work([], self.state))
        self.assertTrue(self.deploy([]))

        self.assertFalse(os.path.lexists(self.target))
        self.assertTrue(os.path.isdir(self.src))
        self.assertEqual(self.installed(), {})

    def test_missing_source_is_skipped_not_linked(self):
        missing = os.path.join(self.tmp, "nope")
        entries = [self.entry(source=missing)]

        self.assertIn("not found", self.diff(entries)[0])
        self.assertTrue(self.deploy(entries))

        self.assertFalse(os.path.lexists(self.target))
        self.assertEqual(self.installed(), {})


class OwnershipTest(LinksTestBase):
    def test_existing_dir_is_never_replaced(self):
        os.makedirs(self.target)

        self.assertIn("not a symlink", self.diff([self.entry()])[0])
        self.deploy([self.entry()])

        self.assertTrue(os.path.isdir(self.target) and not os.path.islink(self.target))
        self.assertEqual(self.installed(), {})

    def test_link_owned_by_skills_is_skipped(self):
        os.makedirs(os.path.dirname(self.target))
        os.symlink(self.src, self.target)
        self.state["skills"] = {"links": {self.target: {"skill": "x/jira", "source": self.src}}}

        self.assertEqual(
            self.diff([self.entry()]),
            [f"  ! skip {self.target}: managed by skills.linkTargets"],
        )
        self.deploy([self.entry()])
        self.assertEqual(self.installed(), {})

    def test_foreign_symlink_is_never_replaced(self):
        os.makedirs(os.path.dirname(self.target))
        os.symlink(self.src2, self.target)

        self.assertIn("not managed by tools", self.diff([self.entry()])[0])
        self.deploy([self.entry()])

        self.assertEqual(os.readlink(self.target), self.src2)
        self.assertEqual(self.installed(), {})

    def test_relative_link_to_same_source_is_adopted(self):
        os.makedirs(os.path.dirname(self.target))
        os.symlink("../../../agents/skills/jira", self.target)

        self.assertEqual(self.diff([self.entry()]), [])
        self.assertTrue(self.deploy([self.entry()]))

        self.assertEqual(os.readlink(self.target), "../../../agents/skills/jira")
        self.assertEqual(self.installed(), {self.target: {"source": self.src}})

    def test_link_repointed_by_hand_is_not_updated(self):
        self.deploy([self.entry()])
        os.remove(self.target)
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        os.symlink(other, self.target)

        self.assertIn("changed since tools made it", self.diff([self.entry(self.src2)])[0])
        self.deploy([self.entry(self.src2)])

        self.assertEqual(os.readlink(self.target), other)

    def test_dropped_link_repointed_by_hand_is_released_not_removed(self):
        self.deploy([self.entry()])
        os.remove(self.target)
        os.symlink(self.src2, self.target)

        self.assertTrue(self.deploy([]))

        self.assertEqual(os.readlink(self.target), self.src2)
        self.assertEqual(self.installed(), {})

    def test_link_into_what_source_resolves_to_is_foreign(self):
        # Source is itself a link (e.g. into the nix store); a hand-made link
        # straight at the store path resolves the same but is not ours.
        store = os.path.join(self.tmp, "store", "jira")
        os.makedirs(store)
        source = os.path.join(self.tmp, "agents", "skills", "linked")
        os.symlink(store, source)
        os.makedirs(os.path.dirname(self.target))
        os.symlink(store, self.target)

        self.deploy([self.entry(source=source)])

        self.assertEqual(os.readlink(self.target), store)
        self.assertEqual(self.installed(), {})

    def test_malformed_state_entry_is_not_trusted(self):
        os.makedirs(os.path.dirname(self.target))
        os.symlink(self.src2, self.target)
        self.state = {"links": {"installed": {self.target: self.src2}}}

        self.assertIn("not managed by tools", self.diff([self.entry()])[0])
        self.assertTrue(self.deploy([]))
        self.assertEqual(os.readlink(self.target), self.src2)

    def test_config_error_holds_removals(self):
        self.deploy([self.entry()])
        entries = [{"source": "relative", "target": "also-relative"}]

        self.assertFalse(self.deploy(entries))
        self.assertTrue(os.path.islink(self.target))
        self.assertNotIn(f"  - remove {self.target}", self.diff(entries))


class ConfigErrorTest(LinksTestBase):
    def assertErrors(self, entries, fragment):
        changes = self.diff(entries)
        self.assertTrue(any(fragment in c for c in changes), changes)
        self.assertFalse(self.deploy(entries))

    def test_relative_paths_rejected(self):
        self.assertErrors([{"source": "x", "target": self.target}], "must be absolute")

    def test_duplicate_target_rejected(self):
        self.assertErrors([self.entry(), self.entry(self.src2)], "declared more than once")

    def test_missing_keys_rejected(self):
        self.assertErrors([{"source": self.src}], "string `source` and `target`")

    def test_unknown_keys_rejected(self):
        self.assertErrors([dict(self.entry(), mode="0644")], "unknown keys")

    def test_not_a_list(self):
        self.assertErrors({"source": self.src}, "must be a list")


class GatingTest(unittest.TestCase):
    def _deploy(self, config, state):
        with (
            mock.patch.object(cli, "load_json", return_value=state),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(cli, "save_json"),
            mock.patch.object(cli, "install_links", return_value=True) as install,
        ):
            cli._deploy(config, "/tmp", ("links",))
        return install

    def test_skipped_when_nothing_configured_or_tracked(self):
        self._deploy({}, {}).assert_not_called()

    def test_runs_when_only_state_remains(self):
        state = {"links": {"installed": {"/x/jira": {"source": "/y/jira"}}}}
        self._deploy({}, state).assert_called_once()


if __name__ == "__main__":
    unittest.main()
