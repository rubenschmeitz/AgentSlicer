# Generation workflows

Agent-Slicer has two resumable generation workflows. They converge only after
an editable CAD model or generated mesh has been selected. The existing
inspection, repair, hollowing, drainage, support, slicing, and UVtools stages
remain authoritative.

Generation never uploads an image or submits a paid task without both an
approved brief and `--confirm-credit-use`. API keys are read only from the
environment and are never stored in manifests or receipts.

## Functional, dimensioned parts

Use the parametric workflow for brackets, holders, adapters, enclosures, and
other dimension-critical parts. The first implementation supports additive and
subtractive boxes and cylinders along X, Y, or Z, plus a final fillet or
chamfer. It writes:

- the strict millimetre specification;
- editable CadQuery Python source;
- STEP and STL exports;
- top, front, and right SVG sketches;
- hashes, dimensions, and mesh inspection evidence.

```bash
agent-slicer generate init mounting-plate \
  --directory projects/generation/mounting-plate \
  --kind part \
  --description "A 60 x 35 x 4 mm mounting plate with four 4.2 mm holes"

agent-slicer generate approve projects/generation/mounting-plate brief
agent-slicer generate build-part projects/generation/mounting-plate \
  --spec examples/generation/mounting-plate.json

# Inspect selected/model.step, selected/model.stl, and sketches/*.svg first.
agent-slicer generate approve projects/generation/mounting-plate selection
agent-slicer generate adopt projects/generation/mounting-plate \
  --agent-project projects/printable-mounting-plate
```

The generated STL is only an input to the manufacturing pipeline. CAD export
does not prove fit, strength, printer compatibility, or printability.

## Concept/image to printable object

Use this workflow for miniatures, sculptures, ornaments, busts, creatures, and
other organic geometry. Target height is mandatory because generated meshes
and STL files do not provide trustworthy physical units.

```bash
agent-slicer generate init owl-miniature \
  --directory projects/generation/owl-miniature \
  --kind concept \
  --description "A calm stylised owl with thick printable feathers and no pedestal" \
  --target-height 80 \
  --image-provider meshy \
  --model-provider tripo

agent-slicer generate approve projects/generation/owl-miniature brief

# This can consume provider credits.
agent-slicer generate submit-reference projects/generation/owl-miniature \
  --count 1 --confirm-credit-use
agent-slicer generate refresh projects/generation/owl-miniature

# Inspect references/, then approve the actual images.
agent-slicer generate approve projects/generation/owl-miniature references
agent-slicer generate submit-model projects/generation/owl-miniature \
  --count 3 --confirm-credit-use
agent-slicer generate refresh projects/generation/owl-miniature
agent-slicer generate status projects/generation/owl-miniature

# Candidate IDs are listed by status.
agent-slicer generate select projects/generation/owl-miniature candidate-1
agent-slicer generate approve projects/generation/owl-miniature selection
agent-slicer generate adopt projects/generation/owl-miniature \
  --agent-project projects/printable-owl
```

Existing images can replace provider image generation:

```bash
agent-slicer generate add-reference projects/generation/owl-miniature \
  front.png back.png side.png
agent-slicer generate approve projects/generation/owl-miniature references
```

The current provider adapters are:

- Meshy: text-to-image, text-to-3D preview, and image-to-3D;
- Tripo v3: text-to-image, text-to-model, image upload, image-to-model, and task
  polling.

Provider model versions may be pinned with `--model`. This is recommended for
repeatable production work. When omitted, the adapter uses Meshy `meshy-7` or
Tripo `v3.1-20260211` for geometry and the provider's documented balanced image
model.

## Credentials

Set only the providers you intend to use:

```bash
export MESHY_API_KEY="..."
export TRIPO_API_KEY="..."
```

Do not place keys in `generation.json`, `project.json`, shell history committed
to a repository, or agent prompts. A missing key fails before any network call.

## Approval and state model

1. `brief`: dimensional intent or concept description accepted.
2. `references`: the actual local/generated images accepted.
3. `selection`: the normalised 3D candidate accepted.
4. `adopt`: the selected STL is copied and hashed into an Agent-Slicer project.

Tasks retain provider, operation, task ID, status, progress, model selection,
fractional credit usage, timestamps, and local artifact IDs. Signed download
URLs and API keys are deliberately not persisted. Downloads are HTTPS-only,
limited to 1 GiB, staged to a temporary file, and hashed after completion.

## Local open-model status

The local RTX A1000 Laptop GPU has 4 GB VRAM. That is below the official
requirements of current high-quality open image-to-3D models such as TRELLIS.2
and Hunyuan3D 2.1. The provider abstraction is ready for a future remote/local
adapter, but installing those models on this laptop would not provide a usable
workflow. A capable remote GPU host can be added later without changing the
generation schema.
