from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from resin_slicer.config import SupportConfig, profile
from resin_slicer.mesh import cube_mesh
from resin_slicer.slicing import prepare_mesh
from resin_slicer.support_cache import (
    load_support_plan_cache,
    store_support_plan_cache,
    support_plan_identity,
)
from resin_slicer.supports import plan_supports
from resin_slicer.transform import MeshTransform


class SupportPlanCacheTests(unittest.TestCase):
    def test_exact_and_prefix_hits_round_trip_without_raster_masks(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=1.0)
        support = SupportConfig(enabled=True, min_island_area_mm2=0.01)
        prepared = prepare_mesh(cube_mesh(8), config, z_offset_mm=5.0)
        bounds = prepared.mesh.bounds()
        point = (
            (bounds.min_x + bounds.max_x) / 2,
            (bounds.min_y + bounds.max_y) / 2,
            bounds.min_z + 0.5,
        )
        plan = plan_supports(
            prepared,
            config,
            support,
            prepared.layer_count,
            manual_points=(point,),
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "model.stl"
            source.write_bytes(b"content identity only")
            identity = support_plan_identity(
                source,
                config,
                support,
                MeshTransform(),
            )
            self.assertRegex(
                identity["support_implementation_sha256"],
                r"^[0-9a-f]{64}$",
            )
            path = store_support_plan_cache(root / "cache", identity, (point,), plan)
            assert path is not None
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["plan"]["raft_mask"]["encoding"], "zlib-base64")
            self.assertEqual(
                payload["plan"]["raft_shadow_mask"]["encoding"],
                "zlib-base64",
            )

            exact = load_support_plan_cache(
                root / "cache",
                identity,
                (point,),
                config,
            )
            prefix = load_support_plan_cache(
                root / "cache",
                identity,
                (point, (point[0] + 1.0, point[1], point[2])),
                config,
            )

        self.assertEqual(exact.status, "hit-exact")
        self.assertIsNotNone(exact.plan)
        assert exact.plan is not None
        self.assertEqual(exact.plan.anchors, plan.anchors)
        self.assertEqual(exact.plan.raft_mask, plan.raft_mask)
        self.assertEqual(exact.plan.raft_shadow_mask, plan.raft_shadow_mask)
        self.assertEqual(prefix.status, "hit-prefix")
        self.assertIsNotNone(prefix.plan)

    def test_checksum_corruption_falls_back_to_complete_planning(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=1.0)
        support = SupportConfig(enabled=True, min_island_area_mm2=0.01)
        prepared = prepare_mesh(cube_mesh(8), config, z_offset_mm=5.0)
        plan = plan_supports(prepared, config, support, prepared.layer_count)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "model.stl"
            source.write_bytes(b"content identity only")
            identity = support_plan_identity(
                source,
                config,
                support,
                MeshTransform(),
            )
            path = store_support_plan_cache(root / "cache", identity, (), plan)
            assert path is not None
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["plan"]["post_radius_px"] += 1
            path.write_text(json.dumps(payload), encoding="utf-8")

            lookup = load_support_plan_cache(
                root / "cache",
                identity,
                (),
                config,
            )

        self.assertEqual(lookup.status, "miss-corrupt")
        self.assertIsNone(lookup.plan)

    def test_valid_checksum_cannot_authorize_out_of_bounds_geometry(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=1.0)
        support = SupportConfig(enabled=True, min_island_area_mm2=0.01)
        prepared = prepare_mesh(cube_mesh(8), config, z_offset_mm=5.0)
        plan = plan_supports(prepared, config, support, prepared.layer_count)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "model.stl"
            source.write_bytes(b"content identity only")
            identity = support_plan_identity(source, config, support, MeshTransform())
            path = store_support_plan_cache(root / "cache", identity, (), plan)
            assert path is not None
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(payload["plan"]["anchors"])
            payload["plan"]["anchors"][0]["x"] = config.resolution_x
            canonical = json.dumps(
                payload["plan"],
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            payload["plan_sha256"] = hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest()
            path.write_text(json.dumps(payload), encoding="utf-8")

            lookup = load_support_plan_cache(
                root / "cache",
                identity,
                (),
                config,
            )

        self.assertEqual(lookup.status, "miss-corrupt")
        self.assertIsNone(lookup.plan)

    def test_manual_request_must_extend_cached_prefix(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=1.0)
        support = SupportConfig(enabled=True, min_island_area_mm2=0.01)
        prepared = prepare_mesh(cube_mesh(8), config, z_offset_mm=5.0)
        bounds = prepared.mesh.bounds()
        point = (bounds.min_x, bounds.min_y, bounds.min_z + 0.5)
        plan = plan_supports(
            prepared,
            config,
            support,
            prepared.layer_count,
            manual_points=(point,),
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "model.stl"
            source.write_bytes(b"content identity only")
            identity = support_plan_identity(source, config, support, MeshTransform())
            store_support_plan_cache(root / "cache", identity, (point,), plan)
            lookup = load_support_plan_cache(
                root / "cache",
                identity,
                ((point[0] + 1.0, point[1], point[2]),),
                config,
            )

        self.assertEqual(lookup.status, "miss-manual-prefix")
        self.assertIsNone(lookup.plan)


if __name__ == "__main__":
    unittest.main()
