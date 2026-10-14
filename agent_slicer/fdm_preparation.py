"""Measured solid repair followed by bounded FDM bed orientation."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .blender_adapter import prepare_resin_model
from .project import sha256_file
from .stl import inspect_stl


def preparation_identity() -> dict[str, str]:
    package = Path(__file__).resolve().parent
    return {name: sha256_file(package / name) for name in (
        "fdm_preparation.py", "blender_adapter.py", "data/blender/prepare_resin.py", "data/blender/topology_repair.py")}


def prepare_fdm_model(source: str | Path, output: str | Path, report: str | Path,
                      *, build_volume_mm: tuple[float, float, float], **arguments: Any) -> dict:
    # Reuse the measured topology-repair implementation, with every resin
    # operation disabled. Its complete measurements remain in the FDM receipt.
    if arguments.get("hollow") or arguments.get("drain"):
        raise ValueError("FDM preparation does not accept resin hollowing or drains")
    orient = bool(arguments.get("orient"))
    arguments.update(orient=False, hollow=False, drain=False)
    result = prepare_resin_model(source, output, report, **arguments)
    result["schema"] = "agent-slicer-fdm-preparation/v1"
    result["implementation_sha256"] = preparation_identity()
    result["requested"].update(orient=orient, build_volume_mm=list(build_volume_mm))
    try:
        result["orientation"] = orient_fdm(Path(output), build_volume_mm) if orient else {"status": "preserved"}
    except Exception as exc:
        result.update(status="failed", error=type(exc).__name__, message=str(exc))
        Path(report).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        raise
    result["output_sha256"] = sha256_file(Path(output))
    result["final_inspection"] = inspect_stl(output).to_dict()
    result["limitations"] = [
        "Repair uses the shared measured topology-repair implementation.",
        "FDM orientation evaluates six axis-aligned bed faces; surface appearance is not recognized.",
        "Support quality and physical printability require review and material calibration.",
    ]
    Path(report).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def orient_fdm(path: Path, build_volume_mm: tuple[float, float, float]) -> dict:
    import numpy as np
    import trimesh

    mesh = trimesh.load_mesh(path, process=True)
    if not isinstance(mesh, trimesh.Trimesh) or not mesh.is_watertight or not mesh.is_winding_consistent:
        raise RuntimeError("FDM orientation requires a repaired watertight solid")
    candidates = []
    rotations = [(0., (1, 0, 0)), (math.pi / 2, (1, 0, 0)), (-math.pi / 2, (1, 0, 0)),
                 (math.pi, (1, 0, 0)), (math.pi / 2, (0, 1, 0)), (-math.pi / 2, (0, 1, 0))]
    for index, (angle, axis) in enumerate(rotations):
        matrix = trimesh.transformations.rotation_matrix(angle, axis)
        vertices = mesh.vertices @ matrix[:3, :3].T
        low, high = vertices.min(axis=0), vertices.max(axis=0)
        normals = mesh.face_normals @ matrix[:3, :3].T
        heights = (vertices[mesh.faces].mean(axis=1) - low)[..., 2]
        # Bed-contact downward faces need no supports. Penalize elevated
        # overhang area, then prefer greater contact area and a shorter print.
        downward = normals[:, 2] < -math.sqrt(0.5)
        bed = downward & (heights <= 0.05)
        overhang_area = float(mesh.area_faces[downward & ~bed].sum())
        contact_area = float(mesh.area_faces[bed].sum())
        dimensions = high - low
        fits = (min(dimensions[:2]) <= min(build_volume_mm[:2])
                and max(dimensions[:2]) <= max(build_volume_mm[:2])
                and dimensions[2] <= build_volume_mm[2])
        candidates.append({"index": index, "angle_deg": math.degrees(angle), "axis": list(axis),
                           "overhang_area_mm2": overhang_area, "bed_contact_area_mm2": contact_area,
                           "height_mm": float(dimensions[2]), "fits": bool(fits)})
    fitting = [candidate for candidate in candidates if candidate["fits"]]
    if not fitting:
        raise RuntimeError("no bounded FDM orientation fits the build volume")
    selected = min(fitting, key=lambda c: (round(c["overhang_area_mm2"], 6), -round(c["bed_contact_area_mm2"], 6),
                                          round(c["height_mm"], 6), c["index"]))
    angle, axis = rotations[selected["index"]]
    mesh.apply_transform(trimesh.transformations.rotation_matrix(angle, axis))
    mesh.apply_translation(-mesh.bounds[0])
    temporary = path.with_suffix(".orientation.tmp")
    mesh.export(temporary, file_type="stl")
    reloaded = trimesh.load_mesh(temporary, file_type="stl", process=True)
    if (not reloaded.is_watertight or not reloaded.is_winding_consistent
            or not np.isclose(abs(reloaded.volume), abs(mesh.volume), rtol=1e-5, atol=1e-5)):
        raise RuntimeError("FDM orientation export failed topology/volume checks")
    temporary.replace(path)
    return {"status": "passed", "method": "six-bed-faces-fdm-v1", "selected": selected, "candidates": candidates}
