from __future__ import annotations

import math
import struct
from pathlib import Path


def write_cube(path: Path, size: float = 10.0) -> None:
    p000 = (0.0, 0.0, 0.0)
    p100 = (size, 0.0, 0.0)
    p110 = (size, size, 0.0)
    p010 = (0.0, size, 0.0)
    p001 = (0.0, 0.0, size)
    p101 = (size, 0.0, size)
    p111 = (size, size, size)
    p011 = (0.0, size, size)
    triangles = [
        (p000, p110, p100), (p000, p010, p110),
        (p001, p101, p111), (p001, p111, p011),
        (p000, p100, p101), (p000, p101, p001),
        (p010, p011, p111), (p010, p111, p110),
        (p000, p001, p011), (p000, p011, p010),
        (p100, p110, p111), (p100, p111, p101),
    ]
    with path.open("wb") as handle:
        handle.write(b"Agent-Slicer cube fixture".ljust(80, b"\0"))
        handle.write(struct.pack("<I", len(triangles)))
        for a, b, c in triangles:
            normal = _normal(a, b, c)
            handle.write(struct.pack("<12fH", *normal, *a, *b, *c, 0))


def _normal(a, b, c):
    ab = tuple(b[i] - a[i] for i in range(3))
    ac = tuple(c[i] - a[i] for i in range(3))
    cross = (
        ab[1] * ac[2] - ab[2] * ac[1],
        ab[2] * ac[0] - ab[0] * ac[2],
        ab[0] * ac[1] - ab[1] * ac[0],
    )
    length = math.sqrt(sum(value * value for value in cross))
    return tuple(value / length for value in cross)

