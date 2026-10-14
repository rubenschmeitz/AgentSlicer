from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any


CTB_ENCRYPTED_MAGIC = 0x12FD0107
GOO_VERSION_PREFIX = b"V3.0"


def validate_sliced_output(
    path: str | Path,
    output_format: str | None = None,
) -> dict[str, Any]:
    source = Path(path)
    selected = (output_format or ("gcode.3mf" if source.name.lower().endswith(".gcode.3mf") else source.suffix.lstrip("."))).lower()
    if selected in {"3mf", "gcode.3mf", "gcode"}:
        from .fdm_validation import validate_fdm_output
        return validate_fdm_output(source)
    if selected == "ctb":
        return validate_ctb(source)
    if selected == "goo":
        return validate_goo(source)
    raise ValueError(f"no basic validator for output format {selected!r}")


def validate_ctb(path: str | Path) -> dict[str, Any]:
    source = Path(path).resolve()
    checks: list[dict[str, Any]] = []
    if not source.is_file():
        return {
            "status": "failed",
            "path": str(source),
            "checks": [{"id": "file.exists", "passed": False}],
        }
    size = source.stat().st_size
    checks.append({"id": "file.nonempty", "passed": size > 128, "value": size})
    with source.open("rb") as handle:
        prefix = handle.read(4)
    magic = struct.unpack("<I", prefix)[0] if len(prefix) == 4 else None
    checks.append(
        {
            "id": "ctb.encrypted-magic",
            "passed": magic == CTB_ENCRYPTED_MAGIC,
            "value": f"0x{magic:08X}" if magic is not None else None,
        }
    )
    passed = all(check["passed"] for check in checks)
    return {
        "schema": "agent-slicer-validation/v1",
        "status": "passed" if passed else "failed",
        "level": "basic",
        "path": str(source),
        "checks": checks,
        "limitations": [
            "Basic validation checks file structure markers only.",
            "Firmware acceptance and physical printability are not established.",
        ],
    }


def validate_goo(path: str | Path) -> dict[str, Any]:
    source = Path(path).resolve()
    checks: list[dict[str, Any]] = []
    if not source.is_file():
        return {
            "status": "failed",
            "path": str(source),
            "checks": [{"id": "file.exists", "passed": False}],
        }
    size = source.stat().st_size
    checks.append({"id": "file.nonempty", "passed": size > 128, "value": size})
    with source.open("rb") as handle:
        prefix = handle.read(len(GOO_VERSION_PREFIX))
    checks.append(
        {
            "id": "goo.version-prefix",
            "passed": prefix == GOO_VERSION_PREFIX,
            "value": prefix.decode("ascii", errors="replace"),
        }
    )
    passed = all(check["passed"] for check in checks)
    return {
        "schema": "agent-slicer-validation/v1",
        "status": "passed" if passed else "failed",
        "level": "basic",
        "path": str(source),
        "checks": checks,
        "limitations": [
            "Basic validation checks file structure markers only.",
            "Firmware acceptance and physical printability are not established.",
        ],
    }


def write_validation(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
