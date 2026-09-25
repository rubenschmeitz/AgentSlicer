"""Bounded LAN SDCP v3 discovery, validated uploads and explicit print start.

Protocol: cbd-tech/SDCP-Smart-Device-Control-Protocol-V3.0.0.
No cloud, deletion, firmware changes or automatic motion commands.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import re
import socket
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .project import sha256_file


def lan_ip(value: str) -> str:
    ip = ipaddress.IPv4Address(value)
    if not any(ip in ipaddress.ip_network(net) for net in
               ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
        raise ValueError("Printer address must be a private LAN IPv4 address")
    return str(ip)


def discover(*, address: str | None = None, timeout: float = 3.0) -> list[dict]:
    if not 0.1 <= timeout <= 15:
        raise ValueError("Discovery timeout must be 0.1 to 15 seconds")
    target = lan_ip(address) if address else "255.255.255.255"
    found = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", 0))
        sock.sendto(b"M99999", (target, 3000))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sock.settimeout(max(0.01, deadline - time.monotonic()))
            try:
                raw, peer = sock.recvfrom(16384)
            except socket.timeout:
                break
            if address and peer[0] != address:
                continue
            try:
                value = json.loads(raw)
                data = value["Data"]
                ip = lan_ip(peer[0])  # Never follow an advertised redirect.
                board = data["MainboardID"]
                if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", board):
                    continue
                if not str(data.get("ProtocolVersion", "")).startswith("V3."):
                    continue
                found[(ip, board)] = {
                    "address": ip, "board_id": board, "brand_id": value["Id"],
                    "name": data.get("Name"), "model": data.get("MachineName"),
                    "firmware": data.get("FirmwareVersion"),
                    "protocol": data["ProtocolVersion"],
                }
            except (ValueError, KeyError, TypeError):
                continue
    return sorted(found.values(), key=lambda item: (item["address"], item["board_id"]))


class SDCPPrinter:
    def __init__(self, device: dict, *, timeout: float = 10.0):
        self.device = device
        self.address = lan_ip(device["address"])
        self.board = device["board_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", self.board):
            raise ValueError("Invalid board identity")
        self.timeout = timeout
        self.ws = None
        self.latest: dict[str, Any] = {}

    def __enter__(self):
        import websocket
        self.ws = websocket.create_connection(
            f"ws://{self.address}:3030/websocket", timeout=self.timeout,
            http_no_proxy=[self.address], suppress_origin=True,
        )
        return self

    def __exit__(self, *_):
        if self.ws is not None:
            self.ws.close()

    def _send(self, command: int, data: dict) -> str:
        request_id = uuid.uuid4().hex
        self.ws.send(json.dumps({
            "Id": self.device["brand_id"],
            "Data": {"Cmd": command, "Data": data, "RequestID": request_id,
                     "MainboardID": self.board, "TimeStamp": int(time.time()), "From": 0},
            "Topic": f"sdcp/request/{self.board}",
        }))
        return request_id

    def _receive(self, deadline: float) -> dict:
        self.ws.settimeout(max(0.01, deadline - time.monotonic()))
        raw = self.ws.recv()
        if raw == "ping":
            self.ws.send("pong")
            return {}
        if raw == "pong":
            return {}
        if not raw or len(raw) > 2_000_000:
            raise RuntimeError("Printer returned an empty or oversized response")
        message = json.loads(raw)
        topic = message.get("Topic", "")
        board = message.get("MainboardID") or message.get("Data", {}).get("MainboardID")
        if board != self.board or not topic.endswith("/" + self.board):
            raise RuntimeError("Printer identity changed; reconnect before continuing")
        if topic == f"sdcp/error/{self.board}":
            raise RuntimeError(f"Printer reported an error: {message.get('Data', {}).get('Data')}")
        for key in ("Status", "Attributes"):
            if key in message and topic == f"sdcp/{key.lower()}/{self.board}":
                self.latest[key] = message[key]
        return message

    def request(self, command: int, data: dict) -> dict:
        if command not in {258, 128}:
            raise ValueError("Unsupported printer command")
        request_id = self._send(command, data)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            message = self._receive(deadline)
            response = message.get("Data", {})
            if (message.get("Topic") == f"sdcp/response/{self.board}"
                    and response.get("RequestID") == request_id
                    and response.get("Cmd") == command):
                result = response.get("Data", {})
                if result.get("Ack") != 0:
                    raise RuntimeError(f"Printer rejected command {command}: Ack={result.get('Ack')}")
                return result
        raise RuntimeError("No matching printer acknowledgement; do not automatically retry print start")

    def status(self) -> dict:
        self.latest.clear()
        self._send(0, {})
        self._send(1, {})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            self._receive(deadline)
            if "Status" in self.latest and "Attributes" in self.latest:
                warnings = []
                if self.latest["Attributes"].get("XYZsize") != "153.36x77.76x165":
                    warnings.append("Network XYZsize differs from Mars 5 Ultra; keep verified slicer dimensions 153.36 x 77.76 x 165 mm.")
                return {"device": self.device, **self.latest, "warnings": warnings,
                        "idle": self.latest["Status"].get("CurrentStatus") == [0]}
        raise RuntimeError("Could not read fresh printer status and attributes")

    def files(self) -> list[dict]:
        result = self.request(258, {"Url": "/local/"})
        files = result.get("FileList")
        if not isinstance(files, list):
            raise RuntimeError("Printer did not return a file listing")
        return files


def connect_device(address: str, board_id: str | None = None) -> dict:
    devices = discover(address=lan_ip(address), timeout=1.5)
    if board_id:
        devices = [item for item in devices if item["board_id"] == board_id]
    if len(devices) != 1:
        raise RuntimeError("Printer not found or identity mismatch; use printer discover")
    return devices[0]


def approved_artifact(project: str | Path) -> dict:
    """Only upload the exact bytes covered by a completed production build."""
    root = Path(project).resolve()
    if root.is_file():
        root = root.parent
    build_path = root / "artifacts/build-manifest.json"
    if not build_path.is_file():
        raise ValueError("Upload blocked: this project has no completed build report. Finish production validation first.")
    build = json.loads(build_path.read_text(encoding="utf-8"))
    manifest = json.loads((root / "project.json").read_text(encoding="utf-8"))
    if manifest.get("machine") != "elegoo-mars-5-ultra" or manifest.get("process") != "resin":
        raise ValueError("LAN delivery currently supports validated Mars 5 Ultra resin projects only")
    if build.get("status") != "completed":
        raise ValueError("Build is not completed; rejected/draft files cannot be uploaded")
    for name in ("validation", "production_validation"):
        validation = build.get(name, {})
        checks = validation.get("checks", [])
        if validation.get("status") != "passed" or not checks or any(c.get("passed") is not True for c in checks):
            raise ValueError(f"Upload blocked by missing or failed {name}")
    path = Path(build["sliced_output"]).resolve()
    if not path.is_relative_to(root / "artifacts") or path.suffix.lower() not in {".ctb", ".goo"}:
        raise ValueError("Build output is outside project artifacts or not CTB/GOO")
    digest = sha256_file(path)
    if digest != build.get("sliced_sha256"):
        raise ValueError("Sliced file changed after validation")
    if build["production_validation"].get("analysis", {}).get("source_sha256") != digest:
        raise ValueError("Production analysis does not cover these file bytes")
    from .uvtools_contract import has_complete_detection_contract
    if not has_complete_detection_contract(build["production_validation"]["analysis"]):
        raise ValueError("Upload blocked: repeat validation with corrected suction-cup and boundary detection")
    from .validation import validate_sliced_output
    if validate_sliced_output(path)["status"] != "passed":
        raise ValueError("File format validation failed")
    return {"path": str(path), "sha256": digest, "size_bytes": path.stat().st_size,
            "project": str(root), "machine": manifest["machine"]}


def check_ready(status: dict, suffix: str, capability: str) -> None:
    attrs = status.get("Attributes", {})
    if status.get("Status", {}).get("CurrentStatus") != [0]:
        raise ValueError("Printer is not idle; no transfer or print was started")
    if attrs.get("MachineName") not in {"Mars 5 Ultra", "ELEGOO Mars 5 Ultra"}:
        raise ValueError("Printer model is not Mars 5 Ultra")
    if str(attrs.get("Resolution", "")).lower() != "8520x4320":
        raise ValueError("Printer resolution does not match Mars 5 Ultra")
    if capability not in attrs.get("Capabilities", []):
        raise ValueError(f"Printer does not advertise {capability}")
    if suffix.lstrip(".").upper() not in attrs.get("SupportFileType", []):
        raise ValueError("Printer does not support this file type")
    if capability == "PRINT_CONTROL":
        devices = attrs.get("DevicesStatus", {})
        required = ("TempSensorStatusOfUVLED", "LCDStatus", "SgStatus",
                    "ZMotorStatus", "RotateMotorStatus", "RelaseFilmState")
        if any(devices.get(key) != 1 for key in required):
            raise ValueError("Printer self-check is incomplete or reports a hardware fault")
        if status.get("Status", {}).get("PrintInfo", {}).get("ErrorNumber") != 0:
            raise ValueError("Printer reports a print error; resolve it before starting")
    # Firmware can report another model's XYZsize. Never copy that into slicing.


def multipart_chunk(filename: str, chunk: bytes, fields: dict[str, str]) -> tuple[bytes, str]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", filename):
        raise ValueError("Unsafe upload filename")
    boundary = "agent-slicer-" + uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="File"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
    parts.extend([chunk, f"\r\n--{boundary}--\r\n".encode()])
    return b"".join(parts), boundary


def _post_chunk(address: str, body: bytes, boundary: str) -> None:
    connection = http.client.HTTPConnection(lan_ip(address), 3030, timeout=30)
    try:
        connection.request("POST", "/uploadFile/upload", body=body, headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        })
        response = connection.getresponse()
        raw = response.read(65537)
        if response.status != 200 or len(raw) > 65536:
            raise RuntimeError(f"Upload HTTP response {response.status}; transfer not confirmed")
        result = json.loads(raw)
        if result.get("success") is not True or result.get("code") != "000000":
            raise RuntimeError(f"Printer rejected upload chunk: {result}")
    finally:
        connection.close()


def upload_project(project: str | Path, address: str, *, board_id: str | None = None,
                   progress: Callable[[dict], None] | None = None) -> dict:
    artifact = approved_artifact(project)  # Fail closed before any network mutation.
    source = Path(artifact["path"])
    device = connect_device(address, board_id)
    name = f"photoprint-{artifact['sha256'][:12]}-{uuid.uuid4().hex[:8]}{source.suffix.lower()}"
    with SDCPPrinter(device) as printer, source.open("rb") as handle:
        status = printer.status()
        check_ready(status, source.suffix, "FILE_TRANSFER")
        if any(Path(item.get("name", "")).name == name for item in printer.files()):
            raise ValueError("Destination already exists; nothing was overwritten")
        md5 = hashlib.md5()
        sha = hashlib.sha256()
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            md5.update(block)
            sha.update(block)
        if sha.hexdigest() != artifact["sha256"]:
            raise ValueError("File changed before upload")
        handle.seek(0)
        transfer_id = uuid.uuid4().hex
        offset = 0
        sent_hash = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            body, boundary = multipart_chunk(name, chunk, {
                "S-File-MD5": md5.hexdigest(), "Check": "1", "Offset": str(offset),
                "Uuid": transfer_id, "TotalSize": str(artifact["size_bytes"]),
            })
            _post_chunk(device["address"], body, boundary)  # No blind chunk retries.
            sent_hash.update(chunk)
            offset += len(chunk)
            if progress:
                progress({"sent": offset, "total": artifact["size_bytes"]})
        if offset != artifact["size_bytes"] or sent_hash.hexdigest() != artifact["sha256"]:
            raise RuntimeError("Source changed during transfer; do not print the remote file")
        listing = printer.files()
        remote = next((item for item in listing if Path(item.get("name", "")).name == name), None)
        if remote is None or ("usedSize" in remote and remote["usedSize"] != offset):
            raise RuntimeError("Chunks accepted but matching remote file not confirmed; do not print")
    receipt = {"schema": "agent-slicer-upload/v1", "status": "uploaded",
               "device": device, "filename": name, "remote_path": remote["name"],
               "artifact": artifact, "md5": md5.hexdigest(), "transfer_id": transfer_id,
               "timestamp": int(time.time()), "print_started": False,
               "verification": "MD5-enabled transfer acknowledgements and remote filename listing",
               "remote_size_verified": remote.get("usedSize") == offset,
               "limitations": ["Firmware may omit file size. Remote bytes were not downloaded and independently hashed."]}
    receipt_path = Path(artifact["project"]) / "artifacts" / f"upload-{transfer_id}.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return {**receipt, "receipt": str(receipt_path)}


def start_print(receipt_path: str | Path, *, confirmed: bool = False) -> dict:
    if not confirmed:
        raise ValueError("Physical printing requires --confirm-start after checking the vat and build plate")
    path = Path(receipt_path).resolve()
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if receipt.get("schema") != "agent-slicer-upload/v1" or receipt.get("status") != "uploaded":
        raise ValueError("A verified upload receipt is required")
    artifact = approved_artifact(receipt["artifact"]["project"])
    if artifact != receipt["artifact"]:
        raise ValueError("Project artifact changed since upload")
    name = receipt["filename"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name):
        raise ValueError("Invalid remote filename")
    attempt = path.with_suffix(".start-attempt.json")
    device = connect_device(receipt["device"]["address"], receipt["device"]["board_id"])
    with SDCPPrinter(device) as printer:
        check_ready(printer.status(), Path(name).suffix, "PRINT_CONTROL")
        remote = next((f for f in printer.files() if Path(f.get("name", "")).name == name), None)
        if remote is None or ("usedSize" in remote and remote["usedSize"] != artifact["size_bytes"]):
            raise ValueError("Uploaded file no longer present with matching size")
        # Exclusive write BEFORE the command: uncertain responses cannot cause
        # an accidental second print when the caller retries.
        with attempt.open("x", encoding="utf-8") as handle:
            json.dump({"status": "start-request-pending", "filename": name,
                       "timestamp": int(time.time())}, handle)
        result = printer.request(128, {"Filename": name, "StartLayer": 0})
    return {"status": "start-acknowledged", "filename": name, "response": result,
            "note": "Printer accepted the request; observe its status for actual printing"}


def add_cli(subparsers) -> None:
    parser = subparsers.add_parser("printer", help="discover/connect/upload and explicitly start a validated print")
    commands = parser.add_subparsers(dest="printer_command", required=True)
    discovery = commands.add_parser("discover")
    discovery.add_argument("--address", help="optional private IPv4 for unicast discovery")
    for name in ("status", "files", "upload"):
        command = commands.add_parser(name)
        command.add_argument("--address", required=True)
        command.add_argument("--board-id")
        if name == "upload":
            command.add_argument("project", type=Path)
    start = commands.add_parser("print")
    start.add_argument("receipt", type=Path)
    start.add_argument("--confirm-start", action="store_true")


def _run_cli(args) -> dict:
    if args.printer_command == "discover":
        return {"printers": discover(address=args.address)}
    if args.printer_command == "upload":
        return upload_project(args.project, args.address, board_id=args.board_id)
    if args.printer_command == "print":
        return start_print(args.receipt, confirmed=args.confirm_start)
    device = connect_device(args.address, args.board_id)
    with SDCPPrinter(device) as printer:
        return printer.status() if args.printer_command == "status" else {"files": printer.files()}


def run_cli(args) -> dict:
    import websocket
    try:
        return _run_cli(args)
    except websocket.WebSocketException as exc:
        raise RuntimeError(f"Printer connection failed: {exc}. No automatic command retry was made.") from exc
