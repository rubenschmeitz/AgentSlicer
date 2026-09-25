"""Sequential, evidence-preserving CLI benchmarks (not printability verdicts)."""
from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import statistics
from pathlib import Path
from typing import Any

from .processes import run_backend
from .resources import heavy_job, native_subprocess_environment


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _save(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def _validate(manifest: dict[str, Any]) -> None:
    if not isinstance(manifest, dict) or manifest.get("schema") != "agent-slicer-benchmark/v1":
        raise ValueError("unsupported benchmark manifest schema")
    if not isinstance(manifest.get("cases"), list) or not manifest["cases"]:
        raise ValueError("benchmark requires cases")
    seen = set()
    for case in manifest["cases"]:
        if not isinstance(case, dict):
            raise ValueError("benchmark cases must be objects")
        name = case.get("id", "")
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", name) or name in seen:
            raise ValueError("benchmark case IDs must be unique safe path components")
        seen.add(name)
        for field in ("argv", "version_argv"):
            argv = case.get(field)
            if not isinstance(argv, list) or not argv or not all(isinstance(v, str) for v in argv):
                raise ValueError(f"{field} must be a nonempty argv array")
        if not any("{input}" in value for value in case["argv"]) or not any("{output}" in value for value in case["argv"]):
            raise ValueError("benchmark argv must use {input} and {output}")
        if case.get("format") not in {"ctb", "goo", "sl1"}:
            raise ValueError("benchmark format must be ctb, goo, or sl1")
        timeout = case.get("timeout_seconds", 600)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("benchmark timeout must be finite and positive")
        environment = case.get("env", {})
        if not isinstance(environment, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in environment.items()):
            raise ValueError("benchmark env must map strings to strings")
        if not isinstance(case.get("input"), str):
            raise ValueError("benchmark input must be a file path")
        source = Path(case["input"])
        if not source.is_absolute() or not source.is_file():
            raise ValueError("benchmark input must be an existing absolute file")
        if case.get("input_sha256") != sha256(source):
            raise ValueError(f"input checksum mismatch: {name}")


@heavy_job
def run_benchmark(manifest_path: str | Path, output: str | Path, *, repeats: int = 3) -> dict[str, Any]:
    """Run trusted local argv manifests; outputs are never uploaded or printed.

    Commands in the manifest are executable code and require the same trust as
    a shell script. No shell is used. Fresh result roots prevent mixing code
    revisions or overwriting previous evidence. Each completed run is checkpointed.
    """
    if isinstance(repeats, bool) or not isinstance(repeats, int) or not 1 <= repeats <= 20:
        raise ValueError("benchmark repeats must be between 1 and 20")
    manifest = json.loads(Path(manifest_path).read_text())
    _validate(manifest)
    root = Path(output).expanduser().resolve()
    if str(root).startswith("/mnt/") and "microsoft" in platform.release().lower():
        raise ValueError("benchmark intermediates must use the native WSL filesystem")
    root.mkdir(parents=True, exist_ok=False)
    _save(root / "manifest.json", manifest)
    result: dict[str, Any] = {
        "schema": "agent-slicer-benchmark-result/v1", "status": "running",
        "platform": platform.platform(), "cpu_count": os.cpu_count(),
        "cpu_affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
        "manifest_sha256": sha256(Path(manifest_path)), "repeats": repeats,
        "notes": ["Timing includes each CLI's own stages, not identical algorithms.",
                  "Measured exports are NOT production-validated or printer-ready.",
                  "No OS cache flush; fresh processes and output paths per repetition."],
        "cases": [],
    }
    for case in manifest["cases"]:
        environment = native_subprocess_environment()
        environment.update(case.get("env", {}))
        entry: dict[str, Any] = {"id": case["id"], "input_sha256": case["input_sha256"],
                                 "runs": []}
        result["cases"].append(entry)
        try:
            identity = run_backend(case["version_argv"], timeout=30, env=environment)
            if identity.returncode:
                raise RuntimeError(identity.stderr)
            entry["version_output"] = identity.stdout
            executable = Path(case["argv"][0])
            entry["executable_sha256"] = sha256(executable) if executable.is_file() else None
            # Agent-authored manifests may pin source modules or binaries beyond
            # a CLI wrapper; this captures dirty checkouts as well as releases.
            entry["implementation_files"] = {str(Path(p).resolve()): sha256(Path(p))
                                              for p in case.get("implementation_files", [])}
        except (OSError, RuntimeError, ValueError) as error:
            result["status"] = "failed"
            entry["error"] = f"engine identity check failed: {error}"
            _save(root / "results.json", result)
            raise RuntimeError(f"benchmark identity failed; evidence retained at {root}: {error}") from error
        for repeat in range(repeats):
            directory = root / case["id"] / str(repeat + 1)
            directory.mkdir(parents=True)
            destination = directory / f"slice.{case['format']}"
            replacements = {"{input}": case["input"], "{output}": str(destination),
                            "{run}": str(directory)}
            argv = list(case["argv"])
            for marker, value in replacements.items():
                argv = [arg.replace(marker, value) for arg in argv]
            record: dict[str, Any] = {"argv": argv, "status": "running"}
            entry["runs"].append(record)
            _save(root / "results.json", result)
            try:
                # GNU time measures child high-water RSS separately from our
                # sampled process-tree watchdog. Never call it "total tree RSS".
                timed = argv
                if Path("/usr/bin/time").is_file():
                    timed = ["/usr/bin/time", "-f", "%e %U %S %M", "-o", str(directory / "time.txt"), *argv]
                completed = run_backend(timed, timeout=float(case.get("timeout_seconds", 600)),
                                        env=environment, cwd=directory, log_directory=directory)
                record.update({"exit_code": completed.returncode, "resources": completed.metrics})
                timing = directory / "time.txt"
                if timing.exists():
                    values = timing.read_text().strip().splitlines()[-1].split()
                    record["gnu_time"] = dict(zip(("wall_seconds", "user_seconds", "system_seconds", "max_rss_kib"), map(float, values)))
                if completed.returncode or not destination.is_file() or destination.stat().st_size == 0:
                    raise RuntimeError(f"engine did not export successfully: {completed.stderr[-2000:]}")
                if sha256(Path(case["input"])) != case["input_sha256"]:
                    raise RuntimeError("engine modified the source model; stopping benchmark")
                if any(sha256(Path(path)) != digest for path, digest in entry["implementation_files"].items()):
                    raise RuntimeError("engine implementation changed during benchmark; results cannot be mixed")
                record.update({"status": "exported-not-production-validated", "output": str(destination),
                               "output_bytes": destination.stat().st_size, "output_sha256": sha256(destination)})
            except (OSError, RuntimeError, ValueError) as error:
                record.update({"status": "failed", "error": str(error)})
                result["status"] = "failed"
                _save(root / "results.json", result)
                raise RuntimeError(f"benchmark failed; evidence retained at {root}: {error}") from error
            _save(root / "results.json", result)
        entry["median_wall_seconds"] = statistics.median(run["resources"]["wall_seconds"] for run in entry["runs"])
        entry["output_hashes_identical"] = len({run["output_sha256"] for run in entry["runs"]}) == 1
    result["status"] = "measured-not-production-validated"
    _save(root / "results.json", result)
    return result
