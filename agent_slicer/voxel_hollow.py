from __future__ import annotations

import hashlib
import gc
import io
import json
import math
import os
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
from scipy import ndimage
from scipy.spatial import cKDTree
from .resources import heavy_job


def _hollow_memory_preflight(extents, voxel_mm: float) -> dict[str, Any]:
    """Conservative grid working-set estimate, not a promise of peak RSS.

    Distance transforms, component labels, topology smoothing, masks and
    temporary arrays coexist. The former cell-only limit allowed multi-GB jobs.
    Mesh storage and library overhead are additional to this estimate.
    """
    budget_mb = float(os.environ.get("AGENT_SLICER_MAX_HOLLOW_MEMORY_MB", "1024"))
    if not math.isfinite(budget_mb) or budget_mb <= 0:
        raise ValueError("AGENT_SLICER_MAX_HOLLOW_MEMORY_MB must be finite and positive")
    if not math.isfinite(voxel_mm) or voxel_mm <= 0 or any(not math.isfinite(float(v)) or v < 0 for v in extents):
        raise ValueError("hollow grid dimensions must be finite and nonnegative")
    # Python integers avoid silent np.int64 overflow on extreme/invalid input.
    cells = math.prod(math.ceil(float(length) / voxel_mm) + 5 for length in extents)
    estimated_bytes = cells * 80
    if cells > 200_000_000 or estimated_bytes > budget_mb * 1024 * 1024:
        raise RuntimeError(
            f"hollowing grid memory estimate {estimated_bytes / 1024**2:.1f} MiB exceeds "
            f"budget {budget_mb:g} MiB or cell limit; choose a coarser explicitly approved "
            "voxel size, split the model, or raise AGENT_SLICER_MAX_HOLLOW_MEMORY_MB"
        )
    return {"estimated_cells": cells, "estimated_grid_bytes": estimated_bytes,
            "bytes_per_cell_allowance": 80, "budget_mb": budget_mb,
            "excludes": "mesh storage and library/runtime overhead"}


@heavy_job
def hollow_and_drain_voxel(
    source: str | Path,
    output: str | Path,
    report: str | Path,
    *,
    wall_mm: float = 1.8,
    voxel_mm: float = 0.15,
    drain_diameter_mm: float = 3.5,
    drain_count: int = 2,
    max_outer_p95_change_mm: float | None = 0.20,
    max_outer_change_mm: float | None = 0.60,
) -> dict[str, Any]:
    """Create one connected, drained cavity using a filled voxel solid.

    Thin features and secondary potential cavities remain solid.  The operation
    fails unless every retained cavity voxel is flood-fill reachable from the
    exterior through each requested drain and the reconstructed surface is
    watertight.
    """

    if any(not math.isfinite(value) or value <= 0 for value in (wall_mm, voxel_mm, drain_diameter_mm)):
        raise ValueError("wall, voxel, and drain dimensions must be finite and positive")
    if drain_count != 2:
        raise ValueError("production voxel hollowing currently requires two drains")
    if wall_mm / voxel_mm < 3.0:
        raise ValueError("wall thickness must span at least three hollowing voxels")
    if drain_diameter_mm / voxel_mm < 4.0:
        raise ValueError("drain diameter must span at least four hollowing voxels")
    source_path = Path(source).resolve()
    output_path = Path(output).resolve()
    report_path = Path(report).resolve()
    if len({source_path, output_path, report_path}) != 3:
        raise ValueError("source model, output model, and report must be distinct files")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    # STL stores independent triangle vertices; processing must merge those
    # records before connected-component analysis or every triangle appears to
    # be a separate open component.
    loaded = trimesh.load_mesh(source_path, process=True)
    if not isinstance(loaded, trimesh.Trimesh) or loaded.is_empty:
        raise RuntimeError("voxel hollowing requires one non-empty mesh")
    components = list(loaded.split(only_watertight=False))
    if not components or any(
        not component.is_watertight or not component.is_winding_consistent
        for component in components
    ):
        raise RuntimeError(
            "voxel hollowing requires watertight, winding-consistent input components"
        )
    components.sort(key=lambda component: abs(float(component.volume)), reverse=True)
    primary = components[0]
    secondary = components[1:]
    if not secondary:
        # split() builds a complete copy; do not keep both million-face meshes
        # and their adjacency caches alive through voxelization and Booleans.
        loaded = primary
    source_bounds = loaded.bounds.tolist()
    memory_estimate = _hollow_memory_preflight(primary.extents, voxel_mm)
    voxel = primary.voxelized(voxel_mm)
    # Retain *all* rasterized source surfaces, including narrow recesses which
    # binary filling may classify as interior. Eroding only the filled volume
    # can cut through these recesses even with an apparently thick outer wall.
    source_surface = np.pad(voxel.matrix.astype(bool), 2, mode="constant")
    voxel = voxel.fill()
    padding = 2
    solid = np.pad(voxel.matrix.astype(bool), padding, mode="constant")
    origin = np.asarray(voxel.transform[:3, 3], dtype=float) - padding * voxel_mm
    solid_count = int(np.count_nonzero(solid))
    if solid_count == 0:
        raise RuntimeError("voxelization produced no solid cells")

    distance = ndimage.distance_transform_edt(solid & ~source_surface, sampling=voxel_mm)
    del source_surface
    # Voxel occupancy and marching cubes both have finite surface error. Keep
    # that allowance inside the solid instead of eating into the requested wall.
    cavity_candidates = distance >= wall_mm + math.sqrt(3.0) * voxel_mm
    structure = ndimage.generate_binary_structure(3, 1)
    labels, candidate_count = ndimage.label(cavity_candidates, structure=structure)
    sizes = np.bincount(labels.ravel())
    if len(sizes) <= 1 or int(sizes[1:].max(initial=0)) < 32:
        raise RuntimeError("model is too thin to form a useful drained cavity")
    retained_label = int(np.argmax(sizes[1:]) + 1)
    inner = labels == retained_label
    cavity_candidate_voxels = int(np.count_nonzero(cavity_candidates))
    del distance, cavity_candidates, labels, sizes
    inner_count = int(np.count_nonzero(inner))
    shell = solid & ~inner

    plans = _plan_local_drains(inner, primary, origin=origin, pitch_mm=voxel_mm,
                               diameter_mm=drain_diameter_mm, wall_mm=wall_mm)
    radius_voxels = (drain_diameter_mm / 2.0) / voxel_mm
    grid_x, grid_y = np.ogrid[: shell.shape[0], : shell.shape[1]]
    holes: list[dict[str, Any]] = []
    for index, (center, depth) in enumerate(plans, start=1):
        center_x, center_y = (int(center[0]), int(center[1]))
        column = np.flatnonzero(inner[center_x, center_y, :])
        if len(column) == 0:
            raise RuntimeError(f"drain {index} is not aligned with the retained cavity")
        target_z = int(column.min())
        center_world = origin + np.array([center_x * voxel_mm, center_y * voxel_mm, 0.0])
        start_z = max(0, int(math.floor((depth["cutter_bottom_z_mm"] - origin[2]) / voxel_mm)) - 2)
        disk = (
            (grid_x - center_x) ** 2 + (grid_y - center_y) ** 2
            <= radius_voxels**2
        )
        before = int(np.count_nonzero(shell))
        shell[disk, start_z : target_z + 3] = False
        removed = before - int(np.count_nonzero(shell))
        if removed <= 0:
            raise RuntimeError(f"drain {index} removed no shell voxels")
        single_hole_shell = (solid & ~inner).copy()
        single_hole_shell[disk, start_z : target_z + 3] = False
        reachable = _exterior_reachable(~single_hole_shell)
        individually_connected = bool(np.all(reachable[inner]))
        del reachable, single_hole_shell
        if not individually_connected:
            raise RuntimeError(f"drain {index} does not independently open the cavity")
        holes.append(
            {
                **depth,
                "index": index,
                "center_index_xy": [center_x, center_y],
                "center_world_xy_mm": center_world[:2].tolist(),
                "diameter_mm": drain_diameter_mm,
                "target_cavity_z_index": target_z,
                "target_cavity_z_mm": float(origin[2] + target_z * voxel_mm),
                "removed_shell_voxels": removed,
                "independently_connects_cavity": individually_connected,
            }
        )

    reachable = _exterior_reachable(~shell)
    sealed_voxels = int(np.count_nonzero(inner & ~reachable))
    if sealed_voxels:
        raise RuntimeError(f"{sealed_voxels} retained cavity voxels are sealed")

    reconstructed_primary, reconstruction = _subtract_cavity_and_drains(
        primary, inner, origin=origin, pitch_mm=voxel_mm, holes=holes
    )
    reconstructed_primary.remove_unreferenced_vertices()
    if not reconstructed_primary.is_watertight:
        raise RuntimeError("voxel shell reconstruction is not watertight")
    if not reconstructed_primary.is_winding_consistent:
        raise RuntimeError("voxel shell reconstruction has inconsistent winding")
    reconstructed = (
        trimesh.util.concatenate([reconstructed_primary, *secondary])
        if secondary
        else reconstructed_primary
    )
    reconstructed.remove_unreferenced_vertices()
    if not reconstructed.is_watertight or not reconstructed.is_winding_consistent:
        raise RuntimeError(
            "component-aware shell reconstruction did not preserve manifold topology"
        )
    bounds_change = float(
        np.max(np.abs(np.asarray(reconstructed.bounds) - np.asarray(source_bounds)))
    )
    preservation_tolerance = max(1e-5, float(np.max(loaded.extents)) * 2e-7)
    outward_expansion = float(max(0., np.max(np.asarray(source_bounds)[0] - reconstructed.bounds[0]),
                                  np.max(reconstructed.bounds[1] - np.asarray(source_bounds)[1])))
    # A drain can remove the extremal point of a curved bottom. That is a valid
    # contraction; expansion is never justified by cavity subtraction.
    if outward_expansion > preservation_tolerance:
        raise RuntimeError("hollow Boolean expanded the original outer bounds")

    source_vertices = np.asarray(loaded.vertices, dtype=float)
    stride = max(1, math.ceil(len(source_vertices) / 20_000))
    samples = source_vertices[::stride]
    retained = np.ones(len(samples), dtype=bool)
    for hole in holes:
        center_xy = np.asarray(hole["center_world_xy_mm"], dtype=float)
        radial = np.linalg.norm(samples[:, :2] - center_xy, axis=1)
        retained &= ~(
            (radial <= drain_diameter_mm / 2.0 + voxel_mm)
            & (samples[:, 2] >= float(hole["cutter_bottom_z_mm"]) - voxel_mm)
            & (samples[:, 2] <= float(hole["target_cavity_z_mm"]) + 3 * voxel_mm)
        )
    comparison_samples = samples[retained]
    if len(comparison_samples) < 4:
        raise RuntimeError("too few outer-surface samples remain after drain exclusion")
    tree = cKDTree(np.asarray(reconstructed.vertices, dtype=float))
    distances, nearest_indices = tree.query(comparison_samples, workers=-1)
    nearest_points = np.asarray(reconstructed.vertices)[nearest_indices].copy()
    # Boolean retriangulation may remove coplanar vertices while preserving the
    # exact surface. Use point-to-triangle distances for those samples.
    pending = np.flatnonzero(distances > preservation_tolerance)
    for offset in range(0, len(pending), 256):
        batch = pending[offset:offset + 256]
        closest, actual, _ = trimesh.proximity.closest_point(reconstructed, comparison_samples[batch])
        distances[batch] = actual
        nearest_points[batch] = closest
    maximum_index = int(np.argmax(distances))
    outer_change = {
        "method": "sampled-outer-vertices-with-point-to-triangle-fallback",
        "sample_count": int(len(comparison_samples)),
        "drain_region_samples_excluded": int(np.count_nonzero(~retained)),
        "mean_mm": float(np.mean(distances)),
        "p95_mm": float(np.percentile(distances, 95)),
        "max_mm": float(np.max(distances)),
        "maximum_source_sample_mm": comparison_samples[maximum_index].tolist(),
        "maximum_nearest_reconstructed_point_mm": nearest_points[maximum_index].tolist(),
        "limits_mm": {
            "p95": max_outer_p95_change_mm,
            "max": max_outer_change_mm,
        },
        "note": "Sampled diagnostic; the Boolean operates on the original exterior mesh.",
    }
    if outer_change["max_mm"] > preservation_tolerance:
        raise RuntimeError(
            "hollowing changed the original exterior outside drain regions: "
            + json.dumps({"outer_surface_change": outer_change,
                          "preservation_tolerance_mm": preservation_tolerance,
                          "drain_holes": holes}, sort_keys=True)
        )
    if (
        max_outer_p95_change_mm is not None
        and outer_change["p95_mm"] > max_outer_p95_change_mm
    ):
        raise RuntimeError(
            f"hollow reconstruction p95 outer change {outer_change['p95_mm']:.4f} mm "
            f"exceeds {max_outer_p95_change_mm:.4f} mm"
        )
    if (
        max_outer_change_mm is not None
        and outer_change["max_mm"] > max_outer_change_mm
    ):
        raise RuntimeError(
            f"hollow reconstruction maximum outer change {outer_change['max_mm']:.4f} mm "
            f"exceeds {max_outer_change_mm:.4f} mm"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    primary_volume = abs(float(primary.volume))
    secondary_volume = float(sum(abs(float(component.volume)) for component in secondary))
    component_handling = {
        "method": "largest-component-hollow-secondary-components-preserved",
        "input_components": len(components),
        "primary_faces": int(len(primary.faces)),
        "primary_volume_mm3": primary_volume,
        "preserved_secondary_components": len(secondary),
        "preserved_secondary_faces": int(sum(len(component.faces) for component in secondary)),
        "preserved_secondary_volume_mm3": secondary_volume,
    }
    payload = reconstructed.export(file_type="stl")
    # Round-trip validation needs its own welded mesh. Release the Boolean and
    # input meshes/caches first rather than retaining three million-face copies.
    del reconstructed, loaded, primary, secondary, components, source_vertices, tree
    gc.collect()
    exported = trimesh.load_mesh(io.BytesIO(payload), file_type="stl", process=True)
    if not isinstance(exported, trimesh.Trimesh) or exported.is_empty:
        raise RuntimeError("exported STL could not be reloaded as a mesh")
    # Float32 export and STL vertex welding can collapse Boolean slivers to
    # exactly zero-area faces. Remove only those and exact duplicates, then
    # verify another complete round trip before replacing any output file.
    keep = exported.nondegenerate_faces(height=0.)
    degenerate_count = int(np.count_nonzero(~keep))
    exported.update_faces(keep)
    keep = exported.unique_faces()
    duplicate_count = int(np.count_nonzero(~keep))
    exported.update_faces(keep)
    exported.remove_unreferenced_vertices()
    if not exported.is_watertight or not exported.is_winding_consistent:
        raise RuntimeError("exported STL did not preserve manifold topology")
    payload = exported.export(file_type="stl")
    del exported
    gc.collect()
    exported = trimesh.load_mesh(io.BytesIO(payload), file_type="stl", process=True)
    if not exported.is_watertight or not exported.is_winding_consistent:
        raise RuntimeError("cleaned STL failed its second topology round trip")
    staged_output = output_path.with_suffix(output_path.suffix + ".tmp")
    staged_output.write_bytes(payload)
    staged_output.replace(output_path)

    primary_voxel_ratio = float(np.count_nonzero(shell)) / float(solid_count)
    estimated_total_ratio = (
        primary_voxel_ratio * primary_volume + secondary_volume
    ) / max(primary_volume + secondary_volume, 1e-12)
    result: dict[str, Any] = {
        "schema": "agent-slicer-voxel-hollow/v2",
        "memory_preflight": memory_estimate,
        "status": "passed",
        "source": str(source_path),
        "source_sha256": _sha256(source_path),
        "output": str(output_path),
        "output_sha256": _sha256(output_path),
        "method": "voxel-inner-cavity-with-original-surface-boolean",
        "outer_surface_preserved": True,
        "stl_cleanup": {"zero_area_faces_removed": degenerate_count,
                        "duplicate_faces_removed": duplicate_count,
                        "second_round_trip_passed": True},
        "outer_preservation_tolerance_mm": preservation_tolerance,
        "voxel_mm": voxel_mm,
        "requested_wall_mm": wall_mm,
        "wall_voxels": wall_mm / voxel_mm,
        "theoretical_surface_quantization_bound_mm": math.sqrt(3.0) * voxel_mm,
        "surface_reconstruction": reconstruction,
        "outer_bounds_max_change_mm": bounds_change,
        "outer_bounds_expansion_mm": outward_expansion,
        "outer_surface_change": outer_change,
        "source_bounds_mm": source_bounds,
        "grid_origin_mm": origin.tolist(),
        "grid_shape": list(shell.shape),
        "solid_voxels_before_hollow": solid_count,
        "component_handling": component_handling,
        "candidate_cavity_components": int(candidate_count),
        "retained_cavity_voxels": inner_count,
        "discarded_candidate_cavity_voxels": int(
            cavity_candidate_voxels - inner_count
        ),
        "shell_voxels_after_drains": int(np.count_nonzero(shell)),
        "primary_voxel_material_ratio": primary_voxel_ratio,
        "material_volume_ratio": estimated_total_ratio,
        "drain_holes": holes,
        "cavity_connected_to_exterior": True,
        "sealed_cavity_voxels": sealed_voxels,
        "mesh_validation": {
            "watertight": bool(exported.is_watertight),
            "winding_consistent": bool(exported.is_winding_consistent),
            "vertices": int(len(exported.vertices)),
            "faces": int(len(exported.faces)),
            "body_count": int(
                len(exported.split(only_watertight=False))
            ),
            "bounds_mm": exported.bounds.tolist(),
        },
        "limitations": [
            "Only the internal cavity is voxelized; exterior surfaces are retained except at drain intersections.",
            "Only the largest connected cavity is retained; smaller candidate pockets remain solid.",
            "Flood-fill connectivity establishes an open path, not physical post-print flushing technique.",
        ],
    }
    _write_json(report_path, result)
    return result


def _local_drain_depth(primary, xy, target_z, radius, pitch):
    """Find the underside directly below the cavity, not the build plate.

    Sample the cylinder rim too so a sloped underside is fully opened. This
    avoids cutting unrelated legs, props or bases below an elevated cavity.
    """
    angles = np.arange(64) * (2 * np.pi / 64)
    ring = np.column_stack((np.cos(angles), np.sin(angles))) * radius
    origins = np.column_stack((np.vstack(([0., 0.], ring)) + xy,
                               np.full(65, target_z)))
    directions = np.tile([0., 0., -1.], (len(origins), 1))
    hits, indices, _ = primary.ray.intersects_location(origins, directions, multiple_hits=False)
    if len(np.unique(indices)) != len(origins):
        raise RuntimeError("drain placement needs review: underside could not be located")
    return {"entry_z_range_mm": [float(hits[:, 2].min()), float(hits[:, 2].max())],
            "cutter_bottom_z_mm": float(hits[:, 2].min()) - pitch * .25,
            "cutter_top_z_mm": target_z + 2 * pitch,
            "depth_method": "cavity-to-local-underside-ray-cast"}


def _plan_local_drains(inner, primary, *, origin, pitch_mm, diameter_mm, wall_mm):
    """Prefer short exits from the lowest cavity region; bound automatic depth."""
    xy = np.argwhere(np.any(inner, axis=2))
    first_z = np.argmax(inner, axis=2)
    candidates = sorted(xy, key=lambda p: (int(first_z[tuple(p)]), int(p[0]), int(p[1])))
    valid = []
    maximum_depth = wall_mm + diameter_mm + 2 * pitch_mm
    for center in candidates[:256]:
        target_z = float(origin[2] + first_z[tuple(center)] * pitch_mm)
        world_xy = origin[:2] + center * pitch_mm
        try:
            depth = _local_drain_depth(primary, world_xy, target_z, diameter_mm/2., pitch_mm)
        except RuntimeError:
            continue
        length = depth["cutter_top_z_mm"] - depth["cutter_bottom_z_mm"]
        if length > maximum_depth:
            continue
        valid.append((center, depth, length))
        if len(valid) >= 32 and any(np.linalg.norm(center - old[0]) * pitch_mm >= diameter_mm * 1.4
                                    for old in valid[:-1]):
            break
    pairs = [(a, b) for i, a in enumerate(valid) for b in valid[i+1:]
             if np.linalg.norm(a[0] - b[0]) * pitch_mm >= diameter_mm * 1.4]
    if not pairs:
        raise RuntimeError("drain placement needs review: cannot fit two separated short underside drains")
    a, b = min(pairs, key=lambda pair: pair[0][2] + pair[1][2])
    return [(a[0], a[1]), (b[0], b[1])]


def _subtract_cavity_and_drains(primary, inner, *, origin, pitch_mm, holes):
    """Subtract a voxel cavity and geometric cylinders from the original mesh.

    Every drain is also checked alone on the actual resulting mesh. A sealed
    inner shell would add a disconnected component even if a coarse grid said
    the cavity was open.
    """
    cavity, reconstruction = _reconstruct_shell(inner, pitch_mm=pitch_mm)
    cavity.apply_translation(origin)
    cutters = []
    for hole in holes:
        bottom = float(hole["cutter_bottom_z_mm"])
        top = float(hole["cutter_top_z_mm"])
        cutter = trimesh.creation.cylinder(radius=hole["diameter_mm"] / 2., height=top-bottom, sections=64)
        cutter.apply_translation([*hole["center_world_xy_mm"], (top+bottom)/2.])
        single = trimesh.boolean.difference([primary, cavity, cutter], engine="manifold")
        if single.is_empty or not single.is_watertight or not single.is_winding_consistent:
            raise RuntimeError(f"drain {hole['index']} failed mesh Boolean validation")
        if len(single.split(only_watertight=False)) != 1:
            raise RuntimeError(f"drain {hole['index']} does not independently open the mesh cavity")
        hole["mesh_independently_connects_cavity"] = True
        cutters.append(cutter)
    result = trimesh.boolean.difference([primary, cavity, *cutters], engine="manifold")
    if result.is_empty or len(result.split(only_watertight=False)) != 1:
        raise RuntimeError("hollow Boolean left a sealed cavity or detached fragment")
    if not 0 < float(result.volume) < float(primary.volume):
        raise RuntimeError("hollow Boolean did not remove a bounded cavity volume")
    return result, {**reconstruction, "scope": "internal-cavity-only",
                    "outer_operation": "original-surface-boolean-difference",
                    "mesh_drain_connectivity_verified": True}


def _select_drain_centers(
    inner: np.ndarray,
    *,
    pitch_mm: float,
    diameter_mm: float,
) -> list[np.ndarray]:
    coordinates = np.argwhere(inner)
    if len(coordinates) == 0:
        raise RuntimeError("retained cavity is empty")
    lowest = int(coordinates[:, 2].min())
    band_layers = max(2, int(math.ceil(max(5.0, diameter_mm * 1.5) / pitch_mm)))
    low_band = coordinates[coordinates[:, 2] <= lowest + band_layers]
    xy = np.unique(low_band[:, :2], axis=0)
    if len(xy) < 2:
        raise RuntimeError("cavity low region cannot host two drains")
    extrema = np.unique(
        np.vstack(
            [
                xy[np.argmin(xy[:, 0])],
                xy[np.argmax(xy[:, 0])],
                xy[np.argmin(xy[:, 1])],
                xy[np.argmax(xy[:, 1])],
                xy[np.argmin(xy[:, 0] + xy[:, 1])],
                xy[np.argmax(xy[:, 0] + xy[:, 1])],
                xy[np.argmin(xy[:, 0] - xy[:, 1])],
                xy[np.argmax(xy[:, 0] - xy[:, 1])],
            ]
        ),
        axis=0,
    )
    best_a, best_b = max(
        (
            (a, b)
            for index, a in enumerate(extrema)
            for b in extrema[index + 1 :]
        ),
        key=lambda pair: float(np.linalg.norm(pair[0] - pair[1])),
    )
    separation_mm = float(np.linalg.norm(best_a - best_b)) * pitch_mm
    if separation_mm < diameter_mm * 1.4:
        raise RuntimeError(
            f"cavity low region cannot separate two {diameter_mm:.2f} mm drains"
        )
    return [best_a, best_b]


def _reconstruct_shell(
    shell: np.ndarray,
    *,
    pitch_mm: float,
) -> tuple[trimesh.Trimesh, dict[str, Any]]:
    direct = trimesh.voxel.ops.matrix_to_marching_cubes(shell, pitch=pitch_mm)
    direct.remove_unreferenced_vertices()
    if direct.is_watertight and direct.is_winding_consistent:
        if direct.volume < 0:
            direct.invert()
        return direct, {
            "method": "binary-lewiner-marching-cubes",
            "topology_disambiguation_applied": False,
            "gaussian_sigma_voxels": None,
        }

    from skimage import measure

    for sigma in (0.35, 0.50, 0.65):
        field = ndimage.gaussian_filter(
            shell.astype(np.float32),
            sigma=sigma,
            mode="constant",
            cval=0.0,
        )
        field = np.pad(field, 1, mode="constant", constant_values=0.0)
        vertices, faces, normals, _ = measure.marching_cubes(
            field,
            level=0.5,
            spacing=(pitch_mm, pitch_mm, pitch_mm),
            allow_degenerate=False,
            method="lewiner",
        )
        vertices -= pitch_mm
        candidate = trimesh.Trimesh(
            vertices=vertices,
            faces=faces,
            vertex_normals=normals,
            process=True,
        )
        candidate.remove_unreferenced_vertices()
        if candidate.volume < 0:
            candidate.invert()
        if candidate.is_watertight and candidate.is_winding_consistent:
            return candidate, {
                "method": "gaussian-topology-disambiguated-lewiner-marching-cubes",
                "topology_disambiguation_applied": True,
                "gaussian_sigma_voxels": sigma,
                "direct_reconstruction_watertight": bool(direct.is_watertight),
                "direct_reconstruction_winding_consistent": bool(
                    direct.is_winding_consistent
                ),
            }
    raise RuntimeError(
        "voxel shell reconstruction is not watertight after bounded topology "
        "disambiguation"
    )


def _smoothed_binary_shell(shell: np.ndarray, *, sigma: float) -> np.ndarray:
    field = ndimage.gaussian_filter(
        shell.astype(np.float32),
        sigma=sigma,
        mode="constant",
        cval=0.0,
    )
    return field >= 0.5


def _exterior_reachable(empty: np.ndarray) -> np.ndarray:
    seed = np.zeros_like(empty, dtype=bool)
    seed[0, :, :] = empty[0, :, :]
    seed[-1, :, :] = empty[-1, :, :]
    seed[:, 0, :] = empty[:, 0, :]
    seed[:, -1, :] = empty[:, -1, :]
    seed[:, :, 0] = empty[:, :, 0]
    seed[:, :, -1] = empty[:, :, -1]
    return ndimage.binary_propagation(seed, mask=empty)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
