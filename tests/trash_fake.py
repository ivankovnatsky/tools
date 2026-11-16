"""Stand-in Trash for tests: never touches the real user Trash.

``fsutil.trash_argv`` is patched to a Python one-liner that moves the path
into a temp dir, so the real ``fsutil.trash`` code path (subprocess, exit
code, path-gone check) still runs. Use ``setUpModule = trash_fake.start`` and
``tearDownModule = trash_fake.stop`` in any test module that removes files.
"""

import os
import shutil
import sys
import tempfile
from unittest import mock

_MOVE = (
    "import os, shutil, sys\n"
    "src, dest = sys.argv[1], sys.argv[2]\n"
    "n = 0\n"
    "while os.path.lexists(dest + (f'.{n}' if n else '')):\n"
    "    n += 1\n"
    "shutil.move(src, dest + (f'.{n}' if n else ''))\n"
)

_state = {}


def trash_dir() -> str:
    return _state["dir"]


def argv(path: str):
    path = os.path.abspath(path)
    dest = os.path.join(trash_dir(), path.strip("/").replace("/", "%"))
    return [sys.executable, "-I", "-c", _MOVE, path, dest]


def start():
    _state["dir"] = tempfile.mkdtemp(prefix="tools-test-trash-")
    _state["patch"] = mock.patch("tools.fsutil.trash_argv", argv)
    _state["patch"].start()


def stop():
    _state.pop("patch").stop()
    shutil.rmtree(_state.pop("dir"), ignore_errors=True)
