"""Defaults and secret-path persistence in config.py.

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


class SecretPathPersistence(unittest.TestCase):
    """`.secret` is what keeps connector URLs valid across a restart.

    The URLs pasted into ChatGPT and Manus embed the secret path. If a restart
    regenerated it, every connector would start 404ing with no obvious cause,
    so the generate-once-then-reuse behaviour is load-bearing.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.secret_file = Path(self._dir.name) / ".secret"

    def _load_secret(self, **env):
        patched = {k: v for k, v in os.environ.items()
                   if k != "ACAD_MCP_SECRET_PATH"}
        patched.update(env)
        with mock.patch.dict(os.environ, patched, clear=True):
            with mock.patch.object(config, "SECRET_FILE", self.secret_file):
                return config._load_or_create_secret()

    def test_generated_when_no_file_exists(self):
        secret = self._load_secret()
        self.assertTrue(secret.startswith("mcp-"))
        self.assertTrue(self.secret_file.exists())
        self.assertEqual(self.secret_file.read_text(encoding="utf-8"), secret)

    def test_reused_on_the_next_start(self):
        first = self._load_secret()
        self.assertEqual(self._load_secret(), first)

    def test_stored_value_is_read_back_without_trailing_newline(self):
        self.secret_file.write_text("mcp-from-disk" + chr(10), encoding="utf-8")
        self.assertEqual(self._load_secret(), "mcp-from-disk")

    def test_blank_file_is_replaced(self):
        self.secret_file.write_text("   " + chr(10), encoding="utf-8")
        secret = self._load_secret()
        self.assertTrue(secret.startswith("mcp-"))
        self.assertEqual(self.secret_file.read_text(encoding="utf-8"), secret)

    def test_environment_override_wins_and_writes_nothing(self):
        self.assertEqual(
            self._load_secret(ACAD_MCP_SECRET_PATH="/chosen/"), "chosen"
        )
        self.assertFalse(self.secret_file.exists())

    def test_override_beats_a_stored_secret(self):
        self.secret_file.write_text("mcp-from-disk", encoding="utf-8")
        self.assertEqual(
            self._load_secret(ACAD_MCP_SECRET_PATH="chosen"), "chosen"
        )

    def test_generated_secrets_are_not_predictable(self):
        produced = set()
        for _ in range(5):
            self.secret_file.unlink(missing_ok=True)
            produced.add(self._load_secret())
        self.assertEqual(len(produced), 5)

    def test_mcp_path_is_derived_from_the_secret(self):
        self.assertEqual(load(ACAD_MCP_SECRET_PATH="abc").MCP_PATH, "/abc/mcp")


def tearDownModule():
    # Leave one known state behind for whatever imports config next.
    load()


if __name__ == "__main__":
    unittest.main()
