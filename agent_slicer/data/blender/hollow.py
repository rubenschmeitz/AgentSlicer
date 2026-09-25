"""Packaged Blender-side hollow/drain operation; use blender_adapter.py."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import bmesh
import bpy
from mathutils import Vector


def mesh_stats(obj: bpy.types.Object) -> dict[str, object]:
    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)
    volume = abs(float(bm.calc_volume(signed=True)))
    boundary = sum(1 for edge in bm.edges if edge.is_boundary)
    non_manifold = sum(1 for edge in bm.edges if not edge.is_manifold)
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
        "volume_mm3": volume,
        "bounds_min_mm": minimum,
        "bounds_max_mm": maximum,
        "dimensions_mm": [maximum[i] - minimum[i] for i in range(3)],
    }


def select_bottom_drain_points(
    obj: bpy.types.Object,
    *,
    count: int,
    diameter_mm: float,
) -> list[tuple[float, float]]:
    z_min = min(vertex.co.z for vertex in obj.data.vertices)
    candidates: list[tuple[float, float]] = []
    for polygon in obj.data.polygons:
        if polygon.center.z <= z_min + 2.0 and polygon.normal.z < -0.25:
            candidates.append((polygon.center.x, polygon.center.y))
    if len(candidates) < count:
        candidates = [
            (vertex.co.x, vertex.co.y)
            for vertex in obj.data.vertices
            if vertex.co.z <= z_min + 1.5
        ]
    if not candidates:
        raise RuntimeError("no bottom-region geometry found for drain placement")
    if count == 1:
        return [_centroid(candidates)]

    left = min(candidates, key=lambda point: (point[0], point[1]))
    right = max(candidates, key=lambda point: (point[0], point[1]))
    centers = [left, right]
    for _ in range(16):
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
    if math.dist(centers[0], centers[1]) < diameter_mm * 1.75:
        return [_centroid(candidates)]
    return sorted(centers)


def _centroid(points: list[tuple[float, float]]) -> tuple[float, float]:
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


def apply_modifier(obj: bpy.types.Object, name: str) -> None:
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    result = bpy.ops.object.modifier_apply(modifier=name)
    if "FINISHED" not in result:
        raise RuntimeError(f"modifier failed: {name}")


def hollow_and_drain(args: argparse.Namespace) -> dict[str, object]:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    result = bpy.ops.wm.stl_import(filepath=str(Path(args.input)))
    if "FINISHED" not in result:
        raise RuntimeError("STL import failed")
    meshes = [obj for obj in bpy.context.selected_objects if obj.type == "MESH"]
    if len(meshes) != 1:
        raise RuntimeError(f"expected one imported mesh, found {len(meshes)}")
    obj = meshes[0]
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)

    source_before_scale = mesh_stats(obj)
    scale_factor = 1.0
    if args.target_height_mm is not None:
        source_height = float(source_before_scale["dimensions_mm"][2])
        if source_height <= 0:
            raise RuntimeError("source has zero height")
        scale_factor = float(args.target_height_mm) / source_height
        obj.scale = (scale_factor, scale_factor, scale_factor)
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    before = mesh_stats(obj)
    if before["boundary_edges"] or before["non_manifold_edges"]:
        raise RuntimeError(
            "source is not manifold; repair must run before controlled hollowing"
        )

    inner = obj.copy()
    inner.data = obj.data.copy()
    inner.name = "AgentSlicerInnerVolume"
    bpy.context.collection.objects.link(inner)
    coordinates = [vertex.co.copy() for vertex in inner.data.vertices]
    local_min = Vector(
        tuple(min(point[axis] for point in coordinates) for axis in range(3))
    )
    local_max = Vector(
        tuple(max(point[axis] for point in coordinates) for axis in range(3))
    )
    local_dimensions = local_max - local_min
    if min(local_dimensions) <= 2.0 * float(args.wall_mm):
        raise RuntimeError("model is too thin for requested hollow wall")
    cavity_scale = min(
        1.0 - (2.0 * float(args.wall_mm) / local_dimensions[axis])
        for axis in range(3)
    )
    local_center = (local_min + local_max) / 2.0
    for vertex in inner.data.vertices:
        vertex.co = local_center + (vertex.co - local_center) * cavity_scale
    inner.data.update()
    inner_stats = mesh_stats(inner)
    if not 0.02 < float(inner_stats["volume_mm3"]) / float(before["volume_mm3"]) < 0.98:
        raise RuntimeError("inner offset produced implausible cavity volume")

    inner.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    hollow = obj.modifiers.new(name="AgentSlicerHollowBoolean", type="BOOLEAN")
    hollow.operation = "DIFFERENCE"
    hollow.solver = "EXACT"
    hollow.object = inner
    apply_modifier(obj, hollow.name)
    bpy.data.objects.remove(inner, do_unlink=True)
    after_hollow = mesh_stats(obj)
    material_ratio = float(after_hollow["volume_mm3"]) / float(before["volume_mm3"])
    if not 0.02 < material_ratio < 0.9:
        raise RuntimeError(
            f"hollowing produced implausible material ratio {material_ratio:.4f}"
        )

    hole_points = select_bottom_drain_points(
        obj,
        count=int(args.drain_count),
        diameter_mm=float(args.drain_diameter_mm),
    )
    z_min = float(after_hollow["bounds_min_mm"][2])
    cutter_depth = max(8.0, float(args.wall_mm) * 5.0)
    print(
        "AGENT_SLICER_DRAIN_POINTS="
        + json.dumps({"points": hole_points, "z_min": z_min, "depth": cutter_depth})
    )
    holes: list[dict[str, float]] = []
    for index, (x, y) in enumerate(hole_points, start=1):
        volume_before_cut = float(mesh_stats(obj)["volume_mm3"])
        bpy.ops.mesh.primitive_cylinder_add(
            vertices=48,
            radius=float(args.drain_diameter_mm) / 2.0,
            depth=cutter_depth,
            location=(x, y, z_min - 1.0 + cutter_depth / 2.0),
        )
        cutter = bpy.context.active_object
        cutter.name = f"AgentSlicerDrain{index}"
        boolean = obj.modifiers.new(name=f"AgentSlicerDrainBoolean{index}", type="BOOLEAN")
        boolean.operation = "DIFFERENCE"
        boolean.solver = "EXACT"
        boolean.object = cutter
        apply_modifier(obj, boolean.name)
        bpy.data.objects.remove(cutter, do_unlink=True)
        volume_after_cut = float(mesh_stats(obj)["volume_mm3"])
        print(
            "AGENT_SLICER_DRAIN_DELTA="
            + json.dumps(
                {
                    "index": index,
                    "before": volume_before_cut,
                    "after": volume_after_cut,
                    "removed": volume_before_cut - volume_after_cut,
                }
            )
        )
        holes.append(
            {
                "x_mm": x,
                "y_mm": y,
                "z_entry_mm": z_min,
                "diameter_mm": float(args.drain_diameter_mm),
            }
        )

    after_drains = mesh_stats(obj)
    if after_drains["boundary_edges"] or after_drains["non_manifold_edges"]:
        raise RuntimeError("hollow/drain result is not manifold")
    if float(after_drains["volume_mm3"]) >= float(after_hollow["volume_mm3"]):
        raise RuntimeError("drain booleans did not remove measurable material")

    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    export = bpy.ops.wm.stl_export(
        filepath=str(output),
        export_selected_objects=True,
        ascii_format=False,
        apply_modifiers=True,
    )
    if "FINISHED" not in export:
        raise RuntimeError("STL export failed")
    if args.blend:
        bpy.ops.wm.save_as_mainfile(filepath=str(Path(args.blend)))

    return {
        "schema": "agent-slicer-blender-hollow/v1",
        "status": "passed",
        "source": str(Path(args.input)),
        "output": str(output),
        "source_before_scale": source_before_scale,
        "scale_factor": scale_factor,
        "target_height_mm": args.target_height_mm,
        "wall_mm": float(args.wall_mm),
        "hollow_method": "uniform-contracted-cavity",
        "cavity_scale": cavity_scale,
        "drain_holes": holes,
        "before": before,
        "after_hollow": after_hollow,
        "inner_cavity": inner_stats,
        "after_drains": after_drains,
        "material_volume_ratio": float(after_drains["volume_mm3"])
        / float(before["volume_mm3"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--blend")
    parser.add_argument("--wall-mm", type=float, default=1.5)
    parser.add_argument("--target-height-mm", type=float)
    parser.add_argument("--drain-diameter-mm", type=float, default=3.0)
    parser.add_argument("--drain-count", type=int, default=2)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1 :])
    try:
        report = hollow_and_drain(args)
    except Exception as exc:
        report = {
            "schema": "agent-slicer-blender-hollow/v1",
            "status": "failed",
            "error": type(exc).__name__,
            "message": str(exc),
        }
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        raise
    Path(args.report).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
