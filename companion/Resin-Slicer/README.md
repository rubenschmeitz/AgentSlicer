# Resin Slicer

Resin Slicer is an MSLA resin slicer with an Electron 3D viewer, deterministic Python slicing backend, automatic island support generation, and native `.goo` / `.ctb` export. Development and command-line workflows are verified on WSL/Linux; a separate portable build targets Windows users.

The project favors stability and explicit settings over silent guessing. Always confirm the machine profile, resin profile, and output format against the printer you intend to use before printing.

The bounded native CTB v5 / GOO v3 reader also supports a read-only single-layer
preview helper. It writes a PNG to standard output and does not extract a layer
stack or modify the sliced file:

```bash
python -m resin_slicer.layer_preview model.ctb --info-json
python -m resin_slicer.layer_preview model.ctb --layer 100 --scale 2 > layer.png
```

## WSL-native development installation

```bash
cd /home/rschmeitz/development/Resin-Slicer
source .venv/bin/activate
resin-slicer --version
```

Large spools, previews, STEP conversions, and support caches default to
`/home/rschmeitz/development-tools/resin-slicer-work`. The Linux Electron
frontend uses the isolated Node.js installation at
`/home/rschmeitz/development-tools/node/current`; run `npm start` after adding
that installation's `bin` directory to `PATH`. The full audited layout is in
Agent-Slicer's `docs/WSL_FULL_MIGRATION_2026-08-31-v1.md`.

## Shared Portable Build

For someone who just wants to run the app:

1. Extract `ResinSlicer-Windows-Portable.zip`.
2. Run `Run ResinSlicer.bat`, or open `ResinSlicer\ResinSlicer.exe`.
3. Keep the `ResinSlicer` folder together. Do not move only the `.exe` out of the folder.

The packaged build includes Electron and a minimal Python runtime, so users should not need Node.js, npm, or a separate Python install.

To create the shareable package from source:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\package_release.ps1
```

Outputs:

- `dist\ResinSlicer-Windows-Portable\`
- `dist\ResinSlicer-Windows-Portable.zip`

## GUI Workflow

1. Open one or more STL/OBJ/STP/STEP files, or drag mesh files into the 3D view.
2. Choose `GOO` or `CTB`.
3. For STEP/STP jobs, choose `Tessellated` or `B-rep sections` from `CAD slice mode` in the Job panel. Tessellated mode slices the converted triangle mesh. B-rep sections mode uses OpenCascade CAD sectioning for STEP/STP model exposure layers while retaining tessellation for display and support planning.
4. Adjust placement/orientation in the 3D viewer and `Placement` section. Multiple loaded meshes are arranged automatically on the build plate.
5. Generate a support preview.
6. Slice to the selected output path.

Layer rendering defaults to one worker. The renderer is Python/GIL-heavy and
each additional worker retains another layer plus contour/support state, so the
single-worker default remains safer on 16 GiB machines. Binary output layers
use lazy row spans and normally avoid a full-resolution byte buffer.

The main buttons stay visible by default. Deeper settings live in collapsible sections:

- `Placement`: rotation, translation, scale, centering.
- `Printer`: machine name, resolution, build volume, layer height.
- `Resin`: exposure, bottom layers, lift/retract distances and speeds, wait time, PWM, density.
- `Support Basics`: lift, overhang threshold, spacing, primary extra supports, post/tip basics.
- `Tip And Bed`: tip shape, tip length/angle, feet/raft/skate options.
- `Support Access`: collision clearance, base reach, shaft angle, enforcers,
  proactive tree merging, tips per root, load-bearing trunk diameter, and
  tapered pedestal height.
- `Bracing`: cross-support structure.

## Profiles

The GUI can import and export:

- Machine profiles
- Resin profiles
- Support settings

Machine profiles can also be imported directly from the UVTools GitHub printer list. Use `Printer` -> `UVTools GitHub`, search for the printer, then import it into the current machine settings.

Exports are JSON files using this slicer's schema. Resin import also includes best-effort Chitubox-style field mapping for common names such as `normalExposureTime`, `bottomExposureTime`, `bottomLayerCount`, `liftingDistance`, `liftingSpeed`, `retractSpeed`, and `lightOffDelay`.

Chitubox profile formats are not fully standardized, so imported values should be reviewed after import.

## Supports

Support generation is island driven:

- Unsupported regions are detected layer-by-layer from overhang/island analysis.
- Detection subtracts and labels sparse row spans directly instead of allocating
  dense dilation, unsupported, and visited masks for every analysis layer.
- Small islands receive a contact on a real component pixel. Larger islands use
  bounded farthest-point coverage plus axis/diagonal extrema so concave centers,
  thin ends, and perimeter corners are not silently lost to centroid placement.
- `Overhang deg` controls how aggressively regions are treated as unsupported.
- `Spacing mm` controls normal support density.
- `Primary extras` adds denser support candidates around each detected island's primary attachment area.
- `Enforcers` allow part-to-part supports for regions that cannot get a safe bed support.
- Nearby automatic and explicit contacts proactively merge into collision-checked,
  acyclic support trees. Thin contact tips feed diagonal branches, shared trunks
  thicken with accumulated tip load, and each root terminates in a tapered
  pedestal on the pad. Contacts outside the local merge radius retain an
  independent bed shaft instead of being forced into an unsafe tree.
- Explicit contacts that cannot reach the bed may also form a tapered
  part-to-part enforcer or a bounded two-segment dogleg through a newly created
  intermediate tree trunk.
- `Path clearance`, `Base reach`, and `Max shaft angle` control route safety.
- `Tree merge distance` bounds local branch reach, `Tips per tree` caps the load
  assigned to one root, `Max trunk diameter` caps load-driven shaft thickening,
  and `Pedestal height` controls the tapered foot. Use `--no-tree-branching` to
  disable tree merging explicitly.

The support preview shows posts, tips, bases, enforcers, and braces before slicing.
Full-plate previews retain an automatic-support audit receipt. Partial or
manual-only regeneration clears that completeness claim because component counts
from different analyses cannot be combined safely.

## File Format Notes

- `.goo`: native Elegoo GOO V3.0 writer with per-layer print settings and RLE compression.
- `.ctb`: native Chitu CTB v5 writer with CTB RLE, layer XOR coding, encrypted settings/signature sections, and preview/resin metadata records.

These are proprietary printer formats. The writer is intended to produce printer-compatible files, but machine firmware can be strict about resolution, build volume, metadata, and CTB/GOO dialects. Test with non-critical files first.

Both writers encode layers into a disk-backed spool and atomically replace the
destination only after the complete file has been synchronized. A failed layer
iterator or serialization step therefore preserves the previous output instead
of leaving a truncated printer file. CTB v5 metadata and GOO V3 global/per-layer
two-stage motion fields follow the field meanings used by UVtools' reference
decoder. Declared transition layers are serialized as a rounded software
exposure fade rather than an immediate bottom-to-normal jump. Stored print-time
metadata includes the same fixed startup allowance and layer-cycle inputs used
by UVtools, rounded down to the whole-second field supported by each format.

The exact pre-encode layer stream also receives a native structural audit. It
records wholly detached components, support-footprint deficit components with
bounded layer/area/bounds evidence, and leading/internal/trailing empty layers
in `--report-json` under `internal_layer_validation`. This is useful independent
evidence, but it is not yet a replacement for decoding and validating the saved
proprietary file with UVtools.

## Command Line

Basic slicing:

```powershell
resin-slicer --version
python -m resin_slicer.cli model.stl --format goo --output model.goo --profile generic-2k
python -m resin_slicer.cli model.stl --format ctb --output model.ctb --profile generic-2k
python -m resin_slicer.cli model.stl --format goo --output model.goo --profile elegoo-jupiter-2-16k
python -m resin_slicer.cli model.obj --format goo --output model.goo --profile generic-2k
```

Orientation and printer overrides:

```powershell
python -m resin_slicer.cli model.stl --rotate-x 30 --rotate-z 45 --scale 0.95 --format goo --output model.goo
python -m resin_slicer.cli model.stl --resolution 3840x2400 --volume 130x82x160 --layer-height 0.05 --format goo --output model.goo
```

Support and resin settings:

```powershell
python -m resin_slicer.cli model.stl --model-lift 5 --overhang-angle 45 --support-spacing 3 --primary-supports --tree-merge-distance 4 --tree-max-tips 12 --tree-trunk-radius 0.65 --pedestal-height 2 --format goo --output model.goo
python -m resin_slicer.cli model.stl --exposure 2.5 --bottom-exposure 35 --lift-distance 5 --lift-speed 65 --retract-speed 150 --format ctb --output model.ctb
```

Add one or more explicit support contacts in build-plate millimeters (useful for
machine-driven validation/refinement loops):

```powershell
python -m resin_slicer.cli model.stl --manual-support 70x44x12.5 --manual-support 72x45x18 --require-manual-supports --format ctb --output model.ctb
```

Explicit contacts are routed through the same collision, shaft-angle, base, and
bracing planner as automatically detected contacts. Repeating
`--manual-support` requests multiple contacts. `--require-manual-supports`
makes the CLI fail if any requested contact cannot be routed; the success line
reports requested and routed counts. Routing is deterministic and multi-pass:
contacts that initially lack a safe path are reconsidered after the other
contacts have created possible tree-support parents, so input order does not
silently determine whether a contact prints. If a detector contact lies between
equally near mesh faces and the sampled side-wall normal would drive its tip into
the part, the router retries a vertical tip while preserving the same collision,
reach, and shaft-angle constraints. If every single-shaft route is still blocked,
it can create a collision-checked intermediate trunk and branch around the body;
both segments keep the configured shaft-angle limit.

For programmatic workflows, add `--report-json slice-report.json`. The receipt
contains output-writer/index memory statistics, balanced automatic component and
route counts, the native pre-encode structural audit, and the exact routed and
unrouted manual contact coordinates. If
an obstructed detector point moves within the same connected unsupported patch,
the receipt also records the actual contact coordinate. Add
`--require-auto-supports` to fail before writing when any actionable automatic
component cannot be routed safely.
`slice_to_file(SliceJob(...))` exposes the same accounting through
`SliceResult.manual_supports_routed_points` and
`SliceResult.manual_supports_actual_contact_points` and
`SliceResult.manual_supports_unrouted_points`.

The automatic receipt covers components actionable under the configured
`--min-island-area` and support-analysis resolution. It is not a substitute for
checking the final encoded layer stack. A production orchestrator should decode
the output and separately gate final islands, overhangs, resin traps, suction
cups, empty layers, bounds, and print height (for example with UVtools).

Disable supports:

```powershell
python -m resin_slicer.cli model.stl --no-supports --format ctb --output model.ctb
```

Control slicing worker threads:

```powershell
python -m resin_slicer.cli model.stl --format goo --output model.goo --workers 8
$env:RESIN_SLICER_LAYER_WORKERS = "8"
```

Increase this only after measuring the job on the target machine. Binary STL
input is memory-mapped and transformed lazily; layer indexing stores compact
triangle intervals in 64-layer blocks instead of repeating every intersecting
triangle in every layer. Encoded output is disk-spooled rather than retained as
one growing in-memory byte array. Scan conversion uses an active-edge DDA and
keeps binary layers as merged row spans through preview and RLE encoding. See
[`docs/OPEN_SOURCE_SLICER_INTEGRATION_2026-08-31-v1.md`](docs/OPEN_SOURCE_SLICER_INTEGRATION_2026-08-31-v1.md)
for the source comparison, license boundary, parity tests, and benchmark.
Surface-normal planning samples at most 250,000 triangles by default. Set
`RESIN_SLICER_SURFACE_NORMAL_SAMPLES` (1,000 to 1,000,000) to tune that bounded
sample when needed.

Reuse deterministic automatic-support work across repeated slices with a
content-addressed cache:

```powershell
python -m resin_slicer.cli model.stl --format ctb --output model.ctb --primary-supports --support-plan-cache-dir C:\ResinSlicerCache --report-json model.slice-report.json
```

The cache identity includes the source STL SHA-256, support-planner code
fingerprint, transform, printer raster, layer height, and every support setting.
Exact matches skip automatic support analysis and raft projection. When a
refinement run only appends manual
contacts, a matching prefix can reuse the automatic plan while collision
checking and routing the new contacts. Cache entries use a strict checksummed
JSON schema, validate numeric bounds and graph references on load, are written
with atomic replacement, and fall back to a full plan if missing, stale, or
corrupt. They do not weaken the final encoded-layer validation requirement.

`--report-json` records `support_plan_cache.status` (`miss`, `hit-exact`,
`hit-prefix`, or `disabled`) plus separate preparation, support-planning,
writing, and total pipeline timings.

## Development

### Prerequisites

- **Node.js** (LTS). Install from https://nodejs.org or, on Windows:

  ```powershell
  winget install OpenJS.NodeJS.LTS
  ```

  After installing, sign out and back in (or reboot) so `node`/`npm` are on `PATH`.

- **Python 3.10 or newer** for the slicing backend. The Electron app auto-detects a
  suitable interpreter (via the `py` launcher, then `python`/`python3`), so you do
  not normally need to configure anything. If detection picks the wrong one, set the
  `PYTHON` environment variable to the interpreter you want and it takes priority.

Run tests:

```powershell
python -m unittest discover -s tests
```

Validate the frontend and build the production renderer:

```powershell
npm ci
npm run check
npm audit --audit-level=high
```

CI repeats the Python suite on Python 3.10, 3.12, and 3.13 and runs the frontend
syntax/build/security checks on Node 24.

Run the Electron app from source with Vite hot renderer updates:

```powershell
npm install
npm run dev
```

`npm install` runs a `postinstall` step (`tools/ensure-electron.js`) that guarantees the
Electron binary is unpacked, working around environments where Electron's own installer
downloads the binary but fails to extract it.

The double-click `Launch Resin Slicer.vbs` launcher also starts this development flow. It launches the Vite dev server on `http://127.0.0.1:5178` and then starts Electron with `VITE_DEV_SERVER_URL` pointed at that server.

Renderer/UI changes hot-update through Vite without closing the Electron window. Electron main-process and preload changes still require restarting the app.

Build and run the production-style renderer files:

```powershell
npm run build
npm start
```

In production mode Electron loads `dist/index.html`; keep `npm run build` in the packaging flow before launching `npm start`.

Build the portable app folder without the release zip:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build_electron_portable.ps1
.\dist\ResinSlicer\ResinSlicer.exe
```

Build the optional root launcher:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build_launcher.ps1
.\ResinSlicer.exe
```

## Safety Checklist

Before printing:

- Match resolution and build volume to the exact printer.
- Confirm resin exposure, bottom exposure, lift speeds, and retract speeds.
- Preview supports after every orientation/settings change.
- Inspect islands and primary attachment regions.
- Decode and validate the final CTB/GOO rather than accepting support-planner
  success as proof that the encoded layers are issue-free.
- Start with a small, non-critical model when testing a new printer/profile combination.
