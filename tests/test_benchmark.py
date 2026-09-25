from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from agent_slicer.benchmark import run_benchmark, sha256


class BenchmarkTests(unittest.TestCase):
    def test_sequential_receipts_hashes_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "model.stl"
            source.write_bytes(b"fixture")
            manifest = {"schema": "agent-slicer-benchmark/v1", "cases": [{
                "id": "test", "input": str(source), "input_sha256": sha256(source),
                "format": "ctb", "version_argv": [sys.executable, "--version"],
                "argv": [sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[2]).write_bytes(Path(sys.argv[1]).read_bytes())", "{input}", "{output}"]}]}
            definition = root / "manifest.json"
            definition.write_text(json.dumps(manifest))
            result = run_benchmark(definition, root / "results", repeats=2)
            self.assertEqual(result["status"], "measured-not-production-validated")
            self.assertTrue(result["cases"][0]["output_hashes_identical"])
            self.assertEqual(len(result["cases"][0]["runs"]), 2)
            with self.assertRaises(FileExistsError):
                run_benchmark(definition, root / "results", repeats=2)
            source.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                run_benchmark(definition, root / "other", repeats=1)
            self.assertFalse((root / "other").exists())

    def test_invalid_repeat_count_rejected_before_execution(self):
        for count in (0, 21, True, 1.5):
            with self.assertRaises(ValueError):
                run_benchmark("missing", "missing", repeats=count)
