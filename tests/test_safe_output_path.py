"""tools._safe_output_path keeps every file inside ACAD_MCP_SAVE_DIR.

save_drawing, export_pdf, open_drawing and the save_first option all take a
file name from the model on the other end of the tunnel, and all of them go
through this one function. Needs no AutoCAD: nothing here reaches COM.

    python -m unittest discover -s tests -t .
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Must be in place before config is first imported, or that import writes a
# .secret file into the source tree.
os.environ.setdefault("ACAD_MCP_SECRET_PATH", "unit-test-secret")

import config  # noqa: E402
import tools  # noqa: E402
from acad import AcadError  # noqa: E402


class SafeOutputPath(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.save_dir = self.root / "Out"
        self.save_dir.mkdir()
        patcher = mock.patch.object(config, "SAVE_DIR", self.save_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assertInsideSaveDir(self, path: Path):
        self.assertTrue(
            path.is_relative_to(self.save_dir),
            f"{path} is outside {self.save_dir}",
        )

    def test_plain_name_gets_the_extension(self):
        self.assertEqual(
            tools._safe_output_path("plan", ".dwg"), self.save_dir / "plan.dwg"
        )

    def test_quotes_and_whitespace_are_stripped(self):
        self.assertEqual(
            tools._safe_output_path(' "plan.dwg" ', ".dwg"), self.save_dir / "plan.dwg"
        )

    def test_wrong_extension_is_replaced(self):
        self.assertEqual(
            tools._safe_output_path("plan.exe", ".pdf"), self.save_dir / "plan.pdf"
        )

    def test_subfolder_is_allowed_and_created(self):
        target = tools._safe_output_path("floor-1/plan.dwg", ".dwg")
        self.assertEqual(target, self.save_dir / "floor-1" / "plan.dwg")
        self.assertTrue(target.parent.is_dir())

    def test_parent_traversal_is_reduced_to_the_file_name(self):
        target = tools._safe_output_path("../../evil.dwg", ".dwg")
        self.assertEqual(target, self.save_dir / "evil.dwg")

    def test_absolute_path_is_reduced_to_the_file_name(self):
        elsewhere = self.root / "elsewhere" / "evil.dwg"
        target = tools._safe_output_path(str(elsewhere), ".dwg")
        self.assertEqual(target, self.save_dir / "evil.dwg")
        self.assertFalse(elsewhere.parent.exists())

    def test_rooted_path_cannot_reach_a_sibling_sharing_the_prefix(self):
        # "\...\Out-evil\x.dwg" has a root but no drive, so is_absolute() is
        # False on Windows, and the old check compared strings: "...\Out-evil"
        # starts with "...\Out". Together they let a file land outside.
        sibling = self.root / "Out-evil"
        rooted = os.sep + str(sibling.relative_to(sibling.anchor))
        target = tools._safe_output_path(os.path.join(rooted, "x.dwg"), ".dwg")
        self.assertInsideSaveDir(target)
        self.assertFalse(sibling.exists(), "a folder was created outside SAVE_DIR")

    @unittest.skipUnless(os.name == "nt", "drive-relative paths are a Windows concept")
    def test_drive_relative_path_is_reduced_to_the_file_name(self):
        target = tools._safe_output_path("D:evil.dwg", ".dwg")
        self.assertEqual(target, self.save_dir / "evil.dwg")

    def test_empty_name_is_rejected(self):
        with self.assertRaises(AcadError):
            tools._safe_output_path("  ", ".dwg")


if __name__ == "__main__":
    unittest.main()
