"""HOME at the temp root for a test module, so `files:` targets in temp dirs are user
files (targets outside HOME are system files, deployed only by root)."""

import os
import tempfile

_saved = []


def setUpModule():
    _saved.append(os.environ.get("HOME"))
    os.environ["HOME"] = os.path.realpath(tempfile.gettempdir())


def tearDownModule():
    home = _saved.pop()
    if home is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = home
