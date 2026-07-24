import unittest
from unittest import mock

from tools.user import ollama_models


class FakeOllama:
    """Stand-in for the ollama CLI, recording the commands it is given."""

    def __init__(self, installed=(), failing=()):
        self.installed = set(installed)
        self.failing = set(failing)
        self.commands = []

    def run(self, cmd, env=None, cwd=None):
        self.commands.append(cmd)
        verb = cmd[1]
        if verb in self.failing:
            return 1, "", "boom"
        if verb == "list":
            header = "NAME\tID\tSIZE"
            rows = "\n".join(f"{m}\tabc\t1GB" for m in sorted(self.installed))
            return 0, f"{header}\n{rows}" if rows else header, ""
        if verb == "pull":
            self.installed.add(cmd[2])
            return 0, "", ""
        if verb == "rm":
            self.installed.discard(cmd[2])
            return 0, "", ""
        raise AssertionError(f"unexpected ollama verb: {verb}")


class OllamaModelsTest(unittest.TestCase):
    def _reconcile(self, config, state, fake):
        with (
            mock.patch.object(ollama_models, "_find_ollama", return_value="ollama"),
            mock.patch.object(ollama_models, "run_command", fake.run),
        ):
            return ollama_models.install_ollama_models(config, state)

    def _diff(self, config, state, fake):
        with (
            mock.patch.object(ollama_models, "_find_ollama", return_value="ollama"),
            mock.patch.object(ollama_models, "run_command", fake.run),
        ):
            return ollama_models.diff_ollama_models(config, state)

    def test_pulls_desired_model_and_tracks_it(self):
        state = {}
        fake = FakeOllama()

        self.assertTrue(self._reconcile({"models": ["llama3"]}, state, fake))

        self.assertIn("llama3:latest", fake.installed)
        self.assertEqual(state["ollamaModels"]["installed"], ["llama3:latest"])

    def test_untagged_ref_matches_latest(self):
        # `ollama pull foo` shows up as `foo:latest`; without normalization the
        # sets never converge and every run re-pulls.
        state = {}
        fake = FakeOllama(installed=["llama3:latest"])

        self.assertEqual(self._diff({"models": ["llama3"]}, state, fake), [])

    def test_preinstalled_model_is_not_adopted(self):
        # Pulled by hand: we did not put it there, so it never enters state and
        # config can never remove it.
        state = {}
        fake = FakeOllama(installed=["llama3:latest"])

        self._reconcile({"models": ["llama3"]}, state, fake)

        self.assertEqual(state["ollamaModels"]["installed"], [])
        self.assertIn("llama3:latest", fake.installed)

    def test_tracked_model_dropped_from_config_is_removed(self):
        state = {"ollamaModels": {"installed": ["llama3:latest"]}}
        fake = FakeOllama(installed=["llama3:latest"])

        self._reconcile({"models": []}, state, fake)

        self.assertNotIn("llama3:latest", fake.installed)
        self.assertEqual(state["ollamaModels"]["installed"], [])

    def test_unmanaged_model_is_left_alone(self):
        state = {"ollamaModels": {"installed": []}}
        fake = FakeOllama(installed=["mistral:latest"])

        self._reconcile({"models": []}, state, fake)

        self.assertIn("mistral:latest", fake.installed)

    def test_stale_tracked_entry_forces_a_diff(self):
        # Tracked, gone from disk, gone from config: without a diff line deploy
        # short-circuits and state keeps claiming it, so a later manual pull
        # would be mistaken for ours and deleted.
        state = {"ollamaModels": {"installed": ["gone:latest"]}}
        fake = FakeOllama()

        changes = self._diff({"models": []}, state, fake)

        self.assertIn("  ~ forget gone:latest", changes)

    def test_repulled_forgotten_model_is_not_deleted(self):
        state = {"ollamaModels": {"installed": ["gone:latest"]}}
        self._reconcile({"models": []}, state, FakeOllama())
        self.assertEqual(state["ollamaModels"]["installed"], [])

        fake = FakeOllama(installed=["gone:latest"])
        self._reconcile({"models": []}, state, fake)

        self.assertIn("gone:latest", fake.installed)

    def test_desired_model_is_not_reported_as_forgotten(self):
        # Tracked and desired but currently absent: the plan is `+ pull`, not
        # both a pull and a forget.
        state = {"ollamaModels": {"installed": ["llama3:latest"]}}
        fake = FakeOllama()

        changes = self._diff({"models": ["llama3"]}, state, fake)

        self.assertEqual(changes, ["  + pull llama3:latest"])

    def test_failed_pull_is_not_tracked(self):
        state = {}
        fake = FakeOllama(failing=["pull"])

        self.assertFalse(self._reconcile({"models": ["llama3"]}, state, fake))

        self.assertEqual(state["ollamaModels"]["installed"], [])

    def test_failed_removal_stays_tracked_for_retry(self):
        state = {"ollamaModels": {"installed": ["llama3:latest"]}}
        fake = FakeOllama(installed=["llama3:latest"], failing=["rm"])

        self.assertFalse(self._reconcile({"models": []}, state, fake))

        self.assertEqual(state["ollamaModels"]["installed"], ["llama3:latest"])

    def test_unreachable_daemon_leaves_state_untouched(self):
        state = {"ollamaModels": {"installed": ["llama3:latest"]}}
        fake = FakeOllama(installed=["llama3:latest"], failing=["list"])

        self.assertTrue(self._reconcile({"models": []}, state, fake))

        self.assertEqual(state["ollamaModels"]["installed"], ["llama3:latest"])
        self.assertFalse(any(c[1] == "rm" for c in fake.commands))

    def test_nothing_configured_does_nothing(self):
        fake = FakeOllama()
        state = {}

        self.assertTrue(self._reconcile({"models": []}, state, fake))

        self.assertEqual(fake.commands, [])

    def test_diff_and_deploy_agree(self):
        # Parity: an empty diff must mean deploy has nothing to do, and a
        # non-empty diff must mean it does something. Most of the bugs in this
        # module's history were preview/apply mismatches.
        scenarios = [
            ({"models": ["a"]}, {}, ["a:latest"]),
            ({"models": []}, {"ollamaModels": {"installed": ["a:latest"]}}, ["a:latest"]),
            ({"models": ["a"]}, {"ollamaModels": {"installed": ["a:latest"]}}, ["a:latest"]),
            ({"models": []}, {"ollamaModels": {"installed": ["a:latest"]}}, []),
            ({"models": ["a"]}, {}, []),
        ]
        for config, state, installed in scenarios:
            with self.subTest(config=config, state=state, installed=installed):
                changes = self._diff(config, dict(state), FakeOllama(installed=installed))
                fake = FakeOllama(installed=installed)
                mutable = {k: dict(v) for k, v in state.items()}
                self._reconcile(config, mutable, fake)
                acted = any(c[1] in ("pull", "rm") for c in fake.commands)
                state_moved = mutable.get("ollamaModels", {}).get("installed", []) != state.get(
                    "ollamaModels", {}
                ).get("installed", [])
                self.assertEqual(bool(changes), acted or state_moved)


if __name__ == "__main__":
    unittest.main()
