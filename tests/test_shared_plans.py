"""Deploy acts on the same plan the preview renders, section by section."""

import contextlib
import copy
import io
import os
import tempfile
import unittest
from unittest import mock

from tools import diff as diff_mod
from tools.user import bun, cargo, git_repos, go, mcp, npm, pi, uv
from tools.util import PACKAGE_ENTRY_KEYS

UNIT = "launchd:org.example.svc"
BAD_UNITS = {"restartUnits": "launchd:x"}


def _quiet(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **k)


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, env=None, cwd=None):
        self.calls.append(list(cmd))
        return 0, "", ""


class SharedPlanTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = os.path.realpath(self._tmp.name)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)

    def patch(self, *args, **kwargs):
        patcher = mock.patch.object(*args, **kwargs)
        patched = patcher.start()
        self.addCleanup(patcher.stop)
        return patched

    def spy(self, module, *names):
        """Record what each named plan function returns while deploy runs."""
        seen = {}
        for name in names:
            real = getattr(module, name)

            def wrapper(*a, _real=real, _name=name, **k):
                result = _real(*a, **k)
                seen.setdefault(_name, []).append(copy.deepcopy(result))
                return result

            self.patch(module, name, wrapper)
        return seen

    def merged(self, seen):
        self.assertEqual([len(v) for v in seen.values()], [1] * len(seen))
        out = {}
        for results in seen.values():
            out.update(results[0])
        return out

    def packages(self, section=None):
        packages = {
            "keep": {"binary": "k"},
            "bumped": {"version": "2", "restartUnits": [UNIT]},
            "loose": {},
            "new": {},
            "bad": BAD_UNITS,
        }
        if section is None:
            return packages
        # Only the keys the section reads: the rest are config errors.
        allowed = PACKAGE_ENTRY_KEYS[section]
        return {n: {k: v for k, v in i.items() if k in allowed} for n, i in packages.items()}

    def tracked(self):
        return {
            "keep": {"installed": True},
            "bumped": {"installed": True, "version": "1"},
            "loose": {"installed": True},
            "bad": {"installed": True},
            "old": {"installed": True},
        }

    def assert_same_plan(self, module, run, packages, paths, state, names, plan_args=None):
        expected = module.plan(*(plan_args or (packages, paths, copy.deepcopy(state), True)))
        seen = self.spy(module, *names)
        _quiet(run)
        self.assertEqual(self.merged(seen), expected)
        return expected

    def acted_on(self, plan):
        return [pkg for pkg, _ in plan["actions"] if pkg not in plan["unit_errors"]]

    def test_uv(self):
        paths = {"uv": "/uv", "uvBin": self.bin, "uvToolDir": os.path.join(self.tmp, "t")}
        state = {"uv": {"context": f"{self.bin}|{paths['uvToolDir']}", "packages": self.tracked()}}
        packages = self.packages()
        rows = diff_mod._diff_uv(packages, paths, copy.deepcopy(state), True)
        rec = self.patch(uv, "run_command", Recorder())
        p = self.assert_same_plan(
            uv,
            lambda: uv.install_uv_packages(packages, paths, state, True),
            packages,
            paths,
            state,
            ("plan_removals", "plan_installs"),
        )
        self.assertEqual(
            [p[k] for k in ("actions", "remove")],
            [
                [("keep", "reinstall"), ("bumped", "update"), ("loose", "upgrade")]
                + [("new", "install"), ("bad", "upgrade")],
                ["old"],
            ],
        )
        acted = [c[3] for c in rec.calls]
        self.assertEqual(acted, ["old", "keep", "bumped@2", "new", "loose"])
        self.assertEqual(
            rows,
            [
                "  ~ reinstall keep (binary k missing)",
                "  ~ update bumped",
                f"    notify: {UNIT}",
                "  ~ upgrade loose",
                "  + install new",
                "  ! uv.packages.bad: restartUnits must be a list of strings",
                "  - remove old",
            ],
        )

    def test_npm_and_bun(self):
        for module, section, key, run in (
            (npm, "npm", "npmBin", npm.install_npm_packages),
            (bun, "bun", "bunBin", bun.install_bun_packages),
        ):
            with self.subTest(section):
                paths = {key: self.bin, "nodejs": "/node", "bun": "/bun"}
                state = {section: {"prefix": self.bin, "packages": self.tracked()}}
                packages = self.packages(section)
                rec = Recorder()
                with mock.patch.object(module, "run_command", rec):
                    p = self.assert_same_plan(
                        module,
                        lambda: run(packages, paths, state, {}, True),
                        packages,
                        paths,
                        state,
                        ("plan_removals", "plan_installs"),
                    )
                self.assertEqual(p["remove"], ["old"])
                install = next(c for c in rec.calls if "install" in c)
                self.assertEqual([spec.split("@")[0] for spec in install[3:]], self.acted_on(p))

    def test_go(self):
        old = os.path.join(self.bin, "old")
        tracked = self.tracked()
        tracked["old"] = {"installed": True, "path": old}
        paths = {"goBin": self.bin}
        state = {"go": {"context": f"{self.bin}|", "packages": tracked}}
        packages = self.packages()
        rows = diff_mod._diff_go(packages, paths, copy.deepcopy(state), True)
        rec = self.patch(go, "run_command", Recorder())
        self.patch(go.shutil, "which", return_value="/go")
        trash = self.patch(go.fsutil, "trash")
        p = self.assert_same_plan(
            go,
            lambda: go.install_go_packages(packages, paths, state, True),
            packages,
            paths,
            state,
            ("plan_removals", "plan_installs"),
        )
        self.assertEqual(p["removals"], [("old", "remove", old)])
        trash.assert_called_once_with(old)
        self.assertEqual([c[2].split("@")[0] for c in rec.calls], self.acted_on(p))
        self.assertIn("  - remove old", rows)

    def test_cargo(self):
        paths = {"cargoRoot": self.tmp}
        state = {"cargo": {"context": f"{self.tmp}|", "root": self.tmp, "packages": self.tracked()}}
        packages = self.packages("cargo")
        rows = diff_mod._diff_cargo(packages, paths, copy.deepcopy(state), True)
        rec = self.patch(cargo, "run_command", Recorder())
        self.patch(cargo.shutil, "which", return_value="/cargo")
        p = self.assert_same_plan(
            cargo,
            lambda: cargo.install_cargo_packages(packages, paths, state, True),
            packages,
            paths,
            state,
            ("plan",),
        )
        self.assertEqual(rec.calls[0], ["/cargo", "uninstall", "old"])
        self.assertEqual([c[2] for c in rec.calls[1:]], self.acted_on(p))
        self.assertIn("  - remove old", rows)

    def test_pi(self):
        state = {"pi": {"packages": self.tracked()}}
        packages = {k: v for k, v in self.packages("pi").items() if k != "bad"}
        rows = diff_mod._diff_pi(packages, {}, copy.deepcopy(state))
        rec = self.patch(pi, "run_command", Recorder())
        self.patch(pi, "resolve_pi_cli", return_value="/pi")
        p = self.assert_same_plan(
            pi,
            lambda: pi.install_pi_packages(packages, {}, state),
            packages,
            {},
            state,
            ("plan",),
            plan_args=(packages, copy.deepcopy(state)),
        )
        self.assertEqual(rec.calls[:2], [["/pi", "remove", "bad"], ["/pi", "remove", "old"]])
        self.assertEqual([c[2] for c in rec.calls[2:]], ["bumped@2", "new"])
        self.assertEqual(p["actions"], [("bumped", "update"), ("new", "install")])
        self.assertEqual(
            rows, ["  - remove bad", "  - remove old", "  ~ update bumped", "  + install new"]
        )

    def test_mcp(self):
        def server(url):
            return {"scope": "user", "transport": "http", "url": url}

        servers = {"new": server("n"), "changed": server("c2"), "adopt": server("a")}
        state = {
            "mcp": {"servers": {"changed": server("c1"), "gone": server("g"), "lost": server("l")}}
        }
        current = {"changed", "adopt", "gone"}
        self.patch(mcp, "resolve_claude_cli", return_value="/claude")
        self.patch(mcp, "get_installed_mcp_servers", return_value=set(current))
        rows = diff_mod._diff_mcp(servers, {}, copy.deepcopy(state))
        rec = self.patch(mcp, "run_command", Recorder())
        p = self.assert_same_plan(
            mcp,
            lambda: mcp.install_mcp_servers(servers, {}, state),
            servers,
            {},
            state,
            ("plan",),
            plan_args=(servers, copy.deepcopy(state), current),
        )
        removed = sorted(c[3] for c in rec.calls if c[2] == "remove")
        added = sorted(c[-2] for c in rec.calls if c[2] == "add")
        self.assertEqual(removed, sorted(p["to_remove"]))
        self.assertEqual(added, sorted(p["to_install"]))
        self.assertEqual(
            rows,
            [
                "  + install new",
                "  ~ re-register changed",
                "  ~ re-register adopt (registered outside tools; adopted if it is in scope "
                "user, otherwise left alone)",
                "  - remove gone",
                "  ~ forget lost",
            ],
        )
        self.assertEqual(sorted(state["mcp"]["servers"]), ["adopt", "changed", "new"])

    def test_git_repos(self):
        new, pulled, gone = (os.path.join(self.tmp, n) for n in ("new", "pulled", "gone"))
        for path in (pulled, gone):
            os.makedirs(os.path.join(path, ".git"))
        repos = {new: "u-new", pulled: "u-pulled"}
        state = {"gitRepos": {"installed": [pulled, gone]}}
        self.patch(git_repos, "_has_local_work", return_value=False)
        self.patch(git_repos, "origin_url", return_value=None)
        rows = diff_mod._diff_git_repos(repos, copy.deepcopy(state), True)
        rec = self.patch(git_repos, "run_command", Recorder())
        self.patch(git_repos, "system_bin", return_value="git")
        trash = self.patch(git_repos.fsutil, "trash")
        self.assert_same_plan(
            git_repos,
            lambda: git_repos.install_git_repos(repos, state, True),
            repos,
            {},
            state,
            ("plan",),
            plan_args=(repos, copy.deepcopy(state), True),
        )
        self.assertEqual(
            rows,
            [f"  + clone {new}", f"  ~ pull {pulled}", f"  - remove {gone} (deletes the checkout)"],
        )
        self.assertEqual(
            rec.calls,
            [["git", "clone", "u-new", new], ["git", "-C", pulled, "pull", "--ff-only"]],
        )
        trash.assert_called_once_with(gone)


if __name__ == "__main__":
    unittest.main()
