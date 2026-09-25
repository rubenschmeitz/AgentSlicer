from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from agent_slicer.resources import (
    heavy_job_guard,
    native_subprocess_environment,
    work_directory,
    _lock_nonblocking,
)


class ResourceGuardTests(unittest.TestCase):
    def test_windows_contender_does_not_read_locked_byte(self):
        handle = MagicMock()
        handle.read.side_effect = PermissionError("locked byte cannot be read")
        runtime = MagicMock()
        runtime.locking.side_effect = PermissionError("already locked")
        with patch("agent_slicer.resources.fcntl", None), \
             patch("agent_slicer.resources.msvcrt", runtime, create=True), \
             patch("agent_slicer.resources.os.fstat", return_value=type("Stat", (), {"st_size":1})()):
            with self.assertRaises(BlockingIOError):
                _lock_nonblocking(handle)
        handle.read.assert_not_called()
        handle.write.assert_not_called()
        runtime.locking.assert_called_once()

    def test_heavy_job_guard_is_reentrant_and_clears_owner_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "heavy.lock"
            with patch.dict(os.environ, {"AGENT_SLICER_RESOURCE_LOCK": str(lock)}):
                with heavy_job_guard("outer"):
                    self.assertIn('"label": "outer"', lock.read_text())
                    with heavy_job_guard("inner"):
                        self.assertIn('"label": "outer"', lock.read_text())
                self.assertEqual(lock.read_text(), "")

    def test_wsl_subprocess_environment_uses_native_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {
                "AGENT_SLICER_WORK_ROOT": temporary,
                "TEMP": "/mnt/c/Windows/Temp",
                "TMP": "/mnt/c/Windows/Temp",
            },
            clear=True,
        ), patch("agent_slicer.resources._is_wsl", return_value=True):
            environment = native_subprocess_environment()

        self.assertEqual(environment["TMPDIR"], str(Path(temporary) / "tmp"))
        self.assertNotIn("TEMP", environment)
        self.assertNotIn("TMP", environment)
        self.assertTrue(environment["RESIN_SLICER_WORK_ROOT"].startswith("/home/"))

    def test_work_directory_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {"AGENT_SLICER_WORK_ROOT": temporary},
            clear=True,
        ):
            self.assertEqual(work_directory("prepare"), Path(temporary) / "prepare")
            with self.assertRaises(ValueError):
                work_directory("../escape")


if __name__ == "__main__":
    unittest.main()
