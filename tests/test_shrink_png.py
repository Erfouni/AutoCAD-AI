"""capture_view downscales its render with Pillow.

tools._shrink_png is what keeps the plotted PNG cheap for a model to look at.
Without Pillow it quietly returns the file at full plot size, so the input PNG
here is written by hand (no Pillow needed) and the tests fail if nothing shrank.

    python -m unittest discover -s tests -t .
"""

import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

# Must be in place before config is first imported, or that import writes a
# .secret file into the source tree.
os.environ.setdefault("ACAD_MCP_SECRET_PATH", "unit-test-secret")

import tools  # noqa: E402


def write_png(path: Path, width: int, height: int) -> None:
    """A plain 8-bit grayscale PNG, built with zlib alone."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    row = b"\x00" + bytes(x % 256 for x in range(width))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


def png_size(data: bytes) -> tuple[int, int]:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("not a PNG")
    return struct.unpack(">II", data[16:24])


class ShrinkPng(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.png = Path(scratch.name) / "view.png"

    def test_large_render_comes_back_at_the_limit(self):
        write_png(self.png, 2400, 1600)
        data, dimensions = tools._shrink_png(self.png, 1100)
        self.assertEqual(png_size(data), (1100, 733))
        self.assertEqual(dimensions, "1100x733, rendered at 2400x1600")

    def test_small_render_keeps_its_size(self):
        write_png(self.png, 800, 600)
        data, dimensions = tools._shrink_png(self.png, 1100)
        self.assertEqual(png_size(data), (800, 600))
        self.assertEqual(dimensions, "800x600")


if __name__ == "__main__":
    unittest.main()
