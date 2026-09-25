# Implementation status

## Verified in 0.3.0

| Area | Status | Evidence |
| --- | --- | --- |
| Project/preset resolution | Implemented | Unit tests and resolved JSON artifact |
| External printer catalogs | Implemented | Unlimited directory-backed entries, semantic validation, atomic add, SHA-256 audit, and collision rejection |
| Evidence-backed setting recommendations | Implemented | Mars 3 Pro supplied resin profiles plus pinned official P2S machine/process/PLA/PETG/ABS values; every result remains calibration-required |
| STL inspection, scaling, and cleanup | Implemented | Binary/ASCII and float32-degeneracy tests |
| Local mesh repair | Implemented via Blender adapter | Preserves detailed meshes; bounded local patches, winding fixes, sampled deviation and STL reload checks; arbitrary self-intersections are not certified |
| Measured mesh simplification | Implemented natively | Component-aware quadric edge collapse, protected small bodies, export/reload topology checks, and sampled bidirectional point-to-triangle deviation evidence |
| Resin orientation | Implemented via Blender adapter | Deterministic bounded candidate scores in each model report |
| Connected hollowing | Implemented natively | Only the cavity is voxelized; Boolean subtraction preserves the original exterior; secondary bodies remain solid |
| Drain generation | Implemented natively | Two short local-underside drains; independent grid and mesh connectivity checks; unsuitable placements fail for review |
| Sealed-cavity validation | Implemented natively | Exterior flood fill must reach every retained cavity voxel |
| Hollowed STL integrity | Implemented natively | Bounded topology disambiguation, adaptive voxel refinement, export/reload watertight and winding checks |
| Support-aware plate packing | Implemented | Unit test plus configured support-footprint reservation |
| Resin automatic supports | Delegated, parameterized and receipt-gated | Size-aware contacts feed proactive collision-checked trees with load-thickened trunks and tapered pedestals; routing, enforcers, braces, and pad flags are preserved. Balanced detected/supported/excluded/unrouted accounting plus roots/branches/load accounting is required before UVtools analysis |
| Support refinement | Implemented | Ordered denser variants plus two bounded feedback passes add UVtools-derived candidates; requested/routed counts remain explicit, saved-CTB validation is authoritative, and unchanged retries stop |
| Support-plan reuse | Implemented | Source/config-addressed, checksummed JSON cache; exact and manual-prefix reuse; receipt-gated phase timings; corrupt entries fail back to full planning |
| CTB slicing | Delegated, verified | Real E2E and encrypted marker check |
| GOO slicing | Delegated, verified | Real E2E and version-prefix check |
| Mars 3 Pro CTB profile | Software-path verified | 4098 x 2560 profile and UVtools property contract |
| UVtools production analysis | Implemented | Seven isolated, serial issue scans and machine/material property checks; WSL prefers the verified native ELF backend, keeps intermediates on ext4, records executable/runtime/GC provenance, and uses a measured memory-conserving .NET policy |
| Failure evidence | Implemented | Failed preparation reports and rejected CTBs are retained |
| Safe feedback resume | Implemented | Exact preparation/plate hashes and geometry contracts gate cache reuse; the rejected CTB is re-analyzed before refinement |
| Parametric part generation | Implemented, bounded | Strict mm schema, editable CadQuery source, STEP/STL, SVG projections, hashes, and measured tessellation cleanup |
| Meshy/Tripo generation intake | Implemented, credential pending | Current Meshy and Tripo v3 task submission/poll/download adapters with explicit credit and approval gates; mocked contract tests pass |

## Remaining limits

| Capability | Current behavior |
| --- | --- |
| Surface-distance evidence | Repair and hollowing use sampled one-sided vertex-neighbour diagnostics; topology checks are exact for the resulting mesh |
| Simplification distance evidence | Deterministic sampled bidirectional point-to-triangle diagnostics, not an exact Hausdorff proof; unsafe results retain the original prepared mesh |
| Orientation search | Bounded deterministic heuristic, not a global optimizer |
| Multiple cavities | Only the largest connected cavity is hollowed; smaller candidates stay solid |
| Semantic foot recognition | Drains target the lowest connected cavity band and are reported for review |
| Manual support painting | Available in Resin-Slicer's GUI; Agent-Slicer uses transient UVtools-derived contacts but does not yet persist painted regions in its stable project schema |
| Anti-aliasing and dimensional compensation | Binary masks are emitted; CHITUBOX-style grayscale/blur AA and inner/outer/bottom tolerance compensation are not implemented |
| Advanced hollowing/infill | Production supports one validated connected cavity and two drains; selectable lattice infill and independently drained multiple cavities are not implemented |
| Advanced export modes | Slicing by model, height, or cross-sectional area and multi-platform project export are not implemented |
| Native resin writer | Resin-Slicer remains the slicing/output adapter |
| Physical Mars 3 Pro acceptance | Not tested; no printer communication is implemented |
| FDM slicing | P2S profiles and FDM capability planning are implemented; no Bambu Studio/production FDM adapter is installed, so slicing remains blocked |
| General-purpose CAD | The current schema supports additive/subtractive boxes and cylinders plus a final all-edge fillet or chamfer; complex sketches, constraints, threads, lofts, assemblies, and drawings are not implemented |
| Cloud generation quality | Provider output remains probabilistic and untrusted; no live paid task was run because no API key is configured |

Passing production validation establishes a specific software evidence chain:
geometry contracts passed, the CTB parser recognized the configured machine and
material properties, and the configured UVtools detectors returned no retained
issues. It does not establish safe exposure settings for an untested resin,
firmware acceptance, successful washing/curing, or a successful physical print.

GOO output embeds its creation time. Repeated runs with the same input therefore
have different whole-file SHA-256 values even when only the timestamp bytes
differ. CTB was byte-identical across the current smoke runs, but this is an
observed backend result rather than a guarantee for every future version.
