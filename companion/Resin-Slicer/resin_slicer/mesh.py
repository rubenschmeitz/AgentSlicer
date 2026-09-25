from __future__ import annotations

import mmap
import os
import struct
from array import array
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path

from .errors import MeshError

Point3 = tuple[float, float, float]
Triangle = tuple[Point3, Point3, Point3]


class BinaryStlTriangles(Sequence[Triangle]):
    """Compact, lazy triangle view over a memory-mapped binary STL."""

    __slots__ = ("_mapping", "_indices")

    def __init__(self, mapping: mmap.mmap, indices: array) -> None:
        self._mapping = mapping
        self._indices = indices

    def __len__(self) -> int:
        return len(self._indices)

    def __getitem__(self, index: int | slice) -> Triangle | tuple[Triangle, ...]:
        if isinstance(index, slice):
            return tuple(self[position] for position in range(*index.indices(len(self))))  # type: ignore[misc]
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        record = int(self._indices[index])
        values = struct.unpack_from("<9f", self._mapping, 84 + record * 50 + 12)
        return (
            (values[0], values[1], values[2]),
            (values[3], values[4], values[5]),
            (values[6], values[7], values[8]),
        )

    def __iter__(self) -> Iterator[Triangle]:
        mapping = self._mapping
        for record in self._indices:
            values = struct.unpack_from("<9f", mapping, 84 + int(record) * 50 + 12)
            yield (
                (values[0], values[1], values[2]),
                (values[3], values[4], values[5]),
                (values[6], values[7], values[8]),
            )

    def __add__(self, other: Sequence[Triangle]) -> tuple[Triangle, ...]:
        return tuple(self) + tuple(other)

    def __radd__(self, other: Sequence[Triangle]) -> tuple[Triangle, ...]:
        return tuple(other) + tuple(self)

    @property
    def storage_bytes(self) -> int:
        return len(self._indices) * self._indices.itemsize


class MappedTriangles(Sequence[Triangle]):
    """Lazy point transform preserving compact source storage."""

    __slots__ = ("_source", "_map_point")

    def __init__(
        self,
        source: Sequence[Triangle],
        map_point: Callable[[Point3], Point3],
    ) -> None:
        self._source = source
        self._map_point = map_point

    def __len__(self) -> int:
        return len(self._source)

    def __getitem__(self, index: int | slice) -> Triangle | tuple[Triangle, ...]:
        if isinstance(index, slice):
            return tuple(self[position] for position in range(*index.indices(len(self))))  # type: ignore[misc]
        triangle = self._source[index]
        return tuple(self._map_point(point) for point in triangle)  # type: ignore[return-value]

    def __iter__(self) -> Iterator[Triangle]:
        map_point = self._map_point
        for triangle in self._source:
            yield tuple(map_point(point) for point in triangle)  # type: ignore[misc]

    def __add__(self, other: Sequence[Triangle]) -> tuple[Triangle, ...]:
        return tuple(self) + tuple(other)

    def __radd__(self, other: Sequence[Triangle]) -> tuple[Triangle, ...]:
        return tuple(other) + tuple(self)


@dataclass(frozen=True)
class Bounds:
    min_x: float
    min_y: float
    min_z: float
    max_x: float
    max_y: float
    max_z: float

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def depth(self) -> float:
        return self.max_y - self.min_y

    @property
    def height(self) -> float:
        return self.max_z - self.min_z


@dataclass(frozen=True)
class Mesh:
    triangles: Sequence[Triangle]
    _bounds_cache: Bounds | None = field(default=None, compare=False, repr=False)

    def bounds(self) -> Bounds:
        cached = self._bounds_cache
        if cached is not None:
            return cached
        if not self.triangles:
            raise MeshError("mesh contains no triangles")
        min_x = min_y = min_z = float("inf")
        max_x = max_y = max_z = float("-inf")
        for triangle_index, tri in enumerate(self.triangles):
            for x, y, z in tri:
                if not (isfinite(x) and isfinite(y) and isfinite(z)):
                    raise MeshError(
                        "mesh contains non-finite coordinates in triangle "
                        f"{triangle_index}"
                    )
                min_x = min(min_x, x)
                min_y = min(min_y, y)
                min_z = min(min_z, z)
                max_x = max(max_x, x)
                max_y = max(max_y, y)
                max_z = max(max_z, z)
        result = Bounds(min_x, min_y, min_z, max_x, max_y, max_z)
        object.__setattr__(self, "_bounds_cache", result)
        return result

    def transformed(self, offset: Point3) -> "Mesh":
        ox, oy, oz = offset
        if ox == 0 and oy == 0 and oz == 0:
            return self
        bounds = self.bounds()
        return Mesh(
            MappedTriangles(
                self.triangles,
                lambda point: (point[0] + ox, point[1] + oy, point[2] + oz),
            ),
            Bounds(
                bounds.min_x + ox,
                bounds.min_y + oy,
                bounds.min_z + oz,
                bounds.max_x + ox,
                bounds.max_y + oy,
                bounds.max_z + oz,
            ),
        )

    def mapped(
        self,
        map_point: Callable[[Point3], Point3],
        bounds: Bounds | None = None,
    ) -> "Mesh":
        """Return a lazy mapped mesh, optionally carrying proven exact bounds.

        Supplying bounds is deliberately explicit: callers may only do so when
        they can derive the exact transformed AABB without walking every
        triangle.  The common positive uniform-scale/translation transform has
        that property and avoids a second full scan of multi-million-triangle
        STL files during preparation.
        """

        return Mesh(MappedTriangles(self.triangles, map_point), bounds)


def load_stl(path: str | Path) -> Mesh:
    source = Path(path)
    size = source.stat().st_size
    if size < 84:
        raise MeshError("STL file is too small")
    with source.open("rb") as handle:
        header = handle.read(84)
    count = struct.unpack_from("<I", header, 80)[0]
    if 84 + count * 50 == size:
        return _load_binary_stl_path(source, count)
    return _load_ascii_stl(source.read_text(encoding="utf-8", errors="replace"))


def load_obj(path: str | Path) -> Mesh:
    return _load_obj(Path(path).read_text(encoding="utf-8", errors="replace"))


def load_step(path: str | Path) -> Mesh:
    from .step import step_to_mesh

    return step_to_mesh(path)


def load_mesh(path: str | Path) -> Mesh:
    suffix = Path(path).suffix.lower()
    if suffix == ".obj":
        return load_obj(path)
    if suffix == ".stl":
        return load_stl(path)
    if suffix in {".stp", ".step"}:
        return load_step(path)
    raise MeshError(f"unsupported mesh format {suffix or '<none>'}; expected .stl, .obj, .stp, or .step")


def _looks_like_binary_stl(payload: bytes) -> bool:
    count = struct.unpack_from("<I", payload, 80)[0]
    return 84 + count * 50 == len(payload)


def _load_binary_stl(payload: bytes) -> Mesh:
    count = struct.unpack_from("<I", payload, 80)[0]
    triangles: list[Triangle] = []
    offset = 84
    for _ in range(count):
        if offset + 50 > len(payload):
            raise MeshError("binary STL ended mid-triangle")
        values = struct.unpack_from("<12fH", payload, offset)
        p1 = (values[3], values[4], values[5])
        p2 = (values[6], values[7], values[8])
        p3 = (values[9], values[10], values[11])
        if _triangle_area2(p1, p2, p3) > 1e-12:
            triangles.append((p1, p2, p3))
        offset += 50
    if not triangles:
        raise MeshError("binary STL contains no non-degenerate triangles")
    return Mesh(tuple(triangles))


def _load_binary_stl_path(path: Path, count: int) -> Mesh:
    mapping = _map_binary_stl(path)
    valid = array("I")
    min_x = min_y = min_z = float("inf")
    max_x = max_y = max_z = float("-inf")
    try:
        for record in range(count):
            values = struct.unpack_from("<9f", mapping, 84 + record * 50 + 12)
            p1 = (values[0], values[1], values[2])
            p2 = (values[3], values[4], values[5])
            p3 = (values[6], values[7], values[8])
            if _triangle_area2(p1, p2, p3) > 1e-12:
                for x, y, z in (p1, p2, p3):
                    if not (isfinite(x) and isfinite(y) and isfinite(z)):
                        raise MeshError(
                            "mesh contains non-finite coordinates in triangle "
                            f"{record}"
                        )
                    min_x = min(min_x, x)
                    min_y = min(min_y, y)
                    min_z = min(min_z, z)
                    max_x = max(max_x, x)
                    max_y = max(max_y, y)
                    max_z = max(max_z, z)
                valid.append(record)
    except Exception:
        mapping.close()
        raise
    if not valid:
        mapping.close()
        raise MeshError("binary STL contains no non-degenerate triangles")
    return Mesh(
        BinaryStlTriangles(mapping, valid),
        Bounds(min_x, min_y, min_z, max_x, max_y, max_z),
    )


def _map_binary_stl(path: Path) -> mmap.mmap:
    """Map an STL without retaining a Windows file lock.

    A file-backed Windows mapping keeps the source file undeletable until the
    owning mesh is released. Use an anonymous mapping there and copy the bytes
    through a short-lived file handle; POSIX keeps the original lazy file-backed
    behavior.
    """

    if os.name != "nt":
        with path.open("rb") as handle:
            return mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)

    size = path.stat().st_size
    mapping = mmap.mmap(-1, size, access=mmap.ACCESS_WRITE)
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                mapping.write(chunk)
        mapping.seek(0)
        return mapping
    except Exception:
        mapping.close()
        raise


def _load_ascii_stl(text: str) -> Mesh:
    vertices: list[Point3] = []
    triangles: list[Triangle] = []
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 4 and parts[0].lower() == "vertex":
            try:
                vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
            except ValueError as exc:
                raise MeshError(f"invalid ASCII STL vertex line: {line!r}") from exc
            if len(vertices) == 3:
                p1, p2, p3 = vertices
                if _triangle_area2(p1, p2, p3) > 1e-12:
                    triangles.append((p1, p2, p3))
                vertices = []
    if not triangles:
        raise MeshError("ASCII STL contains no triangles")
    return Mesh(tuple(triangles))


def _load_obj(text: str) -> Mesh:
    vertices: list[Point3] = []
    triangles: list[Triangle] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        kind = parts[0].lower()
        if kind == "v":
            if len(parts) < 4:
                raise MeshError(f"invalid OBJ vertex line: {raw_line!r}")
            try:
                vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
            except ValueError as exc:
                raise MeshError(f"invalid OBJ vertex line: {raw_line!r}") from exc
        elif kind == "f":
            if len(parts) < 4:
                raise MeshError(f"invalid OBJ face line: {raw_line!r}")
            face = [_obj_vertex(vertices, token, raw_line) for token in parts[1:]]
            for index in range(1, len(face) - 1):
                tri = (face[0], face[index], face[index + 1])
                if _triangle_area2(*tri) > 1e-12:
                    triangles.append(tri)
    if not triangles:
        raise MeshError("OBJ contains no non-degenerate faces")
    return Mesh(tuple(triangles))


def _obj_vertex(vertices: list[Point3], token: str, raw_line: str) -> Point3:
    index_text = token.split("/", 1)[0]
    if not index_text:
        raise MeshError(f"invalid OBJ face line: {raw_line!r}")
    try:
        index = int(index_text)
    except ValueError as exc:
        raise MeshError(f"invalid OBJ face line: {raw_line!r}") from exc
    if index == 0:
        raise MeshError(f"OBJ vertex indices are 1-based: {raw_line!r}")
    resolved = index - 1 if index > 0 else len(vertices) + index
    if resolved < 0 or resolved >= len(vertices):
        raise MeshError(f"OBJ face references missing vertex {index}: {raw_line!r}")
    return vertices[resolved]


def _triangle_area2(a: Point3, b: Point3, c: Point3) -> float:
    ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    ac = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    cross = (
        ab[1] * ac[2] - ab[2] * ac[1],
        ab[2] * ac[0] - ab[0] * ac[2],
        ab[0] * ac[1] - ab[1] * ac[0],
    )
    return cross[0] * cross[0] + cross[1] * cross[1] + cross[2] * cross[2]


def cube_mesh(size: float = 10.0) -> Mesh:
    s = size
    v = [
        (0.0, 0.0, 0.0),
        (s, 0.0, 0.0),
        (s, s, 0.0),
        (0.0, s, 0.0),
        (0.0, 0.0, s),
        (s, 0.0, s),
        (s, s, s),
        (0.0, s, s),
    ]
    faces = [
        (0, 2, 1), (0, 3, 2),
        (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4),
        (1, 2, 6), (1, 6, 5),
        (2, 3, 7), (2, 7, 6),
        (3, 0, 4), (3, 4, 7),
    ]
    return Mesh(tuple((v[a], v[b], v[c]) for a, b, c in faces))
