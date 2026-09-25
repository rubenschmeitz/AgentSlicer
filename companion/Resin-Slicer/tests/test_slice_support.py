import unittest
from unittest.mock import patch

from resin_slicer.config import SupportConfig, profile
from resin_slicer.electron_bridge import _support_from_request
from resin_slicer.errors import ConfigError
from resin_slicer.mesh import Mesh, cube_mesh
from resin_slicer.raster import Component, LayerRaster
from resin_slicer.pipeline import SliceJob, slice_to_file
from resin_slicer.slicing import prepare_mesh, render_model_layer, render_prepared_layer
from resin_slicer.slicing import _build_layer_index
from resin_slicer.supports import (
    PaintZone,
    SupportAnchor,
    _SurfaceNormalSampler,
    _assign_tree_loads,
    _anchor_candidates,
    _bed_base_candidates,
    _bed_base_fits,
    _find_branch_route,
    _find_dogleg_route,
    _find_enforcer_route,
    _find_nearby_manual_contact_route,
    _find_support_route,
    _occupied_layer,
    _support_angle_deg,
    _support_radius_at_layer,
    apply_supports,
    attach_raft_masks,
    build_support_plan,
    extend_support_plan,
    plan_supports,
)


class SliceSupportTests(unittest.TestCase):
    def test_bed_candidates_reserve_full_raft_margin_at_every_edge(self):
        config = profile("small-test").with_overrides(layer_height_mm=.5)
        support = SupportConfig(foot_radius_mm=1.0, raft_margin_mm=2.0)
        for x, y in ((1, 45), (158, 45), (80, 1), (80, 88)):
            self.assertFalse(_bed_base_fits(x, y, config, support))
            candidates = list(_bed_base_candidates(x, y, 40, config, support))
            self.assertTrue(candidates)
            for bx, by in candidates:
                self.assertTrue(_bed_base_fits(bx, by, config, support))
                # Six pixels for foot + raft, with a reserved dark border.
                self.assertGreater(bx-6, 0)
                self.assertGreater(by-6, 0)
                self.assertLess(bx+6, config.resolution_x-1)
                self.assertLess(by+6, config.resolution_y-1)

    def test_manual_contact_near_edge_routes_to_inset_base(self):
        config = profile("small-test").with_overrides(layer_height_mm=.5)
        support = SupportConfig(branching_enabled=False, foot_radius_mm=1.0,
                                raft_margin_mm=2.0)
        route = _find_support_route([], config, support, 2, 45, 40,
                                    (0.0, 0.0, -1.0), 1, 1, "manual")
        self.assertIsNotNone(route)
        self.assertEqual(route.kind, "bed")
        self.assertTrue(_bed_base_fits(route.base_x, route.base_y, config, support))
        self.assertNotEqual(route.base_x, route.x)

    def test_adaptive_short_triangle_index_is_exact_for_every_layer(self):
        config = profile("small-test").with_overrides(layer_height_mm=.05)
        # Short disconnected cubes exercise empty layers and bucket boundaries.
        triangles = []
        for offset in (.001, .201, .851):
            triangles.extend(cube_mesh(.08).transformed((1, 1, offset)).triangles)
        mesh = Mesh(tuple(triangles))
        prepared = prepare_mesh(mesh, config)
        exact = prepared.layer_triangles
        compact = _build_layer_index(prepared.mesh, config, prepared.layer_count, block_size=64)
        self.assertEqual(exact.block_size, 1)
        for index in reversed(range(prepared.layer_count)):
            self.assertEqual(list(exact.indices_for_layer(index)), list(compact.indices_for_layer(index)))
            self.assertEqual(render_prepared_layer(prepared, config, index).pixels,
                             render_model_layer(prepared.mesh, config, index).pixels)
        self.assertEqual(list(exact.indices_for_layer(-1)), [])
        self.assertEqual(list(exact.indices_for_layer(prepared.layer_count)), [])

    def test_concave_island_contacts_are_always_on_the_component(self) -> None:
        config = profile("small-test")
        width = config.resolution_x
        pixels = {
            y * width + x
            for y in range(10, 31)
            for x in range(10, 31)
            if x in {10, 30} or y == 30
        }
        component = Component(tuple(sorted(pixels)), 10, 10, 30, 30)
        candidates = _anchor_candidates(
            component,
            config,
            SupportConfig(support_spacing_mm=100.0),
        )

        self.assertEqual(len(candidates), 1)
        candidate_index = candidates[0].y * width + candidates[0].x
        self.assertIn(candidate_index, pixels)
        # The rounded center of mass is in the empty middle of this U shape.
        cx, cy = component.centroid(width)
        self.assertNotIn(round(cy) * width + round(cx), pixels)

    def test_large_island_sampling_covers_perimeter_extremes(self) -> None:
        config = profile("small-test")
        width = config.resolution_x
        pixels = tuple(
            y * width + x
            for y in range(10, 31)
            for x in range(10, 91)
        )
        component = Component(pixels, 10, 10, 90, 30)
        candidates = _anchor_candidates(
            component,
            config,
            SupportConfig(
                support_spacing_mm=5.0,
                max_supports_per_island=12,
            ),
        )

        self.assertGreaterEqual(len(candidates), 4)
        self.assertTrue(
            all(candidate.y * width + candidate.x in set(pixels) for candidate in candidates)
        )
        xs = [candidate.x for candidate in candidates]
        ys = [candidate.y for candidate in candidates]
        self.assertLessEqual(min(xs), 12)
        self.assertGreaterEqual(max(xs), 88)
        self.assertLessEqual(min(ys), 12)
        self.assertGreaterEqual(max(ys), 28)

    def test_surface_normal_index_is_bounded_for_large_meshes(self) -> None:
        triangles = cube_mesh(1).triangles * 100
        with patch("resin_slicer.supports._surface_normal_sample_limit", return_value=25):
            sampler = _SurfaceNormalSampler(triangles)
        self.assertEqual(sampler.sample_stride, 48)
        self.assertLessEqual(len(sampler.surfaces), 25)

    def test_cube_slices_to_non_empty_layers(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=1.0)
        prepared = prepare_mesh(cube_mesh(8), config)
        counts = [render_model_layer(prepared.mesh, config, i).count_on() for i in range(prepared.layer_count)]
        self.assertEqual(prepared.layer_count, 8)
        self.assertTrue(all(count > 0 for count in counts))

    def test_block_interval_index_matches_dense_render_with_fewer_references(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=0.05)
        prepared = prepare_mesh(cube_mesh(20), config)
        index = prepared.layer_triangles

        self.assertLess(index.reference_count, index.dense_reference_count)
        self.assertLess(index.reference_count * 20, index.dense_reference_count)
        for layer_index in (0, 63, 64, prepared.layer_count - 1):
            with self.subTest(layer=layer_index):
                dense = render_model_layer(prepared.mesh, config, layer_index)
                compact = render_prepared_layer(prepared, config, layer_index)
                self.assertEqual(compact.pixels, dense.pixels)

    def test_overlapping_bodies_fill_their_union_not_even_odd(self) -> None:
        # Two overlapping, non-boolean-unioned solids (e.g. a multi-body CAD
        # export flattened to one STL) must fill the union of both bodies.
        # An even-odd fill rule would cancel the overlap and leave it hollow.
        config = profile("small-test").with_overrides(layer_height_mm=1.0)
        cube_a = cube_mesh(10)
        cube_b = cube_mesh(10).transformed((6, 0, 0))
        combined = Mesh(cube_a.triangles + cube_b.triangles)
        prepared = prepare_mesh(combined, config)
        mid_layer = prepared.layer_count // 2
        raster = render_model_layer(prepared.mesh, config, mid_layer)

        y_mid = config.resolution_y // 2
        row = raster.pixels[y_mid * raster.width : (y_mid + 1) * raster.width]
        on_xs = [x for x, value in enumerate(row) if value]
        gaps = [b - a for a, b in zip(on_xs, on_xs[1:]) if b - a > 1]
        self.assertEqual(gaps, [], "overlap region between the two bodies must stay filled")

    def test_preserve_coordinates_keeps_plate_position(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=1.0, center_model=False)
        prepared = prepare_mesh(cube_mesh(8).transformed((20, 10, 3)), config, preserve_coordinates=True)
        bounds = prepared.mesh.bounds()
        self.assertAlmostEqual(bounds.min_x, 20)
        self.assertAlmostEqual(bounds.min_y, 10)
        self.assertAlmostEqual(bounds.min_z, 3)
        self.assertAlmostEqual(bounds.max_x, 28)
        self.assertAlmostEqual(bounds.max_y, 18)
        self.assertAlmostEqual(bounds.max_z, 11)
        self.assertEqual(prepared.layer_count, 11)

    def test_supports_are_deterministic_and_add_pixels(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=1.0)
        prepared = prepare_mesh(_ledge_mesh(), config)
        support = SupportConfig(enabled=True, support_spacing_mm=4.0, min_island_area_mm2=0.01)
        plan_a = plan_supports(prepared, config, support, prepared.layer_count)
        plan_b = plan_supports(prepared, config, support, prepared.layer_count)
        self.assertEqual(plan_a, plan_b)
        self.assertGreater(len(plan_a.anchors), 0)

        layer = render_model_layer(prepared.mesh, config, 0)
        before = layer.count_on()
        apply_supports(layer, 0, plan_a)
        self.assertGreaterEqual(layer.count_on(), before)
        self.assertGreater(plan_a.auto_components_detected, 0)
        self.assertEqual(plan_a.auto_components_unrouted, 0)
        self.assertEqual(
            plan_a.auto_components_supported,
            plan_a.auto_components_detected,
        )

    def test_strict_auto_supports_fail_before_writing_unroutable_job(self) -> None:
        import tempfile
        from pathlib import Path

        config = profile("small-test").with_overrides(layer_height_mm=0.5)
        support = SupportConfig(
            enabled=True,
            model_lift_mm=5.0,
            support_spacing_mm=2.0,
            min_island_area_mm2=0.01,
            overhang_angle_deg=70,
            branching_enabled=False,
            enforcers_enabled=False,
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "must-not-exist.ctb"
            with (
                patch("resin_slicer.supports._find_support_route", return_value=None),
                self.assertRaisesRegex(ConfigError, "could not be routed safely"),
            ):
                slice_to_file(
                    SliceJob(
                        _ledge_mesh(),
                        config,
                        support,
                        require_auto_supports=True,
                    ),
                    output,
                    "ctb",
                )
            self.assertFalse(output.exists())

    def test_supported_pipeline_lifts_plate_contact_model(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=1.0)
        support = SupportConfig(enabled=True, model_lift_mm=4.0, support_spacing_mm=4.0)
        prepared = prepare_mesh(cube_mesh(8), config, z_offset_mm=support.model_lift_mm)
        plan = plan_supports(prepared, config, support, prepared.layer_count)
        self.assertGreater(len(plan.anchors), 0)
        self.assertGreater(prepared.layer_count, 8)

    def test_attach_raft_masks_matches_full_plan(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=1.0)
        support = SupportConfig(enabled=True, model_lift_mm=4.0, support_spacing_mm=4.0, bed_interface="raft")
        prepared = prepare_mesh(cube_mesh(8), config, z_offset_mm=support.model_lift_mm)

        full = plan_supports(prepared, config, support, prepared.layer_count)
        bare = plan_supports(prepared, config, support, prepared.layer_count, include_raft_mask=False)
        self.assertIsNone(bare.raft_mask)

        attached = attach_raft_masks(bare, prepared, config, support)
        self.assertEqual(attached, full)

    def test_manual_point_adds_routed_anchor(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=1.0)
        # A cube lifted off the plate so a manual support has room to route down.
        prepared = prepare_mesh(cube_mesh(8).transformed((20, 10, 6)), config, preserve_coordinates=True)
        support = SupportConfig(enabled=True, support_spacing_mm=4.0, min_island_area_mm2=0.01)
        bounds = prepared.mesh.bounds()
        click = ((bounds.min_x + bounds.max_x) / 2, (bounds.min_y + bounds.max_y) / 2, bounds.min_z + 0.5)

        base = plan_supports(prepared, config, support, prepared.layer_count)
        with_manual = plan_supports(prepared, config, support, prepared.layer_count, manual_points=(click,))

        manual_anchors = [anchor for anchor in with_manual.anchors if anchor.role == "manual"]
        self.assertEqual(len(with_manual.anchors), len(base.anchors) + len(manual_anchors))
        self.assertEqual(len(manual_anchors), 1)
        self.assertEqual(with_manual.manual_requested, 1)
        self.assertEqual(with_manual.manual_routed, 1)
        anchor = manual_anchors[0]
        # The routed anchor's tip lands at the clicked location and reaches the plate.
        self.assertAlmostEqual((anchor.x + 0.5) * config.pixel_size_x_mm, click[0], delta=config.pixel_size_x_mm * 2)
        self.assertAlmostEqual((anchor.y + 0.5) * config.pixel_size_y_mm, click[1], delta=config.pixel_size_y_mm * 2)
        self.assertEqual(anchor.base_layer, 0)

    def test_cached_plan_extension_matches_complete_manual_replan(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=1.0)
        prepared = prepare_mesh(
            cube_mesh(8).transformed((20, 10, 6)),
            config,
            preserve_coordinates=True,
        )
        support = SupportConfig(
            enabled=True,
            support_spacing_mm=4.0,
            min_island_area_mm2=0.01,
        )
        bounds = prepared.mesh.bounds()
        click = (
            (bounds.min_x + bounds.max_x) / 2,
            (bounds.min_y + bounds.max_y) / 2,
            bounds.min_z + 0.5,
        )

        base = plan_supports(prepared, config, support, prepared.layer_count)
        complete = plan_supports(
            prepared,
            config,
            support,
            prepared.layer_count,
            manual_points=(click,),
        )
        extended = extend_support_plan(
            prepared,
            config,
            support,
            prepared.layer_count,
            base,
            (click,),
        )

        self.assertEqual(extended.anchors, complete.anchors)
        self.assertEqual(extended.braces, complete.braces)
        self.assertEqual(extended.manual_routed_points, complete.manual_routed_points)
        self.assertEqual(extended.manual_unrouted_points, complete.manual_unrouted_points)
        self.assertEqual(extended.tree_root_count, complete.tree_root_count)
        self.assertEqual(extended.tree_branch_count, complete.tree_branch_count)

    def test_manual_only_routes_just_the_manual_points(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=1.0)
        prepared = prepare_mesh(cube_mesh(8).transformed((20, 10, 6)), config, preserve_coordinates=True)
        support = SupportConfig(enabled=True, support_spacing_mm=4.0, min_island_area_mm2=0.01)
        bounds = prepared.mesh.bounds()
        click = ((bounds.min_x + bounds.max_x) / 2, (bounds.min_y + bounds.max_y) / 2, bounds.min_z + 0.5)

        # Auto analysis would normally place several anchors; manual_only skips it
        # and routes only the supplied point.
        auto = plan_supports(prepared, config, support, prepared.layer_count)
        self.assertGreater(len(auto.anchors), 1)

        manual_only = plan_supports(
            prepared, config, support, prepared.layer_count, manual_points=(click,), manual_only=True
        )
        self.assertEqual(len(manual_only.anchors), 1)
        self.assertTrue(all(anchor.role == "manual" for anchor in manual_only.anchors))
        self.assertEqual(len(manual_only.braces), 0)
        self.assertEqual(manual_only.anchors[0].base_layer, 0)

    def test_manual_points_retry_after_later_tree_parent_routes(self) -> None:
        from unittest.mock import patch

        config = profile("small-test").with_overrides(layer_height_mm=1.0)
        prepared = prepare_mesh(cube_mesh(8).transformed((20, 10, 6)), config, preserve_coordinates=True)
        support = SupportConfig(enabled=True, enforcers_enabled=True)
        first = (20.5, 10.5, 6.5)
        second = (22.5, 10.5, 6.5)

        def route_after_parent(*args, **kwargs):
            top_x, top_y, top_layer = args[3], args[4], args[5]
            parents = kwargs.get("branch_anchors", ())
            if top_x < 22 and not parents:
                return None
            return SupportAnchor(
                top_x,
                top_y,
                top_layer,
                base_x=top_x,
                base_y=top_y,
                base_layer=0,
                joint_x=top_x,
                joint_y=top_y,
                joint_layer=max(0, top_layer - 1),
                kind="branch" if parents else "bed",
                role="manual",
            )

        with patch("resin_slicer.supports._find_support_route", side_effect=route_after_parent):
            plan = plan_supports(
                prepared,
                config,
                support,
                prepared.layer_count,
                manual_points=(first, second),
                manual_only=True,
            )

        self.assertEqual(plan.manual_requested, 2)
        self.assertEqual(plan.manual_routed, 2)
        self.assertEqual(plan.manual_unrouted_points, ())
        self.assertCountEqual(plan.manual_routed_points, (first, second))
        self.assertEqual(len(plan.anchors), 2)

    def test_manual_route_retries_with_vertical_tip_when_sampled_normal_is_blocked(self) -> None:
        from math import sqrt

        config = profile("small-test").with_overrides(layer_height_mm=1.0)
        support = SupportConfig(
            enabled=True,
            tip_length_mm=3.0,
            post_radius_mm=0.2,
            tip_radius_mm=0.2,
            collision_clearance_mm=0.0,
            enforcers_enabled=False,
        )
        occupied = []
        for layer_index in range(8):
            layer = LayerRaster(config.resolution_x, config.resolution_y)
            # The sampled diagonal tip would put its joint at x=16, inside this
            # obstacle. The detector contact at x=10 has clear space below it.
            layer.add_disk(16, 10, 0, 255)
            occupied_layer = _occupied_layer(layer_index, layer)
            assert occupied_layer is not None
            occupied.append(occupied_layer)

        route = _find_support_route(
            occupied,
            config,
            support,
            10,
            10,
            10,
            (1.0 / sqrt(2.0), 0.0, -1.0 / sqrt(2.0)),
            1,
            1,
            "manual",
        )

        self.assertIsNotNone(route)
        assert route is not None
        self.assertEqual((route.joint_x, route.joint_y), (10, 10))
        self.assertEqual((route.normal_x, route.normal_y, route.normal_z), (0.0, 0.0, -1.0))

    def test_dogleg_route_builds_collision_checked_trunk_and_branch(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=0.5)
        support = SupportConfig(enabled=True, max_support_angle_deg=35.0)

        dogleg = _find_dogleg_route(
            [],
            config,
            support,
            60,
            40,
            40,
            1,
            1,
            "manual",
        )

        self.assertIsNotNone(dogleg)
        assert dogleg is not None
        trunk, branch = dogleg
        self.assertEqual((trunk.kind, trunk.role), ("bed", "manual-trunk"))
        self.assertEqual(branch.kind, "branch")
        self.assertEqual((branch.base_x, branch.base_y, branch.base_layer), (trunk.x, trunk.y, trunk.top_layer))
        self.assertLessEqual(_support_angle_deg(trunk, config), support.max_support_angle_deg)
        self.assertLessEqual(_support_angle_deg(branch, config), support.max_support_angle_deg)

    def test_dogleg_route_can_slope_trunk_around_lower_obstruction(self) -> None:
        from unittest.mock import patch

        config = profile("small-test").with_overrides(layer_height_mm=0.5)
        support = SupportConfig(enabled=True, max_support_angle_deg=35.0)

        def vertical_trunk_is_blocked(_layers, _width, _height, anchor, _radius):
            if anchor.kind == "branch":
                return False
            return (anchor.base_x, anchor.base_y) == (anchor.joint_x, anchor.joint_y)

        with patch("resin_slicer.supports._route_collides", side_effect=vertical_trunk_is_blocked):
            dogleg = _find_dogleg_route(
                [], config, support, 60, 40, 40, 1, 1, "manual"
            )

        self.assertIsNotNone(dogleg)
        assert dogleg is not None
        trunk, _branch = dogleg
        self.assertNotEqual((trunk.base_x, trunk.base_y), (trunk.joint_x, trunk.joint_y))
        self.assertLessEqual(_support_angle_deg(trunk, config), support.max_support_angle_deg)

    def test_blocked_manual_contact_moves_only_within_same_unsupported_patch(self) -> None:
        from unittest.mock import patch

        config = profile("small-test").with_overrides(
            layer_height_mm=0.5,
            size_x_mm=16.0,
            size_y_mm=9.0,
        )
        support = SupportConfig(enabled=True)
        current = LayerRaster(config.resolution_x, config.resolution_y)
        previous = LayerRaster(config.resolution_x, config.resolution_y)
        for x in range(10, 15):
            current.add_disk(x, 10, 0, 255)

        def only_far_edge_routes(*args, **_kwargs):
            x, y, top_layer = args[3], args[4], args[5]
            if x != 14:
                return None
            return SupportAnchor(
                x,
                y,
                top_layer,
                base_x=x,
                base_y=y,
                base_layer=0,
                joint_x=x,
                joint_y=y,
                joint_layer=max(0, top_layer - 1),
                kind="bed",
                role="manual",
            )

        with patch("resin_slicer.supports._find_support_route", side_effect=only_far_edge_routes):
            result = _find_nearby_manual_contact_route(
                current,
                previous,
                [],
                config,
                support,
                10,
                10,
                20,
                _SurfaceNormalSampler(()),
                1,
                1,
                "manual",
                (),
            )

        self.assertIsNotNone(result)
        assert result is not None
        routes, actual = result
        self.assertEqual(routes[-1].x, 14)
        self.assertAlmostEqual(actual[0], (14.5) * config.pixel_size_x_mm)
        self.assertEqual(actual[2], (20.5) * config.layer_height_mm)

    def test_manual_only_slice_plans_only_manual_supports(self) -> None:
        import tempfile
        from pathlib import Path

        from resin_slicer.pipeline import SliceJob, slice_to_file

        config = profile("small-test").with_overrides(layer_height_mm=1.0)
        support = SupportConfig(enabled=True, support_spacing_mm=4.0, min_island_area_mm2=0.01)
        mesh = cube_mesh(8).transformed((20, 10, 0))
        click = (24.0, 14.0, support.model_lift_mm + 0.5)
        with tempfile.TemporaryDirectory() as tmp:
            manual_only = slice_to_file(
                SliceJob(
                    mesh=mesh,
                    print_config=config,
                    support_config=support,
                    preserve_coordinates=True,
                    manual_support_points=(click,),
                    manual_support_only=True,
                ),
                Path(tmp) / "manual.goo",
                "goo",
            )
            full = slice_to_file(
                SliceJob(
                    mesh=mesh,
                    print_config=config,
                    support_config=support,
                    preserve_coordinates=True,
                    manual_support_points=(click,),
                ),
                Path(tmp) / "full.goo",
                "goo",
            )
        # Manual-only slicing must not invent automatic supports.
        self.assertEqual(manual_only.support_count, 1)
        self.assertGreater(full.support_count, 1)

    def test_manual_only_braces_between_manual_points(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=1.0)
        prepared = prepare_mesh(cube_mesh(8).transformed((20, 10, 6)), config, preserve_coordinates=True)
        support = SupportConfig(
            enabled=True,
            support_spacing_mm=4.0,
            min_island_area_mm2=0.01,
            brace_enabled=True,
            brace_max_distance_mm=8.0,
        )
        bounds = prepared.mesh.bounds()
        z = bounds.min_z + 0.5
        clicks = ((22.0, 14.0, z), (26.0, 14.0, z))
        plan = plan_supports(
            prepared, config, support, prepared.layer_count, manual_points=clicks, manual_only=True
        )
        self.assertEqual(len(plan.anchors), 2)
        self.assertGreater(len(plan.braces), 0)

    def test_paint_exclude_zone_blocks_auto_supports(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=1.0)
        prepared = prepare_mesh(cube_mesh(8).transformed((20, 10, 6)), config, preserve_coordinates=True)
        support = SupportConfig(enabled=True, support_spacing_mm=4.0, min_island_area_mm2=0.01)

        base = plan_supports(prepared, config, support, prepared.layer_count)
        self.assertGreater(len(base.anchors), 0)

        # A brush sphere big enough to cover the whole part removes every
        # automatic anchor.
        everything = (PaintZone(24.0, 14.0, 6.0, 50.0, "exclude"),)
        blocked = plan_supports(prepared, config, support, prepared.layer_count, paint_zones=everything)
        self.assertEqual(len(blocked.anchors), 0)

        # A zone far away from the model changes nothing.
        far_away = (PaintZone(70.0, 40.0, 30.0, 3.0, "exclude"),)
        unaffected = plan_supports(prepared, config, support, prepared.layer_count, paint_zones=far_away)
        self.assertEqual(len(unaffected.anchors), len(base.anchors))

    def test_paint_exclude_zone_does_not_block_manual_points(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=1.0)
        prepared = prepare_mesh(cube_mesh(8).transformed((20, 10, 6)), config, preserve_coordinates=True)
        support = SupportConfig(enabled=True, support_spacing_mm=4.0, min_island_area_mm2=0.01)
        bounds = prepared.mesh.bounds()
        click = ((bounds.min_x + bounds.max_x) / 2, (bounds.min_y + bounds.max_y) / 2, bounds.min_z + 0.5)
        everything = (PaintZone(24.0, 14.0, 6.0, 50.0, "exclude"),)

        plan = plan_supports(
            prepared, config, support, prepared.layer_count, manual_points=(click,), paint_zones=everything
        )
        self.assertEqual(len(plan.anchors), 1)
        self.assertEqual(plan.anchors[0].role, "manual")

    def test_paint_require_zones_route_spaced_anchors(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=1.0)
        prepared = prepare_mesh(cube_mesh(8).transformed((20, 10, 6)), config, preserve_coordinates=True)
        support = SupportConfig(enabled=True, support_spacing_mm=4.0, min_island_area_mm2=0.01)
        # A dense stroke of brush samples along the bottom face.
        stroke = tuple(PaintZone(20.5 + i * 0.5, 14.0, 6.2, 2.0, "require") for i in range(16))

        plan = plan_supports(
            prepared, config, support, prepared.layer_count, manual_only=True, paint_zones=stroke
        )
        painted = [anchor for anchor in plan.anchors if anchor.role == "painted"]
        self.assertGreater(len(painted), 0)
        # The stroke is thinned to the configured support spacing, so far fewer
        # anchors than brush samples are placed.
        self.assertLess(len(painted), len(stroke))
        for i, first in enumerate(painted):
            for second in painted[i + 1 :]:
                dx_mm = (first.x - second.x) * config.pixel_size_x_mm
                dy_mm = (first.y - second.y) * config.pixel_size_y_mm
                distance = (dx_mm * dx_mm + dy_mm * dy_mm) ** 0.5
                self.assertGreaterEqual(distance, support.support_spacing_mm - config.pixel_size_x_mm)

    def test_brace_interval_repeats_braces_up_tall_supports(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=1.0)
        prepared = prepare_mesh(cube_mesh(8), config, z_offset_mm=20.0)
        common = dict(
            enabled=True,
            model_lift_mm=20.0,
            support_spacing_mm=2.0,
            min_island_area_mm2=0.01,
            overhang_angle_deg=70,
            brace_enabled=True,
            brace_max_distance_mm=12.0,
        )
        single = plan_supports(prepared, config, SupportConfig(**common), prepared.layer_count)
        repeated = plan_supports(
            prepared, config, SupportConfig(brace_interval_mm=4.0, **common), prepared.layer_count
        )

        self.assertGreater(len(single.braces), 0)
        self.assertEqual(len({brace.start_layer for brace in single.braces}), 1)
        levels = sorted({brace.start_layer for brace in repeated.braces})
        self.assertGreater(len(levels), 1)
        for earlier, later in zip(levels, levels[1:]):
            self.assertEqual(later - earlier, 4)
        self.assertGreater(len(repeated.braces), len(single.braces))
        # Every rung still stays below the joint of the supports it ties into.
        max_joint = max(
            anchor.joint_layer if anchor.joint_layer is not None else anchor.top_layer - 1
            for anchor in repeated.anchors
        )
        for brace in repeated.braces:
            self.assertLessEqual(brace.end_layer, max_joint)

    def test_braces_flatten_to_fit_short_supports(self) -> None:
        # At fine layer heights a full 45-degree diagonal cannot fit below the
        # joints of lift-height supports; the rung should slide down to the bed
        # interface and flatten instead of disappearing.
        config = profile("small-test").with_overrides(layer_height_mm=0.1)
        prepared = prepare_mesh(cube_mesh(10), config, z_offset_mm=4.0)
        support = SupportConfig(
            enabled=True,
            model_lift_mm=4.0,
            support_spacing_mm=4.0,
            min_island_area_mm2=0.01,
            overhang_angle_deg=70,
            brace_enabled=True,
            brace_height_mm=3.0,
            brace_max_distance_mm=8.0,
        )
        plan = plan_supports(prepared, config, support, prepared.layer_count)
        self.assertGreater(len(plan.anchors), 1)
        self.assertGreater(len(plan.braces), 0)
        max_joint = max(
            anchor.joint_layer if anchor.joint_layer is not None else anchor.top_layer - 1
            for anchor in plan.anchors
        )
        for brace in plan.braces:
            self.assertGreater(brace.end_layer, brace.start_layer)
            self.assertLessEqual(brace.end_layer, max_joint)

    def test_higher_overhang_angle_generates_more_supports(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=1.0)
        prepared = prepare_mesh(_ledge_mesh(), config, z_offset_mm=4.0)
        loose = plan_supports(
            prepared,
            config,
            SupportConfig(
                enabled=True,
                model_lift_mm=4.0,
                support_spacing_mm=4.0,
                min_island_area_mm2=0.01,
                overhang_angle_deg=20,
            ),
            prepared.layer_count,
        )
        strict = plan_supports(
            prepared,
            config,
            SupportConfig(
                enabled=True,
                model_lift_mm=4.0,
                support_spacing_mm=4.0,
                min_island_area_mm2=0.01,
                overhang_angle_deg=70,
            ),
            prepared.layer_count,
        )
        self.assertGreater(len(strict.anchors), len(loose.anchors))

    def test_preferred_45_degree_orientation_generates_supports(self) -> None:
        from resin_slicer.transform import MeshTransform, apply_transform

        config = profile("small-test").with_overrides(layer_height_mm=0.5)
        mesh = apply_transform(cube_mesh(20), MeshTransform(rotate_x_deg=45, rotate_y_deg=45))
        prepared = prepare_mesh(mesh, config, z_offset_mm=5.0)
        for angle in (45, 65, 89):
            support = SupportConfig(
                enabled=True,
                model_lift_mm=5.0,
                support_spacing_mm=3.0,
                min_island_area_mm2=0.08,
                overhang_angle_deg=angle,
            )
            plan = plan_supports(prepared, config, support, prepared.layer_count)
            self.assertGreater(len(plan.anchors), 0)

    def test_support_columns_do_not_pass_through_model_below(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=1.0)
        prepared = prepare_mesh(_stacked_gap_mesh(), config, z_offset_mm=4.0)
        support = SupportConfig(
            enabled=True,
            model_lift_mm=4.0,
            support_spacing_mm=2.0,
            min_island_area_mm2=0.01,
            overhang_angle_deg=70,
            branching_enabled=False,
        )
        plan = plan_supports(prepared, config, support, prepared.layer_count)
        self.assertGreater(len(plan.anchors), 0)

        for anchor in plan.anchors:
            base_x = anchor.base_x if anchor.base_x is not None else anchor.x
            base_y = anchor.base_y if anchor.base_y is not None else anchor.y
            joint_layer = anchor.joint_layer if anchor.joint_layer is not None else max(0, anchor.top_layer - 1)
            for layer_index in range(joint_layer + 1):
                layer = render_model_layer(prepared.mesh, config, layer_index)
                x, y = _anchor_center(
                    base_x,
                    base_y,
                    anchor.joint_x if anchor.joint_x is not None else anchor.x,
                    anchor.joint_y if anchor.joint_y is not None else anchor.y,
                    layer_index,
                    joint_layer,
                )
                self.assertFalse(_disk_has_model(layer, x, y, plan.post_radius_px))

    def test_support_overhaul_options_create_braces(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=0.5)
        prepared = prepare_mesh(_ledge_mesh(), config, z_offset_mm=5.0)
        support = SupportConfig(
            enabled=True,
            model_lift_mm=5.0,
            support_spacing_mm=2.0,
            min_island_area_mm2=0.01,
            overhang_angle_deg=70,
            tip_type="sphere",
            tip_length_mm=0.6,
            bed_interface="skate",
            brace_enabled=True,
            brace_max_distance_mm=12.0,
        )
        plan = plan_supports(prepared, config, support, prepared.layer_count)
        self.assertGreater(len(plan.anchors), 1)
        self.assertGreater(len(plan.braces), 0)
        self.assertEqual(plan.bed_interface, "skate")
        self.assertTrue(all(anchor.tip_type == "sphere" for anchor in plan.anchors))
        for brace in plan.braces:
            horizontal_mm = (
                ((brace.x1 - brace.x0) * config.pixel_size_x_mm) ** 2
                + ((brace.y1 - brace.y0) * config.pixel_size_y_mm) ** 2
            ) ** 0.5
            vertical_mm = (brace.end_layer - brace.start_layer) * config.layer_height_mm
            self.assertGreater(brace.end_layer, brace.start_layer)
            # Braces are 45-degree diagonals, flattened (down to a third of the
            # rise) only when short supports cannot fit the full diagonal.
            self.assertLessEqual(vertical_mm, horizontal_mm + config.layer_height_mm)
            self.assertGreaterEqual(vertical_mm, horizontal_mm / 3 - config.layer_height_mm)
            for layer_index in range(brace.start_layer, brace.end_layer + 1):
                layer = render_model_layer(prepared.mesh, config, layer_index)
                x, y = _brace_center_for_test(brace, layer_index)
                self.assertFalse(_disk_has_model(layer, x, y, plan.post_radius_px))

    def test_support_posts_are_vertical(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=0.5)
        prepared = prepare_mesh(_ledge_mesh(), config, z_offset_mm=5.0)
        support = SupportConfig(
            enabled=True,
            model_lift_mm=5.0,
            support_spacing_mm=2.0,
            min_island_area_mm2=0.01,
            overhang_angle_deg=70,
            branching_enabled=False,
        )
        plan = plan_supports(prepared, config, support, prepared.layer_count)
        self.assertGreater(len(plan.anchors), 0)
        for anchor in plan.anchors:
            base_x = anchor.base_x if anchor.base_x is not None else anchor.x
            base_y = anchor.base_y if anchor.base_y is not None else anchor.y
            joint_x = anchor.joint_x if anchor.joint_x is not None else anchor.x
            joint_y = anchor.joint_y if anchor.joint_y is not None else anchor.y
            self.assertEqual((base_x, base_y), (joint_x, joint_y))

    def test_surface_normal_sampler_ignores_misaligned_surfaces(self) -> None:
        downward = ((0.0, 0.0, 0.0), (0.0, 2.0, 0.0), (2.0, 0.0, 0.0))
        nearby_downward = ((0.2, 0.0, 0.05), (0.2, 2.0, 0.05), (2.2, 0.0, 0.05))
        vertical = ((0.0, 0.0, 0.0), (0.0, 0.0, 2.0), (0.0, 2.0, 0.0))
        sampler = _SurfaceNormalSampler((downward, nearby_downward, vertical))
        normal = sampler.normal_at_contact((0.5, 0.5, 0.0), (0.0, 0.0, -1.0))
        self.assertAlmostEqual(normal[0], 0.0, places=5)
        self.assertAlmostEqual(normal[1], 0.0, places=5)
        self.assertAlmostEqual(normal[2], -1.0, places=5)

    def test_bed_interface_thickness_controls_base_layers(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=0.5)
        prepared = prepare_mesh(_ledge_mesh(), config, z_offset_mm=5.0)
        support = SupportConfig(
            enabled=True,
            model_lift_mm=5.0,
            support_spacing_mm=2.0,
            min_island_area_mm2=0.01,
            overhang_angle_deg=70,
            bed_interface="raft",
            bed_interface_thickness_mm=1.25,
        )
        plan = plan_supports(prepared, config, support, prepared.layer_count)
        self.assertEqual(plan.raft_layers, 3)

    def test_spacing_changes_support_density(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=0.5)
        prepared = prepare_mesh(_ledge_mesh(), config, z_offset_mm=5.0)
        dense = plan_supports(
            prepared,
            config,
            SupportConfig(
                enabled=True,
                model_lift_mm=5.0,
                support_spacing_mm=1.5,
                min_island_area_mm2=0.01,
                overhang_angle_deg=70,
            ),
            prepared.layer_count,
        )
        sparse = plan_supports(
            prepared,
            config,
            SupportConfig(
                enabled=True,
                model_lift_mm=5.0,
                support_spacing_mm=6.0,
                min_island_area_mm2=0.01,
                overhang_angle_deg=70,
            ),
            prepared.layer_count,
        )
        self.assertGreater(len(dense.anchors), len(sparse.anchors))

    def test_primary_supports_add_dense_island_attachments(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=0.5)
        prepared = prepare_mesh(_ledge_mesh(), config, z_offset_mm=5.0)
        common = dict(
            enabled=True,
            model_lift_mm=5.0,
            support_spacing_mm=4.0,
            min_island_area_mm2=0.01,
            overhang_angle_deg=70,
        )
        standard = plan_supports(prepared, config, SupportConfig(**common), prepared.layer_count)
        primary = plan_supports(
            prepared,
            config,
            SupportConfig(
                primary_supports_enabled=True,
                primary_density_multiplier=2.0,
                primary_area_radius_mm=4.0,
                primary_max_extra_per_island=8,
                **common,
            ),
            prepared.layer_count,
        )
        self.assertGreater(len(primary.anchors), len(standard.anchors))
        self.assertGreater(len([anchor for anchor in primary.anchors if anchor.role == "primary"]), 0)

    def test_sphere_tip_has_bulb_radius(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=0.5)
        prepared = prepare_mesh(_ledge_mesh(), config, z_offset_mm=5.0)
        support = SupportConfig(
            enabled=True,
            model_lift_mm=5.0,
            support_spacing_mm=2.0,
            min_island_area_mm2=0.01,
            overhang_angle_deg=70,
            tip_type="sphere",
        )
        plan = plan_supports(prepared, config, support, prepared.layer_count)
        anchor = next(anchor for anchor in plan.anchors if anchor.top_layer > (anchor.joint_layer or 0))
        joint_layer = anchor.joint_layer or 0
        radii = [_support_radius_at_layer(anchor, layer, plan) for layer in range(joint_layer + 1, anchor.top_layer + 1)]
        self.assertGreater(max(radii), max(plan.post_radius_px, plan.tip_radius_px))

    def test_bed_supports_respect_max_shaft_angle(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=1.0)
        prepared = prepare_mesh(_offset_stack_mesh(), config, z_offset_mm=4.0)
        support = SupportConfig(
            enabled=True,
            model_lift_mm=4.0,
            support_spacing_mm=2.0,
            min_island_area_mm2=0.01,
            overhang_angle_deg=70,
            max_support_angle_deg=10,
            branching_enabled=False,
            enforcers_enabled=False,
        )
        plan = plan_supports(prepared, config, support, prepared.layer_count)
        self.assertGreater(len(plan.anchors), 0)
        self.assertTrue(all(_support_angle_deg(anchor, config) <= 10.01 for anchor in plan.anchors))
        self.assertTrue(all(anchor.kind == "bed" for anchor in plan.anchors))

    def test_vertical_contact_normal_keeps_bed_routes_vertical(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=1.0)
        prepared = prepare_mesh(_offset_stack_mesh(), config, z_offset_mm=4.0)
        common = dict(
            enabled=True,
            model_lift_mm=4.0,
            support_spacing_mm=2.0,
            min_island_area_mm2=0.01,
            overhang_angle_deg=70,
            max_support_angle_deg=0,
            enforcer_reach_mm=12.0,
            enforcer_min_drop_mm=1.0,
        )
        without_enforcers = plan_supports(
            prepared,
            config,
            SupportConfig(enforcers_enabled=False, **common),
            prepared.layer_count,
        )
        with_enforcers = plan_supports(
            prepared,
            config,
            SupportConfig(enforcers_enabled=True, **common),
            prepared.layer_count,
        )
        self.assertGreater(len(without_enforcers.anchors), 0)
        # Upper surfaces sharing XY with lower contacts now get their own
        # enforcers. The bed routes must still remain unchanged and vertical.
        bed_routes = tuple(a for a in with_enforcers.anchors if a.kind == "bed")
        self.assertEqual(len(bed_routes), len(without_enforcers.anchors))
        self.assertTrue(any(a.kind == "enforcer" for a in with_enforcers.anchors))
        self.assertTrue(all(_support_angle_deg(anchor, config) <= 0.01 for anchor in with_enforcers.anchors))

    def test_enforcer_searches_a_nearby_lower_surface(self) -> None:
        config = profile("small-test").with_overrides(
            layer_height_mm=1.0,
            center_model=False,
        )
        triangles = []
        triangles.extend(_translated_cube(8, (0, 0, 0)))
        triangles.extend(_translated_cube(4, (10, 2, 12)))
        prepared = prepare_mesh(
            Mesh(tuple(triangles)),
            config,
            preserve_coordinates=True,
        )
        occupied = []
        for layer_index in range(prepared.layer_count):
            layer = render_model_layer(prepared.mesh, config, layer_index)
            record = _occupied_layer(layer_index, layer)
            if record is not None:
                occupied.append(record)
        support = SupportConfig(
            enforcers_enabled=True,
            enforcer_reach_mm=5.0,
            enforcer_min_drop_mm=1.0,
            max_base_reach_mm=20.0,
            max_support_angle_deg=45.0,
        )
        route = _find_enforcer_route(
            occupied,
            config,
            support,
            top_x=21,
            top_y=8,
            top_layer=12,
            normal=(0.0, 0.0, -1.0),
            collision_radius=1,
            role="manual",
        )
        self.assertIsNotNone(route)
        assert route is not None
        self.assertEqual(route.kind, "enforcer")
        self.assertGreater(route.base_tip_layers, 0)
        self.assertLess(route.base_x, route.x)
        self.assertLessEqual(_support_angle_deg(route, config), 45.01)

    def test_manual_branch_can_join_an_existing_support_shaft(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=0.5)
        support = SupportConfig(
            enforcer_reach_mm=10.0,
            enforcer_min_drop_mm=1.0,
            max_base_reach_mm=30.0,
            max_support_angle_deg=45.0,
        )
        trunk = SupportAnchor(
            x=60,
            y=45,
            top_layer=70,
            base_x=60,
            base_y=45,
            base_layer=0,
            joint_x=60,
            joint_y=45,
            joint_layer=68,
            kind="bed",
        )
        route = _find_branch_route(
            [],
            config,
            support,
            top_x=70,
            top_y=45,
            top_layer=60,
            normal=(0.0, 0.0, -1.0),
            collision_radius=1,
            role="manual",
            branch_anchors=(trunk,),
        )
        self.assertIsNotNone(route)
        assert route is not None
        self.assertEqual(route.kind, "branch")
        self.assertEqual(route.base_x, 60)
        self.assertEqual(route.base_y, 45)
        self.assertLessEqual(_support_angle_deg(route, config), 45.01)

    def test_automatic_contact_can_join_an_existing_support_shaft(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=0.5)
        support = SupportConfig(
            branching_enabled=True,
            enforcers_enabled=False,
            enforcer_min_drop_mm=1.0,
            max_base_reach_mm=30.0,
            max_support_angle_deg=45.0,
        )
        trunk = SupportAnchor(
            x=60,
            y=45,
            top_layer=70,
            base_x=60,
            base_y=45,
            base_layer=0,
            joint_x=60,
            joint_y=45,
            joint_layer=68,
            kind="bed",
        )

        with patch(
            "resin_slicer.supports._route_collides",
            # The normal-width bed route is blocked, while the existing parent
            # has independently been proven clear at the wider trunk radius.
            side_effect=lambda _layers, _width, _height, anchor, radius: (
                anchor.kind == "bed" and radius <= 1
            ),
        ):
            route = _find_support_route(
                [],
                config,
                support,
                top_x=70,
                top_y=45,
                top_layer=60,
                normal=(0.0, 0.0, -1.0),
                body_collision_radius=1,
                collision_radius=1,
                role="secondary",
                branch_anchors=(trunk,),
            )

        self.assertIsNotNone(route)
        assert route is not None
        self.assertEqual(route.kind, "branch")

    def test_clear_automatic_contact_prefers_a_nearby_tree_branch(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=0.5)
        support = SupportConfig(
            branching_enabled=True,
            tree_merge_distance_mm=4.0,
            tree_max_tips=12,
            max_support_angle_deg=45.0,
        )
        trunk = SupportAnchor(
            x=60,
            y=45,
            top_layer=70,
            base_x=60,
            base_y=45,
            base_layer=0,
            joint_x=60,
            joint_y=45,
            joint_layer=68,
            kind="bed",
            anchor_id=0,
            tree_id=0,
            tree_clearance_ok=True,
        )

        route = _find_support_route(
            [],
            config,
            support,
            top_x=68,
            top_y=45,
            top_layer=60,
            normal=(0.0, 0.0, -1.0),
            body_collision_radius=1,
            collision_radius=1,
            role="secondary",
            branch_anchors=(trunk,),
        )

        self.assertIsNotNone(route)
        assert route is not None
        self.assertEqual(route.kind, "branch")
        self.assertEqual(route.parent_id, 0)
        self.assertEqual(route.tree_id, 0)

    def test_full_tree_starts_a_new_bed_root(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=0.5)
        support = SupportConfig(
            branching_enabled=True,
            tree_merge_distance_mm=4.0,
            tree_max_tips=1,
            max_support_angle_deg=45.0,
        )
        full_trunk = SupportAnchor(
            x=60,
            y=45,
            top_layer=70,
            base_x=60,
            base_y=45,
            joint_x=60,
            joint_y=45,
            joint_layer=68,
            kind="bed",
            anchor_id=0,
            tree_id=0,
        )

        route = _find_support_route(
            [],
            config,
            support,
            top_x=70,
            top_y=45,
            top_layer=60,
            normal=(0.0, 0.0, -1.0),
            body_collision_radius=1,
            collision_radius=1,
            role="secondary",
            branch_anchors=(full_trunk,),
        )

        self.assertIsNotNone(route)
        assert route is not None
        self.assertEqual(route.kind, "bed")

    def test_shared_tree_trunk_thickens_and_has_tapered_pedestal(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=0.5)
        support = SupportConfig(
            post_radius_mm=0.2,
            tree_trunk_radius_mm=0.6,
            foot_radius_mm=1.2,
            pedestal_height_mm=2.0,
        )
        anchors = _assign_tree_loads(
            (
                SupportAnchor(
                    40, 40, 30, joint_x=40, joint_y=40, joint_layer=28,
                    anchor_id=0, tree_id=0, kind="bed",
                ),
                SupportAnchor(
                    48, 40, 30, base_x=40, base_y=40, base_layer=20,
                    joint_x=48, joint_y=40, joint_layer=28, anchor_id=1,
                    parent_id=0, tree_id=0, kind="branch",
                ),
                SupportAnchor(
                    40, 48, 30, base_x=40, base_y=40, base_layer=20,
                    joint_x=40, joint_y=48, joint_layer=28, anchor_id=2,
                    parent_id=0, tree_id=0, kind="branch",
                ),
            )
        )
        plan = build_support_plan(anchors, (), config, support)

        self.assertEqual(anchors[0].load_count, 3)
        self.assertEqual(anchors[1].load_count, 1)
        root_shaft = _support_radius_at_layer(anchors[0], 6, plan)
        branch_shaft = _support_radius_at_layer(anchors[1], 22, plan)
        self.assertGreater(root_shaft, branch_shaft)
        self.assertGreater(_support_radius_at_layer(anchors[0], 0, plan), root_shaft)

    def test_spherical_contact_shape_adds_contact_bulb(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=0.5)
        prepared = prepare_mesh(_ledge_mesh(), config, z_offset_mm=5.0)
        base_support = SupportConfig(
            enabled=True,
            model_lift_mm=5.0,
            support_spacing_mm=2.0,
            min_island_area_mm2=0.01,
            overhang_angle_deg=70,
            spherical_contact_enabled=False,
        )
        sphere_support = SupportConfig(
            enabled=True,
            model_lift_mm=5.0,
            support_spacing_mm=2.0,
            min_island_area_mm2=0.01,
            overhang_angle_deg=70,
            spherical_contact_enabled=True,
            spherical_contact_diameter_mm=2.0,
            spherical_contact_inset_mm=0.1,
        )
        base_plan = plan_supports(prepared, config, base_support, prepared.layer_count)
        sphere_plan = plan_supports(prepared, config, sphere_support, prepared.layer_count)
        base_pixels = 0
        sphere_pixels = 0
        for layer_index in range(prepared.layer_count):
            base_layer = render_model_layer(prepared.mesh, config, layer_index)
            sphere_layer = render_model_layer(prepared.mesh, config, layer_index)
            apply_supports(base_layer, layer_index, base_plan)
            apply_supports(sphere_layer, layer_index, sphere_plan)
            base_pixels += base_layer.count_on()
            sphere_pixels += sphere_layer.count_on()
        self.assertGreater(sphere_pixels, base_pixels)

    def test_spherical_contact_inset_percent_maps_to_diameter_fraction(self) -> None:
        half_inset = _support_from_request(
            {"support": {"sphericalContactDiameter": 2.0, "sphericalContactInsetPercent": 50}}
        )
        deep_inset = _support_from_request(
            {"support": {"sphericalContactDiameter": 2.0, "sphericalContactInsetPercent": 95}}
        )
        self.assertAlmostEqual(half_inset.spherical_contact_inset_mm, 1.0)
        self.assertAlmostEqual(deep_inset.spherical_contact_inset_mm, 1.9)

    def test_projected_raft_offset_expands_model_shadow(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=0.5)
        prepared = prepare_mesh(_ledge_mesh(), config, z_offset_mm=5.0)
        common = dict(
            enabled=True,
            model_lift_mm=5.0,
            support_spacing_mm=2.0,
            min_island_area_mm2=0.01,
            overhang_angle_deg=70,
            bed_interface="raft",
            bed_interface_thickness_mm=1.0,
        )
        base_plan = plan_supports(
            prepared,
            config,
            SupportConfig(raft_margin_mm=0.0, **common),
            prepared.layer_count,
        )
        offset_plan = plan_supports(
            prepared,
            config,
            SupportConfig(raft_margin_mm=1.5, **common),
            prepared.layer_count,
        )
        base_layer = LayerRaster(config.resolution_x, config.resolution_y)
        offset_layer = LayerRaster(config.resolution_x, config.resolution_y)
        apply_supports(base_layer, 0, base_plan)
        apply_supports(offset_layer, 0, offset_plan)

        self.assertGreater(base_layer.count_on(), 0)
        self.assertGreater(offset_layer.count_on(), base_layer.count_on())

    def test_raft_chamfer_expands_from_bottom_layer(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=0.5)
        prepared = prepare_mesh(_ledge_mesh(), config, z_offset_mm=5.0)
        support = SupportConfig(
            enabled=True,
            model_lift_mm=5.0,
            support_spacing_mm=2.0,
            min_island_area_mm2=0.01,
            overhang_angle_deg=70,
            bed_interface="raft",
            raft_margin_mm=2.5,
            raft_chamfer_width_mm=1.5,
            raft_chamfer_angle_deg=45,
            bed_interface_thickness_mm=3.0,
        )
        plan = plan_supports(prepared, config, support, prepared.layer_count)
        bottom = LayerRaster(config.resolution_x, config.resolution_y)
        upper = LayerRaster(config.resolution_x, config.resolution_y)
        apply_supports(bottom, 0, plan)
        apply_supports(upper, min(plan.raft_layers - 1, 5), plan)

        self.assertGreater(bottom.count_on(), 0)
        self.assertGreater(upper.count_on(), bottom.count_on())


def _translated_cube(size: float, offset: tuple[float, float, float]) -> list:
    ox, oy, oz = offset
    return [
        tuple((x + ox, y + oy, z + oz) for x, y, z in tri)
        for tri in cube_mesh(size).triangles
    ]


def _ledge_mesh() -> Mesh:
    triangles = []
    triangles.extend(_translated_cube(6, (0, 0, 0)))
    triangles.extend(_translated_cube(10, (4, 0, 6)))
    return Mesh(tuple(triangles))


def _stacked_gap_mesh() -> Mesh:
    triangles = []
    triangles.extend(_translated_cube(12, (0, 0, 0)))
    triangles.extend(_translated_cube(8, (2, 2, 14)))
    return Mesh(tuple(triangles))


def _offset_stack_mesh() -> Mesh:
    triangles = []
    triangles.extend(_translated_cube(12, (0, 0, 0)))
    triangles.extend(_translated_cube(8, (6, 2, 14)))
    return Mesh(tuple(triangles))


def _disk_has_model(layer, x: int, y: int, radius: int) -> bool:
    radius = max(0, radius)
    radius2 = radius * radius
    for yy in range(max(0, y - radius), min(layer.height, y + radius + 1)):
        for xx in range(max(0, x - radius), min(layer.width, x + radius + 1)):
            if (xx - x) * (xx - x) + (yy - y) * (yy - y) <= radius2:
                if layer.pixels[yy * layer.width + xx]:
                    return True
    return False


def _anchor_center(base_x: int, base_y: int, top_x: int, top_y: int, layer_index: int, top_layer: int) -> tuple[int, int]:
    if top_layer <= 0:
        return top_x, top_y
    t = layer_index / top_layer
    return (
        round(base_x + (top_x - base_x) * t),
        round(base_y + (top_y - base_y) * t),
    )


def _brace_center_for_test(brace, layer_index: int) -> tuple[int, int]:
    if brace.end_layer <= brace.start_layer:
        return brace.x0, brace.y0
    t = (layer_index - brace.start_layer) / (brace.end_layer - brace.start_layer)
    return (
        round(brace.x0 + (brace.x1 - brace.x0) * t),
        round(brace.y0 + (brace.y1 - brace.y0) * t),
    )


if __name__ == "__main__":
    unittest.main()
