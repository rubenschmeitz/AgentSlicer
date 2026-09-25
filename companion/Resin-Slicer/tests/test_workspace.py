from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from resin_slicer.workspace import work_root, work_subdirectory


class WorkspaceTests(unittest.TestCase):
    def test_wsl_default_ignores_inherited_windows_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {"TEMP": "/mnt/c/Windows/Temp", "TMP": "/mnt/c/Windows/Temp"},
            clear=True,
        ), patch("resin_slicer.workspace._is_wsl", return_value=True), patch(
            "resin_slicer.workspace.Path.home", return_value=Path(temporary)
        ):
            root = work_root()

        self.assertEqual(
            root,
            Path(temporary) / "development-tools" / "resin-slicer-work",
        )

    def test_explicit_work_root_and_safe_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {"RESIN_SLICER_WORK_ROOT": temporary},
            clear=True,
        ):
            self.assertEqual(work_subdirectory("spool"), Path(temporary) / "spool")
            with self.assertRaises(ValueError):
                work_subdirectory("../escape")


if __name__ == "__main__":
    unittest.main()
