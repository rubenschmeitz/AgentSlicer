"""Bounded external-tool execution without shell expansion or pipe buffering.

Linux children start in a new session. Timeout, cancellation, and optional RSS
budget failures kill that entire process group, not just the launcher. RSS is
sampled, so the optional limit is a watchdog, not an OS-enforced hard cap.
"""
from __future__ import annotations

import math
import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .resources import native_subprocess_environment, work_directory


@dataclass
class BackendResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    metrics: dict[str, Any]


def _normalize_log_newlines(stream: Any) -> int:
    """Normalize Windows CRLF text output while keeping the full log on disk.

    Backend stdout/stderr are text evidence. Python and several native tools
    emit CRLF on Windows, while the Linux/WSL path emits LF. Compacting CRLF
    to LF makes byte limits and audit logs deterministic across hosts without
    dropping any textual content. The compaction is done in-place so large
    logs are never loaded into Python memory.
    """
    if os.name != "nt":
        return stream.seek(0, os.SEEK_END)

    stream.seek(0)
    read_position = 0
    write_position = 0
    pending_cr = False
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        read_position += len(chunk)
        normalized = bytearray()
        for value in chunk:
            if pending_cr:
                if value == 0x0A:
                    normalized.append(0x0A)
                    pending_cr = False
                    continue
                normalized.append(0x0D)
                pending_cr = False
            if value == 0x0D:
                pending_cr = True
            else:
                normalized.append(value)
        if normalized:
            stream.seek(write_position)
            stream.write(normalized)
            write_position += len(normalized)
            stream.seek(read_position)

    if pending_cr:
        stream.seek(write_position)
        stream.write(b"\r")
        write_position += 1
    stream.seek(write_position)
    stream.truncate()
    stream.flush()
    return write_position


def _tree_rss_kib(pid: int) -> int:
    pending = [pid]
    seen: set[int] = set()
    total = 0
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        try:
            status = Path(f"/proc/{current}/status").read_text()
            total += next((int(line.split()[1]) for line in status.splitlines()
                           if line.startswith("VmRSS:")), 0)
            # Children may be spawned by any thread, not only the main thread.
            for children in Path(f"/proc/{current}/task").glob("*/children"):
                try:
                    pending.extend(map(int, children.read_text().split()))
                except (OSError, ValueError):
                    pass
        except (OSError, ValueError):
            pass  # Processes may exit between samples.
    return total


def _stop(process: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:  # Windows has no equivalent session/process-group kill here.
            process.kill()
    except ProcessLookupError:
        pass
    process.wait()


def run_backend(
    command: Sequence[str], *, timeout: float,
    env: Mapping[str, str] | None = None, cwd: str | Path | None = None,
    tail_bytes: int = 65536, log_directory: str | Path | None = None,
) -> BackendResult:
    """Run argv, retain bounded log tails and return sampled resource metrics.

    Optional persistent logs use exclusive creation, never overwrite evidence.
    Agents receive at most ``tail_bytes`` of each stream. Full logs are streamed
    to native temporary files (or log_directory), not retained in Python RAM.
    """
    if not command or isinstance(command, (str, bytes)):
        raise ValueError("backend command must be a nonempty argv sequence")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("backend timeout must be finite and positive")
    if isinstance(tail_bytes, bool) or not isinstance(tail_bytes, int) or tail_bytes < 1:
        raise ValueError("tail_bytes must be a positive integer")
    environment = dict(env) if env is not None else native_subprocess_environment()
    budget = float(environment.get("AGENT_SLICER_MAX_BACKEND_RSS_MB", "0"))
    if not math.isfinite(budget) or budget < 0:
        raise ValueError("AGENT_SLICER_MAX_BACKEND_RSS_MB must be finite and nonnegative")
    directory = Path(log_directory) if log_directory is not None else work_directory("logs")
    directory.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    peak = 0
    argv = list(command)
    with tempfile.TemporaryFile(dir=directory) if log_directory is None else (directory / "stdout.log").open("x+b") as stdout, \
         tempfile.TemporaryFile(dir=directory) if log_directory is None else (directory / "stderr.log").open("x+b") as stderr:
        process = subprocess.Popen(argv, cwd=cwd, env=environment, stdout=stdout,
                                   stderr=stderr, stdin=subprocess.DEVNULL,
                                   start_new_session=os.name == "posix")
        try:
            while True:
                peak = max(peak, _tree_rss_kib(process.pid))
                if budget and peak > budget * 1024:
                    raise RuntimeError(f"backend process tree exceeded sampled RSS budget {budget:g} MiB")
                elapsed = time.monotonic() - started
                if elapsed >= timeout:
                    raise RuntimeError(f"backend timed out after {timeout:g}s: {argv[0]}")
                try:
                    process.wait(timeout=min(0.1, timeout - elapsed))
                    break
                except subprocess.TimeoutExpired:
                    pass
        except BaseException:
            _stop(process)
            raise
        tails = []
        lengths = []
        for stream in (stdout, stderr):
            length = _normalize_log_newlines(stream)
            lengths.append(length)
            stream.seek(max(0, length - tail_bytes))
            tails.append(stream.read().decode("utf-8", errors="replace"))
    return BackendResult(argv, process.returncode, *tails, {
        "wall_seconds": time.monotonic() - started,
        "sampled_peak_tree_rss_kib": peak if Path("/proc").is_dir() else None,
        "rss_method": "sum of live process-tree VmRSS, approximately 100ms sampling; shared pages may be double-counted",
        "threads": environment.get("AGENT_SLICER_THREADS"),
        "stdout_bytes": lengths[0], "stderr_bytes": lengths[1],
        "log_tail_bytes": tail_bytes,
        "log_directory": str(directory.resolve()) if log_directory is not None else None,
    })
