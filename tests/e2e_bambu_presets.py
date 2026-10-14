"""Verify imported Bambu presets with a real CLI; never connect to a printer."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from agent_slicer.pipeline import build_project, compile_plan
from agent_slicer.project import add_models, init_project
from agent_slicer.revalidation import revalidate_project
from agent_slicer.fdm_profiles import resolve_fdm_profiles
from agent_slicer.fdm_validation import validate_fdm_output
from agent_slicer.project import sha256_file
from tests.helpers import write_cube


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slicer", required=True)
    parser.add_argument("--catalog", type=Path, default=Path("presets/bambu-work-pc-20261014"))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--resume", action="store_true", help="recheck verified cases; rebuild failed/incomplete cases")
    args = parser.parse_args()
    catalog = args.catalog.resolve()
    os.environ["AGENT_SLICER_PRESET_PATH"] = str(catalog)
    provenance = json.loads((catalog / "provenance.json").read_text())
    materials = json.loads((catalog / "materials.json").read_text())
    qualities = json.loads((catalog / "qualities.json").read_text())
    selected = provenance["selected_ids"]
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=args.resume)
    cube = root / "cube.stl"
    if not cube.is_file():
        write_cube(cube, 6)
    cases = [(key, selected["machine"], selected["quality"], selected["purpose"])
             for key, value in materials.items() if selected["machine"] in value["compatible_machines"]]
    miniature_id = next(key for key, value in qualities.items() if "MAX DETAIL" in value["display_name"])
    cases.append(("bambu-stock-pla-basic-p2s-0.2-v02.08.02.61", qualities[miniature_id]["compatible_machines"][0],
                  miniature_id, miniature_id + "-source-supports"))
    evidence = {"status": "running", "cases": [], "untested": [value["display_name"] for value in materials.values()
        if selected["machine"] not in value["compatible_machines"] and value["upstream_profile"]["name"] != "Bambu PLA Basic @BBL P2S 0.2 nozzle"]}
    for number, (material, machine, quality, purpose) in enumerate(cases):
        print(f"Starting {materials[material]['display_name']}", flush=True)
        source = cube
        if quality == miniature_id:
            import trimesh
            source = root / "supported-jack.stl"
            mesh = trimesh.boolean.union([trimesh.creation.box(extents=extent)
                for extent in ((9, 3, 3), (3, 9, 3), (3, 3, 9))], engine="manifold")
            mesh.apply_translation(-mesh.bounds[0])
            if not source.is_file():
                mesh.export(source)
        project = root / f"case-{number}"
        if not (project / "project.json").is_file():
            project = init_project(project, name=f"imported-bambu-{number}", process="fdm",
                machine=machine, material=material, quality=quality, purpose=purpose, output_format="gcode.3mf")
            add_models(project, [source])
        _, plan = compile_plan(project, fdm_slicer_path=args.slicer)
        assert not plan.blockers, plan.blockers
        previous = project / "artifacts/build-manifest.json"
        result = json.loads(previous.read_text()) if args.resume and previous.is_file() else None
        if result is None or result["status"] != "completed":
            result = build_project(project, fdm_slicer_path=args.slicer)
        else:
            projected = resolve_fdm_profiles(plan.resolved_config, args.slicer)
            assert sha256_file(Path(result["sliced_output"])) == result["sliced_sha256"]
            verified = validate_fdm_output(result["sliced_output"], resolved=plan.resolved_config,
                expected_profiles=projected["profiles"], native_keys={k: v["validated_keys"] for k, v in projected["native_evidence"].items()})
            assert verified["status"] == "passed", [check for check in verified["checks"] if not check["passed"]]
        assert result["status"] == "completed"
        assert result["validation"]["status"] == "passed"
        assert any(check["id"].startswith("fdm.imported-setting.") for check in result["validation"]["checks"])
        case = {"material_id": material, "machine_id": machine, "quality_id": quality, "status": "passed",
            "backend": result["slice"]["version"], "sliced_sha256": result["sliced_sha256"],
            "checks": len(result["validation"]["checks"]), "statistics": result["validation"]["gcode_statistics"],
            "inactive_calibration": result["slice"]["native_profile_evidence"].get("filament", {}).get("inactive_calibration", {})}
        if number == 0:
            assert revalidate_project(project, fdm_slicer_path=args.slicer)["status"] == "completed"
            case["revalidation_verified"] = True
        if quality == miniature_id:
            import zipfile
            with zipfile.ZipFile(result["sliced_output"]) as archive:
                code = archive.read("Metadata/plate_1.gcode")
                case["support_features"] = code.count(b"; FEATURE: Support")
                assert case["support_features"] > 0
        evidence["cases"].append(case)
        (root / "verification.json").write_text(json.dumps(evidence, indent=2) + "\n")
        print(f"Passed {materials[material]['display_name']}: {case['checks']} checks", flush=True)
    evidence["status"] = "passed"
    (root / "verification.json").write_text(json.dumps(evidence, indent=2) + "\n")


if __name__ == "__main__":
    main()
