# FDM printing with OrcaSlicer

Version 0.4 also supports **Bambu Studio CLI** for imported Bambu presets.
Set `AGENT_SLICER_BAMBU_STUDIO` to its executable (`bambu-studio.exe` on Windows).
Discovery prefers Bambu Studio for archived Bambu settings and OrcaSlicer for
bundled generic settings. `--fdm-slicer` selects an explicit backend. Imported
presets were verified with Bambu Studio 02.08.02.61.

## Reuse uploaded Bambu presets

The [work-PC collection](../presets/bambu-work-pc-20261014/README.md) contains the
saved selection, custom filaments, inherited values and source hashes. Set
`AGENT_SLICER_PRESET_PATH` before creating, building or revalidating a project.
Pair an imported quality with its source support purpose so miniature supports
remain enabled. The catalog is in the repository; copy it alongside a wheel
installation when using those personal presets.

```bash
export AGENT_SLICER_PRESET_PATH="$PWD/presets/bambu-work-pc-20261014"
export AGENT_SLICER_BAMBU_STUDIO=/absolute/path/to/bambu-studio
python - <<'PY'
import json, os
from pathlib import Path
from agent_slicer.project import init_project, add_models
from agent_slicer.pipeline import build_project
selected = json.loads((Path(os.environ['AGENT_SLICER_PRESET_PATH']) / 'provenance.json').read_text())['selected_ids']
project = init_project('projects/my-bambu-part', name='my-bambu-part', process='fdm',
    machine=selected['machine'], material=selected['materials'][0], quality=selected['quality'],
    purpose=selected['purpose'], output_format='gcode.3mf')
add_models(project, ['part.stl'])
print(build_project(project)['sliced_output'])
PY
```

Source vectors stay unchanged in the archive and are projected to the named
Standard extruder at runtime. Installed matching profiles supply the firmware
snippets excluded by the collection; their hashes and template identities are
retained. Self-contained reinforced nylon uses the explicitly recorded nylon
firmware template while keeping its saved polymer, cooling, temperatures and
flow values. Material settings are never substituted with generic defaults.

Saved output settings are checked individually against effective imported
values, including supports, cooling, flow and speeds. Legacy wall-order and
top-wall settings have explicit translations. Unknown active settings fail
validation. Receipts distinguish printing values from inactive compatibility,
UI, material-certification and AMS-drying metadata. Disabled pressure-advance
extensions remain archived and are not enabled on Bambu Studio; requesting an
enabled unsupported extension blocks the build.

A separately labeled stock 0.2 mm PLA supplement supports the imported 0.06 mm
miniature process. The 0.4 mm calibrations are not reassigned. Thuis remains
unavailable, and X1-compatible material does not establish printer ownership or
a verified X1 build. No physical print has been tested.

```bash
python -m tests.e2e_bambu_presets --slicer /absolute/path/to/bambu-studio \
  --output /new/evidence/imported-bambu
```

Non-extruding Bambu spiral Z-hop is accepted as travel outside the deposition
bounds audit. Extruding arcs remain blocked; model arc fitting is disabled.

Agent-Slicer executes OrcaSlicer's existing CLI engine for walls, infill,
bridges, supports, travel, cooling, and firmware-specific output. OrcaSlicer
remains a separately installed application; its source and vendor presets are
not copied into Agent-Slicer's MIT package. The implemented path was verified
with OrcaSlicer **2.4.2** and Blender on Linux without a display.

## Installation

Install [OrcaSlicer](https://github.com/OrcaSlicer/OrcaSlicer/releases) and Blender.
Configure the CLI executable, which must support `--help`, `--load-settings`,
`--load-filaments`, `--slice`, and `--export-3mf`:

```powershell
$env:AGENT_SLICER_ORCA_SLICER = 'C:\Program Files\OrcaSlicer\orca-slicer.exe'
$env:AGENT_SLICER_FDM_PROFILE_ROOT = 'C:\Program Files\OrcaSlicer\resources\profiles'
agent-slicer tools
```

On Linux, an extracted AppImage's `AppRun` is suitable. Extract it in its own
directory using `./OrcaSlicer.AppImage --appimage-extract`, then configure:

```bash
export AGENT_SLICER_ORCA_SLICER=/absolute/path/squashfs-root/AppRun
export AGENT_SLICER_FDM_PROFILE_ROOT=/absolute/path/squashfs-root/resources/profiles
agent-slicer tools
```

Install the host runtime libraries requested by OrcaSlicer's launcher. No Xvfb
was needed for the verified 2.4.2 CLI. Profile discovery checks directories near
the executable; `AGENT_SLICER_FDM_PROFILE_ROOT` makes the choice explicit for
wrappers, AppImages, and custom installations. CLI alternatives are
`--fdm-slicer` and `--fdm-profile-root` on `plan`, `build`, and `revalidate`.
No downloads occur automatically during a build.

## Create and build a project

```bash
agent-slicer project init fdm-part --directory projects/fdm-part \
  --machine bambu-lab-p2s-0.4 --material bambu-pla-basic-p2s \
  --purpose fdm-general --quality p2s-0.20-standard
agent-slicer project add projects/fdm-part part.stl
agent-slicer plan projects/fdm-part
agent-slicer build projects/fdm-part
agent-slicer validate projects/fdm-part/artifacts/fdm-part.gcode.3mf
agent-slicer revalidate projects/fdm-part
```

The CLI infers the FDM process and default `gcode.3mf` format from the machine.
Set `--target-height` when adding a model if its units or intended size are
ambiguous. Source files are copied and hashed; preparation never changes them.

Use `fdm-display-supported` for automatic Orca tree supports. `fdm-general`
keeps supports disabled. Geometry preparation uses the shared measured local
repair implementation with resin tilt, hollowing, and drains disabled. FDM
orientation scores six bed faces by elevated overhang area, bed contact area,
and height. It does not recognize cosmetic surfaces or strength requirements;
set the existing `purpose.geometry.orient` override to `false` to preserve a
chosen orientation. Target size is applied before orientation.

Each model's `quantity` creates that many placements. At most five total
placements use the exhaustive order/rotation search. Larger plates try bounded
deterministic shelf heuristics; a feasible arrangement may still require manual
changes. One plate is limited to 4096 copies. A build that does not fit fails.

## Profiles and settings

Bundled coverage is Bambu Lab P2S with a 0.4 mm nozzle, Bambu PLA Basic,
PETG HF, and ABS, and 0.08/0.12/0.20/0.24 mm quality presets.

Agent-Slicer resolves `upstream_profile.name` against the installed Orca
profile directory, recursively expands inheritance, pins the Standard extruder
variant, then applies the selected Agent-Slicer quality, material, and purpose
settings. Vendor start/end sequences remain intact. Installed profile files
and emitted configurations are hashed; installed vendor profiles may differ
from the older Bambu commit used for setting recommendations, so receipts
record the actual source bytes used in each build.

Missing, ambiguous, cyclic, incompatible, or unmapped profiles/settings block
the build. The installed profile must match the declared nozzle and rectangular
build volume, and speeds, accelerations, temperatures, and layer height must
respect machine limits. Support choices in the purpose supersede quality
defaults. Slicing uses isolated CLI data; it does not use the user's GUI session.

Other FDM printers can be added through external machine/material/quality and
purpose catalogs. They need matching installed `upstream_profile.name` values,
complete backend-neutral settings, compatible preset selections, and allowed
output formats. There is no generic printer fallback. Sliced `3mf` and
`gcode.3mf` are supported; ASCII `gcode` can be extracted for a machine whose
preset explicitly allows it. The bundled P2S permits sliced 3MF only.

## Validation and evidence

Every accepted sliced 3MF must contain exactly one G-code plate, saved settings,
slice statistics, and a matching G-code checksum. The saved printer, nozzle,
material, quality, supports, speeds, temperatures, cooling, and other selected
settings are compared with the emitted configuration. Validation streams the
G-code, interprets absolute/relative motion and extrusion, checks layer and
filament statistics, and checks deposited linear-move endpoints against the
machine volume. ZIP members, settings, XML, and G-code lines have size limits.
Mesh-only 3MF, multiple plates/filaments, arcs, malformed moves, and changed
settings are rejected.

Artifacts include `plate.stl`, prepared meshes and measured reports, the sliced
3MF, `validation.json`, `build-manifest.json`, and a human-readable build report.
Each `artifacts/fdm-*/slice/` directory retains expanded profile JSON, source
hashes, CLI version, executable/implementation hashes where discoverable, argv,
full stdout/stderr logs, native Orca log, resource metrics, and `receipt.json`.
Rejected FDM candidates remain there. Revalidation verifies unchanged geometry,
output, and configuration evidence without reslicing or calling UVtools.

`--reuse-prepared` accepts only matching source/settings/output and preparation
implementation hashes. `--skip-slice` can prepare geometry without OrcaSlicer.
The resin-specific `--resume-rejected` feedback loop is not an FDM option.
The default FDM slice timeout is three hours; set
`AGENT_SLICER_FDM_SLICE_TIMEOUT_SECONDS` to an integer from 60 to 86400.
Existing process-tree cancellation and optional RSS watchdogs also apply.

These are software checks, not a physical print test. Vendor purge,
calibration, travel, and end sequences are outside the deposition bounds audit.
Slicer warnings are retained in the receipt and report. Support appearance,
adhesion, functional orientation, filament calibration, and firmware acceptance
still need review. Single material/plate and linear ASCII G-code are the
verified scope. Builds never upload files or start a print.

## Reproduce real verification

```bash
python -m unittest tests.test_fdm -v
python -m tests.e2e_fdm --slicer /absolute/path/to/OrcaSlicer \
  --profile-root /absolute/path/resources/profiles --output /new/evidence/fdm
```

The real suite slices all four bundled layer-height profiles across PLA,
PETG HF, and ABS, verifies scaled repeated copies and preparation-cache reuse,
checks an overhang fixture produces actual support toolpaths, validates both
sliced 3MF and extracted G-code, and revalidates a completed build. It leaves
all artifacts and writes `verification.json`; it does not connect to a printer.
