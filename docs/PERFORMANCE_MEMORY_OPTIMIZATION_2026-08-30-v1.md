# Agent-Slicer / Resin-Slicer performance and memory optimization

Date: 2026-08-30  
Agent-Slicer base commit: `fe68f97f4ceee33d2256792bfbf2a77361c9d4cb`  
Resin-Slicer base commit: `b7438ddbd66c4f714bfc0031a1f1d3787dbf011f`  

Both repositories already contained uncommitted production-workflow changes.
Those changes were preserved. No printer or firmware operation was performed.

## Outcome

- Large binary STL files are memory-mapped and transformed lazily.
- The 9,092,809-triangle Tentoonstelling plate geometry can be loaded, bounded,
  layer-indexed across 3,502 layers, and sampled at a measured peak RSS of
  867,872 KiB. Its source index uses 36,371,236 bytes and its layer index uses
  140,040,836 bytes.
- Resin-Slicer defaults to one layer worker. A worker override remains
  available, but extra Python threads retain extra full-resolution rasters and
  did not improve the measured GIL-bound path.
- Surface-normal planning uses a deterministic bounded sample (250,000
  triangles by default) rather than allocating a normal/index record for every
  triangle.
- Agent-Slicer serializes memory-intensive builds and UVtools analyses across
  local processes with a re-entrant, kernel-released file lock.
- UVtools detectors remain separate because combining flags changes UVtools
  4.4.3 results. They run sequentially and use the measured default
  `DOTNET_GCConserveMemory=5`.
- Lightweight Agent-Slicer CLI startup no longer imports NumPy/SciPy, the full
  geometry pipeline, or UVtools analysis. Measured `--help` startup changed
  from approximately 14.4 seconds to 0.20 seconds and 13,440 KiB peak RSS.
- Resin-Slicer is installed editable in its existing isolated virtual
  environment. Agent-Slicer fingerprints the actual editable source package in
  every slice receipt.

## Production CTB benchmark

Exact input:

`Tentoonstelling_100mm_FINAL_MARS3PRO.ctb`

SHA-256:

`eb58c9da9e46a3aace44a5df238f680105c654074e34a42d23b4728944a703f7`

| Path | Wall time | Peak memory | Result |
|---|---:|---:|---|
| Prior audited full analysis | 9:07 | 13,487,464,448-byte service peak | production passed |
| Sequential isolated detectors, no GC policy | 10:31.77 | 11,942,756 KiB max RSS | production-equivalent |
| Final sequential isolated detectors, GC policy 5 | 9:58.76 | 5,485,492 KiB max RSS; 5,675,782,144-byte cgroup peak | production passed; no swap |

Against the prior audited parallel analysis, the final path reduces cgroup peak
memory by 57.9% at a 9.5% wall-time cost. Against the directly comparable
sequential path, it reduces max RSS by 54.1% and improves wall time by 5.2%.

The final analysis found 282 raw islands, 12 raw overhangs, 137 raw resin-trap
records, and zero suction-cup, touching-bound, print-height, or empty-layer
records. Production evaluation passed with zero actionable records in every
category. The prior audited run reported 143 resin-trap records and a repeated
sequential run reported 142. The differences are UVtools run-to-run grouping of
tiny 3-14 pixel specks; every variant is far below the configured 0.5 mm3
action threshold. Island and overhang records match the audited run exactly.

Combining detector flags was explicitly rejected: the all-flags experiment
changed island, trap, and suction results, while grouping four housekeeping
flags produced 49 false touching-bound records instead of zero.

Evidence is stored on the native WSL filesystem:

- `/home/rschmeitz/development-tools/agent-slicer-work/optimization-gc-full-2026-08-30/analysis.json`
- `/home/rschmeitz/development-tools/agent-slicer-work/optimization-gc-full-2026-08-30/production-validation.json`
- `/home/rschmeitz/development-tools/agent-slicer-work/optimization-gc-full-2026-08-30/time.txt`

## Implementation changes

### Resin-Slicer

- `resin_slicer/mesh.py`: compact memory-mapped binary STL sequence, lazy
  transformed triangle sequence, streaming/cached bounds.
- `resin_slicer/slicing.py`: compact 32-bit triangle indices per layer instead
  of repeated Python triangle references.
- `resin_slicer/transform.py`: lazy transforms.
- `resin_slicer/supports.py`: bounded deterministic surface-normal sampling.
- `resin_slicer/pipeline.py` and `resin_slicer/cli.py`: one memory-safe layer
  worker by default.

### Agent-Slicer

- `agent_slicer/resources.py`: cross-process heavy-job lock, re-entrant for
  nested build/analysis calls and automatically released on process exit.
- `agent_slicer/pipeline.py`: heavy-build guard, one Resin-Slicer worker by
  default, and editable-package source fingerprinting.
- `agent_slicer/uvtools_adapter.py`: isolated serial detectors, bounded worker
  metadata, configurable .NET GC conservation, and recorded scan provenance.
- `agent_slicer/cli.py`: command-local imports for fast low-memory help/version
  and lightweight commands.

Configuration knobs:

```bash
export AGENT_SLICER_WORK_ROOT="$HOME/development-tools/agent-slicer-work"
export AGENT_SLICER_RESIN_WORKERS=1
export AGENT_SLICER_UVTOOLS_GC_CONSERVE=5
# Optional: explicit .NET setting overrides the Agent-Slicer GC default.
# export DOTNET_GCConserveMemory=5
# Optional lock and six-hour default wait timeout:
# export AGENT_SLICER_RESOURCE_LOCK="$AGENT_SLICER_WORK_ROOT/.heavy-job.lock"
# export AGENT_SLICER_RESOURCE_WAIT_SECONDS=21600
```

For direct Resin-Slicer use:

```bash
export RESIN_SLICER_LAYER_WORKERS=1
# Optional bounded normal sample, valid range 1000..1000000:
# export RESIN_SLICER_SURFACE_NORMAL_SAMPLES=250000
```

## Verification

- Agent-Slicer: 48 tests passed.
- Resin-Slicer: 77 tests passed.
- `compileall` and `git diff --check`: passed in both repositories.
- Cross-process lock test: a second process waited and timed out without
  entering the heavy section while the first process held the lock.
- Final repository smoke E2E: passed; 160-layer CTB, three supports, basic CTB
  marker validation passed, 15.30 seconds wall time, 92,424 KiB peak RSS.
- Smoke CTB SHA-256:
  `0c48506ae293e464ab41b76ec7beba1d7a011dbca3df4b18071ad95c1d0e9898`.
- Slice receipt implementation SHA-256:
  `78d7aa3663006322df7e5f1af9b4b00b589b5c0f8061d3c29b9cf863f072ddca`.

## Limitations

- The existing audited production CTB was revalidated but intentionally not
  overwritten or sent to a printer.
- A new full 38-minute-class Tentoonstelling re-slice was not performed solely
  for this performance change. The real plate's geometry/index stage and the
  exact existing CTB's full production validation were measured separately.
- Raw UVtools resin-trap speck grouping is not bit-for-bit deterministic across
  runs. Production decisions remain equivalent because all differing records
  are far below the explicit action threshold.
- One worker prioritizes predictable memory use. A measured machine with more
  RAM can opt into more Resin-Slicer workers, but local Agent-Slicer heavy jobs
  remain serialized by design.
