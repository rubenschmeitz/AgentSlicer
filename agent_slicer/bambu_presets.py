"""Offline Bambu profile resolution; never reads network, printer or login files."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .presets import PresetError

# Free text, executable snippets and service identifiers are deliberately excluded.
METADATA = {
    "name", "type", "from", "version", "inherits", "include", "instantiation",
    "setting_id", "filament_id", "filament_settings_id", "print_settings_id",
    "printer_settings_id", "description", "filament_notes", "printer_notes",
    "notes", "alias", "renamed_from",
}
FREE_TEXT = {"description", "filament_notes", "printer_notes", "notes"}
# These saved calibration fields occur in user filament exports even when the
# active Bambu vendor schema does not declare them. Preserve them explicitly.
CALIBRATION_FIELDS = {"pressure_advance", "enable_pressure_advance"}
USER_PRINT_FIELDS = {
    "compatible_printers_condition", "compatible_prints", "compatible_prints_condition",
    "default_filament_colour", "enable_overhang_bridge_fan",
    "overhang_threshold_participating_cooling", "first_x_layer_part_fan_speed",
    "ironing_fan_speed", "support_remove_small_overhang", "tree_support_branch_distance",
}
PRIVATE_KEY = re.compile(
    r"gcode|credential|password|token|account|user_id|access_code|serial|"
    r"network|login|host|ip_address|device_id|printer_id|url", re.I
)
PRIVATE_VALUE = re.compile(r"https?://|(?:[A-Z]:[\\/])|/Users/|/home/|@[^ ]+\.[a-z]{2,}", re.I)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


class BambuProfiles:
    """Resolve parent -> ordered includes -> child, retaining vector values.

    Include templates in the inspected vendor bundle are sparse, so their
    explicit keys are applied (no implicit application defaults are invented).
    Missing links, duplicate system names and cycles fail closed.
    """

    def __init__(self, system: Path):
        self.system = system
        self.index = {}
        self.allowed = {}
        for kind in ("machine", "filament", "process"):
            self.index[kind] = {}
            self.allowed[kind] = set()
            self.allowed[kind].update(USER_PRINT_FIELDS)
            if kind == "filament":
                self.allowed[kind].update(CALIBRATION_FIELDS)
            for path in sorted((system / kind).glob("*.json")):
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                name = data.get("name") or path.stem
                if name in self.index[kind]:
                    raise PresetError(f"duplicate system profile: {name}")
                self.index[kind][name] = path
                self.allowed[kind].update(data)

    def resolve(self, kind: str, name: str, *, path: Path | None = None, stack=()):
        path = path or self.index[kind].get(name)
        if path is None:
            raise PresetError(f"missing {kind} profile: {name}")
        if path in stack:
            raise PresetError(f"inheritance/include cycle: {name}")
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        settings, sources, excluded = {}, [], set()
        links = ([data["inherits"]] if data.get("inherits") else [])
        includes = data.get("include", [])
        if isinstance(includes, str):
            includes = [includes]
        links += includes
        for parent in links:
            resolved, receipt, dropped = self.resolve(kind, parent, stack=(*stack, path))
            settings.update(resolved)
            sources.extend(receipt)
            excluded.update(dropped)
        for key, value in data.items():
            if key in FREE_TEXT:
                excluded.add(key)
                continue
            if key in METADATA:
                continue
            if (key not in self.allowed[kind] or PRIVATE_KEY.search(key)
                    or PRIVATE_VALUE.search(json.dumps(value))):
                excluded.add(key)
                continue
            settings[key] = value
        scope = "system" if path.is_relative_to(self.system) else "user"
        sources.append({
            "scope": scope, "kind": kind, "file": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "inherits": data.get("inherits", ""), "includes": includes,
        })
        return settings, sources, sorted(excluded)


def first_number(settings, key):
    value = settings[key]
    return float(value[0] if isinstance(value, list) else value)


def definition(kind, name, settings, sources, excluded, host, version):
    value = {
        "display_name": name, "process": "fdm", "settings": {},
        "upstream_profile": {
            "vendor": "Bambu Studio", "name": name, "host": host,
            "vendor_config_version": version,
            "resolved_settings": settings, "sources": sources,
            "excluded_keys": excluded,
            "backend_status": "unavailable: fdm.slice and fdm.validate.basic",
        },
        "limitations": [
            "Bambu vectors are retained by extruder variant, not collapsed.",
            "Full upstream settings are archival; no Agent-Slicer FDM adapter consumes them.",
            "Free text, G-code snippets and service identifiers are excluded and reported.",
            "Implicit application defaults are not reconstructed; this is file inheritance resolution.",
        ],
    }
    # Mapping is intentionally explicit; all other keys remain in the report.
    maps = {
        "filament": {
            "filament_flow_ratio": "flow_ratio",
            "filament_max_volumetric_speed": "max_volumetric_speed_mm3_s",
            "nozzle_temperature": "nozzle_temperature_c",
            "nozzle_temperature_initial_layer": "initial_nozzle_temperature_c",
            "textured_plate_temp": "textured_pei_bed_temperature_c",
            "pressure_advance": "pressure_advance",
        },
        "process": {"layer_height": "layer_height_mm"},
        "machine": {"nozzle_diameter": "nozzle_diameter_mm"},
    }[kind]
    variants = settings.get("filament_extruder_variant", settings.get("print_extruder_variant", []))
    mapped = set()
    for source, target in maps.items():
        if source in settings:
            raw = settings[source]
            if isinstance(raw, list) and len(raw) > 1 and len(set(raw)) > 1:
                if "Direct Drive Standard" not in variants:
                    continue
                raw = raw[variants.index("Direct Drive Standard")]
            else:
                raw = raw[0] if isinstance(raw, list) else raw
            value["settings"][target] = float(raw)
            mapped.add(source)
    value["upstream_profile"]["unmapped_keys"] = sorted(set(settings) - mapped)
    value["upstream_profile"]["user_extension_keys"] = sorted(
        (CALIBRATION_FIELDS | USER_PRINT_FIELDS) & set(settings)
    )
    if kind == "filament" and "pressure_advance" in settings:
        value["upstream_profile"]["calibration_extensions"] = sorted(CALIBRATION_FIELDS & set(settings))
    if kind == "machine":
        points = [[float(v) for v in point.split("x")] for point in settings["printable_area"]]
        value.update(build_volume_mm=[
            max(p[0] for p in points) - min(p[0] for p in points),
            max(p[1] for p in points) - min(p[1] for p in points),
            first_number(settings, "printable_height"),
        ], nozzle_diameter_mm=first_number(settings, "nozzle_diameter"),
            output_formats=["3mf", "gcode.3mf"], default_output_format="gcode.3mf")
    if kind == "process":
        value["layer_height_mm"] = first_number(settings, "layer_height")
        value["validation_policy"] = "standard"
    return value
