import os
import tempfile
import unittest
from unittest import mock

from tools import diff as diff_mod
from tools.user import git_repos


class FakeGit:
    """Stand-in for git, with per-repo dirty/unpushed state."""

    def __init__(self, dirty=(), unpushed=(), failing_status=()):
        self.dirty = set(dirty)
        self.unpushed = set(unpushed)
        self.failing_status = set(failing_status)
        self.commands = []

    def run(self, cmd, env=None, cwd=None):
        self.commands.append(cmd)
        # ["git", "-C", <path>, <verb>, ...]
        path = cmd[2] if len(cmd) > 2 and cmd[1] == "-C" else ""
        verb = cmd[3] if len(cmd) > 3 and cmd[1] == "-C" else cmd[1]
        if verb == "status":
            if path in self.failing_status:
                return 1, "", "not a repo"
            return 0, (" M file.txt" if path in self.dirty else ""), ""
        if verb == "log":
            return 0, ("abc1234 local commit" if path in self.unpushed else ""), ""
        return 0, "", ""


class GitReposRemovalTest(unittest.TestCase):
    def _reconcile(self, repos, state, fake, removed):
        with (
            mock.patch.object(git_repos, "system_bin", return_value="git"),
            mock.patch.object(git_repos, "system_dir", return_value="/usr/bin"),
            mock.patch.object(git_repos, "run_command", fake.run),
            mock.patch.object(git_repos.shutil, "rmtree", side_effect=removed.append),
        ):
            return git_repos.install_git_repos(repos, state)

    def test_clean_repo_dropped_from_config_is_removed(self):
        with tempfile.TemporaryDirectory() as d:
            state = {"gitRepos": {"installed": [d]}}
            removed = []

            self._reconcile({}, state, FakeGit(), removed)

            self.assertEqual(removed, [d])
            self.assertEqual(state["gitRepos"]["installed"], [])

    def test_repo_with_uncommitted_work_is_kept(self):
        # Removal is an rmtree; uncommitted work would be unrecoverable.
        with tempfile.TemporaryDirectory() as d:
            state = {"gitRepos": {"installed": [d]}}
            removed = []

            self._reconcile({}, state, FakeGit(dirty=[d]), removed)

            self.assertEqual(removed, [])

    def test_repo_with_unpushed_commits_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            state = {"gitRepos": {"installed": [d]}}
            removed = []

            self._reconcile({}, state, FakeGit(unpushed=[d]), removed)

            self.assertEqual(removed, [])

    def test_unreadable_repo_is_kept(self):
        # A path git cannot interrogate counts as having work: refusing to
        # delete costs a stale directory, deleting costs the work.
        with tempfile.TemporaryDirectory() as d:
            state = {"gitRepos": {"installed": [d]}}
            removed = []

            self._reconcile({}, state, FakeGit(failing_status=[d]), removed)

            self.assertEqual(removed, [])

    def test_untracked_directory_is_never_removed(self):
        with tempfile.TemporaryDirectory() as d:
            # Present on disk, absent from state: not ours.
            state = {"gitRepos": {"installed": []}}
            removed = []

            self._reconcile({}, state, FakeGit(), removed)

            self.assertEqual(removed, [])
            self.assertTrue(os.path.isdir(d))

    def test_dropping_last_repo_still_reconciles(self):
        # cli gates on config-or-state; the reconciler must not early-return
        # on an empty config while state still tracks something.
        with tempfile.TemporaryDirectory() as d:
            state = {"gitRepos": {"installed": [d]}}
            removed = []

            self._reconcile({}, state, FakeGit(), removed)

            self.assertEqual(removed, [d])


class GitReposUpdateTest(unittest.TestCase):
    def _reconcile(self, repos, state, fake):
        with (
            mock.patch.object(git_repos, "system_bin", return_value="git"),
            mock.patch.object(git_repos, "system_dir", return_value="/usr/bin"),
            mock.patch.object(git_repos, "run_command", fake.run),
        ):
            return git_repos.install_git_repos(repos, state)

    def test_already_tracked_repo_is_pulled(self):
        # "Installed once" is not "up to date"; nothing else revisits these.
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".git"))
            state = {"gitRepos": {"installed": [d]}}
            fake = FakeGit()

            self._reconcile({d: "https://example/r.git"}, state, fake)

            self.assertTrue(any(c[3:4] == ["pull"] for c in fake.commands), fake.commands)

    def test_non_repo_directory_is_not_marked_installed(self):
        # Marking it installed would make it an rmtree candidate later.
        with tempfile.TemporaryDirectory() as d:
            state = {}
            fake = FakeGit()

            self.assertFalse(self._reconcile({d: "https://example/r.git"}, state, fake))

            self.assertNotIn(d, state.get("gitRepos", {}).get("installed", []))


class GitReposDiffTest(unittest.TestCase):
    def test_removal_is_previewed(self):
        # rmtree with no preview line is the one missing diff entry that
        # costs data rather than clarity.
        state = {"gitRepos": {"installed": ["~/repo"]}}

        changes = diff_mod._diff_git_repos({}, state)

        self.assertTrue(any(c.startswith("  - remove ~/repo") for c in changes), changes)

    def test_untracked_repo_is_not_previewed_for_removal(self):
        changes = diff_mod._diff_git_repos({}, {"gitRepos": {"installed": []}})

        self.assertEqual(changes, [])

    def test_configured_repo_is_not_previewed_for_removal(self):
        with tempfile.TemporaryDirectory() as d:
            state = {"gitRepos": {"installed": [d]}}

            changes = diff_mod._diff_git_repos({d: "https://example/r.git"}, state)

            self.assertFalse(any("remove" in c for c in changes), changes)

    def test_missing_checkout_is_previewed_as_clone(self):
        changes = diff_mod._diff_git_repos(
            {os.path.join(tempfile.gettempdir(), "no-such-repo"): "https://example/r.git"}, {}
        )

        self.assertTrue(any(c.startswith("  + clone") for c in changes), changes)


if __name__ == "__main__":
    unittest.main()
