# Local resin mesh repair

The default `local-topology` operation fixes defects without reconstructing the
whole model. This follows the separation of basic repair, hole filling and
surface reconstruction described in [CHITUBOX's repair documentation](https://docs.chitubox.com/en-US/chitubox/latest/repair).
[Lychee's documentation](https://doc.mango3d.io/doc/resin-documentation/resin-layout/repair-tool/)
also describes local boundary/non-manifold repair and explicitly allows repair
to fail on complex defects. These are behavioral references, not claims that
Agent-Slicer reproduces their proprietary algorithms.

## Default behavior

- Keep healthy curved meshes at their original resolution, including meshes
  well above 500,000 triangles. STL import joins identical triangle vertices.
- Remove duplicate and zero-area triangles. Correct inconsistent winding and
  orient nested cavity shells using containment parity.
- Replace only small defective neighborhoods (at most 128 faces and a 0.1 mm
  bounding-box diagonal). Preserve all surrounding vertex coordinates.
- Fill closed boundary loops with at most six edges and a 0.5 mm diameter bound.
  New hole surfaces are bounded patches, not treated as existing source surfaces
  when measuring deviation.
- Measure both directions at replacement patches, including every local patch
  vertex and face center. Also sample the original surface. Default limits are
  0.01 mm p95 and 0.05 mm maximum deviation.
- Fail with `needs review` for larger or ambiguous defects. Never switch to
  smoothing, decimation or voxel reconstruction on failure.
- Reload the exported STL and check watertightness and winding before committing
  it. Source files and existing outputs are preserved on rejected preparation.

`measured-voxel` remains an explicit reconstruction option for callers that want
that different operation. High-detail presets now disable mesh simplification;
the existing measured simplification command remains available separately.

## Hollowing and drains

Voxel sampling creates only the inside cavity. A conservative grid allowance is
added to the requested wall offset. Manifold Boolean subtraction retains the
original exterior except where drain cylinders intersect it. Exterior samples
outside the drain regions must agree within floating-point tolerance, regardless
of legacy coarse deviation settings. A drain may legitimately remove an extremal
point at the bottom; subtraction must never expand the outside bounds.

Drain candidates are ranked near the lowest cavity region. Downward rays through
the center and rim locate the local underside. Candidates with excessive depth
are rejected; the selected pair must be separated and each must independently
connect the cavity to the outside in both grid and actual mesh checks. This avoids
drilling through an unrelated base, leg or prop under an elevated cavity.
Automatic placement is bounded to 256 candidates and downward exits. Failure
means a different size, manual drain placement or a solid print may be needed.

Preparation report/cache schema v3 includes the repair method and rejects old v2
artifacts. Hollow reports v2 distinguish preserved exterior geometry from the
internal voxel cavity. Reports keep the measured effects and limitations.

## Verification and limits

Native Blender regressions cover a detailed curved mesh, flipped winding, nested
cavity orientation, small missing faces and rejection without output replacement.
Hollowing regressions cover curved exterior preservation, independent drainage,
and an elevated body whose base must remain untouched.

Topology checks do not certify arbitrary global self-intersections, print
strength or a successful physical print. A passed repair is a geometric repair
result, not a print-ready claim. Supports and saved-slice validation remain later
steps.
