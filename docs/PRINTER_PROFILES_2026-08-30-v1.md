# Evidence-backed printer profiles — 2026-08-30 v1

## Scope and interpretation

This snapshot adds settings for:

- ELEGOO Mars 3 Pro with the three user-supplied resin profiles already installed
  in Resin-Slicer.
- Bambu Lab P2S with its stock 0.4 mm hardened-steel nozzle and the official
  Bambu PLA Basic, PETG HF, and ABS profiles.

There is no universal “best” setting. A defensible recommendation is a pinned,
manufacturer-backed starting profile selected by printer, nozzle, material,
layer height, and purpose, followed by material calibration and validation. The
API therefore reports `evidence-backed-starting-point` and
`calibration-required`; it never calls an unprinted profile proven or optimal.

No printer was contacted, no print was started, and no firmware was changed.

## Source snapshot

Primary manufacturer sources were preferred over blogs and community tables.

- [ELEGOO Mars 3 Pro product specifications](https://www.elegoo.com/en-gb/collections/elegoo-product-ex-s3-s4-m5/products/elegoo-mars-3-pro-4k-mono-lcd-3d-printer)
- [Current ELEGOO resin settings](https://wiki.elegoo.com/resin-settings),
  source updated 2026-05-21 and retrieved 2026-08-30
- [Bambu Lab P2S launch specifications](https://blog.bambulab.com/the-icon-redefined-meet-the-p2s-a-completely-reengineered-version-of-the-ultra-productive-p1-series/)
- [Official Bambu Studio repository](https://github.com/bambulab/BambuStudio),
  resolved at commit `108e108635f2a223219dcf8f5e8fe3fce5ef40df`
  (2026-08-29), vendor configuration `02.08.00.05`
- [Official Bambu Studio command-line documentation](https://github.com/bambulab/BambuStudio/wiki/Command-Line-Usage)
- Material-specific manufacturer sources embedded in `materials.json`

The Bambu values below were resolved through each profile's inheritance chain.
Where Bambu stores one array element per extruder variant, the first element for
the stock `Direct Drive Standard` extruder is used. High-flow values were not
silently substituted.

## ELEGOO Mars 3 Pro

Hard constraints:

- MSLA, 4098 x 2560 pixels, 0.035 mm XY pixel size.
- 143.43 x 89.6 x 175 mm build volume.
- CTB output in the current Resin-Slicer path.
- Official printer layer-height range: 0.01-0.2 mm.

ELEGOO's current Mars 3 / Mars 3 Pro table gives this starting range for its own
grey Standard Resin 1.0 and 2.0 at 0.05 mm:

| Variable | Manufacturer range/value |
| --- | ---: |
| Normal exposure | 2.5-3.0 s |
| Bottom exposure | 25-30 s |
| Lift distance | 5 mm |
| Lift speed | 80 mm/min |
| Retract speed | 210 mm/min |
| Rest after retract | 0.5 s |

Those values must not be copied blindly to other brands. The production-capable
profiles remain the user's supplied, Mars-3-Pro-specific settings:

| Resin | Layer | Normal | Bottom | Bottom layers | Lift | Lift speed | Retract |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Anycubic Water-Wash Resin 2.0 Grey | 0.05 mm | 2.5 s | 30 s | 5 | 5 mm | 90 mm/min | 150 mm/min |
| iFun iF3121 Toughness Grey | 0.05 mm | 2.5 s | 32 s | 5 | 5 mm | 100 mm/min | 150 mm/min |
| Photocentric UV LCD Hard Black | 0.05 mm | 4.3 s | 38 s | 5 | 5 mm | 90 mm/min | 150 mm/min |

Each also uses 5 transition layers, 70 mm/min bottom lift, 0.5 s rest before
lift, and 0.8 s wait after retract. These exact values came from
`resin_configs_mars3pro.zip`; they are starting points, not physical-print
evidence. The source archive used here has SHA-256
`d7ed33267d052e2053b7d82a67747b77cdd535bb826b86e2788ab6ad6cc344c7`.

Important: the existing `high-detail` quality selects 0.03 mm, while every
supplied resin profile was sourced at 0.05 mm. Agent-Slicer now reports that
mismatch as a calibration reason. It does not pretend that a 0.05 mm exposure
is automatically optimal at 0.03 mm.

For the current hollow supported miniature purpose, geometry and support choices
remain separate from resin exposure: 5 mm model lift, 40 degree overhang angle,
1.35 mm contact spacing, 0.16 mm tip radius, 0.30 mm post radius, raft/braces,
two 3.5 mm drains, and fail-closed UVtools refinement. These are Agent-Slicer's
current production-validation policy, not values claimed to be ELEGOO material
recommendations.

Verified recommendation command:

```bash
.venv/bin/agent-slicer profiles recommend \
  --machine elegoo-mars-3-pro \
  --material anycubic-water-washable-2-grey \
  --purpose miniature-display-high-detail \
  --quality standard
```

## Bambu Lab P2S, stock 0.4 mm nozzle

Hard constraints and stock-machine values:

- FFF, 256 x 256 x 256 mm build volume.
- Stock 0.4 mm hardened-steel nozzle; 0.2/0.4/0.6/0.8 mm supported.
- 300 C maximum nozzle temperature and 110 C maximum bed temperature.
- Machine X/Y speed limit 600 mm/s; X/Y and extruding acceleration limit
  20,000 mm/s2; travel acceleration limit 10,000 mm/s2.
- 0.8 mm retraction at 30 mm/s and 0.4 mm Z-hop in the official machine
  profile.

Resolved official 0.4 mm process choices:

| Agent-Slicer quality | Official Bambu profile | Walls | Top / bottom | Infill | Outer / inner / infill | Default accel |
| --- | --- | ---: | ---: | --- | --- | ---: |
| `p2s-0.08-high-quality` | 0.08mm High Quality @BBL P2S | 2 | 9 / 7 | 15% gyroid | 60 / 120 / 150 mm/s | 4000 mm/s2 |
| `p2s-0.12-high-quality` | 0.12mm High Quality @BBL P2S | 2 | 5 / 5 | 15% gyroid | 60 / 150 / 180 mm/s | 4000 mm/s2 |
| `p2s-0.20-standard` | 0.20mm Standard @BBL P2S | 2 | 5 / 3 | 15% grid | 200 / 300 / 270 mm/s | 10000 mm/s2 |
| `p2s-0.24-standard` | 0.24mm Standard @BBL P2S | 2 | 4 / 3 | 15% grid | 200 / 230 / 270 mm/s | 10000 mm/s2 |

All four use a 0.20 mm first layer, 0.42 mm default/outer line width, 0.45 mm
inner-wall line width, 0.50 mm first-layer line width, 600 mm/s travel, and a
0.012 mm mesh-resolution setting. Official support defaults are off. If enabled,
the inherited type is `tree(auto)` and the threshold is 15/20/30/35 degrees for
0.08/0.12/0.20/0.24 mm respectively. `fdm-display-supported` explicitly enables
tree supports; `fdm-general` retains the official disabled default.

Resolved stock-extruder materials:

| Agent-Slicer material | Nozzle normal / first | Textured PEI bed | Flow | Volumetric cap | Part fan | Aux fan |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `bambu-pla-basic-p2s` | 220 / 220 C | 55 C | 0.98 | 21 mm3/s | 100% | 70% |
| `bambu-petg-hf-p2s` | 245 / 230 C | 70 C | 0.95 | 21 mm3/s | 30-60% | 0% |
| `bambu-abs-p2s` | 270 / 260 C | 90 C | 0.95 | 16 mm3/s | 10-60% | 0% |

These are Bambu-branded filament profiles. Another brand or formulation needs
its matching profile and flow dynamics/flow-rate calibration; only changing the
name would not constitute optimization.

Verified recommendation command:

```bash
.venv/bin/agent-slicer profiles recommend \
  --machine bambu-lab-p2s-0.4 \
  --material bambu-pla-basic-p2s \
  --purpose fdm-general \
  --quality p2s-0.20-standard
```

## Unbounded external printer catalogs

The resolver is no longer limited to the bundled printer dictionary. Set an
OS-path-separated list of catalog directories:

```bash
export AGENT_SLICER_PRESET_PATH="$HOME/.config/agent-slicer/presets"
```

Each directory may contain any subset of `machines.json`, `materials.json`,
`purposes.json`, and `qualities.json`. Each file is a JSON object keyed by a
stable preset id. There is no coded preset-count ceiling. IDs are merged across
directories, and any collision fails closed rather than replacing a machine
silently.

Add and validate one new definition:

```bash
.venv/bin/agent-slicer profiles add \
  --directory "$HOME/.config/agent-slicer/presets" \
  --catalog machines \
  --id my-printer-0.4 \
  --definition my-printer.json

.venv/bin/agent-slicer profiles validate \
  "$HOME/.config/agent-slicer/presets"
```

The same operations are available through `load_catalog`,
`add_external_preset`, `validate_catalog_directory`, `resolve_presets`, and
`recommend_settings` in `agent_slicer.presets`.

## Remaining limits

- No physical calibration or print was performed. Material values remain
  starting points until test coupons and real parts confirm them.
- Bambu Studio is not installed on this computer and Agent-Slicer does not yet
  implement its FDM adapter. P2S profile selection and fail-closed planning work;
  producing a P2S sliced 3MF does not yet work in Agent-Slicer.
- Bambu's official CLI can load machine/process/filament settings and slice STL
  or 3MF input, but current upstream reports include a
  [Windows STL-slicing failure](https://github.com/bambulab/BambuStudio/issues/9802)
  and a [Linux full-settings export defect](https://github.com/bambulab/BambuStudio/issues/9699).
  A future adapter needs an isolated install and an end-to-end saved-output test
  before being enabled.
- Official online profiles can change. Every bundled recommendation records its
  retrieval date; Bambu profiles additionally pin the exact repository commit
  and vendor configuration version.
- The runtime does not scrape search results or silently self-update production
  settings. A coding agent can research another printer, add a source-bearing
  definition through the unbounded catalog API, validate it, and test its
  adapter; this keeps profile changes reviewable and reproducible.
- Manufacturer presets do not know model-specific requirements such as thin
  walls, bridge span, dimensional tolerance, support scars, or desired strength.
  Those remain separate purpose/geometry choices.
