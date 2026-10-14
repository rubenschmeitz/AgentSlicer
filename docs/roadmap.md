# Native replacement roadmap

## M0 — contracts and vertical slice

Project/preset schemas, capability planning, native STL inspection/scaling/shelf
packing/combination, Resin-Slicer adapter, basic CTB validation, reports, and E2E
tests.

## M1 — Blender transitional adapter (implemented in 0.3)

Typed request/result protocol for measured repair and bounded resin orientation.
Adapter output conforms to Agent-Slicer's reports and never exposes Blender data
as a public project type.

## M2 — native MeshIR

Rust core with Python bindings: indexed topology, half-edge adjacency, connected
components, BVH, robust predicates, safe repair, transforms, deterministic I/O,
and native 3MF through an embedded standards library.

## M3 — native SolidIR (voxel production subset implemented in 0.3)

The current subset provides component-aware filled-voxel distance-field
hollowing, one retained cavity, two drain paths, exterior flood validation,
bounded topology disambiguation, adaptive voxel refinement, and support-aware
packing. Remaining work is a persistent SolidIR, exact Booleans,
multiple-cavity graphs, wall-thickness maps, and semantic drain/vent feature
editing.

## M4 — native resin engine

Robust plane intersection, contours, rasterization, island/suction analysis,
orientation optimization, graph-based support routing, CTB/GOO writers, parser
round trips, and independent validation. The current Python backend now has
content-addressed exact/manual-prefix support-plan reuse, disk-spooled output,
active-edge scan conversion, lazy row-span layers, span-native support analysis,
and streaming pre-encode plus decoded CTB v5/GOO v3 structural audits. It also
retains bounded support-footprint issue records for future native feedback and
requires exact pre/post parity before publishing an output. Remaining
performance work is a compiled/vectorized contour-intersection kernel and a
reproducible cross-slicer benchmark corpus. Remaining mature-slicer features
include native resin-trap/suction parity, grayscale/blur
anti-aliasing, dimensional compensation, multiple independently drained
cavities, selectable hollow infill, richer support editing/presets, and
slice-by-model/height/area export.

## M5 — FDM adapters and presets

An isolated OrcaSlicer CLI adapter now executes existing FDM algorithms and
installed vendor profiles, with measured solid preparation, FDM orientation,
tree supports, repeated copies, sliced 3MF, saved-settings/G-code validation,
and revalidation. Real P2S 0.4 mm builds cover PLA/PETG HF/ABS and four quality
profiles. Further printer verification, multi-material/plate support, stable
ToolpathIR, and additional engine adapters remain future work. See [FDM](fdm.md).

## M6 — native FDM

Walls, top/bottom skin, infill, bridges, supports, seams, travel, multi-tool,
cooling, acceleration, and printer-dialect G-code.

## M7 — agentic model builder (bounded intake implemented)

The first bounded layer provides strict parametric primitives, editable
CadQuery/STEP artifacts, sketches, cloud organic-generation adapters, resumable
tasks, approval gates, and hashed adoption. Remaining work includes general
sketch constraints, richer BREP features and assemblies, semantic feature
graphs, manufacturing-aware constraints, local/remote open-model adapters,
multi-view labeling, richer previews, and reversible operation history.
