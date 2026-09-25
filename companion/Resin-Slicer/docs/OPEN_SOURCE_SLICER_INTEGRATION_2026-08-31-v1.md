# Open-source slicer integration audit v1

Date: 2026-08-31  
Primary project: Resin-Slicer  
Integration project: Agent-Slicer  
Resin-Slicer base commit: `b7438ddbd66c4f714bfc0031a1f1d3787dbf011f`

## Outcome

This change integrates the useful permissive scanline design and implements a
native sparse raster/validation layer around it. It does not copy or link GPL
or AGPL slicer code into this MIT project.

Implemented:

- active-edge scanline DDA with nonzero-winding union fill;
- lazy sparse row-span storage through rasterization, support drawing, preview,
  and RLE encoding;
- span-native overhang subtraction and connected-component labelling for
  support planning;
- a streaming pre-encode structural audit for detached connected components
  and leading/internal/trailing empty layers;
- a separate, tolerance-bounded support-footprint deficit detector with
  machine-readable layer, area, unsupported area, and pixel bounds records;
- validation and retention of that audit in Agent-Slicer's build manifest;
- dense-reference parity tests and a full Penumbra MIT notice.

The internal audit is deliberately supplementary. It sees the exact raster
stream passed to the CTB/GOO writer, but it does not decode the saved proprietary
file. UVtools remains the independent production gate until the native readers,
post-encode checks, resin-trap analysis, and parity corpus are complete.

## Source comparison and decisions

| Project | Useful capability | License finding | Decision |
|---|---|---|---|
| Penumbra `stage-slice-rasterizer` (`01a94c9`) | Scanline DDA and cropped/sparse layer intent | MIT | Adapted the active-edge concept; retained Resin-Slicer's nonzero-winding semantics and added full parity tests. |
| Penumbra `stage-support` (`d701d47` audit checkout) | GPU depth peeling, tip growth, zones, and truss supports | MIT | Design reference only. It is Vulkan-coupled and its own repository describes the stages as reference implementations, so importing it would add a large runtime without demonstrated production parity. |
| Open Resin Alliance `df-plugin-ctb` (`980e483`) | Concrete CTB encoder and RLE implementation | MIT | Kept as an interoperability oracle. Resin-Slicer already has CTB v5 encryption, paging, metadata, and atomic disk spooling; replacing it would introduce Rust/build complexity without closing a current gap. |
| Open Resin Alliance `df-plugin-elegoo` (`f36592e`) | Elegoo profiles and GOO ecosystem | MIT, with an unresolved notice for printer preview assets | Kept as profile/format reference. No preview assets were copied. |
| `mslicer` (`19abc4a`) | Rust/Rayon layer segmentation, active-edge scanline work, direct RLE | GPL | No code copied or linked. General performance ideas were independently implemented in Python and tested against the pre-change Resin-Slicer algorithm. |
| PrusaSlicer SLA | Mature hollowing, orientation, and support algorithms | AGPL | Architecture/reference only; no direct incorporation into the MIT codebase. |
| UVtools | Broad format parsing and issue detection | AGPL | Remains an isolated executable validator, not a linked library. Native parity must precede replacement. |
| OpenSLAice (`1f6ddd5`) and `uv3dp` (`9a09778`) | Exposure/format references | MIT | Retained as reference material; no component was superior to the current writers for this integration. |
| `resin-mesh-tools` (`3661ad5`) | Mesh hollowing helpers | MIT | Not imported. Agent-Slicer's current voxel hollowing has stricter two-drain exterior reachability checks. |
| SliceLab audit checkout (`a9927a1`) | Experimental resin slicing | README says MIT, but no repository license file was found | No code copied because provenance was not sufficiently explicit. |
| SoulCrafted Slicer (`5a40140`) | Emerging web resin slicer | AGPL; slicing CLI still incomplete in the audited snapshot | Reference only. |

## Implementation details

### Raster kernel

`LayerRaster` now starts with a row-to-span map instead of a
`width * height` byte array. Calling the compatibility `pixels` property still
returns the same mutable `bytearray`, but only materializes it when a caller
needs random access. CTB/GOO RLE, PNG preview generation, support capsules,
layer unions, and material estimation consume spans directly.

The active-edge table stores every contour edge once. Each scanline advances
its X crossing by a precomputed DDA step, sorts crossings with stable source
ordering, and applies the existing nonzero-winding rule. This retains correct
union fill for overlapping multi-body meshes; Penumbra's even-odd rule was not
copied.

### Support analysis

The former automatic-support path allocated a dilated mask, an unsupported
mask, and a visited mask. The replacement subtracts merged row spans and labels
4-connected horizontal runs with a compact union-find. Tests compare the exact
component pixel sets and bounds against the previous dense implementation.

This is particularly relevant to Agent-Slicer's miniature preset, which uses
2,000,000 support-analysis pixels and refinement passes of 4,000,000 and
6,000,000 pixels.

### Native structural audit

Every output layer is observed once before encoding. A complete current-layer
connected component is recorded as detached only if none of its pixels overlaps
the preceding layer. A second metric records a whole component when a
substantive part of its footprint lacks support from the preceding layer. The
default writer audit uses a two-pixel attachment tolerance, a 0.02 mm2 minimum
unsupported area converted to native pixels, and ignores bottom plus transition
layers. These parameters are written into the receipt. Up to 1,000 issue records
retain the exact layer, component area, unsupported area, and pixel bounds;
additional records are counted as truncated. Layer zero is treated as
build-plate attached. The audit also distinguishes leading, internal, and
trailing empty layers.

The JSON receipt field is `internal_layer_validation`, mode
`pre-encode-native-raster-v1`. Agent-Slicer validates the receipt's types,
counts, bounds, record limits, ordering, and internal consistency and stores it
next to the independent UVtools evidence. It is not currently a production
pass/fail gate.

## Benchmark

Measured with Python `tracemalloc`, one low-priority process, on the same WSL
host while unrelated workloads remained untouched. These are Python allocation
peaks rather than whole-process RSS and should be treated as directional.

| Case | Previous dense path | New sparse path | Result |
|---|---:|---:|---:|
| Mars 3 Pro raster, 120 deterministic rectangles | 1.286027 s; 11,669,095 B peak | 0.427810 s; 938,360 B peak | Identical 7,355,854 lit pixels; about 3.0x faster and 12.4x lower Python allocation peak. |
| 2,000 x 1,000 support-component case | 0.063085 s; 8,005,448 B peak | 0.046256 s; 95,112 B peak | Identical result; about 1.36x faster and 84x lower Python allocation peak. |

A blank or span-filled Mars 3 Pro `4098 x 2560` layer now reports zero dense
payload bytes until compatibility random access is requested. Materializing
that property still allocates the expected 10,490,880-byte buffer.

## Verification

- Dense versus active-edge raster parity: deterministic rectangles and sloped
  convex polygons, including off-plate clipping and overlapping bodies.
- Dense versus span component parity: radii 0, 1, and 2 with exact pixel-set and
  bound comparison.
- Sparse RLE and PNG paths verified not to materialize the dense buffer.
- Streaming validation tests cover attached/detached regions, one-pixel
  tolerance, bounded footprint-deficit records, empty gaps, and invalid layer
  ordering.
- CTB and GOO smoke tests continue to exercise native writers.

### Mars 3 Pro native/UVtools smoke parity

A generated 6 mm cube with nine native supports was sliced using the supplied
Mars 3 Pro Anycubic Water-Washable Resin 2.0 profile at 0.05 mm:

- CTB SHA-256:
  `bbcd9e2d8a9d2a81cde193e383319ec72c3284861ca21665a3ea98907fc97067`;
- native result: 220 nonempty layers, no empty gap, no wholly detached
  component, one footprint-deficit component at zero-based layer 100;
- native issue: 29,744 component pixels, 27,967 unsupported footprint pixels,
  bounds X=1958..2139 and Y=1189..1370;
- UVtools 4.4.3 result on the exact same hash: one island at layer 100,
  29,744 px, with the same bounds; no empty-layer issue;
- UVtools parsed the file as encrypted CTB v5, 4098 x 2560, 220 layers,
  143.43 x 89.6 x 175 mm, and 35 um pixels.

This is exact parity for the one bounded smoke artifact, not evidence of broad
detector parity. The CTB is intentionally not a passing production candidate:
the detected base footprint needs more support.

- Resin-Slicer: 111/111 tests passed; maximum suite RSS was 39,452 KiB.
- Agent-Slicer: 78/78 tests passed; maximum suite RSS was 148,340 KiB.
- `git diff --check` and Python byte-compilation passed in both repositories.

## Remaining native gaps

The project is materially closer to an independent slicer, but it is not yet at
feature parity with mature commercial slicers. The important remaining work is:

1. native CTB/GOO readers and post-encode structural/checksum round trips;
2. native resin-trap, suction-cup, and trapped-volume detection with a parity
   corpus against UVtools;
3. production validation of automatic supports on detailed miniature corpora;
4. grayscale anti-aliasing, blur, dimensional compensation, and per-layer
   compensation controls;
5. compiled/vectorized contour intersection for high-triangle meshes;
6. multiple independently drained cavities, selectable hollow infill, and wall
   thickness mapping;
7. more mature orientation optimization and interactive support editing.

No CTB was sent to a printer, and no firmware or printer configuration was
changed as part of this work.
