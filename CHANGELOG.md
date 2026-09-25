# Changelog

## Unreleased

- Retain and validate Resin-Slicer's native pre-encode layer audit, including
  detached connected components, bounded support-footprint issue records, and
  leading/internal/trailing empty layers alongside the independent post-encode
  UVtools evidence.
- Add source-backed Mars 3 Pro and Bambu Lab P2S 0.4 mm setting
  recommendations, including pinned official P2S machine/process/PLA/PETG/ABS
  profile identities, compatibility checks, and explicit calibration status.
- Add unbounded external preset catalogs, atomic validated preset addition,
  SHA-256 catalog validation, and fail-closed duplicate-id handling.
- Keep P2S slicing fail-closed by requesting FDM rather than resin capabilities;
  the Bambu Studio production adapter remains unimplemented.
- Add resumable, approval-gated generation projects for parametric CadQuery
  parts and Meshy/Tripo text/image-to-model tasks.
- Add strict millimetre CAD specifications, editable Python and STEP output,
  three SVG projections, measured STL cleanup, and hashed handoff into the
  existing manufacturing pipeline.
- Require explicit credit confirmation, keep provider credentials and signed
  URLs out of project receipts, and require review before generated geometry is
  adopted.
- Prefer an isolated Linux-native UVtools installation in WSL while retaining
  explicit CLI/environment overrides and the existing Windows fallback.
- Keep large build transactions and UVtools layer extraction on the native
  Linux filesystem by default, with atomic cross-filesystem publication.
- Record UVtools executable provenance and bound WSL analysis to two workers by
  default after measured four-worker memory saturation on a Mars 3 Pro CTB.
- Add component-aware quadric mesh reduction between preparation and plate
  packing, with protected small bodies, conservative per-component fallback,
  export/reload topology gates, measured surface/volume limits, and exact cache
  reuse. Unsafe candidates retain the original prepared mesh.
- Require Resin-Slicer's balanced automatic-support receipt and retain its
  unresolved count, while making the mandatory UVtools decoded-layer audit the
  fail-closed production authority after validation-derived contacts are added.
- Configure proactive resin-tree merging with bounded tips per root,
  load-thickened trunks, tapered pedestals, and collision-checked branch paths;
  retain independent UVtools analysis of the encoded result.
- Raise the resin backend's bounded default timeout from 30 minutes to three
  hours, with an explicit environment override, after a measured production
  tree slice required 49 minutes.
- Add a persistent content-addressed Resin-Slicer support-plan cache in the
  native work root, including exact-plan and append-only manual-contact reuse.
- Require checksummed schema validation, atomic cache writes, bounded pruning,
  and receipt-gated cache status and phase timings; corrupt entries recompute
  instead of being trusted.
- Preserve byte-identical CTB output in the measured 373,722-triangle benchmark
  while reducing the warm-repeat wall-time median by 58.6% and peak-RSS median
  by 45.8%; cold-run timings remained load-sensitive and are not claimed as an
  improvement.

## 0.3.0 - 2026-08-29

- Added transactional measured repair and bounded resin orientation.
- Added filled-voxel hollowing with a configured wall, one retained cavity,
  two independently verified bottom drains, exterior flood-fill validation,
  STL round-trip manifold validation, and bounded outer-surface change.
- Added component-aware hollowing that preserves smaller repaired bodies,
  bounded topology disambiguation, and adaptive voxel refinement when the
  requested pitch would exceed an outer-detail limit.
- Added support-footprint-aware packing and forwarded Resin-Slicer's primary
  contacts, collision-aware routing, enforcers, braces, and chamfered raft controls.
- Added iterative support-density refinement followed by fail-closed UVtools
  island, overhang, resin-trap, suction-cup, bounds, height, and empty-layer checks.
- Added UVtools-derived support contacts, strict requested-contact accounting,
  internal enforcer routing, branch-to-existing-support fallback, and bounded
  final-density feedback retries that stop when no new contact can be added.
- Corrected UVtools-to-build-plate Y-axis conversion and added actual-pixel
  sampling for broad overhang feedback regions.
- Added fail-closed preparation-cache reuse and rejected-build resume, guarded
  by source/settings/output/topology/drain checks, exact plate identity, and a
  fresh analysis of the retained CTB.
- Distinguished strict user contacts from detector-sampled candidate contacts;
  partial safe routing is recorded and accepted only if the next CTB audit has
  no actionable issue.
- Added deep validated project overrides and persistent rejected/failure evidence.
- Added NumPy, SciPy, scikit-image, and trimesh as isolated package dependencies.

## 0.2.0 - 2026-08-28

- Added bounded Blender-backed approximate hollowing, scaling, and bottom drains.
- Added front/bottom preview rendering for geometry trials.
- Added experimental dense miniature-support purpose preset.
- Added compact UVtools property and issue analysis through the CLI.
- Fixed degenerate triangles introduced only during float32 STL quantization.
- Tested two real miniatures and retained fail-closed quality findings.

## 0.1.0 - 2026-08-28

- Added versioned project, plan, resolved-config, validation, and build schemas.
- Added native STL inspection, scaling, deterministic packing, cleanup, and plate export.
- Added Resin-Slicer discovery and CTB/GOO slicing adapter.
- Added machine/material/purpose/quality presets, including Mars 3 Pro.
- Added CTB/GOO basic validators and transactional output commits.
- Added unit, fail-closed, and real backend E2E tests.
