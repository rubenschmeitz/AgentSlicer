from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_slicer.blender_adapter import blender_script
from agent_slicer.processes import run_backend
from agent_slicer.resources import native_subprocess_environment


class BackendProcessTests(unittest.TestCase):
    def test_packaged_blender_scripts(self):
        for name in ("hollow.py", "prepare_resin.py"):
            self.assertTrue(blender_script(name).is_file())
            self.assertIn("agent_slicer/data/blender", str(blender_script(name)))
        with self.assertRaises(ValueError):
            blender_script("../../unsafe.py")

    def test_logs_are_bounded_and_full_evidence_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_backend([sys.executable, "-c", "print('x'*200000); raise SystemExit(7)"],
                                 timeout=10, tail_bytes=128, log_directory=directory)
            self.assertEqual(result.returncode, 7)
            self.assertEqual(len(result.stdout), 128)
            self.assertEqual(result.metrics["stdout_bytes"], 200001)
            self.assertEqual((Path(directory) / "stdout.log").stat().st_size, 200001)
            with self.assertRaises(FileExistsError):
                run_backend([sys.executable, "-V"], timeout=10, log_directory=directory)

    @unittest.skipUnless(sys.platform == "linux", "requires Linux process groups")
    def test_timeout_kills_spawned_child_too(self):
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory) / "child.pid"
            code = "import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)"
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                run_backend([sys.executable, "-c", code, str(pidfile)], timeout=1)
            pid = int(pidfile.read_text())
            for _ in range(20):
                status = Path(f"/proc/{pid}/status")
                if not status.exists() or "State:\tZ" in status.read_text():
                    break
                time.sleep(.05)
            else:
                self.fail("backend child remained running after timeout")

    def test_invalid_limits_and_explicit_thread_budget(self):
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                run_backend([sys.executable, "-V"], timeout=timeout)
        with patch.dict(os.environ, {"AGENT_SLICER_THREADS": "2", "OPENBLAS_NUM_THREADS": "99"}):
            self.assertEqual(native_subprocess_environment()["OPENBLAS_NUM_THREADS"], "2")
        with patch.dict(os.environ, {"AGENT_SLICER_THREADS": "0"}):
            with self.assertRaises(ValueError):
                native_subprocess_environment()

    @unittest.skipUnless(sys.platform == "linux", "requires procfs")
    def test_memory_watchdog(self):
        with patch.dict(os.environ, {"AGENT_SLICER_MAX_BACKEND_RSS_MB": "16"}):
            with self.assertRaisesRegex(RuntimeError, "RSS budget"):
                run_backend([sys.executable, "-c", "import time; x=bytearray(64000000); time.sleep(10)"], timeout=5)
