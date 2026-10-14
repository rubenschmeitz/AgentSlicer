"""Translate backend-neutral presets into installed OrcaSlicer profiles.

Vendor profiles (including firmware-specific G-code) stay in OrcaSlicer's
installation. No upstream slicer code or profiles are bundled in this package.
"""
from __future__ import annotations

import json
import math
import os
import hashlib
from copy import deepcopy
from pathlib import Path
from typing import Any

from .project import sha256_file


def profile_root(executable: str, configured: str | Path | None = None) -> Path:
    requested = configured or os.environ.get("AGENT_SLICER_FDM_PROFILE_ROOT")
    if requested:
        root = Path(requested).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"FDM profile directory does not exist: {root}")
        return root
    location = Path(executable).resolve().parent
    for parent in (location, *list(location.parents)[:3]):
        for relative in ("resources/profiles", "share/OrcaSlicer/profiles", "share/BambuStudio/profiles", "profiles"):
            candidate = parent / relative
            if candidate.is_dir():
                return candidate
    raise ValueError("OrcaSlicer profiles not found; set AGENT_SLICER_FDM_PROFILE_ROOT")


def _profile_name(preset: dict[str, Any]) -> str:
    name = preset.get("upstream_profile", {}).get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("FDM presets must identify an installed upstream_profile.name")
    return name


def resolve_fdm_profiles(
    resolved: dict[str, Any], executable: str, configured_root: str | Path | None = None,
) -> dict[str, Any]:
    root = profile_root(executable, configured_root)
    index: dict[str, list[Path]] = {}
    for path in sorted(root.rglob("*.json")):
        index.setdefault(path.stem, []).append(path)
    evidence: dict[str, dict[str, str]] = {}
    native_evidence = {}
    archived = any(isinstance(resolved[label].get("upstream_profile", {}).get("resolved_settings"), dict)
                   for label in ("machine", "material", "quality"))
    if archived and "bambu" not in executable.lower():
        raise ValueError("Collected Bambu presets require Bambu Studio; set AGENT_SLICER_BAMBU_STUDIO or --fdm-slicer")

    def load(name: str, kind: str, vendor: str | None = None, stack: tuple[str, ...] = ()) -> dict:
        if name in stack:
            raise ValueError(f"FDM profile inheritance cycle: {name}")
        if len(stack) >= 32:
            raise ValueError("FDM profile inheritance exceeds 32 levels")
        candidates = [p for p in index.get(name, []) if kind in p.relative_to(root).parts]
        preferred = [p for p in candidates if vendor and p.relative_to(root).parts[0] == vendor]
        candidates = preferred or candidates
        if len(candidates) != 1:
            raise ValueError(f"installed {kind} profile {name!r}: expected one match, found {len(candidates)}")
        path = candidates[0]
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("name", name) != name or data.get("type", kind) != kind:
            raise ValueError(f"invalid installed FDM profile identity: {path}")
        evidence[str(path)] = {"path": str(path), "sha256": sha256_file(path), "name": name}
        parent = data.get("inherits")
        value = load(parent, kind, path.relative_to(root).parts[0], (*stack, name)) if parent else {}
        includes = data.get("include", [])
        if isinstance(includes, str):
            includes = [includes]
        for included in includes:
            value.update(load(included, kind, path.relative_to(root).parts[0], (*stack, name)))
        value.update(deepcopy(data))
        value.pop("inherits", None)
        value.pop("include", None)
        return value

    machine_name = _profile_name(resolved["machine"])
    profiles = {}
    for kind, label in (("machine", "machine"), ("process", "quality"), ("filament", "material")):
        preset = resolved[label]
        name = _profile_name(preset)
        upstream = preset.get("upstream_profile", {})
        native = upstream.get("resolved_settings")
        template = name
        if isinstance(native, dict) and name not in index:
            parents = upstream.get("sources", [])
            template = parents[-1].get("inherits") if parents else None
            if not template or template not in index:
                if kind != "filament":
                    raise ValueError(f"missing installed base for imported {kind} profile {name!r}")
                # A self-contained user filament can use the generic firmware
                # template for its explicitly saved polymer and printer family.
                polymer = native["filament_type"][0]
                family = {"Bambu Lab P2S": "P2S", "Bambu Lab X1 Carbon": "X1C"}.get(
                    resolved["machine"].get("upstream_profile", {}).get("resolved_settings", {}).get("printer_model"))
                template = f"Generic {polymer} @BBL {family}"
                if template not in index and polymer in {"PA-GF", "PA-CF"}:
                    # Reinforced nylon keeps every saved material value/type;
                    # only the excluded firmware snippet uses its nylon-family
                    # template. It is not substituted with generic PA settings.
                    template = f"Generic PA @BBL {family}"
        profile = load(template, kind)
        if isinstance(native, dict):
            from .bambu_presets import PRIVATE_KEY, PRIVATE_VALUE, FREE_TEXT
            unsafe = [key for key, value in native.items() if PRIVATE_KEY.search(key) or key in FREE_TEXT
                      or PRIVATE_VALUE.search(json.dumps(value))]
            if unsafe:
                raise ValueError(f"unsafe fields in imported Bambu settings: {sorted(unsafe)}")
            profile.update(deepcopy(native))
            source_scope = upstream.get("sources", [{}])[-1].get("scope", "user")
            profile.update(name=name, type=kind, **{"from": "system" if source_scope == "system" else "user"})
            if source_scope != "system":
                profile["inherits"] = template
            native_evidence[kind] = {"sha256": hashlib.sha256(json.dumps(native, sort_keys=True).encode()).hexdigest(),
                "sources": upstream.get("sources", []), "keys": sorted(native), "excluded_keys": upstream.get("excluded_keys", []),
                "firmware_template": template}
        profiles[kind] = profile
    for kind in ("process", "filament"):
        compatible = profiles[kind].get("compatible_printers", [])
        if compatible and machine_name not in compatible:
            raise ValueError(f"installed {kind} profile is incompatible with {machine_name}")
    machine = profiles["machine"]
    expected_nozzle = resolved["machine"].get("settings", {}).get("nozzle_diameter_mm")
    if expected_nozzle is None or float(machine["nozzle_diameter"][0]) != float(expected_nozzle):
        raise ValueError("installed FDM nozzle diameter differs from machine preset")
    volume = [float(v) for v in resolved["machine"]["build_volume_mm"]]
    points = [[float(v) for v in point.lower().split("x")] for point in machine["printable_area"]]
    if (any(len(p) != 2 for p in points) or len(points) != 4
            or sorted(points) != sorted([[0., 0.], [volume[0], 0.], [volume[0], volume[1]], [0., volume[1]]])
            or float(machine["printable_height"]) != volume[2]):
        raise ValueError("installed FDM build volume differs from machine preset (rectangular beds required)")
    if not machine.get("machine_start_gcode") or not machine.get("machine_end_gcode"):
        raise ValueError("installed machine profile lacks firmware start/end G-code")

    # P2S profiles contain parallel Standard/High Flow values. Pin the requested
    # variant before loading, rather than depending on backend default selection.
    for kind, variant_key, label in (("machine", "printer_extruder_variant", "machine"),
                                      ("process", "print_extruder_variant", "quality"),
                                      ("filament", "filament_extruder_variant", "material")):
        profile = profiles[kind]
        variants = profile.get(variant_key)
        requested = resolved[label].get("upstream_profile", {}).get("extruder_variant")
        if variants and requested:
            if requested not in variants:
                raise ValueError(f"installed {kind} profile lacks extruder variant {requested!r}")
            variant_index = variants.index(requested)
            for key, value in list(profile.items()):
                if kind == "machine" and key.startswith("machine_max_") and isinstance(value, list) and len(value) == 2 * len(variants):
                    profile[key] = value[variant_index * 2:variant_index * 2 + 2]
                elif isinstance(value, list) and len(value) == len(variants) and key not in {"compatible_printers", "bed_exclude_area", "printable_area"}:
                    profile[key] = [value[variant_index]]
                elif isinstance(value, list) and len(value) > 1 and all(item == value[0] for item in value) and key not in {"compatible_printers", "bed_exclude_area", "printable_area"}:
                    # A copied user profile may retain identical inherited E3D
                    # values after reducing its variant list to Standard/High Flow.
                    profile[key] = [value[0]]

    _apply_settings(profiles, resolved)
    for kind, receipt in native_evidence.items():
        profile = profiles[kind]
        omitted = {}
        informational = {"compatible_printers", "extruder_clearance_radius", "extruder_height_gap",
            "deretract_speed_extruder_change", "filament_contact_safe", "filament_emission_safe", "filament_ingredients_safe",
            "filament_dev_ams_drying_ams_limitations", "filament_dev_ams_drying_temperature", "filament_dev_ams_drying_time"}
        # The collection includes UI/certification/drying/sequential-operation
        # fields. They stay in the archive; this single-filament, by-layer build
        # never executes those operations. All omissions are explicit evidence.
        for key in informational:
            if key in profile:
                omitted[key] = profile[key]
        for key in ("compatible_prints", "compatible_prints_condition"):
            if key not in profile:
                continue
            constraint = profile[key]
            if constraint not in ([], "", [""]):
                raise ValueError(f"unsupported imported process compatibility constraint: {key}")
            omitted[key] = profile.pop(key)
        for key in ("filament_long_retractions_when_ec", "filament_retraction_distances_when_ec"):
            if key in profile and all(value == "nil" for value in profile[key]):
                omitted[key] = profile.pop(key)
        if "adaptive_layer_height" in profile:
            if str(profile["adaptive_layer_height"]) != "0":
                raise ValueError("imported adaptive layer-height workflow is not supported")
            omitted["adaptive_layer_height"] = profile.pop("adaptive_layer_height")
        if "layer_time_smoothing" in profile:
            if str(profile["layer_time_smoothing"]) != "0":
                raise ValueError("the installed backend cannot preserve enabled layer-time smoothing")
            for key in ("layer_time_smoothing", "layer_time_smoothing_threshold"):
                if key in profile:
                    omitted[key] = profile.pop(key)
        translations = {}
        if "wall_infill_order" in profile:
            order = profile.pop("wall_infill_order")
            choices = {"inner wall/outer wall/infill": "inner wall/outer wall",
                "outer wall/inner wall/infill": "outer wall/inner wall", "inner-outer-inner wall/infill": "inner-outer-inner wall"}
            if order not in choices:
                raise ValueError(f"unsupported imported wall/infill order: {order}")
            profile["wall_sequence"] = choices[order]
            translations["wall_infill_order"] = "wall_sequence"
        if "only_one_wall_top" in profile:
            enabled = profile.pop("only_one_wall_top")
            profile["top_one_wall_type"] = "all top" if str(enabled) == "1" else "not apply"
            translations["only_one_wall_top"] = "top_one_wall_type"
        for key in ("bottom_surface_density", "top_surface_density", "monotonic_travel_into_wall"):
            if key in profile and not str(profile[key]).endswith("%"):
                profile[key] = str(profile[key]) + "%"
        if "best_object_pos" in profile:
            profile["best_object_pos"] = profile["best_object_pos"].lower().replace("x", ",")
        if "extruder_offset" in profile:
            profile["extruder_offset"] = [point.lower() for point in profile["extruder_offset"]]
        receipt["informational_or_inactive_fields"] = omitted
        receipt["translations"] = translations
        receipt["validated_keys"] = sorted(((set(receipt["keys"]) & set(profile)) - set(omitted)) | set(translations.values()))
    # Bambu Studio has no Orca pressure-advance extension. Disabled calibration
    # values stay in source evidence; enabling an unsupported extension blocks.
    if "bambu" in executable.lower():
        filament = profiles["filament"]
        if "pressure_advance" in filament or "enable_pressure_advance" in filament:
            flag = filament.get("enable_pressure_advance", ["0"])
            if str(flag[0] if isinstance(flag, list) else flag).lower() not in {"0", "0.0", "false"}:
                raise ValueError("Bambu Studio cannot apply the enabled pressure-advance extension")
            native_evidence.setdefault("filament", {})["inactive_calibration"] = {
                key: filament.pop(key) for key in ("pressure_advance", "enable_pressure_advance") if key in filament}
            if "validated_keys" in native_evidence["filament"]:
                native_evidence["filament"]["validated_keys"] = [key for key in native_evidence["filament"]["validated_keys"] if key in filament]
    return {"profiles": profiles, "profile_root": str(root), "sources": list(evidence.values()), "native_evidence": native_evidence}


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError("FDM settings must be finite")
        return str(value)
    if not isinstance(value, str):
        raise ValueError("FDM setting must be a scalar")
    return value


def _apply_settings(profiles: dict[str, dict], resolved: dict[str, Any]) -> None:
    process, filament, machine = (profiles[k] for k in ("process", "filament", "machine"))
    quality = resolved["quality"]
    settings = quality.get("settings", {})
    if float(settings.get("layer_height_mm", quality["layer_height_mm"])) != float(quality["layer_height_mm"]):
        raise ValueError("FDM quality layer height and settings.layer_height_mm must agree")
    process_map = {
        "layer_height_mm": "layer_height", "initial_layer_height_mm": "initial_layer_print_height",
        "line_width_mm": "line_width", "initial_layer_line_width_mm": "initial_layer_line_width",
        "outer_wall_line_width_mm": "outer_wall_line_width", "inner_wall_line_width_mm": "inner_wall_line_width",
        "top_surface_line_width_mm": "top_surface_line_width", "wall_loops": "wall_loops",
        "top_shell_layers": "top_shell_layers", "bottom_shell_layers": "bottom_shell_layers",
        "sparse_infill_percent": "sparse_infill_density", "sparse_infill_pattern": "sparse_infill_pattern",
        "support_enabled": "enable_support", "support_type": "support_type",
        "support_threshold_angle_deg": "support_threshold_angle", "support_top_z_distance_mm": "support_top_z_distance",
        "support_bottom_z_distance_mm": "support_bottom_z_distance", "mesh_resolution_mm": "resolution",
    }
    groups = {"speeds_mm_s": {"initial_layer": "initial_layer_speed", "outer_wall": "outer_wall_speed",
                  "inner_wall": "inner_wall_speed", "sparse_infill": "sparse_infill_speed", "top_surface": "top_surface_speed",
                  "support": "support_speed", "support_interface": "support_interface_speed", "travel": "travel_speed"},
              "accelerations_mm_s2": {"default": "default_acceleration", "outer_wall": "outer_wall_acceleration",
                  "initial_layer": "initial_layer_acceleration", "travel": "travel_acceleration"},
              "overhang_speeds_mm_s": {"25_to_50_percent": "overhang_2_4_speed", "50_to_75_percent": "overhang_3_4_speed",
                  "75_to_100_percent": "overhang_4_4_speed"}}

    def put(profile: dict, key: str, value: Any) -> None:
        converted = _scalar(value)
        profile[key] = [converted] if isinstance(profile.get(key), list) else converted

    hardware = resolved["machine"]["settings"]
    allowed_hardware = {"technology", "build_volume_mm", "nozzle_diameter_mm", "nozzle_type", "max_nozzle_temperature_c",
                        "max_bed_temperature_c", "machine_max_speed_mm_s", "machine_max_acceleration_mm_s2", "retraction", "default_bed"}
    if set(hardware) - allowed_hardware:
        raise ValueError(f"unmapped FDM machine settings: {sorted(set(hardware) - allowed_hardware)}")
    if hardware.get("technology") != "FFF" or hardware.get("build_volume_mm") != resolved["machine"]["build_volume_mm"]:
        raise ValueError("FDM machine settings must declare FFF and the configured build volume")
    for key in ("max_nozzle_temperature_c", "max_bed_temperature_c"):
        if not _positive_number(hardware[key]):
            raise ValueError(f"invalid FDM machine hard limit: {key}")
    machine["nozzle_type"] = [_scalar(hardware["nozzle_type"])]
    for group, prefix, allowed_axes in (("machine_max_speed_mm_s", "machine_max_speed_", {"x", "y", "z", "e"}),
                                       ("machine_max_acceleration_mm_s2", "machine_max_acceleration_", {"x", "y", "z", "e", "extruding", "travel", "retracting"})):
        for axis, value in hardware[group].items():
            if axis not in allowed_axes or not _positive_number(value):
                raise ValueError(f"invalid FDM machine limit: {group}.{axis}")
            target = prefix + axis
            vendor_limits = machine.get(target)
            if vendor_limits:
                if float(value) > max(float(v) for v in vendor_limits):
                    raise ValueError(f"FDM {group}.{axis} exceeds installed machine limit")
                machine[target] = [_scalar(min(float(v), float(value))) for v in vendor_limits]
            else:
                machine[target] = [_scalar(value), _scalar(value)]
    retract_map = {"length_mm": "retraction_length", "speed_mm_s": "retraction_speed", "z_hop_mm": "z_hop"}
    for key, value in hardware.get("retraction", {}).items():
        if key not in retract_map or not math.isfinite(float(value)) or float(value) < 0:
            raise ValueError(f"invalid FDM retraction setting: {key}")
        machine[retract_map[key]] = [_scalar(value)]

    unknown = set(settings) - set(process_map) - set(groups)
    if unknown:
        raise ValueError(f"unmapped FDM quality settings: {sorted(unknown)}")
    for key, target in process_map.items():
        if key in settings:
            if key not in {"sparse_infill_pattern", "support_enabled", "support_type"}:
                number = float(settings[key])
                if not math.isfinite(number) or number < 0:
                    raise ValueError(f"invalid FDM quality setting: {key}")
                if key in {"wall_loops", "top_shell_layers", "bottom_shell_layers"} and (not number.is_integer() or number > 1000):
                    raise ValueError(f"FDM {key} must be an integer at most 1000")
                if key == "sparse_infill_percent" and number > 100:
                    raise ValueError("FDM infill must be between 0 and 100 percent")
                if key.endswith("line_width_mm") and not 0 < number <= float(machine["nozzle_diameter"][0]) * 3:
                    raise ValueError(f"FDM {key} must be positive and at most three nozzle diameters")
            value = f"{settings[key]}%" if key == "sparse_infill_percent" else settings[key]
            put(process, target, value)
    put(process, "layer_height", quality["layer_height_mm"])
    if not 0 < float(quality["layer_height_mm"]) <= float(machine["nozzle_diameter"][0]) * 0.8:
        raise ValueError("FDM layer height must be positive and at most 80% of nozzle diameter")
    for group, mapping in groups.items():
        for key, value in settings.get(group, {}).items():
            if key not in mapping:
                raise ValueError(f"unmapped FDM setting: {group}.{key}")
            if float(value) <= 0:
                raise ValueError(f"FDM {group}.{key} must be positive")
            limits = resolved["machine"]["settings"]
            if group == "speeds_mm_s":
                maximum = min(limits["machine_max_speed_mm_s"][axis] for axis in ("x", "y"))
                if float(value) > maximum:
                    raise ValueError(f"FDM {group}.{key} exceeds machine speed limit")
            if group == "accelerations_mm_s2":
                maximum = limits["machine_max_acceleration_mm_s2"]["travel" if key == "travel" else "extruding"]
                if float(value) > maximum:
                    raise ValueError(f"FDM {group}.{key} exceeds machine acceleration limit")
            put(process, mapping[key], value)
    supports = resolved["purpose"]["supports"]
    put(process, "enable_support", bool(supports.get("enabled", False)))
    put(process, "support_type", supports.get("type", "tree(auto)"))
    if "build_plate_only" in supports:
        put(process, "support_on_build_plate_only", bool(supports["build_plate_only"]))
    if supports.get("threshold", "quality-profile") != "quality-profile":
        put(process, "support_threshold_angle", supports["threshold"])
    # Keep one plate and ASCII linear G-code so independent validation can read
    # every deposited move. Firmware sequences always come from the vendor.
    process.update({"print_sequence": "by layer", "enable_arc_fitting": "0", "enable_prime_tower": "0"})

    material_map = {"filament_type": "filament_type", "nozzle_temperature_c": "nozzle_temperature",
        "initial_nozzle_temperature_c": "nozzle_temperature_initial_layer", "flow_ratio": "filament_flow_ratio",
        "max_volumetric_speed_mm3_s": "filament_max_volumetric_speed", "part_fan_min_percent": "fan_min_speed",
        "part_fan_max_percent": "fan_max_speed", "aux_fan_percent": "additional_cooling_fan_speed",
        "fan_off_first_layers": "close_fan_the_first_x_layers", "slow_down_layer_time_s": "slow_down_layer_time",
        "slow_down_min_speed_mm_s": "slow_down_min_speed", "textured_pei_bed_temperature_c": "textured_plate_temp",
        "engineering_plate_temperature_c": "eng_plate_temp", "hot_plate_temperature_c": "hot_plate_temp",
        "supertack_plate_temperature_c": "supertack_plate_temp", "pressure_advance": "pressure_advance",
        "enable_pressure_advance": "enable_pressure_advance"}
    material = resolved["material"]["settings"]
    if set(material) - set(material_map):
        raise ValueError(f"unmapped FDM material settings: {sorted(set(material) - set(material_map))}")
    limits = resolved["machine"]["settings"]
    for key, value in material.items():
        if key != "filament_type":
            number = float(value)
            if not math.isfinite(number) or number < 0:
                raise ValueError(f"invalid FDM material setting: {key}")
            if key.endswith("percent") and number > 100:
                raise ValueError(f"FDM {key} must be at most 100")
            if key in {"flow_ratio", "max_volumetric_speed_mm3_s"} and number <= 0:
                raise ValueError(f"FDM {key} must be positive")
        if "temperature_c" in key:
            limit = limits["max_nozzle_temperature_c" if "nozzle" in key else "max_bed_temperature_c"]
            if not 0 < float(value) <= float(limit):
                raise ValueError(f"FDM {key} exceeds machine temperature limit")
        target = material_map[key]
        filament[target] = [_scalar(value)]
        if "plate_temp" in target:
            filament[target + "_initial_layer"] = [_scalar(value)]
    machine["curr_bed_type"] = limits.get("default_bed", "Textured PEI Plate")


def _positive_number(value: Any) -> bool:
    return math.isfinite(float(value)) and float(value) > 0
