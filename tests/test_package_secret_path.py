import ast
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PACKAGES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "packages")
SOURCES = [
    "backup-home/backup-home.py",
    "beszel-mgmt/constants.py",
    "nextdns-mgmt/nextdns-mgmt.py",
    "rebuild/notify.py",
    "youtube/constants.py",
]


def _secret_path(rel):
    """``secret_path`` (and the constants it uses) from a package, without its imports."""
    with open(os.path.join(PACKAGES, rel)) as f:
        tree = ast.parse(f.read())
    wanted = {"secret_path", "TOOLS_SECRETS_DIR", "SOPS_NIX_SECRETS_DIR"}
    body = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in wanted)
        or (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id in wanted for t in node.targets)
        )
    ]
    namespace = {"os": os, "Path": Path}
    exec(compile(ast.Module(body=body, type_ignores=[]), rel, "exec"), namespace)
    return namespace["secret_path"]


# flake check builds tests without packages/.
@unittest.skipUnless(os.path.isdir(PACKAGES), "packages/ not in this source tree")
class SecretPathTest(unittest.TestCase):
    def test_falls_back_per_file(self):
        with tempfile.TemporaryDirectory() as home:
            tools_dir = os.path.join(home, ".local/state/tools/secrets")
            os.makedirs(tools_dir)
            with open(os.path.join(tools_dir, "rendered-by-tools"), "w"):
                pass
            with mock.patch.dict(os.environ, {"HOME": home}):
                for rel in SOURCES:
                    with self.subTest(rel):
                        secret_path = _secret_path(rel)
                        self.assertEqual(
                            os.path.expanduser(str(secret_path("rendered-by-tools"))),
                            os.path.join(tools_dir, "rendered-by-tools"),
                        )
                        self.assertEqual(
                            os.path.expanduser(str(secret_path("only-in-sops-nix"))),
                            os.path.join(home, ".config/sops-nix/secrets/only-in-sops-nix"),
                        )


if __name__ == "__main__":
    unittest.main()
