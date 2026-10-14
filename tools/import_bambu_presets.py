"""Create an additive, host/version-scoped external catalog from printing files."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_slicer.bambu_presets import BambuProfiles, definition, slug
from agent_slicer.presets import validate_catalog_directory


def collect(root: Path, output: Path, host: str, date: str):
    # Only this selected section is read. Never copy the complete configuration.
    config, _ = json.JSONDecoder().raw_decode(
        (root / "BambuStudio.conf").read_text(encoding="utf-8-sig").lstrip()
    )
    selected = config["presets"]
    selection = {k: selected[k] for k in ("machine", "process", "filaments")}
    version = json.loads((root / "system/BBL.json").read_text())["version"]
    profiles = BambuProfiles(root / "system/BBL")
    prefix = slug(f"{host}-{date}-{version}")
    catalogs = {k: {} for k in ("machines", "materials", "qualities", "purposes")}
    records = []
    machine_ids = {}

    def add(kind, name, path=None):
        settings, sources, excluded = profiles.resolve(kind, name, path=path)
        scope = "user" if path else "system"
        # Digest keeps conflicting versions separate, including duplicate user names.
        preset_id = f"{prefix}-{scope}-{slug(name)}-{sources[-1]['sha256'][:8]}"
        catalog = {"machine": "machines", "filament": "materials", "process": "qualities"}[kind]
        value = definition(kind, name, settings, sources, excluded, host, version)
        if kind == "machine":
            machine_ids[name] = preset_id
        else:
            compatibility = settings.get("compatible_printers", [])
            if kind == "process" and "0.2 nozzle" in name:
                compatibility = ["Bambu Lab P2S 0.2 nozzle"]
            supported = []
            for machine in compatibility:
                if machine not in profiles.index["machine"]:
                    continue
                if machine not in machine_ids:
                    add("machine", machine)
                supported.append(machine_ids[machine])
            if not supported:
                raise ValueError(f"no confirmed machine compatibility for {name}")
            value["compatible_machines"] = supported
        catalogs[catalog][preset_id] = value
        records.append({"id": preset_id, "catalog": catalog, "name": name, "scope": scope})
        return preset_id

    selected_ids = {"machine": add("machine", selection["machine"])}
    selected_ids["quality"] = add("process", selection["process"])
    selected_ids["materials"] = [add("filament", n) for n in selection["filaments"]]
    for path in sorted((root / "user").rglob("*.json")):
        if path.parent.name not in {"filament", "process", "machine", "base"}:
            continue
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        kind = "filament" if path.parent.name == "base" else path.parent.name
        add(kind, data.get("name", path.stem), path)
    # Purposes carry source support enablement so the existing purpose precedence
    # cannot silently disable the imported miniature process's supports.
    for quality_id, quality in catalogs["qualities"].items():
        settings = quality["upstream_profile"]["resolved_settings"]
        enabled = settings.get("enable_support", "0")
        enabled = enabled[0] if isinstance(enabled, list) else enabled
        purpose_id = quality_id + "-source-supports"
        catalogs["purposes"][purpose_id] = {
            "process": "fdm", "compatible_machines": quality["compatible_machines"],
            "required_capabilities": ["mesh.inspect", "fdm.slice", "fdm.validate.basic"],
            "supports": {"enabled": str(enabled).lower() in {"1", "true"},
                         "type": settings.get("support_type", "normal(auto)")},
            "geometry": {"repair": False, "hollow": False},
            "arrangement": {"enabled": True, "clearance_mm": 5.0},
        }
        if quality_id == selected_ids["quality"]:
            selected_ids["purpose"] = purpose_id
    output.mkdir(parents=True, exist_ok=False)
    for name, catalog in catalogs.items():
        (output / (name + ".json")).write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n")
    provenance = {
        "schema": "agent-slicer-bambu-collection/v1", "host": host, "date": date,
        "vendor_config_version": version, "selected_names": selection,
        "selected_ids": selected_ids, "profiles": records,
        "source_roots": ["%APPDATA%/BambuStudio/system/BBL", "%APPDATA%/BambuStudio/user/[redacted]"],
        "remote_comparison": {"host": "Thuis", "status": "unavailable",
            "missing": ["selected presets", "user overrides and calibration", "system inheritance files", "cross-host conflicts"]},
        "calibration": "Saved filament flow/pressure values retained; no physical calibration receipt or separate local calibration file found.",
        "privacy": "Only printing settings, profile filenames and profile hashes; no config copies, .info files, accounts, printer/network files or models.",
        "inheritance": "Explicit file parent -> ordered include templates -> child; application implicit defaults excluded.",
        "fdm_test": {"status": "blocked", "missing_capabilities": ["fdm.slice", "fdm.validate.basic"]},
    }
    (output / "provenance.json").write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    result = validate_catalog_directory(output)
    print(json.dumps({"status": result["status"] if "status" in result else "validated", "counts": {k: len(v) for k, v in catalogs.items()}}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--host", required=True)
    parser.add_argument("--date", required=True)
    args = parser.parse_args()
    collect(args.root, args.output, args.host, args.date)
