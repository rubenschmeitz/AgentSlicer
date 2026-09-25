import unittest
import contextlib
import io
import json
import tempfile
from pathlib import Path
from resin_slicer.layer_preview import parse_layer_selection, main
from resin_slicer.config import profile
from resin_slicer.formats.ctb import write_ctb
from resin_slicer.raster import LayerRaster


class LayerSelectionTests(unittest.TestCase):
    def test_detached_diagnostic_returns_exact_bounds_and_applies_size_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = profile("small-test")
            previous = LayerRaster(config.resolution_x, config.resolution_y)
            current = LayerRaster(config.resolution_x, config.resolution_y)
            previous.add_disk(10,10,2,255)
            current.add_disk(10,10,2,255)
            current.add_disk(30,30,2,255)
            source = root / "island.ctb"
            write_ctb(source, config, 2, iter(((0, previous),(1, current))))
            args = [str(source), "--detached-json", "1", "--attachment-radius", "2"]
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(main(args), 0)
            records = json.loads(stdout.getvalue())["records"]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["bounds_pixels"], {"x":28,"y":28,"width":5,"height":5})
            self.assertEqual(records[0]["layer_index_zero_based"], 1)
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(main(args+["--min-area-px", "1000"]), 0)
            self.assertEqual(json.loads(stdout.getvalue())["records"], [])
            from resin_slicer.config import SupportConfig
            from resin_slicer.supports import build_support_plan
            from resin_slicer.support_cache import support_plan_to_payload
            cache = root / "plan.json"
            plan = build_support_plan((), (), config, SupportConfig(bed_interface="none"))
            cache.write_text(json.dumps({"plan": support_plan_to_payload(plan)}))
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(main(args+["--support-cache", str(cache)]), 0)
            self.assertEqual(json.loads(stdout.getvalue())["records"][0]["support_overlap_pixels_in_bounds"], 0)

    def test_batch_export_matches_exact_crop_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = profile("small-test")
            layer = LayerRaster(config.resolution_x, config.resolution_y)
            layer.set_span(10, 12, 18)
            source = root / "source.ctb"
            write_ctb(source, config, 1, iter(((0, layer),)))
            output = root / "previews"
            args = [str(source), "--layers", "0", "--output-dir", str(output),
                    "--crop", "11", "9", "10", "3", "--scale", "1"]
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(main(args), 0)
            self.assertEqual(json.loads(stdout.getvalue())["indices_zero_based"], [0])
            expected = LayerRaster(10, 3)
            expected.set_span(1, 1, 7)
            self.assertEqual((output / "layer-00000.png").read_bytes(), expected.to_png_bytes())
            with self.assertRaises(FileExistsError):
                main(args)

    def test_ranges_are_zero_based_exclusive_and_deduplicated(self):
        self.assertEqual(parse_layer_selection("4,1:4,2", 10), [1,2,3,4])

    def test_rejects_out_of_file_and_unbounded_exports(self):
        for selection in ("-1", "10", "0:1000000000", "0:257", "3:2", "1:1", "1:2:3", ""):
            with self.assertRaises(ValueError):
                parse_layer_selection(selection, 100 if selection == "0:257" else 10)
