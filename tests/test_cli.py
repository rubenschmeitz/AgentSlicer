from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def test_cli_import_does_not_eagerly_load_geometry_stack(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; import agent_slicer.cli; "
                    "assert 'scipy' not in sys.modules; "
                    "assert 'agent_slicer.pipeline' not in sys.modules; "
                    "assert 'agent_slicer.uvtools_adapter' not in sys.modules"
                ),
            ],
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_profiles_recommend_resolves_p2s_defaults(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_slicer.cli",
                "profiles",
                "recommend",
                "--machine",
                "bambu-lab-p2s-0.4",
                "--material",
                "bambu-pla-basic-p2s",
                "--purpose",
                "fdm-general",
                "--quality",
                "p2s-0.20-standard",
            ],
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertEqual(value["selection"]["process"], "fdm")
        self.assertEqual(value["selection"]["output_format"], "gcode.3mf")
        self.assertEqual(value["settings"]["material"]["nozzle_temperature_c"], 220)

    def test_project_init_derives_p2s_process_and_output_format(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "p2s"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_slicer.cli",
                    "project",
                    "init",
                    "p2s-defaults",
                    "--directory",
                    str(project),
                    "--machine",
                    "bambu-lab-p2s-0.4",
                    "--material",
                    "bambu-pla-basic-p2s",
                    "--purpose",
                    "fdm-general",
                    "--quality",
                    "p2s-0.20-standard",
                ],
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            manifest = json.loads((project / "project.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["process"], "fdm")
            self.assertEqual(manifest["output_format"], "gcode.3mf")


if __name__ == "__main__":
    unittest.main()
