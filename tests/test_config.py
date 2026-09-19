"""Defaults in config.py.

config reads the environment once, at import, so each case reloads the module
under a patched environment. ACAD_MCP_SECRET_PATH is always set: without it the
import would write a .secret file into the source tree.

    python -m unittest discover -s tests -t .
"""

import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Must be in place before the first import, which is when .secret gets written.
os.environ.setdefault("ACAD_MCP_SECRET_PATH", "unit-test-secret")

import config  # noqa: E402

_VARIABLES = ("ACAD_MCP_PORT", "ACAD_MCP_SAVE_DIR", "ACAD_MCP_SECRET_PATH")


def load(**env):
    """Reload config with only the given ACAD_MCP_* variables set."""
    patched = {k: v for k, v in os.environ.items() if k not in _VARIABLES}
    patched["ACAD_MCP_SECRET_PATH"] = "unit-test-secret"
    patched.update(env)
    with mock.patch.dict(os.environ, patched, clear=True):
        return importlib.reload(config)


class PortDefault(unittest.TestCase):
    def test_default_is_the_documented_port(self):
        # 8765 belongs to the Codex AutoCAD host this runs alongside.
        self.assertEqual(load().PORT, 8770)

    def test_environment_wins(self):
        self.assertEqual(load(ACAD_MCP_PORT="9000").PORT, 9000)

    def test_empty_variable_counts_as_unset(self):
        self.assertEqual(load(ACAD_MCP_PORT="").PORT, 8770)


class SaveDirDefault(unittest.TestCase):
    def test_default_lives_in_the_current_users_profile(self):
        home = Path(tempfile.gettempdir()) / "someone-else"
        with mock.patch.object(Path, "home", return_value=home):
            save_dir = load().SAVE_DIR
        self.assertEqual(save_dir, (home / "AutoCAD-MCP-Out").resolve())

    def test_environment_wins(self):
        wanted = Path(tempfile.gettempdir()) / "dwg-out"
        self.assertEqual(
            load(ACAD_MCP_SAVE_DIR=str(wanted)).SAVE_DIR, wanted.resolve()
        )

    def test_empty_variable_does_not_mean_current_directory(self):
        save_dir = load(ACAD_MCP_SAVE_DIR="").SAVE_DIR
        self.assertNotEqual(save_dir, Path.cwd().resolve())
        self.assertEqual(save_dir.name, "AutoCAD-MCP-Out")


def tearDownModule():
    # Leave one known state behind for whatever imports config next.
    load()


if __name__ == "__main__":
    unittest.main()
