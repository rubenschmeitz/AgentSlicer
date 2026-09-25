"""Alternate old 64-layer versus adaptive index with otherwise identical code.

Run under the Resin-Slicer environment with the normalized benchmark model.
This isolates the index change without mutating either checkout between runs.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time
from unittest.mock import patch
from agent_slicer.resources import heavy_job

from resin_slicer.config import SupportConfig, profile
from resin_slicer.mesh import load_mesh
from resin_slicer.pipeline import SliceJob, slice_to_file
from resin_slicer.slicing import _build_layer_index


@heavy_job
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("model", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    results = []
    for index, mode in enumerate(("block64", "adaptive", "adaptive", "block64", "block64", "adaptive")):
        started = time.perf_counter()
        source = load_mesh(args.model)
        destination = args.output / f"{index}-{mode}.ctb"
        def build(mesh, config, count):
            return _build_layer_index(mesh, config, count, block_size=64 if mode == "block64" else None)
        with patch("resin_slicer.slicing._build_layer_index", build):
            result = slice_to_file(SliceJob(source, profile("elegoo-mars-3-pro"),
                                           SupportConfig(primary_supports_enabled=True)), destination, "ctb", layer_workers=1)
        elapsed = time.perf_counter() - started
        results.append({"mode": mode, "seconds": elapsed, "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                        "support_count": result.support_count, "layers": result.layer_count})
        (args.output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
        print(results[-1], flush=True)


if __name__ == "__main__":
    main()
