# Resin-Slicer command-line setup

Setup completed on 2026-08-28, enhanced through 2026-08-31, and fully migrated
to WSL-native storage and runtimes on 2026-08-31.

## Installation summary

- Host: Windows 11, using Ubuntu 24.04.3 LTS under WSL2.
- Project directory: `/home/rschmeitz/development/Resin-Slicer`
- Virtual environment: `/home/rschmeitz/development/Resin-Slicer/.venv`
- Installed package: `resin-slicer 0.1.0`
- Git branch and base commit: `main`, `b7438ddbd66c4f714bfc0031a1f1d3787dbf011f`, with local Mars 3 Pro resin-profile and support-engine enhancements
- Commit date/subject: `2026-07-10T11:08:26-04:00`, `Support rework and Overlapping body fix`
- Python: 3.12.3
- Installation method: WSL-native project-local virtual environment followed by `.venv/bin/python -m pip install -e .`
- Mandatory third-party Python dependencies: none declared by `pyproject.toml`.
- Exact executable: `/home/rschmeitz/development/Resin-Slicer/.venv/bin/resin-slicer`
- CLI tests: succeeded (`resin-slicer --help` and `resin-slicer --version` exited 0).
- Repository tests: all 118 passed in the WSL-native checkout, including the supplied Mars 3 Pro resin-profile values, CTB/GOO writer and reader tests, native work-root enforcement, rotation, deterministic multi-pass/tree/dogleg/same-component support routing, exact contact accounting, and parallel layer rendering.

The locally installed support engine additionally has sparse occupied-layer
storage, repeated machine-specified contacts, strict requested/routed contact
accounting, tapered internal enforcers, and acyclic branch-to-existing-support
routing. Manual contacts are resolved in deterministic retry passes so an
earlier contact can attach to a safe tree parent created later in the request.
Ambiguous detector contacts also get a vertical-tip retry when the sampled
side-wall normal is blocked; all collision, reach, and shaft-angle limits remain
enforced. Contacts obstructed below the tip can use a bounded, collision-checked
dogleg branch through a new intermediate trunk with a reinforced bed pad.
If an exact detector pixel remains obstructed, the router may move by at most
0.5 mm to another lit pixel in the same connected unsupported layer patch; the
actual coordinate is preserved in the receipt.
`--report-json` records the exact routed and unrouted coordinates for automated
validation loops, including the actual contact when an obstructed detector point
moves within its connected unsupported patch. These changes are installed inside this project-local environment;
they are not claimed to be present in upstream commit `b7438dd`.

The repository contains no example STL/OBJ/STEP model. In accordance with the setup request, no unrelated model was downloaded and no file was sent to a printer.

## Starting it after reboot

From a WSL/Ubuntu terminal:

```bash
cd /home/rschmeitz/development/Resin-Slicer
source .venv/bin/activate
resin-slicer --help
resin-slicer --version
```

Leave the environment with:

```bash
deactivate
```

Activation is optional when using the absolute executable path shown above.

## Verified Mars 3 Pro command

The Mars 3 Pro needs a 4098 x 2560 pixel image and has a 143.43 x 89.6 x 175 mm build volume. The supplied archive at `C:/Users/20160600/Downloads/resin_configs_mars3pro.zip` was inspected, validated, and installed as four named CLI profiles:

- `elegoo-mars-3-pro`: machine profile with the slicer's standard resin defaults.
- `elegoo-mars-3-pro-anycubic-water-washable-2-grey`
- `elegoo-mars-3-pro-photocentric-uv-lcd-hard-black`
- `elegoo-mars-3-pro-ifun-if3121-toughness-grey`

Select the profile matching the resin actually loaded in the vat. For example, for iFun iF3121 Toughness Grey:

```bash
resin-slicer model.stl \
  --output model.ctb \
  --format ctb \
  --profile elegoo-mars-3-pro-ifun-if3121-toughness-grey
```

Equivalent commands for the other supplied resins use:

```bash
--profile elegoo-mars-3-pro-anycubic-water-washable-2-grey
--profile elegoo-mars-3-pro-photocentric-uv-lcd-hard-black
```

The installed profile values are:

| Resin | Normal exposure | Bottom exposure | Bottom layers | Transition layers | Normal lift speed | Bottom lift speed | Retract speed |
|---|---:|---:|---:|---:|---:|---:|---:|
| ANYCUBIC Water-Washable Resin 2.0 Grey | 2.5 s | 30.0 s | 5 | 5 | 90 mm/min | 70 mm/min | 150 mm/min |
| Photocentric UV LCD Hard Black | 4.3 s | 38.0 s | 5 | 5 | 90 mm/min | 70 mm/min | 150 mm/min |
| iFun iF3121 LCD/DLP Toughness Grey | 2.5 s | 32.0 s | 5 | 5 | 100 mm/min | 70 mm/min | 150 mm/min |

All three use 0.05 mm layers, 5.0 mm lift distance, 0.8 s wait before cure, and 0.5 s wait after cure. In Resin-Slicer's layer-cycle terminology, these last two values are stored as `wait_after_retract_s=0.8` and `rest_before_lift_s=0.5`. The command and profile values were verified through the installed package outside the source checkout. They should still be calibrated for the actual resin batch, temperature, and printer before relying on a real print.

## Output formats and Mars 3 Pro compatibility

The installed CLI supports exactly:

- `ctb`: native encrypted Chitu CTB v5, CTB RLE, per-layer XOR coding, encrypted settings/signature records, preview and resin metadata. The writer uses magic `0x12FD0107`.
- `goo`: native Elegoo GOO V3.0 with per-layer settings and RLE compression.

No other output choices are accepted by the installed CLI. In particular, there is no `cbddlp`, `pwmb`, `sl1`, or `sl1s` exporter.

Mars 3 Pro CTB output **appears supported at the software level**, because:

- ELEGOO documents the printer at 4098 x 2560 pixels and 143.43 x 89.6 x 175 mm.
- Mars 3-family printers use the encrypted CTB dialect; the UVtools maintainer identifies `0x12FD0107` as its required encrypted CTB magic.
- The installed writer emits that magic and encrypted CTB v5 structures.
- A five-model production slice made with the supplied Anycubic Water-Washable
  Grey profile was reopened by UVtools 2.0.1 as CTB v5 with the exact Mars 3 Pro
  machine name, 4098 x 2560 resolution, 0.03 mm layers, 2.5 s normal exposure,
  30 s bottom exposure, and five bottom layers.

This is not the same as confirmed printer compatibility. A local Mars 3 Pro profile is now configured, but the repository warns that firmware may reject strict format/metadata dialect differences, and no physical-printer acceptance test was performed. Inspect a generated file in a current CTB validator/slicer and use a small non-critical calibration model before relying on it. Do not use GOO for the Mars 3 Pro merely because it is an Elegoo-branded format; GOO is associated with other/newer Elegoo printer generations.

## Verified automated miniature workflow

Resin-Slicer's CLI now supplies deterministic support generation, collision
checking, bracing, part-to-part enforcers, tree branches, dogleg routing, and a
machine-readable contact receipt. Mesh repair, automatic tilt/orientation,
component-aware hollowing, drain placement, sealed-cavity validation, packing,
and UVtools feedback are coordinated by the separate Agent-Slicer environment
at `/home/rschmeitz/development/agent-slicer` (version 0.3.0,
base commit `fe68f97f4ceee33d2256792bfbf2a77361c9d4cb`, with local production
enhancements).

The completed Tentoonstelling project is located at
`C:/Users/20160600/OneDrive - TU Eindhoven/Documents/Prints/Miniatures/Tentoonstelling/Tentoonstelling_AgentSlicer_Rerun_2026-08-28`.
Its final CTB has SHA-256
`dfc290cdc32bb8b96e86a2a68f3d402c049300293c871b13a248d4e7d4eeef45`.
The final slice receipt records 311 requested contacts, 311 routed contacts,
311 actual contacts, zero unrouted contacts, 1,837 total generated supports,
2,547 layers, and an estimated 41.84 ml of resin. The saved-file production
audit reported zero actionable islands, overhangs, resin traps, suction cups,
empty layers, build-bound violations, or print-height violations. UVtools still
reported raw sub-threshold detector records; these were retained in the JSON
audit instead of being deleted or described as nonexistent.

Each large connected miniature body was repaired and hollowed to a nominal
1.8 mm shell with two independently flood-tested 3.5 mm bottom-facing drains;
smaller disconnected detail bodies stayed solid. The voxel cavity tests found
zero sealed cavity voxels. No internal lattice was added: for these miniatures,
an open, drainable hollow shell avoids creating resin-trapping internal
structures. Digital cavity checks cannot guarantee that uncured liquid resin
will be absent after printing, so both drains must still be flushed, washed,
allowed to drain, and fully post-cured.

References:

- [Resin-Slicer README](https://github.com/jagers-HHMI/Resin-Slicer)
- [ELEGOO Mars 3 Pro specifications](https://ca.elegoo.com/products/elegoo-mars-3-pro-4k-mono-lcd-3d-printer)
- [UVtools CTB magic discussion](https://github.com/sn4k3/UVtools/discussions/750)

## Verified CLI parameters

| Requested control | Installed CLI option | Notes |
|---|---|---|
| Orientation/rotation | `--rotate-x`, `--rotate-y`, `--rotate-z` | Degrees around model centre. There is no automatic orientation command. |
| Model lift | `--model-lift` | Millimetres; default 5.0 when supports are enabled. |
| Automatic supports | On by default; disable with `--no-supports` | Island-driven support generation. |
| Support angle | `--overhang-angle` | Higher values generate more supports; default 45 degrees. |
| Support spacing/density | `--support-spacing`; optional `--primary-supports`, `--primary-density-multiplier`, `--primary-area-radius`, `--primary-max-extra` | Smaller spacing means denser normal supports. |
| Explicit/feedback support contacts | repeat `--manual-support XxYxZ`; add `--require-manual-supports` | Coordinates are build-plate millimetres. Strict mode fails if any requested contact is dropped. |
| Machine-readable slice receipt | `--report-json PATH` | Records layer/support/material totals plus exact routed and unrouted manual contact coordinates. |
| Layer height | `--layer-height` | Millimetres. |
| Normal exposure | `--exposure` | Seconds. |
| Bottom exposure | `--bottom-exposure` | Seconds. |
| Bottom layer count | `--bottom-layers` | Integer. |
| Lift distance | `--lift-distance` | Millimetres. |
| Lift speed | `--lift-speed` | Millimetres/minute. |
| Retract speed | `--retract-speed` | Millimetres/minute. `--retract-distance` is also available. |
| Resolution | `--resolution WIDTHxHEIGHT` | Pixels. |
| Printer dimensions | `--volume XxYxZ` | Millimetres. `--machine-name` controls output metadata. |

Other verified placement controls are `--scale`, `--translate XxYxZ`, and `--no-center`. Other support controls include tip/post/foot sizes, tip type, raft/feet/skate interface, braces, collision clearance, shaft angle, enforcers, minimum island area, and the support-analysis pixel budget. Bed-inaccessible explicit contacts can route as tapered part-to-part enforcers or acyclic branches into existing support shafts. Run `resin-slicer --help` for the installed exhaustive list.

Check the installed version with either:

```bash
resin-slicer --version
python -c "import importlib.metadata; print(importlib.metadata.version('resin-slicer'))"
```

## Python API

There is a usable in-process Python API. The package explicitly exports `PrintConfig`, `SupportConfig`, `MeshTransform`, `SliceJob`, and `slice_to_file`; model loading is available as `resin_slicer.mesh.load_mesh`.

```python
from resin_slicer import MeshTransform, PrintConfig, SliceJob, SupportConfig, slice_to_file
from resin_slicer.mesh import load_mesh

from resin_slicer.config import profile

config = profile("elegoo-mars-3-pro-ifun-if3121-toughness-grey")
supports = SupportConfig(
    enabled=True,
    model_lift_mm=5.0,
    overhang_angle_deg=45.0,
    support_spacing_mm=3.0,
)
job = SliceJob(
    mesh=load_mesh("model.stl"),
    print_config=config,
    support_config=supports,
    transform=MeshTransform(rotate_x_deg=30.0),
    manual_support_points=((70.0, 44.0, 12.5),),
    require_manual_supports=True,
)
result = slice_to_file(job, "model.ctb", "ctb")
print(result.manual_supports_routed_points)
print(result.manual_supports_actual_contact_points)
print(result.manual_supports_unrouted_points)
```

The API exposes more print fields than the CLI, including two-stage lift/retract and bottom-layer motion overrides. It is a direct Python library, not an HTTP/REST service. The separate `resin-slicer-electron-bridge` executable is an internal JSON-line bridge for the Electron app and should not be mistaken for a network API.

## Performance and memory defaults (2026-08-30)

The isolated virtual environment now uses an editable install of this checkout,
so the CLI invoked by Agent-Slicer executes the optimized source directly.
Binary STL input is memory-mapped, transforms are lazy, layer indices use
compact 32-bit triangle references, and surface-normal planning is bounded.
Layer rendering defaults to one worker to avoid retaining multiple
full-resolution raster buffers.

```bash
export RESIN_SLICER_LAYER_WORKERS=1
# Optional, valid range 1000..1000000:
# export RESIN_SLICER_SURFACE_NORMAL_SAMPLES=250000
```

The 9,092,809-triangle Tentoonstelling plate load/bounds/layer-index/sample
benchmark peaked at 867,872 KiB RSS. Resin-Slicer passed 77 tests after these
changes. The complete Agent-Slicer/UVtools measurements are in
`../agent-slicer/docs/PERFORMANCE_MEMORY_OPTIMIZATION_2026-08-30-v1.md`.

## Limitations

- The supplied YAML files are now represented by local built-in Python profiles. Pulling/updating the upstream checkout may require reconciling these local changes.
- No physical Mars 3 Pro/firmware validation was performed, so CTB compatibility is evidence-backed but provisional.
- Exposure, lift, and retract settings are resin-, batch-, and temperature-dependent; supplied values should still be calibrated.
- No automatic orientation algorithm in the CLI; rotations are explicit numeric inputs.
- STEP/STP loading is optional and requires OpenCascade/OCP, which is not declared by the package and was not installed. STL and OBJ need no extra package.
- This setup installs the Python CLI in WSL. It does not globally install a Windows executable or the optional Electron GUI/Node dependencies.
