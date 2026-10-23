import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tools.user import skills

GIT = shutil.which("git")
GIT_BIN = GIT or "/usr/bin/git"


def _write(path, content="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _make_skill(root, rel, body="# skill\n"):
    _write(os.path.join(root, rel, "SKILL.md"), body)


class SkillsTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.config_dir = os.path.join(self.tmp, "config")
        self.src = os.path.join(self.config_dir, "my-skills")
        self.t1 = os.path.join(self.tmp, "home", ".claude", "skills")
        self.t2 = os.path.join(self.tmp, "home", ".codex", "skills")
        self.cache = os.path.join(self.tmp, "cache")
        _make_skill(self.src, "pdf")
        _make_skill(self.src, "nested/xlsx")
        self.state = {}
        for name, value in (("system_bin", GIT_BIN), ("system_dir", os.path.dirname(GIT_BIN))):
            patcher = mock.patch.object(skills, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def config(self, enable, targets=None, **extra):
        cfg = {
            "cacheDir": self.cache,
            "sources": {"mine": {"path": "my-skills"}},
            "targets": targets if targets is not None else [self.t1, self.t2],
            "enable": enable,
        }
        cfg.update(extra)
        return cfg

    def deploy(self, cfg):
        return skills.install_skills(cfg, self.config_dir, self.state)

    def diff(self, cfg):
        return skills.diff_skills(cfg, self.config_dir, self.state)


class InstallTest(SkillsTestBase):
    def test_installs_to_every_target_and_is_idempotent(self):
        cfg = self.config(["mine/pdf", "mine/nested/xlsx"])
        self.assertEqual(len(self.diff(cfg)), 4)

        self.assertTrue(self.deploy(cfg))

        for target in (self.t1, self.t2):
            self.assertTrue(os.path.isfile(os.path.join(target, "pdf", "SKILL.md")))
            # Nested skills install flat: agents expect <dir>/<name>/SKILL.md.
            self.assertTrue(os.path.isfile(os.path.join(target, "xlsx", "SKILL.md")))
        self.assertEqual(
            self.state["skills"]["installed"][os.path.join(self.t1, "pdf")]["skill"],
            "mine/pdf",
        )
        self.assertEqual(self.diff(cfg), [])

    def test_wildcard_per_entry_targets_and_rename(self):
        claw = os.path.join(self.tmp, "home", "claw")
        cfg = self.config(
            [
                "mine/*",
                {"skill": "mine/pdf", "targets": [claw], "as": "claw-pdf"},
            ],
            targets=[self.t1],
        )
        self.assertTrue(self.deploy(cfg))

        self.assertEqual(sorted(os.listdir(self.t1)), ["pdf", "xlsx"])
        self.assertEqual(os.listdir(claw), ["claw-pdf"])

    def test_source_change_updates_installed_copy(self):
        cfg = self.config(["mine/pdf"], targets=[self.t1])
        self.deploy(cfg)
        _write(os.path.join(self.src, "pdf", "SKILL.md"), "v2")

        self.assertEqual(self.diff(cfg), [f"  ~ update mine/pdf -> {self.t1}/pdf"])
        self.assertTrue(self.deploy(cfg))

        with open(os.path.join(self.t1, "pdf", "SKILL.md")) as f:
            self.assertEqual(f.read(), "v2")

    def test_ignores_git_metadata_in_sources(self):
        _write(os.path.join(self.src, "pdf", ".git", "HEAD"), "ref")
        self.deploy(self.config(["mine/pdf"], targets=[self.t1]))

        self.assertFalse(os.path.exists(os.path.join(self.t1, "pdf", ".git")))


class OwnershipTest(SkillsTestBase):
    def test_unmanaged_existing_dir_is_never_overwritten(self):
        _write(os.path.join(self.t1, "pdf", "SKILL.md"), "hand-made")
        cfg = self.config(["mine/pdf"], targets=[self.t1])

        self.assertIn("exists and is not managed", self.diff(cfg)[0])
        self.deploy(cfg)

        with open(os.path.join(self.t1, "pdf", "SKILL.md")) as f:
            self.assertEqual(f.read(), "hand-made")
        self.assertNotIn(os.path.join(self.t1, "pdf"), self.state["skills"]["installed"])

    def test_locally_modified_copy_is_not_updated(self):
        cfg = self.config(["mine/pdf"], targets=[self.t1])
        self.deploy(cfg)
        _write(os.path.join(self.t1, "pdf", "SKILL.md"), "my edit")
        _write(os.path.join(self.src, "pdf", "SKILL.md"), "upstream v2")

        self.assertIn("modified since tools wrote it", self.diff(cfg)[0])
        self.deploy(cfg)

        with open(os.path.join(self.t1, "pdf", "SKILL.md")) as f:
            self.assertEqual(f.read(), "my edit")

    def test_dropped_skill_is_removed(self):
        self.deploy(self.config(["mine/pdf", "mine/nested/xlsx"], targets=[self.t1]))
        cfg = self.config(["mine/pdf"], targets=[self.t1])

        self.assertEqual(self.diff(cfg), [f"  - remove {self.t1}/xlsx"])
        self.assertTrue(self.deploy(cfg))

        self.assertFalse(os.path.exists(os.path.join(self.t1, "xlsx")))
        self.assertNotIn(os.path.join(self.t1, "xlsx"), self.state["skills"]["installed"])

    def test_dropping_whole_section_removes_everything_installed(self):
        self.deploy(self.config(["mine/pdf"], targets=[self.t1]))

        self.assertTrue(skills.has_work({}, self.state))
        self.assertTrue(self.deploy({"cacheDir": self.cache}))
        self.assertFalse(os.path.exists(os.path.join(self.t1, "pdf")))

    def test_modified_dropped_skill_is_kept(self):
        self.deploy(self.config(["mine/pdf", "mine/nested/xlsx"], targets=[self.t1]))
        _write(os.path.join(self.t1, "xlsx", "notes.md"), "mine")
        cfg = self.config(["mine/pdf"], targets=[self.t1])

        self.assertEqual(self.diff(cfg), [])
        self.deploy(cfg)

        self.assertTrue(os.path.exists(os.path.join(self.t1, "xlsx", "notes.md")))

    def test_resolution_error_blocks_all_removals(self):
        # An unresolvable entry makes the rest look undesired; deleting on
        # that basis would remove skills that are merely unresolved.
        self.deploy(self.config(["mine/pdf", "mine/nested/xlsx"], targets=[self.t1]))
        cfg = self.config(["mine/pdf", "mine/typo"], targets=[self.t1])

        self.assertFalse(self.deploy(cfg))
        self.assertTrue(os.path.exists(os.path.join(self.t1, "xlsx")))


class ConfigErrorTest(SkillsTestBase):
    def assertErrors(self, cfg, fragment):
        changes = self.diff(cfg)
        self.assertTrue(any(fragment in c for c in changes), changes)
        self.assertFalse(self.deploy(cfg))

    def test_unknown_skill(self):
        self.assertErrors(self.config(["mine/nope"]), "no SKILL.md found")

    def test_unknown_source(self):
        self.assertErrors(self.config(["other/pdf"]), "unknown source")

    def test_source_needs_exactly_one_of_url_or_path(self):
        cfg = self.config(["mine/pdf"])
        cfg["sources"]["mine"]["url"] = "https://example.invalid/x"
        self.assertErrors(cfg, "exactly one of")

    def test_relative_target_rejected(self):
        self.assertErrors(self.config(["mine/pdf"], targets=["skills"]), "must be absolute")

    def test_name_collision_in_one_target(self):
        _make_skill(self.src, "other/pdf")
        self.assertErrors(self.config(["mine/pdf", "mine/other/pdf"]), "claimed by both")

    def test_subdir_traversal_rejected(self):
        cfg = self.config(["mine/pdf"])
        cfg["sources"]["mine"]["subdir"] = "../.."
        self.assertErrors(cfg, "subdir")


class DiffIsReadOnlyTest(SkillsTestBase):
    def test_unfetched_git_source_is_previewed_without_network_or_writes(self):
        cfg = self.config(["remote/pdf"])
        cfg["sources"] = {"remote": {"url": "https://example.invalid/skills.git"}}

        with mock.patch.object(skills, "run_command") as run:
            changes = self.diff(cfg)

        run.assert_not_called()
        self.assertEqual(
            changes,
            [
                "  + fetch remote (https://example.invalid/skills.git)",
                "  + install remote/pdf (after fetch)",
            ],
        )
        self.assertFalse(os.path.exists(self.cache))


class ReviewRegressionTest(SkillsTestBase):
    def test_skill_nested_inside_another_is_not_discovered(self):
        _make_skill(self.src, "pdf/examples/demo")
        self.deploy(self.config(["mine/*"], targets=[self.t1]))

        self.assertEqual(sorted(os.listdir(self.t1)), ["pdf", "xlsx"])
        self.assertTrue(
            os.path.isfile(os.path.join(self.t1, "pdf", "examples", "demo", "SKILL.md"))
        )

    def test_dot_git_file_in_source_does_not_mark_copy_modified(self):
        # Submodules and worktrees have a .git *file*, which the copy skips.
        _write(os.path.join(self.src, "pdf", ".git"), "gitdir: elsewhere")
        cfg = self.config(["mine/pdf"], targets=[self.t1])
        self.deploy(cfg)
        _write(os.path.join(self.src, "pdf", "SKILL.md"), "v2")

        self.assertEqual(self.diff(cfg), [f"  ~ update mine/pdf -> {self.t1}/pdf"])

    def test_option_like_url_or_ref_rejected(self):
        for key, value in (("url", "--upload-pack=touch /tmp/x"), ("ref", "--orphan=x")):
            cfg = self.config(["remote/pdf"])
            cfg["sources"] = {"remote": {"url": "https://example.invalid/x", key: value}}
            with mock.patch.object(skills, "run_command") as run:
                self.assertFalse(self.deploy(cfg))
            run.assert_not_called()

    def test_unused_git_source_is_not_fetched(self):
        cfg = self.config(["mine/pdf"], targets=[self.t1])
        cfg["sources"]["remote"] = {"url": "https://example.invalid/x"}
        with mock.patch.object(skills, "run_command") as run:
            self.assertTrue(self.deploy(cfg))
            self.assertEqual(self.diff(cfg), [])
        run.assert_not_called()

    def test_non_checkout_in_cache_dir_is_never_deleted(self):
        # cacheDir is user-settable; a same-named dir there is not ours.
        keep = os.path.join(self.cache, "remote", "notes.md")
        _write(keep, "mine")
        cfg = self.config(["remote/pdf"])
        cfg["sources"] = {"remote": {"url": "https://example.invalid/x"}}

        self.assertIn("is not a git checkout", " ".join(self.diff(cfg)))
        with mock.patch.object(skills, "run_command") as run:
            self.assertFalse(self.deploy(cfg))
        run.assert_not_called()
        self.assertTrue(os.path.exists(keep))

        self.state["skills"]["sources"] = {"remote": {"url": "x", "ref": None}}
        self.assertTrue(self.deploy({"cacheDir": self.cache}))
        self.assertTrue(os.path.exists(keep))

    def test_diff_holds_removals_while_config_has_errors(self):
        self.deploy(self.config(["mine/pdf", "mine/nested/xlsx"], targets=[self.t1]))
        cfg = self.config(["mine/pdf", "mine/typo"], targets=[self.t1])

        self.assertFalse(any(c.startswith("  - remove") for c in self.diff(cfg)))


class LinkTargetsTest(SkillsTestBase):
    def setUp(self):
        super().setUp()
        self.agents = os.path.join(self.tmp, "home", ".agents", "skills")

    def link_config(self, enable, **extra):
        return self.config(enable, targets=[self.agents], linkTargets=[self.t1], **extra)

    def assertLinked(self, link, copy):
        self.assertTrue(os.path.islink(link), link)
        self.assertEqual(os.readlink(link), copy)

    def test_links_to_copy_and_is_idempotent(self):
        cfg = self.link_config(["mine/pdf"])
        link, copy = os.path.join(self.t1, "pdf"), os.path.join(self.agents, "pdf")

        self.assertEqual(
            self.diff(cfg),
            [f"  + install mine/pdf -> {copy}", f"  + link mine/pdf: {link} -> {copy}"],
        )
        self.assertTrue(self.deploy(cfg))

        self.assertLinked(link, copy)
        self.assertEqual(self.state["skills"]["links"][link], {"skill": "mine/pdf", "source": copy})
        self.assertNotIn(link, self.state["skills"]["installed"])
        self.assertEqual(self.diff(cfg), [])

    def test_glob_source_links_every_skill(self):
        self.assertTrue(self.deploy(self.link_config(["mine/*"])))

        for name in ("pdf", "xlsx"):
            self.assertLinked(os.path.join(self.t1, name), os.path.join(self.agents, name))

    def test_existing_link_to_copy_is_adopted(self):
        self.deploy(self.config(["mine/pdf"], targets=[self.agents]))
        link = os.path.join(self.t1, "pdf")
        os.makedirs(self.t1)
        os.symlink("../../.agents/skills/pdf", link)
        cfg = self.link_config(["mine/pdf"])

        self.assertEqual(self.diff(cfg), [])
        self.assertTrue(self.deploy(cfg))

        self.assertEqual(os.readlink(link), "../../.agents/skills/pdf")
        self.assertIn(link, self.state["skills"]["links"])

    def test_foreign_link_and_dir_are_skipped(self):
        os.makedirs(self.t1)
        os.symlink(self.tmp, os.path.join(self.t1, "pdf"))
        _write(os.path.join(self.t1, "xlsx", "SKILL.md"), "hand-made")
        cfg = self.link_config(["mine/*"])

        changes = self.diff(cfg)
        self.assertIn(
            f"  ! skip {self.t1}/pdf: symlink to {self.tmp} not managed by tools", changes
        )
        self.assertIn(f"  ! skip {self.t1}/xlsx: exists and is not managed by tools", changes)
        self.assertTrue(self.deploy(cfg))

        self.assertEqual(os.readlink(os.path.join(self.t1, "pdf")), self.tmp)
        self.assertFalse(os.path.islink(os.path.join(self.t1, "xlsx")))
        self.assertEqual(self.state["skills"]["links"], {})

    def test_blocked_copy_gets_no_link(self):
        _write(os.path.join(self.agents, "pdf", "SKILL.md"), "hand-made")
        cfg = self.link_config(["mine/pdf"])

        self.assertIn(
            f"  ! skip {self.t1}/pdf: its copy {self.agents}/pdf is skipped", self.diff(cfg)
        )
        self.deploy(cfg)
        self.assertFalse(os.path.lexists(os.path.join(self.t1, "pdf")))

    def test_failed_copy_gets_no_link(self):
        cfg = self.link_config(["mine/pdf"])
        with mock.patch.object(skills, "_copy_tree", side_effect=OSError("disk full")):
            self.assertFalse(self.deploy(cfg))

        self.assertFalse(os.path.lexists(os.path.join(self.t1, "pdf")))
        self.assertEqual(self.state["skills"]["links"], {})

    def test_copy_target_moved_to_link_targets_replaces_copy(self):
        self.deploy(self.config(["mine/pdf"], targets=[self.agents, self.t1]))
        link, copy = os.path.join(self.t1, "pdf"), os.path.join(self.agents, "pdf")
        cfg = self.link_config(["mine/pdf"])

        self.assertEqual(self.diff(cfg), [f"  ~ replace copy with link mine/pdf: {link} -> {copy}"])
        self.assertTrue(self.deploy(cfg))

        self.assertLinked(link, copy)
        self.assertNotIn(link, self.state["skills"]["installed"])
        self.assertIn(link, self.state["skills"]["links"])
        self.assertEqual(self.diff(cfg), [])

    def test_modified_copy_is_kept_instead_of_linked(self):
        self.deploy(self.config(["mine/pdf"], targets=[self.agents, self.t1]))
        link = os.path.join(self.t1, "pdf")
        _write(os.path.join(link, "notes.md"), "mine")
        cfg = self.link_config(["mine/pdf"])

        self.assertEqual(
            self.diff(cfg), [f"  ! skip {link}: copy modified since tools wrote it; kept"]
        )
        self.deploy(cfg)

        self.assertFalse(os.path.islink(link))
        self.assertTrue(os.path.exists(os.path.join(link, "notes.md")))

    def test_dropped_skill_or_link_target_removes_link(self):
        self.deploy(self.link_config(["mine/pdf", "mine/nested/xlsx"]))

        cfg = self.link_config(["mine/pdf"])
        self.assertEqual(
            self.diff(cfg), [f"  - remove {self.agents}/xlsx", f"  - unlink {self.t1}/xlsx"]
        )
        self.assertTrue(self.deploy(cfg))
        self.assertFalse(os.path.lexists(os.path.join(self.t1, "xlsx")))

        self.assertTrue(self.deploy(self.config(["mine/pdf"], targets=[self.agents])))
        self.assertFalse(os.path.lexists(os.path.join(self.t1, "pdf")))
        self.assertTrue(os.path.isdir(os.path.join(self.agents, "pdf")))
        self.assertEqual(self.state["skills"]["links"], {})

    def test_repointed_link_is_left_in_place(self):
        self.deploy(self.link_config(["mine/pdf"]))
        link = os.path.join(self.t1, "pdf")
        os.remove(link)
        os.symlink(self.tmp, link)

        self.assertTrue(self.deploy(self.config(["mine/pdf"], targets=[self.agents])))
        self.assertEqual(os.readlink(link), self.tmp)

    def test_errors_hold_link_removal_and_replacement(self):
        xlsx_copies = {"skill": "mine/nested/xlsx", "targets": [self.agents, self.t1]}
        self.deploy(self.link_config(["mine/pdf", {**xlsx_copies, "linkTargets": []}]))
        cfg = self.link_config(["mine/nested/xlsx", "mine/typo"])

        changes = self.diff(cfg)
        self.assertFalse([c for c in changes if c.startswith(("  ~ replace", "  - "))], changes)
        self.assertFalse(self.deploy(cfg))

        self.assertTrue(os.path.islink(os.path.join(self.t1, "pdf")))
        self.assertFalse(os.path.islink(os.path.join(self.t1, "xlsx")))

    def test_per_entry_link_targets(self):
        other = os.path.join(self.tmp, "home", ".pi", "skills")
        claw = os.path.join(self.tmp, "home", "claw")
        cfg = self.link_config(
            [
                {"skill": "mine/pdf", "linkTargets": [other]},
                {"skill": "mine/nested/xlsx", "targets": [claw, self.agents]},
            ]
        )
        self.assertTrue(self.deploy(cfg))

        self.assertLinked(os.path.join(other, "pdf"), os.path.join(self.agents, "pdf"))
        self.assertFalse(os.path.lexists(os.path.join(self.t1, "pdf")))
        self.assertLinked(os.path.join(self.t1, "xlsx"), os.path.join(claw, "xlsx"))

    def test_path_both_copy_and_link_is_an_error(self):
        cfg = self.config(["mine/pdf"], targets=[self.agents, self.t1], linkTargets=[self.t1])

        self.assertIn(f"  ! {self.t1}/pdf: both a copy target and a link target", self.diff(cfg))
        self.assertFalse(self.deploy(cfg))
        self.assertFalse(os.path.islink(os.path.join(self.t1, "pdf")))

    def test_link_targets_moved_back_to_targets_replaces_link(self):
        self.deploy(self.link_config(["mine/pdf"]))
        cfg = self.config(["mine/pdf"], targets=[self.agents, self.t1])

        self.assertEqual(self.diff(cfg), [f"  + install mine/pdf -> {self.t1}/pdf"])
        self.assertTrue(self.deploy(cfg))

        self.assertTrue(os.path.isfile(os.path.join(self.t1, "pdf", "SKILL.md")))
        self.assertFalse(os.path.islink(os.path.join(self.t1, "pdf")))
        self.assertEqual(self.state["skills"]["links"], {})

    def test_old_copy_kept_while_link_cannot_move(self):
        self.deploy(self.link_config(["mine/pdf"]))
        new_home = os.path.join(self.tmp, "home", "new")
        _write(os.path.join(new_home, "pdf", "SKILL.md"), "hand-made")
        cfg = self.config(["mine/pdf"], targets=[new_home], linkTargets=[self.t1])

        self.assertNotIn(f"  - remove {self.agents}/pdf", self.diff(cfg))
        self.deploy(cfg)

        self.assertTrue(os.path.exists(os.path.join(self.t1, "pdf", "SKILL.md")))

    def test_link_never_points_at_another_skills_copy(self):
        _make_skill(self.src, "other/pdf")
        other = os.path.join(self.tmp, "home", ".pi", "skills")
        cfg = self.link_config(["mine/pdf", {"skill": "mine/other/pdf", "linkTargets": [other]}])

        self.assertFalse(self.deploy(cfg))
        self.assertFalse(os.path.lexists(os.path.join(other, "pdf")))

    def test_link_owned_by_links_section_is_skipped(self):
        self.deploy(self.config(["mine/pdf"], targets=[self.agents]))
        link = os.path.join(self.t1, "pdf")
        os.makedirs(self.t1)
        os.symlink(os.path.join(self.agents, "pdf"), link)
        self.state["links"] = {"installed": {link: {"source": os.path.join(self.agents, "pdf")}}}
        cfg = self.link_config(["mine/pdf"])

        self.assertEqual(self.diff(cfg), [f"  ! skip {link}: managed by the links section"])
        self.assertTrue(self.deploy(cfg))
        self.assertEqual(self.state["skills"]["links"], {})

    def test_malformed_link_state_entry_does_not_crash(self):
        self.state["skills"] = {"links": {os.path.join(self.t1, "pdf"): "junk"}}
        self.assertTrue(self.deploy(self.link_config(["mine/pdf"])))
        self.assertLinked(os.path.join(self.t1, "pdf"), os.path.join(self.agents, "pdf"))


@unittest.skipUnless(GIT, "git not available")
class GitSourceTest(SkillsTestBase):
    def _git(self, *args):
        subprocess.run(
            [GIT, "-C", self.repo, *args],
            check=True,
            capture_output=True,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "upstream")
        _make_skill(os.path.join(self.repo, "skills"), "pdf", "v1")
        os.makedirs(self.repo, exist_ok=True)
        self._git("init", "-q", "-b", "main")
        self._git("add", "-A")
        self._git("commit", "-q", "--no-gpg-sign", "-m", "v1")
        self._git("tag", "--no-sign", "v1")
        _write(os.path.join(self.repo, "skills", "pdf", "SKILL.md"), "v2")
        self._git("commit", "-q", "--no-gpg-sign", "-am", "v2")

    def git_config(self, **source):
        cfg = self.config(["up/pdf"], targets=[self.t1])
        cfg["sources"] = {"up": {"url": self.repo, "subdir": "skills", **source}}
        return cfg

    def read_installed(self):
        with open(os.path.join(self.t1, "pdf", "SKILL.md")) as f:
            return f.read()

    def test_clones_default_branch(self):
        self.assertTrue(self.deploy(self.git_config()))
        self.assertEqual(self.read_installed(), "v2")

    def test_clone_checks_out_only_subdir(self):
        _write(os.path.join(self.repo, "other", "big.bin"), "x")
        self._git("add", "-A")
        self._git("commit", "-q", "--no-gpg-sign", "-m", "other")

        self.assertTrue(self.deploy(self.git_config()))
        self.assertTrue(os.path.isdir(os.path.join(self.cache, "up", "skills")))
        self.assertFalse(os.path.exists(os.path.join(self.cache, "up", "other")))

    def test_subdir_change_is_previewed_and_applied(self):
        _make_skill(os.path.join(self.repo, "agents"), "xlsx", "a1")
        self._git("add", "-A")
        self._git("commit", "-q", "--no-gpg-sign", "-m", "agents")
        self.assertTrue(self.deploy(self.git_config()))

        moved = self.git_config(subdir="agents")
        moved["enable"] = ["up/xlsx"]
        changes = self.diff(moved)
        self.assertIn("  ~ refetch up (url, ref or subdir changed)", changes)
        self.assertFalse([c for c in changes if c.startswith("  !")], changes)

        self.assertTrue(self.deploy(moved))
        self.assertTrue(os.path.isfile(os.path.join(self.t1, "xlsx", "SKILL.md")))
        self.assertFalse(os.path.exists(os.path.join(self.t1, "pdf")))
        self.assertFalse(os.path.exists(os.path.join(self.cache, "up", "skills")))

    def test_dropping_subdir_checks_out_everything(self):
        _write(os.path.join(self.repo, "other", "big.bin"), "x")
        self._git("add", "-A")
        self._git("commit", "-q", "--no-gpg-sign", "-m", "other")
        self.assertTrue(self.deploy(self.git_config()))

        cfg = self.git_config(subdir=None)
        cfg["enable"] = ["up/skills/pdf"]
        self.assertTrue(self.deploy(cfg))
        self.assertTrue(os.path.isfile(os.path.join(self.cache, "up", "other", "big.bin")))

    def test_ref_pins_and_ref_change_is_previewed(self):
        self.assertTrue(self.deploy(self.git_config(ref="v1")))
        self.assertEqual(self.read_installed(), "v1")

        moved = self.git_config(ref="main")
        self.assertIn("  ~ refetch up (url, ref or subdir changed)", self.diff(moved))
        self.assertTrue(self.deploy(moved))
        self.assertEqual(self.read_installed(), "v2")

    def test_dropped_source_cache_is_removed(self):
        self.deploy(self.git_config())
        self.assertTrue(os.path.isdir(os.path.join(self.cache, "up")))

        self.assertTrue(self.deploy({"cacheDir": self.cache}))
        self.assertFalse(os.path.exists(os.path.join(self.cache, "up")))
        self.assertFalse(os.path.exists(os.path.join(self.t1, "pdf")))

    def test_failed_checkout_does_not_install_from_stale_ref(self):
        self.deploy(self.git_config(ref="v1"))
        cfg = self.git_config(ref="v9")
        cfg["enable"] = ["up/pdf", {"skill": "up/pdf", "targets": [self.t2]}]

        self.assertFalse(self.deploy(cfg))
        self.assertFalse(os.path.exists(os.path.join(self.t2, "pdf")))

    def test_ref_naming_a_path_is_not_taken_as_pathspec(self):
        # `git checkout skills` would restore that path and exit 0.
        self.deploy(self.git_config(ref="v1"))

        self.assertFalse(self.deploy(self.git_config(ref="skills")))
        self.assertEqual(self.read_installed(), "v1")

    def test_ref_does_not_resolve_to_stale_local_branch(self):
        self.deploy(self.git_config(ref="v1"))
        # The clone's local `main` stays at clone time; upstream drops it.
        self._git("branch", "-q", "-m", "main", "trunk")

        self.assertFalse(self.deploy(self.git_config(ref="main")))
        self.assertEqual(self.read_installed(), "v1")

    def test_ref_can_be_a_commit_hash(self):
        sha = subprocess.run(
            ["git", "-C", self.repo, "rev-parse", "HEAD~1"], capture_output=True, text=True
        ).stdout.strip()

        self.assertTrue(self.deploy(self.git_config(ref=sha[:12])))
        self.assertEqual(self.read_installed(), "v1")

    def test_skill_from_unfetched_source_is_not_previewed_as_removed(self):
        self.deploy(self.git_config())
        shutil.rmtree(os.path.join(self.cache, "up"))

        self.assertNotIn(f"  - remove {self.t1}/pdf", self.diff(self.git_config()))

    def test_failed_fetch_blocks_removals(self):
        self.deploy(self.git_config())
        shutil.rmtree(self.repo)

        self.assertFalse(self.deploy(self.git_config()))
        self.assertTrue(os.path.exists(os.path.join(self.t1, "pdf")))


if __name__ == "__main__":
    unittest.main()
