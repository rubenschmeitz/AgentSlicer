from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from agent_slicer.inspection_gui import (
    _binary_stl_preview_info,
    _cached_project_snapshot,
    _safe_project_path,
    project_snapshot,
)
from agent_slicer.project import init_project

from tests.helpers import write_cube


class InspectionGuiTests(unittest.TestCase):
    def _project(self, temporary: str) -> Path:
        root = init_project(
            Path(temporary) / "viewer-project",
            name="Read-only viewer",
            process="resin",
            machine="test-small",
            material="standard-test",
            purpose="test-smoke",
        )
        source = root / "sources" / "cube.stl"
        write_cube(source, size=4.0)
        manifest = json.loads((root / "project.json").read_text(encoding="utf-8"))
        manifest["models"] = [
            {
                "id": "cube",
                "source": "sources/cube.stl",
                "sha256": "0" * 64,
                "target_height_mm": 10.0,
                "quantity": 1,
            }
        ]
        (root / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
        return root

    def test_snapshot_projects_saved_evidence_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._project(temporary)
            (root / "artifacts" / "validation.json").write_text(
                json.dumps(
                    {
                        "status": "passed",
                        "checks": [{"id": "file.nonempty", "passed": True}],
                        "limitations": ["Saved evidence only."],
                    }
                ),
                encoding="utf-8",
            )
            before = {path: path.stat().st_mtime_ns for path in root.rglob("*")}
            value = project_snapshot(root)
            after = {path: path.stat().st_mtime_ns for path in root.rglob("*")}

            self.assertTrue(value["read_only"])
            self.assertEqual(value["project"]["name"], "Read-only viewer")
            self.assertEqual(value["validation"]["basic_status"], "passed")
            self.assertEqual(value["models"][0]["asset_url"], "/asset/sources/cube.stl")
            self.assertTrue(
                any(item["path"] == "sources/cube.stl" for item in value["viewer_assets"])
            )
            source_asset = next(
                item for item in value["viewer_assets"] if item["path"] == "sources/cube.stl"
            )
            self.assertEqual(source_asset["triangle_count"], 12)
            self.assertEqual(source_asset["preview_url"], "/preview-stl/sources/cube.stl")
            self.assertEqual(before, after)

    def test_safe_project_path_rejects_parent_and_absolute_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaisesRegex(ValueError, "escapes"):
                _safe_project_path(root, "../secret.ctb")
            with self.assertRaisesRegex(ValueError, "escapes"):
                _safe_project_path(root, "/etc/passwd")

    def test_versioned_audit_is_presented_without_hiding_pipeline_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._project(temporary)
            (root / "artifacts" / "build-manifest.json").write_text(
                json.dumps({"status": "rejected"}), encoding="utf-8"
            )
            audit_dir = root / "artifacts" / "audited-v2" / "abc123"
            audit_dir.mkdir(parents=True)
            (audit_dir / "final.ctb").write_bytes(b"ctb")
            (audit_dir / "audit-manifest.json").write_text(
                json.dumps(
                    {
                        "schema": "agent-slicer-audited-delivery/v1",
                        "status": "passed-software-production-validation",
                        "created_at": "2026-08-31T10:00:00+02:00",
                        "final_ctb": {
                            "filename": "final.ctb",
                            "sha256": "a" * 64,
                            "bytes": 3,
                        },
                        "validation": {
                            "basic_status": "passed",
                            "production_status": "passed",
                            "after_actionable_issues": {"islands": 0},
                        },
                        "supports": {"requested": 12, "routed": 12, "unrouted": 0},
                        "limitations": ["No physical print proof."],
                    }
                ),
                encoding="utf-8",
            )

            value = project_snapshot(root)
            self.assertEqual(value["project"]["pipeline_status"], "rejected")
            self.assertEqual(value["project"]["status"], "passed")
            self.assertEqual(value["validation"]["source"], "audited delivery")
            self.assertEqual(value["validation"]["issue_counts"], {"islands": 0})
            self.assertEqual(value["current_audit"]["supports"]["routed"], 12)

    def test_binary_stl_preview_is_bounded_without_modifying_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cube.stl"
            write_cube(path, size=4.0)
            before = path.read_bytes()
            self.assertEqual(_binary_stl_preview_info(path), (12, 1, 12))
            self.assertEqual(path.read_bytes(), before)

    def test_snapshot_cache_coalesces_repeated_browser_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._project(temporary)
            with patch(
                "agent_slicer.inspection_gui.project_snapshot",
                wraps=project_snapshot,
            ) as snapshot:
                first = _cached_project_snapshot(root)
                second = _cached_project_snapshot(root)
            self.assertIs(first, second)
            self.assertEqual(snapshot.call_count, 1)


if __name__ == "__main__":
    unittest.main()
