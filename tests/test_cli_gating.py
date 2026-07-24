import unittest
from unittest import mock

from click.testing import CliRunner

from tools import cli


class DeployGatingTest(unittest.TestCase):
    """bun/npm index paths['bun'] and paths['nodejs'] directly, so calling
    them with nothing configured raised KeyError on a machine whose diff
    reported clean. Every section must be skippable the same way."""

    def _deploy(self, config, state):
        with (
            mock.patch.object(cli, "load_json", return_value=state),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(cli, "save_json"),
        ):
            return cli._deploy(config, "/tmp")

    def test_empty_config_does_not_crash(self):
        # No paths at all: the pre-fix code raised KeyError: 'bun'.
        self.assertTrue(self._deploy({}, {}))

    def test_bun_runs_when_state_has_packages(self):
        state = {"bun": {"packages": {"x": {"installed": True}}}}
        with mock.patch.object(cli, "install_bun_packages", return_value=True) as bun:
            self._deploy({}, state)

        bun.assert_called_once()

    def test_bun_skipped_when_nothing_configured_or_tracked(self):
        with mock.patch.object(cli, "install_bun_packages", return_value=True) as bun:
            self._deploy({}, {})

        bun.assert_not_called()

    def test_npm_skipped_when_nothing_configured_or_tracked(self):
        with mock.patch.object(cli, "install_npm_packages", return_value=True) as npm:
            self._deploy({}, {})

        npm.assert_not_called()

    def test_git_repos_runs_when_only_state_remains(self):
        # Dropping the last entry from config must still reconcile, or the
        # state is orphaned and cleanup never happens.
        state = {"gitRepos": {"installed": ["~/repo"]}}
        with mock.patch.object(cli, "install_git_repos", return_value=True) as repos:
            self._deploy({}, state)

        repos.assert_called_once()

    def test_curl_shell_runs_when_only_state_remains(self):
        state = {"curlShell": {"installed": ["https://example/x.sh"]}}
        with mock.patch.object(cli, "install_curl_shell_scripts", return_value=True) as curl:
            self._deploy({}, state)

        curl.assert_called_once()

    def test_section_failure_propagates(self):
        state = {"gitRepos": {"installed": ["~/repo"]}}
        with mock.patch.object(cli, "install_git_repos", return_value=False):
            self.assertFalse(self._deploy({}, state))


class DeployCleanDiffTest(unittest.TestCase):
    """A clean diff means nothing approval-worthy — not nothing to do.
    Non-destructive maintenance (git pulls) must run on every deploy, not
    only under --approve."""

    def test_clean_diff_still_deploys_without_prompting(self):
        with (
            mock.patch.object(cli, "_load_merged_config", return_value={}),
            mock.patch.object(cli, "show_diff", return_value=True),
            mock.patch.object(cli, "_deploy", return_value=True) as deploy,
            mock.patch.object(cli.click, "prompt") as prompt,
        ):
            result = CliRunner().invoke(cli.main, ["deploy"])

        self.assertEqual(result.exit_code, 0)
        deploy.assert_called_once()
        prompt.assert_not_called()

    def test_dirty_diff_still_requires_approval(self):
        with (
            mock.patch.object(cli, "_load_merged_config", return_value={}),
            mock.patch.object(cli, "show_diff", return_value=False),
            mock.patch.object(cli, "_deploy", return_value=True) as deploy,
            mock.patch.object(cli.click, "prompt", return_value="no"),
        ):
            result = CliRunner().invoke(cli.main, ["deploy"])

        self.assertEqual(result.exit_code, 1)
        deploy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
