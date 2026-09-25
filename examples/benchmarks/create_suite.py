"""Create a pinned, matched SLA benchmark from an existing STL (no downloads).

All paths refer to isolated local tools. Engine commands/flags were verified
against Resin-Slicer 0.1.0, PrusaSlicer 2.8.1 and SuperSlicer 2.5.59.13.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import trimesh

from agent_slicer.benchmark import sha256


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--resin", type=Path, required=True)
    parser.add_argument("--prusa", type=Path, required=True)
    parser.add_argument("--super", type=Path, required=True)
    parser.add_argument("--prusa-libraries", required=True)
    args = parser.parse_args()
    root = args.directory.resolve()
    root.mkdir(parents=True, exist_ok=False)
    mesh = trimesh.load_mesh(args.source, process=False)
    mesh.apply_scale(10.0 / mesh.extents[2])
    mesh.apply_translation(-mesh.bounds[0])
    model = root / "miniature-10mm.stl"
    mesh.export(model)
    cube = trimesh.creation.box(extents=(6.0, 6.0, 6.0))
    cube.apply_translation(-cube.bounds[0])
    cube_path = root / "cube-6mm.stl"
    cube.export(cube_path)
    cases = []
    for fixture, path, supported in (("cube", cube_path, False), ("miniature", model, False),
                                      ("supported-miniature", model, True)):
        for name, executable in (("resin", args.resin), ("prusa", args.prusa), ("super", args.super)):
            version = "--version" if name == "resin" else "--help"
            if name == "resin":
                argv = [str(executable.resolve()), "{input}", "--output", "{output}",
                        "--profile", "elegoo-mars-3-pro", "--layer-height", "0.05", "--workers", "1",
                        "--report-json", "{run}/slice-report.json"]
                argv.extend(["--primary-supports", "--model-lift", "5"] if supported else ["--no-supports"])
            else:
                argv = [str(executable.resolve()), "--export-sla", "--output", "{output}",
                        "--display-width", "143.43", "--display-height", "89.6",
                        "--display-pixels-x", "4098", "--display-pixels-y", "2560",
                        "--display-orientation", "landscape", "--no-display-mirror-x", "--no-display-mirror-y",
                        "--bed-shape", "0x0,143.43x0,143.43x89.6,0x89.6", "--max-print-height", "175",
                        "--center", "71.715,44.8", "--layer-height", "0.05", "--initial-layer-height", "0.05",
                        "--exposure-time", "2.5", "--initial-exposure-time", "30",
                        "--gamma-correction", "0", "--absolute-correction", "0",
                        "--elefant-foot-compensation" if name == "prusa" else "--first-layer-size-compensation",
                        "0", "--no-pad-enable", "--no-hollowing-enable",
                        "--supports-enable" if supported else "--no-supports-enable",
                        "--support-object-elevation", "5" if supported else "0",
                        "--datadir", "{run}/profile", "{input}"]
            cases.append({"id": f"{fixture}-{name}", "input": str(path), "input_sha256": sha256(path),
                          "format": "ctb" if name == "resin" else "sl1", "argv": argv,
                          "version_argv": [str(executable.resolve()), version],
                          "implementation_files": [str(p) for p in sorted((executable.resolve().parents[2] / "resin_slicer").rglob("*.py"))] if name == "resin" else [],
                          "timeout_seconds": 600,
                          "env": {"LD_LIBRARY_PATH": args.prusa_libraries} if name == "prusa" else {}})
    manifest = {"schema": "agent-slicer-benchmark/v1", "cases": cases,
                "source": {"path": str(args.source.resolve()), "sha256": sha256(args.source)},
                "matched": {"resolution": [4098, 2560], "size_mm": [143.43, 89.6, 175],
                            "layer_height_mm": .05, "antialiasing": "binary",
                            "normalization": "uniform scale to 10mm height, translated to positive origin; original untouched"},
                "limitations": ["Reference engines are related Prusa/Slic3r implementations, not independent families.",
                                "Supported case uses engine-default support shapes/density; not output-equivalent.",
                                "Reference TBB may ignore OMP limits; use common CPU affinity for measured runs.",
                                "CTB versus SL1 encoding and integrated validation have different costs."]}
    (root / "suite.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(root / "suite.json")


if __name__ == "__main__":
    main()
