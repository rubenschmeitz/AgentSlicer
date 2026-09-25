"""Read-only, conservative drain-clearance screening of a saved support plan.

This is not a replacement for saved-layer/UVtools validation. It operates on
an accepted geometry report and companion support cache, without editing either.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
from scipy import ndimage

from .project import sha256_file


def audit_encoded_drain(evidence_path: str | Path, *, center_mm: tuple[float, float],
                        diameter_mm: float, first_layer: int, last_layer: int,
                        pixel_mm: float, channel_diameter_mm: float = 1.0) -> dict:
    """Measure a finite-width connected passage in exact exported CTB pixels.

    The caller supplies the bore's final plate coordinates and inclusive layer
    range from the accepted geometry/placement. Never treats a resized preview
    or an incomplete crop as drainage evidence. This measures clearance, not
    washing or curing access.
    """
    from PIL import Image

    evidence_path = Path(evidence_path)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if evidence.get("schema") != "agent-slicer-layer-evidence/v1" or evidence.get("scale") != 1:
        raise ValueError("Drain audit requires exact, unscaled saved-layer evidence")
    if not (0 < pixel_mm < diameter_mm and 0 < channel_diameter_mm < diameter_mm):
        raise ValueError("Invalid pixel, bore or channel dimensions")
    if first_layer < 0 or last_layer < first_layer:
        raise ValueError("Invalid layer range")
    x0, y0, width, height = evidence["crop_pixels"]
    cx, cy = center_mm
    radius = diameter_mm / 2
    if not (x0*pixel_mm < cx-radius and (x0+width)*pixel_mm > cx+radius and
            y0*pixel_mm < cy-radius and (y0+height)*pixel_mm > cy+radius):
        raise ValueError("Crop must contain the entire drain with surrounding pixels")
    indices, files = evidence["indices_zero_based"], evidence["files"]
    if len(indices) != len(files) or len(set(indices)) != len(indices):
        raise ValueError("Invalid evidence index mapping")
    mapping = dict(zip(indices, files))
    if any(index not in mapping for index in range(first_layer, last_layer+1)):
        raise ValueError("Missing drain layers")
    xx, yy = np.meshgrid((np.arange(width)+x0+.5)*pixel_mm,
                         (np.arange(height)+y0+.5)*pixel_mm)
    # The small inset is conservative for pixel-boundary quantization.
    inside = (xx-cx)**2 + (yy-cy)**2 < (radius-pixel_mm)**2
    free_layers = []
    for index in range(first_layer, last_layer+1):
        with Image.open(mapping[index]) as img:
            if img.size != (width, height):
                raise ValueError("Saved layer dimensions do not match evidence")
            free_layers.append((np.asarray(img.convert("L")) == 0) & inside)
    free = np.asarray(free_layers)
    channels = np.asarray([ndimage.distance_transform_edt(layer)*pixel_mm >
                           channel_diameter_mm/2 + pixel_mm for layer in free])
    labels, _ = ndimage.label(channels)
    connected = bool((set(np.unique(labels[0])) & set(np.unique(labels[-1]))) - {0})
    straight = ndimage.distance_transform_edt(free.all(axis=0)).max()*pixel_mm*2
    areas = free.sum(axis=(1,2))*pixel_mm**2
    return {"schema": "agent-slicer-encoded-drain-audit/v1",
            "status": "clear" if connected else "needs-review",
            "source_sha256": evidence["source_sha256"],
            "evidence_sha256": sha256_file(evidence_path),
            "center_mm": list(center_mm), "bore_diameter_mm": diameter_mm,
            "first_layer_zero_based": first_layer, "last_layer_zero_based": last_layer,
            "tested_channel_diameter_mm": channel_diameter_mm,
            "continuous_channel": connected,
            "straight_channel_diameter_lower_bound_mm": max(0., float(straight)-2*pixel_mm),
            "min_free_cross_section_mm2": float(areas.min()),
            "narrowest_layer_zero_based": int(areas.argmin())+first_layer,
            "limitations": ["Measures the specified bore only, not whole-cavity connectivity.",
                            "Does not certify washing, curing access or physical flow rate."]}


def audit_planned_drains(cache_path: str | Path, geometry_report: str | Path,
                        *, grid_mm: float = 0.05, channel_radius_mm: float = 0.5) -> dict:
    if not 0.025 <= grid_mm <= 0.2 or not 0 < channel_radius_mm <= 2:
        raise ValueError("Invalid drain screening resolution or channel radius")
    cache = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    geometry = json.loads(Path(geometry_report).read_text(encoding="utf-8"))
    if geometry.get("status") != "passed" or geometry.get("export_validation", {}).get("status") != "passed":
        raise ValueError("Drain screening requires accepted geometry")
    plan, config = cache["plan"], cache["identity"]["print_config"]
    bounds = geometry["export_validation"]["bounds_mm"]
    if not config.get("center_model"):
        raise ValueError("Drain screening currently requires centered, unrotated prepared geometry")
    transform = cache["identity"].get("transform", {})
    if any(transform.get(key, 0) != 0 for key in ("rotate_x_deg", "rotate_y_deg", "rotate_z_deg")):
        raise ValueError("Additional slicer rotation is not supported by this audit")
    shift = ((config["size_x_mm"] - bounds[0][0] - bounds[1][0]) / 2,
             (config["size_y_mm"] - bounds[0][1] - bounds[1][1]) / 2)
    lift = cache["identity"]["support_config"]["model_lift_mm"]
    dz = plan["layer_height_mm"]
    protected_floor = max(plan["raft_layers"], plan.get("pedestal_height_layers", 0)) * dz
    if lift <= protected_floor:
        raise ValueError("Drain screening near raft/pedestals requires encoded-layer review")
    sx, sy = plan["pixel_size_x_mm"], plan["pixel_size_y_mm"]
    pixel_mm = min(sx, sy)
    # Pad analytic disks for pixel rounding and entire local grid cells.
    pad = max(sx, sy) * 2 + grid_mm * math.sqrt(2)
    shafts = []
    for anchor in plan["anchors"]:
        def xyz(x, y, layer):
            return np.array(((x + .5) * sx, (y + .5) * sy, (layer + .5) * dz))
        bx = anchor["x"] if anchor["base_x"] is None else anchor["base_x"]
        by = anchor["y"] if anchor["base_y"] is None else anchor["base_y"]
        jx = anchor["x"] if anchor["joint_x"] is None else anchor["joint_x"]
        jy = anchor["y"] if anchor["joint_y"] is None else anchor["joint_y"]
        jl = max(0, anchor["top_layer"]-1) if anchor["joint_layer"] is None else anchor["joint_layer"]
        radius = max(plan["post_radius_px"], min(plan["tree_trunk_radius_px"],
                     math.ceil(plan["post_radius_px"] * math.sqrt(max(1, anchor["load_count"]))))) * pixel_mm
        shafts.append((xyz(bx, by, anchor["base_layer"]), xyz(jx, jy, jl),
                       xyz(anchor["x"], anchor["y"], anchor["top_layer"]), radius,
                       anchor["kind"], plan.get("contact_depth_layers", 0) * dz))
    results = []
    for hole in geometry["stages"]["hollow_and_drain"]["drain_holes"]:
        cx, cy = (hole["center_world_xy_mm"][i] + shift[i] for i in (0, 1))
        radius = hole["diameter_mm"] / 2
        axis = np.arange(-radius, radius + grid_mm * .5, grid_mm)
        xx, yy = np.meshgrid(axis + cx, axis + cy)
        inside = (xx-cx)**2 + (yy-cy)**2 <= (radius-pad)**2
        first = math.ceil((hole["cutter_bottom_z_mm"] + lift) / dz - .5)
        last = math.floor((hole["cutter_top_z_mm"] + lift) / dz - .5)
        free_layers = []
        for index in range(first, last + 1):
            z = (index + .5) * dz
            blocked = np.zeros_like(inside)
            def disk(x, y, r):
                nonlocal blocked
                if abs(x-cx) <= radius+r+pad and abs(y-cy) <= radius+r+pad:
                    blocked |= (xx-x)**2 + (yy-y)**2 <= (r+pad)**2
            for base, joint, top, r, kind, depth in shafts:
                base_depth = depth if kind == "enforcer" else 0
                if z < base[2] - base_depth or z > top[2] + depth:
                    continue
                if z < base[2]:
                    disk(base[0], base[1], plan["tip_radius_px"] * pixel_mm)
                    continue
                if z > top[2]:
                    disk(top[0], top[1], plan["tip_radius_px"] * pixel_mm)
                    continue
                a, b = (base, joint) if z <= joint[2] else (joint, top)
                t = min(1., max(0., (z-a[2]) / max(dz, b[2]-a[2])))
                pos = a + (b-a)*t
                # Upper-bound the entire taper by the shaft radius.
                disk(pos[0], pos[1], r)
                if abs(z-joint[2]) <= r:
                    disk(joint[0], joint[1], r)
                if kind == "branch" and 0 <= z-base[2] <= r:
                    disk(base[0], base[1], r)
            for brace in plan["braces"]:
                z0, z1 = (brace["start_layer"]+.5)*dz, (brace["end_layer"]+.5)*dz
                if z0-plan["brace_layer_radius"]*dz <= z <= z1+plan["brace_layer_radius"]*dz:
                    t = min(1., max(0., (z-z0) / max(dz, z1-z0)))
                    disk((brace["x0"] + (brace["x1"]-brace["x0"])*t+.5)*sx,
                         (brace["y0"] + (brace["y1"]-brace["y0"])*t+.5)*sy,
                         brace["radius_px"]*pixel_mm)
            free_layers.append(inside & ~blocked)
        free = np.asarray(free_layers)
        if not len(free):
            raise ValueError("Drain has no sampled layers")
        # Erode each cross-section to test a finite-width path, not a one-voxel
        # leak. Connectivity then requires face-adjacent cells through the bore.
        channel = np.asarray([ndimage.distance_transform_edt(layer) * grid_mm >
                              channel_radius_mm + grid_mm for layer in free])
        labels, _ = ndimage.label(channel)
        bottom, top = set(np.unique(labels[0])), set(np.unique(labels[-1]))
        connected = bool((bottom & top) - {0})
        results.append({"index": hole["index"], "layers_tested": len(free),
                        "min_free_cross_section_mm2": float(free.sum(axis=(1,2)).min()*grid_mm**2),
                        "continuous_channel_diameter_mm": 2*channel_radius_mm if connected else None,
                        "status": "clear-in-conservative-support-screen" if connected else "needs-encoded-layer-review"})
    return {"schema": "agent-slicer-planned-drain-screen/v1",
            "status": "screen-passed" if results and all(r["continuous_channel_diameter_mm"] for r in results) else "needs-review",
            "support_cache_sha256": sha256_file(Path(cache_path)),
            "geometry_report_sha256": sha256_file(Path(geometry_report)), "holes": results,
            "limitations": ["Conservative support-only screening, not encoded-layer validation.",
                           "Prepared-geometry drain connectivity is a separate prerequisite.",
                           "Does not certify washing, curing access or physical flow rate."]}
