from __future__ import annotations

import os
import tempfile
from pathlib import Path


def work_root() -> Path:
    """Return a writable native work directory for large intermediates."""

    configured = os.environ.get("RESIN_SLICER_WORK_ROOT")
    if configured:
        root = Path(configured).expanduser().resolve()
    elif _is_wsl():
        root = Path.home() / "development-tools" / "resin-slicer-work"
    else:
        cache = os.environ.get("XDG_CACHE_HOME")
        root = (
            Path(cache).expanduser().resolve() / "resin-slicer"
            if cache
            else Path(tempfile.gettempdir()) / "resin-slicer"
        )
    root.mkdir(parents=True, exist_ok=True)
    return root


def work_subdirectory(name: str) -> Path:
    if not name or Path(name).name != name:
        raise ValueError("work subdirectory must be one path component")
    directory = work_root() / name
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _is_wsl() -> bool:
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8")
    except OSError:
        return False
    return "microsoft" in release.lower()
