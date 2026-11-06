import unittest
from unittest import mock

from tools.user.uv import _uv_commit_spec, install_uv_packages

REPO = "git+https://github.com/ivankovnatsky/tools"
PATHS = {"uv": "/uv", "uvBin": "/bin", "uvToolDir": "/tooldir"}


class UvCommitSpecTest(unittest.TestCase):
    def test_plain_source(self):
        self.assertEqual(_uv_commit_spec(REPO, "abc"), f"{REPO}@abc")

    def test_ref_goes_before_fragment(self):
        self.assertEqual(
            _uv_commit_spec(f"{REPO}#subdirectory=packages/gh-pr", "abc"),
            f"{REPO}@abc#subdirectory=packages/gh-pr",
        )

    def test_at_in_path_is_kept(self):
        self.assertEqual(
            _uv_commit_spec("git+https://h/o@x/r#subdirectory=p", "abc"),
            "git+https://h/o@x/r@abc#subdirectory=p",
        )

    def test_local_path_with_at(self):
        self.assertEqual(_uv_commit_spec("/Users/a@b/pkg", "abc"), "/Users/a@b/pkg@abc")

    def test_ssh_userinfo_is_kept(self):
        self.assertEqual(
            _uv_commit_spec("git+ssh://git@github.com/o/r#subdirectory=x", "abc"),
            "git+ssh://git@github.com/o/r@abc#subdirectory=x",
        )


class UvInstallTest(unittest.TestCase):
    def test_installs_fragment_source_with_ref_before_fragment(self):
        source = f"{REPO}#subdirectory=packages/gh-pr"
        packages = {"gh-pr": {"source": source, "commit": "abc"}}
        state = {}
        with mock.patch("tools.user.uv.run_command", return_value=(0, "", "")) as run:
            self.assertTrue(install_uv_packages(packages, PATHS, state))
        cmd = run.call_args[0][0]
        self.assertEqual(
            cmd, ["/uv/uv", "tool", "install", f"{REPO}@abc#subdirectory=packages/gh-pr"]
        )
        self.assertEqual(state["uv"]["packages"]["gh-pr"]["source"], source)
        self.assertEqual(state["uv"]["packages"]["gh-pr"]["commit"], "abc")

    def test_unchanged_pinned_package_is_not_reinstalled(self):
        source = f"{REPO}#subdirectory=packages/gh-pr"
        packages = {"gh-pr": {"source": source, "commit": "abc"}}
        state = {
            "uv": {
                "context": "/bin|/tooldir",
                "packages": {
                    "gh-pr": {
                        "installed": True,
                        "version": "latest",
                        "source": source,
                        "commit": "abc",
                    }
                },
            }
        }
        with mock.patch("tools.user.uv.run_command") as run:
            self.assertTrue(install_uv_packages(packages, PATHS, state))
        run.assert_not_called()


class UvUpdateTest(unittest.TestCase):
    def test_unpinned_packages_upgrade_only_with_update(self):
        packages = {"loose": {}, "pinned": {"version": "1.0.0"}}
        for update, expected in ((False, []), (True, [["/uv/uv", "tool", "upgrade", "loose"]])):
            state = {"uv": {"context": "/bin|/tooldir", "packages": {}}}
            for pkg, info in packages.items():
                state["uv"]["packages"][pkg] = {
                    "installed": True,
                    "version": "latest",
                    "source": "",
                    **info,
                }
            with mock.patch("tools.user.uv.run_command", return_value=(0, "", "")) as run:
                self.assertTrue(install_uv_packages(packages, PATHS, state, update))
            self.assertEqual([c.args[0] for c in run.call_args_list], expected)


if __name__ == "__main__":
    unittest.main()
