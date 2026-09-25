# Agent-Slicer

Agent-Slicer is an agent-first, auditable additive-manufacturing pipeline with
deterministic planning and native geometry preparation.
Agents express intent through typed projects and presets; versioned geometry and
slicing backends execute operations and produce auditable artifacts.

For Mars 5 Ultra discovery, validated uploads, and separately confirmed print
start, see [LAN printer workflow](docs/lan-printer.md). The companion slicer
also exposes this workflow from its Job panel.

## Standalone installation

```powershell
git clone https://github.com/rubenschmeitz/AgentSlicer.git
cd AgentSlicer
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\agent-slicer.exe --help
```

The companion resin backend is included in `companion/Resin-Slicer` and needs
its own installation. Blender and UVtools are external dependencies. Configure
their paths using `AGENT_SLICER_BLENDER`, `AGENT_SLICER_UVTOOLS`, and
`AGENT_SLICER_RESIN_SLICER`. FDM preparation is supported, while the FDM slicing
adapter still requires end-to-end verification.

## Original integrated Windows installation

The following describes the original local workspace; its wrapper scripts
are outside this standalone repository. Use the installation above for a new clone.

Deze geïntegreerde checkout draait op deze pc in twee projectomgevingen:

```powershell
cd C:\Users\ruben\OneDrive\Documents\Prints\3d-print-agents
.\scripts\agent-slicer.ps1 version
.\scripts\agent-slicer.ps1 tools
```

De wrapper koppelt Resin-Slicer, Blender en UVtools automatisch en gebruikt een
lokale werkmap buiten versiebeheer. Rechtstreeks vanuit deze map kan ook
`.venv\Scripts\agent-slicer.exe` worden gebruikt; zet dan voor resin slicing
`AGENT_SLICER_RESIN_SLICER` op de executable in
`companion\Resin-Slicer\.venv\Scripts`. De oorspronkelijke bron op de andere
pc was WSL-native; die layout blijft als overdrachtsdocument behouden.

Agent-Slicer also supports gated, resumable creation workflows for parametric
parts and text/image-generated organic models. See
[`docs/generation-workflows.md`](docs/generation-workflows.md). Generated
geometry must still pass the normal manufacturing and validation pipeline.

## CLI engineering and measured benchmarks

Blender operations ship inside the Python wheel; an editable checkout is no
longer required to locate the repair scripts. Small, verified convex solids
keep their exact geometry instead of being unnecessarily voxel-remeshed.
Other inputs still take the measured repair path; hollowing remains lossy and
must pass the existing deviation and two-drain connectivity checks.

Heavy preparation, hollowing, builds, analyses and benchmarks share the native
per-user job lock. Blender/Resin subprocesses now stream logs to disk with at
most 64 KiB per stream embedded in receipts. Linux timeout/cancellation kills
their process group, including descendants. Resource controls:

```bash
export AGENT_SLICER_THREADS=2
export AGENT_SLICER_MAX_BACKEND_RSS_MB=2048
export AGENT_SLICER_MAX_HOLLOW_MEMORY_MB=1024
```

The backend RSS watchdog is optional (unset means no RSS limit), sampled and
not a hard OS memory cap. The hollowing preflight defaults to a 1 GiB estimated
grid working-set budget, excluding mesh/runtime overhead; it rejects expensive
requests rather than silently coarsening geometry. The thread setting controls
Blender and common numerical libraries; third-party TBB pools may ignore it.
Use Linux CPU affinity when a benchmark needs a strict shared CPU allocation.

```bash
agent-slicer benchmark /native/path/suite.json \
  --output /native/path/new-evidence-directory --repeats 3
```

Benchmark manifests contain executable argv and must be trusted like local
scripts. No shell expansion is performed. Runs are sequential, input hashes
are checked, commands/versions/logs/timing are recorded, and completed runs are
checkpointed. Existing result directories are refused; use a new versioned
directory for a rerun. Benchmarks measure exports, **not production approval**.
The build workflow's existing resumability and validation gates remain separate.

See [the measured engineering report](docs/CLI_ENGINEERING_BENCHMARK_2026-09-08-v1.md)
for real PrusaSlicer/SuperSlicer comparisons and the remaining support-quality
gap. Reproducible fixture, analysis and alternating-index scripts are under
[`examples/benchmarks`](examples/benchmarks). No reference slicer code is copied
into the MIT-licensed package; they are external benchmark tools.

```bash
python -m unittest discover -s tests -v
# Opt in to tests that launch the real installed Blender:
AGENT_SLICER_TEST_BLENDER=/path/to/blender python -m unittest discover -s tests -v
```

## Read-only inspection GUI

Agents remain responsible for planning, repair, hollowing, support generation,
slicing, and validation. A local GUI is available only to inspect the project
they produced:

```bash
agent-slicer gui /path/to/project
```

The dashboard opens on `http://127.0.0.1:8765/` and refreshes saved evidence
without modifying the project. It shows source/prepared/plate STL geometry,
placements, resolved machine dimensions, support accounting, phase timing,
basic and production checks, UVtools issue counts, limitations, and all final
artifacts. For a versioned audited delivery, it also decodes any selected CTB
v5 or GOO v3 layer directly from the saved file using Resin-Slicer's bounded
native reader. No extracted layer stack is written. The STL viewer samples very
large meshes for display only; neither preview path alters the model or
manufacturing output. Use `--no-open` on a headless WSL session, or `--port 0`
to select an available ephemeral port.

The server binds to localhost by default and exposes no mutation endpoints.
Binding `--host` to another interface should be done only on a trusted network;
the viewer is deliberately not an authenticated multi-user service.

The current `0.3.0` implementation is a validation-gated resin workflow. It is
designed for unattended CLI/SDK use while retaining machine-readable evidence
for every lossy geometry and slicing step. It is focused on resin production;
it is not a universal replacement for every interactive feature in mature GUI
slicers.

## Working today

- Project creation with copied, hashed source STLs.
- Composable machine, material, purpose, and quality presets.
- Evidence-bearing Mars 3 Pro and Bambu Lab P2S setting recommendations, with
  pinned upstream profile identity, explicit calibration status, and
  compatibility checks.
- Unbounded external preset directories with schema validation, atomic entry
  addition, and fail-closed duplicate-id handling.
- Streaming binary/ASCII STL inspection.
- Bounds, volume, unit-ambiguity, and degenerate-triangle diagnostics.
- Target-height scaling.
- Measured voxel repair with manifold/topology and deviation limits.
- Component-aware quadric mesh reduction after repair/hollowing and before
  plate assembly. Small connected bodies are protected, unsafe candidates are
  rejected, and exact source/config/output hashes make accepted results reusable.
- Bounded resin orientation scoring.
- Component-aware filled-voxel hollowing with wall-resolution limits: the
  largest printable body is hollowed while smaller repaired bodies are
  preserved as solids.
- Two automatic bottom drains, each proven to connect the retained cavity.
- Exterior flood-fill proof that no retained cavity voxels are sealed.
- Bounded topology disambiguation, adaptive voxel refinement for thin features,
  STL round-trip manifold checks, and bounded outer-surface change.
- Deterministic, support-footprint-aware shelf packing with hard plate limits.
- Safe removal of degenerate triangles during combined-plate export.
- Combined binary STL generation.
- Resin-Slicer execution with primary/secondary contacts, collision-aware
  resin support trees, load-thickened shared trunks, tapered pedestals,
  part-to-part enforcers, cross-braces, and a chamfered pad.
- Iterative denser-support retries plus bounded UVtools-feedback passes when a
  candidate fails validation. User-authored contacts can remain all-or-fail;
  detector-sampled candidates retain requested/routed accounting and may be
  skipped only when the subsequent saved-CTB audit passes. An unchanged rejected
  pass is never repeated.
- UVtools issue pixels are mapped in the decoded CTB raster's native row order
  (without a second image-origin flip); broad overhang feedback is sampled on
  actual exposed layer pixels rather than bounding-box centres.
- Basic encrypted-CTB marker validation.
- Native pre-encode layer evidence from Resin-Slicer is schema-checked and
  retained in every available slice receipt. It counts complete detached
  regions, records bounded support-footprint deficits with layer/area/bounds,
  and separates leading, internal, and trailing empty layers without allocating
  full-resolution analysis masks.
- Native post-encode CTB v5/GOO v3 evidence is also schema-checked and must
  reproduce the pre-encode audit exactly. Agent-Slicer records this as
  corroborating saved-file evidence; Linux UVtools remains the independent
  production authority for islands, traps, suction cups, bounds, and height.
- Transactional artifacts, resolved settings, plan, hashes, and reports.
- Rejected-build resumes retain previously verified native-raster feedback
  contacts across bounded cycles; legacy mirrored-coordinate receipts are not
  carried forward. The miniature profile keeps detector contacts at least
  0.24 mm apart and bounds the accumulated set at 360, so distinct residual
  pixels can be targeted without allowing retries to grow without limit.
- Explicit capability discovery and fail-closed planning.
- Exact command, executable hash, input hashes, and output hashes in build manifests.
- UVtools property checks plus island, overhang, resin-trap, suction-cup,
  bounds, print-height, and empty-layer validation.
- Persistent rejected CTB candidates and failed geometry reports.

## Deliberate limits

- The production hollowing policy retains the largest connected cavity and
  leaves smaller candidate pockets solid. It does not create multiple sealed
  cavities and hope that one drain pair serves all of them.
- Bottom drains are selected from the lowest cavity band. This is geometric,
  not semantic recognition of a character's feet; their coordinates remain in
  the report for review.
- Orientation is a deterministic bounded search, not a learned or global
  optimizer.
- Automatic supports are generated by the installed Resin-Slicer backend.
  Agent-Slicer can add validation-derived contacts through its private retry
  plan, but interactive support painting is not yet a stable project-schema
  feature.
- Outer-surface preservation is a conservative sampled vertex-neighbour check,
  not an exact Hausdorff-distance proof.
- Simplification surface evidence is a deterministic sampled bidirectional
  point-to-triangle measurement, not an exact Hausdorff proof. If no candidate
  satisfies every topology, deviation, bounds, and volume gate, the original
  prepared STL is retained.
- P2S machine, Bambu material, and process profiles can be resolved and audited,
  but the production FDM/Bambu Studio adapter is not implemented. FDM plans
  therefore remain blocked; profile availability does not imply sliced-output
  support.
- UVtools passing is detector evidence. It does not prove firmware acceptance,
  resin calibration, or a successful physical print.
- Resin-Slicer's native structural receipt observes rasters before proprietary
  serialization. It is supplementary evidence, not a replacement for the
  decoded-file UVtools gate until reader and detector parity is demonstrated.
- `geometry hollow-drain` remains the legacy approximate Blender experiment.
  Use `geometry prepare-resin` or a production project for validated hollowing.

## Quick start

```bash
python -m agent_slicer.cli tools
python -m agent_slicer.cli presets purposes
python -m agent_slicer.cli profiles recommend \
  --machine bambu-lab-p2s-0.4 \
  --material bambu-pla-basic-p2s \
  --purpose fdm-general \
  --quality p2s-0.20-standard

python -m agent_slicer.cli project init smoke \
  --directory projects/smoke \
  --machine test-small \
  --material standard-test \
  --purpose test-smoke \
  --output-format ctb

python -m agent_slicer.cli project add projects/smoke model.stl
python -m agent_slicer.cli plan projects/smoke
python -m agent_slicer.cli build projects/smoke
```

For normalized source meshes, declare physical size:

```bash
python -m agent_slicer.cli project add projects/miniatures model.stl \
  --target-height 65
```

Validated one-model preparation:

```bash
agent-slicer geometry prepare-resin model.stl \
  --output work/model-prepared.stl \
  --report work/model-prepared.json \
  --target-height 65 \
  --repair-method local-topology \
  --hollow-voxel 0.15 \
  --wall 1.8 \
  --drain-diameter 3.5 \
  --drain-count 2
```

Standalone measured reduction is also available for inspection or tuning:

```bash
agent-slicer geometry simplify model-prepared.stl \
  --output model-reduced.stl \
  --report model-reduced.json \
  --target-triangles 350000
```

Repair now preserves valid surfaces and fixes normals, duplicate/degenerate
triangles, trivial holes, and bounded non-manifold neighborhoods. It does not
smooth or remesh the model. Larger or ambiguous defects fail for review.
`--repair-method measured-voxel` explicitly selects the older lossy reconstruction;
it is never an automatic repair fallback.

Hollowing voxelizes only the inner cavity and subtracts it from the original
surface. Drains have bounded depths based on the local underside; the operation
fails if two separated, short drains cannot be placed. Grid connectivity and
actual mesh connectivity are both checked. See [repair behavior](docs/LOCAL_MESH_REPAIR.md).

Simplification is separate and disabled by default for the high-detail preset.
The project pipeline invokes it only when explicitly enabled in the resolved
purpose settings. Preparation cache v3 rejects older reconstructed artifacts.

For a complete Mars 3 Pro build, initialize a project with
`miniature-display-high-detail`, add all source STLs with explicit target
heights, run `plan`, and only then run `build`. A build that retains any UVtools
production issue exits with status 3 and stores the candidate under
`artifacts/rejected/`; it is never labeled complete.

After correcting a slicing/support implementation without changing geometry,
an exact rejected build can be resumed without repeating lossy preparation:

```bash
agent-slicer build projects/miniatures \
  --reuse-prepared \
  --resume-rejected
```

Cached preparation and simplification are accepted only when source hash, every
requested geometry setting, output hash, and the applicable topology/repair/
drain contracts still match. Resume additionally requires the
newly combined plate hash to equal the rejected build's plate hash and
re-analyzes the rejected CTB before generating contacts.

### Native WSL validation

On WSL, backend discovery prefers the isolated Linux executable at
`~/development-tools/uvtools/current/UVtoolsCmd` over both `PATH` and the
legacy Windows installation. Override it per invocation with `--uvtools` or
persistently with `AGENT_SLICER_UVTOOLS`. Large build transactions and decoded
layers default to `~/development-tools/agent-slicer-work`, not `/mnt/c`; use
`--work-root` or `AGENT_SLICER_WORK_ROOT` to select another native filesystem
location.

UVtools detectors run one at a time in separate processes. This preserves the
4.4.3 detector semantics while preventing multiple decoded 4K layer stacks
from occupying RAM concurrently. The requested worker count is retained in the
audit metadata for compatibility, but detector execution remains serial.

Agent-Slicer sets `.NET`'s `DOTNET_GCConserveMemory=5` for UVtools unless that
variable is already set. Override the Agent-Slicer default with
`AGENT_SLICER_UVTOOLS_GC_CONSERVE=0..9`; an explicit
`DOTNET_GCConserveMemory` takes precedence. Builds and analyses also share a
cross-process heavy-job lock, so separate local agents wait instead of loading
the same large plate simultaneously. Configure the lock with
`AGENT_SLICER_RESOURCE_LOCK` and its wait timeout with
`AGENT_SLICER_RESOURCE_WAIT_SECONDS`.

High-detail resin slicing has a three-hour subprocess timeout by default so a
measured full-plate job is not killed by the older 30-minute ceiling. Override
it with `AGENT_SLICER_RESIN_SLICE_TIMEOUT_SECONDS=60..86400` when a different
bounded operational limit is required.

Agent-Slicer also supplies Resin-Slicer with a persistent, content-addressed
support-plan cache under
`~/development-tools/agent-slicer-work/resin-support-plans`. Exact repeats skip
automatic support analysis and raft projection; feedback retries may reuse a
verified manual-contact prefix while routing only the appended contacts. The
Resin-Slicer receipt must contain an allowed cache status and finite phase
timings or the build fails closed. The final CTB is still decoded and validated;
a cache hit is never treated as production evidence by itself.

```bash
export AGENT_SLICER_UVTOOLS="$HOME/development-tools/uvtools/current/UVtoolsCmd"
export AGENT_SLICER_WORK_ROOT="$HOME/development-tools/agent-slicer-work"
export AGENT_SLICER_RESIN_WORKERS=1
agent-slicer analyze model.ctb --output model.analysis.json
agent-slicer build projects/miniatures --reuse-prepared --resume-rejected
```

Each analysis records the resolved executable path, executable SHA-256, core
version, runtime type, work root, requested worker count, scan strategy, and GC
conservation setting. Publishing final CTBs and reports remains transactional
even when the work root and project are on different filesystems.

## Tests

```bash
python -m unittest discover -s tests -v
python tests/e2e_real.py --format ctb --keep work/e2e-ctb
python tests/e2e_real.py --format goo --keep work/e2e-goo
```

The real E2E test creates its own cube fixture, prepares the plate, invokes the
installed Resin-Slicer, writes CTB, and validates the encrypted CTB marker. It
does not communicate with a printer.

CTB output from the smoke fixture is byte-reproducible in the current backend.
GOO contains a creation timestamp, so repeated GOO output differs in that header
field even when geometry and settings are unchanged.

## Design

See:

- `docs/architecture.md`
- `docs/roadmap.md`
- `docs/presets.md`
- `docs/PRINTER_PROFILES_2026-08-30-v1.md`
- `docs/CHITUBOX_PARITY_2026-08-30-v1.md`
- `docs/python-api.md`
- `docs/implementation-status.md`
- `AGENTS.md`
