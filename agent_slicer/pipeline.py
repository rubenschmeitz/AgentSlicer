from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import hashlib
from pathlib import Path
from typing import Any

from .backends import available_capabilities, discover_tools
from .blender_adapter import prepare_resin_model
from .mesh_simplify import SIMPLIFICATION_SCHEMA, simplify_mesh_measured
from .models import BuildPlan, SourceModel
from .presets import resolve_presets
from .project import load_project, sha256_file
from .resources import heavy_job, native_subprocess_environment, release_process_memory
from .processes import run_backend
from .stl import PlatePackingError, inspect_stl, pack_shelf, write_combined_binary_stl
from .validation import validate_sliced_output, write_validation
from .uvtools_adapter import analyze_sliced_file, evaluate_production_analysis
from .uvtools_contract import has_complete_detection_contract

DEFAULT_RESIN_SLICE_TIMEOUT_SECONDS = 3 * 60 * 60


class BuildBlocked(RuntimeError):
    def __init__(self, plan: BuildPlan):
        super().__init__("build is blocked; inspect plan.blockers")
        self.plan = plan


class BuildRejected(RuntimeError):
    def __init__(self, result: dict[str, Any]):
        super().__init__("production validation rejected the sliced output")
        self.result = result


def compile_plan(project: str | Path) -> tuple[Path, BuildPlan]:
    root, manifest = load_project(project)
    resolved = resolve_presets(
        process=manifest.process,
        machine=manifest.machine,
        material=manifest.material,
        purpose=manifest.purpose,
        quality=manifest.quality,
        output_format=manifest.output_format,
        overrides=manifest.overrides,
    )
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    inspections = []
    if not manifest.models:
        blockers.append({"code": "PROJECT_NO_MODELS", "message": "project has no models"})
    for model in manifest.models:
        source = (root / model.source).resolve()
        if not source.is_file():
            blockers.append(
                {"code": "SOURCE_MISSING", "model": model.id, "path": str(source)}
            )
            continue
        digest = sha256_file(source)
        if digest != model.sha256:
            blockers.append(
                {
                    "code": "SOURCE_HASH_CHANGED",
                    "model": model.id,
                    "expected": model.sha256,
                    "actual": digest,
                }
            )
            continue
        inspection = inspect_stl(source)
        inspections.append(inspection)
        if inspection.units_ambiguous and model.target_height_mm is None:
            blockers.append(
                {
                    "code": "MODEL_UNITS_AMBIGUOUS",
                    "model": model.id,
                    "dimensions": inspection.dimensions,
                    "message": "set target_height_mm before building",
                }
            )
        if inspection.degenerate_triangles:
            warnings.append(
                {
                    "code": "MODEL_DEGENERATE_TRIANGLES",
                    "model": model.id,
                    "count": inspection.degenerate_triangles,
                    "resolution": "native plate writer will remove them",
                }
            )

    tools = discover_tools()
    capabilities = available_capabilities(tools)
    required = {
        "mesh.inspect",
        "mesh.clean.degenerate",
        "mesh.scale",
        "plate.pack.shelf",
        "mesh.combine",
    }
    if manifest.process == "resin":
        required.update({"resin.slice", "resin.validate.basic"})
    elif manifest.process == "fdm":
        required.update({"fdm.slice", "fdm.validate.basic"})
    required.update(resolved["purpose"].get("required_capabilities", []))
    if (
        resolved["purpose"]
        .get("geometry", {})
        .get("simplification", {})
        .get("enabled", False)
    ):
        required.add("mesh.simplify.measured")
    if resolved["purpose"].get("supports", {}).get("enabled", True):
        required.add(
            "resin.support.generate"
            if manifest.process == "resin"
            else "fdm.support.generate"
        )
    for missing in sorted(required - capabilities):
        blockers.append(
            {
                "code": "CAPABILITY_UNAVAILABLE",
                "capability": missing,
                "message": "no implemented backend currently provides this capability",
            }
        )

    placements = []
    if len(inspections) == len(manifest.models) and manifest.models:
        machine = resolved["machine"]
        try:
            placements = pack_shelf(
                root=root,
                models=manifest.models,
                inspections=inspections,
                plate_size_mm=tuple(machine["build_volume_mm"]),
                clearance_mm=float(resolved["purpose"]["arrangement"]["clearance_mm"]),
                margin_mm=float(machine["margin_mm"]),
                support_footprint_margin_mm=float(
                    resolved["purpose"]["arrangement"].get(
                        "support_footprint_margin_mm", 0.0
                    )
                ),
            )
        except PlatePackingError as exc:
            blockers.append({"code": "PLATE_PACKING_FAILED", "message": str(exc)})

    operations = [
        {
            "id": capability.replace(".", "-"),
            "capability": capability,
            "status": "ready" if capability in capabilities else "blocked",
        }
        for capability in sorted(required)
    ]
    plan = BuildPlan(
        project=str(root),
        resolved_config=resolved,
        inspections=[item.to_dict() for item in inspections],
        placements=[item.to_dict() for item in placements],
        required_capabilities=sorted(required),
        available_capabilities=sorted(capabilities),
        blockers=blockers,
        warnings=warnings,
        operations=operations,
    )
    return root, plan


def write_plan(root: Path, plan: BuildPlan) -> Path:
    path = root / "artifacts" / "plan.json"
    _write_json_atomic(path, plan.to_dict())
    _write_json_atomic(root / "artifacts" / "resolved-config.json", plan.resolved_config)
    return path


@heavy_job
def build_project(
    project: str | Path,
    *,
    resin_slicer_path: str | None = None,
    uvtools_path: str | None = None,
    work_root: str | Path | None = None,
    uvtools_workers: int | None = None,
    skip_slice: bool = False,
    reuse_prepared: bool = False,
    resume_rejected: bool = False,
) -> dict[str, Any]:
    root, plan = compile_plan(project)
    write_plan(root, plan)
    blockers = list(plan.blockers)
    if skip_slice:
        blockers = [
            blocker
            for blocker in blockers
            if blocker.get("capability") not in {
                "resin.slice",
                "resin.validate.basic",
                "resin.validate.production",
            }
        ]
    if blockers:
        plan.blockers = blockers
        raise BuildBlocked(plan)

    _, manifest = load_project(root)
    artifacts = root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    previous_build: dict[str, Any] | None = None
    previous_evidence_path: Path | None = None
    previous_manifest_sha256: str | None = None
    previous_manifest_path = artifacts / "build-manifest.json"
    if resume_rejected:
        if skip_slice:
            raise ValueError("--resume-rejected cannot be combined with --skip-slice")
        if not previous_manifest_path.is_file():
            raise RuntimeError("no previous build manifest is available to resume")
        previous_build = json.loads(previous_manifest_path.read_text(encoding="utf-8"))
        if previous_build.get("status") != "rejected":
            raise RuntimeError("only a previously rejected build can be resumed")
        previous_manifest_sha256 = sha256_file(previous_manifest_path)
        evidence_dir = artifacts / "resume-evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        previous_evidence_path = evidence_dir / f"{previous_manifest_sha256}.json"
        if not previous_evidence_path.exists():
            shutil.copy2(previous_manifest_path, previous_evidence_path)
    resolved = plan.resolved_config
    purpose = resolved["purpose"]
    transaction_parent = _build_work_root(artifacts, work_root)
    with tempfile.TemporaryDirectory(
        prefix="agent-slicer-build-", dir=transaction_parent
    ) as temp:
        transaction = Path(temp)
        models = manifest.models
        inspections = [inspect_stl(root / model.source) for model in models]
        preparation_results: list[dict[str, Any]] = []
        if _requires_geometry_preparation(purpose):
            models, inspections, preparation_results = _prepare_models(
                root=root,
                models=manifest.models,
                transaction=transaction,
                geometry=purpose["geometry"],
                reuse_cached=reuse_prepared,
            )
        committed_preparation = _commit_preparation_results(
            artifacts,
            preparation_results,
        )
        simplification_results: list[dict[str, Any]] = []
        simplification = purpose.get("geometry", {}).get("simplification", {})
        if simplification.get("enabled", False):
            models, inspections, simplification_results = _simplify_models(
                root=root,
                models=models,
                transaction=transaction,
                settings=_simplification_settings(simplification),
                reuse_cached=reuse_prepared,
            )
        committed_simplification = _commit_simplification_results(
            artifacts,
            simplification_results,
        )
        placements = pack_shelf(
            root=root,
            models=models,
            inspections=inspections,
            plate_size_mm=tuple(resolved["machine"]["build_volume_mm"]),
            clearance_mm=float(purpose["arrangement"]["clearance_mm"]),
            margin_mm=float(resolved["machine"]["margin_mm"]),
            support_footprint_margin_mm=float(
                purpose["arrangement"].get("support_footprint_margin_mm", 0.0)
            ),
        )
        plan.placements = [placement.to_dict() for placement in placements]
        write_plan(root, plan)
        plate_temp = transaction / "plate.stl"
        mesh_stats = write_combined_binary_stl(plate_temp, placements)
        release_process_memory()
        plate_temp_sha256 = sha256_file(plate_temp)
        resumed_from: dict[str, Any] | None = None
        seed_validation: dict[str, Any] | None = None
        if previous_build is not None:
            if previous_build.get("plate_sha256") != plate_temp_sha256:
                raise RuntimeError(
                    "the current prepared plate differs from the rejected build; "
                    "a feedback resume would be unsafe"
                )
            rejected_output = Path(str(previous_build.get("sliced_output", "")))
            if not rejected_output.is_file():
                raise RuntimeError("the rejected CTB required for feedback resume is missing")
            rejected_sha256 = sha256_file(rejected_output)
            assert previous_evidence_path is not None
            assert previous_manifest_sha256 is not None
            seed_analysis_path = previous_evidence_path.parent / (
                f"{rejected_sha256}-uvtools-analysis-v4.json"
            )
            seed_analysis: dict[str, Any] | None = None
            if seed_analysis_path.is_file():
                try:
                    cached_analysis = json.loads(
                        seed_analysis_path.read_text(encoding="utf-8")
                    )
                    if (
                        cached_analysis.get("schema")
                        == "agent-slicer-uvtools-analysis/v4"
                        and has_complete_detection_contract(cached_analysis)
                        and cached_analysis.get("source_sha256") == rejected_sha256
                    ):
                        seed_analysis = cached_analysis
                except (OSError, json.JSONDecodeError):
                    pass
            if seed_analysis is None:
                seed_analysis = _embedded_analysis_for_sha(
                    previous_build,
                    rejected_sha256,
                )
                if seed_analysis is not None:
                    _write_json_atomic(seed_analysis_path, seed_analysis)
            if seed_analysis is None:
                seed_analysis = analyze_sliced_file(
                    rejected_output,
                    uvtools_path=uvtools_path,
                    work_root=transaction_parent,
                    workers=uvtools_workers,
                )
                _write_json_atomic(seed_analysis_path, seed_analysis)
            seed_validation = evaluate_production_analysis(seed_analysis, resolved)
            resumed_from = {
                "manifest": str(previous_evidence_path.resolve()),
                "manifest_sha256": previous_manifest_sha256,
                "rejected_output": str(rejected_output.resolve()),
                "rejected_output_sha256": rejected_sha256,
                "seed_analysis": str(seed_analysis_path.resolve()),
                "seed_analysis_sha256": sha256_file(seed_analysis_path),
                "plate_sha256": plate_temp_sha256,
                "seed_validation_status": seed_validation["status"],
            }
        plate_final = artifacts / "plate.stl"
        slice_result = None
        output_final = None
        output_temp = None
        validation = None
        production_validation = None
        attempts: list[dict[str, Any]] = []
        targeted_support_points: list[tuple[float, float, float]] = []
        support_plan_cache_dir = transaction_parent.parent / "resin-support-plans"
        if not skip_slice:
            variants = _support_variants(purpose["supports"])
            if seed_validation is not None:
                carried_points = _resume_targeted_support_points(
                    previous_build or {},
                    resolved,
                )
                targeted_support_points = _targeted_support_points(
                    seed_validation,
                    resolved,
                    existing=carried_points,
                )
                if not targeted_support_points:
                    raise RuntimeError(
                        "the rejected CTB produced no actionable support feedback"
                    )
                feedback_passes = max(
                    0, int(purpose["supports"].get("feedback_passes", 0))
                )
                variants = [dict(variants[-1]) for _ in range(1 + feedback_passes)]
            for attempt_index, supports in enumerate(variants, start=1):
                # A previous UVtools pass can leave large decoded-layer arrays in
                # allocator arenas.  Release them before the next full-resolution
                # support analysis so refinement retries stay within one bounded
                # memory envelope.
                release_process_memory()
                candidate = transaction / (
                    f"{_safe_name(plan.project)}-attempt-{attempt_index}."
                    f"{resolved['output_format']}"
                )
                slice_result = _slice_resin(
                    plate_temp,
                    candidate,
                    resolved,
                    support_config=supports,
                    manual_support_points=targeted_support_points,
                    require_manual_supports=False,
                    resin_slicer_path=resin_slicer_path,
                    support_plan_cache_dir=support_plan_cache_dir,
                )
                validation = validate_sliced_output(candidate, resolved["output_format"])
                if validation["status"] != "passed":
                    raise RuntimeError("sliced output failed basic validation")
                attempt: dict[str, Any] = {
                    "attempt": attempt_index,
                    "path": str(candidate),
                    "sha256": sha256_file(candidate),
                    "supports": supports,
                    "targeted_support_points": targeted_support_points,
                    "slice": slice_result,
                    "basic_validation": validation,
                }
                if "resin.validate.production" in plan.required_capabilities:
                    analysis = analyze_sliced_file(
                        candidate,
                        uvtools_path=uvtools_path,
                        work_root=transaction_parent,
                        workers=uvtools_workers,
                    )
                    production_validation = evaluate_production_analysis(
                        analysis,
                        resolved,
                    )
                    attempt["production_validation"] = production_validation
                attempts.append(attempt)
                output_temp = candidate
                if (
                    production_validation is None
                    or production_validation["status"] == "passed"
                ):
                    break
                next_targeted_support_points = _targeted_support_points(
                    production_validation,
                    resolved,
                    existing=targeted_support_points,
                )
                # A repeated final-density pass is useful only when detector
                # feedback contributed at least one new contact.  If neither
                # settings nor contacts would change, fail closed now instead
                # of producing the same rejected CTB again.
                next_variant = (
                    variants[attempt_index]
                    if attempt_index < len(variants)
                    else None
                )
                if (
                    next_variant == supports
                    and next_targeted_support_points == targeted_support_points
                ):
                    break
                targeted_support_points = next_targeted_support_points
            assert output_temp is not None

        _commit_file(plate_temp, plate_final)
        rejected = bool(
            production_validation is not None
            and production_validation["status"] != "passed"
        )
        if output_temp is not None:
            assert validation is not None
            if rejected:
                candidate_hash = sha256_file(output_temp)
                candidate_dir = artifacts / "rejected" / candidate_hash[:12]
                candidate_dir.mkdir(parents=True, exist_ok=True)
                output_final = candidate_dir / output_temp.name
                shutil.copy2(output_temp, output_final)
                shutil.copy2(plate_final, candidate_dir / "plate.stl")
                write_validation(candidate_dir / "validation.json", validation)
                assert production_validation is not None
                _write_json_atomic(
                    candidate_dir / "production-validation.json",
                    production_validation,
                )
            else:
                output_final = artifacts / (
                    f"{_safe_name(plan.project)}.{resolved['output_format']}"
                )
                _commit_file(output_temp, output_final)
                write_validation(artifacts / "validation.json", validation)
                if production_validation is not None:
                    _write_json_atomic(
                        artifacts / "production-validation.json",
                        production_validation,
                    )
            validation["path"] = str(output_final.resolve())

        plate_inspection = inspect_stl(plate_final)
        result: dict[str, Any] = {
            "schema": "agent-slicer-build/v1",
            "feedback_coordinate_system": "ctb-raster-native-v1",
            "status": "prepared" if skip_slice else ("rejected" if rejected else "completed"),
            "project": str(root),
            "plate": str(plate_final),
            "plate_sha256": sha256_file(plate_final),
            "mesh_stats": mesh_stats,
            "plate_inspection": plate_inspection.to_dict(),
            "warnings": plan.warnings,
            "geometry_preparation": committed_preparation,
            "mesh_simplification": committed_simplification,
            "slice_attempts": attempts,
            "work_root": str(transaction_parent),
            "uvtools_executable": (
                uvtools_path or discover_tools()["uvtools"].path
            ),
            "resin_support_plan_cache": str(support_plan_cache_dir.resolve()),
        }
        if resumed_from is not None:
            result["resumed_from_rejected_build"] = resumed_from
        if output_final is not None:
            result.update(
                {
                    "sliced_output": str(output_final),
                    "sliced_sha256": sha256_file(output_final),
                    "slice": slice_result,
                    "validation": validation,
                }
            )
        if production_validation is not None:
            result["production_validation"] = production_validation
    _write_json_atomic(artifacts / "build-manifest.json", result)
    _write_markdown_report(root / "reports" / "build-report.md", result, plan)
    if result["status"] == "rejected":
        raise BuildRejected(result)
    return result


def _requires_geometry_preparation(purpose: dict[str, Any]) -> bool:
    geometry = purpose.get("geometry", {})
    return bool(
        geometry.get("repair_method")
        or geometry.get("orient")
        or geometry.get("hollow")
        or geometry.get("drain")
    )


def _preparation_arguments(
    model: SourceModel,
    geometry: dict[str, Any],
) -> dict[str, Any]:
    return {
        "target_height_mm": model.target_height_mm,
        "repair_method": str(geometry.get("repair_method") or "local-topology"),
        "voxel_mm": float(geometry.get("repair_voxel_mm", 0.12)),
        "max_p95_deviation_mm": float(
            geometry.get("repair_max_p95_deviation_mm", 0.01)
        ),
        "max_deviation_mm": float(geometry.get("repair_max_deviation_mm", 0.05)),
        "wall_mm": float(geometry.get("wall_mm", 1.8)),
        "hollow_voxel_mm": float(geometry.get("hollow_voxel_mm", 0.15)),
        "hollow_max_p95_change_mm": float(
            geometry.get("hollow_max_p95_change_mm", 0.20)
        ),
        "hollow_max_change_mm": float(
            geometry.get("hollow_max_change_mm", 0.60)
        ),
        "drain_diameter_mm": float(
            geometry.get("drain_hole_diameter_mm", 3.5)
        ),
        "drain_count": int(geometry.get("drain_hole_count", 2)),
        "max_tilt_deg": float(geometry.get("max_tilt_deg", 20.0)),
        "orient": bool(geometry.get("orient", False)),
        "hollow": bool(geometry.get("hollow", False)),
        "drain": bool(geometry.get("drain", False)),
    }


def _expected_preparation_request(arguments: dict[str, Any]) -> dict[str, Any]:
    hollow = bool(arguments["hollow"])
    return {
        "target_height_mm": arguments["target_height_mm"],
        "repair_method": arguments.get("repair_method", "local-topology"),
        "repair_voxel_mm": arguments["voxel_mm"],
        "repair_max_p95_deviation_mm": arguments["max_p95_deviation_mm"],
        "repair_max_deviation_mm": arguments["max_deviation_mm"],
        "orient": arguments["orient"],
        "max_tilt_deg": arguments["max_tilt_deg"],
        "hollow": hollow,
        "hollow_voxel_mm": arguments["hollow_voxel_mm"],
        "hollow_max_p95_change_mm": arguments["hollow_max_p95_change_mm"],
        "hollow_max_change_mm": arguments["hollow_max_change_mm"],
        "wall_mm": arguments["wall_mm"],
        "drain": bool(arguments["drain"]) if hollow else False,
        "drain_diameter_mm": arguments["drain_diameter_mm"],
        "drain_count": arguments["drain_count"],
    }


def _find_cached_preparation(
    root: Path,
    model: SourceModel,
    arguments: dict[str, Any],
) -> tuple[Path, Path, dict[str, Any]] | None:
    """Find an exact, passed preparation result without weakening its gates."""

    cache = root / "artifacts" / "prepared"
    expected = _expected_preparation_request(arguments)
    candidates = sorted(
        cache.glob(f"{model.id}-*.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for report in candidates:
        mesh = report.with_suffix(".stl")
        if not mesh.is_file():
            continue
        try:
            result = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            result.get("schema") != "agent-slicer-resin-preparation/v3"
            or result.get("status") != "passed"
            or result.get("export_validation", {}).get("status") != "passed"
            or result.get("source_sha256") != model.sha256
            or result.get("requested") != expected
            or result.get("output_sha256") != sha256_file(mesh)
        ):
            continue
        repair = result.get("stages", {}).get("repair_and_orientation", {})
        if repair.get("status") != "passed":
            continue
        if expected["hollow"]:
            hollow = result.get("stages", {}).get("hollow_and_drain", {})
            holes = hollow.get("drain_holes", [])
            if (
                hollow.get("status") != "passed"
                or not hollow.get("outer_surface_preserved")
                or hollow.get("sealed_cavity_voxels") != 0
                or len(holes) != int(expected["drain_count"])
                or not all(hole.get("independently_connects_cavity") for hole in holes)
                or not all(hole.get("mesh_independently_connects_cavity") for hole in holes)
                or not hollow.get("mesh_validation", {}).get("watertight")
                or not hollow.get("mesh_validation", {}).get("winding_consistent")
            ):
                continue
        return mesh, report, result
    return None


def _prepare_models(
    *,
    root: Path,
    models: list[SourceModel],
    transaction: Path,
    geometry: dict[str, Any],
    reuse_cached: bool = False,
) -> tuple[list[SourceModel], list[Any], list[dict[str, Any]]]:
    prepared_dir = transaction / "prepared"
    prepared_dir.mkdir(parents=True, exist_ok=True)
    prepared_models: list[SourceModel] = []
    inspections = []
    results: list[dict[str, Any]] = []
    for model in models:
        output = prepared_dir / f"{model.id}.stl"
        report = prepared_dir / f"{model.id}.json"
        preparation_arguments = _preparation_arguments(model, geometry)
        cached = (
            _find_cached_preparation(root, model, preparation_arguments)
            if reuse_cached
            else None
        )
        try:
            if cached is not None:
                cached_mesh, cached_report, result = cached
                shutil.copy2(cached_mesh, output)
                shutil.copy2(cached_report, report)
            else:
                result = prepare_resin_model(
                    root / model.source,
                    output,
                    report,
                    **preparation_arguments,
                )
        except Exception as exc:
            failure_dir = root / "artifacts" / "failed-preparation"
            failure_dir.mkdir(parents=True, exist_ok=True)
            failure_report = failure_dir / f"{model.id}-{model.sha256[:12]}.json"
            if report.is_file():
                shutil.copy2(report, failure_report)
            else:
                _write_json_atomic(
                    failure_report,
                    {
                        "schema": "agent-slicer-preparation-failure/v1",
                        "status": "failed",
                        "model_id": model.id,
                        "source": str((root / model.source).resolve()),
                        "error": type(exc).__name__,
                        "message": str(exc),
                    },
                )
            raise RuntimeError(
                f"geometry preparation failed for {model.id}; "
                f"diagnostic report: {failure_report}"
            ) from exc
        digest = sha256_file(output)
        prepared_models.append(
            SourceModel(
                id=model.id,
                source=str(output),
                sha256=digest,
                target_height_mm=None,
                quantity=model.quantity,
            )
        )
        inspections.append(inspect_stl(output))
        results.append(
            {
                "model_id": model.id,
                "source": str((root / model.source).resolve()),
                "prepared": str(output),
                "prepared_sha256": digest,
                "report": str(report),
                "result": result,
                "cache_reused": cached is not None,
            }
        )
    return prepared_models, inspections, results


def _commit_preparation_results(
    artifacts: Path,
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not results:
        return []
    destination = artifacts / "prepared"
    destination.mkdir(parents=True, exist_ok=True)
    committed: list[dict[str, Any]] = []
    for item in results:
        suffix = item["prepared_sha256"][:12]
        mesh_target = destination / f"{item['model_id']}-{suffix}.stl"
        report_target = destination / f"{item['model_id']}-{suffix}.json"
        shutil.copy2(item["prepared"], mesh_target)
        shutil.copy2(item["report"], report_target)
        committed.append(
            {
                "model_id": item["model_id"],
                "source": item["source"],
                "prepared": str(mesh_target.resolve()),
                "prepared_sha256": item["prepared_sha256"],
                "report": str(report_target.resolve()),
                "status": item["result"]["status"],
                "cache_reused": bool(item.get("cache_reused", False)),
            }
        )
    return committed


def _simplification_settings(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "minimum_input_triangles": int(value.get("minimum_input_triangles", 500_000)),
        "target_triangles": int(value.get("target_triangles", 350_000)),
        "max_p95_deviation_mm": float(value.get("max_p95_deviation_mm", 0.035)),
        "max_deviation_mm": float(value.get("max_deviation_mm", 0.10)),
        "max_volume_change_ratio": float(value.get("max_volume_change_ratio", 0.005)),
        "sample_points": int(value.get("sample_points", 20_000)),
        "aggression": int(value.get("aggression", 5)),
        "protected_component_triangles": int(
            value.get("protected_component_triangles", 5_000)
        ),
    }


def _find_cached_simplification(
    root: Path,
    model: SourceModel,
    settings: dict[str, Any],
) -> tuple[Path | None, Path, dict[str, Any]] | None:
    cache = root / "artifacts" / "simplified"
    if not cache.is_dir():
        return None
    for report in sorted(cache.glob(f"{model.id}-*.json"), reverse=True):
        try:
            result = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            result.get("schema") != SIMPLIFICATION_SCHEMA
            or result.get("status") != "passed"
            or result.get("source_sha256") != model.sha256
            or result.get("requested") != settings
        ):
            continue
        output_hash = result.get("output_sha256")
        if not isinstance(output_hash, str):
            continue
        if result.get("applied"):
            mesh = report.with_suffix(".stl")
            if mesh.is_file() and sha256_file(mesh) == output_hash:
                return mesh, report, result
            continue
        if (
            result.get("decision")
            in {
                "below-threshold-original-retained",
                "no-safe-candidate-original-retained",
            }
            and output_hash == model.sha256
        ):
            return None, report, result
    return None


def _simplify_models(
    *,
    root: Path,
    models: list[SourceModel],
    transaction: Path,
    settings: dict[str, Any],
    reuse_cached: bool,
) -> tuple[list[SourceModel], list[Any], list[dict[str, Any]]]:
    simplified_dir = transaction / "simplified"
    simplified_dir.mkdir(parents=True, exist_ok=True)
    selected_models: list[SourceModel] = []
    inspections = []
    results: list[dict[str, Any]] = []
    for model in models:
        input_source = (
            Path(model.source)
            if Path(model.source).is_absolute()
            else root / model.source
        )
        output = simplified_dir / f"{model.id}.stl"
        report = simplified_dir / f"{model.id}.json"
        cached = (
            _find_cached_simplification(root, model, settings)
            if reuse_cached
            else None
        )
        if cached is not None:
            cached_mesh, cached_report, result = cached
            if cached_mesh is not None:
                shutil.copy2(cached_mesh, output)
            shutil.copy2(cached_report, report)
        else:
            result = simplify_mesh_measured(
                input_source,
                output,
                report,
                **settings,
            )
        applied = bool(result.get("applied"))
        selected_source = output if applied else input_source
        selected_hash = sha256_file(selected_source)
        selected_models.append(
            SourceModel(
                id=model.id,
                source=str(selected_source),
                sha256=selected_hash,
                target_height_mm=model.target_height_mm,
                quantity=model.quantity,
            )
        )
        inspections.append(inspect_stl(selected_source))
        results.append(
            {
                "model_id": model.id,
                "source": str(input_source.resolve()),
                "source_sha256": model.sha256,
                "simplified": str(output) if applied else None,
                "simplified_sha256": selected_hash if applied else None,
                "report": str(report),
                "result": result,
                "applied": applied,
                "cache_reused": cached is not None,
            }
        )
    return selected_models, inspections, results


def _commit_simplification_results(
    artifacts: Path,
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not results:
        return []
    destination = artifacts / "simplified"
    destination.mkdir(parents=True, exist_ok=True)
    committed: list[dict[str, Any]] = []
    for item in results:
        if item["applied"]:
            suffix = item["simplified_sha256"][:12]
            mesh_target = destination / f"{item['model_id']}-{suffix}.stl"
            report_target = destination / f"{item['model_id']}-{suffix}.json"
            shutil.copy2(item["simplified"], mesh_target)
        else:
            suffix = item["source_sha256"][:12]
            mesh_target = None
            report_target = destination / f"{item['model_id']}-{suffix}-retained.json"
        report_payload = json.loads(Path(item["report"]).read_text(encoding="utf-8"))
        report_payload["output"] = str(
            mesh_target.resolve() if mesh_target else Path(item["source"]).resolve()
        )
        report_target.write_text(
            json.dumps(report_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        committed.append(
            {
                "model_id": item["model_id"],
                "source": item["source"],
                "source_sha256": item["source_sha256"],
                "simplified": str(mesh_target.resolve()) if mesh_target else None,
                "simplified_sha256": item["simplified_sha256"],
                "report": str(report_target.resolve()),
                "status": "simplified" if item["applied"] else "original-retained",
                "reduction_ratio": item["result"].get("reduction_ratio", 0.0),
                "cache_reused": bool(item.get("cache_reused", False)),
            }
        )
    return committed


def _support_variants(base: dict[str, Any]) -> list[dict[str, Any]]:
    control_keys = {
        "refinement_passes",
        "feedback_passes",
        "feedback_contact_spacing_mm",
    }
    common = {key: value for key, value in base.items() if key not in control_keys}
    variants = [dict(common)]
    for refinement in base.get("refinement_passes", []):
        variant = dict(common)
        variant.update(refinement)
        variants.append(variant)
    for _ in range(max(0, int(base.get("feedback_passes", 0)))):
        variants.append(dict(variants[-1]))
    return variants


def _slice_resin(
    plate: Path,
    output: Path,
    resolved: dict[str, Any],
    *,
    support_config: dict[str, Any] | None = None,
    manual_support_points: list[tuple[float, float, float]] | None = None,
    require_manual_supports: bool = True,
    resin_slicer_path: str | None = None,
    support_plan_cache_dir: str | Path | None = None,
) -> dict[str, Any]:
    tools = discover_tools()
    executable = resin_slicer_path or tools["resin-slicer"].path
    if not executable:
        raise RuntimeError("Resin-Slicer executable not found")
    purpose = resolved["purpose"]
    supports = support_config or purpose["supports"]
    slice_report = output.with_suffix(output.suffix + ".slice-report.json")
    command = [
        executable,
        str(plate),
        "--output",
        str(output),
        "--format",
        resolved["output_format"],
        "--profile",
        resolved["material"]["resin_slicer_profile"],
        "--layer-height",
        str(resolved["quality"]["layer_height_mm"]),
        "--model-lift",
        str(supports["model_lift_mm"]),
        "--overhang-angle",
        str(supports["overhang_angle_deg"]),
        "--support-spacing",
        str(supports["spacing_mm"]),
        "--workers",
        str(_resin_layer_workers()),
        "--report-json",
        str(slice_report),
        "--verbose",
    ]
    if support_plan_cache_dir is not None:
        command.extend(
            ["--support-plan-cache-dir", str(Path(support_plan_cache_dir))]
        )
    if supports.get("primary"):
        command.append("--primary-supports")
    optional_arguments = {
        "primary_density_multiplier": "--primary-density-multiplier",
        "primary_area_radius_mm": "--primary-area-radius",
        "primary_max_extra_per_island": "--primary-max-extra",
        "post_radius_mm": "--support-radius",
        "tip_radius_mm": "--tip-radius",
        "tip_type": "--tip-type",
        "tip_length_mm": "--tip-length",
        "tip_contact_depth_mm": "--tip-contact-depth",
        "foot_radius_mm": "--foot-radius",
        "bed_interface": "--bed-interface",
        "raft_offset_mm": "--raft-offset",
        "raft_chamfer_width_mm": "--raft-chamfer-width",
        "raft_chamfer_angle_deg": "--raft-chamfer-angle",
        "bed_interface_thickness_mm": "--bed-interface-thickness",
        "brace_radius_mm": "--brace-radius",
        "brace_height_mm": "--brace-height",
        "brace_distance_mm": "--brace-distance",
        "brace_interval_mm": "--brace-interval",
        "collision_clearance_mm": "--collision-clearance",
        "max_base_reach_mm": "--max-base-reach",
        "max_support_angle_deg": "--max-support-angle",
        "tree_merge_distance_mm": "--tree-merge-distance",
        "tree_max_tips": "--tree-max-tips",
        "tree_trunk_radius_mm": "--tree-trunk-radius",
        "pedestal_height_mm": "--pedestal-height",
        "enforcer_reach_mm": "--enforcer-reach",
        "enforcer_min_drop_mm": "--enforcer-min-drop",
        "min_island_area_mm2": "--min-island-area",
        "analysis_max_pixels": "--support-analysis-pixels",
    }
    for key, flag in optional_arguments.items():
        if key in supports:
            command.extend([flag, str(supports[key])])
    if supports.get("enforcers"):
        command.append("--enforcers")
    if not supports.get("branching", True):
        command.append("--no-tree-branching")
    if not supports.get("braces", True):
        command.append("--no-braces")
    if not supports.get("enabled", True):
        command.append("--no-supports")
    for point in manual_support_points or []:
        command.extend(
            ["--manual-support", "x".join(f"{value:.6f}" for value in point)]
        )
    if manual_support_points and require_manual_supports:
        command.append("--require-manual-supports")
    completed = run_backend(
        command,
        timeout=_resin_slice_timeout_seconds(),
        env=native_subprocess_environment(),
        log_directory=output.with_suffix(output.suffix + ".logs"),
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Resin-Slicer failed with exit {completed.returncode}: {completed.stderr.strip()}"
        )
    manual_requested = len(manual_support_points or [])
    manual_routed = 0
    routed_points: list[list[float]] = []
    actual_contact_points: list[list[float]] = []
    unrouted_points: list[list[float]] = []
    report_payload: dict[str, Any] | None = None
    automatic_report: dict[str, Any] = {}
    if not slice_report.is_file():
        raise RuntimeError("Resin-Slicer did not write the required slice receipt")
    report_payload = json.loads(slice_report.read_text(encoding="utf-8"))
    if report_payload.get("schema") != "resin-slicer-slice-report-v1":
        raise RuntimeError("Resin-Slicer returned an unsupported slice receipt schema")
    manual_report = report_payload.get("manual_supports", {})
    automatic_report = report_payload.get("automatic_supports", {})
    cache_report = _validate_support_cache_receipt(
        report_payload.get("support_plan_cache"),
        required=support_plan_cache_dir is not None,
    )
    performance_report = _validate_slice_performance_receipt(
        report_payload.get("performance"),
        required=support_plan_cache_dir is not None,
    )
    internal_layer_validation = _validate_internal_layer_validation_receipt(
        report_payload.get("internal_layer_validation")
    )
    post_encode_validation = _validate_post_encode_validation_receipt(
        report_payload.get("post_encode_validation"),
        report_payload.get("encode_validation_parity"),
        internal_layer_validation,
    )
    support_tree_report = report_payload.get("support_tree")
    _validate_support_tree_receipt(
        support_tree_report,
        support_count=report_payload.get("support_count"),
        maximum_tip_load=int(supports.get("tree_max_tips", 12)),
    )
    if supports.get("enabled", True):
        _validate_automatic_support_receipt(automatic_report)
        # Resin-Slicer's model-only pass runs before later detector-derived
        # manual contacts are appended, so its balanced unresolved count is a
        # conservative planning receipt rather than the production verdict.
        # Preserve it, then fail closed on the independently decoded saved CTB
        # in the mandatory UVtools production-validation stage.
    if not isinstance(manual_report, dict):
        raise RuntimeError("Resin-Slicer slice receipt omitted manual-support accounting")
    if type(manual_report.get("requested")) is not int or type(
        manual_report.get("routed")
    ) is not int:
        raise RuntimeError("Resin-Slicer slice receipt has invalid manual-support accounting")
    manual_routed = manual_report["routed"]
    report_requested = manual_report["requested"]
    if report_requested != manual_requested:
        raise RuntimeError(
            "Resin-Slicer slice receipt manual-contact count does not match the request"
        )
    if manual_routed < 0 or manual_routed > manual_requested:
        raise RuntimeError("Resin-Slicer slice receipt has invalid manual-support accounting")
    routed_points = _validated_receipt_points(
        manual_report.get("routed_points_mm"),
        "routed",
    )
    actual_contact_points = _validated_receipt_points(
        manual_report.get("actual_contact_points_mm", routed_points),
        "actual contact",
    )
    unrouted_points = _validated_receipt_points(
        manual_report.get("unrouted_points_mm"),
        "unrouted",
    )
    if manual_routed != len(routed_points):
        raise RuntimeError(
            "Resin-Slicer slice receipt routed-contact count is inconsistent"
        )
    if manual_requested - manual_routed != len(unrouted_points):
        raise RuntimeError(
            "Resin-Slicer slice receipt unrouted-contact count is inconsistent"
        )
    if manual_routed != len(actual_contact_points):
        raise RuntimeError(
            "Resin-Slicer slice receipt actual-contact count is inconsistent"
        )
    if manual_requested and manual_routed == 0:
        raise RuntimeError("Resin-Slicer routed none of the detector feedback contacts")
    return {
        "backend": "resin-slicer",
        "process_resources": getattr(completed, "metrics", {}),
        "executable": str(Path(executable).resolve()),
        "executable_sha256": sha256_file(Path(executable).resolve()),
        "implementation": _resin_slicer_implementation_identity(Path(executable)),
        "command": command,
        "exit_code": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
        "manual_support_points": manual_support_points or [],
        "manual_supports_requested": manual_requested,
        "manual_supports_routed": manual_routed,
        "manual_supports_routed_points": routed_points,
        "manual_supports_actual_contact_points": actual_contact_points,
        "manual_supports_unrouted_points": unrouted_points,
        "manual_supports_required": require_manual_supports,
        "automatic_supports": automatic_report,
        "automatic_supports_need_encoded_validation": bool(
            automatic_report.get("components_unrouted", 0)
        ),
        "support_tree": support_tree_report,
        "support_plan_cache": cache_report,
        "performance": performance_report,
        "internal_layer_validation": internal_layer_validation,
        "post_encode_validation": post_encode_validation,
        "support_plan_cache_dir": (
            str(Path(support_plan_cache_dir).resolve())
            if support_plan_cache_dir is not None
            else None
        ),
        "slice_report": str(slice_report.resolve()) if slice_report.is_file() else None,
        "slice_report_payload": report_payload,
    }


def _validate_support_cache_receipt(
    payload: object,
    *,
    required: bool,
) -> dict[str, Any]:
    if payload is None and not required:
        return {"status": "disabled"}
    if not isinstance(payload, dict) or not isinstance(payload.get("status"), str):
        raise RuntimeError("Resin-Slicer slice receipt omitted support-cache status")
    allowed = {
        "disabled",
        "miss",
        "miss-identity",
        "miss-manual-prefix",
        "miss-corrupt",
        "hit-exact",
        "hit-prefix",
    }
    if payload["status"] not in allowed:
        raise RuntimeError("Resin-Slicer slice receipt has invalid support-cache status")
    return {"status": payload["status"]}


def _validate_slice_performance_receipt(
    payload: object,
    *,
    required: bool,
) -> dict[str, float]:
    if payload is None and not required:
        return {}
    if not isinstance(payload, dict):
        raise RuntimeError("Resin-Slicer slice receipt omitted phase timings")
    required_fields = (
        "preparation_seconds",
        "support_planning_seconds",
        "output_writing_seconds",
        "pipeline_total_seconds",
    )
    result: dict[str, float] = {}
    for name in required_fields:
        value = payload.get(name)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value < 0
        ):
            raise RuntimeError("Resin-Slicer slice receipt has invalid phase timings")
        result[name] = float(value)
    phase_total = (
        result["preparation_seconds"]
        + result["support_planning_seconds"]
        + result["output_writing_seconds"]
    )
    tolerance = max(1e-6, result["pipeline_total_seconds"] * 1e-6)
    if result["pipeline_total_seconds"] + tolerance < phase_total:
        raise RuntimeError("Resin-Slicer slice receipt phase timings are inconsistent")
    return result


def _validate_internal_layer_validation_receipt(
    payload: object,
    *,
    expected_mode: str = "pre-encode-native-raster-v1",
) -> dict[str, Any]:
    """Validate Resin-Slicer's supplementary pre-encode structural evidence.

    The matching post-encode receipt is validated separately. UVtools remains
    the production gate until the native detectors have broad parity evidence.
    """

    if payload is None:
        return {"status": "unavailable"}
    if not isinstance(payload, dict):
        raise RuntimeError("Resin-Slicer returned invalid internal layer validation")
    if payload.get("mode") != expected_mode:
        raise RuntimeError("Resin-Slicer returned unsupported internal layer validation")
    integer_fields = (
        "attachment_radius_px",
        "footprint_min_unsupported_area_px",
        "ignored_initial_layers",
        "layers_observed",
        "nonempty_layers",
        "leading_empty_layers",
        "internal_empty_layers",
        "trailing_empty_layers",
        "detached_component_count",
        "detached_pixel_count",
        "largest_detached_component_px",
    )
    if any(type(payload.get(name)) is not int or payload[name] < 0 for name in integer_fields):
        raise RuntimeError("Resin-Slicer returned invalid internal layer validation")
    observed = int(payload["layers_observed"])
    nonempty = int(payload["nonempty_layers"])
    empty_total = sum(
        int(payload[name])
        for name in (
            "leading_empty_layers",
            "internal_empty_layers",
            "trailing_empty_layers",
        )
    )
    if (
        observed <= 0
        or int(payload["footprint_min_unsupported_area_px"]) <= 0
        or int(payload["ignored_initial_layers"]) > observed
        or nonempty + empty_total != observed
    ):
        raise RuntimeError("Resin-Slicer internal layer counts are inconsistent")
    first = payload.get("first_nonempty_layer_zero_based")
    last = payload.get("last_nonempty_layer_zero_based")
    if nonempty:
        if type(first) is not int or type(last) is not int or not 0 <= first <= last < observed:
            raise RuntimeError("Resin-Slicer internal nonempty-layer bounds are invalid")
    elif first is not None or last is not None:
        raise RuntimeError("Resin-Slicer internal nonempty-layer bounds are invalid")
    detached_count = int(payload["detached_component_count"])
    detached_pixels = int(payload["detached_pixel_count"])
    largest = int(payload["largest_detached_component_px"])
    layers = payload.get("layers_with_detached_components_zero_based")
    if (
        not isinstance(layers, list)
        or any(type(index) is not int or not 0 <= index < observed for index in layers)
        or layers != sorted(set(layers))
    ):
        raise RuntimeError("Resin-Slicer internal detached-layer indices are invalid")
    if detached_count == 0:
        if detached_pixels != 0 or largest != 0 or layers:
            raise RuntimeError("Resin-Slicer internal detached-component counts are inconsistent")
    elif detached_pixels < detached_count or largest <= 0 or not layers:
        raise RuntimeError("Resin-Slicer internal detached-component counts are inconsistent")
    deficit_integer_fields = (
        "footprint_deficit_component_count",
        "footprint_deficit_component_pixels",
        "unsupported_footprint_pixels",
        "largest_footprint_deficit_component_px",
        "footprint_issue_record_limit",
        "footprint_issue_records_truncated",
    )
    deficit_layers_name = "layers_with_footprint_deficit_components_zero_based"
    deficit_issues_name = "footprint_deficit_issues"
    if any(
        name in payload
        for name in deficit_integer_fields + (deficit_layers_name, deficit_issues_name)
    ):
        if any(
            type(payload.get(name)) is not int or payload[name] < 0
            for name in deficit_integer_fields
        ):
            raise RuntimeError("Resin-Slicer internal footprint-deficit counts are invalid")
        deficit_layers = payload.get(deficit_layers_name)
        if (
            not isinstance(deficit_layers, list)
            or any(
                type(index) is not int or not 0 <= index < observed
                for index in deficit_layers
            )
            or deficit_layers != sorted(set(deficit_layers))
        ):
            raise RuntimeError("Resin-Slicer internal footprint-deficit layers are invalid")
        deficit_count = int(payload["footprint_deficit_component_count"])
        component_pixels = int(payload["footprint_deficit_component_pixels"])
        unsupported_pixels = int(payload["unsupported_footprint_pixels"])
        largest_deficit = int(payload["largest_footprint_deficit_component_px"])
        record_limit = int(payload["footprint_issue_record_limit"])
        records_truncated = int(payload["footprint_issue_records_truncated"])
        issue_records = payload.get(deficit_issues_name)
        if (
            record_limit <= 0
            or not isinstance(issue_records, list)
            or len(issue_records) > record_limit
            or len(issue_records) + records_truncated != deficit_count
        ):
            raise RuntimeError("Resin-Slicer internal footprint-deficit records are invalid")
        for issue in issue_records:
            if not isinstance(issue, dict):
                raise RuntimeError("Resin-Slicer internal footprint-deficit records are invalid")
            layer_index = issue.get("layer_index_zero_based")
            area = issue.get("component_area_px")
            unsupported_area = issue.get("unsupported_area_px")
            bounds = issue.get("bounds_px")
            if (
                type(layer_index) is not int
                or layer_index not in deficit_layers
                or type(area) is not int
                or type(unsupported_area) is not int
                or area <= 0
                or not 1 <= unsupported_area <= area
                or not isinstance(bounds, dict)
            ):
                raise RuntimeError("Resin-Slicer internal footprint-deficit records are invalid")
            coordinates = tuple(bounds.get(name) for name in ("min_x", "min_y", "max_x", "max_y"))
            if (
                any(type(value) is not int or value < 0 for value in coordinates)
                or coordinates[0] > coordinates[2]
                or coordinates[1] > coordinates[3]
            ):
                raise RuntimeError("Resin-Slicer internal footprint-deficit records are invalid")
        if deficit_count == 0:
            if component_pixels or unsupported_pixels or largest_deficit or deficit_layers:
                raise RuntimeError(
                    "Resin-Slicer internal footprint-deficit counts are inconsistent"
                )
        elif (
            component_pixels < deficit_count
            or not deficit_count <= unsupported_pixels <= component_pixels
            or not 1 <= largest_deficit <= component_pixels
            or not deficit_layers
        ):
            raise RuntimeError(
                "Resin-Slicer internal footprint-deficit counts are inconsistent"
            )
    return dict(payload)


def _validate_post_encode_validation_receipt(
    payload: object,
    parity_payload: object,
    pre_encode: dict[str, Any],
) -> dict[str, Any]:
    """Validate decoded-file evidence while retaining UVtools as authority."""

    if payload is None and parity_payload is None:
        return {"status": "unavailable"}
    if not isinstance(payload, dict) or not isinstance(parity_payload, dict):
        raise RuntimeError("Resin-Slicer omitted post-encode validation parity")
    fmt = payload.get("format")
    if fmt not in {"ctb", "goo"} or not isinstance(payload.get("format_version"), str):
        raise RuntimeError("Resin-Slicer returned invalid post-encode format metadata")
    resolution = payload.get("resolution")
    if (
        not isinstance(resolution, list)
        or len(resolution) != 2
        or any(type(value) is not int or value <= 0 for value in resolution)
    ):
        raise RuntimeError("Resin-Slicer returned invalid post-encode resolution")
    integer_fields = ("declared_layer_count", "file_size_bytes", "decoded_layer_bytes")
    if any(type(payload.get(name)) is not int or payload[name] <= 0 for name in integer_fields):
        raise RuntimeError("Resin-Slicer returned invalid post-encode file accounting")
    checks = payload.get("integrity_checks")
    if not isinstance(checks, list) or not checks or any(not isinstance(item, str) for item in checks):
        raise RuntimeError("Resin-Slicer omitted post-encode integrity checks")
    decoded = _validate_internal_layer_validation_receipt(
        payload.get("layer_validation"),
        expected_mode=f"post-encode-native-decoded-{fmt}-v1",
    )
    if decoded.get("layers_observed") != payload["declared_layer_count"]:
        raise RuntimeError("Resin-Slicer post-encode layer count is inconsistent")
    if parity_payload.get("matches") is not True or parity_payload.get("differing_fields") != []:
        raise RuntimeError("Resin-Slicer pre/post-encode structural validation differs")
    if pre_encode.get("status") != "unavailable":
        ignored = {"mode", "scope"}
        pre_comparable = {key: value for key, value in pre_encode.items() if key not in ignored}
        post_comparable = {key: value for key, value in decoded.items() if key not in ignored}
        if pre_comparable != post_comparable:
            raise RuntimeError("Resin-Slicer post-encode receipt does not reproduce pre-encode evidence")
    return {
        **payload,
        "layer_validation": decoded,
        "parity": dict(parity_payload),
        "status": "verified-parity",
        "production_authority": False,
    }


def _validate_automatic_support_receipt(payload: object) -> None:
    if not isinstance(payload, dict):
        raise RuntimeError(
            "Resin-Slicer slice receipt omitted automatic-support routing accounting"
        )
    required = (
        "components_detected",
        "components_supported",
        "components_excluded",
        "components_unrouted",
        "candidates_attempted",
        "candidates_routed",
        "candidates_unrouted",
        "unrouted_area_mm2",
    )
    if any(key not in payload for key in required):
        raise RuntimeError(
            "Resin-Slicer slice receipt has incomplete automatic-support accounting"
        )
    if any(type(payload[key]) is not int for key in required[:-1]):
        raise RuntimeError(
            "Resin-Slicer slice receipt has invalid automatic-support accounting"
        )
    try:
        integers = {key: int(payload[key]) for key in required[:-1]}
        area = float(payload["unrouted_area_mm2"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError(
            "Resin-Slicer slice receipt has invalid automatic-support accounting"
        ) from exc
    if any(value < 0 for value in integers.values()) or not math.isfinite(area) or area < 0:
        raise RuntimeError(
            "Resin-Slicer slice receipt has invalid automatic-support accounting"
        )
    if integers["components_detected"] != (
        integers["components_supported"]
        + integers["components_excluded"]
        + integers["components_unrouted"]
    ):
        raise RuntimeError(
            "Resin-Slicer slice receipt component accounting is inconsistent"
        )
    if integers["candidates_attempted"] != (
        integers["candidates_routed"] + integers["candidates_unrouted"]
    ):
        raise RuntimeError(
            "Resin-Slicer slice receipt candidate accounting is inconsistent"
        )


def _resin_slice_timeout_seconds() -> int:
    raw = os.environ.get("AGENT_SLICER_RESIN_SLICE_TIMEOUT_SECONDS")
    if raw is None:
        return DEFAULT_RESIN_SLICE_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            "AGENT_SLICER_RESIN_SLICE_TIMEOUT_SECONDS must be an integer"
        ) from exc
    if value < 60 or value > 24 * 60 * 60:
        raise ValueError(
            "AGENT_SLICER_RESIN_SLICE_TIMEOUT_SECONDS must be between 60 and 86400"
        )
    return value


def _validate_support_tree_receipt(
    payload: object,
    *,
    support_count: object,
    maximum_tip_load: int,
) -> None:
    if not isinstance(payload, dict):
        raise RuntimeError("Resin-Slicer slice receipt omitted support-tree accounting")
    required = ("roots", "branches", "max_tip_load")
    if any(type(payload.get(key)) is not int for key in required):
        raise RuntimeError("Resin-Slicer slice receipt has invalid support-tree accounting")
    if type(support_count) is not int or maximum_tip_load <= 0:
        raise RuntimeError("Resin-Slicer slice receipt has invalid support-tree accounting")
    roots, branches, max_load = (int(payload[key]) for key in required)
    if (
        roots < 0
        or branches < 0
        or max_load < 0
        or roots + branches != support_count
        or (support_count == 0 and max_load != 0)
        or (support_count > 0 and not 1 <= max_load <= maximum_tip_load)
    ):
        raise RuntimeError("Resin-Slicer slice receipt has inconsistent support-tree accounting")


def _validated_receipt_points(payload: object, label: str) -> list[list[float]]:
    if not isinstance(payload, list):
        raise RuntimeError(
            f"Resin-Slicer slice receipt has invalid {label}-contact coordinates"
        )
    points: list[list[float]] = []
    for point in payload:
        if not isinstance(point, (list, tuple)) or len(point) != 3:
            raise RuntimeError(
                f"Resin-Slicer slice receipt has invalid {label}-contact coordinates"
            )
        try:
            coordinates = [float(value) for value in point]
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError(
                f"Resin-Slicer slice receipt has invalid {label}-contact coordinates"
            ) from exc
        if not all(math.isfinite(value) for value in coordinates):
            raise RuntimeError(
                f"Resin-Slicer slice receipt has invalid {label}-contact coordinates"
            )
        points.append(coordinates)
    return points


def _resin_slicer_implementation_identity(executable: Path) -> dict[str, Any] | None:
    """Fingerprint the installed backend code, not only its console-script stub."""

    executable = executable.resolve()
    library = executable.parent.parent / "lib"
    candidates = sorted(library.glob("python*/site-packages/resin_slicer"))
    package = next((candidate for candidate in candidates if candidate.is_dir()), None)
    if package is None:
        # Editable installs intentionally keep the package outside site-packages.
        # Ask the venv interpreter where the console script will import it from
        # so audit receipts continue to fingerprint the code actually executed.
        interpreter = executable.parent / "python"
        if interpreter.is_file():
            located = subprocess.run(
                [
                    str(interpreter),
                    "-c",
                    (
                        "import pathlib, resin_slicer; "
                        "print(pathlib.Path(resin_slicer.__file__).resolve().parent)"
                    ),
                ],
                check=False,
                text=True,
                capture_output=True,
                timeout=30,
            )
            if located.returncode == 0:
                candidate = Path(located.stdout.strip())
                if candidate.is_dir() and candidate.name == "resin_slicer":
                    package = candidate
    if package is None:
        return None
    files = sorted(
        path
        for path in package.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix.lower() in {".py", ".json"}
    )
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(package).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return {
        "package_path": str(package.resolve()),
        "sha256": digest.hexdigest(),
        "file_count": len(files),
    }


def _resin_layer_workers() -> int:
    value = os.environ.get("AGENT_SLICER_RESIN_WORKERS", "1")
    try:
        workers = int(value)
    except ValueError as exc:
        raise ValueError("AGENT_SLICER_RESIN_WORKERS must be an integer") from exc
    if workers < 1 or workers > 8:
        raise ValueError("AGENT_SLICER_RESIN_WORKERS must be between 1 and 8")
    return workers


def _targeted_support_points(
    validation: dict[str, Any],
    resolved: dict[str, Any],
    *,
    existing: list[tuple[float, float, float]] | None = None,
) -> list[tuple[float, float, float]]:
    """Translate actionable UVtools island evidence into routed contacts."""

    machine = resolved["machine"]
    pixel_x = float(machine["build_volume_mm"][0]) / float(machine["resolution"][0])
    pixel_y = float(machine["build_volume_mm"][1]) / float(machine["resolution"][1])
    layer_height = float(resolved["quality"]["layer_height_mm"])
    minimum_spacing_mm = max(
        0.05,
        float(
            resolved.get("purpose", {})
            .get("supports", {})
            .get("feedback_contact_spacing_mm", 0.3)
        ),
    )
    points = list(existing or [])

    def append(point: tuple[float, float, float]) -> None:
        for current in points:
            if (
                abs(current[2] - point[2]) <= layer_height * 2
                and (current[0] - point[0]) ** 2 + (current[1] - point[1]) ** 2
                <= minimum_spacing_mm**2
            ):
                return
        points.append(point)

    wanted = {"uvtools.issue.islands", "uvtools.issue.overhangs"}
    for check in validation.get("checks", []):
        if check.get("id") not in wanted or check.get("passed"):
            continue
        for record in check.get("largest_actionable_records", []):
            bounds = record.get("bounds_pixels")
            first_layer = record.get("first_layer")
            if not isinstance(bounds, dict) or first_layer is None:
                continue
            width = max(1.0, float(bounds["width"]))
            height = max(1.0, float(bounds["height"]))
            center_x = float(bounds["x"]) + width / 2.0
            center_y = float(bounds["y"]) + height / 2.0
            suggested = record.get("suggested_contact_pixels")
            if isinstance(suggested, list) and suggested:
                pixel_points = [
                    (float(point[0]), float(point[1]))
                    for point in suggested
                    if isinstance(point, list) and len(point) == 2
                ]
            else:
                fractions = [(0.0, 0.0)]
                if float(record.get("area_mm2", 0.0)) >= 0.10:
                    fractions.extend(
                        [
                            (-0.25, 0.0),
                            (0.25, 0.0),
                            (0.0, -0.25),
                            (0.0, 0.25),
                        ]
                    )
                pixel_points = [
                    (center_x + dx * width, center_y + dy * height)
                    for dx, dy in fractions
                ]
            for px, py in pixel_points:
                append(
                    (
                        (px + 0.5) * pixel_x,
                        (py + 0.5) * pixel_y,
                        (float(first_layer) + 0.5) * layer_height,
                    )
                )
    return points[:_feedback_contact_limit(resolved)]


def _feedback_contact_limit(resolved: dict[str, Any]) -> int:
    configured = int(
        resolved.get("purpose", {})
        .get("supports", {})
        .get("max_feedback_contacts", 256)
    )
    return min(1024, max(1, configured))


def _embedded_analysis_for_sha(
    build_manifest: dict[str, Any],
    source_sha256: str,
) -> dict[str, Any] | None:
    """Return reusable UVtools evidence already embedded in a build receipt.

    Rejected manifests retain the complete production-validation payload.  A
    resume should not decode and scan a CTB again when complete current analysis
    is already bound to the CTB hash. Refuse older schemas, incomplete detectors,
    or missing/mismatched hashes so cache reuse remains fail closed.
    """

    analysis = build_manifest.get("production_validation", {}).get("analysis")
    if not isinstance(analysis, dict):
        return None
    if (analysis.get("schema") != "agent-slicer-uvtools-analysis/v4"
            or not has_complete_detection_contract(analysis)):
        return None
    if analysis.get("source_sha256") != source_sha256:
        return None
    return analysis


def _resume_targeted_support_points(
    build_manifest: dict[str, Any],
    resolved: dict[str, Any],
) -> list[tuple[float, float, float]]:
    """Carry verified native-raster contacts across rejected build cycles.

    Builds written after the coordinate contract was made explicit identify it
    directly.  For the immediately preceding development receipts, infer the
    contract only when one attempt's saved UVtools feedback reproduces the next
    attempt's complete contact list under the current native-row mapping.  This
    prevents older, mirrored-Y contacts from consuming the bounded contact set.
    """

    attempts = build_manifest.get("slice_attempts")
    if not isinstance(attempts, list) or not attempts:
        return []

    coordinate_system = build_manifest.get("feedback_coordinate_system")
    native_rows_verified = coordinate_system == "ctb-raster-native-v1"
    if coordinate_system not in {None, "ctb-raster-native-v1"}:
        return []

    if not native_rows_verified:
        for current, following in zip(attempts, attempts[1:]):
            if not isinstance(current, dict) or not isinstance(following, dict):
                continue
            current_points = _normalise_saved_support_points(
                current.get("targeted_support_points")
            )
            following_points = _normalise_saved_support_points(
                following.get("targeted_support_points")
            )
            validation = current.get("production_validation")
            if not isinstance(validation, dict):
                continue
            expected = _targeted_support_points(
                validation,
                resolved,
                existing=current_points,
            )
            if _support_point_lists_equal(expected, following_points):
                native_rows_verified = True
                break

    if not native_rows_verified:
        return []
    final = attempts[-1]
    if not isinstance(final, dict):
        return []
    return _normalise_saved_support_points(final.get("targeted_support_points"))[
        :_feedback_contact_limit(resolved)
    ]


def _normalise_saved_support_points(value: Any) -> list[tuple[float, float, float]]:
    if not isinstance(value, list):
        return []
    points: list[tuple[float, float, float]] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            return []
        try:
            point = (float(item[0]), float(item[1]), float(item[2]))
        except (TypeError, ValueError):
            return []
        if not all(math.isfinite(component) for component in point):
            return []
        points.append(point)
    return points


def _support_point_lists_equal(
    first: list[tuple[float, float, float]],
    second: list[tuple[float, float, float]],
) -> bool:
    if len(first) != len(second):
        return False
    return all(
        all(abs(left - right) <= 1e-7 for left, right in zip(a, b))
        for a, b in zip(first, second)
    )


def _placements_from_plan(plan: BuildPlan):
    from .models import Placement

    return [
        Placement(
            model_id=item["model_id"],
            source=item["source"],
            scale=float(item["scale"]),
            translate=tuple(item["translate"]),
            dimensions_mm=tuple(item["dimensions_mm"]),
            rotate_z_deg=float(item.get("rotate_z_deg", 0.0)),
        )
        for item in plan.placements
    ]


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _build_work_root(artifacts: Path, configured: str | Path | None) -> Path:
    requested = configured or os.environ.get("AGENT_SLICER_WORK_ROOT")
    if requested:
        root = Path(requested).expanduser().resolve() / "builds"
    else:
        try:
            release = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8")
        except OSError:
            release = ""
        if "microsoft" in release.lower():
            root = Path.home() / "development-tools" / "agent-slicer-work" / "builds"
        else:
            root = artifacts
    root.mkdir(parents=True, exist_ok=True)
    return root


def _commit_file(source: Path, destination: Path) -> None:
    """Atomically publish a transaction file, including across filesystems."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    shutil.copy2(source, temporary)
    temporary.replace(destination)


def _write_markdown_report(path: Path, result: dict[str, Any], plan: BuildPlan) -> None:
    validation = result.get("validation", {})
    lines = [
        f"# Agent-Slicer build: {Path(plan.project).name}",
        "",
        f"- Status: `{result['status']}`",
        f"- Purpose: `{plan.resolved_config['purpose_id']}`",
        f"- Machine: `{plan.resolved_config['machine_id']}`",
        f"- Material: `{plan.resolved_config['material_id']}`",
        f"- Plate: `{result['plate']}`",
    ]
    if result.get("sliced_output"):
        lines.extend(
            [
                f"- Sliced output: `{result['sliced_output']}`",
                f"- Basic validation: `{validation.get('status')}`",
            ]
        )
    production = result.get("production_validation")
    if production:
        lines.append(f"- Production validation: `{production.get('status')}`")
    if result.get("geometry_preparation"):
        lines.append(
            f"- Prepared models: `{len(result['geometry_preparation'])}` with measured repair reports"
        )
    if result.get("mesh_simplification"):
        reduced = sum(
            item.get("status") == "simplified"
            for item in result["mesh_simplification"]
        )
        lines.append(
            f"- Mesh simplification: `{reduced}/{len(result['mesh_simplification'])}` models reduced with measured reports"
        )
    lines.extend(
        [
            "",
            "## Limitations",
            "",
            "- Local topology repair preserves detail outside bounded patches; explicit voxel reconstruction is lossy.",
            "- Mesh simplification is lossy and accepted only when topology, volume, bounds, and sampled surface-deviation gates pass.",
            "- Resin orientation uses a bounded deterministic heuristic, not a global optimum.",
            "- Support-aware shelf packing reserves support footprint but does not globally optimize placement.",
            "- UVtools production validation is detector evidence, not proof of firmware acceptance or a successful physical print.",
            "- GOO embeds a creation timestamp, so otherwise identical GOO builds are not byte-for-byte identical.",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")




def _safe_name(value: str) -> str:
    return Path(value).name.replace(" ", "-") or "build"
