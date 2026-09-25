from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import re
import math
from typing import Any


PROJECT_SCHEMA = "agent-slicer-project/v1"
PLAN_SCHEMA = "agent-slicer-plan/v1"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class SourceModel:
    id: str
    source: str
    sha256: str
    target_height_mm: float | None = None
    quantity: int = 1

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SourceModel":
        allowed = {"id", "source", "sha256", "target_height_mm", "quantity"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown model fields: {sorted(unknown)}")
        required = {"id", "source", "sha256"}
        missing = required - set(value)
        if missing:
            raise ValueError(f"missing model fields: {sorted(missing)}")
        quantity = value.get("quantity", 1)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise ValueError("model quantity must be a positive integer")
        height = value.get("target_height_mm")
        if height is not None and (isinstance(height, bool) or not isinstance(height, (int, float))
                                   or not math.isfinite(height) or height <= 0):
            raise ValueError("model target height must be a finite positive number")
        model = cls(
            id=str(value["id"]),
            source=str(value["source"]),
            sha256=str(value["sha256"]),
            target_height_mm=(
                float(value["target_height_mm"])
                if value.get("target_height_mm") is not None
                else None
            ),
            quantity=int(value.get("quantity", 1)),
        )
        if not model.id:
            raise ValueError("model id must not be empty")
        if not model.source:
            raise ValueError("model source must not be empty")
        if not SHA256_PATTERN.fullmatch(model.sha256):
            raise ValueError(f"invalid SHA-256 for model {model.id!r}")
        if model.target_height_mm is not None and model.target_height_mm <= 0:
            raise ValueError(f"target height for model {model.id!r} must be positive")
        if model.quantity < 1:
            raise ValueError(f"quantity for model {model.id!r} must be at least one")
        return model


@dataclass
class ProjectManifest:
    name: str
    process: str
    machine: str
    material: str
    purpose: str
    quality: str = "standard"
    output_format: str = "ctb"
    models: list[SourceModel] = field(default_factory=list)
    overrides: dict[str, Any] = field(default_factory=dict)
    schema: str = PROJECT_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProjectManifest":
        allowed = {
            "schema",
            "name",
            "process",
            "machine",
            "material",
            "purpose",
            "quality",
            "output_format",
            "models",
            "overrides",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown project fields: {sorted(unknown)}")
        if value.get("schema") != PROJECT_SCHEMA:
            raise ValueError(f"unsupported project schema: {value.get('schema')!r}")
        required = {"name", "process", "machine", "material", "purpose"}
        missing = required - set(value)
        if missing:
            raise ValueError(f"missing project fields: {sorted(missing)}")
        manifest = cls(
            name=str(value["name"]),
            process=str(value["process"]),
            machine=str(value["machine"]),
            material=str(value["material"]),
            purpose=str(value["purpose"]),
            quality=str(value.get("quality", "standard")),
            output_format=str(value.get("output_format", "ctb")),
            models=[SourceModel.from_dict(item) for item in value.get("models", [])],
            overrides=dict(value.get("overrides", {})),
        )
        for field_name in ("name", "machine", "material", "purpose", "quality"):
            if not getattr(manifest, field_name):
                raise ValueError(f"project {field_name} must not be empty")
        if manifest.process not in {"resin", "fdm"}:
            raise ValueError(f"unsupported process: {manifest.process!r}")
        model_ids = [model.id for model in manifest.models]
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("model ids must be unique")
        return manifest


@dataclass(frozen=True)
class MeshInspection:
    path: str
    format: str
    triangle_count: int
    degenerate_triangles: int
    bounds_min: tuple[float, float, float]
    bounds_max: tuple[float, float, float]
    dimensions: tuple[float, float, float]
    signed_volume_mm3: float
    units_ambiguous: bool
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Placement:
    model_id: str
    source: str
    scale: float
    translate: tuple[float, float, float]
    dimensions_mm: tuple[float, float, float]
    rotate_z_deg: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BuildPlan:
    project: str
    resolved_config: dict[str, Any]
    inspections: list[dict[str, Any]]
    placements: list[dict[str, Any]]
    required_capabilities: list[str]
    available_capabilities: list[str]
    blockers: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    operations: list[dict[str, Any]]
    schema: str = PLAN_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def project_file(path: str | Path) -> Path:
    candidate = Path(path).resolve()
    return candidate / "project.json" if candidate.is_dir() else candidate
