import tempfile
import struct
import unittest
from unittest.mock import patch
from pathlib import Path

from resin_slicer.config import SupportConfig, profile
from resin_slicer.errors import FormatError
from resin_slicer.formats.binary import ByteWriter
from resin_slicer.formats.ctb import (
    PAGE_SIZE,
    _build_settings,
    _encode_layer,
    _paged_offset,
    _write_resin,
    write_ctb,
)
from resin_slicer.formats.goo import (
    HEADER_SIZE as GOO_HEADER_SIZE,
    _encode_layer as _encode_goo_layer,
    _write_header as _write_goo_header,
)
from resin_slicer.mesh import cube_mesh
from resin_slicer.pipeline import SliceJob, slice_to_file
from resin_slicer.raster import LayerRaster
from resin_slicer.transform import MeshTransform


class FormatSmokeTests(unittest.TestCase):
    def test_goo_header_preserves_bottom_and_two_stage_motion(self) -> None:
        config = profile("small-test").with_overrides(
            bottom_layers=1,
            transition_layers=0,
            exposure_time_s=2.5,
            bottom_exposure_time_s=30.0,
            lift_distance_mm=3.0,
            lift_speed_mm_min=60.0,
            bottom_lift_distance_mm=4.0,
            bottom_lift_speed_mm_min=40.0,
            lift_distance2_mm=2.0,
            lift_speed2_mm_min=120.0,
            retract_distance_mm=5.0,
            retract_speed_mm_min=150.0,
            bottom_retract_distance_mm=6.0,
            bottom_retract_speed_mm_min=80.0,
            retract_distance2_mm=1.5,
            retract_speed2_mm_min=90.0,
            rest_before_lift_s=0.3,
            rest_after_lift_s=0.4,
            wait_after_retract_s=0.2,
        )
        writer = ByteWriter()
        _write_goo_header(writer, config, 2, 12.0)
        payload = bytes(writer.data)

        self.assertEqual(len(payload), GOO_HEADER_SIZE)
        motion_offset = GOO_HEADER_SIZE - 100
        self.assertEqual(
            struct.unpack_from(">16f", payload, motion_offset),
            (
                4.0,
                40.0,
                3.0,
                60.0,
                6.0,
                80.0,
                5.0,
                150.0,
                2.0,
                120.0,
                2.0,
                120.0,
                1.5,
                90.0,
                1.5,
                90.0,
            ),
        )
        self.assertEqual(payload[motion_offset + 68], 1)
        self.assertEqual(struct.unpack_from(">I", payload, motion_offset + 69)[0], 353)

    def test_software_transition_layer_exposures_match_reference_steps(self) -> None:
        config = profile("small-test").with_overrides(
            bottom_layers=1,
            transition_layers=2,
            bottom_exposure_time_s=30.0,
            exposure_time_s=3.0,
        )
        raster = LayerRaster(config.resolution_x, config.resolution_y)

        ctb_exposures = [_encode_layer(index, raster, config).exposure_s for index in range(4)]
        goo_exposures = [_encode_goo_layer(index, raster, config).exposure_s for index in range(4)]
        self.assertEqual(ctb_exposures, [30.0, 21.0, 12.0, 3.0])
        self.assertEqual(goo_exposures, ctb_exposures)

    def test_ctb_motion_settings_match_reference_field_semantics(self) -> None:
        config = profile("small-test").with_overrides(
            bottom_layers=1,
            transition_layers=0,
            exposure_time_s=2.5,
            bottom_exposure_time_s=30.0,
            lift_distance_mm=3.0,
            lift_speed_mm_min=60.0,
            bottom_lift_distance_mm=4.0,
            bottom_lift_speed_mm_min=40.0,
            lift_distance2_mm=2.0,
            lift_speed2_mm_min=120.0,
            retract_distance_mm=5.0,
            retract_speed_mm_min=150.0,
            retract_distance2_mm=1.5,
            retract_speed2_mm_min=90.0,
            rest_before_lift_s=0.3,
            rest_after_lift_s=0.4,
            wait_after_retract_s=0.2,
        )
        settings = bytes(_build_settings(config, [None, None], 12.0, 0).data)  # type: ignore[list-item]

        self.assertEqual(struct.unpack_from("<I", settings, 76)[0], 351)
        self.assertEqual(struct.unpack_from("<4f", settings, 84), (6.0, 40.0, 5.0, 60.0))
        second_stage = struct.unpack_from("<7f", settings, 132)
        self.assertEqual(second_stage[:6], (2.0, 120.0, 2.0, 120.0, 1.5, 90.0))
        self.assertAlmostEqual(second_stage[6], 0.4)
        self.assertAlmostEqual(struct.unpack_from("<f", settings, 184)[0], 0.4)
        self.assertAlmostEqual(struct.unpack_from("<f", settings, 196)[0], 90.0)
        self.assertAlmostEqual(struct.unpack_from("<f", settings, 216)[0], 0.2)
        self.assertAlmostEqual(struct.unpack_from("<f", settings, 220)[0], 0.4)
        self.assertAlmostEqual(struct.unpack_from("<f", settings, 224)[0], 0.3)
        self.assertAlmostEqual(struct.unpack_from("<f", settings, 228)[0], 1.5)

        raster = LayerRaster(config.resolution_x, config.resolution_y)
        bottom_layer = _encode_layer(0, raster, config)
        normal_layer = _encode_layer(1, raster, config)
        self.assertEqual((bottom_layer.lift_height_mm, bottom_layer.lift_height2_mm), (6.0, 2.0))
        self.assertEqual((normal_layer.lift_height_mm, normal_layer.lift_height2_mm), (5.0, 2.0))

    def test_ctb_page_offsets_cross_four_gibibyte_boundary(self) -> None:
        self.assertEqual(_paged_offset(PAGE_SIZE - 1), (PAGE_SIZE - 1, 0))
        self.assertEqual(_paged_offset(PAGE_SIZE), (0, 1))
        self.assertEqual(_paged_offset(PAGE_SIZE + 123), (123, 1))

    def test_ctb_v5_resin_strings_follow_serialized_field_order(self) -> None:
        config = profile("small-test").with_overrides(
            machine_name="Test Machine",
            resin_name="Test Resin",
        )
        writer = ByteWriter()
        _write_resin(writer, config)
        payload = bytes(writer.data)

        machine_address = struct.unpack_from("<I", payload, 8)[0]
        resin_type_length, resin_type_address = struct.unpack_from("<II", payload, 12)
        resin_name_length, resin_name_address = struct.unpack_from("<II", payload, 20)
        machine_length = struct.unpack_from("<I", payload, 28)[0]
        self.assertEqual(payload[resin_type_address : resin_type_address + resin_type_length], b"Normal")
        self.assertEqual(payload[resin_name_address : resin_name_address + resin_name_length], b"Test Resin")
        self.assertEqual(payload[machine_address : machine_address + machine_length], b"Test Machine")
        self.assertEqual(
            payload[40:],
            b"NormalTest ResinTest Machine",
        )

    def test_writes_goo_and_ctb(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=2.0)
        job = SliceJob(cube_mesh(6), config, SupportConfig(enabled=True))
        with tempfile.TemporaryDirectory() as tmp:
            goo_path = Path(tmp) / "cube.goo"
            ctb_path = Path(tmp) / "cube.ctb"
            goo_result = slice_to_file(job, goo_path, "goo")
            ctb_result = slice_to_file(job, ctb_path, "ctb")

            self.assertTrue(goo_path.read_bytes().startswith(b"V3.0"))
            ctb_payload = ctb_path.read_bytes()
            self.assertEqual(ctb_payload[:4], (0x12FD0107).to_bytes(4, "little"))
            signature_size, signature_offset = struct.unpack_from("<II", ctb_payload, 20)
            self.assertEqual(signature_size, 32)
            self.assertEqual(signature_offset, len(ctb_payload) - 36)
            self.assertEqual(
                ctb_payload[signature_offset - 8 : signature_offset],
                struct.pack("<II", 0x422052FA, 0),
            )
            self.assertEqual(
                ctb_payload[-4:],
                struct.pack("<I", 0x6D4232B3),
            )
            # The sixth plane is at z=11, exactly at the lifted cube's top;
            # it has no section and must not become an empty exposure layer.
            self.assertEqual(goo_result.layer_count, 5)
            self.assertEqual(ctb_result.layer_count, 5)
            self.assertEqual(goo_result.trimmed_trailing_empty_layers, 1)
            self.assertEqual(ctb_result.trimmed_trailing_empty_layers, 1)
            self.assertGreater(goo_result.support_count, 0)
            self.assertGreater(ctb_result.support_count, 0)
            self.assertGreater(goo_path.stat().st_size, 1000)
            self.assertGreater(ctb_path.stat().st_size, 1000)
            self.assertEqual(goo_result.output_storage_mode, "disk-spooled-atomic")
            self.assertEqual(ctb_result.output_storage_mode, "disk-spooled-atomic")
            self.assertGreater(goo_result.encoded_layer_bytes, goo_result.max_encoded_layer_bytes)
            self.assertGreater(ctb_result.encoded_layer_bytes, ctb_result.max_encoded_layer_bytes)
            self.assertTrue(goo_result.encode_validation_parity.matches)
            self.assertTrue(ctb_result.encode_validation_parity.matches)
            self.assertEqual(goo_result.post_encode_validation.format_version, "3.0")
            self.assertEqual(ctb_result.post_encode_validation.format_version, "5")
            self.assertEqual(
                ctb_result.post_encode_validation.layer_validation.mode,
                "post-encode-native-decoded-ctb-v1",
            )

    def test_failed_post_encode_validation_preserves_existing_output(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=2.0)
        job = SliceJob(cube_mesh(6), config, SupportConfig(enabled=False))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "existing.ctb"
            path.write_bytes(b"previous-good-output")
            with patch(
                "resin_slicer.pipeline.validate_encoded_file",
                side_effect=FormatError("simulated native decode failure"),
            ), self.assertRaisesRegex(FormatError, "simulated native decode failure"):
                slice_to_file(job, path, "ctb")

            self.assertEqual(path.read_bytes(), b"previous-good-output")
            self.assertEqual(list(path.parent.glob(f".{path.name}.native-validate.*")), [])

    def test_failed_layer_sequence_preserves_existing_output(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=2.0)
        raster = LayerRaster(config.resolution_x, config.resolution_y)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "existing.ctb"
            path.write_bytes(b"previous-good-output")

            with self.assertRaisesRegex(FormatError, "expected 1, received 2"):
                write_ctb(path, config, 2, iter(((0, raster), (2, raster))))

            self.assertEqual(path.read_bytes(), b"previous-good-output")
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_writes_with_rotation(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=2.0)
        job = SliceJob(cube_mesh(6), config, SupportConfig(enabled=False), MeshTransform(rotate_z_deg=30))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cube.goo"
            result = slice_to_file(job, path, "goo")
            self.assertTrue(path.exists())
            self.assertGreater(result.layer_count, 0)

    def test_writes_with_parallel_layer_rendering(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=2.0)
        job = SliceJob(cube_mesh(6), config, SupportConfig(enabled=True))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "parallel.goo"
            result = slice_to_file(job, path, "goo", layer_workers=2)

            self.assertTrue(path.exists())
            self.assertEqual(result.layer_count, 5)
            self.assertEqual(result.trimmed_trailing_empty_layers, 1)
            self.assertGreater(result.support_count, 0)


if __name__ == "__main__":
    unittest.main()
