"""Ownership is only meaningful against the place it was recorded.

A tracked name on a different Ollama host, npm prefix, or flatpak remote URL
refers to something this tool never installed, so it must not be a removal
candidate.
"""

import unittest
from unittest import mock

from tools.user import bun, npm, ollama_models


class FakeOllama:
    """Stand-in for the ollama CLI. Kept local: `unittest discover` imports
    test modules top-level, so a relative import between them fails."""

    def __init__(self, installed=()):
        self.installed = set(installed)
        self.commands = []

    def run(self, cmd, env=None, cwd=None):
        self.commands.append(cmd)
        verb = cmd[1]
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


class OllamaContextTest(unittest.TestCase):
    def _reconcile(self, config, state, fake):
        with (
            mock.patch.object(ollama_models, "_find_ollama", return_value="ollama"),
            mock.patch.object(ollama_models, "run_command", fake.run),
        ):
            return ollama_models.install_ollama_models(config, state)

    def test_same_host_keeps_ownership(self):
        state = {}
        cfg = {"models": ["llama3"], "host": "http://a:11434"}
        self._reconcile(cfg, state, FakeOllama())
        self.assertEqual(state["ollamaModels"]["installed"], ["llama3:latest"])

        # Dropping it from config on the same host removes it.
        fake = FakeOllama(installed=["llama3:latest"])
        self._reconcile({"models": [], "host": "http://a:11434"}, state, fake)
        self.assertNotIn("llama3:latest", fake.installed)

    def test_changed_host_releases_ownership(self):
        # The recorded name refers to a model on the old server; an
        # identically-named model on the new one is not ours to delete.
        state = {"ollamaModels": {"installed": ["llama3:latest"], "context": "http://a:11434|"}}
        fake = FakeOllama(installed=["llama3:latest"])

        self._reconcile({"models": [], "host": "http://b:11434"}, state, fake)

        self.assertIn("llama3:latest", fake.installed)

    def test_changed_models_path_releases_ownership(self):
        state = {"ollamaModels": {"installed": ["llama3:latest"], "context": "|/old/models"}}
        fake = FakeOllama(installed=["llama3:latest"])

        self._reconcile({"models": [], "modelsPath": "/new/models"}, state, fake)

        self.assertIn("llama3:latest", fake.installed)

    def test_context_is_recorded(self):
        state = {}
        self._reconcile({"models": [], "host": "http://a:11434"}, state, FakeOllama())
        # Nothing desired and nothing tracked short-circuits, so seed one first.
        state = {"ollamaModels": {"installed": [], "context": "http://a:11434|"}}
        self._reconcile({"models": ["m"], "host": "http://a:11434"}, state, FakeOllama())

        self.assertEqual(state["ollamaModels"]["context"], "http://a:11434|")


class NpmPrefixTest(unittest.TestCase):
    def test_changed_prefix_releases_ownership(self):
        # Uninstall is by package name; against a new prefix that name is
        # whatever now lives there, not what we installed.
        state = {
            "npm": {"packages": {"pkg-a": {"installed": True}}, "prefix": "/old/npm/bin"},
        }
        paths = {"npmBin": "/new/npm/bin", "nodejs": "/usr/bin"}
        with mock.patch.object(npm, "run_command", return_value=(0, "", "")) as run:
            npm.install_npm_packages({}, paths, state, {"configFile": None})

        self.assertEqual(state["npm"]["packages"], {})
        self.assertFalse(any("uninstall" in " ".join(c.args[0]) for c in run.call_args_list))

    def test_prefix_is_recorded(self):
        state = {}
        paths = {"npmBin": "/npm/bin", "nodejs": "/usr/bin"}
        with mock.patch.object(npm, "run_command", return_value=(0, "", "")):
            npm.install_npm_packages({}, paths, state, {"configFile": None})

        self.assertEqual(state["npm"]["prefix"], "/npm/bin")


class BunPrefixTest(unittest.TestCase):
    def test_changed_prefix_releases_ownership(self):
        state = {
            "bun": {"packages": {"pkg-a": {"installed": True}}, "prefix": "/old/bun/bin"},
        }
        paths = {"bunBin": "/new/bun/bin", "bun": "/usr/bin", "nodejs": "/usr/bin"}
        with mock.patch.object(bun, "run_command", return_value=(0, "", "")) as run:
            bun.install_bun_packages({}, paths, state, {"configFile": None})

        self.assertEqual(state["bun"]["packages"], {})
        self.assertFalse(any("remove" in " ".join(c.args[0]) for c in run.call_args_list))


if __name__ == "__main__":
    unittest.main()
