from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from resin_slicer.config import profile
from resin_slicer.errors import FormatError
from resin_slicer.formats.aes import aes256_cbc_decrypt, aes256_cbc_zero_encrypt
from resin_slicer.formats.ctb import write_ctb
from resin_slicer.formats.goo import write_goo
from resin_slicer.formats.readers import inspect_sliced_file
from resin_slicer.raster import LayerRaster


class NativeReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = profile("small-test").with_overrides(
            layer_height_mm=1.0,
            bottom_layers=1,
            transition_layers=1,
        )
        self.raster = LayerRaster(self.config.resolution_x, self.config.resolution_y)
        self.raster.set_span(2, 3, 9)

    def test_aes256_cbc_decrypt_round_trip(self) -> None:
        key = bytes(range(32))
        iv = bytes(range(16))
        plaintext = b"native CTB settings" + b"\0" * 13
        encrypted = aes256_cbc_zero_encrypt(plaintext, key, iv)
        self.assertEqual(aes256_cbc_decrypt(encrypted, key, iv), plaintext)

    def test_reads_ctb_v5_layers_as_sparse_rasters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "test.ctb"
            write_ctb(path, self.config, 2, iter(((0, self.raster), (1, self.raster))))
            sliced = inspect_sliced_file(path)
            layers = list(sliced.iter_layers())

        self.assertEqual(sliced.info.format, "ctb")
        self.assertEqual(sliced.info.version, "5")
        self.assertEqual(sliced.info.layer_count, 2)
        self.assertEqual(sliced.info.bottom_layers, 1)
        self.assertEqual(sliced.info.transition_layers, 1)
        self.assertEqual([layer.raster.count_on() for layer in layers], [7, 7])
        self.assertTrue(all(layer.raster.storage_mode == "sparse" for layer in layers))

    def test_reads_one_ctb_layer_directly_and_bounds_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "test.ctb"
            second = LayerRaster(self.config.resolution_x, self.config.resolution_y)
            second.set_span(5, 12, 20)
            write_ctb(path, self.config, 2, iter(((0, self.raster), (1, second))))
            sliced = inspect_sliced_file(path)
            layer = sliced.read_layer(1)

            self.assertEqual(layer.index, 1)
            self.assertEqual(layer.raster.count_on(), 9)
            self.assertEqual(layer.raster.storage_mode, "sparse")
            with self.assertRaises(IndexError):
                sliced.read_layer(2)
            with self.assertRaises(TypeError):
                sliced.read_layer(True)

    def test_reads_goo_v3_and_verifies_checksums(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "test.goo"
            write_goo(path, self.config, 2, iter(((0, self.raster), (1, self.raster))))
            sliced = inspect_sliced_file(path)
            layers = list(sliced.iter_layers())

            payload = bytearray(path.read_bytes())
            payload[-20] ^= 1
            corrupt = Path(temporary) / "corrupt.goo"
            corrupt.write_bytes(payload)
            with self.assertRaisesRegex(FormatError, "checksum mismatch"):
                list(inspect_sliced_file(corrupt).iter_layers())

        self.assertEqual(sliced.info.format, "goo")
        self.assertEqual(sliced.info.bottom_layers, 1)
        self.assertEqual([layer.raster.count_on() for layer in layers], [7, 7])

    def test_rejects_truncated_ctb_section(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "test.ctb"
            write_ctb(path, self.config, 1, iter(((0, self.raster),)))
            path.write_bytes(path.read_bytes()[:100])
            with self.assertRaises(FormatError):
                inspect_sliced_file(path)


if __name__ == "__main__":
    unittest.main()
