"""Public read-only SDK for bounded exact saved-layer PNG evidence."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .backends import discover_tools
from .project import sha256_file
from .resources import native_subprocess_environment


def inspect_detached_layers(source: str | Path, selection: str, *,
                            minimum_area_px: int = 1, attachment_radius_px: int = 2,
                            support_cache: str | Path | None = None,
                            output: str | Path | None = None) -> dict:
    """Read exact detached-region bounds without changing the sliced file."""
    source = Path(source).resolve()
    executable = discover_tools()["resin-slicer"].path
    if not executable:
        raise RuntimeError("Resin-Slicer backend is unavailable")
    python = Path(executable).resolve().parent / "python"
    if not python.is_file():
        python = python.with_suffix(".exe")
    if not python.is_file():
        raise RuntimeError("Resin-Slicer Python runtime is unavailable")
    digest = sha256_file(source)
    command = [str(python), "-m", "resin_slicer.layer_preview", str(source),
        "--detached-json", selection, "--attachment-radius", str(attachment_radius_px),
        "--min-area-px", str(minimum_area_px)]
    if support_cache is not None:
        command.extend(["--support-cache", str(Path(support_cache).resolve())])
    completed = subprocess.run(command, check=False, capture_output=True,
        text=True, timeout=600, env=native_subprocess_environment())
    if completed.returncode:
        raise RuntimeError(f"Detached-layer inspection failed: {completed.stderr[-4000:]}")
    if sha256_file(source) != digest:
        raise RuntimeError("Sliced file changed during inspection")
    report = json.loads(completed.stdout)
    report.update({"source": str(source), "source_sha256": digest,
                   "limitations": ["Selected-layer diagnostic, not full production validation."]})
    if output is not None:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")
    return report


def export_slice_layers(source: str | Path, selection: str, output_directory: str | Path,
                        *, crop: tuple[int, int, int, int] | None = None,
                        scale: int = 1) -> dict:
    source = Path(source).resolve()
    output = Path(output_directory).resolve()
    executable = discover_tools()["resin-slicer"].path
    if not executable:
        raise RuntimeError("Resin-Slicer backend is unavailable")
    python = Path(executable).resolve().parent / "python"
    if not python.is_file():
        python = python.with_suffix(".exe")
    if not python.is_file():
        raise RuntimeError("Resin-Slicer Python runtime is unavailable")
    digest = sha256_file(source)
    command = [str(python), "-m", "resin_slicer.layer_preview", str(source),
               "--layers", selection, "--output-dir", str(output), "--scale", str(scale)]
    if crop is not None:
        if len(crop) != 4 or any(type(value) is not int for value in crop):
            raise ValueError("crop requires integer x, y, width, height")
        command.extend(["--crop", *map(str, crop)])
    completed = subprocess.run(command, check=False, capture_output=True, text=True,
                               timeout=300, env=native_subprocess_environment())
    if completed.returncode:
        raise RuntimeError(f"Saved-layer export failed: {completed.stderr[-4000:]}")
    report = json.loads(completed.stdout)
    if sha256_file(source) != digest:
        raise RuntimeError("Sliced file changed during preview export")
    report.update({"schema": "agent-slicer-layer-evidence/v1", "source": str(source),
                   "source_sha256": digest, "purpose": "Read-only exact saved-layer inspection"})
    (output / "evidence.json").write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
    return report
