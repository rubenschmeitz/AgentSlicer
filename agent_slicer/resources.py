from __future__ import annotations

import functools
import gc
import json
import math
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

try:  # pragma: no cover - the production workflow runs on Linux/WSL.
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback.
    fcntl = None  # type: ignore[assignment]
    import msvcrt


_T = TypeVar("_T")
_thread_state = threading.local()
_process_thread_lock = threading.RLock()


def heavy_job(function: Callable[..., _T]) -> Callable[..., _T]:
    """Serialize memory-intensive Agent-Slicer jobs across local agents."""

    @functools.wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> _T:
        with heavy_job_guard(function.__name__):
            return function(*args, **kwargs)

    return wrapped


def release_process_memory() -> None:
    """Return large transient arrays before the next heavy pipeline phase."""

    gc.collect()
    if os.name != "posix":
        return
    try:
        import ctypes

        libc = ctypes.CDLL(None)
        trim = getattr(libc, "malloc_trim", None)
        if trim is not None:
            trim(0)
    except (AttributeError, OSError):
        pass


@contextmanager
def heavy_job_guard(label: str) -> Iterator[None]:
    """Hold one per-user file lock while a heavy build or analysis is active.

    File locks are released by the kernel if an agent exits unexpectedly. Calls
    nested inside a build are re-entrant in the same process, while a different
    Codex task waits without allocating the slicer's large geometry buffers.
    """

    with _process_thread_lock:
        depth = int(getattr(_thread_state, "depth", 0))
        if depth:
            _thread_state.depth = depth + 1
            try:
                yield
            finally:
                _thread_state.depth -= 1
            return

        path = _lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        timeout = _wait_timeout_seconds()
        deadline = time.monotonic() + timeout
        # Windows byte-range locks deny reads through other handles. Keep the
        # human-readable owner record separate from the mutex so agents can
        # inspect it while another heavy job is running.
        mutex_path = (
            path.with_name(path.name + ".mutex")
            if fcntl is None
            else path
        )
        with mutex_path.open("a+", encoding="utf-8") as lock_handle, \
             path.open("a+", encoding="utf-8") as metadata_handle:
            announced = False
            while True:
                try:
                    _lock_nonblocking(lock_handle)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            f"timed out waiting {timeout:.0f}s for heavy-job lock {path}"
                        ) from None
                    if not announced:
                        print(
                            f"waiting for another Agent-Slicer heavy job ({path})",
                            file=sys.stderr,
                        )
                        announced = True
                    time.sleep(0.5)

            metadata = {
                "pid": os.getpid(),
                "label": label,
                "started_unix": time.time(),
                "command": sys.argv,
            }
            metadata_handle.seek(0)
            metadata_handle.truncate()
            json.dump(metadata, metadata_handle, sort_keys=True)
            metadata_handle.write("\n")
            metadata_handle.flush()
            _thread_state.depth = 1
            try:
                yield
            finally:
                _thread_state.depth = 0
                metadata_handle.seek(0)
                metadata_handle.truncate()
                metadata_handle.flush()
                _unlock(lock_handle)


def _lock_path() -> Path:
    configured = os.environ.get("AGENT_SLICER_RESOURCE_LOCK")
    if configured:
        return Path(configured).expanduser().resolve()
    work_root = os.environ.get("AGENT_SLICER_WORK_ROOT")
    if work_root:
        return Path(work_root).expanduser().resolve() / ".heavy-job.lock"
    return (
        _home_directory()
        / "development-tools"
        / "agent-slicer-work"
        / ".heavy-job.lock"
    )


def _wait_timeout_seconds() -> float:
    value = os.environ.get("AGENT_SLICER_RESOURCE_WAIT_SECONDS", "21600")
    try:
        timeout = float(value)
    except ValueError as exc:
        raise ValueError("AGENT_SLICER_RESOURCE_WAIT_SECONDS must be numeric") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("AGENT_SLICER_RESOURCE_WAIT_SECONDS must be positive")
    return timeout


def _lock_nonblocking(handle: Any) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    try:
        # A Windows byte-range lock also denies reads. Inspect file size,
        # not the locked byte, when another worker owns this mutex.
        if os.fstat(handle.fileno()).st_size == 0:
            handle.seek(0)
            handle.write("\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        raise BlockingIOError from exc


def _unlock(handle: Any) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def work_directory(category: str) -> Path:
    if not category or Path(category).name != category:
        raise ValueError("work category must be one path component")
    configured = os.environ.get("AGENT_SLICER_WORK_ROOT")
    root = (
        Path(configured).expanduser().resolve()
        if configured
        else _home_directory() / "development-tools" / "agent-slicer-work"
    )
    directory = root / category
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def native_subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    threads = int(environment.get("AGENT_SLICER_THREADS", "2"))
    if not 1 <= threads <= 256:
        raise ValueError("AGENT_SLICER_THREADS must be an integer between 1 and 256")
    environment["AGENT_SLICER_THREADS"] = str(threads)
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                "NUMEXPR_NUM_THREADS", "BLIS_NUM_THREADS"):
        environment[key] = str(threads)
    if _is_wsl():
        environment["TMPDIR"] = str(work_directory("tmp"))
        environment.pop("TEMP", None)
        environment.pop("TMP", None)
        environment.setdefault(
            "RESIN_SLICER_WORK_ROOT",
            (_home_directory() / "development-tools" / "resin-slicer-work").as_posix(),
        )
    return environment


def _home_directory() -> Path:
    """Return a usable home directory even with a deliberately sparse env."""
    # A WSL subprocess must receive a Linux path. On Windows, Path.home()
    # otherwise resolves to the host profile even when the caller is testing
    # or emulating the WSL environment.
    if _is_wsl():
        configured = os.environ.get("HOME")
        if configured:
            return Path(configured).expanduser()
        return Path("/home/agent")
    try:
        return Path.home()
    except RuntimeError:
        pass
    for key in ("USERPROFILE", "HOME"):
        value = os.environ.get(key)
        if value:
            return Path(value).expanduser()
    drive = os.environ.get("HOMEDRIVE")
    home_path = os.environ.get("HOMEPATH")
    if drive and home_path:
        return Path(drive + home_path)
    return Path.cwd()


def _is_wsl() -> bool:
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8")
    except OSError:
        return False
    return "microsoft" in release.lower()
