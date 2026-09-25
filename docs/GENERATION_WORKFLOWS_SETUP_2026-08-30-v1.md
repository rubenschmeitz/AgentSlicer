# Generation workflows setup report v1

Date: 2026-08-30

## Outcome

Agent-Slicer now has two resumable creation workflows in front of its existing
repair, hollowing, drainage, support, slicing, and UVtools validation pipeline:

1. a dimensioned-part workflow producing editable CadQuery Python, STEP, STL,
   and top/front/right SVG projections from a strict millimetre JSON spec;
2. a concept workflow that can generate reference images and 3D candidates
   through current Meshy or Tripo APIs, or accept existing local images.

Both workflows stop for explicit approval before generated geometry is adopted
into a manufacturing project. External submission also requires an explicit
credit-use flag. No provider call, printer communication, or firmware change
was performed during setup.

## Installation

- Repository: `/mnt/c/Users/20160600/Documents/Development/agent-slicer`
- Virtual environment: `/mnt/c/Users/20160600/Documents/Development/agent-slicer/.venv`
- CLI: `/mnt/c/Users/20160600/Documents/Development/agent-slicer/.venv/bin/agent-slicer`
- Package version: `0.3.0`
- Base commit: `fe68f97f4ceee33d2256792bfbf2a77361c9d4cb`
- CAD dependency: CadQuery `2.8.0`, installed only in the project virtual environment
- Dependency check: passed (`pip check` reported no broken requirements)

The setup changes are currently working-tree changes on top of the base commit;
the commit ID alone therefore does not identify this generation implementation.

## Provider integration

The adapters use the current dedicated APIs:

- Meshy: text-to-image v1, text-to-3D v2, and image-to-3D v1; geometry defaults
  to `meshy-7` and untextured STL/GLB output.
- Tripo: v3 text-to-image, text-to-model, image upload, image-to-model, task
  query, and immediate expiring-result download; geometry is pinned by default
  to `v3.1-20260211`.

The live documentation checked for this setup is:

- [Meshy text-to-image](https://docs.meshy.ai/en/api/text-to-image)
- [Meshy text-to-3D](https://docs.meshy.ai/en/api/text-to-3d)
- [Meshy image-to-3D](https://docs.meshy.ai/en/api/image-to-3d)
- [Tripo v3 quick start](https://developers.tripo3d.ai/en/docs/quick-start)
- [Tripo v3 changelog](https://developers.tripo3d.ai/en/docs/changelog)
- [Tripo file upload](https://developers.tripo3d.ai/en/docs/files)
- [CadQuery installation](https://cadquery.readthedocs.io/en/stable/installation.html)

Current credential state:

- `MESHY_API_KEY`: not configured
- `TRIPO_API_KEY`: not configured

Missing credentials fail before a network request. API keys and expiring signed
URLs are not written to generation manifests. Provider/model/task IDs,
fractional credit usage, progress, local artifact hashes, and approvals are
retained for provenance.

## Verified CAD sample

The checked example is `examples/generation/mounting-plate.json`: a
60 x 35 x 4 mm plate with four 4.2 mm through holes and a 1 mm fillet.

Generated project:

`/home/rschmeitz/development-tools/agent-slicer-work/generation-demo-2026-08-30`

Evidence:

- STEP, STL, editable Python, JSON source, and three SVG projections generated;
- exact STL bounds: 60 x 35 x 4 mm;
- 75,996 triangles;
- 8 measured CadQuery tessellation degeneracies removed;
- post-cleanup degenerate triangles: 0;
- final STL SHA-256:
  `3a5413482448eb02e1b4a0ea1956eacbfccce6631dd64db12c3534a9ca00d7d6`;
- CAD job: 72.40 seconds wall time, 622,380 KiB peak RSS, zero swaps.

The selected STL was then approved and adopted by hash into:

`/home/rschmeitz/development-tools/agent-slicer-work/generation-demo-2026-08-30/print-project`

That project was configured for the Elegoo Mars 3 Pro, CTB output,
Anycubic Water-Washable 2 Grey resin, functional dimensional handling, and
automatic supports. Inspection and planning completed with zero blockers. A CTB
was deliberately not produced from this test part because the purpose of this
setup run was to verify generation and manufacturing handoff, not authorize a
physical print.

## Tests

- Focused generation/provider tests: 10 passed.
- Full test suite: 58 passed in 37.63 seconds wall time.
- Full-suite peak RSS: 138,272 KiB; swaps: 0.
- Python compile check: passed.
- CLI help/version/provider discovery: passed.
- JSON schemas parse successfully.

Provider HTTP behavior was verified with mocked current response contracts. A
live paid task cannot be honestly marked verified until one of the user's API
keys is configured and credit use is explicitly confirmed.

## Use after reboot

```bash
cd /mnt/c/Users/20160600/Documents/Development/agent-slicer
source .venv/bin/activate
agent-slicer version
agent-slicer generate providers
```

Configure only the provider being used, preferably through a secure shell or
credential mechanism rather than a committed file:

```bash
export MESHY_API_KEY="your-key"
export TRIPO_API_KEY="your-key"
agent-slicer generate providers
```

Do not paste credentials into project JSON, source code, committed shell files,
or an agent prompt.

## Example commands

Dimensioned part:

```bash
agent-slicer generate init mounting-plate \
  --directory /home/rschmeitz/development-tools/agent-slicer-work/my-plate \
  --kind part \
  --description "A dimensioned mounting plate"
agent-slicer generate approve \
  /home/rschmeitz/development-tools/agent-slicer-work/my-plate brief
agent-slicer generate build-part \
  /home/rschmeitz/development-tools/agent-slicer-work/my-plate \
  --spec examples/generation/mounting-plate.json
```

Image/concept workflow:

```bash
agent-slicer generate init owl \
  --directory /home/rschmeitz/development-tools/agent-slicer-work/owl \
  --kind concept \
  --description "A printable stylised owl with robust feet and separated wings" \
  --target-height 80 \
  --image-provider meshy \
  --model-provider tripo
agent-slicer generate approve \
  /home/rschmeitz/development-tools/agent-slicer-work/owl brief
agent-slicer generate submit-reference \
  /home/rschmeitz/development-tools/agent-slicer-work/owl \
  --count 1 --confirm-credit-use
```

Review and approve the downloaded references before submitting any model task;
review and approve the normalized STL before adopting it into an Agent-Slicer
print project.

## Limitations

- The parametric schema currently supports boxes and cylinders, Boolean
  add/cut operations, and one final all-edge fillet or chamfer. It is not yet a
  general sketch/constraint/assembly CAD system.
- Cloud-generated geometry is probabilistic and may be non-watertight,
  anatomically wrong, thin, disconnected, or unsuitable for resin printing.
  The existing manufacturing pipeline must repair and validate it.
- The first provider implementation uses one approved image as the
  image-to-model input. Labeled multi-view submission remains future work.
- Local high-quality open image-to-3D inference is not practical on the laptop's
  4 GB RTX A1000 GPU, so it was not installed.
- CadQuery's OCP/VTK stack starts slowly from the `/mnt/c` virtual environment.
  Generation jobs are serialized by the shared heavy-job lock to prevent
  concurrent agents from multiplying memory pressure.
- Software validation does not prove dimensional fit, resin calibration,
  firmware acceptance, or a successful physical print.
