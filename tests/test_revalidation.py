import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_slicer.models import BuildPlan
from agent_slicer.project import sha256_file
from agent_slicer.revalidation import revalidate_project


class RevalidationTests(unittest.TestCase):
    def test_prepared_placement_uses_verified_geometry_not_original_dimensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan, source, manifest = self.fixture(root)
            build = json.loads(manifest.read_text())
            build["geometry_preparation"] = [{"model_id": "prepared"}]
            manifest.write_text(json.dumps(build))
            saved = json.loads((root / "artifacts/plan.json").read_text())
            saved["placements"] = [{"source": "deleted-transaction/model.stl", "scale": 1}]
            (root / "artifacts/plan.json").write_text(json.dumps(saved))
            plan.placements = [{"source": "original.stl", "scale": 10}]
            with patch("agent_slicer.revalidation._prepared_placement_evidence",
                       return_value=([{"source": "committed/model.stl", "scale": 1}], {})):
                self.assertEqual(self.run_fixture(root, plan, source)["status"], "completed")

    def test_changed_prepared_placement_cannot_be_revalidated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan, source, manifest = self.fixture(root)
            build = json.loads(manifest.read_text())
            build["geometry_preparation"] = [{"model_id": "prepared"}]
            manifest.write_text(json.dumps(build))
            with patch("agent_slicer.revalidation._prepared_placement_evidence",
                       return_value=([{"scale": 2}], {})):
                with self.assertRaisesRegex(ValueError, "Prepared placement changed"):
                    self.run_fixture(root, plan, source)

    def fixture(self, root):
        artifacts = root / "artifacts"
        artifacts.mkdir()
        (root / "project.json").write_text("{}")
        source, plate = artifacts / "test.ctb", artifacts / "plate.stl"
        source.write_bytes(b"unchanged slice")
        plate.write_bytes(b"unchanged geometry")
        plan = BuildPlan(str(root), {}, [], [], [], [], [], [], [])
        (artifacts / "plan.json").write_text(json.dumps(plan.to_dict()))
        (artifacts / "resolved-config.json").write_text("{}")
        build = {"status": "rejected", "sliced_output": str(source),
                 "sliced_sha256": sha256_file(source), "plate": str(plate),
                 "plate_sha256": sha256_file(plate)}
        manifest = artifacts / "build-manifest.json"
        manifest.write_text(json.dumps(build))
        return plan, source, manifest

    def run_fixture(self, root, plan, source, status="passed", mutate=False):
        analysis = {"source_sha256": sha256_file(source)}
        def analyze(*args, **kwargs):
            if mutate:
                source.write_bytes(b"changed")
            return analysis
        with patch("agent_slicer.revalidation.compile_plan", return_value=(root, plan)), \
             patch("agent_slicer.revalidation.validate_sliced_output", return_value={"status":"passed"}), \
             patch("agent_slicer.revalidation.analyze_sliced_file", side_effect=analyze), \
             patch("agent_slicer.revalidation.evaluate_production_analysis", return_value={"status":status,"analysis":analysis}), \
             patch("agent_slicer.revalidation._write_markdown_report"):
            return revalidate_project.__wrapped__(root)

    def test_rechecks_unchanged_file_and_keeps_old_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan, source, manifest = self.fixture(root)
            before = manifest.read_bytes()
            result = self.run_fixture(root, plan, source)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(source.read_bytes(), b"unchanged slice")
            self.assertEqual(Path(result["revalidation"]["previous_build_report"]).read_bytes(), before)

    def test_failed_recheck_stays_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan, source, manifest = self.fixture(root)
            self.assertEqual(self.run_fixture(root, plan, source, "rejected")["status"], "rejected")

    def test_changed_file_during_scan_cannot_be_approved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan, source, manifest = self.fixture(root)
            before = manifest.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "changed during"):
                self.run_fixture(root, plan, source, mutate=True)
            self.assertEqual(manifest.read_bytes(), before)

    def test_changed_geometry_plan_requires_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan, source, manifest = self.fixture(root)
            plan.placements.append({"scale": 2})
            with self.assertRaisesRegex(ValueError, "placement changed"):
                self.run_fixture(root, plan, source)
