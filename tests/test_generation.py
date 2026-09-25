from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import trimesh
from PIL import Image

from agent_slicer.generation import (
    PARAMETRIC_SCHEMA,
    _validate_parametric_spec,
    add_reference_images,
    approve_generation,
    generation_status,
    init_generation,
    load_generation,
    refresh_generation,
    select_candidate,
    submit_model_generation,
)
from agent_slicer.generation_providers import ProviderTask


class GenerationTests(unittest.TestCase):
    def test_concept_requires_explicit_target_height(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "target height"):
                init_generation(
                    Path(temporary) / "concept",
                    name="Owl",
                    kind="concept",
                    description="A carved owl",
                )

    def test_reference_and_credit_approval_gates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = init_generation(
                Path(temporary) / "concept",
                name="Owl",
                kind="concept",
                description="A carved owl",
                target_height_mm=80,
            )
            image = Path(temporary) / "owl.png"
            Image.new("RGB", (8, 8), "white").save(image)
            add_reference_images(root, [image])
            approve_generation(root, "brief")
            approve_generation(root, "references")

            with self.assertRaisesRegex(RuntimeError, "confirm-credit-use"):
                submit_model_generation(root)

    def test_provider_candidate_is_downloaded_scaled_and_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = init_generation(
                Path(temporary) / "concept",
                name="Owl",
                kind="concept",
                description="A carved owl",
                target_height_mm=80,
            )
            approve_generation(root, "brief")
            backend = SimpleNamespace()
            backend.submit_model = lambda *args, **kwargs: ProviderTask(
                "meshy", "text-to-3d", "task-1", "pending", 0, None, {}
            )
            backend.status = lambda *args, **kwargs: ProviderTask(
                "meshy",
                "text-to-3d",
                "task-1",
                "succeeded",
                100,
                20.0,
                {"model.stl": "https://example.test/model.stl"},
            )

            def fake_download(url: str, destination: Path) -> int:
                del url
                mesh = trimesh.creation.box(extents=(10, 10, 20))
                mesh.export(destination, file_type="stl")
                return destination.stat().st_size

            with patch("agent_slicer.generation.provider", return_value=backend):
                submit_model_generation(root, confirm_credit_use=True)
                with patch("agent_slicer.generation.download", side_effect=fake_download):
                    refreshed = refresh_generation(root)
            candidates = [item for item in refreshed["artifacts"] if item["kind"] == "candidate"]
            self.assertEqual(len(candidates), 1)
            selected = select_candidate(root, candidates[0]["id"])
            inspection = trimesh.load_mesh(root / selected["path"])
            self.assertAlmostEqual(float(inspection.extents[2]), 80.0, places=3)
            self.assertEqual(generation_status(root)["status"], "awaiting-selection-approval")

    def test_parametric_spec_is_strict_and_unit_explicit(self) -> None:
        valid = {
            "schema": PARAMETRIC_SCHEMA,
            "name": "plate",
            "description": "mounting plate",
            "units": "mm",
            "parameters": {},
            "operations": [
                {"operation": "add", "shape": "box", "size_mm": [40, 20, 4]},
                {
                    "operation": "cut",
                    "shape": "cylinder",
                    "diameter_mm": 4.2,
                    "height_mm": 8,
                    "axis": "z",
                    "center_mm": [10, 0, 0],
                },
            ],
        }
        _validate_parametric_spec(valid)
        invalid = json.loads(json.dumps(valid))
        invalid["units"] = "inch"
        with self.assertRaisesRegex(ValueError, "explicitly 'mm'"):
            _validate_parametric_spec(invalid)


if __name__ == "__main__":
    unittest.main()
