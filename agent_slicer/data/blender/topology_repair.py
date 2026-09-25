"""Conservative local mesh repair. No remeshing, smoothing or decimation.

Small defective neighborhoods are replaced by bounded boundary patches. Larger
or ambiguous defects fail for review; voxel reconstruction is a separate opt-in.
"""
from __future__ import annotations

import math
import statistics

import bmesh
from mathutils import Vector
from mathutils.bvhtree import BVHTree


def _boundary_loops(bm):
    bm.edges.index_update()
    remaining = {edge for edge in bm.edges if edge.is_boundary}
    loops = []
    while remaining:
        seed = min(remaining, key=lambda e: e.index)
        pending, loop = [seed], set()
        while pending:
            edge = pending.pop()
            if edge not in remaining:
                continue
            remaining.remove(edge)
            loop.add(edge)
            for vertex in edge.verts:
                adjacent = [e for e in vertex.link_edges if e.is_boundary]
                if len(adjacent) != 2:
                    raise RuntimeError("local repair needs review: branching boundary")
                pending.extend(e for e in adjacent if e in remaining)
        loops.append(loop)
    return loops


def _diameter(vertices):
    points = [v.co for v in vertices]
    # Bounding-box diagonal is a conservative upper bound, in millimetres.
    return math.sqrt(sum((max(p[a] for p in points) - min(p[a] for p in points)) ** 2
                         for a in range(3)))


def _remove_loose(bm):
    edges = [e for e in bm.edges if not e.link_faces]
    if edges:
        bmesh.ops.delete(bm, geom=edges, context='EDGES')
    vertices = [v for v in bm.verts if not v.link_faces]
    if vertices:
        bmesh.ops.delete(bm, geom=vertices, context='VERTS')


def _inside(tree, point, epsilon):
    votes = []
    for direction in (Vector((1, .371, .529)), Vector((.217, 1, .613)), Vector((.719, .331, 1))):
        direction.normalize()
        origin, count = point.copy(), 0
        for _ in range(10000):
            hit, _, _, _ = tree.ray_cast(origin, direction)
            if hit is None:
                break
            count += 1
            origin = hit + direction * epsilon
        else:
            raise RuntimeError("local repair needs review: ambiguous nested shell")
        votes.append(count % 2 == 1)
    if len(set(votes)) != 1:
        raise RuntimeError("local repair needs review: intersecting or ambiguous shells")
    return votes[0]


def _orient_shells(bm):
    """Consistent normals with containment parity, retaining existing cavities."""
    bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
    unseen = set(bm.faces)
    shells = []
    while unseen:
        seed = min(unseen, key=lambda f: f.index)
        unseen.remove(seed)
        faces, pending = [seed], [seed]
        while pending:
            face = pending.pop()
            for edge in face.edges:
                for adjacent in edge.link_faces:
                    if adjacent in unseen:
                        unseen.remove(adjacent)
                        faces.append(adjacent)
                        pending.append(adjacent)
        shells.append(faces)
    if len(shells) == 1:
        volume = bm.calc_volume(signed=True)
        if abs(volume) <= 1e-12:
            raise RuntimeError("local repair needs review: zero-volume shell")
        if volume < 0:
            bmesh.ops.reverse_faces(bm, faces=shells[0])
        return {"shells": 1, "nested_cavity_shells": 0}
    if len(shells) > 512:
        raise RuntimeError("local repair needs review: more than 512 shells")
    records = []
    for faces in shells:
        vertices = sorted({v for f in faces for v in f.verts}, key=lambda v: v.index)
        indices = {v: i for i, v in enumerate(vertices)}
        points = [v.co.copy() for v in vertices]
        polygons = [tuple(indices[v] for v in f.verts) for f in faces]
        volume = sum(f.verts[0].co.dot(f.verts[i].co.cross(f.verts[i+1].co)) / 6
                     for f in faces for i in range(1, len(f.verts)-1))
        if abs(volume) <= 1e-12:
            raise RuntimeError("local repair needs review: zero-volume shell")
        records.append((faces, points[0], BVHTree.FromPolygons(points, polygons), volume))
    cavity_count = 0
    for index, (faces, point, _, volume) in enumerate(records):
        depth = sum(_inside(tree, point, 1e-6) for j, (_, _, tree, _) in enumerate(records) if j != index)
        inward = depth % 2 == 1
        cavity_count += int(inward)
        if (volume < 0) != inward:
            bmesh.ops.reverse_faces(bm, faces=faces)
    return {"shells": len(shells), "nested_cavity_shells": cavity_count}


def repair_topology(obj, *, mesh_stats, max_p95_deviation_mm=.01, max_deviation_mm=.05,
                    max_patch_diameter_mm=.1, max_hole_diameter_mm=.5, max_hole_edges=6):
    if any(not math.isfinite(v) or v <= 0 for v in (
        max_p95_deviation_mm, max_deviation_mm, max_patch_diameter_mm, max_hole_diameter_mm
    )):
        raise ValueError("repair tolerances must be finite and positive")
    before = mesh_stats(obj)
    bm = bmesh.new()
    report = {"status": "checking", "method": "local-topology-repair", "before": before,
              "remeshed": False, "decimated": False, "patches": [],
              "surface_preserved_outside_repairs": True}
    try:
        bm.from_mesh(obj.data)
        if not bm.faces or any(not math.isfinite(x) for v in bm.verts for x in v.co):
            raise RuntimeError("local repair requires non-empty finite geometry")
        bm.verts.index_update()
        bm.edges.index_update()
        bm.faces.index_update()
        # Duplicate triangles necessarily meet at suspect edges. Restrict the
        # dictionary to this neighborhood rather than copying millions of faces.
        candidates = {f for e in bm.edges if len(e.link_faces) > 2 for f in e.link_faces}
        seen, duplicates = set(), []
        for face in sorted(candidates, key=lambda f: f.index):
            key = tuple(sorted(v.index for v in face.verts))
            if key in seen:
                duplicates.append(face)
            seen.add(key)
        degenerate = [f for f in bm.faces if f.calc_area() <= 1e-12]
        bad_vertices = {v for e in bm.edges if len(e.link_faces) > 2 for v in e.verts}
        has_boundary = any(e.is_boundary for e in bm.edges)
        source_bvh = BVHTree.FromBMesh(bm) if bad_vertices or has_boundary or degenerate else None
        stride = max(1, math.ceil(len(bm.verts) / 20000))
        samples = [v.co.copy() for i, v in enumerate(bm.verts) if i % stride == 0]
        report["duplicate_faces_removed"] = len(duplicates)
        report["degenerate_faces_removed"] = len(degenerate)
        removal = list(set(duplicates + degenerate))
        if removal:
            bmesh.ops.delete(bm, geom=removal, context='FACES_ONLY')
        _remove_loose(bm)
        bad_vertices = {v for e in bm.edges if len(e.link_faces) > 2 for v in e.verts}
        bad_vertices.update(v for v in bm.verts if not v.is_manifold and not v.is_boundary)
        # Group connected defects. Replacing a bounded one-ring patch resolves
        # overshared edges without changing any surrounding vertex coordinates.
        regions = []
        while bad_vertices:
            seed = min(bad_vertices, key=lambda v: v.index)
            pending, cluster = [seed], set()
            while pending:
                vertex = pending.pop()
                if vertex not in bad_vertices:
                    continue
                bad_vertices.remove(vertex)
                cluster.add(vertex)
                pending.extend(e.other_vert(vertex) for e in vertex.link_edges)
            faces = {f for v in cluster for f in v.link_faces}
            vertices = {v for f in faces for v in f.verts}
            diameter = _diameter(vertices)
            if diameter > max_patch_diameter_mm or len(faces) > 128:
                raise RuntimeError(f"local repair needs review: defect patch {diameter:.6f} mm "
                                   f"exceeds {max_patch_diameter_mm:g} mm or 128 faces")
            regions.append((faces, vertices, diameter))
        patch_vertices = set()
        for faces, vertices, diameter in regions:
            samples.extend(v.co.copy() for v in vertices)
            samples.extend(f.calc_center_median().copy() for f in faces)
            patch_vertices.update(vertices)
            report["patches"].append({"diameter_bound_mm": diameter, "faces_removed": len(faces)})
        if regions:
            bmesh.ops.delete(bm, geom=sorted({f for faces, _, _ in regions for f in faces}, key=lambda f: f.index), context='FACES_ONLY')
        _remove_loose(bm)
        added, replacement_faces = [], []
        report["hole_patches"] = []
        for loop in _boundary_loops(bm):
            vertices = {v for e in loop for v in e.verts}
            local_patch = bool(regions) and vertices.issubset(patch_vertices)
            limit = max_patch_diameter_mm if local_patch else max_hole_diameter_mm
            edge_limit = 128 if local_patch else max_hole_edges
            if len(loop) > edge_limit or _diameter(vertices) > limit:
                raise RuntimeError("local repair needs review: hole exceeds automatic patch limits")
            filled = bmesh.ops.holes_fill(bm, edges=sorted(loop, key=lambda e: e.index), sides=edge_limit)['faces']
            triangles = bmesh.ops.triangulate(bm, faces=filled)['faces']
            added.extend(triangles)
            if local_patch:
                replacement_faces.extend(triangles)
            else:
                report["hole_patches"].append({"boundary_edges": len(loop),
                                               "diameter_bound_mm": _diameter(vertices),
                                               "faces_added": len(triangles)})
        _remove_loose(bm)
        if any(not e.is_manifold for e in bm.edges) or any(not v.is_manifold for v in bm.verts):
            raise RuntimeError("local repair needs review: unresolved non-manifold topology")
        report["normals"] = _orient_shells(bm)
        bm.normal_update()
        report["faces_added"] = len(added)
        distances, reverse = [], []
        if source_bvh is not None:
            repaired_bvh = BVHTree.FromBMesh(bm)
            distances = [float(repaired_bvh.find_nearest(p)[3]) for p in samples]
            # Filling a missing face has no original surface to compare against.
            # Its size is bounded above; bidirectional deviation applies to
            # replacement patches and all originally present sampled surfaces.
            points = [f.calc_center_median() for f in replacement_faces]
            points.extend(v.co for f in replacement_faces for v in f.verts)
            reverse = [float(source_bvh.find_nearest(p)[3]) for p in points]
        values = distances + reverse
        ordered = sorted(values)
        deviation = {"method": "bidirectional-point-to-triangle-with-all-local-patch-samples",
                     "sample_count": len(values), "mean_mm": statistics.fmean(values) if values else 0.,
                     "p95_mm": ordered[max(0, math.ceil(.95*len(ordered))-1)] if ordered else 0.,
                     "max_mm": max(values, default=0.),
                     "limits_mm": {"p95": max_p95_deviation_mm, "max": max_deviation_mm}}
        deviation["new_hole_surfaces"] = "bounded by hole diameter/edge limits; no original surface exists"
        report["source_to_repaired_deviation"] = deviation
        if deviation['max_mm'] > max_deviation_mm or deviation['p95_mm'] > max_p95_deviation_mm:
            raise RuntimeError("local repair exceeds surface deviation limits")
        report["lossy"] = bool(regions or added or degenerate)
        bm.to_mesh(obj.data)
        obj.data.update()
        report["after"] = mesh_stats(obj)
        report["status"] = "passed"
        report["limitations"] = ["Topology and sampled surface deviation are verified; arbitrary global self-intersections are not certified."]
        return report
    finally:
        bm.free()
