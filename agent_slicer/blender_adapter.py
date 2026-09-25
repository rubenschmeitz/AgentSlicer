from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .backends import discover_tools
from .processes import run_backend
from .resources import heavy_job, native_subprocess_environment, work_directory
from .voxel_hollow import hollow_and_drain_voxel


_PathType = type(Path())


class _PortablePath(_PathType):
    """Path with stable slash formatting for commands and agent evidence."""

    def __str__(self) -> str:
        return _PathType.__str__(self).replace("\\", "/")


def blender_script(name: str) -> Path:
    if name not in {"hollow.py", "prepare_resin.py"}:
        raise ValueError(f"unknown Blender operation: {name}")
    script = Path(__file__).resolve().parent / "data" / "blender" / name
    if not script.is_file():
        raise RuntimeError(f"missing packaged Blender script: {script}")
    return _PortablePath(script)


@heavy_job
def hollow_and_drain(
    source: str | Path,
    output: str | Path,
    report: str | Path,
    *,
    wall_mm: float = 1.5,
    target_height_mm: float | None = None,
    drain_diameter_mm: float = 3.0,
    drain_count: int = 2,
    blend: str | Path | None = None,
    blender_path: str | None = None,
) -> dict[str, Any]:
    executable = blender_path or discover_tools()["blender"].path
    if not executable:
        raise RuntimeError("Blender executable not found")
    script = blender_script("hollow.py")
    source_path = Path(source).resolve()
    output_path = Path(output).resolve()
    report_path = Path(report).resolve()
    _distinct_paths(source_path, output_path, report_path, blend)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        executable,
        "--background",
        "--factory-startup",
        "--threads", native_subprocess_environment()["AGENT_SLICER_THREADS"],
        "--python",
        _backend_path(script, executable),
        "--",
        "--input",
        _backend_path(source_path, executable),
        "--output",
        _backend_path(output_path, executable),
        "--report",
        _backend_path(report_path, executable),
        "--wall-mm",
        str(wall_mm),
        "--drain-diameter-mm",
        str(drain_diameter_mm),
        "--drain-count",
        str(drain_count),
    ]
    if target_height_mm is not None:
        command.extend(["--target-height-mm", str(target_height_mm)])
    if blend is not None:
        blend_path = Path(blend).resolve()
        blend_path.parent.mkdir(parents=True, exist_ok=True)
        command.extend(["--blend", _backend_path(blend_path, executable)])
    completed = run_backend(
        command,
        timeout=1800,
        env=native_subprocess_environment(),
    )
    if not report_path.is_file():
        raise RuntimeError(
            f"Blender produced no report (exit {completed.returncode}): "
            f"{completed.stderr.strip()}"
        )
    result = json.loads(report_path.read_text(encoding="utf-8"))
    result["command"] = command
    result["exit_code"] = completed.returncode
    result["stdout"] = completed.stdout.strip()
    result["stderr"] = completed.stderr.strip()
    result["process_resources"] = completed.metrics
    if completed.returncode != 0 or result.get("status") != "passed":
        details = result.get("message", completed.stderr.strip())
        if completed.stdout.strip():
            details = f"{details}; Blender output: {completed.stdout.strip()[-4000:]}"
        raise RuntimeError(
            f"Blender hollowing failed: {details}"
        )
    temporary = report_path.with_suffix(report_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(report_path)
    return result


@heavy_job
def prepare_resin_model(
    source: str | Path,
    output: str | Path,
    report: str | Path,
    *,
    target_height_mm: float | None = None,
    repair_method: str = "local-topology",
    voxel_mm: float = 0.12,
    max_p95_deviation_mm: float = 0.01,
    max_deviation_mm: float = 0.05,
    wall_mm: float = 1.8,
    hollow_voxel_mm: float = 0.15,
    hollow_max_p95_change_mm: float = 0.20,
    hollow_max_change_mm: float = 0.60,
    drain_diameter_mm: float = 3.5,
    drain_count: int = 2,
    max_tilt_deg: float = 20.0,
    orient: bool = True,
    hollow: bool = True,
    drain: bool = True,
    blend: str | Path | None = None,
    blender_path: str | None = None,
    timeout: int = 3600,
) -> dict[str, Any]:
    """Repair, orient, hollow, drain, and verify one resin model.

    Blender performs local topology repair and bounded orientation. Hollowing then
    uses a voxel field only for the internal cavity, subtracts that cavity from
    the original exterior, and verifies two local underside drains on the grid
    and mesh. Originals are never modified and the output is committed only
    after every requested stage passes.
    """

    executable = blender_path or discover_tools()["blender"].path
    if not executable:
        raise RuntimeError("Blender executable not found")
    script = blender_script("prepare_resin.py")
    source_path = Path(source).resolve()
    output_path = Path(output).resolve()
    report_path = Path(report).resolve()
    _distinct_paths(source_path, output_path, report_path, blend)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    if hollow and not drain:
        raise ValueError("hollow resin models must be drained")
    if repair_method not in {"local-topology", "measured-voxel"}:
        raise ValueError("unknown repair method")
    if hollow_voxel_mm <= 0:
        raise ValueError("hollow voxel size must be positive")

    result: dict[str, Any] = {
        "schema": "agent-slicer-resin-preparation/v3",
        "status": "running",
        "source": str(source_path),
        "source_sha256": _sha256(source_path),
        "output": str(output_path),
        "requested": {
            "target_height_mm": target_height_mm,
            "repair_method": repair_method,
            "repair_voxel_mm": voxel_mm,
            "repair_max_p95_deviation_mm": max_p95_deviation_mm,
            "repair_max_deviation_mm": max_deviation_mm,
            "orient": orient,
            "max_tilt_deg": max_tilt_deg,
            "hollow": hollow,
            "hollow_voxel_mm": hollow_voxel_mm,
            "hollow_max_p95_change_mm": hollow_max_p95_change_mm,
            "hollow_max_change_mm": hollow_max_change_mm,
            "wall_mm": wall_mm,
            "drain": drain if hollow else False,
            "drain_diameter_mm": drain_diameter_mm,
            "drain_count": drain_count,
        },
        "stages": {},
    }
    try:
        with tempfile.TemporaryDirectory(
            prefix="agent-slicer-prepare-", dir=work_directory("prepare")
        ) as temporary:
            transaction = Path(temporary)
            repaired_output = transaction / "repaired-oriented.stl"
            blender_report = transaction / "repair-orient.json"
            command = [
                executable,
                "--background",
                "--factory-startup",
                "--threads", native_subprocess_environment()["AGENT_SLICER_THREADS"],
                "--python",
                _backend_path(script, executable),
                "--",
                "--input",
                _backend_path(source_path, executable),
                "--output",
                _backend_path(repaired_output, executable),
                "--report",
                _backend_path(blender_report, executable),
                "--voxel-mm",
                str(voxel_mm),
                "--repair-method",
                repair_method,
                "--max-p95-deviation-mm",
                str(max_p95_deviation_mm),
                "--max-deviation-mm",
                str(max_deviation_mm),
                "--wall-mm",
                str(wall_mm),
                "--drain-diameter-mm",
                str(drain_diameter_mm),
                "--drain-count",
                str(drain_count),
                "--max-tilt-deg",
                str(max_tilt_deg),
                "--no-hollow",
                "--no-drain",
            ]
            if target_height_mm is not None:
                command.extend(["--target-height-mm", str(target_height_mm)])
            if not orient:
                command.append("--no-orient")
            if blend is not None:
                blend_path = Path(blend).resolve()
                blend_path.parent.mkdir(parents=True, exist_ok=True)
                command.extend(["--blend", _backend_path(blend_path, executable)])
                result["blend"] = {
                    "path": str(blend_path),
                    "contains": "repaired-oriented-solid-before-cavity-subtraction",
                }
            completed = run_backend(
                command,
                timeout=timeout,
                env=native_subprocess_environment(),
            )
            if blender_report.is_file():
                backend = json.loads(blender_report.read_text(encoding="utf-8"))
            else:
                backend = {
                    "schema": "agent-slicer-blender-resin-preparation/v1",
                    "status": "failed",
                    "message": "Blender produced no repair/orientation report",
                }
            backend.update(
                {
                    "command": command,
                    "exit_code": completed.returncode,
                    "stdout": completed.stdout.strip(),
                    "stderr": completed.stderr.strip(),
                    "process_resources": completed.metrics,
                }
            )
            result["stages"]["repair_and_orientation"] = backend
            if completed.returncode != 0 or backend.get("status") != "passed":
                details = backend.get("message", completed.stderr.strip())
                raise RuntimeError(f"Blender repair/orientation failed: {details}")
            if not repaired_output.is_file() or repaired_output.stat().st_size <= 84:
                raise RuntimeError("Blender repair/orientation produced no valid STL")

            candidate = transaction / "prepared.stl"
            if hollow:
                voxel_report = transaction / "voxel-hollow.json"
                hollow_attempts: list[dict[str, Any]] = []
                hollow_result = None
                pitches = _adaptive_hollow_pitches(hollow_voxel_mm)
                for pitch in pitches:
                    candidate.unlink(missing_ok=True)
                    voxel_report.unlink(missing_ok=True)
                    try:
                        hollow_result = hollow_and_drain_voxel(
                            repaired_output,
                            candidate,
                            voxel_report,
                            wall_mm=wall_mm,
                            voxel_mm=pitch,
                            drain_diameter_mm=drain_diameter_mm,
                            drain_count=drain_count,
                            max_outer_p95_change_mm=hollow_max_p95_change_mm,
                            max_outer_change_mm=hollow_max_change_mm,
                        )
                    except RuntimeError as exc:
                        hollow_attempts.append(
                            {
                                "voxel_mm": pitch,
                                "status": "failed",
                                "error": str(exc),
                            }
                        )
                        if pitch == pitches[-1] or not _is_refinable_hollow_error(exc):
                            raise
                        continue
                    hollow_attempts.append(
                        {"voxel_mm": pitch, "status": "passed"}
                    )
                    break
                assert hollow_result is not None
                hollow_result["adaptive_refinement"] = {
                    "requested_voxel_mm": hollow_voxel_mm,
                    "selected_voxel_mm": hollow_result["voxel_mm"],
                    "refined": hollow_result["voxel_mm"] != hollow_voxel_mm,
                    "attempts": hollow_attempts,
                }
                result["stages"]["hollow_and_drain"] = hollow_result
            else:
                shutil.copy2(repaired_output, candidate)
            result["export_validation"] = _validate_export(candidate)
            if not candidate.is_file() or candidate.stat().st_size <= 84:
                raise RuntimeError("resin preparation produced no valid STL")
            _commit_file(candidate, output_path)
        result["status"] = "passed"
        result["output_sha256"] = _sha256(output_path)
        result["capabilities"] = [
            "mesh.repair.measured",
            "mesh.repair.safe",
            *(["mesh.orient.resin"] if orient else []),
            *(
                ["mesh.hollow", "mesh.drain.auto", "mesh.cavity.validate"]
                if hollow
                else []
            ),
        ]
        result["limitations"] = [
            "Local repair preserves surfaces outside bounded patches; explicit voxel reconstruction is lossy.",
            "Orientation is a bounded heuristic, not a global optimum.",
            "Drain topology is verified computationally; physical flushing and curing remain operator responsibilities.",
        ]
    except Exception as exc:
        import traceback
        result["status"] = "failed"
        result["error"] = type(exc).__name__
        result["message"] = str(exc)
        result["traceback"] = traceback.format_exc()
        _write_json(report_path, result)
        raise
    _write_json(report_path, result)
    return result


def _validate_export(path: Path) -> dict[str, Any]:
    import numpy as np
    import trimesh

    mesh = trimesh.load_mesh(path, process=True)
    if (not isinstance(mesh, trimesh.Trimesh) or mesh.is_empty
            or not np.isfinite(mesh.vertices).all()
            or not mesh.is_watertight or not mesh.is_winding_consistent):
        raise RuntimeError("prepared STL failed export/reload topology validation")
    return {"status": "passed", "watertight": True, "winding_consistent": True,
            "faces": int(len(mesh.faces)), "bounds_mm": mesh.bounds.tolist()}


def _backend_path(path: Path, executable: str) -> str:
    if executable.lower().endswith(".exe") and str(path).startswith("/mnt/"):
        completed = subprocess.run(
            ["wslpath", "-w", str(path)],
            check=True,
            text=True,
            capture_output=True,
        )
        return completed.stdout.strip()
    return str(path)


def _adaptive_hollow_pitches(requested: float) -> list[float]:
    pitches = [round(float(requested), 6)]
    for candidate in (round(requested * 0.8, 3), round(requested * 2.0 / 3.0, 3)):
        candidate = max(0.10, candidate)
        if candidate < pitches[-1] - 1e-9:
            pitches.append(candidate)
    return pitches


def _distinct_paths(source: Path, output: Path, report: Path, blend: str | Path | None) -> None:
    paths = [source, output, report]
    if blend is not None:
        paths.append(Path(blend).resolve())
    if len(paths) != len(set(paths)):
        raise ValueError("source, output, report, and blend must be distinct files")


def _is_refinable_hollow_error(error: RuntimeError) -> bool:
    message = str(error).lower()
    return "outer change" in message or "outer bounds" in message


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


def _commit_file(source: Path, destination: Path) -> None:
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    shutil.copy2(source, temporary)
    temporary.replace(destination)
