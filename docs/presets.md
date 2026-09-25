# Presets

Presets are independent, versioned configuration layers:

1. Machine hard constraints.
2. Process constraints.
3. Material settings.
4. Purpose objectives.
5. Quality objectives.
6. User overrides that do not violate hard constraints.

The resolver writes a flattened `resolved-config.json` into each build so later
changes to preset catalogs cannot silently change historical builds.

Initial purposes:

- `test-smoke`: small deterministic E2E workflow without hollowing.
- `miniature-support-dense-experimental`: denser automatic supports for measured
  comparison trials; it is not a production-readiness claim.
- `miniature-display-high-detail`: declares hollowing, drains, resin orientation,
  support-aware packing, primary/secondary automatic supports, proactive
  collision-checked resin trees with bounded load-bearing trunks, enforcers,
  braces, a raft, iterative refinement, and UVtools
  production validation as required capabilities. Its geometry defaults are a
  0.12 mm measured repair grid, 0.15 mm hollow grid, 1.8 mm wall, and two
  independently validated 3.5 mm bottom drains. Prepared meshes above 500,000
  triangles are also offered a component-aware quadric reduction toward
  350,000 triangles; connected bodies below 5,000 triangles are retained and
  the result is used only when all measured safety gates pass.
- `functional-resin-dimensional`: keeps parts solid and protects dimensional and
  mating surfaces.

Planned catalogs include miniature durability/draft/display variants,
functional resin, calibration generators, and FDM functional/display/prototype
purposes. Material exposure settings must never be embedded in purpose presets.

Nested project overrides are deep-merged only when every supplied key already
exists in the selected preset. Unknown keys are rejected so a spelling error
cannot silently fall back to a default. Each build stores the full resolved
configuration next to its plan and output evidence.

## External catalogs

Additional printer fleets do not require edits to Agent-Slicer's Python code.
Set `AGENT_SLICER_PRESET_PATH` to an `os.pathsep`-separated list of directories,
or pass one or more `--catalog-path` options to `presets`, `profiles list`, or
`profiles recommend`. Each directory may contain `machines.json`,
`materials.json`, `purposes.json`, and/or `qualities.json`, using the same
top-level id-to-definition object as the bundled files. There is no entry-count
limit.

External ids may add to bundled and earlier external catalogs, but may not
replace an existing id. A collision, malformed machine constraint, invalid
process, bad compatibility list, or malformed evidence record fails closed.

```bash
agent-slicer profiles add \
  --directory "$HOME/.config/agent-slicer/presets" \
  --catalog machines \
  --id my-printer-0.4 \
  --definition my-printer.json

agent-slicer profiles validate "$HOME/.config/agent-slicer/presets"
```

`--definition` contains one preset definition object, not a full catalog. The
write is atomic and refuses both bundled and external duplicate ids.

## Evidence-backed recommendations

`profiles recommend` checks process, machine/material/quality compatibility,
and output format, then returns the exact selected settings, upstream profile
identity, source claims, calibration reasons, and full resolved configuration.
Its output deliberately distinguishes a source-backed starting point from a
physically calibrated result. The returned precedence record makes purpose
choices (for example, enabling supports) override a quality profile's default,
while machine limits remain hard constraints.

```bash
agent-slicer profiles recommend \
  --machine elegoo-mars-3-pro \
  --material anycubic-water-washable-2-grey \
  --purpose miniature-display-high-detail \
  --quality standard

agent-slicer profiles recommend \
  --machine bambu-lab-p2s-0.4 \
  --material bambu-pla-basic-p2s \
  --purpose fdm-general \
  --quality p2s-0.20-standard
```

The Mars 3 Pro values retain the three user-supplied resin profiles and the
current official ELEGOO hardware/resin-range evidence. The P2S values are
resolved from the official Bambu Studio vendor profiles pinned in
[`PRINTER_PROFILES_2026-08-30-v1.md`](PRINTER_PROFILES_2026-08-30-v1.md).
P2S FDM slicing is not implemented yet, so the corresponding project plan keeps
`fdm.slice` and `fdm.validate.basic` as blockers instead of falling through to
the resin backend.
