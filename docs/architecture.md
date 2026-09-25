# Architecture

## Boundary

Agent-Slicer owns project state, schemas, presets, operation plans, provenance,
validation contracts, and public APIs. Blender, Resin-Slicer, UVtools,
PrusaSlicer, and CuraEngine are optional adapters. No external application's
data model is part of Agent-Slicer's public contract.

The inspection GUI is a read-only projection over existing project, plan,
build, and validation files. It does not invoke a geometry backend, compile a
plan, or expose an HTTP mutation route. Assets are served only after resolving
them beneath the selected project root and checking an explicit file-type
allow-list. This keeps the GUI replaceable and prevents it from becoming an
unaudited second execution path.

Audited CTB/GOO layer previews cross the backend boundary through a short-lived
Resin-Slicer subprocess in its own virtual environment. CTB v5 random access
decodes one encrypted RLE payload; GOO v3 falls back to its bounded streaming
reader. A one-process semaphore prevents rapid browser requests from decoding
multiple full-resolution layers concurrently.

## Layers

1. CLI, SDK, and future MCP/JSON-RPC interfaces.
2. Intent and preset resolver.
3. Immutable operation-plan compiler.
4. Capability registry and backend selection.
5. Geometry/process kernels, including the native voxel cavity validator.
6. Validators and evidence renderers.
7. Transactional artifact store.

Generation projects form a gated intake layer before `ProjectIR`. A strict
millimetre CAD specification can produce editable CadQuery source, STEP, STL,
and orthographic SVG sketches. Organic generation providers return untrusted
reference and candidate artifacts. Provider task IDs, model versions, credit
usage, local hashes, and approvals are persisted; credentials and expiring
download URLs are not. Only an explicitly selected and approved STL crosses
into the existing project contract, where normal repair and production
validation remain authoritative.

## Intermediate representations

The current MVP uses indexed STL streams and JSON projects. Planned stable IRs:

- `ProjectIR`: objects, instances, semantic features, plates, and operation graph.
- `MeshIR`: indexed mesh, topology, units, components, provenance, and regions.
- `SolidIR`: volumes, cavities, openings, walls, and Boolean feature history.
- `SliceIR`: planar contours and nested material regions.
- `ResinLayerIR`: rasters, exposure/motion, islands, and suction metrics.
- `ToolpathIR`: extrusion/travel/tool semantics for FDM and related processes.

## Backend rule

Backends advertise implemented and planned capabilities. Only implemented
capabilities satisfy a plan. Discovery of Blender or UVtools is not equivalent
to an implemented adapter.

## Transaction rule

Builds execute in a temporary transaction directory. On WSL this defaults to
the native Linux filesystem under `~/development-tools/agent-slicer-work`; on
other platforms it falls back to `artifacts/`. Files are copied atomically into
the stable artifact set only after a stage completes, including when the
transaction and project live on different filesystems. Source models are
copied and hash-checked before planning and execution.

UVtools discovery is explicitly configurable. `AGENT_SLICER_UVTOOLS` and the
CLI `--uvtools` option override discovery; WSL otherwise prefers the isolated
Linux installation at `~/development-tools/uvtools/current/UVtoolsCmd` before
considering `PATH` or the retained Windows fallback. Analysis receipts record
the resolved executable, its hash, core version, ELF/PE runtime type, requested
worker count, serial scan strategy, GC conservation setting, and native work
root. Memory-intensive builds and analyses share a per-user file lock. Nested
analysis inside a build is re-entrant, while a second agent waits before it
allocates geometry or decoded-layer buffers.

Production geometry is split intentionally: Blender repairs and orients an
intermediate solid; native NumPy/SciPy/trimesh code separates its watertight
bodies, voxelizes the largest body, derives one connected inner cavity from a
distance transform, drills two bottom paths, and proves exterior connectivity by
flood fill. Smaller bodies are preserved as repaired solids. Ambiguous voxel
surfaces receive a bounded topology-disambiguation pass, and outer-detail
failures may retry at two finer voxel pitches. The final STL is committed only
after export/reload manifold checks and configured surface-change limits pass.

High-detail prepared meshes then enter an optional component-aware quadric
edge-collapse stage before plate packing. Connected bodies below the configured
triangle threshold are copied unchanged. Larger bodies receive proportional
budgets and independently back off toward conservative targets until their
watertightness, winding, body count, and Euler number remain unchanged. The
combined STL is reloaded and retained only after whole-model topology, volume,
bounds, and sampled bidirectional surface-deviation checks pass. Otherwise the
prepared source is used unchanged. Reports and outputs are content-addressed so
the one-time reduction can be reused without repeating it on later builds.

Resin support planning has a separate content-addressed cache in the native
work root. Its identity covers the plate content, transform, printer raster,
layer height, and complete support configuration. Strict checksummed JSON stores
the support graph and compressed full-resolution projection masks. An exact hit
reuses the complete plan; a manual-contact prefix hit rebuilds collision
occupancy and extends the graph with the newly requested contacts. The backend
receipt exposes the cache decision and phase timings, which Agent-Slicer
validates before accepting the slicing stage. Encoded-output validation remains
independent and mandatory.

Deterministic plans and native plate geometry do not imply every adapter output
is byte-identical. For example, GOO embeds a creation timestamp. Build manifests
therefore preserve exact commands and hashes for both the selected executable
and produced artifacts.
