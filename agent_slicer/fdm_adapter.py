"""Audited OrcaSlicer/Bambu Studio CLI execution; algorithms remain upstream."""
from __future__ import annotations

import json
import os
import tempfile
import shutil
import zipfile
from pathlib import Path
from typing import Any

from .backends import discover_tools, fdm_tool
from .fdm_profiles import resolve_fdm_profiles
from .processes import run_backend
from .project import sha256_file
from .resources import native_subprocess_environment
from .fdm_validation import validate_fdm_output


def slice_fdm(plate: Path, output: Path, resolved: dict[str, Any], *,
              evidence_directory: Path, executable: str | None = None,
              configured_profile_root: str | Path | None = None) -> dict:
    executable = fdm_tool(discover_tools(), resolved, executable)
    if not executable:
        raise RuntimeError("FDM slicer not found; set AGENT_SLICER_ORCA_SLICER, AGENT_SLICER_BAMBU_STUDIO or --fdm-slicer")
    executable_path = Path(executable).resolve()
    if not executable_path.is_file():
        raise ValueError(f"FDM executable does not exist: {executable_path}")
    if plate.resolve() == output.resolve():
        raise ValueError("FDM source and output must be distinct")
    if output.exists():
        raise FileExistsError(f"FDM output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    settings = resolve_fdm_profiles(resolved, str(executable_path), configured_profile_root)
    evidence_directory.mkdir(parents=True, exist_ok=False)
    profile_paths = {}
    for kind, profile in settings["profiles"].items():
        path = evidence_directory / f"{kind}.json"
        path.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        profile_paths[kind] = str(path.resolve())
    environment = native_subprocess_environment()
    timeout = int(os.environ.get("AGENT_SLICER_FDM_SLICE_TIMEOUT_SECONDS", "10800"))
    if not 60 <= timeout <= 86400:
        raise ValueError("AGENT_SLICER_FDM_SLICE_TIMEOUT_SECONDS must be between 60 and 86400")
    receipt: dict[str, Any] = {
        "schema": "agent-slicer-fdm-slice/v1", "backend": "OrcaSlicer", "status": "running",
        "executable": str(executable_path), "executable_sha256": sha256_file(executable_path),
        "source_sha256": sha256_file(plate), "profiles": profile_paths,
        "profile_sha256": {k: sha256_file(Path(v)) for k, v in profile_paths.items()},
        "upstream_profiles": settings["sources"], "profile_root": settings["profile_root"],
        "receipt": str((evidence_directory / "receipt.json").resolve()),
        "native_profile_evidence": settings["native_evidence"],
    }
    implementation = executable_path.parent / "bin" / ("bambu-studio" if "bambu" in str(executable_path).lower() else "orca-slicer")
    if implementation.is_file():
        receipt["implementation"] = {"path": str(implementation), "sha256": sha256_file(implementation)}
    try:
        version = run_backend([str(executable_path), "--help"], timeout=60, env=environment,
                              cwd=evidence_directory, log_directory=evidence_directory / "version-logs")
        first_line = next((line for line in version.stdout.splitlines() if line.startswith(("OrcaSlicer-", "BambuStudio-"))), "")
        if version.returncode != 0 or not first_line:
            raise RuntimeError(f"configured FDM executable is not a supported CLI: {version.stderr[-2000:]}")
        receipt["version"] = first_line
        bambu = first_line.startswith("BambuStudio-")
        receipt["backend"] = "Bambu Studio" if bambu else "OrcaSlicer"
        with tempfile.TemporaryDirectory(prefix="orca-", dir=output.parent) as temp:
            transaction = Path(temp)
            command = [str(executable_path), "--datadir", str(transaction / "user-data"),
                       "--load-settings", profile_paths["machine"] + ";" + profile_paths["process"],
                       "--load-filaments", profile_paths["filament"], "--orient", "0", "--arrange", "0",
                       "--slice", "0", "--export-3mf", "slice.gcode.3mf", "--outputdir", str(transaction), str(plate.resolve())]
            if not bambu:
                command[1:1] = ["--logfile", str((evidence_directory / "orca.log").resolve())]
            completed = run_backend(command, timeout=timeout, env=environment,
                                    cwd=transaction, log_directory=evidence_directory / "slice-logs")
            receipt.update(command=command, exit_code=completed.returncode,
                           stdout=completed.stdout, stderr=completed.stderr, process_resources=completed.metrics)
            result_path = transaction / "result.json"
            if result_path.is_file():
                receipt["backend_result"] = json.loads(result_path.read_text(encoding="utf-8"))
            sliced = transaction / "slice.gcode.3mf"
            if completed.returncode != 0 or not sliced.is_file():
                if sliced.is_file():
                    rejected = evidence_directory / "failed.gcode.3mf"
                    shutil.copy2(sliced, rejected)
                    receipt["rejected_output"] = str(rejected.resolve())
                raise RuntimeError(f"{receipt['backend']} failed (exit {completed.returncode}): {completed.stderr[-4000:] or completed.stdout[-2000:]}")
            receipt["validation"] = validate_fdm_output(sliced, resolved=resolved, expected_profiles=settings["profiles"],
                native_keys={kind: value.get("validated_keys", []) for kind, value in settings["native_evidence"].items()})
            if receipt["validation"]["status"] != "passed":
                rejected = evidence_directory / "rejected.gcode.3mf"
                shutil.copy2(sliced, rejected)
                receipt["rejected_output"] = str(rejected)
                receipt["validation"]["path"] = str(rejected.resolve())
                raise RuntimeError("FDM output failed validation; see slice evidence receipt")
            if resolved["output_format"] == "gcode":
                with zipfile.ZipFile(sliced) as archive, archive.open("Metadata/plate_1.gcode") as source, output.open("wb") as target:
                    shutil.copyfileobj(source, target)
            else:
                shutil.copy2(sliced, output)
        receipt["validation"]["path"] = str(output.resolve())
        receipt.update(status="passed", output=str(output.resolve()), output_sha256=sha256_file(output))
    except Exception as exc:
        receipt.update(status="failed", error=type(exc).__name__, message=str(exc))
        raise
    finally:
        (evidence_directory / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt
