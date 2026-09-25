from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from .models import ProjectManifest, SourceModel, project_file


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def init_project(
    directory: str | Path,
    *,
    name: str,
    process: str,
    machine: str,
    material: str,
    purpose: str,
    quality: str = "standard",
    output_format: str = "ctb",
) -> Path:
    root = Path(directory).resolve()
    if (root / "project.json").exists():
        raise FileExistsError(f"project already exists: {root}")
    (root / "sources").mkdir(parents=True, exist_ok=True)
    (root / "artifacts").mkdir(exist_ok=True)
    (root / "reports").mkdir(exist_ok=True)
    manifest = ProjectManifest(
        name=name,
        process=process,
        machine=machine,
        material=material,
        purpose=purpose,
        quality=quality,
        output_format=output_format,
    )
    save_project(root, manifest)
    return root


def load_project(path: str | Path) -> tuple[Path, ProjectManifest]:
    manifest_path = project_file(path)
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = manifest_path.parent.resolve()
    manifest = ProjectManifest.from_dict(value)
    for model in manifest.models:
        source = (root / model.source).resolve()
        if not source.is_relative_to(root):
            raise ValueError(
                f"model source escapes project directory: {model.source!r}"
            )
    return root, manifest


def save_project(root: str | Path, manifest: ProjectManifest) -> None:
    path = Path(root).resolve() / "project.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def add_models(
    project: str | Path,
    paths: list[str | Path],
    *,
    target_height_mm: float | None = None,
) -> list[SourceModel]:
    root, manifest = load_project(project)
    added: list[SourceModel] = []
    existing_ids = {model.id for model in manifest.models}
    existing_hashes = {model.sha256 for model in manifest.models}
    for raw_path in paths:
        source = Path(raw_path).resolve()
        if source.suffix.lower() != ".stl":
            raise ValueError(f"MVP accepts STL sources only: {source}")
        digest = sha256_file(source)
        if digest in existing_hashes:
            continue
        base_id = _slug(source.stem)
        model_id = base_id
        index = 2
        while model_id in existing_ids:
            model_id = f"{base_id}-{index}"
            index += 1
        destination = root / "sources" / f"{model_id}.stl"
        shutil.copy2(source, destination)
        model = SourceModel(
            id=model_id,
            source=str(destination.relative_to(root)),
            sha256=digest,
            target_height_mm=target_height_mm,
        )
        manifest.models.append(model)
        added.append(model)
        existing_ids.add(model_id)
        existing_hashes.add(digest)
    save_project(root, manifest)
    return added


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return result or "model"
