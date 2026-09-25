from __future__ import annotations

import json
import math
import shutil
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from .project import sha256_file
from .resources import heavy_job, release_process_memory, work_directory
from .stl import inspect_stl


SIMPLIFICATION_SCHEMA = "agent-slicer-mesh-simplification/v1"


@heavy_job
def simplify_mesh_measured(
    source: str | Path,
    output: str | Path,
    report: str | Path,
    *,
    minimum_input_triangles: int = 500_000,
    target_triangles: int = 350_000,
    max_p95_deviation_mm: float = 0.035,
    max_deviation_mm: float = 0.10,
    max_volume_change_ratio: float = 0.005,
    sample_points: int = 20_000,
    aggression: int = 5,
    protected_component_triangles: int = 5_000,
) -> dict[str, Any]:
    """Simplify a printable mesh and retain it only after measured checks.

    The source is never modified. Quadric edge collapse is attempted from the
    requested target toward progressively more conservative targets. A result
    is accepted only when watertightness, winding, component count, Euler
    number, volume, bounds, and sampled bidirectional surface deviation pass.
    Otherwise the original is retained and the rejected measurements remain in
    the report.
    """

    requested = _validated_request(
        minimum_input_triangles=minimum_input_triangles,
        target_triangles=target_triangles,
        max_p95_deviation_mm=max_p95_deviation_mm,
        max_deviation_mm=max_deviation_mm,
        max_volume_change_ratio=max_volume_change_ratio,
        sample_points=sample_points,
        aggression=aggression,
        protected_component_triangles=protected_component_triangles,
    )
    source_path = Path(source).resolve()
    output_path = Path(output).resolve()
    report_path = Path(report).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    source_inspection = inspect_stl(source_path).to_dict()
    result: dict[str, Any] = {
        "schema": SIMPLIFICATION_SCHEMA,
        "status": "running",
        "source": str(source_path),
        "source_sha256": source_inspection["sha256"],
        "requested": requested,
        "source_inspection": source_inspection,
        "attempts": [],
        "applied": False,
    }

    try:
        if source_inspection["triangle_count"] < requested["minimum_input_triangles"]:
            shutil.copy2(source_path, output_path)
            result.update(
                {
                    "status": "passed",
                    "decision": "below-threshold-original-retained",
                    "output": str(output_path),
                    "output_sha256": sha256_file(output_path),
                    "output_inspection": inspect_stl(output_path).to_dict(),
                }
            )
            _write_json_atomic(report_path, result)
            return result

        import trimesh

        source_mesh = trimesh.load_mesh(source_path, process=True)
        if not isinstance(source_mesh, trimesh.Trimesh) or len(source_mesh.faces) == 0:
            raise RuntimeError("simplification input is not one triangle mesh")
        source_topology = _topology(source_mesh)
        if not source_topology["watertight"] or not source_topology["winding_consistent"]:
            raise RuntimeError("simplification input must be watertight and winding-consistent")
        source_components: list[Any] | None = None

        selected_path: Path | None = None
        with tempfile.TemporaryDirectory(
            prefix="agent-slicer-simplify-", dir=work_directory("simplify")
        ) as temporary:
            transaction = Path(temporary)
            for index, face_target in enumerate(
                _candidate_targets(len(source_mesh.faces), requested["target_triangles"]),
                start=1,
            ):
                candidate_path = transaction / f"candidate-{index}.stl"
                attempt: dict[str, Any] = {"target_triangles": face_target}
                try:
                    if source_components is None:
                        source_components = _connected_components(
                            source_mesh,
                            expected_count=source_topology["body_count"],
                        )
                    candidate, component_result = _simplify_components(
                        source_components,
                        total_target=face_target,
                        aggression=requested["aggression"],
                        protected_component_triangles=requested[
                            "protected_component_triangles"
                        ],
                    )
                    candidate.update_faces(candidate.nondegenerate_faces())
                    candidate.remove_unreferenced_vertices()
                    candidate.export(candidate_path, file_type="stl")
                    exported = trimesh.load_mesh(candidate_path, process=True)
                    if not isinstance(exported, trimesh.Trimesh):
                        raise RuntimeError("simplified STL did not reload as one mesh")
                    candidate_topology = _topology(exported)
                    topology_checks = _topology_checks(
                        source_topology,
                        candidate_topology,
                    )
                    attempt.update(
                        {
                            "component_result": component_result,
                            "output_triangles": int(len(exported.faces)),
                            "output_topology": candidate_topology,
                            "checks": topology_checks,
                        }
                    )
                    if len(exported.faces) >= len(source_mesh.faces):
                        attempt["status"] = "rejected"
                        attempt["rejection_stage"] = "no-triangle-reduction"
                        result["attempts"].append(attempt)
                        continue
                    if not all(topology_checks.values()):
                        attempt["status"] = "rejected"
                        attempt["rejection_stage"] = "topology-preflight"
                        result["attempts"].append(attempt)
                        continue
                    del candidate
                    source_components = None
                    release_process_memory()
                    metrics = _measure_candidate(
                        source_mesh,
                        exported,
                        source_topology=source_topology,
                        candidate_topology=candidate_topology,
                        sample_points=requested["sample_points"],
                    )
                    checks = _checks(metrics, requested)
                    attempt.update({"metrics": metrics, "checks": checks})
                    attempt["status"] = (
                        "passed" if all(checks.values()) else "rejected"
                    )
                    result["attempts"].append(attempt)
                    if attempt["status"] == "passed":
                        selected_path = candidate_path
                        break
                except Exception as exc:
                    attempt.update(
                        {
                            "status": "failed",
                            "error": type(exc).__name__,
                            "message": str(exc),
                        }
                    )
                    result["attempts"].append(attempt)

            if selected_path is not None:
                _commit_file(selected_path, output_path)
                result["applied"] = True
                result["decision"] = "simplified"
            else:
                _commit_file(source_path, output_path)
                result["decision"] = "no-safe-candidate-original-retained"

        output_inspection = inspect_stl(output_path).to_dict()
        result.update(
            {
                "status": "passed",
                "output": str(output_path),
                "output_sha256": output_inspection["sha256"],
                "output_inspection": output_inspection,
                "reduction_ratio": 1.0
                - output_inspection["triangle_count"]
                / source_inspection["triangle_count"],
                "limitations": [
                    "Surface deviation is a deterministic sampled bidirectional point-to-triangle measurement, not an exact Hausdorff proof.",
                    "Production slicing and UVtools validation remain required after simplification.",
                ],
            }
        )
    except Exception as exc:
        result.update(
            {
                "status": "failed",
                "error": type(exc).__name__,
                "message": str(exc),
            }
        )
        _write_json_atomic(report_path, result)
        raise
    _write_json_atomic(report_path, result)
    return result


def _measure_candidate(
    source: Any,
    candidate: Any,
    *,
    source_topology: dict[str, Any],
    candidate_topology: dict[str, Any] | None = None,
    sample_points: int,
) -> dict[str, Any]:
    import trimesh

    candidate_topology = candidate_topology or _topology(candidate)
    source_points = _comparison_points(source, sample_points, seed=1729)
    candidate_points = _comparison_points(candidate, sample_points, seed=1730)
    _, source_to_candidate, _ = trimesh.proximity.closest_point(candidate, source_points)
    _, candidate_to_source, _ = trimesh.proximity.closest_point(source, candidate_points)
    distances = np.concatenate((source_to_candidate, candidate_to_source))
    if len(distances) == 0 or not np.all(np.isfinite(distances)):
        raise RuntimeError("simplification deviation query returned invalid distances")
    source_volume = abs(float(source.volume))
    candidate_volume = abs(float(candidate.volume))
    volume_ratio = (
        abs(candidate_volume - source_volume) / source_volume
        if source_volume > 1e-12
        else math.inf
    )
    bounds_change = float(
        np.max(np.abs(np.asarray(candidate.bounds) - np.asarray(source.bounds)))
    )
    return {
        "source_triangles": int(len(source.faces)),
        "output_triangles": int(len(candidate.faces)),
        "reduction_ratio": 1.0 - len(candidate.faces) / len(source.faces),
        "source_topology": source_topology,
        "output_topology": candidate_topology,
        "source_volume_mm3": source_volume,
        "output_volume_mm3": candidate_volume,
        "volume_change_ratio": volume_ratio,
        "bounds_max_change_mm": bounds_change,
        "surface_deviation": {
            "method": "sampled-bidirectional-point-to-triangle",
            "sample_count": int(len(distances)),
            "p95_mm": float(np.percentile(distances, 95)),
            "max_mm": float(np.max(distances)),
            "mean_mm": float(np.mean(distances)),
        },
    }


def _comparison_points(mesh: Any, count: int, *, seed: int) -> np.ndarray:
    import trimesh

    surface_count = max(1, count * 3 // 4)
    vertex_count = min(max(1, count - surface_count), len(mesh.vertices))
    surface, _ = trimesh.sample.sample_surface(mesh, surface_count, seed=seed)
    indices = np.linspace(0, len(mesh.vertices) - 1, vertex_count, dtype=np.int64)
    vertices = np.asarray(mesh.vertices, dtype=np.float64)[indices]
    return np.vstack((surface, vertices))


def _topology(mesh: Any) -> dict[str, Any]:
    return {
        "watertight": bool(mesh.is_watertight),
        "winding_consistent": bool(mesh.is_winding_consistent),
        "body_count": int(mesh.body_count),
        "euler_number": int(mesh.euler_number),
        "degenerate_triangles": int((~mesh.nondegenerate_faces()).sum()),
    }


def _checks(metrics: dict[str, Any], requested: dict[str, Any]) -> dict[str, bool]:
    source = metrics["source_topology"]
    output = metrics["output_topology"]
    deviation = metrics["surface_deviation"]
    return {
        **_topology_checks(source, output),
        "p95_deviation": deviation["p95_mm"]
        <= requested["max_p95_deviation_mm"],
        "maximum_deviation": deviation["max_mm"]
        <= requested["max_deviation_mm"],
        "bounds": metrics["bounds_max_change_mm"]
        <= requested["max_deviation_mm"],
        "volume": metrics["volume_change_ratio"]
        <= requested["max_volume_change_ratio"],
    }


def _topology_checks(
    source: dict[str, Any],
    output: dict[str, Any],
) -> dict[str, bool]:
    return {
        "watertight": output["watertight"],
        "winding_consistent": output["winding_consistent"],
        "body_count_preserved": output["body_count"] == source["body_count"],
        "euler_number_preserved": output["euler_number"] == source["euler_number"],
        "no_degenerate_triangles": output["degenerate_triangles"] == 0,
    }


def _connected_components(mesh: Any, *, expected_count: int) -> list[Any]:
    components = [mesh] if expected_count == 1 else list(
        mesh.split(only_watertight=False)
    )
    if len(components) != expected_count:
        raise RuntimeError("could not resolve every connected mesh component")
    return components


def _simplify_components(
    components: list[Any],
    *,
    total_target: int,
    aggression: int,
    protected_component_triangles: int,
) -> tuple[Any, dict[str, Any]]:
    """Reduce each connected body independently and preserve small details."""

    import trimesh

    if len(components) == 1:
        protected: list[Any] = []
        reducible = list(components)
    else:
        protected = [
            component
            for component in components
            if len(component.faces) < protected_component_triangles
        ]
        reducible = [
            component
            for component in components
            if len(component.faces) >= protected_component_triangles
        ]
    protected_faces = sum(len(component.faces) for component in protected)
    reducible_faces = sum(len(component.faces) for component in reducible)
    available_target = max(4 * len(reducible), total_target - protected_faces)
    ratio = min(1.0, available_target / max(1, reducible_faces))
    selected = list(protected)
    component_attempts: list[dict[str, Any]] = []

    for component in reducible:
        source_faces = int(len(component.faces))
        source_topology = _topology(component)
        desired = max(4, min(source_faces - 1, int(round(source_faces * ratio))))
        chosen = component
        record: dict[str, Any] = {
            "source_triangles": source_faces,
            "requested_triangles": desired,
            "output_triangles": source_faces,
            "decision": "original-component-retained",
        }
        for target in _candidate_targets(source_faces, desired):
            candidate = component.simplify_quadric_decimation(
                face_count=target,
                aggression=aggression,
            )
            candidate.update_faces(candidate.nondegenerate_faces())
            candidate.remove_unreferenced_vertices()
            candidate_topology = _topology(candidate)
            if all(_topology_checks(source_topology, candidate_topology).values()):
                chosen = candidate
                record.update(
                    {
                        "output_triangles": int(len(candidate.faces)),
                        "decision": "simplified",
                        "accepted_target_triangles": target,
                    }
                )
                break
        selected.append(chosen)
        component_attempts.append(record)

    combined = trimesh.util.concatenate(selected)
    return combined, {
        "strategy": "component-aware-quadric-edge-collapse",
        "component_count": len(components),
        "protected_component_triangles": protected_component_triangles,
        "protected_components": len(protected),
        "protected_triangles": protected_faces,
        "reducible_components": len(reducible),
        "allocated_reduction_ratio": 1.0 - ratio,
        "components": component_attempts,
    }


def _candidate_targets(source_faces: int, desired_faces: int) -> list[int]:
    desired = min(source_faces - 1, desired_faces)
    if desired < 4:
        return []
    candidates = [
        desired,
        int(round(math.sqrt(desired * source_faces))),
        int(round(source_faces * 0.75)),
        int(round(source_faces * 0.90)),
    ]
    return sorted({value for value in candidates if desired <= value < source_faces})


def _validated_request(**values: Any) -> dict[str, Any]:
    integer_fields = {
        "minimum_input_triangles": (100, None),
        "target_triangles": (4, None),
        "sample_points": (1000, 200_000),
        "aggression": (0, 10),
        "protected_component_triangles": (4, 1_000_000),
    }
    for key, (minimum, maximum) in integer_fields.items():
        value = values[key]
        if isinstance(value, bool) or int(value) != value:
            raise ValueError(f"{key} must be an integer")
        number = int(value)
        if number < minimum or (maximum is not None and number > maximum):
            raise ValueError(f"{key} is outside the supported range")
        values[key] = number
    for key in (
        "max_p95_deviation_mm",
        "max_deviation_mm",
        "max_volume_change_ratio",
    ):
        value = float(values[key])
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be a positive finite number")
        values[key] = value
    if values["target_triangles"] >= values["minimum_input_triangles"]:
        raise ValueError("target_triangles must be below minimum_input_triangles")
    if values["max_p95_deviation_mm"] > values["max_deviation_mm"]:
        raise ValueError("p95 deviation limit must not exceed maximum deviation")
    return values


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _commit_file(source: Path, destination: Path) -> None:
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    shutil.copy2(source, temporary)
    temporary.replace(destination)
