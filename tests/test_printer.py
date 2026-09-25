import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_slicer.printer import (
    SDCPPrinter, approved_artifact, check_ready, lan_ip, multipart_chunk,
    start_print, upload_project,
)
from agent_slicer.validation import CTB_ENCRYPTED_MAGIC
from agent_slicer.uvtools_contract import DETECTION_CONTRACT, SCAN_FLAGS


DEVICE = {"address": "192.168.1.114", "board_id": "0e526160f6250100", "brand_id": "brand"}


def ready():
    return {"Status": {"CurrentStatus": [0], "PrintInfo": {"ErrorNumber": 0}},
            "Attributes": {"MachineName": "Mars 5 Ultra", "Resolution": "8520x4320",
                           "Capabilities": ["FILE_TRANSFER", "PRINT_CONTROL"],
                           "SupportFileType": ["CTB", "GOO"],
                           "DevicesStatus": dict.fromkeys(("TempSensorStatusOfUVLED", "LCDStatus",
                               "SgStatus", "ZMotorStatus", "RotateMotorStatus", "RelaseFilmState"), 1)}}


class PrinterTests(unittest.TestCase):
    def fixture(self, root):
        artifact_dir = root / "artifacts"
        artifact_dir.mkdir()
        file = artifact_dir / "example.ctb"
        file.write_bytes(struct.pack("<I", CTB_ENCRYPTED_MAGIC) + b"test" * 300000)
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        build = {"status": "completed", "sliced_output": str(file), "sliced_sha256": digest,
                 "validation": {"status": "passed", "checks": [{"passed": True}]},
                 "production_validation": {"status": "passed", "checks": [{"passed": True}],
                                           "analysis": {"source_sha256": digest,
                                               "detection_contract": DETECTION_CONTRACT,
                                               "scan_flags": SCAN_FLAGS}}}
        (artifact_dir / "build-manifest.json").write_text(json.dumps(build))
        (root / "project.json").write_text(json.dumps({"machine": "elegoo-mars-5-ultra", "process": "resin"}))
        return file, build

    def test_only_private_ipv4_destinations(self):
        for value in ("127.0.0.1", "8.8.8.8", "::1", "printer.local", "192.168.1.1/path", "0.0.0.0"):
            with self.assertRaises(ValueError):
                lan_ip(value)
        self.assertEqual(lan_ip("192.168.1.114"), "192.168.1.114")

    def test_old_incomplete_detector_contract_blocks_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, build = self.fixture(root)
            build["production_validation"]["analysis"].pop("detection_contract")
            (root / "artifacts/build-manifest.json").write_text(json.dumps(build))
            with self.assertRaisesRegex(ValueError, "suction-cup"):
                approved_artifact(root)

    def test_busy_wrong_machine_resolution_format_and_fault_block(self):
        for key, value in (("MachineName", "Mars 3 Pro"), ("Resolution", "4098x2560"),
                           ("SupportFileType", ["GOO"]), ("Capabilities", [])):
            status = ready()
            status["Attributes"][key] = value
            with self.assertRaises(ValueError):
                check_ready(status, ".ctb", "FILE_TRANSFER")
        for flags in ([1], [0, 2], [], None):
            status = ready()
            status["Status"]["CurrentStatus"] = flags
            with self.assertRaises(ValueError):
                check_ready(status, ".ctb", "FILE_TRANSFER")
        status = ready()
        status["Attributes"]["DevicesStatus"]["LCDStatus"] = 0
        with self.assertRaises(ValueError):
            check_ready(status, ".ctb", "PRINT_CONTROL")

    def test_only_completed_unchanged_analyzed_bytes_are_approved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file, build = self.fixture(root)
            self.assertEqual(approved_artifact(root)["path"], str(file))
            file.write_bytes(file.read_bytes() + b"changed")
            with self.assertRaisesRegex(ValueError, "changed"):
                approved_artifact(root)
            build["status"] = "rejected"
            (root / "artifacts/build-manifest.json").write_text(json.dumps(build))
            with self.assertRaisesRegex(ValueError, "not completed"):
                approved_artifact(root)

    def test_multipart_uses_binary_and_rejects_header_injection(self):
        payload = bytes(range(256))
        body, boundary = multipart_chunk("safe.ctb", payload, {"Offset": "0", "Check": "1"})
        self.assertIn(payload, body)
        self.assertIn(b'name="File"; filename="safe.ctb"', body)
        self.assertTrue(body.endswith(("--" + boundary + "--\r\n").encode()))
        for name in ("../x.ctb", 'x"\r\nBad: yes.ctb', "folder/x.ctb"):
            with self.assertRaises(ValueError):
                multipart_chunk(name, payload, {})

    def test_upload_is_chunked_verified_and_never_starts_print(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            printer = MagicMock()
            printer.status.return_value = ready()
            captured = []
            def post(ip, body, boundary):
                captured.append(body)
            def listing():
                if not captured:
                    return []
                import re
                name = re.search(rb'filename="([^"]+)"', captured[0]).group(1).decode()
                return [{"name": "/local//" + name, "type": 1}]  # Mars firmware omits size.
            printer.files.side_effect = listing
            with patch("agent_slicer.printer.connect_device", return_value=DEVICE), \
                 patch("agent_slicer.printer.SDCPPrinter") as cls, \
                 patch("agent_slicer.printer._post_chunk", side_effect=post):
                cls.return_value.__enter__.return_value = printer
                result = upload_project(root, DEVICE["address"])
            self.assertEqual(result["status"], "uploaded")
            self.assertFalse(result["print_started"])
            self.assertFalse(result["remote_size_verified"])
            self.assertEqual(len(captured), 2)
            self.assertIn(b"1048576", captured[1])
            self.assertTrue(Path(result["receipt"]).is_file())
            printer.request.assert_not_called()

    def test_failed_gate_has_no_network_side_effect(self):
        with patch("agent_slicer.printer.approved_artifact", side_effect=ValueError("rejected")), \
             patch("agent_slicer.printer.connect_device") as connect:
            with self.assertRaises(ValueError):
                upload_project("draft", DEVICE["address"])
            connect.assert_not_called()

    def test_print_requires_explicit_confirmation_before_reading_receipt(self):
        with self.assertRaisesRegex(ValueError, "confirm-start"):
            start_print("nonexistent")

    def test_print_does_not_repeat_an_uncertain_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "upload.json"
            artifact = {"project": tmp, "size_bytes": 42}
            path.write_text(json.dumps({"schema": "agent-slicer-upload/v1", "status": "uploaded",
                "artifact": artifact, "device": DEVICE, "filename": "test.ctb"}))
            printer = MagicMock()
            printer.status.return_value = ready()
            printer.files.return_value = [{"name": "/local/test.ctb", "usedSize": 42}]
            printer.request.side_effect = RuntimeError("connection lost after send")
            with patch("agent_slicer.printer.approved_artifact", return_value=artifact), \
                 patch("agent_slicer.printer.connect_device", return_value=DEVICE), \
                 patch("agent_slicer.printer.SDCPPrinter") as cls:
                cls.return_value.__enter__.return_value = printer
                with self.assertRaises(RuntimeError):
                    start_print(path, confirmed=True)
                with self.assertRaises(FileExistsError):
                    start_print(path, confirmed=True)
            self.assertEqual(printer.request.call_count, 1)

    def test_identity_change_and_error_frames_fail_closed(self):
        for message in (
            {"MainboardID": "wrong", "Topic": "sdcp/status/wrong", "Status": {}},
            {"MainboardID": DEVICE["board_id"], "Topic": "sdcp/error/" + DEVICE["board_id"],
             "Data": {"Data": {"ErrorCode": 1}}},
        ):
            printer = SDCPPrinter(DEVICE)
            printer.ws = MagicMock()
            printer.ws.recv.return_value = json.dumps(message)
            with self.assertRaises(RuntimeError):
                printer._receive(1e20)
