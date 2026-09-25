from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from agent_slicer.validation import CTB_ENCRYPTED_MAGIC, validate_sliced_output


class ValidationTests(unittest.TestCase):
    def test_validates_ctb_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "slice.ctb"
            path.write_bytes(struct.pack("<I", CTB_ENCRYPTED_MAGIC) + bytes(256))
            result = validate_sliced_output(path)
            self.assertEqual(result["status"], "passed")

    def test_validates_goo_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "slice.goo"
            path.write_bytes(b"V3.0" + bytes(256))
            result = validate_sliced_output(path)
            self.assertEqual(result["status"], "passed")

    def test_rejects_unknown_format(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "slice.bin"
            path.write_bytes(bytes(256))
            with self.assertRaises(ValueError):
                validate_sliced_output(path)


if __name__ == "__main__":
    unittest.main()
