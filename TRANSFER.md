# Agent-Slicer transfer

## Purpose and source snapshot

This is a portable, source-only snapshot made on 2026-09-14. It contains the
agent-first manufacturing orchestration project and its locally modified
Resin-Slicer companion backend. It does not contact a printer, Meshy, Tripo,
or any networked printer service.

Primary source repository (working tree, including uncommitted source changes):

`/home/rschmeitz/development/agent-slicer`

Companion source repository required for resin slicing:

`/home/rschmeitz/development/Resin-Slicer`

The primary repository has no configured Git remote. At capture time it was on
branch `main`, at base commit
`fe68f97f4ceee33d2256792bfbf2a77361c9d4cb` (`Add experimental miniature
hollowing and analysis`, 2026-08-28), with substantial uncommitted source,
test, documentation, schema, and preset changes included in this transfer.

The companion repository has remote
`https://github.com/jagers-HHMI/Resin-Slicer.git`, branch `main`, and base
commit `b7438ddbd66c4f714bfc0031a1f1d3787dbf011f` (`Support rework and
Overlapping body fix`, 2026-07-10). Its local uncommitted source, test, and
documentation changes are included because Agent-Slicer relies on their native
CTB/GOO readers, validation, and support improvements.

## Contents

- `agent_slicer/`, `schemas/`, `adapters/`, `skills/`, `examples/`, `docs/`,
  `tests/`, `README.md`, `AGENTS.md`, `pyproject.toml`, and project metadata:
  the Agent-Slicer source tree.
- `companion/Resin-Slicer/`: source for the companion `resin-slicer` CLI,
  Electron UI source, tests, `package.json`, `package-lock.json`, Python
  packaging metadata, and documentation.

Agent-Slicer converts typed project manifests plus machine/material/purpose/
quality presets into deterministic plans and auditable artifacts. It inspects,
scales, repairs, simplifies, hollows/drains, orients, packs, combines and
validates meshes; delegates resin supports/slicing to Resin-Slicer; records
hashes, resolved settings, commands, logs, and validation receipts; and offers
a read-only local inspection GUI. Its gated generation workflow can accept
local models or explicitly approved Meshy/Tripo work, but credentials are read
only from environment variables and remote credit use needs explicit approval.

## Installation and start

Use Python 3.10+; Python 3.12 was used for the verification snapshot. On the
destination, place this directory at `tools/agent-slicer` and retain the
companion at `tools/agent-slicer/companion/Resin-Slicer` (or install it
elsewhere and set the environment variable below).

```powershell
cd C:\Users\ruben\OneDrive\Documents\Prints\3d-print-agents\tools\agent-slicer\companion\Resin-Slicer
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .

cd ..\..\..
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
$env:AGENT_SLICER_RESIN_SLICER = (Resolve-Path .\companion\Resin-Slicer\.venv\Scripts\resin-slicer.exe)
agent-slicer version
agent-slicer tools
python -m unittest discover -s tests -v
```

On Windows, install Blender separately only for measured repair/orientation
operations and UVtools separately for the independent resin production gate;
set `AGENT_SLICER_BLENDER` and `AGENT_SLICER_UVTOOLS` to their explicit
executables when automatic discovery does not find them. The Resin-Slicer
Electron application additionally needs Node.js/npm for its UI build; the CLI
does not need `node_modules` to be transferred and npm should recreate it from
the included `package-lock.json`.

## Configuration still to supply

- The target integration now contains a Mars 5 Ultra machine preset and eight
  printer-specific resin start profiles. They use 153.36 x 77.76 x 165 mm and
  8520 x 4320 as hard local constraints and keep the printer's tilt-release
  motion separate from Mars 3 Pro lift/drop values. The preset and profiles
  pass local structural checks, but accepted output dialect, motion semantics,
  firmware acceptance, and each resin exposure still require a small controlled
  validation job.
- Resin calibration remains printer-, bottle-, temperature-, film-, and layer-
  height-specific. Do not treat any bundled setting as calibrated for a new
  printer or resin.
- Meshy and Tripo are optional. If intentionally used, set `MESHY_API_KEY` and
  `TRIPO_API_KEY` only in the local environment/session. Do not place either in
  project files or commits. The workflow fails before a request when keys are
  absent and requires explicit approval before credit-consuming submission.
- CHITUBOX is not an integration backend. It can be used independently as a
  human review/reference slicer, but its proprietary project/profile files are
  not imported as Agent-Slicer configuration.

## Compatibility assessment

- Meshy/Tripo: implemented as optional, resumable provider adapters with
  explicit approval and credential gates; no generation task is made merely by
  installing this transfer.
- CHITUBOX: no local installation or proprietary API is required. Agent-Slicer
  provides an automated, audited subset rather than feature parity.
- ELEGOO Mars 5 Ultra: the local candidate profile/output path is wired and
  tested structurally. Its CTB v5/GOO v3 writer alone does not establish Mars
  5 Ultra firmware acceptance, and no physical Mars 5 Ultra print test is
  included.
- Resin profiles: versioned, composable presets are supported, but only
  evidence-bearing profiles supplied with this code may be selected. New
  resins require explicit calibration/evidence.

## Excluded deliberately

No `.git` history, virtual environments, `node_modules`, Python caches,
build/dist/egg-info outputs, temporary work/project/artifact directories,
generated mesh/model files, sliced print files, or secret-bearing files are
included. Specifically excluded patterns include `.env*`, `*.pem`, and
`*.key`. No API key, token, cookie, or local credential was read or copied.

## Known limits

- This is a source snapshot with dirty working-tree changes, not a clean
  release commit. Preserve it as an import and establish a destination commit
  after reviewing the diff.
- FDM/Bambu profiles can be resolved but no production FDM slicing adapter is
  implemented.
- Resin preparation/slicing is validation-gated but cannot prove firmware
  acceptance, resin calibration, dimensional fit, or physical-print success.
- The local GUI is read-only and localhost-bound by default.

## Recommended integration

1. The original archive is retained in
   `incoming/agent-slicer-agent-slicer-20260914` for review and provenance.
2. The working source is integrated under `tools/agent-slicer`.
3. Keep the companion backend beneath
   `tools/agent-slicer/companion/Resin-Slicer` or make its executable explicit
   with `AGENT_SLICER_RESIN_SLICER`.
4. Use `scripts/agent-slicer.ps1` for local Windows runs and keep the
   non-printing file-format validation separate from any physical printer
   workflow.
