import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from resin_slicer.config import SupportConfig, profile
from resin_slicer.errors import ConfigError
from resin_slicer.mesh import cube_mesh
from resin_slicer.pipeline import SliceJob, slice_to_file
from resin_slicer.raster import LayerRaster


class TrailingLayerTests(unittest.TestCase):
    def job(self):
        return SliceJob(cube_mesh(5.01),
                        profile("small-test").with_overrides(layer_height_mm=.5),
                        SupportConfig(enabled=False))

    def test_rounding_tail_removed_without_losing_printed_geometry(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = slice_to_file(self.job(), Path(tmp)/"cube.ctb", "ctb")
            self.assertEqual(result.layer_count, 10)
            self.assertEqual(result.trimmed_trailing_empty_layers, 1)
            self.assertEqual(result.layer_validation.trailing_empty_layers, 0)
            self.assertTrue(result.encode_validation_parity.matches)

    def test_internal_gap_is_preserved_and_reported(self):
        def render(_prepared, _model, config, _enabled, _plan, index, _cad=None):
            raster = LayerRaster(config.resolution_x, config.resolution_y)
            if index not in (4, 10):
                raster.add_disk(30,30,3,255)
            return index, raster
        with tempfile.TemporaryDirectory() as tmp, patch(
                "resin_slicer.pipeline._render_layer", side_effect=render):
            result = slice_to_file(self.job(), Path(tmp)/"gap.ctb", "ctb")
            self.assertEqual(result.layer_count, 10)
            self.assertEqual(result.layer_validation.internal_empty_layers, 1)

    def test_entirely_empty_print_fails_without_publishing(self):
        def render(_prepared, _model, config, _enabled, _plan, index, _cad=None):
            return index, LayerRaster(config.resolution_x, config.resolution_y)
        with tempfile.TemporaryDirectory() as tmp, patch(
                "resin_slicer.pipeline._render_layer", side_effect=render):
            output = Path(tmp)/"empty.ctb"
            with self.assertRaisesRegex(ConfigError, "No nonempty"):
                slice_to_file(self.job(), output, "ctb")
            self.assertFalse(output.exists())
