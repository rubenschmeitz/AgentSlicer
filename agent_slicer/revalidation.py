"""Repeat production validation on an unchanged completed/rejected build."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import uuid4

from .pipeline import compile_plan, BuildBlocked, _write_json_atomic, _write_markdown_report
from .pipeline import _find_cached_preparation, _preparation_arguments
from .project import sha256_file, load_project
from .models import SourceModel
from .stl import inspect_stl, pack_shelf
from .resources import heavy_job
from .uvtools_adapter import analyze_sliced_file, evaluate_production_analysis
from .validation import validate_sliced_output


def _prepared_placement_evidence(root: Path, build: dict, resolved: dict) -> tuple[list, dict]:
    """Reconstruct placement from the exact accepted prepared meshes, read-only.

    The saved build plan uses prepared coordinates, while compile_plan uses
    original source dimensions. Those legitimately differ after tilt/hollowing.
    Never compare them as if they described the same geometry.
    """
    if build.get("mesh_simplification"):
        raise ValueError("Revalidation of simplified geometry requires a new build")
    _, manifest = load_project(root)
    entries = build["geometry_preparation"]
    if len(entries) != len(manifest.models):
        raise ValueError("Prepared model inventory changed; build again")
    models, inspections, tracked = [], [], {}
    for model, entry in zip(manifest.models, entries):
        cached = _find_cached_preparation(root, model, _preparation_arguments(
            model, resolved["purpose"]["geometry"]))
        if cached is None or entry.get("model_id") != model.id:
            raise ValueError("Prepared geometry/settings changed; build again")
        mesh, report, result = cached
        if (mesh.resolve() != Path(entry["prepared"]).resolve()
                or report.resolve() != Path(entry["report"]).resolve()
                or result["output_sha256"] != entry.get("prepared_sha256")):
            raise ValueError("Prepared geometry evidence changed; build again")
        models.append(SourceModel(model.id, str(mesh), result["output_sha256"],
                                  quantity=model.quantity))
        inspections.append(inspect_stl(mesh))
        tracked[mesh] = result["output_sha256"]
        tracked[report] = sha256_file(report)
    arrangement = resolved["purpose"]["arrangement"]
    placements = pack_shelf(root=root, models=models, inspections=inspections,
        plate_size_mm=tuple(resolved["machine"]["build_volume_mm"]),
        clearance_mm=float(arrangement["clearance_mm"]),
        margin_mm=float(resolved["machine"]["margin_mm"]),
        support_footprint_margin_mm=float(arrangement.get("support_footprint_margin_mm", 0)))
    return [p.to_dict() for p in placements], tracked


@heavy_job
def revalidate_project(project: str | Path, *, uvtools_path: str | None = None,
                       work_root: str | Path | None = None) -> dict:
    """Do not reslice, weaken checks, upload or print. Preserve prior evidence."""
    root, plan = compile_plan(project)
    if plan.blockers:
        raise BuildBlocked(plan)
    artifacts = root / "artifacts"
    manifest_path = artifacts / "build-manifest.json"
    before = manifest_path.read_bytes()
    build = json.loads(before)
    if build.get("status") not in {"completed", "rejected"}:
        raise ValueError("Revalidation requires a completed or rejected build")
    project_digest = sha256_file(root / "project.json")
    saved_config = json.loads((artifacts / "resolved-config.json").read_text(encoding="utf-8"))
    if saved_config != plan.resolved_config:
        raise ValueError("Project settings changed; build again instead of revalidating")
    plan_path = artifacts / "plan.json"
    saved_plan_bytes = plan_path.read_bytes()
    saved_plan = json.loads(saved_plan_bytes)
    current_plan = json.loads(json.dumps(plan.to_dict()))
    for key in ("resolved_config", "inspections", "operations"):
        if saved_plan.get(key) != current_plan.get(key):
            raise ValueError("Project geometry or placement changed; build again")
    tracked = {}
    if build.get("geometry_preparation"):
        placements, tracked = _prepared_placement_evidence(root, build, plan.resolved_config)
        # Only the ephemeral transaction source path changes after commit. Its
        # content is separately bound to the accepted preparation hash above.
        def normalized(items):
            return json.loads(json.dumps([{k:v for k,v in p.items() if k != "source"}
                                          for p in items]))
        if normalized(placements) != normalized(saved_plan.get("placements", [])):
            raise ValueError("Prepared placement changed; build again")
        plan.placements = placements
    elif saved_plan.get("placements") != current_plan.get("placements"):
        raise ValueError("Project geometry or placement changed; build again")
    source = Path(build["sliced_output"]).resolve()
    if not source.is_relative_to(artifacts.resolve()):
        raise ValueError("Build output must be inside project artifacts")
    digest = sha256_file(source)
    if digest != build.get("sliced_sha256"):
        raise ValueError("Sliced file changed after its build")
    plate = Path(build["plate"]).resolve()
    if not plate.is_relative_to(artifacts.resolve()) or sha256_file(plate) != build.get("plate_sha256"):
        raise ValueError("Prepared plate changed after its build")
    basic = validate_sliced_output(source)
    if basic["status"] != "passed":
        raise ValueError("Basic file validation failed")
    run_id = uuid4().hex
    report = root / "reports" / f"revalidation-{run_id}.json"
    analysis = analyze_sliced_file(source, output=report, uvtools_path=uvtools_path,
                                   work_root=work_root)
    production = evaluate_production_analysis(analysis, plan.resolved_config)
    if (sha256_file(source) != digest or analysis.get("source_sha256") != digest or
            sha256_file(plate) != build["plate_sha256"] or
            sha256_file(root / "project.json") != project_digest or
            any(sha256_file(p) != value for p, value in tracked.items()) or
            manifest_path.read_bytes() != before or
            plan_path.read_bytes() != saved_plan_bytes or
            json.loads((artifacts / "resolved-config.json").read_text()) != saved_config):
        raise RuntimeError("Build or project changed during revalidation; no approval updated")
    history = artifacts / "validation-history"
    history.mkdir(parents=True, exist_ok=True)
    backup = history / f"build-{run_id}.json"
    with backup.open("xb") as handle:
        handle.write(before)
    passed = production["status"] == "passed"
    if passed and source.parent != artifacts.resolve():
        approved = artifacts / f"approved-{digest[:16]}{source.suffix}"
        if approved.exists():
            if sha256_file(approved) != digest:
                raise ValueError("Approved artifact name already contains different bytes")
        else:
            with source.open("rb") as incoming, approved.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
        if sha256_file(approved) != digest:
            raise RuntimeError("Approved copy failed integrity check")
        source = approved
    basic["path"] = str(source)
    result = {**build, "status": "completed" if passed else "rejected",
              "sliced_output": str(source), "validation": basic,
              "production_validation": production,
              "revalidation": {"analysis_report": str(report),
                               "previous_build_report": str(backup), "source_sha256": digest}}
    _write_json_atomic(artifacts / "production-validation.json", production)
    _write_json_atomic(artifacts / "validation.json", basic)
    _write_json_atomic(manifest_path, result)
    _write_markdown_report(root / "reports" / "build-report.md", result, plan)
    return result
