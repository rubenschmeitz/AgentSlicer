from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from agent_slicer.pipeline import build_project
from agent_slicer.project import add_models, init_project
from tests.helpers import write_cube


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", type=Path)
    parser.add_argument("--format", choices=["ctb", "goo"], default="ctb")
    args = parser.parse_args()
    if args.keep:
        root = args.keep.resolve()
        root.mkdir(parents=True, exist_ok=True)
        run(root, args.format)
        return 0
    with tempfile.TemporaryDirectory() as temporary:
        run(Path(temporary), args.format)
    return 0


def run(root: Path, output_format: str) -> None:
    source = root / "cube.stl"
    write_cube(source, 6.0)
    project = init_project(
        root / "smoke-project",
        name="real-e2e-smoke",
        process="resin",
        machine="test-small",
        material="standard-test",
        purpose="test-smoke",
        output_format=output_format,
    )
    add_models(project, [source])
    result = build_project(project)
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["validation"]["status"] != "passed":
        raise SystemExit("validation did not pass")


if __name__ == "__main__":
    raise SystemExit(main())
