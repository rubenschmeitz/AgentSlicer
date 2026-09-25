from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path


NATIVE_CAPABILITIES = {
    "mesh.inspect",
    "mesh.clean.degenerate",
    "mesh.scale",
    "mesh.simplify.measured",
    "mesh.hollow",
    "mesh.drain.auto",
    "mesh.cavity.validate",
    "plate.pack.shelf",
    "plate.pack.support-aware",
    "mesh.combine",
    "resin.validate.basic",
}


@dataclass(frozen=True)
class ToolInfo:
    id: str
    path: str | None
    discovered: bool
    implemented_capabilities: tuple[str, ...]
    planned_capabilities: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def discover_tools() -> dict[str, ToolInfo]:
    checkout_root = Path(__file__).resolve().parent.parent
    resin_slicer = _find(
        "AGENT_SLICER_RESIN_SLICER",
        "resin-slicer",
        str(
            checkout_root
            / "companion"
            / "Resin-Slicer"
            / ".venv"
            / "Scripts"
            / "resin-slicer.exe"
        ),
        str(
            checkout_root
            / "companion"
            / "Resin-Slicer"
            / ".venv"
            / "bin"
            / "resin-slicer"
        ),
        str(
            Path.home()
            / "development"
            / "Resin-Slicer"
            / ".venv"
            / "bin"
            / "resin-slicer"
        ),
        "/mnt/c/Users/20160600/Documents/Development/Resin-Slicer/.venv/bin/resin-slicer",
        prefer_known=True,
    )
    blender = _find(
        "AGENT_SLICER_BLENDER",
        "blender",
        *_windows_versioned_paths(
            Path("C:/Program Files/Blender Foundation"),
            "blender.exe",
        ),
        str(Path.home() / "development-tools" / "blender" / "current" / "blender"),
        "/mnt/c/Program Files/Blender Foundation/Blender 4.5/blender.exe",
        prefer_known=True,
    )
    uvtools = _find(
        "AGENT_SLICER_UVTOOLS",
        "UVtoolsCmd",
        "C:/Program Files/UVtools/UVtoolsCmd.exe",
        str(Path.home() / "development-tools" / "uvtools" / "current" / "UVtoolsCmd"),
        "/mnt/c/Program Files/UVtools/UVtoolsCmd.exe",
        prefer_known=True,
    )
    return {
        "native": ToolInfo(
            id="native",
            path=None,
            discovered=True,
            implemented_capabilities=tuple(sorted(NATIVE_CAPABILITIES)),
            planned_capabilities=(
                "plate.pack.optimized",
            ),
        ),
        "resin-slicer": ToolInfo(
            id="resin-slicer",
            path=resin_slicer,
            discovered=resin_slicer is not None,
            implemented_capabilities=("resin.slice", "resin.support.generate")
            if resin_slicer
            else (),
            planned_capabilities=(),
        ),
        "blender": ToolInfo(
            id="blender",
            path=blender,
            discovered=blender is not None,
            implemented_capabilities=(
                "mesh.hollow.approximate",
                "mesh.drain.bottom.auto",
                "mesh.repair.measured",
                "mesh.repair.safe",
                "mesh.orient.resin",
            )
            if blender
            else (),
            planned_capabilities=(
                "mesh.render",
            ),
        ),
        "uvtools": ToolInfo(
            id="uvtools",
            path=uvtools,
            discovered=uvtools is not None,
            implemented_capabilities=(
                "resin.analyze.uvtools",
                "resin.validate.production",
            ) if uvtools else (),
            planned_capabilities=(),
        ),
    }


def available_capabilities(tools: dict[str, ToolInfo] | None = None) -> set[str]:
    catalog = tools or discover_tools()
    result: set[str] = set()
    for tool in catalog.values():
        if tool.discovered:
            result.update(tool.implemented_capabilities)
    return result


def _find(
    environment: str,
    command: str,
    *known_paths: str,
    prefer_known: bool = False,
) -> str | None:
    configured = os.environ.get(environment)
    if configured and Path(configured).is_file():
        return str(Path(configured).resolve())
    if prefer_known:
        for known_path in known_paths:
            candidate = Path(known_path)
            if candidate.is_file():
                return str(candidate.resolve())
    discovered = shutil.which(command)
    if discovered:
        return discovered
    for known_path in known_paths:
        candidate = Path(known_path)
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def _windows_versioned_paths(root: Path, executable: str) -> tuple[str, ...]:
    """Return installed Windows version paths, newest first."""
    if os.name != "nt" or not root.is_dir():
        return ()
    return tuple(
        str(path)
        for path in sorted(
            root.glob(f"*/{executable}"),
            key=lambda candidate: candidate.parent.name,
            reverse=True,
        )
    )
