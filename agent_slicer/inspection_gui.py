from __future__ import annotations

import json
import mimetypes
import math
import struct
import subprocess
import threading
import time
import webbrowser
from dataclasses import asdict
from functools import lru_cache, partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .project import load_project


_VIEWABLE_SUFFIXES = {".stl", ".png", ".jpg", ".jpeg", ".webp"}
_DOWNLOADABLE_SUFFIXES = _VIEWABLE_SUFFIXES | {
    ".ctb",
    ".goo",
    ".json",
    ".md",
    ".txt",
    ".sha256",
    ".mp4",
    ".webm",
}
_MAX_PREVIEW_TRIANGLES = 200_000
_SLICE_PREVIEW_LOCK = threading.BoundedSemaphore(1)
_SNAPSHOT_CACHE_LOCK = threading.RLock()
_SNAPSHOT_CACHE: dict[Path, tuple[float, dict[str, Any]]] = {}
_SNAPSHOT_CACHE_SECONDS = 12.0


def project_snapshot(project: str | Path) -> dict[str, Any]:
    """Build a bounded, read-only projection of one Agent-Slicer project."""
    root, manifest = load_project(project)
    plan = _read_json(root / "artifacts" / "plan.json")
    build = _read_json(root / "artifacts" / "build-manifest.json")
    basic = _read_json(root / "artifacts" / "validation.json")
    production = _read_json(root / "artifacts" / "production-validation.json")
    audits = _audited_deliveries(root)
    current_audit = audits[0] if audits else None

    inspections_by_hash = {
        item.get("sha256"): item
        for item in (plan or {}).get("inspections", [])
        if isinstance(item, dict)
    }
    models = []
    for model in manifest.models:
        source = _safe_project_path(root, model.source)
        models.append(
            {
                **asdict(model),
                "exists": source.is_file(),
                "size_bytes": source.stat().st_size if source.is_file() else None,
                "inspection": inspections_by_hash.get(model.sha256),
                "asset_url": _asset_url(root, source) if source.is_file() else None,
            }
        )

    assets = _project_assets(root)
    _attach_audited_slice_preview(root, assets, current_audit)
    pipeline_status = _project_status(plan, build)
    basic_checks = (basic or {}).get("checks", [])
    production_checks = (production or {}).get("checks", [])
    audit_validation = (current_audit or {}).get("validation", {})
    audit_issues = audit_validation.get("actionable_counts") or audit_validation.get(
        "after_actionable_issues", {}
    )
    if current_audit and not production_checks:
        production_checks = [
            {"id": f"audited.issue.{name}", "passed": count == 0, "actual": count}
            for name, count in sorted(audit_issues.items())
        ]
    delivery_status = (
        "passed"
        if current_audit
        and str(current_audit.get("status", "")).startswith("passed")
        and current_audit.get("artifact_present") is True
        and current_audit.get("size_matches") is not False
        else pipeline_status
    )
    resolved = (plan or {}).get("resolved_config", {})
    machine = resolved.get("machine", {}) if isinstance(resolved, dict) else {}
    placement_rows = (plan or {}).get("placements", [])

    return {
        "schema": "agent-slicer-inspection/v1",
        "read_only": True,
        "project": {
            "root": str(root),
            **manifest.to_dict(),
            "status": delivery_status,
            "pipeline_status": pipeline_status,
        },
        "models": models,
        "viewer_assets": [item for item in assets if item["viewable"]],
        "artifacts": assets,
        "plan": {
            "present": plan is not None,
            "blockers": (plan or {}).get("blockers", []),
            "warnings": (plan or {}).get("warnings", []),
            "operations": (plan or {}).get("operations", []),
            "placements": placement_rows,
        },
        "machine": {
            "id": manifest.machine,
            "build_volume_mm": machine.get("build_volume_mm"),
            "resolution": machine.get("resolution"),
        },
        "build": _build_summary(build),
        "audits": audits,
        "current_audit": current_audit,
        "validation": {
            "source": "audited delivery" if current_audit else "project artifacts",
            "basic_status": audit_validation.get(
                "basic_status", (basic or {}).get("status", "not-run")
            ),
            "production_status": audit_validation.get(
                "status",
                audit_validation.get(
                    "production_status", (production or {}).get("status", "not-run")
                ),
            ),
            "basic_checks": basic_checks,
            "production_checks": production_checks,
            "issue_counts": audit_issues
            or ((production or {}).get("analysis") or {}).get("issues", {}),
            "limitations": list(
                dict.fromkeys(
                    [
                        *(basic or {}).get("limitations", []),
                        *(production or {}).get("limitations", []),
                        *((current_audit or {}).get("limitations", [])),
                    ]
                )
            ),
        },
    }


def serve_inspection_gui(
    project: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    root, _ = load_project(project)
    static_root = Path(str(files("agent_slicer").joinpath("inspection_ui")))
    handler = partial(_InspectionHandler, project_root=root, static_root=static_root)
    server = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{server.server_address[1]}/"
    print(json.dumps({"status": "serving", "read_only": True, "url": url}))
    if open_browser:
        threading.Timer(0.25, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever(poll_interval=0.35)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


class _InspectionHandler(SimpleHTTPRequestHandler):
    server_version = "AgentSlicerInspection/1"

    def __init__(
        self,
        *args: Any,
        project_root: Path,
        static_root: Path,
        **kwargs: Any,
    ) -> None:
        self.project_root = project_root.resolve()
        super().__init__(*args, directory=str(static_root), **kwargs)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        request = urlsplit(self.path)
        path = request.path
        if path == "/api/snapshot":
            try:
                self._send_json(_cached_project_snapshot(self.project_root))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._send_json(
                    {"status": "error", "message": str(exc)},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            return
        if path.startswith("/asset/"):
            self._send_project_asset(unquote(path[len("/asset/") :]))
            return
        if path.startswith("/preview-stl/"):
            self._send_stl_preview(unquote(path[len("/preview-stl/") :]))
            return
        if path.startswith("/slice-layer/"):
            self._send_slice_layer(
                unquote(path[len("/slice-layer/") :]),
                parse_qs(request.query),
            )
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - explicit read-only contract
        self.send_error(HTTPStatus.METHOD_NOT_ALLOWED, "inspection GUI is read-only")

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'")
        super().end_headers()

    def _send_json(self, value: object, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_project_asset(self, relative: str) -> None:
        try:
            path = _safe_project_path(self.project_root, relative)
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not path.is_file() or path.suffix.lower() not in _DOWNLOADABLE_SUFFIXES:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                self.wfile.write(chunk)

    def _send_stl_preview(self, relative: str) -> None:
        try:
            path = _safe_project_path(self.project_root, relative)
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        info = _binary_stl_preview_info(path)
        if info is None:
            self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "binary STL required")
            return
        triangle_count, step, preview_count = info
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "model/stl")
        self.send_header("Content-Length", str(84 + preview_count * 50))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Source-Triangle-Count", str(triangle_count))
        self.send_header("X-Preview-Triangle-Count", str(preview_count))
        self.end_headers()
        with path.open("rb") as handle:
            header = handle.read(80)
            handle.read(4)
            self.wfile.write(header + struct.pack("<I", preview_count))
            output = bytearray()
            processed = 0
            records_per_chunk = 100_000
            while processed < triangle_count:
                count = min(records_per_chunk, triangle_count - processed)
                data = handle.read(count * 50)
                if len(data) != count * 50:
                    return
                first = (-processed) % step
                for local_index in range(first, count, step):
                    offset = local_index * 50
                    output.extend(data[offset : offset + 50])
                processed += count
                if len(output) >= 1024 * 1024:
                    self.wfile.write(output)
                    output.clear()
            if output:
                self.wfile.write(output)

    def _send_slice_layer(
        self,
        relative: str,
        query: dict[str, list[str]],
    ) -> None:
        try:
            path = _safe_project_path(self.project_root, relative)
            index = int(query.get("index", [""])[0])
            scale = int(query.get("scale", ["2"])[0])
        except (ValueError, IndexError):
            self.send_error(HTTPStatus.BAD_REQUEST, "invalid slice layer request")
            return
        if not path.is_file() or path.suffix.lower() not in {".ctb", ".goo"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        info = _slice_info(path)
        if info is None or index < 0 or index >= int(info["layer_count"]):
            self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            return
        scale = max(1, min(8, scale))
        if not _SLICE_PREVIEW_LOCK.acquire(timeout=5):
            self.send_error(HTTPStatus.TOO_MANY_REQUESTS, "slice decoder is busy")
            return
        try:
            completed = _run_resin_preview(
                path,
                "--layer",
                str(index),
                "--scale",
                str(scale),
            )
        finally:
            _SLICE_PREVIEW_LOCK.release()
        if completed.returncode != 0 or not completed.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
            self.send_error(HTTPStatus.UNPROCESSABLE_ENTITY, "slice layer decode failed")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(completed.stdout)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("X-Slice-Layer", str(index))
        self.end_headers()
        self.wfile.write(completed.stdout)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _cached_project_snapshot(root: Path) -> dict[str, Any]:
    resolved = root.resolve()
    now = time.monotonic()
    with _SNAPSHOT_CACHE_LOCK:
        cached = _SNAPSHOT_CACHE.get(resolved)
        if cached is not None and now - cached[0] < _SNAPSHOT_CACHE_SECONDS:
            return cached[1]
        value = project_snapshot(resolved)
        _SNAPSHOT_CACHE[resolved] = (time.monotonic(), value)
        return value


def _safe_project_path(root: Path, relative: str | Path) -> Path:
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("asset path escapes the inspected project")
    return candidate


def _asset_url(root: Path, path: Path) -> str:
    return "/asset/" + quote(path.relative_to(root).as_posix(), safe="/")


def _project_assets(root: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        suffix = path.suffix.lower()
        if suffix not in _DOWNLOADABLE_SUFFIXES:
            continue
        relative = path.relative_to(root).as_posix()
        record = {
                "name": path.name,
                "path": relative,
                "size_bytes": path.stat().st_size,
                "modified_unix": path.stat().st_mtime,
                "kind": _asset_kind(relative, suffix),
                "viewable": suffix in _VIEWABLE_SUFFIXES,
                "url": _asset_url(root, path),
            }
        if suffix == ".stl":
            preview = _binary_stl_preview_info(path)
            if preview is not None:
                triangle_count, _, preview_count = preview
                record.update(
                    {
                        "triangle_count": triangle_count,
                        "preview_triangle_count": preview_count,
                        "preview_sampled": preview_count < triangle_count,
                        "preview_url": "/preview-stl/"
                        + quote(path.relative_to(root).as_posix(), safe="/"),
                    }
                )
        result.append(record)
    return result


def _attach_audited_slice_preview(
    root: Path,
    assets: list[dict[str, Any]],
    audit: dict[str, Any] | None,
) -> None:
    relative = (audit or {}).get("final_path")
    if not isinstance(relative, str):
        return
    record = next((item for item in assets if item["path"] == relative), None)
    if record is None:
        return
    info = _slice_info(_safe_project_path(root, relative))
    if info is None:
        record["slice_preview_error"] = "native slice metadata could not be decoded"
        return
    record.update(
        {
            "kind": "audited sliced layers",
            "viewable": True,
            "is_audited_slice": True,
            "slice_info": info,
            "slice_layer_url": "/slice-layer/" + quote(relative, safe="/"),
        }
    )


def _slice_info(path: Path) -> dict[str, Any] | None:
    if path.stat().st_size < 100:
        return None
    return _slice_info_cached(str(path), path.stat().st_size, path.stat().st_mtime_ns)


@lru_cache(maxsize=16)
def _slice_info_cached(
    path: str,
    _size: int,
    _modified_ns: int,
) -> dict[str, Any] | None:
    completed = _run_resin_preview(Path(path), "--info-json")
    if completed.returncode != 0:
        return None
    try:
        value = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _run_resin_preview(
    path: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[bytes]:
    from .backends import discover_tools
    from .resources import native_subprocess_environment

    executable = discover_tools()["resin-slicer"].path
    if not executable:
        return subprocess.CompletedProcess([], 127, b"", b"Resin-Slicer not found")
    python = Path(executable).resolve().parent / "python"
    if not python.is_file():
        python = python.with_suffix(".exe")
    if not python.is_file():
        return subprocess.CompletedProcess([], 127, b"", b"Resin-Slicer Python not found")
    return subprocess.run(
        [
            str(python),
            "-m",
            "resin_slicer.layer_preview",
            str(path),
            *arguments,
        ],
        check=False,
        capture_output=True,
        timeout=30,
        env=native_subprocess_environment(),
    )


def _asset_kind(relative: str, suffix: str) -> str:
    if relative == "artifacts/plate.stl":
        return "assembled plate"
    if relative.startswith("sources/"):
        return "source model"
    if "/prepared/" in relative:
        return "prepared model"
    if "/simplified/" in relative:
        return "simplified model"
    if suffix in {".ctb", ".goo"}:
        return "sliced output"
    if suffix == ".json":
        return "machine report"
    if suffix == ".md":
        return "human report"
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm"}:
        return "preview"
    return "artifact"


def _binary_stl_preview_info(path: Path) -> tuple[int, int, int] | None:
    if not path.is_file() or path.suffix.lower() != ".stl" or path.stat().st_size < 84:
        return None
    with path.open("rb") as handle:
        handle.seek(80)
        raw_count = handle.read(4)
    if len(raw_count) != 4:
        return None
    triangle_count = struct.unpack("<I", raw_count)[0]
    if 84 + triangle_count * 50 != path.stat().st_size:
        return None
    step = max(1, math.ceil(triangle_count / _MAX_PREVIEW_TRIANGLES))
    preview_count = math.ceil(triangle_count / step)
    return triangle_count, step, preview_count


def _project_status(plan: dict[str, Any] | None, build: dict[str, Any] | None) -> str:
    if build and isinstance(build.get("status"), str):
        return str(build["status"])
    if plan and plan.get("blockers"):
        return "blocked"
    if plan:
        return "planned"
    return "draft"


def _build_summary(build: dict[str, Any] | None) -> dict[str, Any]:
    if not build:
        return {"present": False, "status": "not-run"}
    attempts = build.get("slice_attempts", [])
    last_slice = build.get("slice", {}) if isinstance(build.get("slice"), dict) else {}
    automatic = last_slice.get("automatic_supports", {})
    return {
        "present": True,
        "status": build.get("status", "unknown"),
        "plate_sha256": build.get("plate_sha256"),
        "sliced_sha256": build.get("sliced_sha256"),
        "sliced_output": build.get("sliced_output"),
        "triangle_count": (build.get("mesh_stats") or {}).get("triangle_count"),
        "plate_dimensions_mm": (build.get("plate_inspection") or {}).get("dimensions"),
        "slice_attempts": len(attempts) if isinstance(attempts, list) else 0,
        "automatic_supports": automatic,
        "performance": last_slice.get("performance", {}),
        "support_tree": last_slice.get("support_tree", {}),
    }


def _audited_deliveries(root: Path) -> list[dict[str, Any]]:
    deliveries: list[dict[str, Any]] = []
    for path in root.glob("artifacts/audited*/**/audit-manifest.json"):
        try:
            value = _read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if value is None or not str(value.get("schema", "")).startswith("agent-slicer-"):
            continue
        final_ctb = value.get("final_ctb", {})
        final_name = final_ctb.get("filename") if isinstance(final_ctb, dict) else None
        final_path = path.parent / str(final_name) if final_name else None
        artifact_present = bool(final_path and final_path.is_file())
        expected_bytes = final_ctb.get("bytes") if isinstance(final_ctb, dict) else None
        size_matches = (
            final_path.stat().st_size == int(expected_bytes)
            if artifact_present and isinstance(expected_bytes, int)
            else None
        )
        support_geometry = value.get("support_geometry", {})
        legacy_supports = value.get("supports", {})
        deliveries.append(
            {
                "manifest": path.relative_to(root).as_posix(),
                "manifest_url": _asset_url(root, path),
                "schema": value.get("schema"),
                "status": value.get("status", "unknown"),
                "created_at": value.get("created_at"),
                "final_ctb": final_ctb,
                "final_path": (
                    final_path.relative_to(root).as_posix()
                    if final_path and final_path.is_relative_to(root)
                    else None
                ),
                "artifact_present": artifact_present,
                "size_matches": size_matches,
                "validation": value.get("validation", {}),
                "limitations": value.get("limitations", []),
                "supports": {
                    "requested": support_geometry.get(
                        "manual_contacts_requested", legacy_supports.get("requested")
                    ),
                    "routed": support_geometry.get(
                        "manual_contacts_routed", legacy_supports.get("routed")
                    ),
                    "unrouted": support_geometry.get(
                        "manual_contacts_unrouted", legacy_supports.get("unrouted")
                    ),
                    "support_elements": support_geometry.get("support_elements"),
                    "roots": support_geometry.get("roots"),
                    "branches": support_geometry.get("branches"),
                },
            }
        )
    return sorted(
        deliveries,
        key=lambda item: str(item.get("created_at") or ""),
        reverse=True,
    )
