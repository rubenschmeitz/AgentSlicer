"""Bounded saved-file checks for supported single-plate FDM output."""
from __future__ import annotations

import hashlib
import json
import math
import re
import zipfile
import zlib
from pathlib import Path
from typing import Any, BinaryIO
from xml.etree import ElementTree

MAX_GCODE_BYTES = 2 * 1024**3
MAX_LINE_BYTES = 1024**2
NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
PARAMETER = re.compile(r"([XYZEF])(" + NUMBER + r")", re.IGNORECASE)


def validate_fdm_output(path: str | Path, *, resolved: dict[str, Any] | None = None,
                        expected_profiles: dict[str, dict] | None = None,
                        native_keys: dict[str, list[str]] | None = None) -> dict:
    source = Path(path).resolve()
    checks: list[dict] = []
    result: dict[str, Any] = {"schema": "agent-slicer-validation/v1", "level": "basic", "path": str(source),
        "status": "failed", "checks": checks, "limitations": [
            "Deposited linear moves are checked; vendor purge, calibration, travel and end sequences are outside this bounds audit.",
            "Single plate, single filament, millimetre ASCII G-code only; extruding arcs are rejected, non-extruding spiral travel is outside the deposition audit.",
            "Support appearance, adhesion, material calibration, firmware acceptance and physical printability require review.",
        ]}

    def check(identifier: str, passed: bool, **details: Any) -> None:
        checks.append({"id": identifier, "passed": bool(passed), **details})

    check("file.exists", source.is_file())
    if not source.is_file():
        return result
    try:
        if source.name.lower().endswith(".3mf"):
            with zipfile.ZipFile(source) as archive:
                names = archive.namelist()
                if len(names) != len(set(names)) or len(names) > 10000:
                    raise ValueError("duplicate or excessive 3MF members")
                gcodes = [n for n in names if n.lower().endswith(".gcode")]
                check("fdm.single-sliced-plate", gcodes == ["Metadata/plate_1.gcode"], members=gcodes)
                if gcodes != ["Metadata/plate_1.gcode"]:
                    return result
                settings = json.loads(_read_member(archive, "Metadata/project_settings.config", 16 * 1024**2))
                if not isinstance(settings, dict):
                    raise ValueError("invalid saved FDM settings")
                metadata = _read_member(archive, "Metadata/slice_info.config", 4 * 1024**2)
                if b"<!DOCTYPE" in metadata.upper() or b"<!ENTITY" in metadata.upper():
                    raise ValueError("unsupported XML declarations")
                plates = ElementTree.fromstring(metadata).findall("plate")
                check("fdm.single-plate-metadata", len(plates) == 1)
                if len(plates) != 1:
                    return result
                stats = {item.get("key"): item.get("value") for item in plates[0].findall("metadata")}
                check("fdm.slicer-statistics", _positive(stats.get("prediction")) and _positive(stats.get("weight")), values=stats)
                check("fdm.slicer-outside", stats.get("outside") == "false")
                check("fdm.single-filament", len(plates[0].findall("filament")) == 1)
                result["slicer_warnings"] = [dict(warning.attrib) for warning in plates[0].findall("warning")]
                _validate_settings(settings, resolved, expected_profiles, check)
                if native_keys and expected_profiles:
                    _validate_native_settings(settings, expected_profiles, native_keys, check)
                member = archive.getinfo(gcodes[0])
                if member.file_size > MAX_GCODE_BYTES:
                    raise ValueError("G-code exceeds 2 GiB validation limit")
                with archive.open(member) as handle:
                    audit = _audit_gcode(handle, _volume(settings, resolved))
                expected_md5 = _read_member(archive, "Metadata/plate_1.gcode.md5", 128).decode("ascii").strip().lower()
                check("fdm.gcode-checksum", audit["md5"] == expected_md5)
        else:
            if source.stat().st_size > MAX_GCODE_BYTES:
                raise ValueError("G-code exceeds 2 GiB validation limit")
            # Orca exports its full settings as bounded comment lines. Read only
            # that block, then independently stream the executable toolpath.
            settings = _gcode_settings(source)
            _validate_settings(settings, resolved, expected_profiles, check)
            if native_keys and expected_profiles:
                _validate_native_settings(settings, expected_profiles, native_keys, check)
            with source.open("rb") as handle:
                audit = _audit_gcode(handle, _volume(settings, resolved))
        result["gcode_statistics"] = audit
        check("fdm.gcode-statistics", audit["declared_layers"] == audit["layers"] and audit["layers"] > 0
              and audit["filament_mm"] > 0 and audit["has_time_estimate"], layers=audit["layers"])
        check("fdm.deposited-moves", audit["deposited_moves"] > 0)
        check("fdm.build-volume", audit["out_of_bounds_moves"] == 0, bounds=audit["deposited_bounds_mm"])
        check("fdm.supported-gcode", not audit["unsupported_commands"], commands=audit["unsupported_commands"])
    except (OSError, ValueError, KeyError, TypeError, IndexError, zipfile.BadZipFile, EOFError,
            UnicodeError, ElementTree.ParseError, RuntimeError, zlib.error, OverflowError) as exc:
        check("fdm.readable-structure", False, message=str(exc))
    result["status"] = "passed" if all(item["passed"] for item in checks) else "failed"
    return result


def _validate_native_settings(settings: dict, profiles: dict, native_keys: dict, check: Any) -> None:
    # The CLI merges machine/process before filament. Shared fields such as fan
    # timing are checked at their effective material precedence.
    expected = {}
    for kind in ("machine", "process", "filament"):
        expected.update({key: profiles[kind][key] for key in native_keys.get(kind, []) if key in profiles[kind]})
    for key, value in sorted(expected.items()):
        check(f"fdm.imported-setting.{key}", _equivalent(settings.get(key), value), actual=settings.get(key), expected=value)


def _read_member(archive: zipfile.ZipFile, name: str, limit: int) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > limit or info.flag_bits & 1:
        raise ValueError(f"oversized or encrypted 3MF member: {name}")
    with archive.open(info) as handle:
        value = handle.read(limit + 1)
    if len(value) > limit:
        raise ValueError(f"oversized 3MF member: {name}")
    return value


def _positive(value: Any) -> bool:
    number = float(value or 0)
    return math.isfinite(number) and number > 0


def _volume(settings: dict, resolved: dict | None) -> list[float]:
    if resolved:
        volume = [float(v) for v in resolved["machine"]["build_volume_mm"]]
    else:
        area = settings["printable_area"]
        points = [[float(v) for v in point.lower().split("x")] for point in area]
        volume = [max(p[0] for p in points), max(p[1] for p in points), float(settings["printable_height"])]
        if len(points) != 4 or sorted(points) != sorted([[0, 0], [volume[0], 0], [volume[0], volume[1]], [0, volume[1]]]):
            raise ValueError("G-code bounds validation requires a rectangular bed with origin 0,0")
    if len(volume) != 3 or not all(_positive(v) for v in volume):
        raise ValueError("invalid FDM build volume")
    return volume


def _equivalent(actual: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        if not isinstance(actual, list) and len(expected) == 1:
            return _equivalent(actual, expected[0])
        if not isinstance(actual, list) or len(actual) != len(expected):
            return False
        return all(_equivalent(a, e) for a, e in zip(actual, expected))
    # Single-variant settings may be normalized from scalar to vector by Orca.
    if isinstance(actual, list):
        return len(actual) == 1 and _equivalent(actual[0], expected)
    if isinstance(actual, str) and isinstance(expected, str) and actual.endswith("%") and expected.endswith("%"):
        return _equivalent(actual[:-1], expected[:-1])
    try:
        return math.isclose(float(actual), float(expected), rel_tol=1e-6, abs_tol=1e-6)
    except (TypeError, ValueError):
        return str(actual) == str(expected)


def _validate_settings(settings: dict, resolved: dict | None, profiles: dict | None, check: Any) -> None:
    check("fdm.linear-toolpaths", _equivalent(settings.get("enable_arc_fitting"), "0"))
    if resolved:
        check("fdm.machine-identity", settings.get("printer_settings_id") == resolved["machine"]["upstream_profile"]["name"])
        check("fdm.material-identity", settings.get("filament_settings_id") == [resolved["material"]["upstream_profile"]["name"]])
        check("fdm.layer-height", _equivalent(settings.get("layer_height"), resolved["quality"]["layer_height_mm"]))
        check("fdm.support-settings", _equivalent(settings.get("enable_support"), "1" if resolved["purpose"]["supports"].get("enabled") else "0"))
        check("fdm.nozzle", _equivalent(settings.get("nozzle_diameter"), [resolved["machine"]["settings"]["nozzle_diameter_mm"]]))
        saved_volume = _volume(settings, None)
        check("fdm.machine-volume", _equivalent(saved_volume, resolved["machine"]["build_volume_mm"]))
    if profiles:
        keys = {
            "machine": {"machine_start_gcode", "machine_end_gcode", "nozzle_diameter", "curr_bed_type", "nozzle_type",
                "printer_extruder_variant", "retraction_length", "retraction_speed", "z_hop", "machine_max_speed_x",
                "machine_max_speed_y", "machine_max_speed_z", "machine_max_speed_e", "machine_max_acceleration_x",
                "machine_max_acceleration_y", "machine_max_acceleration_z", "machine_max_acceleration_extruding",
                "machine_max_acceleration_travel"},
            "process": {"layer_height", "initial_layer_print_height", "line_width", "initial_layer_line_width",
                "outer_wall_line_width", "inner_wall_line_width", "top_surface_line_width", "wall_loops", "top_shell_layers",
                "bottom_shell_layers", "sparse_infill_density", "sparse_infill_pattern", "enable_support", "support_type",
                "support_threshold_angle", "support_top_z_distance", "support_bottom_z_distance", "support_on_build_plate_only",
                "initial_layer_speed", "outer_wall_speed", "inner_wall_speed", "sparse_infill_speed", "top_surface_speed",
                "support_speed", "support_interface_speed", "travel_speed", "default_acceleration", "outer_wall_acceleration",
                "initial_layer_acceleration", "travel_acceleration", "overhang_2_4_speed", "overhang_3_4_speed", "overhang_4_4_speed",
                "resolution", "print_extruder_variant", "print_sequence"},
            "filament": {"nozzle_temperature", "nozzle_temperature_initial_layer", "filament_type", "filament_flow_ratio",
                "filament_max_volumetric_speed", "fan_min_speed", "fan_max_speed", "additional_cooling_fan_speed",
                "close_fan_the_first_x_layers", "slow_down_layer_time", "slow_down_min_speed", "textured_plate_temp",
                "textured_plate_temp_initial_layer", "eng_plate_temp", "hot_plate_temp", "supertack_plate_temp", "filament_extruder_variant"},
        }
        for kind, selected in keys.items():
            for key in sorted(selected & profiles[kind].keys()):
                check(f"fdm.saved-setting.{key}", _equivalent(settings.get(key), profiles[kind][key]),
                      actual=settings.get(key), expected=profiles[kind][key])


def _gcode_settings(path: Path) -> dict:
    value = {}
    count = 0
    with path.open("rb") as handle:
        for _ in range(20000):
            raw = handle.readline(MAX_LINE_BYTES + 1)
            count += len(raw)
            if len(raw) > MAX_LINE_BYTES or count > 16 * 1024**2:
                raise ValueError("G-code settings block too large")
            line = raw.decode("utf-8").strip()
            if line == "; CONFIG_BLOCK_END":
                break
            if line.startswith("; ") and " = " in line:
                key, text = line[2:].split(" = ", 1)
                value[key] = text
        else:
            raise ValueError("missing G-code settings block")
    value["printable_area"] = value.get("printable_area", "").split(",")
    for key in ("filament_settings_id", "nozzle_diameter", "filament_type"):
        value[key] = value.get(key, "").strip('"').split(";")
    for key in list(value):
        if key.startswith("machine_max_"):
            value[key] = value[key].split(",")
        elif key.endswith("_extruder_variant"):
            value[key] = [json.loads(item) if item.startswith('"') else item for item in value[key].split(";")]
        elif key in {"machine_start_gcode", "machine_end_gcode"}:
            # Orca's comment block quotes a C-style serialized multiline string.
            # Undo the comment escaping, then the multiline-string escaping.
            text = json.loads('"' + value[key] + '"')
            escapes = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
            value[key] = re.sub(r'\\([nrt"\\])', lambda match: escapes[match.group(1)], text)
    return value


def _audit_gcode(handle: BinaryIO, volume: list[float]) -> dict:
    position: list[float | None] = [None, None, None]
    absolute, extruder_absolute, extruder = True, True, 0.
    layers, declared_layers, deposited, outside = 0, 0, 0, 0
    feature = "Custom"
    low, high = [math.inf] * 3, [-math.inf] * 3
    unsupported: set[str] = set()
    digest = hashlib.md5()
    filament_mm, time_estimate, consumed = 0., False, 0
    generated = False
    while raw := handle.readline(MAX_LINE_BYTES + 1):
        consumed += len(raw)
        if len(raw) > MAX_LINE_BYTES or consumed > MAX_GCODE_BYTES:
            raise ValueError("G-code validation size limit exceeded")
        digest.update(raw)
        line = raw.decode("utf-8").strip()
        if line.startswith(("; generated by OrcaSlicer", "; generated by BambuStudio", "; generated by Bambu Studio", "; BambuStudio ")):
            generated = True
        if line.startswith("; total layer number:"):
            declared_layers = int(line.split(":", 1)[1])
        if line == "; CHANGE_LAYER":
            layers += 1
        if line.startswith("; FEATURE:"):
            feature = line.split(":", 1)[1].strip()
        if line.startswith("; filament used [mm] ="):
            filament_mm = float(line.split("=", 1)[1].strip())
        if line.startswith("; total filament length [mm] :"):
            filament_mm = float(line.split(":", 1)[1].strip())
        if line.startswith("; model printing time:") and "total estimated time:" in line:
            time_estimate = True
        code = line.split(";", 1)[0].strip().upper()
        if not code:
            continue
        match = re.match(r"^([GMT]\d+(?:\.\d+)?)(?=\s|[XYZEF]|$)", code)
        if not match:
            continue
        token = match.group(1)
        numeric, separator, subcode = token[1:].partition(".")
        command = token[0] + str(int(numeric)) + (separator + subcode if separator else "")
        active = layers > 0 and feature != "Custom"
        if command == "G20":
            unsupported.add("G20")
        if command.startswith("T") and active and command != "T0":
            unsupported.add(command)
        if command == "G90":
            absolute = True
        elif command == "G91":
            absolute = False
        elif command == "M82":
            extruder_absolute = True
        elif command == "M83":
            extruder_absolute = False
        elif command in {"G0", "G1", "G2", "G3", "G92"}:
            parameters = {key: float(value) for key, value in PARAMETER.findall(code[match.end():])}
            remainder = PARAMETER.sub("", code[match.end():]).strip()
            if command in {"G0", "G1", "G92"} and remainder:
                raise ValueError(f"unsupported or nonfinite motion parameters: {code[:120]}")
            if not all(math.isfinite(v) for v in parameters.values()):
                raise ValueError("nonfinite G-code coordinates")
            if command == "G92" and active and any(axis in parameters for axis in "XYZ"):
                unsupported.add("G92-XYZ")
            previous = list(position)
            for i, axis in enumerate("XYZ"):
                if axis in parameters:
                    if command == "G92" or absolute:
                        position[i] = parameters[axis]
                    elif position[i] is not None:
                        position[i] += parameters[axis]
            extrusion = 0.
            if "E" in parameters:
                extrusion = parameters["E"] - extruder if extruder_absolute else parameters["E"]
                extruder = parameters["E"] if extruder_absolute or command == "G92" else extruder + parameters["E"]
            if command in {"G2", "G3"} and active and extrusion > 0:
                unsupported.add(command)
            if active and command in {"G0", "G1"} and extrusion > 0 and any(axis in parameters for axis in "XY"):
                if any(v is None for v in position + previous):
                    raise ValueError("deposited move has unknown coordinates")
                deposited += 1
                for point in (previous, position):
                    for i, v in enumerate(point):
                        low[i], high[i] = min(low[i], v), max(high[i], v)
                if any(v < -0.001 or v > volume[i] + 0.001 for point in (previous, position) for i, v in enumerate(point)):
                    outside += 1
        elif active and command in {"G28", "G29"}:
            unsupported.add(command)
        elif active and command.startswith("G") and command not in {"G4", "G17", "G18", "G19", "G21", "G90", "G91"}:
            unsupported.add(command)
    if not generated or not math.isfinite(filament_mm):
        raise ValueError("missing supported slicer header or invalid filament statistics")
    return {"layers": layers, "declared_layers": declared_layers, "filament_mm": filament_mm,
            "has_time_estimate": time_estimate, "deposited_moves": deposited, "out_of_bounds_moves": outside,
            "deposited_bounds_mm": {"min": low, "max": high} if deposited else None,
            "unsupported_commands": sorted(unsupported), "md5": digest.hexdigest(), "bytes": consumed}
