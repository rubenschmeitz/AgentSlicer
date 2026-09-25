from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_slicer.project import load_project


class ProjectValidationTests(unittest.TestCase):
    def _manifest(self) -> dict[str, object]:
        return {
            "schema": "agent-slicer-project/v1",
            "name": "test",
            "process": "resin",
            "machine": "test-small",
            "material": "standard-test",
            "purpose": "test-smoke",
            "models": [],
        }

    def test_rejects_unknown_project_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._manifest()
            manifest["surprise"] = True
            (root / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown project fields"):
                load_project(root)

    def test_rejects_source_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._manifest()
            manifest["models"] = [
                {
                    "id": "escape",
                    "source": "../outside.stl",
                    "sha256": "0" * 64,
                }
            ]
            (root / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "escapes project directory"):
                load_project(root)


if __name__ == "__main__":
    unittest.main()
