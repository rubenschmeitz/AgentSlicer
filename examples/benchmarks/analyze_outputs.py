"""Stream CTB/SL1 rasters through the same structural auditor after timing.

Requires the separately installed Resin-Slicer package on PYTHONPATH. This is
a diagnostic, not resin-trap analysis or a production printability approval.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import struct
import zipfile

import numpy as np
from PIL import Image

from agent_slicer.benchmark import sha256
from agent_slicer.resources import heavy_job
from resin_slicer.formats.readers import inspect_sliced_file
from resin_slicer.raster import LayerRaster
from resin_slicer.validation import LayerStackValidator


def layers(path):
    if path.suffix == ".ctb":
        for layer in inspect_sliced_file(path).iter_layers():
            yield layer.raster
    else:
        with zipfile.ZipFile(path) as archive:
            names = sorted(n for n in archive.namelist() if re.fullmatch(r"slice\d+\.png", n))
            if not names:
                raise ValueError(f"no slice PNGs: {path}")
            for name in names:
                with archive.open(name) as stream, Image.open(stream) as image:
                    if image.size != (4098, 2560):
                        raise ValueError(f"unexpected layer dimensions: {image.size}")
                    image = image.convert("L")
                    bounds = image.getbbox()
                    raster = LayerRaster(*image.size)
                    if bounds:
                        data = np.asarray(image.crop(bounds))
                        if np.any((data != 0) & (data != 255)):
                            raise ValueError("reference output unexpectedly has antialiasing")
                        for y, row in enumerate(data):
                            edges = np.flatnonzero(np.diff(np.pad(row != 0, (1, 1))))
                            for x0, x1 in edges.reshape(-1, 2):
                                raster.set_span(y + bounds[1], int(x0) + bounds[0], int(x1) + bounds[0] - 1)
                    yield raster


@heavy_job
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("analysis output already exists")
    results = json.loads(args.results.read_text())
    output = {"schema": "agent-slicer-benchmark-analysis/v1", "results_sha256": sha256(args.results),
              "criteria": "same zero-radius layer connectivity test; not production policy or resin-trap validation",
              "cases": []}
    for case in results["cases"]:
        entry = {"id": case["id"], "runs": []}
        output["cases"].append(entry)
        # Analyze every repetition, not just one favorable result.
        for run in case["runs"]:
            path = Path(run["output"])
            if sha256(path) != run["output_sha256"]:
                raise ValueError(f"checksum mismatch: {path}")
            validator = LayerStackValidator(mode="cross-engine-zero-radius-diagnostic-v1")
            digest = hashlib.sha256()
            count = 0
            for index, raster in enumerate(layers(path)):
                if (raster.width, raster.height) != (4098, 2560):
                    raise ValueError("unexpected resolution")
                validator.observe(index, raster)
                count += raster.count_on()
                digest.update(struct.pack("<I", index))
                for y, spans in raster.nonzero_rows():
                    for x0, x1 in spans:
                        digest.update(struct.pack("<III", y, x0, x1))
            entry["runs"].append({"output_sha256": run["output_sha256"],
                                  "canonical_rasters_sha256": digest.hexdigest(), "on_voxels": count,
                                  "material_ml": count * (143.43 / 4098) * (89.6 / 2560) * .05 / 1000,
                                  "structure": asdict(validator.result())})
        entry["repeated_rasters_identical"] = len({r["canonical_rasters_sha256"] for r in entry["runs"]}) == 1
        args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")
        print(case["id"], entry["runs"][0]["material_ml"], flush=True)


if __name__ == "__main__":
    main()
