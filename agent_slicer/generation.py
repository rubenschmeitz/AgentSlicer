from __future__ import annotations

import json
import math
import shutil
import time
from pathlib import Path
from typing import Any

from .generation_providers import ProviderTask, download, provider
from .project import add_models, sha256_file
from .resources import heavy_job
from .stl import inspect_stl


GENERATION_SCHEMA = "agent-slicer-generation/v1"
PARAMETRIC_SCHEMA = "agent-slicer-parametric-part/v1"
APPROVAL_STAGES = {"brief", "references", "selection"}
TERMINAL_TASK_STATES = {"succeeded", "failed", "canceled"}


def init_generation(
    directory: str | Path,
    *,
    name: str,
    kind: str,
    description: str,
    target_height_mm: float | None = None,
    image_provider: str = "meshy",
    model_provider: str = "meshy",
) -> Path:
    if kind not in {"part", "concept"}:
        raise ValueError("generation kind must be 'part' or 'concept'")
    if not name.strip() or not description.strip():
        raise ValueError("generation name and description must not be empty")
    if kind == "concept" and (target_height_mm is None or target_height_mm <= 0):
        raise ValueError("concept generation requires an explicit positive target height")
    if target_height_mm is not None and target_height_mm <= 0:
        raise ValueError("target height must be positive")
    for selected in (image_provider, model_provider):
        if selected not in {"meshy", "tripo"}:
            raise ValueError(f"unsupported provider: {selected!r}")

    root = Path(directory).resolve()
    manifest_path = root / "generation.json"
    if manifest_path.exists():
        raise FileExistsError(f"generation project already exists: {root}")
    for relative in ("references", "candidates", "selected", "source", "sketches", "reports"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": GENERATION_SCHEMA,
        "name": name.strip(),
        "kind": kind,
        "description": description.strip(),
        "target_height_mm": target_height_mm,
        "image_provider": image_provider,
        "model_provider": model_provider,
        "status": "draft",
        "approvals": {stage: False for stage in sorted(APPROVAL_STAGES)},
        "tasks": [],
        "artifacts": [],
        "selected_candidate_id": None,
        "created_unix": time.time(),
        "updated_unix": time.time(),
    }
    save_generation(root, manifest)
    (root / "request.md").write_text(_request_markdown(manifest), encoding="utf-8")
    return root


def load_generation(path: str | Path) -> tuple[Path, dict[str, Any]]:
    candidate = Path(path).resolve()
    manifest_path = candidate / "generation.json" if candidate.is_dir() else candidate
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = manifest_path.parent.resolve()
    _validate_manifest(value)
    for collection in ("tasks", "artifacts"):
        if not isinstance(value[collection], list):
            raise ValueError(f"generation {collection} must be a list")
    for artifact in value["artifacts"]:
        relative = artifact.get("path")
        if relative is not None:
            resolved = (root / str(relative)).resolve()
            if not resolved.is_relative_to(root):
                raise ValueError(f"generation artifact escapes project: {relative!r}")
    return root, value


def save_generation(root: str | Path, manifest: dict[str, Any]) -> None:
    root_path = Path(root).resolve()
    manifest["updated_unix"] = time.time()
    _validate_manifest(manifest)
    destination = root_path / "generation.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def approve_generation(path: str | Path, stage: str) -> dict[str, Any]:
    if stage not in APPROVAL_STAGES:
        raise ValueError(f"approval stage must be one of {sorted(APPROVAL_STAGES)}")
    root, manifest = load_generation(path)
    if stage == "references" and not _artifacts(manifest, "reference"):
        raise RuntimeError("no reference images are available to approve")
    if stage == "selection" and manifest.get("selected_candidate_id") is None:
        raise RuntimeError("no generated candidate has been selected")
    manifest["approvals"][stage] = True
    _update_status(manifest)
    save_generation(root, manifest)
    return manifest


def add_reference_images(path: str | Path, images: list[str | Path]) -> list[dict[str, Any]]:
    root, manifest = load_generation(path)
    added: list[dict[str, Any]] = []
    existing_hashes = {item["sha256"] for item in _artifacts(manifest, "reference")}
    for raw in images:
        source = Path(raw).resolve()
        if source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError(f"unsupported reference image: {source}")
        digest = sha256_file(source)
        if digest in existing_hashes:
            continue
        index = len(_artifacts(manifest, "reference")) + 1
        destination = root / "references" / f"reference-{index}{source.suffix.lower()}"
        shutil.copy2(source, destination)
        record = _artifact_record(
            root,
            destination,
            artifact_id=f"reference-{index}",
            kind="reference",
            source_task_id=None,
        )
        manifest["artifacts"].append(record)
        added.append(record)
        existing_hashes.add(digest)
    if added:
        manifest["approvals"]["references"] = False
        _update_status(manifest)
        save_generation(root, manifest)
    return added


def submit_reference_generation(
    path: str | Path,
    *,
    count: int = 1,
    model: str | None = None,
    confirm_credit_use: bool = False,
) -> list[dict[str, Any]]:
    root, manifest = load_generation(path)
    _require_credit_confirmation(confirm_credit_use)
    _require_approval(manifest, "brief")
    count = _bounded_count(count)
    backend = provider(manifest["image_provider"])
    tasks = []
    for _ in range(count):
        result = backend.submit_reference(
            _reference_prompt(manifest),
            model=model,
            multi_view=True,
        )
        task = _task_record(result, stage="reference", model=model)
        manifest["tasks"].append(task)
        tasks.append(task)
    manifest["status"] = "generating-references"
    save_generation(root, manifest)
    return tasks


def submit_model_generation(
    path: str | Path,
    *,
    count: int = 1,
    model: str | None = None,
    confirm_credit_use: bool = False,
) -> list[dict[str, Any]]:
    root, manifest = load_generation(path)
    _require_credit_confirmation(confirm_credit_use)
    _require_approval(manifest, "brief")
    references = _artifacts(manifest, "reference")
    if references:
        _require_approval(manifest, "references")
        reference = root / references[0]["path"]
    else:
        reference = None
    count = _bounded_count(count)
    backend = provider(manifest["model_provider"])
    tasks = []
    for _ in range(count):
        result = backend.submit_model(
            manifest["description"],
            image=reference,
            model=model,
        )
        task = _task_record(result, stage="model", model=model)
        manifest["tasks"].append(task)
        tasks.append(task)
    manifest["status"] = "generating-models"
    save_generation(root, manifest)
    return tasks


def refresh_generation(path: str | Path, *, fetch: bool = True) -> dict[str, Any]:
    root, manifest = load_generation(path)
    changed = False
    for task in manifest["tasks"]:
        if task["status"] in TERMINAL_TASK_STATES and (
            task["status"] != "succeeded" or task.get("fetched")
        ):
            continue
        backend = provider(task["provider"])
        result = backend.status(task["operation"], task["task_id"])
        task.update(result.receipt())
        task["updated_unix"] = time.time()
        changed = True
        if result.status == "succeeded" and fetch and not task.get("fetched"):
            records = _fetch_task_outputs(root, manifest, task, result)
            task["fetched"] = True
            task["artifact_ids"] = [record["id"] for record in records]
    if changed:
        _update_status(manifest)
        save_generation(root, manifest)
    return manifest


def select_candidate(path: str | Path, candidate_id: str) -> dict[str, Any]:
    root, manifest = load_generation(path)
    candidate = next(
        (item for item in _artifacts(manifest, "candidate") if item["id"] == candidate_id),
        None,
    )
    if candidate is None:
        raise ValueError(f"unknown candidate: {candidate_id!r}")
    selected = _normalise_candidate(root, manifest, candidate)
    manifest["selected_candidate_id"] = candidate_id
    manifest["approvals"]["selection"] = False
    manifest["artifacts"] = [
        item for item in manifest["artifacts"] if item["kind"] != "selected-model"
    ]
    manifest["artifacts"].append(selected)
    _update_status(manifest)
    save_generation(root, manifest)
    return selected


def adopt_selected_model(path: str | Path, project_path: str | Path) -> dict[str, Any]:
    root, manifest = load_generation(path)
    _require_approval(manifest, "selection")
    selected = _artifacts(manifest, "selected-model")
    if len(selected) != 1:
        raise RuntimeError("generation project does not contain one selected STL")
    model = root / selected[0]["path"]
    added = add_models(project_path, [model])
    if not added:
        raise RuntimeError("selected model is already present in the Agent-Slicer project")
    manifest["status"] = "adopted"
    manifest["adopted_project"] = str(Path(project_path).resolve())
    manifest["adopted_model_id"] = added[0].id
    save_generation(root, manifest)
    return {
        "status": "adopted",
        "project": manifest["adopted_project"],
        "model_id": added[0].id,
        "source_sha256": added[0].sha256,
    }


@heavy_job
def build_parametric_part(path: str | Path, spec_path: str | Path) -> dict[str, Any]:
    root, manifest = load_generation(path)
    if manifest["kind"] != "part":
        raise RuntimeError("parametric CAD generation is only valid for part projects")
    _require_approval(manifest, "brief")
    spec = json.loads(Path(spec_path).resolve().read_text(encoding="utf-8"))
    _validate_parametric_spec(spec)
    try:
        import cadquery as cq
    except ImportError as exc:
        raise RuntimeError(
            "CadQuery is not installed; install the agent-slicer 'cad' extra"
        ) from exc

    spec_destination = root / "source" / "part-spec.json"
    _write_json_atomic(spec_destination, spec)
    source_path = root / "source" / "model.py"
    source_path.write_text(_cadquery_source(spec), encoding="utf-8")
    solid = _build_cadquery_solid(cq, spec)
    selected_dir = root / "selected"
    step_path = selected_dir / "model.step"
    stl_path = selected_dir / "model.stl"
    cq.exporters.export(solid, str(step_path))
    cq.exporters.export(solid, str(stl_path), tolerance=0.02, angularTolerance=0.1)
    sketches: list[dict[str, Any]] = []
    projections = {
        "top": (0, 0, 1),
        "front": (0, -1, 0),
        "right": (1, 0, 0),
    }
    for name, direction in projections.items():
        sketch_path = root / "sketches" / f"{name}.svg"
        cq.exporters.export(solid, str(sketch_path), opt={"projectionDir": direction})
        sketches.append(
            _artifact_record(
                root,
                sketch_path,
                artifact_id=f"sketch-{name}",
                kind="sketch",
                source_task_id=None,
            )
        )

    cleanup = _clean_cad_stl(stl_path)
    inspection = inspect_stl(stl_path).to_dict()
    if inspection["degenerate_triangles"]:
        raise RuntimeError("CadQuery STL still contains degenerate triangles after cleanup")
    report = {
        "schema": "agent-slicer-parametric-build/v1",
        "spec_sha256": sha256_file(spec_destination),
        "source_sha256": sha256_file(source_path),
        "step_sha256": sha256_file(step_path),
        "stl_sha256": sha256_file(stl_path),
        "inspection": inspection,
        "tessellation_cleanup": cleanup,
        "limitations": [
            "CAD export success does not establish fit, strength, or printability.",
            "The selected STL must still pass Agent-Slicer's manufacturing workflow.",
        ],
    }
    report_path = root / "reports" / "parametric-build.json"
    _write_json_atomic(report_path, report)
    retained = {
        "reference",
        "candidate",
    }
    manifest["artifacts"] = [
        item for item in manifest["artifacts"] if item["kind"] in retained
    ]
    manifest["artifacts"].extend(sketches)
    for artifact_id, kind, artifact_path in (
        ("parametric-step", "cad-source", step_path),
        ("parametric-stl", "selected-model", stl_path),
        ("parametric-report", "report", report_path),
    ):
        manifest["artifacts"].append(
            _artifact_record(
                root,
                artifact_path,
                artifact_id=artifact_id,
                kind=kind,
                source_task_id=None,
            )
        )
    manifest["selected_candidate_id"] = "parametric-stl"
    manifest["approvals"]["selection"] = False
    manifest["status"] = "awaiting-selection-approval"
    save_generation(root, manifest)
    return report


def generation_status(path: str | Path) -> dict[str, Any]:
    root, manifest = load_generation(path)
    return {
        "schema": manifest["schema"],
        "project": str(root),
        "name": manifest["name"],
        "kind": manifest["kind"],
        "status": manifest["status"],
        "approvals": manifest["approvals"],
        "tasks": manifest["tasks"],
        "artifacts": manifest["artifacts"],
        "selected_candidate_id": manifest["selected_candidate_id"],
    }


def _fetch_task_outputs(
    root: Path,
    manifest: dict[str, Any],
    task: dict[str, Any],
    result: ProviderTask,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for filename, url in sorted(result.output_urls.items()):
        if task["stage"] == "reference" and filename.startswith(("image-", "preview")):
            kind = "reference"
            directory = root / "references"
            prefix = "generated-reference"
        elif task["stage"] == "model" and filename.startswith("model"):
            kind = "candidate"
            directory = root / "candidates"
            prefix = "candidate"
        elif task["stage"] == "model" and filename.startswith("preview"):
            kind = "candidate-preview"
            directory = root / "candidates"
            prefix = "candidate-preview"
        else:
            continue
        suffix = Path(filename).suffix.lower()
        index = len(_artifacts(manifest, kind)) + 1
        destination = directory / f"{prefix}-{index}{suffix}"
        download(url, destination)
        record = _artifact_record(
            root,
            destination,
            artifact_id=f"{prefix}-{index}",
            kind=kind,
            source_task_id=task["task_id"],
        )
        manifest["artifacts"].append(record)
        records.append(record)
    return records


def _normalise_candidate(
    root: Path,
    manifest: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    try:
        import trimesh
    except ImportError as exc:  # pragma: no cover - core dependency
        raise RuntimeError("trimesh is required to normalise generated models") from exc
    target_height = manifest.get("target_height_mm")
    if target_height is None or float(target_height) <= 0:
        raise RuntimeError("generated models require an explicit positive target height")
    source = root / candidate["path"]
    loaded = trimesh.load(source, force="scene", process=True)
    if isinstance(loaded, trimesh.Scene):
        geometries = [mesh for mesh in loaded.geometry.values() if len(mesh.faces)]
        if not geometries:
            raise RuntimeError("generated candidate contains no triangle geometry")
        mesh = trimesh.util.concatenate(tuple(geometries))
    else:
        mesh = loaded
    if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
        raise RuntimeError("generated candidate contains no triangle mesh")
    bounds = mesh.bounds
    height = float(bounds[1][2] - bounds[0][2])
    if not math.isfinite(height) or height <= 0:
        raise RuntimeError("generated candidate has invalid height")
    scale = float(target_height) / height
    mesh.apply_scale(scale)
    bounds = mesh.bounds
    center_x = float((bounds[0][0] + bounds[1][0]) / 2.0)
    center_y = float((bounds[0][1] + bounds[1][1]) / 2.0)
    mesh.apply_translation((-center_x, -center_y, -float(bounds[0][2])))
    destination = root / "selected" / "model.stl"
    mesh.export(destination, file_type="stl")
    report = {
        "schema": "agent-slicer-generated-normalisation/v1",
        "source": candidate,
        "source_sha256": sha256_file(source),
        "target_height_mm": float(target_height),
        "scale": scale,
        "triangle_count": int(len(mesh.faces)),
        "body_count": len(loaded.geometry) if isinstance(loaded, trimesh.Scene) else 1,
        "watertight_after_import": bool(mesh.is_watertight),
        "output_sha256": sha256_file(destination),
        "limitations": [
            "Generated geometry is untrusted until repair and production validation pass.",
            "Normalisation establishes size and placement, not structural or print quality.",
        ],
    }
    _write_json_atomic(root / "reports" / "candidate-normalisation.json", report)
    return _artifact_record(
        root,
        destination,
        artifact_id="selected-model",
        kind="selected-model",
        source_task_id=candidate.get("source_task_id"),
        metadata=report,
    )


def _clean_cad_stl(path: Path) -> dict[str, Any]:
    """Remove only measured degenerate tessellation faces from a CAD STL."""

    before = inspect_stl(path).to_dict()
    if before["degenerate_triangles"] == 0:
        return {
            "applied": False,
            "removed_degenerate_triangles": 0,
            "before_sha256": before["sha256"],
            "after_sha256": before["sha256"],
        }
    try:
        import trimesh
    except ImportError as exc:  # pragma: no cover - core dependency
        raise RuntimeError("trimesh is required to clean CAD tessellation") from exc
    mesh = trimesh.load_mesh(path, process=False)
    if not isinstance(mesh, trimesh.Trimesh):
        raise RuntimeError("CadQuery STL did not load as one triangle mesh")
    face_count = int(len(mesh.faces))
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.remove_unreferenced_vertices()
    removed = face_count - int(len(mesh.faces))
    if removed != int(before["degenerate_triangles"]):
        raise RuntimeError(
            "CAD tessellation cleanup did not match the measured degenerate count"
        )
    mesh.export(path, file_type="stl")
    after = inspect_stl(path).to_dict()
    return {
        "applied": True,
        "removed_degenerate_triangles": removed,
        "before_sha256": before["sha256"],
        "after_sha256": after["sha256"],
    }


def _build_cadquery_solid(cq: Any, spec: dict[str, Any]) -> Any:
    result = None
    for item in spec["operations"]:
        shape = _cadquery_primitive(cq, item)
        if result is None:
            result = shape
        elif item["operation"] == "add":
            result = result.union(shape)
        else:
            result = result.cut(shape)
    assert result is not None
    finish = spec.get("finish", {})
    if finish.get("fillet_mm"):
        result = result.edges().fillet(float(finish["fillet_mm"]))
    if finish.get("chamfer_mm"):
        result = result.edges().chamfer(float(finish["chamfer_mm"]))
    return result


def _cadquery_primitive(cq: Any, item: dict[str, Any]) -> Any:
    center = tuple(float(value) for value in item.get("center_mm", (0, 0, 0)))
    if item["shape"] == "box":
        size = tuple(float(value) for value in item["size_mm"])
        return cq.Workplane("XY").box(*size).translate(center)
    diameter = float(item["diameter_mm"])
    height = float(item["height_mm"])
    axis = item.get("axis", "z")
    workplane = {"z": "XY", "x": "YZ", "y": "XZ"}[axis]
    return cq.Workplane(workplane).cylinder(height, diameter / 2.0).translate(center)


def _cadquery_source(spec: dict[str, Any]) -> str:
    encoded = json.dumps(spec, indent=2, sort_keys=True)
    return (
        "# Generated by Agent-Slicer. Dimensions are millimetres.\n"
        "import json\n"
        "import cadquery as cq\n\n"
        f"SPEC = json.loads({encoded!r})\n\n"
        "def primitive(item):\n"
        "    center = tuple(float(v) for v in item.get('center_mm', (0, 0, 0)))\n"
        "    if item['shape'] == 'box':\n"
        "        return cq.Workplane('XY').box(*item['size_mm']).translate(center)\n"
        "    plane = {'z': 'XY', 'x': 'YZ', 'y': 'XZ'}[item.get('axis', 'z')]\n"
        "    return cq.Workplane(plane).cylinder(item['height_mm'], item['diameter_mm'] / 2).translate(center)\n\n"
        "result = None\n"
        "for item in SPEC['operations']:\n"
        "    shape = primitive(item)\n"
        "    result = shape if result is None else (result.union(shape) if item['operation'] == 'add' else result.cut(shape))\n"
        "finish = SPEC.get('finish', {})\n"
        "if finish.get('fillet_mm'):\n"
        "    result = result.edges().fillet(float(finish['fillet_mm']))\n"
        "if finish.get('chamfer_mm'):\n"
        "    result = result.edges().chamfer(float(finish['chamfer_mm']))\n"
    )


def _validate_parametric_spec(spec: dict[str, Any]) -> None:
    allowed = {"schema", "name", "description", "units", "parameters", "operations", "finish"}
    unknown = set(spec) - allowed
    if unknown:
        raise ValueError(f"unknown parametric specification fields: {sorted(unknown)}")
    if spec.get("schema") != PARAMETRIC_SCHEMA:
        raise ValueError(f"unsupported parametric schema: {spec.get('schema')!r}")
    if spec.get("units") != "mm":
        raise ValueError("parametric part units must be explicitly 'mm'")
    operations = spec.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ValueError("parametric part requires at least one operation")
    for index, item in enumerate(operations):
        if not isinstance(item, dict):
            raise ValueError(f"operation {index} must be an object")
        allowed_operation = {"operation", "shape", "center_mm", "size_mm", "diameter_mm", "height_mm", "axis"}
        unknown_operation = set(item) - allowed_operation
        if unknown_operation:
            raise ValueError(f"unknown fields in operation {index}: {sorted(unknown_operation)}")
        if item.get("operation") not in {"add", "cut"}:
            raise ValueError(f"operation {index} must be add or cut")
        if index == 0 and item["operation"] != "add":
            raise ValueError("the first parametric operation must add material")
        shape = item.get("shape")
        if shape == "box":
            _positive_vector(item.get("size_mm"), 3, f"operation {index} size_mm")
        elif shape == "cylinder":
            _positive_number(item.get("diameter_mm"), f"operation {index} diameter_mm")
            _positive_number(item.get("height_mm"), f"operation {index} height_mm")
            if item.get("axis", "z") not in {"x", "y", "z"}:
                raise ValueError(f"operation {index} axis must be x, y, or z")
        else:
            raise ValueError(f"operation {index} has unsupported shape {shape!r}")
        if "center_mm" in item:
            _finite_vector(item["center_mm"], 3, f"operation {index} center_mm")
    finish = spec.get("finish", {})
    if not isinstance(finish, dict) or set(finish) - {"fillet_mm", "chamfer_mm"}:
        raise ValueError("finish accepts only fillet_mm and chamfer_mm")
    for key, value in finish.items():
        _positive_number(value, f"finish {key}")


def _artifact_record(
    root: Path,
    path: Path,
    *,
    artifact_id: str,
    kind: str,
    source_task_id: str | None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("artifact must be inside the generation project")
    return {
        "id": artifact_id,
        "kind": kind,
        "path": str(resolved.relative_to(root)),
        "sha256": sha256_file(resolved),
        "bytes": resolved.stat().st_size,
        "source_task_id": source_task_id,
        "metadata": metadata or {},
    }


def _task_record(task: ProviderTask, *, stage: str, model: str | None) -> dict[str, Any]:
    value = task.receipt()
    value.setdefault("model", model)
    value.update(
        {
            "stage": stage,
            "submitted_unix": time.time(),
            "updated_unix": time.time(),
            "fetched": False,
            "artifact_ids": [],
        }
    )
    return value


def _artifacts(manifest: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [item for item in manifest["artifacts"] if item.get("kind") == kind]


def _update_status(manifest: dict[str, Any]) -> None:
    if manifest.get("status") == "adopted":
        return
    if manifest["approvals"].get("selection"):
        manifest["status"] = "ready-to-adopt"
    elif manifest.get("selected_candidate_id"):
        manifest["status"] = "awaiting-selection-approval"
    elif _artifacts(manifest, "candidate"):
        manifest["status"] = "awaiting-selection"
    elif _artifacts(manifest, "reference") and not manifest["approvals"].get("references"):
        manifest["status"] = "awaiting-reference-approval"
    elif any(task["status"] not in TERMINAL_TASK_STATES for task in manifest["tasks"]):
        manifest["status"] = "generating"
    elif not manifest["approvals"].get("brief"):
        manifest["status"] = "draft"
    else:
        manifest["status"] = "brief-approved"


def _validate_manifest(value: dict[str, Any]) -> None:
    required = {
        "schema", "name", "kind", "description", "target_height_mm",
        "image_provider", "model_provider", "status", "approvals", "tasks",
        "artifacts", "selected_candidate_id", "created_unix", "updated_unix",
    }
    missing = required - set(value)
    if missing:
        raise ValueError(f"generation manifest missing fields: {sorted(missing)}")
    allowed = required | {"adopted_project", "adopted_model_id"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unknown generation fields: {sorted(unknown)}")
    if value.get("schema") != GENERATION_SCHEMA:
        raise ValueError(f"unsupported generation schema: {value.get('schema')!r}")
    if value.get("kind") not in {"part", "concept"}:
        raise ValueError("generation kind must be part or concept")
    if not isinstance(value.get("approvals"), dict):
        raise ValueError("generation approvals must be an object")


def _request_markdown(manifest: dict[str, Any]) -> str:
    height = manifest.get("target_height_mm")
    return (
        f"# {manifest['name']}\n\n"
        f"Kind: `{manifest['kind']}`\n\n"
        f"Target height: {height if height is not None else 'defined by CAD specification'} mm\n\n"
        "## Description\n\n"
        f"{manifest['description']}\n\n"
        "## Approval checklist\n\n"
        "- [ ] Brief and dimensional intent approved\n"
        "- [ ] Reference images approved when used\n"
        "- [ ] Selected geometry approved before manufacturing preparation\n"
    )


def _reference_prompt(manifest: dict[str, Any]) -> str:
    return (
        f"Create a clean multi-view 3D reconstruction reference sheet for: {manifest['description']}. "
        "Show the complete isolated object from front, back, left, right, and three-quarter views. "
        "Use consistent proportions, neutral lighting, a plain or transparent background, no text, "
        "no pedestal, no cropped parts, and no duplicate accessories."
    )


def _require_approval(manifest: dict[str, Any], stage: str) -> None:
    if not manifest["approvals"].get(stage):
        raise RuntimeError(f"{stage} approval is required before this operation")


def _require_credit_confirmation(value: bool) -> None:
    if not value:
        raise RuntimeError("external generation can consume credits; pass --confirm-credit-use")


def _bounded_count(value: int) -> int:
    if value < 1 or value > 4:
        raise ValueError("candidate count must be between 1 and 4")
    return value


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _positive_vector(value: Any, length: int, label: str) -> None:
    _finite_vector(value, length, label)
    if any(float(item) <= 0 for item in value):
        raise ValueError(f"{label} values must be positive")


def _finite_vector(value: Any, length: int, label: str) -> None:
    if not isinstance(value, list) or len(value) != length:
        raise ValueError(f"{label} must contain {length} numbers")
    try:
        numbers = [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain numbers") from exc
    if not all(math.isfinite(item) for item in numbers):
        raise ValueError(f"{label} values must be finite")


def _positive_number(value: Any, label: str) -> None:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be positive and finite")
