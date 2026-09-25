"""Packaged Blender-side measured resin preparation.

This script is an implementation detail of Agent-Slicer.  Invoke it through
``agent_slicer.blender_adapter.prepare_resin_model`` so paths, reports, and
failure handling remain transactional and machine-readable.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

import bmesh
import bpy
from mathutils import Euler, Vector
from mathutils.bvhtree import BVHTree

sys.path.insert(0, str(Path(__file__).resolve().parent))
from topology_repair import repair_topology


REPORT: dict[str, object] = {
    "schema": "agent-slicer-blender-resin-preparation/v1",
    "status": "running",
    "stages": {},
}


def mesh_stats(obj: bpy.types.Object) -> dict[str, object]:
    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)
    volume = abs(float(bm.calc_volume(signed=True)))
    boundary = sum(1 for edge in bm.edges if edge.is_boundary)
    non_manifold = sum(1 for edge in bm.edges if not edge.is_manifold)
    non_manifold_vertices = sum(1 for vertex in bm.verts if not vertex.is_manifold)
    components = _component_count(bm)
    bounds = [obj.matrix_world @ vertex.co for vertex in mesh.vertices]
    minimum = [min(point[axis] for point in bounds) for axis in range(3)]
    maximum = [max(point[axis] for point in bounds) for axis in range(3)]
    bm.free()
    return {
        "vertices": len(mesh.vertices),
        "edges": len(mesh.edges),
        "faces": len(mesh.polygons),
        "boundary_edges": boundary,
        "non_manifold_edges": non_manifold,
        "non_manifold_vertices": non_manifold_vertices,
        "surface_components": components,
        "volume_mm3": volume,
        "bounds_min_mm": minimum,
        "bounds_max_mm": maximum,
        "dimensions_mm": [maximum[i] - minimum[i] for i in range(3)],
    }


def _component_count(bm: bmesh.types.BMesh) -> int:
    unseen = set(bm.verts)
    components = 0
    while unseen:
        components += 1
        seed = unseen.pop()
        stack = [seed]
        while stack:
            current = stack.pop()
            for edge in current.link_edges:
                other = edge.other_vert(current)
                if other in unseen:
                    unseen.remove(other)
                    stack.append(other)
    return components


def _component_vertex_counts(obj: bpy.types.Object) -> list[int]:
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    unseen = set(bm.verts)
    sizes: list[int] = []
    while unseen:
        seed = unseen.pop()
        size = 1
        stack = [seed]
        while stack:
            current = stack.pop()
            for edge in current.link_edges:
                other = edge.other_vert(current)
                if other in unseen:
                    unseen.remove(other)
                    stack.append(other)
                    size += 1
        sizes.append(size)
    bm.free()
    return sorted(sizes, reverse=True)


def _apply_modifier(obj: bpy.types.Object, name: str) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    result = bpy.ops.object.modifier_apply(modifier=name)
    if "FINISHED" not in result:
        raise RuntimeError(f"modifier failed: {name}")


def _sample_vectors(values, maximum: int = 20_000) -> list[Vector]:
    count = len(values)
    stride = max(1, math.ceil(count / maximum))
    return [values[index].co.copy() for index in range(0, count, stride)]


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
    return float(ordered[index])


def _verified_small_convex_solid(obj: bpy.types.Object, stats: dict[str, object]) -> bool:
    """Conservative exact-geometry fast path for small, closed convex CAD parts.

    Manifoldness alone is insufficient: also require one consistently wound
    outward shell, positive signed volume, nondegenerate faces, and every vertex
    inside every face half-space. Bound the quadratic test to small solids.
    Complex miniatures continue through measured voxel repair.
    """
    if (stats["boundary_edges"] or stats["non_manifold_edges"] or stats["surface_components"] != 1
            or not 4 <= int(stats["vertices"]) <= 256 or not 4 <= int(stats["faces"]) <= 256):
        return False
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bm.normal_update()
        if bm.calc_volume(signed=True) <= 0 or any(not edge.is_contiguous for edge in bm.edges):
            return False
        points = [vertex.co for vertex in bm.verts]
        if any(not math.isfinite(value) for point in points for value in point):
            return False
        for face in bm.faces:
            if face.calc_area() <= 1e-12:
                return False
            origin = face.verts[0].co
            if any((point - origin).dot(face.normal) > 1e-7 for point in points):
                return False
        return True
    finally:
        bm.free()


def repair_voxel_measured(
    obj: bpy.types.Object,
    *,
    voxel_mm: float,
    max_p95_deviation_mm: float,
    max_deviation_mm: float,
) -> dict[str, object]:
    before = mesh_stats(obj)
    if _verified_small_convex_solid(obj, before):
        return {
            "status": "passed", "method": "verified-convex-solid-passthrough",
            "before": before, "after": before, "lossy": False,
            "requested_voxel_mm": voxel_mm, "actual_voxel_mm": None, "attempts": [],
            "source_to_repaired_deviation": {"max_mm": 0.0, "p95_mm": 0.0,
                                               "method": "geometry unchanged; no remesh applied"},
        }
    samples = _sample_vectors(obj.data.vertices)
    repair_attempts: list[dict[str, object]] = []
    actual_voxel = voxel_mm
    after: dict[str, object] = before
    for multiplier in (1.0, 1.25, 1.5, 2.0):
        actual_voxel = voxel_mm * multiplier
        modifier = obj.modifiers.new(
            name=f"AgentSlicerMeasuredVoxelRepair{multiplier:g}",
            type="REMESH",
        )
        modifier.mode = "VOXEL"
        modifier.voxel_size = actual_voxel
        modifier.adaptivity = 0.0
        modifier.use_remove_disconnected = True
        modifier.threshold = 0.003
        _apply_modifier(obj, modifier.name)
        after = mesh_stats(obj)
        repair_attempts.append(
            {
                "voxel_mm": actual_voxel,
                "surface_components": after["surface_components"],
                "largest_component_vertices": _component_vertex_counts(obj)[:12],
            }
        )
        if 1 <= int(after["surface_components"]) <= 512:
            break
    partial = {
        "status": "checking",
        "method": "blender-adaptive-voxel-remesh-measured",
        "requested_voxel_mm": voxel_mm,
        "actual_voxel_mm": actual_voxel,
        "attempts": repair_attempts,
        "before": before,
        "after": after,
        "lossy": True,
    }
    REPORT["stages"]["repair"] = partial
    if after["boundary_edges"] or after["non_manifold_edges"]:
        partial["status"] = "failed"
        raise RuntimeError("measured voxel repair did not produce a manifold mesh")
    if int(after["surface_components"]) > 512:
        partial["status"] = "failed"
        raise RuntimeError(
            "measured voxel repair left more than 512 disconnected bodies"
        )

    polygons = [tuple(vertex for vertex in polygon.vertices) for polygon in obj.data.polygons]
    repaired_bvh = BVHTree.FromPolygons(
        [vertex.co.copy() for vertex in obj.data.vertices],
        polygons,
        all_triangles=False,
    )
    distances: list[float] = []
    for point in samples:
        nearest = repaired_bvh.find_nearest(point)
        if nearest is None:
            raise RuntimeError("repair deviation query failed")
        distances.append(float(nearest[3]))
    deviation = {
        "sample_count": len(distances),
        "mean_mm": statistics.fmean(distances) if distances else 0.0,
        "p95_mm": _percentile(distances, 0.95),
        "max_mm": max(distances, default=0.0),
        "limits_mm": {
            "p95": max_p95_deviation_mm,
            "max": max_deviation_mm,
        },
    }
    if deviation["p95_mm"] > max_p95_deviation_mm:
        partial["source_to_repaired_deviation"] = deviation
        partial["status"] = "failed"
        raise RuntimeError(
            f"repair p95 deviation {deviation['p95_mm']:.4f} mm exceeds "
            f"{max_p95_deviation_mm:.4f} mm"
        )
    if deviation["max_mm"] > max_deviation_mm:
        partial["source_to_repaired_deviation"] = deviation
        partial["status"] = "failed"
        raise RuntimeError(
            f"repair maximum deviation {deviation['max_mm']:.4f} mm exceeds "
            f"{max_deviation_mm:.4f} mm"
        )
    return {
        "status": "passed",
        "method": "blender-adaptive-voxel-remesh-measured",
        "requested_voxel_mm": voxel_mm,
        "actual_voxel_mm": actual_voxel,
        "attempts": repair_attempts,
        "before": before,
        "after": after,
        "source_to_repaired_deviation": deviation,
        "lossy": True,
    }


def orient_resin(obj: bpy.types.Object, max_tilt_deg: float) -> dict[str, object]:
    tilts = [0.0, 10.0, 15.0, min(20.0, max_tilt_deg)]
    tilts = sorted(set(value for value in tilts if value <= max_tilt_deg))
    candidates: list[tuple[float, float, float]] = [(0.0, 0.0, 0.0)]
    for tilt in tilts:
        if tilt == 0:
            continue
        candidates.extend(
            [
                (tilt, 0.0, 0.0),
                (-tilt, 0.0, 0.0),
                (0.0, tilt, 0.0),
                (0.0, -tilt, 0.0),
                (tilt, tilt * 0.5, 0.0),
                (tilt, -tilt * 0.5, 0.0),
                (-tilt, tilt * 0.5, 0.0),
                (-tilt, -tilt * 0.5, 0.0),
            ]
        )
    candidates += [(x, y, 90.0) for x, y, _ in candidates]

    vertex_samples = _sample_vectors(obj.data.vertices, maximum=12_000)
    polygon_count = len(obj.data.polygons)
    polygon_stride = max(1, math.ceil(polygon_count / 20_000))
    polygon_samples = [obj.data.polygons[i] for i in range(0, polygon_count, polygon_stride)]
    scored: list[dict[str, float]] = []
    for rx, ry, rz in candidates:
        matrix = Euler(tuple(math.radians(value) for value in (rx, ry, rz)), "XYZ").to_matrix()
        rotated = [matrix @ point for point in vertex_samples]
        minimum = [min(point[axis] for point in rotated) for axis in range(3)]
        maximum = [max(point[axis] for point in rotated) for axis in range(3)]
        dimensions = [maximum[i] - minimum[i] for i in range(3)]
        downward_area = 0.0
        horizontal_area = 0.0
        for polygon in polygon_samples:
            normal = matrix @ polygon.normal
            area = float(polygon.area) * polygon_stride
            downward_area += area * max(0.0, -normal.z)
            horizontal_area += area * abs(normal.z)
        # Prefer less downward-facing surface and smaller peel cross-section,
        # while avoiding extreme height growth and needless tilt.
        score = (
            downward_area
            + 0.18 * horizontal_area
            + 0.015 * dimensions[2] * dimensions[2]
            + 0.02 * (abs(rx) + abs(ry))
        )
        scored.append(
            {
                "rotate_x_deg": rx,
                "rotate_y_deg": ry,
                "rotate_z_deg": rz,
                "score": score,
                "estimated_downward_area_mm2": downward_area,
                "estimated_projected_area_mm2": horizontal_area,
                "estimated_height_mm": dimensions[2],
            }
        )
    selected = min(
        scored,
        key=lambda item: (
            item["score"],
            abs(item["rotate_x_deg"]) + abs(item["rotate_y_deg"]),
            item["rotate_z_deg"],
        ),
    )
    obj.rotation_euler = tuple(
        math.radians(selected[key])
        for key in ("rotate_x_deg", "rotate_y_deg", "rotate_z_deg")
    )
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
    z_min = min(vertex.co.z for vertex in obj.data.vertices)
    obj.location.z -= z_min
    bpy.ops.object.transform_apply(location=True, rotation=False, scale=False)
    return {
        "status": "passed",
        "method": "bounded-candidate-resin-heuristic-v1",
        "max_tilt_deg": max_tilt_deg,
        "selected": selected,
        "candidate_count": len(scored),
        "candidate_scores": sorted(scored, key=lambda item: item["score"])[:8],
        "after": mesh_stats(obj),
    }


def select_bottom_drain_points(
    obj: bpy.types.Object,
    *,
    count: int,
    diameter_mm: float,
) -> list[tuple[float, float]]:
    z_min = min(vertex.co.z for vertex in obj.data.vertices)
    candidates: list[tuple[float, float]] = []
    inner_points = _inner_surface_coordinates(obj)
    if inner_points is not None:
        inner_z_min = min(point.z for point in inner_points)
        candidates = [
            (point.x, point.y)
            for point in inner_points
            if point.z <= inner_z_min + 1.25
        ]
    for polygon in obj.data.polygons:
        if not candidates and polygon.center.z <= z_min + 2.5 and polygon.normal.z < -0.2:
            candidates.append((polygon.center.x, polygon.center.y))
    if len(candidates) < count:
        candidates = [
            (vertex.co.x, vertex.co.y)
            for vertex in obj.data.vertices
            if vertex.co.z <= z_min + 2.0
        ]
    if not candidates:
        raise RuntimeError("no bottom-region geometry found for drain placement")
    if count == 1:
        return [_centroid(candidates)]
    centers = [
        min(candidates, key=lambda point: (point[0], point[1])),
        max(candidates, key=lambda point: (point[0], point[1])),
    ]
    for _ in range(24):
        groups: list[list[tuple[float, float]]] = [[], []]
        for point in candidates:
            distances = [
                (point[0] - center[0]) ** 2 + (point[1] - center[1]) ** 2
                for center in centers
            ]
            groups[0 if distances[0] <= distances[1] else 1].append(point)
        updated = [
            _centroid(group) if group else centers[index]
            for index, group in enumerate(groups)
        ]
        if updated == centers:
            break
        centers = updated
    if math.dist(centers[0], centers[1]) < diameter_mm * 1.6:
        raise RuntimeError("bottom region is too small for two distinct drain holes")
    return sorted(centers)


def _inner_surface_coordinates(obj: bpy.types.Object) -> list[Vector] | None:
    record = _inner_surface_record(obj)
    return None if record is None else record["points"]


def _inner_surface_record(obj: bpy.types.Object) -> dict[str, object] | None:
    records = _component_surface_records(obj)
    if len(records) != 2:
        return None
    return min(records, key=lambda item: abs(float(item["signed_volume_mm3"])))


def _component_coordinate_sets(obj: bpy.types.Object) -> list[list[Vector]]:
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    unseen = set(bm.verts)
    components: list[list[Vector]] = []
    while unseen:
        seed = unseen.pop()
        points = [seed.co.copy()]
        stack = [seed]
        while stack:
            current = stack.pop()
            for edge in current.link_edges:
                other = edge.other_vert(current)
                if other in unseen:
                    unseen.remove(other)
                    stack.append(other)
                    points.append(other.co.copy())
        components.append(points)
    bm.free()
    return components


def _component_surface_records(obj: bpy.types.Object) -> list[dict[str, object]]:
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    bm.verts.index_update()
    unseen = set(bm.verts)
    component_ids = [-1] * len(bm.verts)
    points: list[list[Vector]] = []
    while unseen:
        component_id = len(points)
        seed = unseen.pop()
        component_ids[seed.index] = component_id
        component_points = [seed.co.copy()]
        stack = [seed]
        while stack:
            current = stack.pop()
            for edge in current.link_edges:
                other = edge.other_vert(current)
                if other in unseen:
                    unseen.remove(other)
                    component_ids[other.index] = component_id
                    stack.append(other)
                    component_points.append(other.co.copy())
        points.append(component_points)
    bm.free()
    signed_six = [0.0] * len(points)
    vertices = obj.data.vertices
    for polygon in obj.data.polygons:
        indices = tuple(polygon.vertices)
        if len(indices) < 3:
            continue
        component_id = component_ids[indices[0]]
        a = vertices[indices[0]].co
        for offset in range(1, len(indices) - 1):
            b = vertices[indices[offset]].co
            c = vertices[indices[offset + 1]].co
            signed_six[component_id] += float(a.dot(b.cross(c)))
    return [
        {
            "points": component_points,
            "signed_volume_mm3": signed_six[index] / 6.0,
            "vertex_count": len(component_points),
        }
        for index, component_points in enumerate(points)
    ]


def _centroid(points: list[tuple[float, float]]) -> tuple[float, float]:
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


def _retain_largest_component(
    obj: bpy.types.Object,
) -> tuple[bpy.types.Object, list[dict[str, object]]]:
    stats = mesh_stats(obj)
    if stats["surface_components"] == 1:
        return obj, [stats]
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.separate(type="LOOSE")
    bpy.ops.object.mode_set(mode="OBJECT")
    parts = [part for part in bpy.context.selected_objects if part.type == "MESH"]
    part_stats = [(part, mesh_stats(part)) for part in parts]
    main, _ = max(
        part_stats,
        key=lambda item: (float(item[1]["volume_mm3"]), int(item[1]["vertices"])),
    )
    for part, _ in part_stats:
        if part is not main:
            bpy.data.objects.remove(part, do_unlink=True)
    return main, [item[1] for item in part_stats]


def _apply_uniform_contracted_cavity(
    obj: bpy.types.Object,
    *,
    wall_mm: float,
    solid_before: dict[str, object],
) -> tuple[dict[str, object], dict[str, object], float, float]:
    inner = obj.copy()
    inner.data = obj.data.copy()
    inner.name = "AgentSlicerBoundedContractedCavity"
    bpy.context.collection.objects.link(inner)
    coordinates = [vertex.co.copy() for vertex in inner.data.vertices]
    minimum = Vector(tuple(min(point[axis] for point in coordinates) for axis in range(3)))
    maximum = Vector(tuple(max(point[axis] for point in coordinates) for axis in range(3)))
    dimensions = maximum - minimum
    cavity_scale = min(
        1.0 - (2.0 * wall_mm / dimensions[axis])
        for axis in range(3)
    )
    if not 0.05 < cavity_scale < 0.98:
        bpy.data.objects.remove(inner, do_unlink=True)
        raise RuntimeError("bounded contraction cannot create a useful cavity")
    center = (minimum + maximum) / 2.0
    for vertex in inner.data.vertices:
        vertex.co = center + (vertex.co - center) * cavity_scale
    inner.data.update()
    inner_stats = mesh_stats(inner)
    if inner_stats["boundary_edges"] or inner_stats["non_manifold_edges"]:
        bpy.data.objects.remove(inner, do_unlink=True)
        raise RuntimeError("bounded contracted cavity is non-manifold")
    if inner_stats["surface_components"] != 1:
        bpy.data.objects.remove(inner, do_unlink=True)
        raise RuntimeError("bounded contracted cavity is not connected")
    cavity_ratio = float(inner_stats["volume_mm3"]) / float(solid_before["volume_mm3"])
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    boolean = obj.modifiers.new(name="AgentSlicerContractedCavityBoolean", type="BOOLEAN")
    boolean.operation = "DIFFERENCE"
    boolean.solver = "EXACT"
    boolean.use_hole_tolerant = True
    boolean.object = inner
    _apply_modifier(obj, boolean.name)
    bpy.data.objects.remove(inner, do_unlink=True)
    return mesh_stats(obj), inner_stats, 1.0 - cavity_ratio, cavity_scale


def hollow_and_drain(
    obj: bpy.types.Object,
    *,
    wall_mm: float,
    drain_diameter_mm: float,
    drain_count: int,
) -> dict[str, object]:
    solid_before = mesh_stats(obj)
    if solid_before["boundary_edges"] or solid_before["non_manifold_edges"]:
        raise RuntimeError("hollowing input is not manifold")
    original_components = int(solid_before["surface_components"])
    if original_components > 1:
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.mesh.separate(type="LOOSE")
        bpy.ops.object.mode_set(mode="OBJECT")
        parts = [part for part in bpy.context.selected_objects if part.type == "MESH"]
        if len(parts) != original_components:
            raise RuntimeError(
                f"component separation mismatch: expected {original_components}, found {len(parts)}"
            )
        part_stats = [(part, mesh_stats(part)) for part in parts]
        main, main_stats = max(
            part_stats,
            key=lambda item: (float(item[1]["volume_mm3"]), int(item[1]["vertices"])),
        )
        try:
            result = hollow_and_drain(
                main,
                wall_mm=wall_mm,
                drain_diameter_mm=drain_diameter_mm,
                drain_count=drain_count,
            )
        except RuntimeError as exc:
            message = str(exc)
            cavity_unsuitable = (
                message.startswith("inward cavity offset")
                or message.startswith("hollowing produced implausible")
            )
            if not cavity_unsuitable:
                raise
            for candidate in list(bpy.data.objects):
                if candidate.name.startswith("AgentSlicerInnerCavity"):
                    bpy.data.objects.remove(candidate, do_unlink=True)
            bpy.ops.object.select_all(action="DESELECT")
            for part in parts:
                part.select_set(True)
            bpy.context.view_layer.objects.active = obj
            joined = bpy.ops.object.join()
            if "FINISHED" not in joined:
                raise RuntimeError("failed to rejoin selective-solid fallback") from exc
            combined = mesh_stats(obj)
            if combined["boundary_edges"] or combined["non_manifold_edges"]:
                raise RuntimeError("selective-solid fallback is non-manifold") from exc
            if combined["surface_components"] != original_components:
                raise RuntimeError("selective-solid fallback changed component topology") from exc
            result = {
                "status": "passed",
                "method": "component-aware-selective-solid",
                "component_aware": True,
                "input_surface_components": original_components,
                "hollowed_components": 0,
                "solid_secondary_components": original_components,
                "hollow_skipped": True,
                "hollow_skip_reason": message,
                "selected_component_before": main_stats,
                "final": combined,
                "cavity_connected_to_exterior": True,
                "sealed_surface_components": 0,
                "drain_holes": [],
                "limitations": [
                    "The model remained solid because a bounded connected cavity could not be constructed safely."
                ],
            }
            REPORT["stages"]["hollow_and_drain"] = result
            return result
        bpy.ops.object.select_all(action="DESELECT")
        for part in parts:
            part.select_set(True)
        bpy.context.view_layer.objects.active = obj
        joined = bpy.ops.object.join()
        if "FINISHED" not in joined:
            raise RuntimeError("failed to rejoin component-aware hollow result")
        combined = mesh_stats(obj)
        if combined["boundary_edges"] or combined["non_manifold_edges"]:
            raise RuntimeError("rejoined component-aware result is non-manifold")
        if combined["surface_components"] != original_components:
            raise RuntimeError(
                "rejoined result contains additional sealed cavity surfaces"
            )
        result.update(
            {
                "component_aware": True,
                "input_surface_components": original_components,
                "hollowed_components": 1,
                "solid_secondary_components": original_components - 1,
                "selected_component_before": main_stats,
                "final": combined,
                "sealed_surface_components": 0,
            }
        )
        REPORT["stages"]["hollow_and_drain"] = result
        return result
    if min(solid_before["dimensions_mm"]) <= 2.5 * wall_mm:
        raise RuntimeError("model is too thin for the requested hollow wall")
    outer_backup = obj.data.copy()

    inner = obj.copy()
    inner.data = obj.data.copy()
    inner.name = "AgentSlicerInnerCavity"
    bpy.context.collection.objects.link(inner)
    bpy.ops.object.select_all(action="DESELECT")
    inner.select_set(True)
    bpy.context.view_layer.objects.active = inner
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.transform.shrink_fatten(value=-wall_mm, use_even_offset=True)
    bpy.ops.object.mode_set(mode="OBJECT")
    cavity_voxel_mm = min(0.18, max(0.08, wall_mm / 10.0))
    pre_inner_stats = mesh_stats(inner)
    early_contained = all(
        float(pre_inner_stats["bounds_min_mm"][axis])
        >= float(solid_before["bounds_min_mm"][axis]) - 0.05
        and float(pre_inner_stats["bounds_max_mm"][axis])
        <= float(solid_before["bounds_max_mm"][axis]) + 0.05
        for axis in range(3)
    )
    if early_contained:
        cavity_remesh = inner.modifiers.new(
            name="AgentSlicerCavitySelfUnion",
            type="REMESH",
        )
        cavity_remesh.mode = "VOXEL"
        cavity_remesh.voxel_size = cavity_voxel_mm
        cavity_remesh.adaptivity = 0.0
        cavity_remesh.use_remove_disconnected = True
        cavity_remesh.threshold = 0.02
        _apply_modifier(inner, cavity_remesh.name)
        inner_stats = mesh_stats(inner)
        candidate_cavity_components: list[dict[str, object]] = [inner_stats]
        if inner_stats["surface_components"] != 1:
            inner, candidate_cavity_components = _retain_largest_component(inner)
            inner_stats = mesh_stats(inner)
    else:
        inner_stats = pre_inner_stats
        candidate_cavity_components = [pre_inner_stats]
    cavity_partial: dict[str, object] = {
        "status": "checking",
        "method": "inward-offset-candidate",
        "requested_wall_mm": wall_mm,
        "cavity_voxel_mm": cavity_voxel_mm,
        "solid_before": solid_before,
        "candidate_cavity_component_count": len(candidate_cavity_components),
        "pre_remesh_inner_contained_by_outer_bounds": early_contained,
        "inner_cavity": inner_stats,
        "drain_holes": [],
    }
    REPORT["stages"]["hollow_and_drain"] = cavity_partial
    cavity_ratio = float(inner_stats["volume_mm3"]) / float(solid_before["volume_mm3"])
    containment_tolerance = 0.05
    contained = all(
        float(inner_stats["bounds_min_mm"][axis])
        >= float(solid_before["bounds_min_mm"][axis]) - containment_tolerance
        and float(inner_stats["bounds_max_mm"][axis])
        <= float(solid_before["bounds_max_mm"][axis]) + containment_tolerance
        for axis in range(3)
    )
    cavity_partial["inner_contained_by_outer_bounds"] = contained
    inward_valid = bool(
        not inner_stats["boundary_edges"]
        and not inner_stats["non_manifold_edges"]
        and inner_stats["surface_components"] == 1
        and 0.02 < cavity_ratio < 0.95
        and contained
    )
    if inward_valid:
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        cavity_boolean = obj.modifiers.new(
            name="AgentSlicerCavityBoolean",
            type="BOOLEAN",
        )
        cavity_boolean.operation = "DIFFERENCE"
        cavity_boolean.solver = "EXACT"
        cavity_boolean.use_hole_tolerant = True
        cavity_boolean.object = inner
        _apply_modifier(obj, cavity_boolean.name)
        bpy.data.objects.remove(inner, do_unlink=True)
        after_hollow = mesh_stats(obj)
        material_ratio = 1.0 - cavity_ratio
        hollow_method = "inward-offset-voxel-cavity-with-exact-booleans"
    else:
        bpy.data.objects.remove(inner, do_unlink=True)
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        modifier = obj.modifiers.new(name="AgentSlicerUniformWallFallback", type="SOLIDIFY")
        modifier.solidify_mode = "EXTRUDE"
        modifier.thickness = wall_mm
        modifier.offset = -1.0
        modifier.use_even_offset = False
        modifier.use_quality_normals = True
        modifier.use_rim = True
        modifier.thickness_clamp = 1.0
        modifier.use_thickness_angle_clamp = True
        _apply_modifier(obj, modifier.name)
        after_hollow = mesh_stats(obj)
        material_ratio = float(after_hollow["volume_mm3"]) / float(
            solid_before["volume_mm3"]
        )
        hollow_method = "uniform-solidify-fallback"
    final_inner_record = (
        _inner_surface_record(obj)
        if not after_hollow["boundary_edges"]
        and not after_hollow["non_manifold_edges"]
        and after_hollow["surface_components"] == 2
        else None
    )

    def inner_bounds_and_containment(record):
        if record is None:
            return None, False
        points = record["points"]
        bounds = {
            "min": [min(point[axis] for point in points) for axis in range(3)],
            "max": [max(point[axis] for point in points) for axis in range(3)],
        }
        is_contained = all(
            float(bounds["min"][axis])
            >= float(solid_before["bounds_min_mm"][axis]) - 0.10
            and float(bounds["max"][axis])
            <= float(solid_before["bounds_max_mm"][axis]) + 0.10
            for axis in range(3)
        )
        return bounds, is_contained

    final_inner_bounds, final_inner_contained = inner_bounds_and_containment(
        final_inner_record
    )
    contraction_scale = None
    if hollow_method == "uniform-solidify-fallback" and (
        final_inner_record is None or not final_inner_contained
    ):
        previous = obj.data
        obj.data = outer_backup.copy()
        bpy.data.meshes.remove(previous)
        after_hollow, inner_stats, material_ratio, contraction_scale = (
            _apply_uniform_contracted_cavity(
                obj,
                wall_mm=wall_mm,
                solid_before=solid_before,
            )
        )
        hollow_method = "bounded-uniform-contraction-fallback"
        final_inner_record = _inner_surface_record(obj)
        final_inner_bounds, final_inner_contained = inner_bounds_and_containment(
            final_inner_record
        )
    if after_hollow["boundary_edges"] or after_hollow["non_manifold_edges"]:
        raise RuntimeError(f"{hollow_method} produced non-manifold geometry")
    if after_hollow["surface_components"] != 2:
        raise RuntimeError("hollowing did not produce exactly one connected internal cavity")
    if final_inner_record is None:
        raise RuntimeError("hollowing inner surface could not be classified")
    if not final_inner_contained:
        raise RuntimeError("hollowing inner surface extends outside the repaired outer shell")
    if not 0.02 < material_ratio < 0.92:
        raise RuntimeError(
            f"hollowing produced implausible material ratio {material_ratio:.4f}"
        )
    partial: dict[str, object] = {
        "status": "checking",
        "method": hollow_method,
        "requested_wall_mm": wall_mm,
        "cavity_voxel_mm": cavity_voxel_mm,
        "inner_cavity": inner_stats,
        "inner_contained_by_outer_bounds": contained,
        "final_inner_surface_bounds_mm": final_inner_bounds,
        "final_inner_surface_contained": final_inner_contained,
        "bounded_contraction_scale": contraction_scale,
        "candidate_cavity_component_count": len(candidate_cavity_components),
        "solid_before": solid_before,
        "after_hollow_before_drains": after_hollow,
        "drain_holes": [],
    }
    REPORT["stages"]["hollow_and_drain"] = partial

    points = select_bottom_drain_points(
        obj,
        count=drain_count,
        diameter_mm=drain_diameter_mm,
    )
    z_min = float(after_hollow["bounds_min_mm"][2])
    model_height = float(after_hollow["dimensions_mm"][2])
    inner_surface = _inner_surface_coordinates(obj)
    if inner_surface:
        inner_low_z = min(point.z for point in inner_surface)
        required_depth = inner_low_z - z_min + wall_mm * 3.0
        inner_bounds = {
            "min": [min(point[axis] for point in inner_surface) for axis in range(3)],
            "max": [max(point[axis] for point in inner_surface) for axis in range(3)],
        }
    else:
        inner_low_z = None
        inner_bounds = None
        required_depth = model_height * 0.45
    cutter_depth = min(
        model_height + 1.0,
        max(12.0, required_depth, wall_mm * 6.0),
    )
    partial["drain_plan"] = {
        "points_xy_mm": points,
        "outer_z_min_mm": z_min,
        "inner_low_z_mm": inner_low_z,
        "inner_bounds_mm": inner_bounds,
        "cutter_depth_mm": cutter_depth,
    }
    holes: list[dict[str, float | int]] = []
    for index, (x, y) in enumerate(points, start=1):
        before_cut = mesh_stats(obj)
        bpy.ops.mesh.primitive_cylinder_add(
            vertices=64,
            radius=drain_diameter_mm / 2.0,
            depth=cutter_depth,
            location=(x, y, z_min - 1.0 + cutter_depth / 2.0),
        )
        cutter = bpy.context.active_object
        cutter.name = f"AgentSlicerDrain{index}"
        boolean = obj.modifiers.new(name=f"AgentSlicerDrainBoolean{index}", type="BOOLEAN")
        boolean.operation = "DIFFERENCE"
        boolean.solver = "EXACT"
        boolean.use_hole_tolerant = True
        boolean.object = cutter
        _apply_modifier(obj, boolean.name)
        bpy.data.objects.remove(cutter, do_unlink=True)
        after_cut = mesh_stats(obj)
        removed = float(before_cut["volume_mm3"]) - float(after_cut["volume_mm3"])
        radius = drain_diameter_mm / 2.0
        wall_faces = sum(
            1
            for polygon in obj.data.polygons
            if abs(math.hypot(polygon.center.x - x, polygon.center.y - y) - radius)
            <= 0.22
            and z_min - 0.1 <= polygon.center.z <= z_min + wall_mm * 2.5
            and abs(polygon.normal.z) < 0.55
        )
        if wall_faces < 8:
            partial["status"] = "failed"
            raise RuntimeError(
                f"drain {index} has insufficient cylindrical wall evidence ({wall_faces} faces)"
            )
        if index == 1 and after_cut["surface_components"] != 1:
            partial["status"] = "failed"
            raise RuntimeError("first drain did not connect the cavity to the exterior")
        if after_cut["boundary_edges"] or after_cut["non_manifold_edges"]:
            partial["status"] = "failed"
            raise RuntimeError(f"drain {index} produced non-manifold geometry")
        holes.append(
            {
                "index": index,
                "x_mm": x,
                "y_mm": y,
                "z_entry_mm": z_min,
                "diameter_mm": drain_diameter_mm,
                "signed_volume_delta_diagnostic_mm3": removed,
                "cylindrical_wall_faces": wall_faces,
                "surface_components_after": int(after_cut["surface_components"]),
            }
        )
        partial["drain_holes"] = holes

    final = mesh_stats(obj)
    if final["boundary_edges"] or final["non_manifold_edges"]:
        partial["status"] = "failed"
        raise RuntimeError("drained hollow result is not manifold")
    if final["surface_components"] != 1:
        partial["status"] = "failed"
        raise RuntimeError(
            "sealed internal surface components remain after drainage"
        )
    result = {
        "status": "passed",
        "method": hollow_method,
        "requested_wall_mm": wall_mm,
        "drain_holes": holes,
        "solid_before": solid_before,
        "after_hollow_before_drains": after_hollow,
        "final": final,
        "material_volume_ratio_before_drains": material_ratio,
        "final_signed_volume_diagnostic_mm3": final["volume_mm3"],
        "cavity_connected_to_exterior": True,
        "sealed_surface_components": 0,
    }
    REPORT["stages"]["hollow_and_drain"] = result
    return result


def prepare(args: argparse.Namespace) -> dict[str, object]:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    imported = bpy.ops.wm.stl_import(filepath=str(Path(args.input)))
    if "FINISHED" not in imported:
        raise RuntimeError("STL import failed")
    meshes = [obj for obj in bpy.context.selected_objects if obj.type == "MESH"]
    if len(meshes) != 1:
        raise RuntimeError(f"expected one imported mesh, found {len(meshes)}")
    obj = meshes[0]
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)

    source = mesh_stats(obj)
    scale_factor = 1.0
    if args.target_height_mm is not None:
        source_height = float(source["dimensions_mm"][2])
        if source_height <= 0:
            raise RuntimeError("source has zero height")
        scale_factor = args.target_height_mm / source_height
        obj.scale = (scale_factor, scale_factor, scale_factor)
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    REPORT["source"] = str(Path(args.input))
    REPORT["output"] = str(Path(args.output))
    REPORT["source_stats"] = source
    REPORT["scale_factor"] = scale_factor
    REPORT["target_height_mm"] = args.target_height_mm

    if args.repair_method == "local-topology":
        repair = repair_topology(
            obj, mesh_stats=mesh_stats,
            max_p95_deviation_mm=args.max_p95_deviation_mm,
            max_deviation_mm=args.max_deviation_mm,
        )
    else:
        repair = repair_voxel_measured(
            obj, voxel_mm=args.voxel_mm,
            max_p95_deviation_mm=args.max_p95_deviation_mm,
            max_deviation_mm=args.max_deviation_mm,
        )
    REPORT["stages"]["repair"] = repair

    if not args.no_orient:
        orientation = orient_resin(obj, args.max_tilt_deg)
        REPORT["stages"]["orientation"] = orientation
    if not args.no_hollow:
        if args.no_drain:
            raise RuntimeError("production hollowing requires drainage")
        hollow = hollow_and_drain(
            obj,
            wall_mm=args.wall_mm,
            drain_diameter_mm=args.drain_diameter_mm,
            drain_count=args.drain_count,
        )
        REPORT["stages"]["hollow_and_drain"] = hollow

    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    exported = bpy.ops.wm.stl_export(
        filepath=str(output),
        export_selected_objects=True,
        ascii_format=False,
        apply_modifiers=True,
    )
    if "FINISHED" not in exported:
        raise RuntimeError("STL export failed")
    if args.blend:
        bpy.ops.wm.save_as_mainfile(filepath=str(Path(args.blend)))
    REPORT["final_stats"] = mesh_stats(obj)
    REPORT["status"] = "passed"
    capabilities = ["mesh.repair.measured", "mesh.repair.safe"]
    if not args.no_orient:
        capabilities.append("mesh.orient.resin")
    if not args.no_hollow:
        capabilities.extend(
            ["mesh.hollow", "mesh.drain.auto", "mesh.cavity.validate"]
        )
    REPORT["capabilities"] = capabilities
    REPORT["limitations"] = [
        "Local repair preserves surfaces outside bounded patches; global self-intersections are not certified.",
        "Explicit voxel reconstruction is lossy and is not ordinary mesh repair.",
        "Orientation is a deterministic bounded heuristic, not a global optimum.",
        "Drain connectivity is established topologically; physical flushing still requires post-print handling.",
    ]
    return REPORT


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--blend")
    parser.add_argument("--target-height-mm", type=float)
    parser.add_argument("--repair-method", choices=("local-topology", "measured-voxel"), default="local-topology")
    parser.add_argument("--voxel-mm", type=float, default=0.12)
    parser.add_argument("--max-p95-deviation-mm", type=float, default=0.01)
    parser.add_argument("--max-deviation-mm", type=float, default=0.05)
    parser.add_argument("--wall-mm", type=float, default=1.8)
    parser.add_argument("--drain-diameter-mm", type=float, default=3.5)
    parser.add_argument("--drain-count", type=int, default=2)
    parser.add_argument("--max-tilt-deg", type=float, default=20.0)
    parser.add_argument("--no-orient", action="store_true")
    parser.add_argument("--no-hollow", action="store_true")
    parser.add_argument("--no-drain", action="store_true")
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1 :])
    if args.voxel_mm <= 0 or args.wall_mm <= 0 or args.drain_diameter_mm <= 0:
        raise SystemExit("voxel, wall, and drain dimensions must be positive")
    if not args.no_hollow and not args.no_drain and args.drain_count < 2:
        raise SystemExit("production resin preparation requires at least two drains")
    try:
        report = prepare(args)
    except Exception as exc:
        REPORT["status"] = "failed"
        REPORT["error"] = type(exc).__name__
        REPORT["message"] = str(exc)
        report = REPORT
        if args.blend:
            try:
                bpy.ops.wm.save_as_mainfile(filepath=str(Path(args.blend)))
            except Exception:
                pass
        Path(args.report).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        raise
    Path(args.report).write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
