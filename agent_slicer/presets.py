from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable


CATALOG_NAMES = ("machines", "materials", "purposes", "qualities")
PRESET_PATH_ENV = "AGENT_SLICER_PRESET_PATH"


class PresetError(ValueError):
    pass


def catalog_search_paths(
    search_paths: Iterable[str | Path] | None = None,
) -> tuple[Path, ...]:
    """Return deterministic external catalog roots.

    Explicit paths take precedence over the environment. When no paths are
    supplied, ``AGENT_SLICER_PRESET_PATH`` is interpreted as an ``os.pathsep``
    separated list. There is intentionally no preset-count limit.
    """

    values: Iterable[str | Path]
    if search_paths is None:
        values = (
            item
            for item in os.environ.get(PRESET_PATH_ENV, "").split(os.pathsep)
            if item
        )
    else:
        values = search_paths
    result: list[Path] = []
    seen: set[Path] = set()
    for value in values:
        path = Path(value).expanduser().resolve()
        if path in seen:
            continue
        if not path.is_dir():
            raise PresetError(f"preset catalog directory does not exist: {path}")
        seen.add(path)
        result.append(path)
    return tuple(result)


def load_catalog(
    name: str,
    *,
    search_paths: Iterable[str | Path] | None = None,
) -> dict[str, Any]:
    """Load a bundled catalog and merge non-conflicting external entries."""

    if name not in CATALOG_NAMES:
        raise PresetError(
            f"unknown catalog {name!r}; expected one of {', '.join(CATALOG_NAMES)}"
        )
    bundled_path = files("agent_slicer.data.presets").joinpath(f"{name}.json")
    bundled = json.loads(bundled_path.read_text(encoding="utf-8"))
    _validate_catalog(name, bundled, origin=f"bundled:{name}.json")
    merged = deepcopy(bundled)

    for root in catalog_search_paths(search_paths):
        path = root / f"{name}.json"
        if not path.exists():
            continue
        external = _read_catalog_file(name, path)
        duplicate = sorted(set(merged).intersection(external))
        if duplicate:
            raise PresetError(
                f"preset id collision in {path}: {', '.join(duplicate)}; "
                "external catalogs may add ids but may not silently replace them"
            )
        merged.update(deepcopy(external))
    return merged


def resolve_presets(
    *,
    process: str,
    machine: str,
    material: str,
    purpose: str,
    quality: str,
    output_format: str,
    overrides: dict[str, Any] | None = None,
    search_paths: Iterable[str | Path] | None = None,
) -> dict[str, Any]:
    machines = load_catalog("machines", search_paths=search_paths)
    materials = load_catalog("materials", search_paths=search_paths)
    purposes = load_catalog("purposes", search_paths=search_paths)
    qualities = load_catalog("qualities", search_paths=search_paths)
    try:
        machine_value = deepcopy(machines[machine])
        material_value = deepcopy(materials[material])
        purpose_value = deepcopy(purposes[purpose])
        quality_value = deepcopy(qualities[quality])
    except KeyError as exc:
        raise PresetError(f"unknown preset: {exc.args[0]}") from exc

    selected_ids = {
        "machine": machine,
        "material": material,
        "purpose": purpose,
        "quality": quality,
    }
    for label, value in (
        ("machine", machine_value),
        ("material", material_value),
        ("purpose", purpose_value),
        ("quality", quality_value),
    ):
        declared_process = value.get("process")
        if declared_process is not None and declared_process != process:
            raise PresetError(
                f"{label} preset is for {declared_process!r}, not {process!r}"
            )
        compatible_machines = value.get("compatible_machines")
        if compatible_machines is not None and machine not in compatible_machines:
            raise PresetError(
                f"{label} preset {selected_ids[label]!r} is not compatible with "
                f"machine {machine!r}"
            )
    if output_format not in machine_value["output_formats"]:
        raise PresetError(
            f"machine {machine!r} does not allow output format {output_format!r}"
        )

    resolved: dict[str, Any] = {
        "schema": "agent-slicer-resolved-config/v1",
        "process": process,
        "machine_id": machine,
        "material_id": material,
        "purpose_id": purpose,
        "quality_id": quality,
        "output_format": output_format,
        "machine": machine_value,
        "material": material_value,
        "purpose": purpose_value,
        "quality": quality_value,
    }
    if overrides:
        resolved["user_overrides"] = deepcopy(overrides)
        _merge_known(resolved, overrides, path="overrides")
    return resolved


def recommend_settings(
    *,
    process: str,
    machine: str,
    material: str,
    purpose: str,
    quality: str,
    output_format: str,
    overrides: dict[str, Any] | None = None,
    search_paths: Iterable[str | Path] | None = None,
) -> dict[str, Any]:
    """Return an evidence-bearing starting profile without claiming print proof."""

    resolved = resolve_presets(
        process=process,
        machine=machine,
        material=material,
        purpose=purpose,
        quality=quality,
        output_format=output_format,
        overrides=overrides,
        search_paths=search_paths,
    )
    machine_value = resolved["machine"]
    material_value = resolved["material"]
    purpose_value = resolved["purpose"]
    quality_value = resolved["quality"]

    reasons = list(material_value.get("calibration", {}).get("reasons", []))
    tested_layers = material_value.get("calibration", {}).get(
        "source_layer_heights_mm", []
    )
    selected_layer = quality_value.get("layer_height_mm")
    if tested_layers and selected_layer not in tested_layers:
        reasons.append(
            f"selected layer height {selected_layer} mm differs from the "
            f"source-backed material setting(s) {tested_layers} mm"
        )
    if process == "fdm":
        reasons.append(
            "flow dynamics and flow rate remain filament batch, color, nozzle, "
            "and wear dependent"
        )
    elif process == "resin":
        reasons.append(
            "normal exposure remains resin batch, color, temperature, FEP, and "
            "LCD-condition dependent"
        )

    evidence: list[dict[str, Any]] = []
    seen_evidence: set[str] = set()
    for value in (machine_value, material_value, purpose_value, quality_value):
        for item in value.get("evidence", []):
            token = json.dumps(item, sort_keys=True, separators=(",", ":"))
            if token not in seen_evidence:
                evidence.append(deepcopy(item))
                seen_evidence.add(token)

    limitations = list(material_value.get("limitations", []))
    limitations.extend(quality_value.get("limitations", []))
    if process == "fdm":
        limitations.append(
            "Agent-Slicer records and resolves this FDM profile, but the current "
            "production pipeline has no installed Bambu Studio slicing adapter."
        )

    return {
        "schema": "agent-slicer-settings-recommendation/v1",
        "recommendation_status": "evidence-backed-starting-point",
        "production_status": "calibration-required",
        "selection": {
            "process": process,
            "machine": machine,
            "material": material,
            "purpose": purpose,
            "quality": quality,
            "output_format": output_format,
        },
        "setting_precedence": {
            "hard_constraints": "machine",
            "layers_low_to_high": [
                "machine",
                "material",
                "quality",
                "purpose",
                "user_overrides",
            ],
            "note": (
                "purpose choices such as enabling supports supersede a quality "
                "profile's default, but may not violate machine hard constraints"
            ),
        },
        "settings": {
            "machine": deepcopy(machine_value.get("settings", machine_value)),
            "material": deepcopy(material_value.get("settings", material_value)),
            "quality": deepcopy(quality_value.get("settings", quality_value)),
            "purpose": {
                key: deepcopy(purpose_value[key])
                for key in ("geometry", "arrangement", "supports", "validation")
                if key in purpose_value
            },
        },
        "upstream_profiles": {
            label: deepcopy(value.get("upstream_profile"))
            for label, value in (
                ("machine", machine_value),
                ("material", material_value),
                ("quality", quality_value),
            )
            if value.get("upstream_profile") is not None
        },
        "calibration": {
            "required": True,
            "reasons": _unique_strings(reasons),
            "procedure": deepcopy(material_value.get("calibration", {}).get("procedure")),
        },
        "evidence": evidence,
        "limitations": _unique_strings(limitations),
        "resolved_config": resolved,
    }


def validate_catalog_directory(path: str | Path) -> dict[str, Any]:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise PresetError(f"preset catalog directory does not exist: {root}")
    summaries: dict[str, Any] = {}
    total = 0
    for name in CATALOG_NAMES:
        catalog_path = root / f"{name}.json"
        if not catalog_path.exists():
            continue
        value = _read_catalog_file(name, catalog_path)
        # Loading against the bundled catalogs also detects id collisions.
        load_catalog(name, search_paths=[root])
        raw = catalog_path.read_bytes()
        summaries[name] = {
            "path": str(catalog_path),
            "entries": len(value),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        total += len(value)
    if not summaries:
        raise PresetError(
            f"no catalog files found in {root}; expected one or more of "
            + ", ".join(f"{name}.json" for name in CATALOG_NAMES)
        )
    return {
        "schema": "agent-slicer-preset-catalog-validation/v1",
        "status": "passed",
        "directory": str(root),
        "catalogs": summaries,
        "entries": total,
    }


def add_external_preset(
    *,
    directory: str | Path,
    catalog: str,
    preset_id: str,
    definition: dict[str, Any],
) -> dict[str, Any]:
    """Append one validated preset to an external catalog atomically."""

    if catalog not in CATALOG_NAMES:
        raise PresetError(f"unknown catalog {catalog!r}")
    if not preset_id or preset_id.strip() != preset_id:
        raise PresetError("preset id must be non-empty and have no edge whitespace")
    if not isinstance(definition, dict):
        raise PresetError("preset definition must be a JSON object")
    if preset_id in load_catalog(catalog, search_paths=[]):
        raise PresetError(f"preset id {preset_id!r} already exists in bundled {catalog}")

    root = Path(directory).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{catalog}.json"
    existing = _read_catalog_file(catalog, path) if path.exists() else {}
    if preset_id in existing:
        raise PresetError(f"preset id {preset_id!r} already exists in {path}")
    updated = deepcopy(existing)
    updated[preset_id] = deepcopy(definition)
    _validate_catalog(catalog, updated, origin=str(path))

    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(updated, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return {
        "status": "added",
        "catalog": catalog,
        "preset_id": preset_id,
        "path": str(path),
        "entries": len(updated),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _read_catalog_file(name: str, path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PresetError(f"invalid JSON in preset catalog {path}: {exc}") from exc
    _validate_catalog(name, value, origin=str(path))
    return value


def _validate_catalog(name: str, value: Any, *, origin: str) -> None:
    if not isinstance(value, dict):
        raise PresetError(f"preset catalog {origin} must contain a JSON object")
    for preset_id, definition in value.items():
        if not isinstance(preset_id, str) or not preset_id:
            raise PresetError(f"preset ids in {origin} must be non-empty strings")
        if not isinstance(definition, dict):
            raise PresetError(f"preset {preset_id!r} in {origin} must be an object")
        location = f"{origin}:{preset_id}"
        process = definition.get("process")
        if process is not None and process not in {"resin", "fdm"}:
            raise PresetError(f"{location} has unsupported process {process!r}")
        if name == "machines":
            _validate_machine(definition, location=location)
        elif name == "materials" and process is None:
            raise PresetError(f"{location} must declare process")
        elif name == "qualities":
            layer_height = definition.get("layer_height_mm")
            if layer_height is not None and (
                not isinstance(layer_height, (int, float)) or layer_height <= 0
            ):
                raise PresetError(f"{location} layer_height_mm must be positive")
        elif name == "purposes":
            capabilities = definition.get("required_capabilities")
            if not isinstance(capabilities, list) or not all(
                isinstance(item, str) and item for item in capabilities
            ):
                raise PresetError(
                    f"{location} required_capabilities must be a string list"
                )
        compatible = definition.get("compatible_machines")
        if compatible is not None and (
            not isinstance(compatible, list)
            or not compatible
            or not all(isinstance(item, str) and item for item in compatible)
        ):
            raise PresetError(f"{location} compatible_machines must be a string list")
        _validate_evidence(definition.get("evidence"), location=location)


def _validate_machine(definition: dict[str, Any], *, location: str) -> None:
    if definition.get("process") not in {"resin", "fdm"}:
        raise PresetError(f"{location} machine must declare resin or fdm process")
    volume = definition.get("build_volume_mm")
    if (
        not isinstance(volume, list)
        or len(volume) != 3
        or not all(isinstance(item, (int, float)) and item > 0 for item in volume)
    ):
        raise PresetError(f"{location} build_volume_mm must contain 3 positive numbers")
    formats = definition.get("output_formats")
    if (
        not isinstance(formats, list)
        or not formats
        or not all(isinstance(item, str) and item for item in formats)
    ):
        raise PresetError(f"{location} output_formats must be a non-empty string list")
    if definition.get("default_output_format") not in formats:
        raise PresetError(f"{location} default_output_format must be in output_formats")


def _validate_evidence(value: Any, *, location: str) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise PresetError(f"{location} evidence must be a list")
    for index, item in enumerate(value):
        item_location = f"{location}.evidence[{index}]"
        if not isinstance(item, dict):
            raise PresetError(f"{item_location} must be an object")
        url = item.get("url")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            raise PresetError(f"{item_location}.url must be an HTTP(S) URL")
        retrieved = item.get("retrieved")
        if not isinstance(retrieved, str) or not retrieved:
            raise PresetError(f"{item_location}.retrieved must be a non-empty string")
        claims = item.get("claims")
        if not isinstance(claims, list) or not claims or not all(
            isinstance(claim, str) and claim for claim in claims
        ):
            raise PresetError(f"{item_location}.claims must be a non-empty string list")


def _merge_known(target: dict[str, Any], updates: dict[str, Any], *, path: str) -> None:
    """Deep-merge explicit project overrides while rejecting misspelled paths."""

    for key, value in updates.items():
        location = f"{path}.{key}"
        if key not in target:
            raise PresetError(f"unknown override path: {location}")
        current = target[key]
        if isinstance(current, dict):
            if not isinstance(value, dict):
                raise PresetError(f"override {location} must be an object")
            _merge_known(current, value, path=location)
        else:
            target[key] = deepcopy(value)


def _unique_strings(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
