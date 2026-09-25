# Production hardening report v1

Date: 2026-08-30  
Scope: Resin-Slicer plus the Agent-Slicer resin orchestration path  
Status: software production-hardened; not physically printer-certified

## Outcome

This pass closes several silent-failure and memory risks in the native slicer,
strengthens automatic support placement/routing, and makes the orchestrator
fail closed on both the slicer's routing receipt and UVtools' decoded-layer
analysis. It does **not** label an artifact printable merely because supports
were generated or a CTB parser opened it.

No printer was contacted and no firmware was changed. Existing dirty working
trees and prior audited/rejected artifacts were preserved.

## Reference behavior adopted from mature slicers

- Prusa's current SLA support documentation uses different contact strategies
  for small, medium, and large/thin islands. Resin-Slicer now uses a real
  component pixel for small islands and bounded farthest-point coverage with
  retained axis/diagonal extrema for broad, concave, and thin regions.
  Source: <https://help.prusa3d.com/article/tree-supports_1515?product=sl1>
- CuraEngine derives support in two-dimensional layer space from overhangs and
  the previous layer, with angle translated into a layer-dependent horizontal
  allowance. Resin-Slicer's layer analysis follows that same class of 2D
  morphological test. Source:
  <https://github.com/Ultimaker/CuraEngine/wiki/Generating-Areas>
- Cura's tracker records that center/tip sampling can miss sharp perimeter
  corners. Resin-Slicer therefore retains perimeter and diagonal extrema before
  bounded sampling. Source:
  <https://github.com/Ultimaker/CuraEngine/issues/2037>
- Bambu Studio exposes branch angle, taper/diameter, branch distance, height,
  and tip-density controls in its tree support implementation. Resin-Slicer now
  permits deterministic acyclic merging into accepted shafts and exposes an
  explicit `--no-tree-branching` override; it does not claim to reproduce
  Bambu's complete optimizer. Source:
  <https://github.com/bambulab/BambuStudio/blob/master/src/libslic3r/Support/TreeSupportCommon.hpp>
- UVtools recommends detect, repair, re-detect, and return to the slicer for
  genuine unsupported geometry. Agent-Slicer continues to treat the saved-file
  re-analysis as authoritative. Source:
  <https://github.com/sn4k3/UVtools/wiki/Recommended-workflow>
- CTB v5 field order, total/second-stage motion semantics, paging, resin string
  order, and signature placement were checked against UVtools' decoder/encoder.
  Source:
  <https://github.com/sn4k3/UVtools/blob/master/UVtools.Core/FileFormats/CTBEncryptedFile.cs>
- GOO V3 bottom/normal and two-stage fields, advance-mode selection, layer
  definitions, and software-transition semantics were checked against UVtools'
  current implementation. Source:
  <https://github.com/sn4k3/UVtools/blob/master/UVtools.Core/FileFormats/GooFile.cs>

## Resin-Slicer changes in this pass

### Final-output safety and memory

- CTB and GOO layers are encoded one at a time into a disk-backed spool.
- The destination is written through a sibling temporary file, flushed,
  synchronized, and atomically replaced. Iterator/order/count failures preserve
  any previous output and remove the temporary file.
- Slice receipts report the storage mode, total encoded bytes, and largest
  encoded layer.
- Triangle-to-layer indexing now stores exact intervals in 64-layer blocks
  instead of repeating every triangle in every intersected layer. Parallel and
  out-of-order layer access remains supported.
- Configuration, transforms, and mesh bounds reject NaN/infinite values;
  integer safety fields reject booleans/floats; used second motion stages require
  positive speeds.

### Supports

- Contacts are always selected from pixels actually belonging to the detected
  component, fixing concave islands whose mathematical centroid lies in empty
  space.
- Large islands use deterministic capped max-min coverage with retained axis
  and diagonal extrema.
- Automatic contacts can merge into a previously accepted safe shaft. The
  directed construction order keeps the resulting graph acyclic.
- Each plan records detected, supported, excluded, and unrouted components;
  attempted, routed, and unrouted candidates; and unrouted area.
- `--require-auto-supports` fails before output publication if any actionable
  automatic component remains unrouted.
- The JSON receipt exposes the same balanced accounting.
- Electron carries the receipt only for a complete full-plate preview. Partial
  and manual-only regeneration clear the completeness claim instead of
  combining incompatible counters.

### CTB and GOO correctness

- Resin type/name/machine strings now follow the CTB v5 serialized field order.
- Layer pointers and payload page numbers are calculated independently and are
  correct across a 4 GiB page boundary.
- The marker, encrypted signature, and trailer now use the reference ordering.
- Global and per-layer bottom/normal lift totals, optional second stages,
  retract stage, rest times, and stored print-time estimate follow UVtools'
  reference field semantics.
- GOO global motion fields now preserve distinct bottom/normal first stages and
  both second stages; advance mode explicitly selects the serialized layer
  definitions.
- Both formats serialize UVtools-compatible rounded software transition
  exposures between the last bottom layer and normal exposure. Their shared
  timing calculation includes those exposures, all configured motion/waits,
  and UVtools' 300-second startup allowance.

### Desktop/runtime hygiene

- Electron was upgraded from 33.x to 44.0.0, Vite to 8.2.2, and concurrently to
  10.0.5. The regenerated lockfile audits with zero known vulnerabilities as of
  this report.
- A GitHub Actions workflow now runs Python 3.10/3.12/3.13 tests plus Node 24
  syntax, production build, and high-severity audit gates.

## Agent-Slicer changes in this pass

- Resin jobs pass `--require-auto-supports` whenever automatic supports are
  enabled.
- The slicer receipt is mandatory. Agent-Slicer validates that component and
  candidate totals balance, all values are non-negative/finite, manual routed
  and unrouted coordinate counts agree with the request, and no automatic
  component is reported unrouted.
- UVtools still decodes the actual saved CTB and gates islands, overhangs,
  resin traps, suction cups, bounds, height, and empty layers independently.
  Slicer receipt success therefore cannot bypass final-layer validation.

## Verification

### Automated

- Resin-Slicer: `93` unit/integration tests passed.
- Agent-Slicer focused receipt/adapter tests: `16` passed plus `3` subtests.
- Agent-Slicer full suite: `72` passed plus `5` passed subtests; `50` upstream
  scikit-image/NumPy deprecation warnings, no failures.
- Electron: all JavaScript syntax checks passed; Vite 8.2.2 production build
  passed (`181.25 kB` main JavaScript before gzip); `npm audit` reported zero
  vulnerabilities.
- Native validator: UVtools Core 4.4.3, Linux x86-64 ELF executable at
  `/home/rschmeitz/development-tools/uvtools/current/UVtoolsCmd`.

### Bounded real-mesh interoperability trial

Input: the existing repaired/hollow/drained elderly-woman STL from the
Tentoonstelling workspace; no model was downloaded. The trial intentionally
used scale `0.08` to bound runtime while retaining a real 373,722-triangle mesh.

Common result:

- Mars 3 Pro raster: 4098 x 2560 at 0.05 mm
- Layers: 197
- Strict slicer receipt: 20 detected/20 supported/0 excluded/0 unrouted
  components; 2/2 routed candidates
- Compact index: 71,779 stored references versus 80,939 dense equivalents
- Material from decoded pixels: 8.27 mm3

CTB:

- Runtime: 12.95 s wall
- Peak RSS: 236,284 KiB (230.75 MiB)
- SHA-256:
  `09b56cb4d291390307f6914b5dad84642a6a1a262e628bfe4a317324787a4bf9`
- UVtools decoded CTB v5, the Mars 3 Pro dimensions, machine/material name,
  32/2.5 s exposures, five transition layers, and 5 mm lifts at 70/100 mm/min.

GOO:

- Runtime: 13.78 s wall
- Peak RSS: 236,260 KiB (230.72 MiB)
- SHA-256:
  `3f0e0bea0df800e8cb00fe62a1a4c1c6c7b6e6d6335b0a5aa3cf59c3f199c9ae`
- UVtools decoded all 197 GOO layers, advance mode, the distinct 70/100 mm/min
  bottom/normal speeds, and the same raster/volume. Mars 3 Pro production
  output should remain CTB, not GOO.

For both formats, the stored whole-second estimate is `2261` s and UVtools'
layer-derived estimate is `2261.26` s. The normalized UVtools issue lists are
identical, not merely equal in count.

### Important rejection result

UVtools found the same `123` final-layer findings in both encodings:

- 117 islands (largest 42 pixels; most are 1 pixel)
- 6 very small resin traps
- 0 overhangs, suction cups, empty layers, or touching-bounds issues

This trial is therefore **not a print-ready artifact** and remains under
`/tmp/resin-slicer-production-validation-20260830/`; it was not copied to the
user's production/output folders. The difference is expected and explicit:
the slicer receipt used its configured 0.08 mm2 actionable-island threshold,
while UVtools inspected the final full-resolution layers down to individual
pixels. Production presets use lower action thresholds and bounded UVtools
feedback passes, but still reject if retained actionable findings remain.

## Remaining limitations

- Support placement is a deterministic bounded approximation informed by
  mature slicers; it is not Prusa's complete medial-axis/Voronoi planner or
  Bambu Studio's complete tree optimizer.
- One-pixel raster artifacts can still be created by slicing detailed geometry.
  Final-file detection/repair or source/orientation/support changes remain
  necessary; automatic route accounting alone is insufficient.
- Resin-Slicer does not itself perform measured mesh repair, hollowing, drain
  validation, or sealed-cavity proof. Those production operations live in
  Agent-Slicer.
- Anti-aliasing is not implemented in the current writers.
- GOO's fixed 32-byte profile-name field truncates this resin's long display
  name, and UVtools does not expose it as the high-level material name.
- CTB/GOO parser interoperability is verified; physical Mars 3 Pro firmware
  acceptance and print success are not. No hardware test was performed.
- The installed native UVtools validator is Core 4.4.3. It is proven for this
  workflow but is not the newest upstream UVtools release.

## Reproduction commands

```bash
cd /mnt/c/Users/20160600/Documents/Development/Resin-Slicer
source .venv/bin/activate
python -m unittest discover -s tests -p 'test_*.py'

PATH=/home/rschmeitz/.cache/local-node/node-v24.18.0-linux-x64/bin:$PATH npm run check
PATH=/home/rschmeitz/.cache/local-node/node-v24.18.0-linux-x64/bin:$PATH npm audit --audit-level=high

/home/rschmeitz/development-tools/uvtools/current/UVtoolsCmd --core-version
/home/rschmeitz/development-tools/uvtools/current/UVtoolsCmd print-properties model.ctb
/home/rschmeitz/development-tools/uvtools/current/UVtoolsCmd --no-progress \
  print-issues model.ctb -i -o -r -s -t -p -e --sort-area

cd /mnt/c/Users/20160600/Documents/Development/agent-slicer
source .venv/bin/activate
python -m pytest -q -s
```

Repository baselines at the time of the pass (working trees also contain
preserved uncommitted work):

- Resin-Slicer HEAD: `b7438ddbd66c4f714bfc0031a1f1d3787dbf011f`
- Agent-Slicer HEAD: `fe68f97f4ceee33d2256792bfbf2a77361c9d4cb`
