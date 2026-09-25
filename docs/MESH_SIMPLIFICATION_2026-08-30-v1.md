# Measured mesh simplification for Agent-Slicer

Date: 2026-08-30  
Agent-Slicer base commit: `fe68f97f4ceee33d2256792bfbf2a77361c9d4cb`

The checkout already contained uncommitted production-workflow changes. They
were preserved. No CTB was sent to a printer and no firmware was changed.

## Outcome

Agent-Slicer now offers a bounded mesh-reduction stage after measured resin
preparation and before plate packing. It is enabled by the
`miniature-display-high-detail` purpose for prepared meshes above 500,000
triangles.

The implementation uses quadric edge collapse, allocates the reduction budget
per connected body, and protects bodies below 5,000 triangles. A component
backs off to progressively more conservative targets until its watertightness,
winding, body count, Euler number, and degenerate-triangle checks match the
source. The exported combined STL is then accepted only when all of these
whole-model checks also pass:

- export/reload watertightness and winding consistency;
- connected-body count and Euler number preservation;
- no degenerate triangles;
- at most 0.035 mm sampled p95 and 0.10 mm sampled maximum surface deviation;
- at most 0.5% volume change and 0.10 mm bounds change.

If no candidate passes, the prepared source STL is retained. The source is
never modified. The output and JSON report are content-addressed and reused
only when the source hash, every simplification setting, and output hash match.

## How established slicers handle this

- [PrusaSlicer](https://help.prusa3d.com/article/simplify-mesh_238941) exposes
  detail-level and triangle-removal controls because dense scans and sculpted
  models can make slicing slow.
- [OrcaSlicer](https://github.com/OrcaSlicer/OrcaSlicer/wiki/prepare_stl_transformation)
  likewise exposes detail presets and a decimation ratio, with visual review
  because aggressive reduction can remove detail.
- [Bambu Studio](https://github.com/bambulab/BambuStudio/blob/master/src/slic3r/GUI/3DScene.cpp)
  uses quadric edge collapse with triangle-count and maximum-error controls and
  validates the result against expanded source bounds.
- The isolated Python implementation uses
  [fast-simplification](https://github.com/pyvista/fast-simplification), a fast
  quadric-error implementation. Agent-Slicer's additional topology, volume,
  surface-distance, audit, and fallback gates are specific to this unattended
  workflow.

The slicer pattern was therefore adopted, but a plain global ratio was not:
unattended resin preparation needs stronger evidence than an interactive
wireframe preview.

## Real miniature benchmark

Input:

`artifacts/prepared/bonsai-tree-3d-model-31497a431733.stl`

The prepared bonsai contains 1,663,454 triangles and 57 watertight connected
bodies. Its binary STL is 83,172,784 bytes.

| Strategy | Output triangles | Reduction | Wall time | Peak RSS | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| Global QEM with measured fallback | 1,247,578 | 25.0% | 1:45.88 | 3,304,100 KiB | Passed only after two aggressive candidates failed topology |
| Component-aware QEM, integrated | 720,160 | 56.7% | 1:26.05 | 2,582,572 KiB | Passed every gate; transient component memory released before distance indexing |

The component-aware output is 36,008,084 bytes. Its accepted measurements are:

- body count: 57 -> 57;
- Euler number: -254 -> -254;
- watertight and winding-consistent: yes;
- sampled p95 deviation: 0.00287 mm;
- sampled maximum deviation: 0.03047 mm;
- volume change: 0.00977%;
- maximum bounds change: 0.000317 mm;
- output SHA-256:
  `94fa27970237b99cb721d70dfbee36b4a72e63aa93899a338444b375178f143c`.

Compared with the original global attempt, this removes 527,418 additional
triangles while reducing measured peak RSS by 21.8%. The one-time validation
cost is cached; later builds run with `--reuse-prepared` reuse the audited
result when the prepared mesh and every setting still match exactly.

Evidence is on WSL's native Linux filesystem:

- `/home/rschmeitz/development-tools/agent-slicer-work/simplification-benchmark-2026-08-30/bonsai-integrated-memory.stl`
- `/home/rschmeitz/development-tools/agent-slicer-work/simplification-benchmark-2026-08-30/bonsai-integrated-memory-report.json`

## Configuration

The high-detail miniature defaults are:

```json
{
  "enabled": true,
  "minimum_input_triangles": 500000,
  "target_triangles": 350000,
  "protected_component_triangles": 5000,
  "max_p95_deviation_mm": 0.035,
  "max_deviation_mm": 0.10,
  "max_volume_change_ratio": 0.005,
  "sample_points": 20000,
  "aggression": 5
}
```

These settings are part of the resolved project configuration and cache key.
The requested target is intentionally a goal, not a promise: preserving a
complex model's topology may require more triangles.

## Limitations

- Sampled bidirectional point-to-triangle deviation is stronger than a vertex
  nearest-neighbour check but is not an exact Hausdorff proof.
- Simplification reduces triangle processing and plate STL size; it does not
  reduce Mars 3 Pro pixel count, layer count, support complexity, CTB encryption
  cost, or UVtools detector cost directly.
- The first build must pay the reduction and validation cost. Cache reuse is
  where repeated slicing receives the largest benefit.
- The complete five-model Tentoonstelling CTB was not regenerated solely for
  this code change. The production CTB must still pass the existing Resin-Slicer
  and UVtools validation workflow after a future build.
