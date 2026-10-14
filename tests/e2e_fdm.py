"""Real OrcaSlicer/Blender verification; writes evidence, never starts a print.

Run: python -m tests.e2e_fdm --slicer /path/to/OrcaSlicer --output /new/evidence
"""
from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

from agent_slicer.pipeline import build_project, compile_plan
from agent_slicer.project import add_models, init_project, load_project, save_project, sha256_file
from agent_slicer.validation import validate_sliced_output
from agent_slicer.revalidation import revalidate_project
from agent_slicer.fdm_validation import validate_fdm_output
from tests.helpers import write_cube


def main() -> None:
    import trimesh

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slicer", required=True)
    parser.add_argument("--profile-root")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    cube = root / "cube.stl"
    write_cube(cube)
    jack = root / "jack.stl"
    mesh = trimesh.boolean.union([trimesh.creation.box(extents=extent)
        for extent in ((24, 6, 6), (6, 24, 6), (6, 6, 24))], engine="manifold")
    mesh.apply_translation(-mesh.bounds[0])
    mesh.export(jack)
    original_hashes = {str(path): sha256_file(path) for path in (cube, jack)}
    cases = [
        ("pla-standard-copies", "bambu-pla-basic-p2s", "p2s-0.20-standard", "fdm-general", cube, "gcode.3mf"),
        ("pla-detail", "bambu-pla-basic-p2s", "p2s-0.08-high-quality", "fdm-general", cube, "gcode.3mf"),
        ("petg-quality", "bambu-petg-hf-p2s", "p2s-0.12-high-quality", "fdm-general", cube, "gcode.3mf"),
        ("abs-standard", "bambu-abs-p2s", "p2s-0.24-standard", "fdm-general", cube, "3mf"),
        ("pla-tree-supports", "bambu-pla-basic-p2s", "p2s-0.20-standard", "fdm-display-supported", jack, "gcode.3mf"),
    ]
    evidence = []
    for name, material, quality, purpose, source, output_format in cases:
        project = init_project(root / name, name=name, process="fdm", machine="bambu-lab-p2s-0.4",
                               material=material, purpose=purpose, quality=quality, output_format=output_format)
        add_models(project, [source], target_height_mm=8 if name.endswith("copies") else None)
        if name.endswith("copies"):
            _, manifest = load_project(project)
            manifest.models[0].quantity = 2
            save_project(project, manifest)
        _, plan = compile_plan(project, fdm_slicer_path=args.slicer, fdm_profile_root=args.profile_root)
        assert not plan.blockers, plan.blockers
        result = build_project(project, fdm_slicer_path=args.slicer, fdm_profile_root=args.profile_root)
        assert result["status"] == "completed"
        assert result["validation"]["status"] == "passed"
        assert not any(capability.startswith("resin.") for capability in plan.required_capabilities)
        output = Path(result["sliced_output"])
        assert validate_sliced_output(output)["status"] == "passed"
        support_count = 0
        with zipfile.ZipFile(output) as archive:
            code = archive.read("Metadata/plate_1.gcode")
            support_count = code.count(b"; FEATURE: Support")
            if purpose == "fdm-display-supported":
                assert support_count > 0, "real support fixture generated no support toolpaths"
            raw = project / "artifacts" / "verification.gcode"
            raw.write_bytes(code)
            assert validate_sliced_output(raw)["status"] == "passed"
            backend_profiles = {kind: json.loads(Path(filename).read_text()) for kind, filename in result["slice"]["profiles"].items()}
            assert validate_fdm_output(raw, resolved=plan.resolved_config, expected_profiles=backend_profiles)["status"] == "passed"
        case = {"case": name, "status": result["status"], "backend": result["slice"]["version"],
                "sliced_output": str(output), "sha256": sha256_file(output), "support_features": support_count,
                "statistics": result["validation"]["gcode_statistics"],
                "slicer_warnings": result["validation"].get("slicer_warnings", [])}
        if name.endswith("copies"):
            rebuilt = build_project(project, fdm_slicer_path=args.slicer, fdm_profile_root=args.profile_root, reuse_prepared=True)
            assert rebuilt["geometry_preparation"][0]["cache_reused"]
            case["preparation_cache_verified"] = True
            revalidated = revalidate_project(project, fdm_slicer_path=args.slicer, fdm_profile_root=args.profile_root)
            assert revalidated["status"] == "completed"
            assert "production_validation" not in revalidated
            case["revalidation_verified"] = True
        evidence.append(case)
        (root / "verification.json").write_text(json.dumps({"status": "running", "cases": evidence}, indent=2) + "\n")
        print(f"{name}: passed, {case['statistics']['layers']} layers, {support_count} support features", flush=True)
    assert all(sha256_file(Path(path)) == digest for path, digest in original_hashes.items())
    (root / "verification.json").write_text(json.dumps({"status": "passed", "sources": original_hashes, "cases": evidence}, indent=2) + "\n")


if __name__ == "__main__":
    main()
