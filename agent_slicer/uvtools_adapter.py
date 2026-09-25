from __future__ import annotations

import json
import hashlib
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from scipy import ndimage

from .backends import discover_tools
from .resources import native_subprocess_environment
from .resources import heavy_job
from .uvtools_contract import DETECTION_CONTRACT, SCAN_FLAGS, has_complete_detection_contract


ISSUE_SCANS = {
    "islands": ("--islands", "Island,"),
    "overhangs": ("--overhangs", "Overhang,"),
    "resin_traps": ("--resin-traps", "ResinTrap,"),
    "suction_cups": ("--suction-cups", "SuctionCup,"),
    "touching_bounds": ("--touching-bounds", ("TouchingBound,", "TouchingBounds,")),
    "print_height": ("--print-height", "PrintHeight,"),
    "empty_layers": ("--empty-layers", "EmptyLayer,"),
}

# Large, highly supported 4K CTB files can legitimately take more than ten
# minutes for UVtools' resin-trap detector.  Keep the audit bounded, but leave
# enough headroom that a successful detector run is not discarded at the old
# 600-second boundary.
DEFAULT_ANALYSIS_TIMEOUT_SECONDS = 1800

PROPERTY_KEYS = (
    "Version",
    "ResolutionX",
    "ResolutionY",
    "LayerHeight",
    "LayerCount",
    "BottomLayerCount",
    "BottomExposureTime",
    "ExposureTime",
    "MachineName",
    "MaterialMilliliters",
    "PrintHeight",
)

DEFAULT_UVTOOLS_GC_CONSERVE_MEMORY = 5


@heavy_job
def analyze_sliced_file(
    source: str | Path,
    *,
    output: str | Path | None = None,
    uvtools_path: str | None = None,
    work_root: str | Path | None = None,
    workers: int | None = None,
    timeout: int = DEFAULT_ANALYSIS_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    path = Path(source).resolve()
    executable = uvtools_path or discover_tools()["uvtools"].path
    if not executable:
        raise RuntimeError("UVtoolsCmd executable not found")
    executable = _resolved_executable(executable)
    analysis_work_root = _analysis_work_root(path, work_root)
    worker_count = _analysis_workers(workers)
    backend_source = _backend_path(path, executable)

    # UVtools decodes and caches every high-resolution layer per process. Two
    # simultaneous detectors nearly exhausted 16 GiB WSL. Keep detectors in
    # isolated processes because UVtools 4.4.3 changes findings when flags are
    # combined, but run those processes sequentially to bound peak memory.
    issue_records: dict[str, list[dict[str, Any]]] = {}
    for name, (flag, prefix) in ISSUE_SCANS.items():
        issues_result = _run(
            executable,
            ["--quiet", "--no-progress", "print-issues", backend_source, *SCAN_FLAGS[name]],
            timeout,
            allowed_exit_codes=(0, 1),
        )
        issue_records[name] = [
            parse_issue_record(line)
            for line in issues_result.stdout.splitlines()
            if line.startswith(prefix)
        ]
    issue_counts = {name: len(records) for name, records in issue_records.items()}
    properties_result = _run(
        executable,
        ["--quiet", "--no-progress", "print-properties", backend_source],
        timeout,
    )
    version_result = _run(executable, ["--version"], timeout)
    core_version_result = _run(executable, ["--core-version"], timeout)
    properties = parse_properties(properties_result.stdout)
    _attach_layer_contact_evidence(
        issue_records,
        source=path,
        executable=executable,
        work_root=analysis_work_root,
        timeout=timeout,
    )
    total = sum(issue_counts.values())
    result: dict[str, Any] = {
        "schema": "agent-slicer-uvtools-analysis/v4",
        "detection_contract": DETECTION_CONTRACT,
        "scan_flags": {name: list(flags) for name, flags in SCAN_FLAGS.items()},
        "status": "passed" if total == 0 else "issues-detected",
        "path": str(path),
        "source_sha256": _sha256(path),
        "uvtools_version": version_result.stdout.strip(),
        "uvtools_core_version": core_version_result.stdout.strip(),
        "uvtools_executable": executable,
        "uvtools_executable_sha256": _sha256(Path(executable)),
        "uvtools_runtime": _runtime_kind(Path(executable)),
        "work_root": str(analysis_work_root),
        "parallel_workers": 1,
        "requested_workers": worker_count,
        "scan_strategy": "sequential-isolated-processes",
        "uvtools_gc_conserve_memory": _uvtools_gc_conserve_memory(),
        "properties": properties,
        "issues": issue_counts,
        "issue_records": issue_records,
        "total_issue_records": total,
        "limitations": [
            "Issue records are detector findings, not equivalent to failed prints.",
            "No automatic repair was applied.",
            "A zero count would not establish firmware acceptance or physical printability.",
        ],
    }
    if output is not None:
        destination = Path(output).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
    return result


def parse_issue_record(line: str) -> dict[str, Any]:
    """Parse the stable numeric fields from one UVtools issue line."""

    record: dict[str, Any] = {"raw": line}
    header = re.match(
        r"^(?P<kind>[A-Za-z]+),\s*(?P<first>\d+)"
        r"(?:-(?P<last>\d+))?(?:\s+\(\d+\))?,\s*(?P<pixels>\d+)px",
        line,
    )
    if header:
        record.update(
            {
                "kind": header.group("kind"),
                "first_layer": int(header.group("first")),
                "last_layer": int(header.group("last") or header.group("first")),
                "measure_pixels": int(header.group("pixels")),
                "measure_kind": (
                    "volume" if header.group("kind") == "ResinTrap" else "area"
                ),
            }
        )
    bounds = re.search(
        r"\{X=(?P<x>-?\d+),Y=(?P<y>-?\d+),Width=(?P<width>\d+),Height=(?P<height>\d+)\}",
        line,
    )
    if bounds:
        record["bounds_pixels"] = {
            key: int(bounds.group(key)) for key in ("x", "y", "width", "height")
        }
    return record


def _attach_layer_contact_evidence(
    issue_records: dict[str, list[dict[str, Any]]],
    *,
    source: Path,
    executable: str,
    work_root: Path,
    timeout: int,
) -> None:
    candidates = [
        record
        for name in ("islands", "overhangs")
        for record in issue_records.get(name, [])
        if int(record.get("measure_pixels", 0)) >= 8
        and int(record.get("first_layer", 0)) > 0
        and isinstance(record.get("bounds_pixels"), dict)
    ]
    layers = sorted(
        {
            int(record["first_layer"])
            for record in sorted(
                candidates,
                key=lambda value: int(value.get("measure_pixels", 0)),
                reverse=True,
            )[:128]
        }
    )
    if not layers:
        return

    selected_layers = set(layers)
    records_by_layer: dict[int, list[dict[str, Any]]] = {}
    for record in candidates:
        layer = int(record["first_layer"])
        if layer in selected_layers:
            records_by_layer.setdefault(layer, []).append(record)

    with tempfile.TemporaryDirectory(
        prefix="agent-slicer-uvtools-layers-", dir=work_root
    ) as temporary:
        destination = Path(temporary)
        completed = subprocess.run(
            [
                executable,
                "--quiet",
                "--no-progress",
                "extract",
                _backend_path(source, executable),
                _backend_path(destination, executable),
                "--content",
                "Layers",
                "--range",
                _uvtools_layer_range(layers[0] - 1, layers[-1]),
            ],
            check=False,
            text=True,
            errors="replace",
            capture_output=True,
            timeout=timeout,
        )
        if completed.returncode not in (0, 1):
            raise RuntimeError(
                f"UVtools layer extraction failed with exit {completed.returncode}: "
                f"{completed.stderr.strip()}"
            )
        layer_files = _index_extracted_layers(destination)
        for layer, records in records_by_layer.items():
            with (
                Image.open(layer_files[layer - 1]) as previous,
                Image.open(layer_files[layer]) as current,
            ):
                for record in records:
                    bounds = record["bounds_pixels"]
                    x = max(0, int(bounds["x"]))
                    y = max(0, int(bounds["y"]))
                    width = min(int(bounds["width"]), current.width - x)
                    height = min(int(bounds["height"]), current.height - y)
                    if width <= 0 or height <= 0:
                        continue
                    box = (x, y, x + width, y + height)
                    current_crop = np.asarray(current.crop(box).convert("L")) > 0
                    previous_crop = np.asarray(previous.crop(box).convert("L")) > 0
                    _attach_record_contact(
                        record,
                        previous_crop=previous_crop,
                        current_crop=current_crop,
                        offset_x=x,
                        offset_y=y,
                    )


def _uvtools_layer_range(first_layer: int, last_layer: int) -> str:
    """Return UVtools' inclusive layer-range syntax.

    Issue records use a dash for first-last display, but ``UVtoolsCmd extract``
    uses a colon. Passing the display syntax exits successfully while extracting
    no requested PNGs on UVtools 4.4.3, so keep this conversion explicit.
    """

    if first_layer < 0 or last_layer < first_layer:
        raise ValueError("invalid UVtools layer range")
    return f"{first_layer}:{last_layer}"


def _index_extracted_layers(destination: Path) -> dict[int, Path]:
    """Index UVtools layer PNGs independent of its variable zero padding."""

    indexed: dict[int, Path] = {}
    for path in destination.glob("layer*.png"):
        suffix = path.stem.removeprefix("layer")
        if suffix.isdigit():
            indexed[int(suffix)] = path
    return indexed


def _attach_record_contact(
    record: dict[str, Any],
    *,
    previous_crop: np.ndarray,
    current_crop: np.ndarray,
    offset_x: int,
    offset_y: int,
) -> None:
    if record.get("kind") == "Overhang":
        # UVtools reports the overhanging subset of a current-layer body,
        # not the whole connected body.  Matching its reported pixel count
        # to a current-layer component therefore fails for broad shelves.
        # Use the actual current pixels that have no exact prior-layer
        # overlap as support-placement candidates.  This is deliberately
        # not classified as proven contact; the next detector pass remains
        # the authority on whether the added supports resolved the finding.
        unsupported = current_crop & ~previous_crop
        if not np.any(unsupported):
            unsupported = current_crop
        target = int(record["measure_pixels"])
        count = min(24, max(5, math.ceil(target / 3000)))
        record["suggested_contact_pixels"] = _component_contact_pixels(
            unsupported,
            offset_x=offset_x,
            offset_y=offset_y,
            count=count,
        )
        return
    labels, component_count = ndimage.label(current_crop)
    if component_count == 0:
        return
    target = int(record["measure_pixels"])
    selected = min(
        range(1, component_count + 1),
        key=lambda label: abs(int(np.count_nonzero(labels == label)) - target),
    )
    component = labels == selected
    selected_size = int(np.count_nonzero(component))
    if abs(selected_size - target) > max(4, math.ceil(target * 0.15)):
        return
    overlap = component & previous_crop
    _, contact_count = ndimage.label(overlap)
    record["prior_layer_contact"] = {
        "overlap_pixels": int(np.count_nonzero(overlap)),
        "contact_components": int(contact_count),
        "overlap_ratio": float(np.count_nonzero(overlap)) / float(selected_size),
    }
    record["suggested_contact_pixels"] = _component_contact_pixels(
        component,
        offset_x=offset_x,
        offset_y=offset_y,
        count=5 if target >= 100 else 1,
    )


def _component_contact_pixels(
    component: np.ndarray,
    *,
    offset_x: int,
    offset_y: int,
    count: int,
) -> list[list[int]]:
    distance = ndimage.distance_transform_edt(component)
    suppression = max(3, int(round(min(component.shape) / 5.0)))
    yy, xx = np.ogrid[: component.shape[0], : component.shape[1]]
    points: list[list[int]] = []
    for _ in range(count):
        flat_index = int(np.argmax(distance))
        if float(distance.flat[flat_index]) <= 0:
            break
        y, x = np.unravel_index(flat_index, distance.shape)
        points.append([int(offset_x + x), int(offset_y + y)])
        distance[(xx - x) ** 2 + (yy - y) ** 2 <= suppression**2] = 0
    return points


def parse_properties(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    patterns = {
        key: re.compile(rf"^{re.escape(key)}:\s*(.+)$") for key in PROPERTY_KEYS
    }
    for line in text.splitlines():
        for key, pattern in patterns.items():
            match = pattern.match(line.strip())
            if match:
                values[key] = match.group(1).strip()
    return values


def evaluate_production_analysis(
    analysis: dict[str, Any],
    resolved_config: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate UVtools evidence against a resolved production contract."""

    purpose = resolved_config["purpose"]
    policy = purpose.get("validation", {})
    issue_limits = {
        "islands": 0,
        "overhangs": 0,
        "resin_traps": 0,
        "suction_cups": 0,
        "touching_bounds": 0,
        "print_height": 0,
        "empty_layers": 0,
    }
    issue_limits.update(policy.get("max_issues", {}))
    checks: list[dict[str, Any]] = []
    checks.append({"id": "uvtools.detection_contract",
                   "passed": has_complete_detection_contract(analysis),
                   "expected": DETECTION_CONTRACT,
                   "actual": analysis.get("detection_contract")})
    issues = analysis.get("issues", {})
    issue_records = analysis.get("issue_records", {})
    minimum_actionable = policy.get("minimum_actionable", {})
    minimum_prior_contact = policy.get("minimum_prior_layer_contact_pixels", {})
    machine = resolved_config["machine"]
    material = resolved_config["material"]
    pixel_area_mm2 = (
        float(machine["build_volume_mm"][0]) / float(machine["resolution"][0])
    ) * (
        float(machine["build_volume_mm"][1]) / float(machine["resolution"][1])
    )
    layer_height_mm = float(resolved_config["quality"]["layer_height_mm"])
    # MainIssue's grouped volume already multiplies every section by
    # layer-height * PpmmMax. Convert those reported voxel units back with
    # pixel area / PpmmMax, not pixel area * layer height a second time.
    volume_unit_mm3 = pixel_area_mm2 * min(
        float(machine["build_volume_mm"][axis]) / float(machine["resolution"][axis])
        for axis in (0, 1)
    )
    for name, maximum in issue_limits.items():
        raw_actual = int(issues.get(name, 0))
        records = issue_records.get(name)
        threshold_key = (
            f"{name}_volume_mm3" if name == "resin_traps" else f"{name}_area_mm2"
        )
        threshold = minimum_actionable.get(threshold_key)
        actionable: list[dict[str, Any]] = []
        ignored_for_size = 0
        ignored_for_contact = 0
        if isinstance(records, list) and threshold is not None:
            for record in records:
                pixels = record.get("measure_pixels")
                if pixels is None:
                    actionable.append(record)
                    continue
                physical = float(pixels) * pixel_area_mm2
                if name == "resin_traps":
                    physical = float(pixels) * volume_unit_mm3
                record = dict(record)
                record[
                    "volume_mm3" if name == "resin_traps" else "area_mm2"
                ] = physical
                if physical < float(threshold):
                    ignored_for_size += 1
                    continue
                contact_threshold = minimum_prior_contact.get(name)
                contact = record.get("prior_layer_contact", {})
                if (
                    contact_threshold is not None
                    and int(contact.get("overlap_pixels", 0))
                    >= int(contact_threshold)
                    and int(contact.get("contact_components", 0)) >= 1
                ):
                    ignored_for_contact += 1
                    continue
                actionable.append(record)
            actual = len(actionable)
        else:
            actual = raw_actual
        physical_key = "volume_mm3" if name == "resin_traps" else "area_mm2"
        largest_actionable = sorted(
            actionable,
            key=lambda record: float(record.get(physical_key, 0.0)),
            reverse=True,
        )[:20]
        checks.append(
            {
                "id": f"uvtools.issue.{name}",
                "passed": actual <= int(maximum),
                "actual": actual,
                "raw_actual": raw_actual,
                "ignored_below_actionable_threshold": ignored_for_size,
                "ignored_with_prior_layer_contact": ignored_for_contact,
                "unclassified_or_actionable": raw_actual
                - ignored_for_size
                - ignored_for_contact,
                "minimum_actionable": threshold,
                "minimum_prior_layer_contact_pixels": minimum_prior_contact.get(name),
                "maximum": int(maximum),
                "largest_actionable_records": largest_actionable,
            }
        )

    properties = analysis.get("properties", {})
    expected_properties: dict[str, object] = {
        "ResolutionX": machine["resolution"][0],
        "ResolutionY": machine["resolution"][1],
        "LayerHeight": resolved_config["quality"]["layer_height_mm"],
    }
    if machine.get("firmware_machine_name"):
        expected_properties["MachineName"] = machine["firmware_machine_name"]
    for key, value in material.get("expected_properties", {}).items():
        expected_properties[key] = value
    numeric_keys = {
        "ResolutionX",
        "ResolutionY",
        "LayerHeight",
        "BottomLayerCount",
        "BottomExposureTime",
        "ExposureTime",
        "PrintHeight",
    }
    for key, expected in expected_properties.items():
        actual_text = properties.get(key)
        if key in numeric_keys:
            try:
                actual = float(actual_text)
                target = float(expected)
                tolerance = 1e-5 if key != "PrintHeight" else 0.05
                passed = abs(actual - target) <= tolerance
            except (TypeError, ValueError):
                actual = actual_text
                passed = False
        else:
            actual = actual_text
            passed = str(actual) == str(expected)
        checks.append(
            {
                "id": f"uvtools.property.{key}",
                "passed": passed,
                "actual": actual,
                "expected": expected,
            }
        )

    try:
        print_height = float(properties.get("PrintHeight", "nan"))
        height_passed = print_height <= float(machine["build_volume_mm"][2])
    except (TypeError, ValueError):
        print_height = properties.get("PrintHeight")
        height_passed = False
    checks.append(
        {
            "id": "uvtools.property.PrintHeightWithinMachine",
            "passed": height_passed,
            "actual": print_height,
            "maximum": machine["build_volume_mm"][2],
        }
    )
    passed = all(check["passed"] for check in checks)
    return {
        "schema": "agent-slicer-production-validation/v1",
        "status": "passed" if passed else "rejected",
        "checks": checks,
        "analysis": analysis,
        "limitations": [
            "Detector success does not prove firmware acceptance or a successful physical print.",
            "Drainage topology is validated separately during geometry preparation.",
        ],
    }


def _run(
    executable: str,
    arguments: list[str],
    timeout: int,
    *,
    allowed_exit_codes: tuple[int, ...] = (0, 1),
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [executable, *arguments],
        check=False,
        text=True,
        errors="replace",
        capture_output=True,
        timeout=timeout,
        env=_uvtools_environment(),
    )
    if completed.returncode not in allowed_exit_codes:
        raise RuntimeError(
            f"UVtoolsCmd failed with exit {completed.returncode}: {completed.stderr.strip()}"
        )
    return completed


def _uvtools_gc_conserve_memory() -> int:
    raw = os.environ.get(
        "AGENT_SLICER_UVTOOLS_GC_CONSERVE",
        str(DEFAULT_UVTOOLS_GC_CONSERVE_MEMORY),
    )
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            "AGENT_SLICER_UVTOOLS_GC_CONSERVE must be an integer from 0 to 9"
        ) from exc
    if not 0 <= value <= 9:
        raise ValueError(
            "AGENT_SLICER_UVTOOLS_GC_CONSERVE must be an integer from 0 to 9"
        )
    return value


def _uvtools_environment() -> dict[str, str]:
    environment = native_subprocess_environment()
    # Respect an explicit .NET setting. Otherwise use the measured,
    # memory-conserving default for UVtools' high-resolution CTB scans.
    dotnet_key = next(
        (
            key for key in environment
            if key.lower() == "dotnet_gcconservememory"
        ),
        None,
    )
    if dotnet_key is not None:
        # Windows normalizes environment keys to uppercase; expose the
        # canonical name as well so callers and reports remain portable.
        environment["DOTNET_GCConserveMemory"] = environment[dotnet_key]
    else:
        environment["DOTNET_GCConserveMemory"] = str(
            _uvtools_gc_conserve_memory()
        )
    return environment


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


def _resolved_executable(executable: str) -> str:
    candidate = Path(executable)
    if candidate.is_file():
        return str(candidate.resolve())
    discovered = shutil.which(executable)
    if discovered:
        return str(Path(discovered).resolve())
    raise RuntimeError(f"UVtoolsCmd executable not found: {executable}")


def _analysis_work_root(source: Path, configured: str | Path | None) -> Path:
    requested = configured or os.environ.get("AGENT_SLICER_WORK_ROOT")
    if requested:
        root = Path(requested).expanduser().resolve() / "uvtools"
    elif _is_wsl():
        root = Path.home() / "development-tools" / "agent-slicer-work" / "uvtools"
    else:
        root = source.parent
    root.mkdir(parents=True, exist_ok=True)
    return root


def _is_wsl() -> bool:
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8")
    except OSError:
        return False
    return "microsoft" in release.lower()


def _runtime_kind(executable: Path) -> str:
    try:
        magic = executable.read_bytes()[:4]
    except OSError:
        return "unknown"
    if magic == b"\x7fELF":
        return "linux-elf"
    if magic[:2] == b"MZ":
        return "windows-pe"
    return "unknown"


def _analysis_workers(configured: int | None) -> int:
    value = configured
    if value is None:
        environment = os.environ.get("AGENT_SLICER_UVTOOLS_WORKERS")
        if environment:
            try:
                value = int(environment)
            except ValueError as exc:
                raise ValueError(
                    "AGENT_SLICER_UVTOOLS_WORKERS must be an integer"
                ) from exc
    if value is None:
        value = 1
    if value < 1 or value > 8:
        raise ValueError("UVtools worker count must be between 1 and 8")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
