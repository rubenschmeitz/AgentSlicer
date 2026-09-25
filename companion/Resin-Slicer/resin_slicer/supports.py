from __future__ import annotations

import os
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, replace
from math import atan, ceil, cos, floor, pi, radians, sin, sqrt, tan
from typing import Callable

from .config import PrintConfig, SupportConfig
from .mesh import Point3, Triangle
from .raster import Component, LayerRaster, dilate_mask, mm_to_px, unsupported_components
from .slicing import PreparedMesh, render_prepared_layer

Progress = Callable[[str], None]

_BINARY_TO_RASTER = bytes(0 if value == 0 else 255 for value in range(256))


@dataclass(frozen=True)
class _OccupiedLayer:
    layer_index: int
    width: int
    height: int
    rows: dict[int, tuple[tuple[int, int], ...]]
    min_x: int
    min_y: int
    max_x: int
    max_y: int


@dataclass(frozen=True)
class SupportAnchor:
    x: int
    y: int
    top_layer: int
    base_x: int | None = None
    base_y: int | None = None
    base_layer: int = 0
    joint_x: int | None = None
    joint_y: int | None = None
    joint_layer: int | None = None
    base_tip_layers: int = 0
    tip_type: str = "cone"
    kind: str = "bed"
    role: str = "secondary"
    # Logical tree metadata. IDs refer to positions in the deterministic anchor
    # sequence. They let the renderer thicken load-bearing ancestors without
    # retaining a separate, memory-heavy graph per layer.
    anchor_id: int = -1
    parent_id: int = -1
    tree_id: int = -1
    load_count: int = 1
    tree_clearance_ok: bool = False
    normal_x: float = 0.0
    normal_y: float = 0.0
    normal_z: float = -1.0


@dataclass(frozen=True)
class PaintZone:
    """A painted brush sample on the model surface, in the same mm space as
    manual support points. ``exclude`` zones block automatic anchors whose
    contact point falls inside the brush sphere; ``require`` zones ask the
    planner to grow supports on the painted surface."""

    x: float
    y: float
    z: float
    radius: float
    mode: str = "exclude"  # "exclude" or "require"


@dataclass(frozen=True)
class _AnchorCandidate:
    x: int
    y: int
    spacing_mm: float
    role: str


@dataclass(frozen=True)
class _Surface:
    triangle: Triangle
    normal: Point3
    min_x: float
    min_y: float
    min_z: float
    max_x: float
    max_y: float
    max_z: float


@dataclass(frozen=True)
class SupportBrace:
    x0: int
    y0: int
    x1: int
    y1: int
    start_layer: int
    end_layer: int
    radius_px: int


@dataclass(frozen=True)
class SupportPlan:
    anchors: tuple[SupportAnchor, ...]
    post_radius_px: int
    tip_radius_px: int
    foot_radius_px: int
    joint_sphere_radius_mm: float
    raft_layers: int
    contact_depth_layers: int = 0
    braces: tuple[SupportBrace, ...] = ()
    bed_interface: str = "raft"
    raft_radius_px: int = 0
    brace_layer_radius: int = 0
    spherical_contact_enabled: bool = False
    contact_sphere_radius_mm: float = 0.0
    contact_sphere_inset_mm: float = 0.0
    pixel_size_x_mm: float = 1.0
    pixel_size_y_mm: float = 1.0
    layer_height_mm: float = 1.0
    tree_trunk_radius_px: int = 0
    pedestal_height_layers: int = 0
    raft_mask: bytes | None = None
    raft_shadow_mask: bytes | None = None
    raft_offset_px: int = 0
    raft_chamfer_width_mm: float = 0.0
    raft_chamfer_angle_deg: float = 45.0
    manual_requested: int = 0
    manual_routed: int = 0
    manual_routed_points: tuple[tuple[float, float, float], ...] = ()
    manual_actual_contact_points: tuple[tuple[float, float, float], ...] = ()
    manual_unrouted_points: tuple[tuple[float, float, float], ...] = ()
    auto_components_detected: int = 0
    auto_components_supported: int = 0
    auto_components_excluded: int = 0
    auto_components_unrouted: int = 0
    auto_candidates_attempted: int = 0
    auto_candidates_routed: int = 0
    auto_candidates_unrouted: int = 0
    auto_unrouted_area_mm2: float = 0.0
    tree_root_count: int = 0
    tree_branch_count: int = 0
    max_tree_load: int = 0


def plan_supports(
    prepared: PreparedMesh,
    config: PrintConfig,
    support: SupportConfig,
    layer_count: int,
    progress: Progress | None = None,
    include_raft_mask: bool = True,
    on_anchor: Callable[[SupportAnchor], None] | None = None,
    manual_points: tuple[tuple[float, float, float], ...] = (),
    manual_only: bool = False,
    paint_zones: tuple[PaintZone, ...] = (),
) -> SupportPlan:
    support.validate()
    exclude_zones = tuple(zone for zone in paint_zones if zone.mode == "exclude")
    require_zones = tuple(zone for zone in paint_zones if zone.mode == "require")
    output_pixel_mm = min(config.pixel_size_x_mm, config.pixel_size_y_mm)
    brace_radius = mm_to_px(support.brace_radius_mm, output_pixel_mm)
    bed_interface_layers = 0 if support.bed_interface == "none" else max(
        1,
        int(ceil(support.bed_interface_thickness_mm / config.layer_height_mm)),
    )
    raft_mask, raft_shadow_mask, raft_offset_px = (
        _projected_raft_masks(prepared, config, support) if include_raft_mask else (None, None, 0)
    )

    analysis_config = _analysis_config(config, support)
    analysis_pixel_mm = min(analysis_config.pixel_size_x_mm, analysis_config.pixel_size_y_mm)
    lookback_layers = _overhang_lookback_layers(analysis_config, support, analysis_pixel_mm)
    min_area_px = max(1, int(ceil(support.min_island_area_mm2 / analysis_config.pixel_area_mm2)))
    body_collision_radius = mm_to_px(
        max(support.post_radius_mm, support.tip_radius_mm), analysis_pixel_mm
    )
    collision_radius = mm_to_px(
        max(support.post_radius_mm, support.tip_radius_mm)
        + support.collision_clearance_mm,
        analysis_pixel_mm,
    )
    history: deque[LayerRaster] = deque(maxlen=lookback_layers)
    occupied_layers: list[_OccupiedLayer] = []
    anchors: list[SupportAnchor] = []
    branch_anchors: list[SupportAnchor] = []
    placed_points = _PlacedPointIndex(config)
    surface_normals = _SurfaceNormalSampler(prepared.mesh.triangles)
    if progress and surface_normals.sample_stride > 1:
        progress(
            "sampling surface normals every "
            f"{surface_normals.sample_stride} triangles to bound memory"
        )

    interval = max(1, layer_count // 20)
    auto_components_detected = 0
    auto_components_supported = 0
    auto_components_excluded = 0
    auto_components_unrouted = 0
    auto_candidates_attempted = 0
    auto_candidates_routed = 0
    auto_unrouted_area_mm2 = 0.0
    for layer_index in range(layer_count):
        if progress and (layer_index == 0 or (layer_index + 1) % interval == 0):
            progress(f"support analysis layer {layer_index + 1}/{layer_count}")

        current = render_prepared_layer(prepared, analysis_config, layer_index)
        occupied = _occupied_layer(layer_index, current)
        if occupied is None:
            # Empty layer (e.g. the model-lift gap or an internal void): there is
            # nothing here to support or to collide against, so skip the costly
            # dilation / unsupported-mask / connected-component analysis entirely.
            if not manual_only:
                history.append(current)
            continue

        if manual_only:
            # Manual-only pass: we still need the per-layer occupied geometry so
            # user points can route down to the model/bed, but the automatic
            # overhang detection and candidate placement are skipped entirely.
            occupied_layers.append(occupied)
            continue

        base_layer = history[0] if history else LayerRaster(analysis_config.resolution_x, analysis_config.resolution_y)
        overhang_px = _overhang_allowance_px(
            analysis_config,
            support,
            analysis_pixel_mm,
            max(1, len(history)),
        )
        components = unsupported_components(current, base_layer, overhang_px, min_area_px)

        for component in components:
            auto_components_detected += 1
            candidates = _anchor_candidates(component, analysis_config, support)
            component_satisfied = False
            component_excluded = 0
            for candidate in candidates:
                x, y = candidate.x, candidate.y
                out_x, out_y = _scale_point_to_output(x, y, analysis_config, config)
                if placed_points.too_close(out_x, out_y, candidate.spacing_mm, layer_index):
                    # Thin contacts in physical 3D space. A contact far below
                    # this surface must not suppress a new overhang above it.
                    component_satisfied = True
                    continue
                contact_point = (
                    (x + 0.5) * analysis_config.pixel_size_x_mm,
                    (y + 0.5) * analysis_config.pixel_size_y_mm,
                    (layer_index + 0.5) * analysis_config.layer_height_mm,
                )
                if exclude_zones and _in_paint_zones(contact_point, exclude_zones):
                    component_excluded += 1
                    continue
                fallback_normal = _estimate_surface_normal(current, base_layer, analysis_config, x, y)
                normal = surface_normals.normal_at_contact(contact_point, fallback_normal)

                auto_candidates_attempted += 1
                route = _find_support_route(
                    occupied_layers,
                    analysis_config,
                    support,
                    x,
                    y,
                    layer_index,
                    normal,
                    body_collision_radius,
                    collision_radius,
                    candidate.role,
                    branch_anchors=tuple(branch_anchors),
                )
                if route is None:
                    continue

                route = _register_tree_routes((route,), branch_anchors)[0]
                out_anchor = _scale_anchor_to_output(route, analysis_config, config)
                placed_points.add(out_anchor.x, out_anchor.y, out_anchor.top_layer)
                anchors.append(out_anchor)
                branch_anchors.append(route)
                component_satisfied = True
                auto_candidates_routed += 1
                if on_anchor is not None:
                    on_anchor(out_anchor)

            if component_satisfied:
                auto_components_supported += 1
            elif component_excluded == len(candidates):
                auto_components_excluded += 1
            else:
                auto_components_unrouted += 1
                auto_unrouted_area_mm2 += (
                    component.area_px * analysis_config.pixel_area_mm2
                )

        history.append(current)
        occupied_layers.append(occupied)

    manual_routed_points: tuple[tuple[float, float, float], ...] = ()
    manual_actual_contact_points: tuple[tuple[float, float, float], ...] = ()
    manual_unrouted_points: tuple[tuple[float, float, float], ...] = ()
    if manual_points:
        (
            manual_routed_points,
            manual_unrouted_points,
            manual_actual_contact_points,
        ) = _append_point_anchors(
            manual_points,
            "manual",
            None,
            prepared,
            anchors,
            occupied_layers,
            analysis_config,
            config,
            support,
            surface_normals,
            body_collision_radius,
            collision_radius,
            placed_points,
            on_anchor,
            layer_count,
        )
    manual_routed = len(manual_routed_points)

    if require_zones:
        # Painted "grow supports here" areas: each brush sample becomes a
        # candidate contact, thinned to the normal support spacing so a dense
        # stroke produces a sanely spaced cluster of supports.
        _append_point_anchors(
            tuple((zone.x, zone.y, zone.z) for zone in require_zones),
            "painted",
            support.support_spacing_mm,
            prepared,
            anchors,
            occupied_layers,
            analysis_config,
            config,
            support,
            surface_normals,
            body_collision_radius,
            collision_radius,
            placed_points,
            on_anchor,
            layer_count,
        )

    # Manual-only passes brace among the freshly routed manual/painted anchors
    # (the existing auto supports are not visible to this pass; slicing replans
    # everything together, so cross braces to them still appear in the print).
    anchors = list(_assign_tree_loads(tuple(anchors)))
    braces = _plan_braces(
        anchors, occupied_layers, analysis_config, config, support, layer_count, brace_radius, bed_interface_layers
    )
    return replace(
        build_support_plan(
        tuple(anchors),
        braces,
        config,
        support,
        raft_mask=raft_mask,
        raft_shadow_mask=raft_shadow_mask,
        raft_offset_px=raft_offset_px,
        ),
        manual_requested=len(manual_points),
        manual_routed=manual_routed,
        manual_routed_points=manual_routed_points,
        manual_actual_contact_points=manual_actual_contact_points,
        manual_unrouted_points=manual_unrouted_points,
        auto_components_detected=auto_components_detected,
        auto_components_supported=auto_components_supported,
        auto_components_excluded=auto_components_excluded,
        auto_components_unrouted=auto_components_unrouted,
        auto_candidates_attempted=auto_candidates_attempted,
        auto_candidates_routed=auto_candidates_routed,
        auto_candidates_unrouted=auto_candidates_attempted - auto_candidates_routed,
        auto_unrouted_area_mm2=auto_unrouted_area_mm2,
    )


def extend_support_plan(
    prepared: PreparedMesh,
    config: PrintConfig,
    support: SupportConfig,
    layer_count: int,
    base_plan: SupportPlan,
    manual_points: tuple[tuple[float, float, float], ...],
    progress: Progress | None = None,
    on_anchor: Callable[[SupportAnchor], None] | None = None,
) -> SupportPlan:
    """Append detector/manual contacts without repeating automatic analysis.

    Validation feedback normally changes only a short list of contact points;
    the mesh, support settings, and automatic tree are identical.  Re-running
    connected-component and morphology analysis over every layer made those
    retries almost as expensive as the first slice.  This path reconstructs
    only the collision occupancy needed to route the new contacts, then
    reassigns tree loads and braces across the combined graph.

    A cache loader must establish that ``base_plan`` was produced for the exact
    mesh/config/support identity and that its requested manual points are an
    exact prefix of the current request.  The CLI cache does both before this
    function is called.
    """

    support.validate()
    if not manual_points:
        return base_plan

    analysis_config = _analysis_config(config, support)
    analysis_pixel_mm = min(
        analysis_config.pixel_size_x_mm,
        analysis_config.pixel_size_y_mm,
    )
    body_collision_radius = mm_to_px(
        max(support.post_radius_mm, support.tip_radius_mm),
        analysis_pixel_mm,
    )
    collision_radius = mm_to_px(
        max(support.post_radius_mm, support.tip_radius_mm)
        + support.collision_clearance_mm,
        analysis_pixel_mm,
    )
    occupied_layers: list[_OccupiedLayer] = []
    interval = max(1, layer_count // 20)
    for layer_index in range(layer_count):
        if progress and (layer_index == 0 or (layer_index + 1) % interval == 0):
            progress(f"support refinement layer {layer_index + 1}/{layer_count}")
        occupied = _occupied_layer(
            layer_index,
            render_prepared_layer(prepared, analysis_config, layer_index),
        )
        if occupied is not None:
            occupied_layers.append(occupied)

    anchors = list(base_plan.anchors)
    placed_points = _PlacedPointIndex(config)
    for anchor in anchors:
        placed_points.add(anchor.x, anchor.y, anchor.top_layer)
    surface_normals = _SurfaceNormalSampler(prepared.mesh.triangles)

    # Previously unrouted contacts are retried because a newly accepted trunk
    # can become their safe tree parent.  Already routed contacts and their
    # actual (possibly nearby) surface points remain stable.
    retry_points = base_plan.manual_unrouted_points + manual_points
    (
        routed_points,
        unrouted_points,
        actual_contact_points,
    ) = _append_point_anchors(
        retry_points,
        "manual",
        None,
        prepared,
        anchors,
        occupied_layers,
        analysis_config,
        config,
        support,
        surface_normals,
        body_collision_radius,
        collision_radius,
        placed_points,
        on_anchor,
        layer_count,
    )

    anchors = list(_assign_tree_loads(tuple(anchors)))
    output_pixel_mm = min(config.pixel_size_x_mm, config.pixel_size_y_mm)
    brace_radius = mm_to_px(support.brace_radius_mm, output_pixel_mm)
    bed_interface_layers = 0 if support.bed_interface == "none" else max(
        1,
        int(ceil(support.bed_interface_thickness_mm / config.layer_height_mm)),
    )
    braces = _plan_braces(
        anchors,
        occupied_layers,
        analysis_config,
        config,
        support,
        layer_count,
        brace_radius,
        bed_interface_layers,
    )
    rebuilt = build_support_plan(
        tuple(anchors),
        braces,
        config,
        support,
        raft_mask=base_plan.raft_mask,
        raft_shadow_mask=base_plan.raft_shadow_mask,
        raft_offset_px=base_plan.raft_offset_px,
    )
    return replace(
        rebuilt,
        manual_requested=base_plan.manual_requested + len(manual_points),
        manual_routed=base_plan.manual_routed + len(routed_points),
        manual_routed_points=base_plan.manual_routed_points + routed_points,
        manual_actual_contact_points=(
            base_plan.manual_actual_contact_points + actual_contact_points
        ),
        manual_unrouted_points=unrouted_points,
        auto_components_detected=base_plan.auto_components_detected,
        auto_components_supported=base_plan.auto_components_supported,
        auto_components_excluded=base_plan.auto_components_excluded,
        auto_components_unrouted=base_plan.auto_components_unrouted,
        auto_candidates_attempted=base_plan.auto_candidates_attempted,
        auto_candidates_routed=base_plan.auto_candidates_routed,
        auto_candidates_unrouted=base_plan.auto_candidates_unrouted,
        auto_unrouted_area_mm2=base_plan.auto_unrouted_area_mm2,
    )


def build_support_plan(
    anchors: tuple[SupportAnchor, ...],
    braces: tuple[SupportBrace, ...],
    config: PrintConfig,
    support: SupportConfig,
    raft_mask: bytes | None = None,
    raft_shadow_mask: bytes | None = None,
    raft_offset_px: int = 0,
    post_radius_mm: float | None = None,
    tip_radius_mm: float | None = None,
    foot_radius_mm: float | None = None,
) -> SupportPlan:
    """Assemble a SupportPlan around externally supplied anchors/braces using
    the same derived scalars plan_supports computes. Used both as plan_supports'
    final step and to rebuild a previously previewed plan for verbatim slicing."""
    output_pixel_mm = min(config.pixel_size_x_mm, config.pixel_size_y_mm)
    post_mm = support.post_radius_mm if post_radius_mm is None else max(0.01, post_radius_mm)
    tip_mm = support.tip_radius_mm if tip_radius_mm is None else max(0.01, tip_radius_mm)
    foot_mm = support.foot_radius_mm if foot_radius_mm is None else max(0.01, foot_radius_mm)
    bed_interface_layers = 0 if support.bed_interface == "none" else max(
        1,
        int(ceil(support.bed_interface_thickness_mm / config.layer_height_mm)),
    )
    plan = SupportPlan(
        anchors,
        mm_to_px(post_mm, output_pixel_mm),
        mm_to_px(tip_mm, output_pixel_mm),
        mm_to_px(foot_mm, output_pixel_mm),
        max(post_mm, tip_mm),
        bed_interface_layers,
        contact_depth_layers=int(ceil(support.tip_contact_depth_mm / config.layer_height_mm)),
        braces=braces,
        bed_interface=support.bed_interface,
        raft_radius_px=mm_to_px(foot_mm + support.raft_margin_mm, output_pixel_mm),
        brace_layer_radius=max(0, int(ceil(support.brace_radius_mm / config.layer_height_mm))),
        spherical_contact_enabled=support.spherical_contact_enabled,
        contact_sphere_radius_mm=max(0.0, support.spherical_contact_diameter_mm * 0.5),
        contact_sphere_inset_mm=support.spherical_contact_inset_mm,
        pixel_size_x_mm=config.pixel_size_x_mm,
        pixel_size_y_mm=config.pixel_size_y_mm,
        layer_height_mm=config.layer_height_mm,
        tree_trunk_radius_px=mm_to_px(
            max(post_mm, support.tree_trunk_radius_mm), output_pixel_mm
        ),
        pedestal_height_layers=max(
            1, int(ceil(support.pedestal_height_mm / config.layer_height_mm))
        ),
        raft_mask=raft_mask,
        raft_shadow_mask=raft_shadow_mask,
        raft_offset_px=raft_offset_px,
        raft_chamfer_width_mm=support.raft_chamfer_width_mm,
        raft_chamfer_angle_deg=support.raft_chamfer_angle_deg,
        tree_root_count=sum(1 for anchor in anchors if anchor.parent_id < 0),
        tree_branch_count=sum(1 for anchor in anchors if anchor.kind == "branch"),
        max_tree_load=max((anchor.load_count for anchor in anchors), default=0),
    )
    return _include_raft_roots(plan, config)


def _append_point_anchors(
    points: tuple[tuple[float, float, float], ...],
    role: str,
    spacing_mm: float | None,
    prepared: PreparedMesh,
    anchors: list[SupportAnchor],
    occupied_layers: list[_OccupiedLayer],
    analysis_config: PrintConfig,
    config: PrintConfig,
    support: SupportConfig,
    surface_normals: "_SurfaceNormalSampler",
    body_collision_radius: int,
    collision_radius: int,
    placed_points: "_PlacedPointIndex",
    on_anchor: Callable[[SupportAnchor], None] | None,
    layer_count: int,
) -> tuple[
    tuple[tuple[float, float, float], ...],
    tuple[tuple[float, float, float], ...],
    tuple[tuple[float, float, float], ...],
]:
    """Route user-supplied contact points using the same machinery as the auto
    planner, so a manual or painted support behaves exactly as an automatic one
    would if it had chosen to anchor at that spot. Contacts that cannot route in
    the first pass are retried after the other contacts have been accepted. This
    matters for tree supports: a later bed-routed shaft can be the safe parent of
    an earlier contact, and input ordering must not decide whether it prints.

    Each point is the (x, y, z) contact location in millimetres (the same world
    coordinates the renderer draws supports in). When spacing_mm is given,
    points that land too close to an already placed support are skipped (used
    to thin dense painted strokes); manual clicks pass None and always route."""
    pixel_x = analysis_config.pixel_size_x_mm
    pixel_y = analysis_config.pixel_size_y_mm
    layer_height = analysis_config.layer_height_mm
    layer_cache: dict[int, LayerRaster] = {}
    branch_anchors = [
        _scale_anchor_to_analysis(anchor, analysis_config, config)
        for anchor in anchors
    ]

    def _layer(layer_index: int) -> LayerRaster:
        if layer_index < 0:
            return LayerRaster(analysis_config.resolution_x, analysis_config.resolution_y)
        cached = layer_cache.get(layer_index)
        if cached is None:
            cached = render_prepared_layer(prepared, analysis_config, layer_index)
            layer_cache[layer_index] = cached
        return cached

    pending = [tuple(float(value) for value in point) for point in points]
    routed_points: list[tuple[float, float, float]] = []
    actual_contact_points: list[tuple[float, float, float]] = []
    while pending:
        deferred: list[tuple[float, float, float]] = []
        routed_this_pass = 0
        for point in pending:
            x_mm, y_mm, z_mm = point
            x = min(max(0, int(round(x_mm / pixel_x - 0.5))), analysis_config.resolution_x - 1)
            y = min(max(0, int(round(y_mm / pixel_y - 0.5))), analysis_config.resolution_y - 1)
            layer_index = min(max(0, int(round(z_mm / layer_height - 0.5))), max(0, layer_count - 1))

            if spacing_mm is not None:
                out_x, out_y = _scale_point_to_output(x, y, analysis_config, config)
                if placed_points.too_close(out_x, out_y, spacing_mm, layer_index):
                    # Painted strokes are deliberately thinned. A skipped brush
                    # sample is satisfied by the nearby support, not a failure.
                    routed_points.append(point)
                    actual_contact_points.append(point)
                    continue

            fallback_normal = _estimate_surface_normal(
                _layer(layer_index), _layer(layer_index - 1), analysis_config, x, y
            )
            contact_point = (x_mm, y_mm, z_mm)
            normal = surface_normals.normal_at_contact(contact_point, fallback_normal)

            route = _find_support_route(
                occupied_layers,
                analysis_config,
                support,
                x,
                y,
                layer_index,
                normal,
                body_collision_radius,
                collision_radius,
                role,
                branch_anchors=tuple(branch_anchors),
            )
            routes: tuple[SupportAnchor, ...]
            actual_contact = point
            if route is not None:
                routes = (route,)
            elif role == "manual":
                dogleg = _find_dogleg_route(
                    occupied_layers,
                    analysis_config,
                    support,
                    x,
                    y,
                    layer_index,
                    body_collision_radius,
                    collision_radius,
                    role,
                )
                if dogleg is None:
                    nearby = _find_nearby_manual_contact_route(
                        _layer(layer_index),
                        _layer(layer_index - 1),
                        occupied_layers,
                        analysis_config,
                        support,
                        x,
                        y,
                        layer_index,
                        surface_normals,
                        body_collision_radius,
                        collision_radius,
                        role,
                        tuple(branch_anchors),
                    )
                    if nearby is None:
                        deferred.append(point)
                        continue
                    routes, actual_contact = nearby
                else:
                    routes = dogleg
            else:
                deferred.append(point)
                continue

            routes = _register_tree_routes(routes, branch_anchors)
            output_routes = tuple(
                _scale_anchor_to_output(item, analysis_config, config)
                for item in routes
            )
            contact_anchor = output_routes[-1]
            placed_points.add(contact_anchor.x, contact_anchor.y, contact_anchor.top_layer)
            anchors.extend(output_routes)
            branch_anchors.extend(routes)
            routed_points.append(point)
            actual_contact_points.append(actual_contact)
            routed_this_pass += 1
            if on_anchor is not None:
                for out_anchor in output_routes:
                    on_anchor(out_anchor)

        if not deferred or routed_this_pass == 0:
            pending = deferred
            break
        pending = deferred

    return tuple(routed_points), tuple(pending), tuple(actual_contact_points)


def _in_paint_zones(point: Point3, zones: tuple[PaintZone, ...]) -> bool:
    for zone in zones:
        dx = point[0] - zone.x
        dy = point[1] - zone.y
        dz = point[2] - zone.z
        if dx * dx + dy * dy + dz * dz <= zone.radius * zone.radius:
            return True
    return False


def apply_supports(layer: LayerRaster, layer_index: int, plan: SupportPlan) -> None:
    # Sample the original model before adding any raft/support pixels. Lower
    # overlap must begin inside existing model material, never in empty space
    # beneath a thin/curved landing surface. Keep sparse rasters sparse.
    lower_contacts = set()
    if plan.contact_depth_layers:
        for anchor in plan.anchors:
            if (anchor.kind == "enforcer" and _is_contact_anchor(anchor) and
                    anchor.base_layer-plan.contact_depth_layers <= layer_index < anchor.base_layer):
                bx, by = _base_xy(anchor)
                if any(start <= bx < end for start, end in layer.row_spans(by)):
                    lower_contacts.add((bx, by))
    if layer_index < plan.raft_layers and plan.bed_interface in {"raft", "skate"}:
        _apply_raft_mask(layer, plan, layer_index)

    for brace in plan.braces:
        if brace.start_layer - plan.brace_layer_radius <= layer_index <= brace.end_layer + plan.brace_layer_radius:
            x, y = _brace_center(brace, layer_index)
            layer.add_disk(x, y, brace.radius_px, 255)

    for anchor in plan.anchors:
        depth = plan.contact_depth_layers if _is_contact_anchor(anchor) else 0
        base_depth = depth if anchor.kind == "enforcer" else 0
        if layer_index < max(0, anchor.base_layer - base_depth) or layer_index > anchor.top_layer + depth:
            continue
        if layer_index < anchor.base_layer:
            # A model-to-model support has two contacts. Its lower contact
            # needs the same bounded overlap as its upper tip; otherwise
            # analysis-grid rounding can leave the foot barely touching or
            # detached in the saved full-resolution layers. Do not extend
            # branch joints or bed feet into unrelated geometry.
            base_x, base_y = _base_xy(anchor)
            if (base_x, base_y) in lower_contacts:
                layer.add_disk(base_x, base_y, plan.tip_radius_px, 255)
            continue
        if layer_index > anchor.top_layer:
            # Only the narrow tip penetrates: no shaft, joint or contact bulb.
            layer.add_disk(anchor.x, anchor.y, plan.tip_radius_px, 255)
            continue

        base_x, base_y = _base_xy(anchor)
        if (
            anchor.kind == "bed"
            and anchor.base_layer == 0
            and layer_index < plan.raft_layers
            and (plan.bed_interface == "feet" or anchor.role == "manual-trunk")
        ):
            layer.add_disk(base_x, base_y, _bed_radius(plan), 255)

        x, y = _support_center(anchor, layer_index)
        radius = _support_radius_at_layer(anchor, layer_index, plan)
        layer.add_disk(x, y, radius, 255)
        _add_branch_base_joint(layer, layer_index, anchor, plan)
        _add_joint_sphere(layer, layer_index, anchor, plan)
        if plan.spherical_contact_enabled:
            _add_spherical_contact(layer, layer_index, anchor, plan)


def attach_raft_masks(
    plan: SupportPlan,
    prepared: PreparedMesh,
    config: PrintConfig,
    support: SupportConfig,
) -> SupportPlan:
    """Fill in the projected raft masks on a plan that was built without them
    (support previews skip the masks because the viewport draws its own raft)."""
    if plan.raft_mask is not None:
        return plan
    raft_mask, raft_shadow_mask, raft_offset_px = _projected_raft_masks(prepared, config, support)
    return _include_raft_roots(replace(
        plan,
        raft_mask=raft_mask,
        raft_shadow_mask=raft_shadow_mask,
        raft_offset_px=raft_offset_px,
    ), config)


def _include_raft_roots(plan: SupportPlan, config: PrintConfig) -> SupportPlan:
    """Include offset feet and their connection to the projected model raft.

    Apply before chamfering so even the narrow bottom layer covers the feet.
    This also runs after feedback adds roots to an existing cached plan.
    """
    if plan.raft_shadow_mask is None or plan.bed_interface not in {"raft", "skate"}:
        return plan
    shadow = LayerRaster(config.resolution_x, config.resolution_y)
    shadow.pixels[:] = plan.raft_shadow_mask
    for anchor in plan.anchors:
        if anchor.kind != "bed" or anchor.base_layer != 0:
            continue
        bx, by = _base_xy(anchor)
        steps = max(1, int(ceil(max(abs(anchor.x - bx), abs(anchor.y - by))
                              / max(1, plan.foot_radius_px))))
        for step in range(steps + 1):
            t = step / steps
            shadow.add_disk(round(bx + (anchor.x - bx) * t),
                            round(by + (anchor.y - by) * t), plan.foot_radius_px, 255)
    return replace(plan, raft_shadow_mask=bytes(shadow.pixels),
                   raft_mask=bytes(dilate_mask(shadow, plan.raft_offset_px).translate(_BINARY_TO_RASTER)))


def _projected_raft_masks(prepared: PreparedMesh, config: PrintConfig, support: SupportConfig) -> tuple[bytes | None, bytes | None, int]:
    if support.bed_interface not in {"raft", "skate"}:
        return None, None, 0

    shadow = LayerRaster(config.resolution_x, config.resolution_y)
    for triangle in prepared.mesh.triangles:
        _add_projected_triangle(shadow, triangle, config)

    if shadow.count_on() == 0:
        return None, None, 0

    offset_px = 0
    if support.raft_margin_mm > 0:
        offset_px = mm_to_px(support.raft_margin_mm, min(config.pixel_size_x_mm, config.pixel_size_y_mm))
    full_mask = bytes(dilate_mask(shadow, offset_px).translate(_BINARY_TO_RASTER))
    return full_mask, bytes(shadow.pixels), offset_px


def _add_projected_triangle(layer: LayerRaster, triangle: Triangle, config: PrintConfig) -> None:
    projected = tuple((point[0] / config.pixel_size_x_mm, point[1] / config.pixel_size_y_mm) for point in triangle)
    area2 = (
        (projected[1][0] - projected[0][0]) * (projected[2][1] - projected[0][1])
        - (projected[1][1] - projected[0][1]) * (projected[2][0] - projected[0][0])
    )
    if abs(area2) < 1e-6:
        return

    min_y = max(0, int(floor(min(point[1] for point in projected))))
    max_y = min(layer.height - 1, int(ceil(max(point[1] for point in projected))) - 1)
    if min_y > max_y:
        return

    for y in range(min_y, max_y + 1):
        scan_y = y + 0.5
        intersections: list[float] = []
        for edge_index in range(3):
            x0, y0 = projected[edge_index]
            x1, y1 = projected[(edge_index + 1) % 3]
            if y0 == y1:
                continue
            if (y0 <= scan_y < y1) or (y1 <= scan_y < y0):
                t = (scan_y - y0) / (y1 - y0)
                intersections.append(x0 + (x1 - x0) * t)
        if len(intersections) < 2:
            continue
        intersections.sort()
        for index in range(0, len(intersections) - 1, 2):
            x0 = int(floor(intersections[index]))
            x1 = int(ceil(intersections[index + 1])) - 1
            layer.set_span(y, x0, x1, 255)


def _apply_raft_mask(layer: LayerRaster, plan: SupportPlan, layer_index: int) -> None:
    mask = _raft_mask_for_layer(plan, layer_index, layer.width, layer.height)
    if not mask:
        return
    if layer.count_on() == 0:
        layer.pixels[:] = mask
        return
    for index, value in enumerate(mask):
        if value:
            layer.pixels[index] = 255


def _raft_mask_for_layer(plan: SupportPlan, layer_index: int, width: int, height: int) -> bytes | None:
    if not plan.raft_mask:
        return None
    inset_px = _raft_chamfer_inset_px(plan, layer_index)
    if inset_px <= 0 or not plan.raft_shadow_mask:
        return plan.raft_mask
    effective_offset_px = max(0, plan.raft_offset_px - inset_px)
    if effective_offset_px >= plan.raft_offset_px:
        return plan.raft_mask
    shadow = LayerRaster(width, height, bytearray(plan.raft_shadow_mask))
    return bytes(dilate_mask(shadow, effective_offset_px).translate(_BINARY_TO_RASTER))


def _raft_chamfer_inset_px(plan: SupportPlan, layer_index: int) -> int:
    if plan.raft_chamfer_width_mm <= 0:
        return 0
    z_mm = (layer_index + 0.5) * plan.layer_height_mm
    inset_mm = plan.raft_chamfer_width_mm - z_mm * tan(radians(plan.raft_chamfer_angle_deg))
    if inset_mm <= 0:
        return 0
    return int(ceil(inset_mm / min(plan.pixel_size_x_mm, plan.pixel_size_y_mm)))


def _overhang_allowance_px(
    config: PrintConfig,
    support: SupportConfig,
    pixel_mm: float,
    gap_layers: int,
) -> int:
    angle = radians(support.overhang_angle_deg)
    allowance_mm = config.layer_height_mm * gap_layers / tan(angle) + support.overhang_margin_mm
    return max(0, int(floor(allowance_mm / pixel_mm)))


def _overhang_lookback_layers(
    config: PrintConfig,
    support: SupportConfig,
    pixel_mm: float,
) -> int:
    angle = radians(support.overhang_angle_deg)
    allowance_per_layer_mm = config.layer_height_mm / tan(angle)
    if allowance_per_layer_mm <= 0:
        return 1
    return max(1, min(24, int(ceil(pixel_mm / allowance_per_layer_mm))))


class _SurfaceNormalSampler:
    def __init__(self, triangles: Sequence[Triangle]) -> None:
        limit = _surface_normal_sample_limit()
        self.sample_stride = max(1, int(ceil(len(triangles) / max(1, limit))))
        self.surfaces = tuple(
            surface
            for index in range(0, len(triangles), self.sample_stride)
            if (surface := _surface_from_triangle(triangles[index])) is not None
        )
        self.cell_size_mm = self._cell_size()
        self.cells: dict[tuple[int, int, int], list[int]] = {}
        self._build_index()

    def normal_at_contact(
        self,
        point: Point3,
        fallback: tuple[float, float, float],
    ) -> tuple[float, float, float]:
        if not self.surfaces:
            return fallback
        surfaces = self._nearby_surfaces(point)
        nearest = min(surfaces, key=lambda surface: _point_triangle_distance2(point, surface.triangle))
        aligned: list[tuple[float, Point3]] = []
        for surface in surfaces:
            if _dot3(nearest.normal, surface.normal) <= 0.95:
                continue
            aligned.append((_point_triangle_distance2(point, surface.triangle), surface.normal))
        if not aligned:
            return nearest.normal
        aligned.sort(key=lambda item: item[0])
        return _average_normals(tuple(normal for _distance, normal in aligned[:30]))

    def _cell_size(self) -> float:
        if not self.surfaces:
            return 4.0
        min_x = min(surface.min_x for surface in self.surfaces)
        min_y = min(surface.min_y for surface in self.surfaces)
        min_z = min(surface.min_z for surface in self.surfaces)
        max_x = max(surface.max_x for surface in self.surfaces)
        max_y = max(surface.max_y for surface in self.surfaces)
        max_z = max(surface.max_z for surface in self.surfaces)
        span = max(max_x - min_x, max_y - min_y, max_z - min_z, 1.0)
        return max(4.0, span / 24.0)

    def _build_index(self) -> None:
        if not self.surfaces:
            return
        for index, surface in enumerate(self.surfaces):
            min_key = self._cell_key((surface.min_x, surface.min_y, surface.min_z))
            max_key = self._cell_key((surface.max_x, surface.max_y, surface.max_z))
            for ix in range(min_key[0], max_key[0] + 1):
                for iy in range(min_key[1], max_key[1] + 1):
                    for iz in range(min_key[2], max_key[2] + 1):
                        self.cells.setdefault((ix, iy, iz), []).append(index)

    def _nearby_surfaces(self, point: Point3) -> tuple[_Surface, ...]:
        if not self.cells:
            return self.surfaces
        center = self._cell_key(point)
        target = min(len(self.surfaces), 96)
        indices: list[int] = []
        seen: set[int] = set()
        for radius in range(5):
            for ix in range(center[0] - radius, center[0] + radius + 1):
                for iy in range(center[1] - radius, center[1] + radius + 1):
                    for iz in range(center[2] - radius, center[2] + radius + 1):
                        if radius and (
                            abs(ix - center[0]) < radius
                            and abs(iy - center[1]) < radius
                            and abs(iz - center[2]) < radius
                        ):
                            continue
                        for index in self.cells.get((ix, iy, iz), ()):
                            if index in seen:
                                continue
                            seen.add(index)
                            indices.append(index)
            if len(indices) >= target:
                break
        if not indices:
            return self.surfaces
        return tuple(self.surfaces[index] for index in indices)

    def _cell_key(self, point: Point3) -> tuple[int, int, int]:
        cell = self.cell_size_mm
        return (
            int(floor(point[0] / cell)),
            int(floor(point[1] / cell)),
            int(floor(point[2] / cell)),
        )


def _surface_normal_sample_limit() -> int:
    value = os.environ.get("RESIN_SLICER_SURFACE_NORMAL_SAMPLES", "250000")
    try:
        return max(1_000, min(1_000_000, int(value)))
    except ValueError:
        return 250_000


def _surface_from_triangle(triangle: Triangle) -> _Surface | None:
    a, b, c = triangle
    normal = _cross3(_sub3(b, a), _sub3(c, a))
    if _length3(normal) < 1e-9:
        return None
    return _Surface(
        triangle,
        _normalize_downward(normal),
        min(a[0], b[0], c[0]),
        min(a[1], b[1], c[1]),
        min(a[2], b[2], c[2]),
        max(a[0], b[0], c[0]),
        max(a[1], b[1], c[1]),
        max(a[2], b[2], c[2]),
    )


def _average_normals(normals: tuple[Point3, ...]) -> tuple[float, float, float]:
    if not normals:
        return (0.0, 0.0, -1.0)
    total = (0.0, 0.0, 0.0)
    for normal in normals:
        total = _add3(total, normal)
    return _normalize_downward(total)


def _estimate_surface_normal(
    current: LayerRaster,
    below: LayerRaster,
    config: PrintConfig,
    x: int,
    y: int,
) -> tuple[float, float, float]:
    center = _occupied_at(current, x, y)
    if not center:
        return (0.0, 0.0, -1.0)
    dx = _occupied_at(current, x + 1, y) - _occupied_at(current, x - 1, y)
    dy = _occupied_at(current, x, y + 1) - _occupied_at(current, x, y - 1)
    dz = center - _occupied_at(below, x, y)
    normal = (
        -dx / max(config.pixel_size_x_mm, 1e-6),
        -dy / max(config.pixel_size_y_mm, 1e-6),
        -dz / max(config.layer_height_mm, 1e-6),
    )
    return _normalize_downward(normal)


def _occupied_at(layer: LayerRaster, x: int, y: int) -> int:
    if x < 0 or y < 0 or x >= layer.width or y >= layer.height:
        return 0
    return 1 if layer.pixels[y * layer.width + x] else 0


def _tip_direction_from_normal(normal: tuple[float, float, float]) -> tuple[float, float, float]:
    return _normalize_downward(normal)


def _normalize_downward(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = vector
    if z > 0:
        x, y, z = -x, -y, -z
    length = _length3((x, y, z))
    if length < 1e-6:
        return (0.0, 0.0, -1.0)
    x, y, z = x / length, y / length, z / length
    if z > -0.05:
        return _normalize_downward((x * 0.35, y * 0.35, -1.0))
    return (x, y, z)


def _length3(vector: tuple[float, float, float]) -> float:
    return sqrt(vector[0] * vector[0] + vector[1] * vector[1] + vector[2] * vector[2])


def _dot3(a: Point3, b: Point3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _sub3(a: Point3, b: Point3) -> Point3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add3(a: Point3, b: Point3) -> Point3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _scale3(vector: Point3, scale: float) -> Point3:
    return (vector[0] * scale, vector[1] * scale, vector[2] * scale)


def _cross3(a: Point3, b: Point3) -> Point3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _point_triangle_distance2(point: Point3, triangle: Triangle) -> float:
    closest = _closest_point_on_triangle(point, triangle)
    delta = _sub3(point, closest)
    return _dot3(delta, delta)


def _closest_point_on_triangle(point: Point3, triangle: Triangle) -> Point3:
    a, b, c = triangle
    ab = _sub3(b, a)
    ac = _sub3(c, a)
    ap = _sub3(point, a)
    d1 = _dot3(ab, ap)
    d2 = _dot3(ac, ap)
    if d1 <= 0.0 and d2 <= 0.0:
        return a

    bp = _sub3(point, b)
    d3 = _dot3(ab, bp)
    d4 = _dot3(ac, bp)
    if d3 >= 0.0 and d4 <= d3:
        return b

    vc = d1 * d4 - d3 * d2
    if vc <= 0.0 and d1 >= 0.0 and d3 <= 0.0:
        v = d1 / (d1 - d3)
        return _add3(a, _scale3(ab, v))

    cp = _sub3(point, c)
    d5 = _dot3(ab, cp)
    d6 = _dot3(ac, cp)
    if d6 >= 0.0 and d5 <= d6:
        return c

    vb = d5 * d2 - d1 * d6
    if vb <= 0.0 and d2 >= 0.0 and d6 <= 0.0:
        w = d2 / (d2 - d6)
        return _add3(a, _scale3(ac, w))

    va = d3 * d6 - d5 * d4
    if va <= 0.0 and (d4 - d3) >= 0.0 and (d5 - d6) >= 0.0:
        w = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        return _add3(b, _scale3(_sub3(c, b), w))

    denom = 1.0 / (va + vb + vc)
    v = vb * denom
    w = vc * denom
    return _add3(a, _add3(_scale3(ab, v), _scale3(ac, w)))


def _analysis_config(config: PrintConfig, support: SupportConfig) -> PrintConfig:
    pixels = config.resolution_x * config.resolution_y
    if pixels <= support.analysis_max_pixels:
        return config

    scale = (support.analysis_max_pixels / pixels) ** 0.5
    resolution_x = max(64, int(config.resolution_x * scale))
    resolution_y = max(64, int(config.resolution_y * scale))
    return config.with_overrides(
        resolution_x=resolution_x,
        resolution_y=resolution_y,
        max_pixels_per_layer=max(config.max_pixels_per_layer, resolution_x * resolution_y),
    )


def _register_tree_routes(
    routes: tuple[SupportAnchor, ...],
    existing: Sequence[SupportAnchor],
) -> tuple[SupportAnchor, ...]:
    """Assign stable parent/tree IDs to newly accepted logical support parts.

    Route search is intentionally stateless.  Registration is the small bridge
    between that collision-safe geometry and the load-bearing tree graph used
    at render time.  A branch returned by the normal tree search already names
    its parent; dogleg/manual routes are linked by their exact shared endpoint.
    """

    registered: list[SupportAnchor] = []
    for route in routes:
        anchor_id = len(existing) + len(registered)
        available = tuple(existing) + tuple(registered)
        parent_id = route.parent_id
        parent: SupportAnchor | None = None
        if parent_id >= 0:
            parent = next(
                (item for item in reversed(available) if item.anchor_id == parent_id),
                None,
            )
        if route.kind == "branch" and parent is None:
            base_x, base_y = _base_xy(route)
            for item in reversed(available):
                if not item.base_layer <= route.base_layer <= item.top_layer:
                    continue
                center_x, center_y = _support_center(item, route.base_layer)
                if (center_x, center_y) == (base_x, base_y):
                    parent = item
                    parent_id = item.anchor_id
                    break

        if parent is not None:
            tree_id = parent.tree_id if parent.tree_id >= 0 else parent.anchor_id
        else:
            parent_id = -1
            tree_id = anchor_id
        registered.append(
            replace(
                route,
                anchor_id=anchor_id,
                parent_id=parent_id,
                tree_id=tree_id,
                load_count=1,
            )
        )
    return tuple(registered)


def _is_contact_anchor(anchor: SupportAnchor) -> bool:
    return not anchor.role.endswith("-trunk")


def _tree_tip_counts(anchors: Sequence[SupportAnchor]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for anchor in anchors:
        if anchor.tree_id >= 0 and _is_contact_anchor(anchor):
            counts[anchor.tree_id] = counts.get(anchor.tree_id, 0) + 1
    return counts


def _assign_tree_loads(anchors: tuple[SupportAnchor, ...]) -> tuple[SupportAnchor, ...]:
    """Accumulate supported contact count from leaves down to each root.

    Parents are always accepted before children, which makes a single reverse
    pass sufficient and keeps this O(number of supports) in time and memory.
    """

    loads: dict[int, int] = {anchor.anchor_id: 0 for anchor in anchors if anchor.anchor_id >= 0}
    for anchor in reversed(anchors):
        if anchor.anchor_id < 0:
            continue
        load = loads.get(anchor.anchor_id, 0)
        if _is_contact_anchor(anchor):
            load += 1
        load = max(1, load)
        loads[anchor.anchor_id] = load
        if anchor.parent_id >= 0:
            loads[anchor.parent_id] = loads.get(anchor.parent_id, 0) + load
    return tuple(
        replace(anchor, load_count=max(1, loads.get(anchor.anchor_id, 1)))
        for anchor in anchors
    )


def _with_tree_clearance(
    anchor: SupportAnchor,
    occupied_layers: list[_OccupiedLayer],
    config: PrintConfig,
    support: SupportConfig,
) -> SupportAnchor:
    if not support.branching_enabled:
        return anchor
    tree_collision_radius = mm_to_px(
        max(support.tree_trunk_radius_mm, support.post_radius_mm)
        + support.collision_clearance_mm,
        min(config.pixel_size_x_mm, config.pixel_size_y_mm),
    )
    return replace(
        anchor,
        tree_clearance_ok=not _route_collides(
            occupied_layers,
            config.resolution_x,
            config.resolution_y,
            anchor,
            tree_collision_radius,
        ),
    )


def _find_support_route(
    occupied_layers: list[_OccupiedLayer],
    config: PrintConfig,
    support: SupportConfig,
    top_x: int,
    top_y: int,
    top_layer: int,
    normal: tuple[float, float, float],
    body_collision_radius: int,
    collision_radius: int,
    role: str,
    branch_anchors: tuple[SupportAnchor, ...] = (),
) -> SupportAnchor | None:
    route_normals = [normal]
    # Detector feedback identifies a first-layer contact pixel, but that pixel
    # can be equidistant from a side wall and the intended lower face. The mesh
    # normal sampler may then aim the short tip sideways into the part and make
    # every otherwise-safe shaft appear blocked. Current resin slicers commonly
    # allow a vertical contact in this situation. Retry manual contacts with a
    # vertical tip, while retaining every path collision and shaft-angle check.
    if role == "manual" and (
        abs(normal[0]) > 1e-6 or abs(normal[1]) > 1e-6 or abs(normal[2] + 1.0) > 1e-6
    ):
        route_normals.append((0.0, 0.0, -1.0))

    for route_normal in route_normals:
        # A resin tree should merge nearby heads even when each one could reach
        # the plate independently.  The older fallback-only order produced the
        # visually incorrect forest of equal vertical posts seen in the audited
        # Tentoonstelling CTB.
        if support.branching_enabled and branch_anchors:
            branch = _find_branch_route(
                occupied_layers,
                config,
                support,
                top_x,
                top_y,
                top_layer,
                route_normal,
                collision_radius,
                role,
                branch_anchors,
                maximum_reach_mm=support.tree_merge_distance_mm,
            )
            if branch is not None:
                return branch

        route = _build_route(
            config,
            support,
            top_x,
            top_y,
            top_layer,
            route_normal,
            top_x,
            top_y,
            0,
            "bed",
            role,
        )
        joint_x, joint_y = _joint_xy(route)
        if (_bed_base_fits(top_x, top_y, config, support)
                and 0 <= joint_x < config.resolution_x and 0 <= joint_y < config.resolution_y):
            if not _route_collides(
                occupied_layers,
                config.resolution_x,
                config.resolution_y,
                route,
                body_collision_radius,
            ):
                return _with_tree_clearance(
                    route, occupied_layers, config, support
                )

        if role == "manual":
            for index, (base_x, base_y) in enumerate(
                _bed_base_candidates(
                    joint_x,
                    joint_y,
                    route.joint_layer or 0,
                    config,
                    support,
                )
            ):
                if (base_x, base_y) == (top_x, top_y):
                    continue
                candidate = replace(route, base_x=base_x, base_y=base_y)
                if not _route_collides(
                    occupied_layers,
                    config.resolution_x,
                    config.resolution_y,
                    candidate,
                    body_collision_radius,
                ):
                    return _with_tree_clearance(
                        candidate, occupied_layers, config, support
                    )

        if support.enforcers_enabled:
            enforcer = _find_enforcer_route(
                occupied_layers,
                config,
                support,
                top_x,
                top_y,
                top_layer,
                route_normal,
                collision_radius,
                role,
                search_nearby=role == "manual",
            )
            if enforcer is not None:
                return _with_tree_clearance(
                    enforcer, occupied_layers, config, support
                )
        if support.branching_enabled and branch_anchors:
            branch = _find_branch_route(
                occupied_layers,
                config,
                support,
                top_x,
                top_y,
                top_layer,
                route_normal,
                collision_radius,
                role,
                branch_anchors,
            )
            if branch is not None:
                return branch
    return None


def _find_branch_route(
    occupied_layers: list[_OccupiedLayer],
    config: PrintConfig,
    support: SupportConfig,
    top_x: int,
    top_y: int,
    top_layer: int,
    normal: tuple[float, float, float],
    collision_radius: int,
    role: str,
    branch_anchors: tuple[SupportAnchor, ...],
    *,
    maximum_reach_mm: float | None = None,
) -> SupportAnchor | None:
    """Join a nearby contact to a safe support shaft.

    The existing shaft is sampled at deterministic heights.  A branch is only
    accepted when it stays inside the configured reach/angle limits and the
    complete branch (apart from its intentional tip contact) clears the model.
    Because candidates only connect to previously accepted anchors, the result
    is an acyclic tree rooted in a bed or model enforcer support.  Proactive
    calls use tree_merge_distance_mm; the blocked-route fallback may search the
    wider max_base_reach_mm envelope.
    """

    min_drop_layers = max(
        1, int(ceil(support.enforcer_min_drop_mm / config.layer_height_mm))
    )
    max_drop_layers = max(
        min_drop_layers,
        int(ceil(support.max_base_reach_mm / config.layer_height_mm)),
    )
    sample_step = max(1, int(round(0.5 / config.layer_height_mm)))
    # Tree branches use the same bounded reach contract as bed routes.  The
    # shaft-angle limit still constrains horizontal span at every candidate
    # height, so a long branch cannot become an unsafe near-horizontal beam.
    maximum_reach_mm = (
        support.max_base_reach_mm
        if maximum_reach_mm is None
        else min(support.max_base_reach_mm, maximum_reach_mm)
    )
    candidates: list[tuple[float, float, int, int, int, int]] = []
    tree_tip_counts = _tree_tip_counts(branch_anchors)
    for parent_index, anchor in enumerate(branch_anchors):
        tree_id = anchor.tree_id if anchor.tree_id >= 0 else anchor.anchor_id
        if tree_id >= 0 and tree_tip_counts.get(tree_id, 0) >= support.tree_max_tips:
            continue
        # A post only becomes a load-bearing trunk when a child joins it. Keep
        # narrow isolated routes possible, but prove the full parent path has
        # room for its configured maximum radius before thickening it.
        if not anchor.tree_clearance_ok:
            # Legacy/external anchors without IDs predate cached clearance and
            # get one conservative check. Registered planner anchors cache this
            # result when first routed, avoiding an O(parents × layers) rescan.
            if anchor.anchor_id >= 0 or not _with_tree_clearance(
                anchor, occupied_layers, config, support
            ).tree_clearance_ok:
                continue
        if anchor.kind == "bed" and anchor.base_layer == 0:
            junction_clearance_layers = max(
                2, int(ceil(support.pedestal_height_mm / config.layer_height_mm))
            )
        else:
            junction_clearance_layers = max(
                2,
                int(
                    ceil(
                        max(0.5, support.post_radius_mm * 2.0)
                        / config.layer_height_mm
                    )
                ),
            )
        first_layer = max(
            anchor.base_layer + junction_clearance_layers,
            top_layer - max_drop_layers,
        )
        # Merge into the load-bearing shaft/bridge, never into the parent's
        # tapered pinhead. A junction on a removable contact neck is a classic
        # weak point and does not match resin-slicer support geometry.
        last_layer = min(_joint_layer(anchor), top_layer - min_drop_layers)
        if first_layer > last_layer:
            continue
        layer = last_layer
        while layer >= first_layer:
            base_x, base_y = _support_center(anchor, layer)
            dx_mm = (top_x - base_x) * config.pixel_size_x_mm
            dy_mm = (top_y - base_y) * config.pixel_size_y_mm
            horizontal_mm = sqrt(dx_mm * dx_mm + dy_mm * dy_mm)
            drop_mm = (top_layer - layer) * config.layer_height_mm
            angle_reach_mm = drop_mm * tan(radians(support.max_support_angle_deg))
            if horizontal_mm <= min(maximum_reach_mm, angle_reach_mm) + 1e-9:
                # Prefer a nearby shaft, then the shortest viable branch.
                score = horizontal_mm + 0.02 * drop_mm
                candidates.append(
                    (score, horizontal_mm, layer, base_x, base_y, parent_index)
                )
            layer -= sample_step

    for _, _, base_layer, base_x, base_y, parent_index in sorted(candidates)[:96]:
        parent = branch_anchors[parent_index]
        route = _build_route(
            config,
            support,
            top_x,
            top_y,
            top_layer,
            normal,
            top_x,
            top_y,
            base_layer,
            "branch",
            role,
        )
        candidate = replace(
            route,
            base_x=base_x,
            base_y=base_y,
            parent_id=parent.anchor_id,
            tree_id=(
                parent.tree_id if parent.tree_id >= 0 else parent.anchor_id
            ),
        )
        if _support_angle_deg(candidate, config) > support.max_support_angle_deg + 1e-6:
            continue
        if not _route_collides(
            occupied_layers,
            config.resolution_x,
            config.resolution_y,
            candidate,
            collision_radius,
        ):
            return _with_tree_clearance(
                candidate, occupied_layers, config, support
            )
    return None


def _find_dogleg_route(
    occupied_layers: list[_OccupiedLayer],
    config: PrintConfig,
    support: SupportConfig,
    top_x: int,
    top_y: int,
    top_layer: int,
    body_collision_radius: int,
    collision_radius: int,
    role: str,
) -> tuple[SupportAnchor, SupportAnchor] | None:
    """Route a manual contact through a new intermediate tree trunk.

    A single sloped shaft cannot pass around every body.  This bounded search
    creates a clear vertical trunk beside the obstruction and joins the contact
    to its top with a collision-checked branch.  Both segments obey the normal
    maximum shaft angle; no collision clearance is relaxed.
    """

    if top_layer <= 1:
        return None
    layer_height = config.layer_height_mm
    maximum_drop_mm = min(
        top_layer * layer_height,
        support.max_base_reach_mm,
    )
    drop_candidates = (2.0, 3.0, 4.5, 6.5, 9.0, 12.0, 16.0, 22.0, 30.0, 40.0)
    radius_candidates = (0.8, 1.2, 1.8, 2.6, 3.8, 5.5, 8.0, 11.5, 16.0, 20.0)
    maximum_radius_mm = min(20.0, support.max_base_reach_mm)

    for drop_mm in drop_candidates:
        if drop_mm > maximum_drop_mm + 1e-9:
            continue
        waypoint_layer = max(1, top_layer - int(round(drop_mm / layer_height)))
        actual_drop_mm = (top_layer - waypoint_layer) * layer_height
        allowed_radius_mm = min(
            maximum_radius_mm,
            actual_drop_mm * tan(radians(support.max_support_angle_deg)),
        )
        for radius_index, radius_mm in enumerate(radius_candidates):
            if radius_mm > allowed_radius_mm + 1e-9:
                continue
            # Alternate the angular phase between rings so neighbouring samples
            # do not line up behind the same thin obstruction.
            phase = (radius_index % 2) * pi / 24.0
            for angle_index in range(24):
                angle = phase + 2.0 * pi * angle_index / 24.0
                waypoint_x = int(
                    round(top_x + cos(angle) * radius_mm / config.pixel_size_x_mm)
                )
                waypoint_y = int(
                    round(top_y + sin(angle) * radius_mm / config.pixel_size_y_mm)
                )
                if not (
                    0 <= waypoint_x < config.resolution_x
                    and 0 <= waypoint_y < config.resolution_y
                ):
                    continue

                branch = _build_route(
                    config,
                    support,
                    top_x,
                    top_y,
                    top_layer,
                    (0.0, 0.0, -1.0),
                    top_x,
                    top_y,
                    waypoint_layer,
                    "branch",
                    role,
                )
                branch = replace(
                    branch,
                    base_x=waypoint_x,
                    base_y=waypoint_y,
                )
                if _support_angle_deg(branch, config) > support.max_support_angle_deg + 1e-6:
                    continue
                if _route_collides(
                    occupied_layers,
                    config.resolution_x,
                    config.resolution_y,
                    branch,
                    collision_radius,
                ):
                    continue

                trunk = SupportAnchor(
                    x=waypoint_x,
                    y=waypoint_y,
                    top_layer=waypoint_layer,
                    base_x=waypoint_x,
                    base_y=waypoint_y,
                    base_layer=0,
                    joint_x=waypoint_x,
                    joint_y=waypoint_y,
                    joint_layer=waypoint_layer,
                    tip_type="cylinder",
                    kind="bed",
                    role="manual-trunk",
                    normal_x=0.0,
                    normal_y=0.0,
                    normal_z=-1.0,
                )
                if _bed_base_fits(waypoint_x, waypoint_y, config, support) and not _route_collides(
                    occupied_layers,
                    config.resolution_x,
                    config.resolution_y,
                    trunk,
                    body_collision_radius,
                ):
                    return trunk, branch

                # The branch waypoint may be clear while a vertical trunk below
                # it is blocked by the body. Try a bounded set of sloped bed
                # bases, using the same base-reach and shaft-angle contract as a
                # normal support. This adds the second bend needed to skirt a
                # wide obstruction without relaxing clearance.
                for base_index, (base_x, base_y) in enumerate(
                    _bed_base_candidates(
                        waypoint_x,
                        waypoint_y,
                        waypoint_layer,
                        config,
                        support,
                    )
                ):
                    if (base_x, base_y) == (waypoint_x, waypoint_y):
                        continue
                    if base_index > 96:
                        break
                    candidate_trunk = replace(trunk, base_x=base_x, base_y=base_y)
                    if _support_angle_deg(candidate_trunk, config) > support.max_support_angle_deg + 1e-6:
                        continue
                    if not _route_collides(
                        occupied_layers,
                        config.resolution_x,
                        config.resolution_y,
                        candidate_trunk,
                        body_collision_radius,
                    ):
                        return candidate_trunk, branch
    return None


def _find_nearby_manual_contact_route(
    current: LayerRaster,
    previous: LayerRaster,
    occupied_layers: list[_OccupiedLayer],
    config: PrintConfig,
    support: SupportConfig,
    top_x: int,
    top_y: int,
    top_layer: int,
    surface_normals: _SurfaceNormalSampler,
    body_collision_radius: int,
    collision_radius: int,
    role: str,
    branch_anchors: tuple[SupportAnchor, ...],
) -> tuple[tuple[SupportAnchor, ...], tuple[float, float, float]] | None:
    """Move a blocked detector contact within its connected unsupported patch.

    The search is limited to 0.5 mm, stays on lit pixels connected to the
    requested pixel, and requires no exact prior-layer exposure at the selected
    pixel. The actual selected coordinate is returned for the slice receipt.
    """

    maximum_mm = max(0.5, support.tip_radius_mm * 3.0)
    radius_px = max(
        1,
        int(ceil(maximum_mm / min(config.pixel_size_x_mm, config.pixel_size_y_mm))),
    )
    x0 = max(0, top_x - radius_px)
    x1 = min(current.width - 1, top_x + radius_px)
    y0 = max(0, top_y - radius_px)
    y1 = min(current.height - 1, top_y + radius_px)

    start = (top_x, top_y)
    if not _occupied_at(current, top_x, top_y):
        lit = [
            (x, y)
            for y in range(y0, y1 + 1)
            for x in range(x0, x1 + 1)
            if _occupied_at(current, x, y)
        ]
        if not lit:
            return None
        start = min(lit, key=lambda point: (point[0] - top_x) ** 2 + (point[1] - top_y) ** 2)

    queue: deque[tuple[int, int]] = deque((start,))
    seen = {start}
    candidates: list[tuple[float, int, int]] = []
    while queue:
        x, y = queue.popleft()
        dx_mm = (x - top_x) * config.pixel_size_x_mm
        dy_mm = (y - top_y) * config.pixel_size_y_mm
        distance2_mm = dx_mm * dx_mm + dy_mm * dy_mm
        if (
            (x, y) != (top_x, top_y)
            and distance2_mm <= maximum_mm * maximum_mm + 1e-9
            and not _occupied_at(previous, x, y)
        ):
            candidates.append((distance2_mm, x, y))
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if (nx, ny) in seen or nx < x0 or nx > x1 or ny < y0 or ny > y1:
                continue
            ndx_mm = (nx - top_x) * config.pixel_size_x_mm
            ndy_mm = (ny - top_y) * config.pixel_size_y_mm
            if ndx_mm * ndx_mm + ndy_mm * ndy_mm > maximum_mm * maximum_mm + 1e-9:
                continue
            seen.add((nx, ny))
            if _occupied_at(current, nx, ny):
                queue.append((nx, ny))

    # Try the far side of the same small patch first; it is most likely to clear
    # the obstruction that blocked the requested edge pixel.
    candidates.sort(key=lambda item: (-item[0], item[2], item[1]))
    candidates = candidates[:32]
    for _distance2, x, y in candidates:
        actual_contact = (
            (x + 0.5) * config.pixel_size_x_mm,
            (y + 0.5) * config.pixel_size_y_mm,
            (top_layer + 0.5) * config.layer_height_mm,
        )
        fallback = _estimate_surface_normal(current, previous, config, x, y)
        normal = surface_normals.normal_at_contact(actual_contact, fallback)
        route = _find_support_route(
            occupied_layers,
            config,
            support,
            x,
            y,
            top_layer,
            normal,
            body_collision_radius,
            collision_radius,
            role,
            branch_anchors=branch_anchors,
        )
        if route is not None:
            return (route,), actual_contact

    for _distance2, x, y in candidates[:8]:
        dogleg = _find_dogleg_route(
            occupied_layers,
            config,
            support,
            x,
            y,
            top_layer,
            body_collision_radius,
            collision_radius,
            role,
        )
        if dogleg is not None:
            return dogleg, (
                (x + 0.5) * config.pixel_size_x_mm,
                (y + 0.5) * config.pixel_size_y_mm,
                (top_layer + 0.5) * config.layer_height_mm,
            )
    return None


def _find_enforcer_route(
    occupied_layers: list[_OccupiedLayer],
    config: PrintConfig,
    support: SupportConfig,
    top_x: int,
    top_y: int,
    top_layer: int,
    normal: tuple[float, float, float],
    collision_radius: int,
    role: str,
    search_nearby: bool = True,
) -> SupportAnchor | None:
    min_drop_layers = max(1, int(ceil(support.enforcer_min_drop_mm / config.layer_height_mm)))
    if not search_nearby:
        max_drop_layers = max(
            min_drop_layers,
            int(ceil(support.enforcer_reach_mm / config.layer_height_mm)),
        )
        min_base_layer = max(0, top_layer - max_drop_layers)
        for occupied in reversed(occupied_layers):
            if occupied.layer_index >= top_layer - min_drop_layers:
                continue
            if occupied.layer_index < min_base_layer:
                break
            route = _build_route(
                config,
                support,
                top_x,
                top_y,
                top_layer,
                normal,
                top_x,
                top_y,
                occupied.layer_index,
                "enforcer",
                role,
            )
            base_x, base_y = _base_xy(route)
            if not _point_occupied(occupied, base_x, base_y):
                continue
            if not _route_collides(
                occupied_layers,
                config.resolution_x,
                config.resolution_y,
                route,
                collision_radius,
            ):
                return route
        return None

    max_drop_layers = max(
        min_drop_layers,
        int(ceil(support.max_base_reach_mm / config.layer_height_mm)),
    )
    min_base_layer = max(0, top_layer - max_drop_layers)
    check_step = max(1, int(round(0.25 / config.layer_height_mm)))
    next_check = top_layer - min_drop_layers
    for occupied in reversed(occupied_layers):
        if occupied.layer_index >= top_layer - min_drop_layers:
            continue
        if occupied.layer_index < min_base_layer:
            break
        if occupied.layer_index > next_check:
            continue
        next_check = occupied.layer_index - check_step
        drop_mm = (top_layer - occupied.layer_index) * config.layer_height_mm
        angle_reach_mm = drop_mm * tan(radians(support.max_support_angle_deg))
        horizontal_reach_mm = min(support.enforcer_reach_mm, angle_reach_mm)
        reach_px = max(
            0,
            int(floor(horizontal_reach_mm / min(config.pixel_size_x_mm, config.pixel_size_y_mm))),
        )
        base_points = _nearest_occupied_points(
            occupied,
            top_x,
            top_y,
            reach_px,
            limit=12,
        )
        if not base_points:
            continue
        route = _build_route(
            config,
            support,
            top_x,
            top_y,
            top_layer,
            normal,
            top_x,
            top_y,
            occupied.layer_index,
            "enforcer",
            role,
        )
        for base_x, base_y in base_points:
            candidate = replace(route, base_x=base_x, base_y=base_y)
            if _support_angle_deg(candidate, config) > support.max_support_angle_deg + 1e-6:
                continue
            if not _route_collides(
                occupied_layers,
                config.resolution_x,
                config.resolution_y,
                candidate,
                collision_radius,
            ):
                return candidate
    return None


def _bed_base_fits(x: int, y: int, config: PrintConfig, support: SupportConfig) -> bool:
    """Reserve the full foot/raft footprint plus one dark boundary pixel.

    Checking only a shaft centre allows feet and the expanded raft to be
    silently clipped by the raster. Use a conservative analysis-grid radius;
    output-grid rounding must not consume the reserved border.
    """
    radius_mm = max(support.foot_radius_mm, support.post_radius_mm,
                    support.tree_trunk_radius_mm if support.branching_enabled else 0.0)
    if support.bed_interface in {"raft", "skate"}:
        radius_mm += support.raft_margin_mm
    radius_px = int(ceil(radius_mm / min(config.pixel_size_x_mm,
                                       config.pixel_size_y_mm))) + 2
    return (radius_px <= x < config.resolution_x-radius_px
            and radius_px <= y < config.resolution_y-radius_px)


def _bed_base_candidates(
    joint_x: int,
    joint_y: int,
    joint_layer: int,
    config: PrintConfig,
    support: SupportConfig,
):
    if _bed_base_fits(joint_x, joint_y, config, support):
        yield joint_x, joint_y
    height_mm = max(config.layer_height_mm, joint_layer * config.layer_height_mm)
    angle_reach = height_mm * tan(radians(support.max_support_angle_deg))
    maximum_mm = min(support.max_base_reach_mm, angle_reach)
    if maximum_mm <= 0:
        return
    step_mm = max(0.6, 2.0 * (support.post_radius_mm + support.collision_clearance_mm))
    radius_mm = step_mm
    while radius_mm <= maximum_mm + 1e-9:
        for index in range(24):
            angle = 2.0 * pi * index / 24.0
            x = int(round(joint_x + cos(angle) * radius_mm / config.pixel_size_x_mm))
            y = int(round(joint_y + sin(angle) * radius_mm / config.pixel_size_y_mm))
            if _bed_base_fits(x, y, config, support):
                yield x, y
        radius_mm = max(radius_mm + step_mm, radius_mm * 1.5)


def _nearest_occupied_points(
    occupied: _OccupiedLayer,
    x: int,
    y: int,
    radius: int,
    *,
    limit: int,
) -> list[tuple[int, int]]:
    radius2 = radius * radius
    points: list[tuple[int, int, int]] = []
    for yy in range(
        max(occupied.min_y, y - radius),
        min(occupied.max_y, y + radius) + 1,
    ):
        dy = yy - y
        max_dx = int(floor(sqrt(max(0, radius2 - dy * dy))))
        start = max(occupied.min_x, x - max_dx)
        end = min(occupied.max_x, x + max_dx)
        for span_start, span_end in occupied.rows.get(yy, ()):
            if span_end <= start or span_start > end:
                continue
            xx = min(max(x, span_start, start), span_end - 1, end)
            points.append(((xx - x) ** 2 + dy * dy, xx, yy))
    points.sort()
    return [(xx, yy) for _, xx, yy in points[:limit]]


def _point_occupied(occupied: _OccupiedLayer, x: int, y: int) -> bool:
    return any(start <= x < end for start, end in occupied.rows.get(y, ()))


def _build_route(
    config: PrintConfig,
    support: SupportConfig,
    top_x: int,
    top_y: int,
    top_layer: int,
    normal: tuple[float, float, float],
    base_x: int,
    base_y: int,
    base_layer: int,
    kind: str,
    role: str,
) -> SupportAnchor:
    span_layers = max(1, top_layer - base_layer)
    direction = _tip_direction_from_normal(normal)
    vertical_drop_mm = max(config.layer_height_mm, abs(direction[2]) * support.tip_length_mm)
    tip_layers = min(span_layers, max(1, int(ceil(vertical_drop_mm / config.layer_height_mm))))
    joint_layer = max(base_layer, top_layer - tip_layers)
    drop_mm = max(config.layer_height_mm, (top_layer - joint_layer) * config.layer_height_mm)
    travel_mm = drop_mm / max(0.05, -direction[2])
    joint_x = int(round(top_x + direction[0] * travel_mm / config.pixel_size_x_mm))
    joint_y = int(round(top_y + direction[1] * travel_mm / config.pixel_size_y_mm))

    return SupportAnchor(
        x=top_x,
        y=top_y,
        top_layer=top_layer,
        base_x=joint_x,
        base_y=joint_y,
        base_layer=base_layer,
        joint_x=joint_x,
        joint_y=joint_y,
        joint_layer=joint_layer,
        base_tip_layers=tip_layers if kind == "enforcer" else 0,
        tip_type=support.tip_type,
        kind=kind,
        role=role,
        normal_x=normal[0],
        normal_y=normal[1],
        normal_z=normal[2],
    )


def _route_collides(
    occupied_layers: list[_OccupiedLayer],
    width: int,
    height: int,
    anchor: SupportAnchor,
    radius: int,
) -> bool:
    joint_layer = anchor.joint_layer if anchor.joint_layer is not None else max(0, anchor.top_layer - 1)
    for occupied in occupied_layers:
        if occupied.layer_index < anchor.base_layer:
            continue
        if occupied.layer_index > joint_layer:
            break
        if (
            anchor.kind == "enforcer"
            and occupied.layer_index <= anchor.base_layer + anchor.base_tip_layers
        ):
            continue
        x, y = _support_center(anchor, occupied.layer_index)
        if (
            x + radius < occupied.min_x
            or x - radius > occupied.max_x
            or y + radius < occupied.min_y
            or y - radius > occupied.max_y
        ):
            continue
        if _disk_collides_layer(occupied, x, y, radius):
            return True
    return False


def _occupied_layer(layer_index: int, layer: LayerRaster) -> _OccupiedLayer | None:
    min_x = layer.width
    min_y = layer.height
    max_x = -1
    max_y = -1
    rows: dict[int, list[tuple[int, int]]] = {}
    for start, end in layer.nonzero_spans():
        y = start // layer.width
        x0 = start % layer.width
        x1 = (end - 1) % layer.width
        rows.setdefault(y, []).append((x0, x1 + 1))
        min_x = min(min_x, x0)
        min_y = min(min_y, y)
        max_x = max(max_x, x1)
        max_y = max(max_y, y)
    if max_x < 0:
        return None
    return _OccupiedLayer(
        layer_index,
        layer.width,
        layer.height,
        {y: tuple(spans) for y, spans in rows.items()},
        min_x,
        min_y,
        max_x,
        max_y,
    )


def _disk_collides_layer(
    occupied: _OccupiedLayer,
    x: int,
    y: int,
    radius: int,
) -> bool:
    radius = max(0, radius)
    radius2 = radius * radius
    for yy in range(max(0, y - radius), min(occupied.height, y + radius + 1)):
        dy = yy - y
        dx = int(floor(sqrt(max(0, radius2 - dy * dy))))
        x0 = max(0, x - dx)
        x1 = min(occupied.width, x + dx + 1)
        for span_start, span_end in occupied.rows.get(yy, ()):
            if span_start < x1 and span_end > x0:
                return True
    return False


def _disk_collides(
    occupied: bytearray,
    width: int,
    height: int,
    x: int,
    y: int,
    radius: int,
) -> bool:
    radius = max(0, radius)
    radius2 = radius * radius
    for yy in range(max(0, y - radius), min(height, y + radius + 1)):
        dy = yy - y
        dx = int(floor(sqrt(max(0, radius2 - dy * dy))))
        x0 = max(0, x - dx)
        x1 = min(width, x + dx + 1)
        base = yy * width
        if occupied[base + x0 : base + x1].find(255) != -1:
            return True
    return False


def _support_angle_deg(anchor: SupportAnchor, config: PrintConfig) -> float:
    base_x, base_y = _base_xy(anchor)
    joint_x, joint_y = _joint_xy(anchor)
    base_layer = anchor.base_layer
    joint_layer = anchor.joint_layer if anchor.joint_layer is not None else max(base_layer, anchor.top_layer - 1)
    vertical_mm = max(config.layer_height_mm, (joint_layer - base_layer) * config.layer_height_mm)
    dx_mm = (joint_x - base_x) * config.pixel_size_x_mm
    dy_mm = (joint_y - base_y) * config.pixel_size_y_mm
    horizontal_mm = sqrt(dx_mm * dx_mm + dy_mm * dy_mm)
    return 180.0 / pi * atan(horizontal_mm / vertical_mm)


def _plan_braces(
    anchors: list[SupportAnchor],
    occupied_layers: list[_OccupiedLayer],
    analysis_config: PrintConfig,
    config: PrintConfig,
    support: SupportConfig,
    layer_count: int,
    radius_px: int,
    bed_interface_layers: int,
) -> tuple[SupportBrace, ...]:
    brace_anchors = [anchor for anchor in anchors if anchor.kind == "bed" and anchor.base_layer == 0]
    if not support.brace_enabled or len(brace_anchors) < 2:
        return ()

    if support.brace_height_mm < 0:
        return ()
    start_layer = max(bed_interface_layers, int(round(support.brace_height_mm / config.layer_height_mm)))
    if start_layer >= layer_count:
        return ()
    interval_layers = (
        max(1, int(round(support.brace_interval_mm / config.layer_height_mm)))
        if support.brace_interval_mm > 0
        else 0
    )
    max_distance_px = max(
        1,
        int(round(support.brace_max_distance_mm / min(config.pixel_size_x_mm, config.pixel_size_y_mm))),
    )
    min_distance_px = max(1, radius_px * 4)
    # Collision checks run against the analysis-resolution layers that were
    # already rendered for anchor planning; rendering (and caching) layers at
    # the full printer resolution here can eat gigabytes on large machines.
    analysis_pixel_mm = min(analysis_config.pixel_size_x_mm, analysis_config.pixel_size_y_mm)
    collision_radius_px = mm_to_px(support.brace_radius_mm + support.collision_clearance_mm, analysis_pixel_mm)
    occupied_by_index = {occupied.layer_index: occupied for occupied in occupied_layers}

    centers = [_support_center(anchor, min(start_layer, anchor.top_layer)) for anchor in brace_anchors]
    braces: list[SupportBrace] = []
    seen: set[tuple[int, int]] = set()
    for i, (x0, y0) in enumerate(centers):
        candidates: list[tuple[int, int]] = []
        for j in range(i + 1, len(centers)):
            x1, y1 = centers[j]
            dx = x1 - x0
            dy = y1 - y0
            d2 = dx * dx + dy * dy
            if min_distance_px * min_distance_px <= d2 <= max_distance_px * max_distance_px:
                candidates.append((d2, j))
        candidates.sort()
        for _d2, j in candidates[:2]:
            pair = (i, j)
            if pair in seen:
                continue
            seen.add(pair)
            # Tall supports get a brace rung at every interval level, not just
            # the first one; alternating the diagonal direction per level makes
            # the repeated rungs zig-zag for a stiffer truss.
            pair_top = min(_joint_layer(brace_anchors[i]), _joint_layer(brace_anchors[j]))
            level = 0
            level_start = start_layer
            while level_start <= pair_top:
                brace = _diagonal_brace_between(
                    brace_anchors[i],
                    brace_anchors[j],
                    level_start,
                    layer_count,
                    radius_px,
                    collision_radius_px,
                    occupied_by_index,
                    analysis_config,
                    config,
                    reverse=level % 2 == 1,
                    # Short supports cannot fit the 45-degree diagonal above the
                    # configured start height (it would overshoot their joints),
                    # so let the first rung slide down towards the bed instead
                    # of dropping the brace entirely.
                    min_start_layer=bed_interface_layers if level == 0 else None,
                )
                if brace is not None:
                    braces.append(brace)
                if interval_layers <= 0:
                    break
                level += 1
                level_start = start_layer + level * interval_layers
    return tuple(braces)


def _diagonal_brace_between(
    first: SupportAnchor,
    second: SupportAnchor,
    start_layer: int,
    layer_count: int,
    radius_px: int,
    collision_radius_px: int,
    occupied_by_index: dict[int, _OccupiedLayer],
    analysis_config: PrintConfig,
    config: PrintConfig,
    reverse: bool = False,
    min_start_layer: int | None = None,
) -> SupportBrace | None:
    ordered = ((second, first), (first, second)) if reverse else ((first, second), (second, first))
    for source, target in ordered:
        brace = _make_diagonal_brace(source, target, start_layer, layer_count, radius_px, config, min_start_layer)
        if brace is not None and not _brace_collides_with_model(
            brace, collision_radius_px, occupied_by_index, analysis_config, config
        ):
            return brace
    return None


def _make_diagonal_brace(
    source: SupportAnchor,
    target: SupportAnchor,
    start_layer: int,
    layer_count: int,
    radius_px: int,
    config: PrintConfig,
    min_start_layer: int | None = None,
) -> SupportBrace | None:
    source_joint_layer = _joint_layer(source)
    target_joint_layer = _joint_layer(target)

    x0, y0 = _support_center(source, min(start_layer, source_joint_layer))
    x1, y1 = _support_center(target, min(start_layer, target_joint_layer))
    dx_mm = (x1 - x0) * config.pixel_size_x_mm
    dy_mm = (y1 - y0) * config.pixel_size_y_mm
    horizontal_mm = sqrt(dx_mm * dx_mm + dy_mm * dy_mm)
    if horizontal_mm <= 0:
        return None
    span_layers = max(1, int(round(horizontal_mm / config.layer_height_mm)))
    end_layer = start_layer + span_layers
    limit = min(layer_count - 1, target_joint_layer)
    if end_layer > limit and min_start_layer is not None:
        # The 45-degree diagonal overshoots the pair's joints (typical for
        # lift-height supports on fine layer heights). Slide the rung down as
        # far as the bed interface allows, then flatten its angle to the
        # remaining height — but no shallower than a third of the 45-degree
        # rise (~18 degrees), so the printed stack of disks stays connected.
        start_layer = max(min_start_layer, limit - span_layers)
        available_layers = limit - start_layer
        min_span_layers = max(1, int(ceil(span_layers / 3)))
        if available_layers < min_span_layers:
            return None
        span_layers = min(span_layers, available_layers)
        end_layer = start_layer + span_layers
        # Re-anchor the ends on the (possibly slanted) posts at the new layers.
        x0, y0 = _support_center(source, min(start_layer, source_joint_layer))
        x1, y1 = _support_center(target, min(end_layer, target_joint_layer))
    if start_layer > source_joint_layer:
        return None
    if end_layer >= layer_count or end_layer > target_joint_layer:
        return None
    return SupportBrace(x0, y0, x1, y1, start_layer, end_layer, radius_px)


def _brace_collides_with_model(
    brace: SupportBrace,
    radius_px: int,
    occupied_by_index: dict[int, _OccupiedLayer],
    analysis_config: PrintConfig,
    config: PrintConfig,
) -> bool:
    """Check the brace path against the analysis-resolution model layers.

    Brace coordinates are in output pixels; the occupied layers were rendered
    at the (possibly downscaled) analysis resolution, so scale the centre into
    that space before testing. radius_px is already in analysis pixels."""
    scale_x = analysis_config.resolution_x / config.resolution_x
    scale_y = analysis_config.resolution_y / config.resolution_y
    for layer_index in range(max(0, brace.start_layer), brace.end_layer + 1):
        occupied = occupied_by_index.get(layer_index)
        if occupied is None:
            continue
        x, y = _brace_center(brace, layer_index)
        ax = min(max(0, int(round((x + 0.5) * scale_x - 0.5))), analysis_config.resolution_x - 1)
        ay = min(max(0, int(round((y + 0.5) * scale_y - 0.5))), analysis_config.resolution_y - 1)
        if (
            ax + radius_px < occupied.min_x
            or ax - radius_px > occupied.max_x
            or ay + radius_px < occupied.min_y
            or ay - radius_px > occupied.max_y
        ):
            continue
        if _disk_collides_layer(occupied, ax, ay, radius_px):
            return True
    return False


def _brace_center(brace: SupportBrace, layer_index: int) -> tuple[int, int]:
    if brace.end_layer <= brace.start_layer:
        return brace.x0, brace.y0
    t = (layer_index - brace.start_layer) / (brace.end_layer - brace.start_layer)
    t = min(1.0, max(0.0, t))
    return (
        int(round(brace.x0 + (brace.x1 - brace.x0) * t)),
        int(round(brace.y0 + (brace.y1 - brace.y0) * t)),
    )


def _joint_layer(anchor: SupportAnchor) -> int:
    return anchor.joint_layer if anchor.joint_layer is not None else max(anchor.base_layer, anchor.top_layer - 1)


def _clearance_px(clearance_mm: float, config: PrintConfig) -> int:
    if clearance_mm <= 0:
        return 0
    return int(ceil(clearance_mm / min(config.pixel_size_x_mm, config.pixel_size_y_mm)))


def _scale_anchor_to_output(
    anchor: SupportAnchor,
    analysis_config: PrintConfig,
    output_config: PrintConfig,
) -> SupportAnchor:
    x, y = _scale_point_to_output(anchor.x, anchor.y, analysis_config, output_config)
    base_x, base_y = _scale_point_to_output(*_base_xy(anchor), analysis_config, output_config)
    joint_x, joint_y = _scale_point_to_output(*_joint_xy(anchor), analysis_config, output_config)
    return SupportAnchor(
        x=x,
        y=y,
        top_layer=anchor.top_layer,
        base_x=base_x,
        base_y=base_y,
        base_layer=anchor.base_layer,
        joint_x=joint_x,
        joint_y=joint_y,
        joint_layer=anchor.joint_layer,
        base_tip_layers=anchor.base_tip_layers,
        tip_type=anchor.tip_type,
        kind=anchor.kind,
        role=anchor.role,
        anchor_id=anchor.anchor_id,
        parent_id=anchor.parent_id,
        tree_id=anchor.tree_id,
        load_count=anchor.load_count,
        tree_clearance_ok=anchor.tree_clearance_ok,
        normal_x=anchor.normal_x,
        normal_y=anchor.normal_y,
        normal_z=anchor.normal_z,
    )


def _scale_anchor_to_analysis(
    anchor: SupportAnchor,
    analysis_config: PrintConfig,
    output_config: PrintConfig,
) -> SupportAnchor:
    """Convert a previously accepted output-resolution route back to analysis
    coordinates so a later manual contact can safely branch from its shaft."""

    x, y = _scale_point_to_output(anchor.x, anchor.y, output_config, analysis_config)
    base_x, base_y = _scale_point_to_output(
        *_base_xy(anchor), output_config, analysis_config
    )
    joint_x, joint_y = _scale_point_to_output(
        *_joint_xy(anchor), output_config, analysis_config
    )
    return replace(
        anchor,
        x=x,
        y=y,
        base_x=base_x,
        base_y=base_y,
        joint_x=joint_x,
        joint_y=joint_y,
    )


def _scale_point_to_output(
    x: int,
    y: int,
    analysis_config: PrintConfig,
    output_config: PrintConfig,
) -> tuple[int, int]:
    sx = output_config.resolution_x / analysis_config.resolution_x
    sy = output_config.resolution_y / analysis_config.resolution_y
    out_x = int(round((x + 0.5) * sx - 0.5))
    out_y = int(round((y + 0.5) * sy - 0.5))
    return (
        min(max(0, out_x), output_config.resolution_x - 1),
        min(max(0, out_y), output_config.resolution_y - 1),
    )


def _support_center(anchor: SupportAnchor, layer_index: int) -> tuple[int, int]:
    base_x, base_y = _base_xy(anchor)
    joint_x, joint_y = _joint_xy(anchor)
    joint_layer = anchor.joint_layer if anchor.joint_layer is not None else max(0, anchor.top_layer - 1)

    if anchor.top_layer <= 0:
        return anchor.x, anchor.y
    if layer_index <= joint_layer:
        if joint_layer <= anchor.base_layer:
            return base_x, base_y
        t = (layer_index - anchor.base_layer) / max(1, joint_layer - anchor.base_layer)
        return (
            int(round(base_x + (joint_x - base_x) * t)),
            int(round(base_y + (joint_y - base_y) * t)),
        )

    tip_layers = max(1, anchor.top_layer - joint_layer)
    t = (layer_index - joint_layer) / tip_layers
    return (
        int(round(joint_x + (anchor.x - joint_x) * t)),
        int(round(joint_y + (anchor.y - joint_y) * t)),
    )


def _support_radius_at_layer(anchor: SupportAnchor, layer_index: int, plan: SupportPlan) -> int:
    joint_layer = anchor.joint_layer if anchor.joint_layer is not None else max(0, anchor.top_layer - 1)
    shaft_radius = _shaft_radius_px(anchor, plan)
    if anchor.kind == "enforcer" and layer_index <= anchor.base_layer + anchor.base_tip_layers:
        base_t = (layer_index - anchor.base_layer) / max(1, anchor.base_tip_layers)
        return max(
            1,
            int(round(_lerp(plan.tip_radius_px, shaft_radius, base_t))),
        )
    if (
        anchor.kind == "bed"
        and anchor.base_layer == 0
        and layer_index <= anchor.base_layer + plan.pedestal_height_layers
    ):
        pedestal_t = (layer_index - anchor.base_layer) / max(
            1, plan.pedestal_height_layers
        )
        return max(
            shaft_radius,
            int(round(_lerp(plan.foot_radius_px, shaft_radius, pedestal_t))),
        )
    if layer_index <= joint_layer or anchor.top_layer <= joint_layer:
        return shaft_radius

    t = (layer_index - joint_layer) / max(1, anchor.top_layer - joint_layer)
    if anchor.tip_type == "cylinder":
        return plan.tip_radius_px if layer_index == anchor.top_layer else shaft_radius
    if anchor.tip_type == "sphere":
        cone_radius = _lerp(shaft_radius, plan.tip_radius_px, t)
        bulb_radius = max(shaft_radius * 1.45, plan.tip_radius_px * 2.2)
        sphere_radius = plan.tip_radius_px + (bulb_radius - plan.tip_radius_px) * sin(pi * t)
        return max(1, int(round(max(cone_radius, sphere_radius))))
    return max(1, int(round(_lerp(shaft_radius, plan.tip_radius_px, t))))


def _shaft_radius_px(anchor: SupportAnchor, plan: SupportPlan) -> int:
    """Area-conserving radius for a branch carrying ``load_count`` tips.

    Radius grows with the square root of supported tip count, then is capped by
    the configured trunk radius.  This mirrors the structural intuition of a
    resin support tree without turning a large cluster into one massive column.
    """

    load = max(1, anchor.load_count)
    if load == 1 or plan.tree_trunk_radius_px <= plan.post_radius_px:
        return plan.post_radius_px
    return min(
        plan.tree_trunk_radius_px,
        max(plan.post_radius_px, int(ceil(plan.post_radius_px * sqrt(load)))),
    )


def _add_joint_sphere(layer: LayerRaster, layer_index: int, anchor: SupportAnchor, plan: SupportPlan) -> None:
    radius_mm = max(
        plan.joint_sphere_radius_mm,
        _shaft_radius_px(anchor, plan)
        * min(plan.pixel_size_x_mm, plan.pixel_size_y_mm),
    )
    if radius_mm <= 0:
        return
    joint_x, joint_y = _joint_xy(anchor)
    joint_layer = anchor.joint_layer if anchor.joint_layer is not None else max(0, anchor.top_layer - 1)
    dz_mm = (layer_index - joint_layer) * plan.layer_height_mm
    if abs(dz_mm) > radius_mm:
        return
    radius_at_layer_mm = sqrt(max(0.0, radius_mm * radius_mm - dz_mm * dz_mm))
    radius_px = max(1, int(round(radius_at_layer_mm / min(plan.pixel_size_x_mm, plan.pixel_size_y_mm))))
    layer.add_disk(joint_x, joint_y, radius_px, 255)


def _add_branch_base_joint(
    layer: LayerRaster,
    layer_index: int,
    anchor: SupportAnchor,
    plan: SupportPlan,
) -> None:
    if anchor.kind != "branch":
        return
    radius_mm = max(
        plan.joint_sphere_radius_mm,
        _shaft_radius_px(anchor, plan)
        * min(plan.pixel_size_x_mm, plan.pixel_size_y_mm),
    )
    dz_mm = (layer_index - anchor.base_layer) * plan.layer_height_mm
    if radius_mm <= 0 or dz_mm < 0 or dz_mm > radius_mm:
        return
    radius_at_layer_mm = sqrt(max(0.0, radius_mm * radius_mm - dz_mm * dz_mm))
    radius_px = max(
        1,
        int(
            round(
                radius_at_layer_mm
                / min(plan.pixel_size_x_mm, plan.pixel_size_y_mm)
            )
        ),
    )
    layer.add_disk(*_base_xy(anchor), radius_px, 255)


def _add_spherical_contact(layer: LayerRaster, layer_index: int, anchor: SupportAnchor, plan: SupportPlan) -> None:
    radius_mm = plan.contact_sphere_radius_mm
    if radius_mm <= 0:
        return
    inset_mm = min(max(0.0, plan.contact_sphere_inset_mm), radius_mm * 2.0)
    offset_mm = radius_mm - inset_mm
    center_x = anchor.x + anchor.normal_x * offset_mm / max(plan.pixel_size_x_mm, 1e-6)
    center_y = anchor.y + anchor.normal_y * offset_mm / max(plan.pixel_size_y_mm, 1e-6)
    center_layer = anchor.top_layer + anchor.normal_z * offset_mm / max(plan.layer_height_mm, 1e-6)
    dz_mm = (layer_index - center_layer) * plan.layer_height_mm
    if abs(dz_mm) > radius_mm:
        return
    radius_at_layer_mm = sqrt(max(0.0, radius_mm * radius_mm - dz_mm * dz_mm))
    radius_px = max(1, int(round(radius_at_layer_mm / min(plan.pixel_size_x_mm, plan.pixel_size_y_mm))))
    layer.add_disk(int(round(center_x)), int(round(center_y)), radius_px, 255)


def _bed_radius(plan: SupportPlan) -> int:
    if plan.bed_interface == "feet":
        return plan.foot_radius_px
    if plan.bed_interface == "skate":
        return max(plan.foot_radius_px, plan.raft_radius_px + plan.foot_radius_px // 2)
    return max(plan.foot_radius_px, plan.raft_radius_px)


def _base_xy(anchor: SupportAnchor) -> tuple[int, int]:
    return (
        anchor.base_x if anchor.base_x is not None else anchor.x,
        anchor.base_y if anchor.base_y is not None else anchor.y,
    )


def _joint_xy(anchor: SupportAnchor) -> tuple[int, int]:
    return (
        anchor.joint_x if anchor.joint_x is not None else anchor.x,
        anchor.joint_y if anchor.joint_y is not None else anchor.y,
    )


def _lerp(a: int, b: int, t: float) -> float:
    return a + (b - a) * t


def _anchor_candidates(
    component: Component,
    config: PrintConfig,
    support: SupportConfig,
) -> list[_AnchorCandidate]:
    primary_spacing_mm = support.support_spacing_mm
    primary_enabled = support.primary_supports_enabled and support.primary_max_extra_per_island > 0
    if primary_enabled:
        primary_spacing_mm = max(
            support.post_radius_mm * 3.0,
            support.support_spacing_mm / support.primary_density_multiplier,
        )

    candidates: list[_AnchorCandidate] = []

    def append_candidate(x: int, y: int, spacing_mm: float, role: str) -> None:
        x = min(max(0, int(x)), config.resolution_x - 1)
        y = min(max(0, int(y)), config.resolution_y - 1)
        for existing in candidates:
            if existing.x == x and existing.y == y:
                return
        candidates.append(_AnchorCandidate(x, y, spacing_mm, role))

    # Match mature SLA planners' size-aware behavior without relying on a
    # mathematical centroid that may sit in the empty middle of a concave
    # island. Small islands receive one real component pixel; larger islands
    # use deterministic farthest-point (Voronoi-style) coverage so thin ends,
    # perimeter extremes, and broad interiors are all represented.
    if primary_enabled:
        cx, cy = component.centroid(config.resolution_x)
        center_x = round(cx)
        center_y = round(cy)
        radius2_mm = support.primary_area_radius_mm * support.primary_area_radius_mm
        primary_pixels: list[int] = []
        for index in component.pixels:
            x = index % config.resolution_x
            y = index // config.resolution_x
            dx_mm = (x - center_x) * config.pixel_size_x_mm
            dy_mm = (y - center_y) * config.pixel_size_y_mm
            if dx_mm * dx_mm + dy_mm * dy_mm <= radius2_mm:
                primary_pixels.append(index)
        primary_component = _component_from_pixels(primary_pixels, config.resolution_x)
        if primary_component is not None:
            for index in _coverage_points(
                primary_component,
                config,
                primary_spacing_mm,
                support.primary_max_extra_per_island + 1,
            ):
                append_candidate(
                    index % config.resolution_x,
                    index // config.resolution_x,
                    primary_spacing_mm,
                    "primary",
                )

    for position, index in enumerate(
        _coverage_points(
            component,
            config,
            support.support_spacing_mm,
            support.max_supports_per_island,
        )
    ):
        append_candidate(
            index % config.resolution_x,
            index // config.resolution_x,
            support.support_spacing_mm,
            "secondary" if primary_enabled or position else "primary",
        )

    return candidates[: support.max_supports_per_island + support.primary_max_extra_per_island]


def _component_from_pixels(
    pixels: Sequence[int],
    width: int,
) -> Component | None:
    if not pixels:
        return None
    # Callers filter an existing connected component, so indices are already
    # unique. Preserve that order and compute bounds in one pass instead of
    # allocating a set, a sorted copy, and two full coordinate tuples. Large
    # unsupported regions can contain hundreds of thousands of pixels.
    ordered = tuple(int(index) for index in pixels)
    first_x = ordered[0] % width
    first_y = ordered[0] // width
    min_x = max_x = first_x
    min_y = max_y = first_y
    for index in ordered[1:]:
        x = index % width
        y = index // width
        min_x = min(min_x, x)
        max_x = max(max_x, x)
        min_y = min(min_y, y)
        max_y = max(max_y, y)
    return Component(ordered, min_x, min_y, max_x, max_y)


def _coverage_points(
    component: Component,
    config: PrintConfig,
    spacing_mm: float,
    limit: int,
) -> list[int]:
    """Return deterministic, real-pixel contacts with bounded max-min coverage.

    The candidate pool is capped independently of island area, keeping support
    generation predictable on high-resolution layers. Axis and diagonal
    extrema are always retained so subsampling cannot lose a thin end or sharp
    outer feature.
    """

    limit = max(1, int(limit))
    width = config.resolution_x
    cx, cy = component.centroid(width)
    center = min(
        component.pixels,
        key=lambda index: (
            ((index % width) - cx) ** 2 + ((index // width) - cy) ** 2,
            index,
        ),
    )
    span_x_mm = (component.max_x - component.min_x + 1) * config.pixel_size_x_mm
    span_y_mm = (component.max_y - component.min_y + 1) * config.pixel_size_y_mm
    area_mm2 = component.area_px * config.pixel_area_mm2
    desired = min(
        limit,
        max(
            1,
            int(ceil(area_mm2 / max(spacing_mm * spacing_mm, 1e-9))),
            int(ceil(span_x_mm / spacing_mm)),
            int(ceil(span_y_mm / spacing_mm)),
        ),
    )
    if desired == 1:
        return [center]

    pool = _component_candidate_pool(component, width)
    if center not in pool:
        pool.append(center)

    def distance2(first: int, second: int) -> float:
        dx_mm = ((first % width) - (second % width)) * config.pixel_size_x_mm
        dy_mm = ((first // width) - (second // width)) * config.pixel_size_y_mm
        return dx_mm * dx_mm + dy_mm * dy_mm

    first = max(pool, key=lambda index: (distance2(index, center), -index))
    second = max(pool, key=lambda index: (distance2(index, first), -index))
    selected = [first]
    if second != first:
        selected.append(second)
    minimum_distance2 = [
        min(distance2(index, chosen) for chosen in selected)
        for index in pool
    ]
    while len(selected) < desired:
        position = max(
            range(len(pool)),
            key=lambda item: (minimum_distance2[item], -pool[item]),
        )
        if minimum_distance2[position] <= 1e-12:
            break
        chosen = pool[position]
        selected.append(chosen)
        for item, index in enumerate(pool):
            minimum_distance2[item] = min(
                minimum_distance2[item],
                distance2(index, chosen),
            )
    return selected


def _component_candidate_pool(
    component: Component,
    width: int,
    maximum: int = 8192,
) -> list[int]:
    stride = max(1, int(ceil(len(component.pixels) / max(1, maximum))))
    pool = list(component.pixels[::stride])
    extrema = (
        min(component.pixels, key=lambda index: (index % width, index // width, index)),
        max(component.pixels, key=lambda index: (index % width, -(index // width), -index)),
        min(component.pixels, key=lambda index: (index // width, index % width, index)),
        max(component.pixels, key=lambda index: (index // width, -(index % width), -index)),
        min(component.pixels, key=lambda index: ((index % width) + (index // width), index)),
        max(component.pixels, key=lambda index: ((index % width) + (index // width), -index)),
        min(component.pixels, key=lambda index: ((index % width) - (index // width), index)),
        max(component.pixels, key=lambda index: ((index % width) - (index // width), -index)),
    )
    seen = set(pool)
    for index in extrema:
        if index not in seen:
            pool.append(index)
            seen.add(index)
    return pool


class _PlacedPointIndex:
    def __init__(self, config: PrintConfig) -> None:
        self.config = config
        self.cell_size_mm = max(1.0, min(config.size_x_mm, config.size_y_mm) / 48.0)
        self.cells: dict[tuple[int, int], list[tuple[int, int, int]]] = {}

    def add(self, x: int, y: int, top_layer: int = 0) -> None:
        self.cells.setdefault(self._key(x, y), []).append((x, y, top_layer))

    def too_close(self, x: int, y: int, spacing_mm: float, top_layer: int = 0) -> bool:
        min_distance2 = spacing_mm * spacing_mm
        radius = max(1, int(ceil(spacing_mm / self.cell_size_mm)))
        cell_x, cell_y = self._key(x, y)
        for ix in range(cell_x - radius, cell_x + radius + 1):
            for iy in range(cell_y - radius, cell_y + radius + 1):
                for other_x, other_y, other_layer in self.cells.get((ix, iy), ()):
                    dx_mm = (x - other_x) * self.config.pixel_size_x_mm
                    dy_mm = (y - other_y) * self.config.pixel_size_y_mm
                    dz_mm = (top_layer - other_layer) * self.config.layer_height_mm
                    if dx_mm * dx_mm + dy_mm * dy_mm + dz_mm * dz_mm < min_distance2:
                        return True
        return False

    def _key(self, x: int, y: int) -> tuple[int, int]:
        return (
            int(floor((x * self.config.pixel_size_x_mm) / self.cell_size_mm)),
            int(floor((y * self.config.pixel_size_y_mm) / self.cell_size_mm)),
        )


def _too_close_to_existing(
    points: list[tuple[int, int]],
    x: int,
    y: int,
    config: PrintConfig,
    spacing_mm: float,
) -> bool:
    min_distance2 = spacing_mm * spacing_mm
    for other_x, other_y in points:
        dx_mm = (x - other_x) * config.pixel_size_x_mm
        dy_mm = (y - other_y) * config.pixel_size_y_mm
        if dx_mm * dx_mm + dy_mm * dy_mm < min_distance2:
            return True
    return False
