# CLI/agent engineering and real slicer benchmarks — v1

Date: 2026-09-07–08, Europe/Amsterdam. All execution was native Ubuntu 24.04
WSL2 x86-64. This is a tested engineering improvement, **not a claim of full
CHITUBOX capability, support-quality parity, or physical printability**.

## Code and tool locations

- Agent-Slicer: `/home/rschmeitz/development/agent-slicer`, `.venv/bin/agent-slicer`,
  package 0.3.0; existing dirty base `fe68f97f4ceee33d2256792bfbf2a77361c9d4cb`.
- Resin-Slicer: `/home/rschmeitz/development/Resin-Slicer`, `.venv/bin/resin-slicer`,
  package 0.1.0; existing dirty base `b7438ddbd66c4f714bfc0031a1f1d3787dbf011f`.
- Blender 4.5.13 LTS: `/home/rschmeitz/development-tools/blender/current/blender`.
- UVtools core 4.4.3, command 2.0.1: `/home/rschmeitz/development-tools/uvtools/current/UVtoolsCmd`.
- PrusaSlicer 2.8.1: `/home/rschmeitz/development-tools/slicer-benchmarks/prusa-2.8.1/squashfs-root/usr/bin/bin/prusa-slicer`.
- SuperSlicer 2.5.59.13: `/home/rschmeitz/development-tools/slicer-benchmarks/super-2.5.59.13/superslicer`.

Existing edits, historical/audited CTBs, Windows tools and original models were
preserved. No printer communication or firmware change occurred. No global
Python packages or Linux system packages were installed.

## Implemented changes

1. **Installable Blender adapter.** Moved the owned Blender scripts into
   `agent_slicer/data/blender`, included them in wheel package data, and resolved
   them from the installed package. Previously a non-editable wheel could not
   find repair scripts outside the package. Added wheel smoke installation to CI.
2. **Bounded backend execution.** `agent_slicer/processes.py` runs argv without a
   shell; disk-backed logs retain only 64 KiB per stream in JSON receipts.
   Timeout, cancellation and optional memory-watchdog failures terminate the
   Linux process group, including descendants. Blender and Resin adapters use it.
   Benchmark runs retain complete stdout/stderr logs in exclusive output files.
3. **Resource controls.** Default two Blender/numerical-library threads;
   configurable sampled RSS watchdog; the heavy-job lock now also wraps direct
   preparation/hollow API calls and benchmarks. Hollowing rejects oversized
   grids using a conservative 80-byte/cell estimate and default 1 GiB grid budget;
   releases distance/label/mask arrays earlier. Geometry is never silently coarsened.
4. **Inspect before remeshing.** Small convex solids may bypass lossy remeshing
   only after closed/manifold/contiguous winding, single-body, positive-volume,
   nondegenerate-face and face-half-space checks. The check is bounded to 256
   vertices/faces. Complex or suspect geometry retains measured voxel repair.
   A real Blender cube test preserves 12 faces before hollowing, versus the
   previous remesh's 122,412 exported triangular faces. Inward-wound input still
   takes the repair path. This is a narrowly verified optimization, not a general
   self-intersection certifier.
5. **Adaptive slicing index.** `Resin-Slicer/resin_slicer/slicing.py` uses exact
   per-layer triangle buckets for fine meshes when bounded to 2 million uint32
   references, eight references/active triangle on average, and 8,192 layers.
   Tall faces keep 64-layer compact blocks. Fine miniature triangles no longer
   incur dozens of unsuccessful interval tests per slice plane. Geometry,
   support generation, output encoding and validation semantics are unchanged.
6. **Agent input contracts.** Reject non-finite/ambiguous target heights and
   fractional/boolean quantities; reject source/output/report path aliases.
   Heavy-job lock timeouts reject NaN/infinity.
7. **Repeatable benchmark interface.** `agent-slicer benchmark` validates input
   checksums, records versions/argv/timing/output hashes, checkpoints each run,
   and refuses existing output roots. Optional implementation-file hashes detect
   code changing during a benchmark. Companion scripts normalize an existing
   STL, decode CTB/SL1 output through a shared structural diagnostic, and perform
   alternating old/new-index experiments.

## Official reference installations

Read and used the official [Prusa CLI documentation](https://github.com/prusa3d/PrusaSlicer/wiki/Command-Line-Interface),
[Prusa Linux installation guide](https://help.prusa3d.com/article/install-prusaslicer_1903),
[Prusa 2.8.1 release](https://github.com/prusa3d/PrusaSlicer/releases/tag/version_2.8.1),
and [SuperSlicer release](https://github.com/supermerill/SuperSlicer/releases/tag/2.5.59.13).
Both engines' actual `--help`/`--help-sla` and saved SL1 configurations were checked.
The initial SuperSlicer run rejected Prusa's `--elefant-foot-compensation` spelling;
its verified option is `--first-layer-size-compensation`. The failed attempt is
retained under `baseline-v1`, excluded from reported comparisons.

Prusa's current stable release was 2.9.6 at inspection and uses Flatpak on Linux.
**2.9.6 was not benchmarked.** The isolated official 2.8.1 AppImage was extracted
without FUSE. Prusa/SuperSlicer share ancestry, so these are not two independent
algorithm families. No AGPL slicer source was copied into this MIT project.

The Prusa binary required isolated copies of WebKitGTK, JavaScriptCoreGTK,
libsoup, libxslt, GStreamer base/GL, harfbuzz-icu, manette, enchant, hyphen,
secret and orc. Ubuntu `.deb` archives were downloaded and extracted under
`slicer-benchmarks/linux-libs`, not installed globally. Prusa's launcher resets
`LD_LIBRARY_PATH`, so the manifest invokes its actual ELF binary with the
isolated library directories explicitly supplied. SuperSlicer uses its wrapper.
`file` identifies Blender and both real slicer binaries as Linux **ELF 64-bit
x86-64**, using `/lib64/ld-linux-x86-64.so.2`.

No publisher release-asset checksum was available for these downloaded assets;
these locally computed hashes provide reproducibility, not independent authenticity:

```text
PrusaSlicer.AppImage
565f2f4bd4dbb05904a459d54db1916b6932124709c1d17b5aacfe9f5f2f1b03
SuperSlicer.tar.zip
6f5588b0467ed00eab6a1ad70d52f075ee65f862a455efcfc214f3e2c7781e1c
```

## Benchmark protocol and evidence

Evidence root:
`/home/rschmeitz/development-tools/agent-slicer-work/production-benchmark-2026-09-07`.

Important subdirectories: `inputs-v2`, `baseline-v2`, `optimized-v1`,
`paired-index`, `wheel-smoke-final`, `e2e-project`, `wheels-release`.

- Two complete 27-export series: three engines × three cases × three repeats.
- Existing 373,722-record user STL; no random model downloaded. A derived copy
  was uniformly scaled to **10 mm** height for a bounded benchmark, not 100 mm.
- Same normalized STL bytes supplied to all engines. Source SHA-256:
  `fb27519b3b53eac2a63985491f1c3d511879582875bfaa4e3ca9e455c705e5aa`.
  Normalized SHA-256:
  `3c04460a2eee975668698915dd0642713f737010ff37576d3d6a48bf8015405e`.
- Also used an authored 6 mm cube as a geometric control.
- Same 4098×2560 resolution, 143.43×89.6 mm bed, 0.05 mm first/normal layers,
  centered placement, binary masks/no anti-aliasing. No hollowing in the timing
  matrix. Supports-off and engine-default supports-on cases are separate.
- Supported models lifted 5 mm. Support shape/density defaults differ; CTB
  versus SL1 writers and integrated validation costs also differ. This is a
  **CLI workload comparison, not an equal-algorithm or equal-quality race**.
- Sequential fresh processes under CPU affinity `0,1`, two numerical threads,
  and a 2 GiB sampled backend RSS budget. Support cache disabled; OS page cache
  not flushed. Other desktop workloads varied. WSL has 15 GiB RAM / 8 GiB swap;
  available memory varied substantially, so small timing differences are noise.
- Timing includes process launch/export; post-run cross-engine decoding is
  excluded. Memory below is GNU time's process high-water RSS, not aggregate
  tree memory. Separate sampled tree RSS is also preserved in JSON.

### Optimized series: three-run medians

| Workload | Resin-Slicer time / peak RSS | PrusaSlicer 2.8.1 | SuperSlicer 2.5.59.13 |
| --- | --- | --- | --- |
| 6 mm cube, supports off | 0.83 s / 23.2 MiB | 2.12 s / 80.1 MiB | 1.58 s / 53.8 MiB |
| 10 mm miniature, supports off | 2.92 s / 46.7 MiB | 3.42 s / 142.7 MiB | 3.26 s / 117.0 MiB |
| 10 mm miniature, supports on | 7.75 s / 231.5 MiB | 7.67 s / 243.7 MiB | 6.22 s / 143.2 MiB |

Resin's first-series medians were 3.93 s without supports and 12.78 s with
supports. A separate alternating experiment, `block64/adaptive/adaptive/block64/
block64/adaptive`, kept all other current code fixed and measured **12.606 s
versus 10.038 s median: 20.37% faster**. Prefer that cautious attribution over
claiming the entire between-series 39% reduction came from the index change.
All six paired CTBs and all corresponding before/after Resin benchmark CTBs
were byte-identical. Paired supported CTB SHA-256:
`d1b3cc69b9b618412953ab1086700a939d48573e7a22237c1fb4ec31df5c541f`.

### Output quality and remaining support gap

Every repetition in `optimized-v1` was decoded. All repeated rasters within
each engine/case were identical. SL1 container hashes vary because timestamps
are embedded; canonical decoded raster hashes do not. The cube's **complete
120-layer raster stack is identical across all three engines**, including its
rasterized volume of 0.2174424 ml (analytical volume 0.216 ml; pixel quantization).

Miniature-only rasterized volumes are 0.00648319 / 0.00656435 / 0.00658346 ml
(Resin/Prusa/Super). They are not pixel-equivalent. Different mesh cleanup,
contour/raster precision and rounding are plausible causes, not an established
attribution. The full-sized exhibition scene was not rebuilt in this task.

The same strict, zero-pixel-radius connectivity diagnostic found:

| Supported miniature | Detached components / pixels | Material volume |
| --- | --- | --- |
| Resin-Slicer | 176 / 632 | 0.02664 ml |
| PrusaSlicer | 73 / 306 | 0.15052 ml |
| SuperSlicer | 69 / 225 | 0.16397 ml |

All had zero internal empty layers. These raw counts include subpixel-scale
features and are not equivalent to failed-print counts, but **support-quality
parity is not established**. Resin's default plan routed only four contacts and
reported two unrouted components. Reference defaults use substantially more
material/support structure. The timing matrix does not use Agent-Slicer's
production-purpose tuning and feedback retries; those would add time. Do not
print these benchmark files. The next quality work should target contact
coverage, support-analysis resolution and encoded-layer feedback using larger,
representative miniature fixtures—not weaker acceptance thresholds.

## Verification

- Agent-Slicer: **97 tests passed**, including two real Blender integration tests.
- Resin-Slicer: **120 tests passed**.
- Both existing environments: `pip check` passed. Compile checks and
  `git diff --check` passed. Some scikit-image/NumPy deprecation warnings remain.
- Wheel built and installed into a fresh isolated `wheel-venv`, then launched
  from `/tmp`. Imported scripts resolve inside its `site-packages`, not the
  checkout. Actual Blender repair/preparation/hollow/drain operation passed;
  both drains independently reach the cavity and sealed cavity voxels equal zero.
  The new convex path and rejection of inward-wound input have real regression tests.
  The final wheel's convex fast path kept 12/12 faces; its hollow STL is also
  byte-identical to the earlier remesh-based result on this cube:
  `9e01b14c7124b9a867a6e7d1bd14a8b504a420749a7ed52d90b86a10d2696cb3`.
- Release smoke wheel SHA-256:
  `43f61de6e05fa715283d3599e58cb69505d02d93282fda29f8da40088a8a573b`.
- End-to-end CLI/API smoke project: supported cube → CTB v5, 160 layers,
  4 supports, exact pre/post-encode audit parity, basic format validation passed.
  Independent native UVtools analysis reported zero islands, overhangs, resin
  traps, suction cups, empty layers, height issues and touching-boundary issues.
  This uses the small test printer preset, not a Mars 3 Pro production plate.
- Smoke CTB:
  `e2e-project/smoke-project/artifacts/smoke-project.ctb` under the evidence root;
  SHA-256 `5e2863021f2aca7d72af4fd07c54b5c0e069bcd1f2d86692549c2a938d26dd3c`.
  Independent evidence: `e2e-project/uvtools-analysis.json`.
- CI wheel checks were added; the hosted CI/Python 3.10/3.13 matrix was not
  remotely run. Local verification used Python 3.12.

## Use after reboot

```bash
cd /home/rschmeitz/development/agent-slicer
source .venv/bin/activate
export AGENT_SLICER_THREADS=2
export AGENT_SLICER_MAX_BACKEND_RSS_MB=2048
export AGENT_SLICER_MAX_HOLLOW_MEMORY_MB=1024
agent-slicer tools
agent-slicer benchmark --help
agent-slicer plan /native/path/to/project
agent-slicer build /native/path/to/project --reuse-prepared
agent-slicer gui /native/path/to/project
```

For a new benchmark evidence run (use a fresh directory every time):

```bash
taskset -c 0,1 agent-slicer benchmark \
  /home/rschmeitz/development-tools/agent-slicer-work/production-benchmark-2026-09-07/inputs-v2/suite.json \
  --output /home/rschmeitz/development-tools/agent-slicer-work/my-new-benchmark-v1 \
  --repeats 3
```

Benchmark manifests are trusted executable configuration, not safe untrusted
documents. Benchmark checkpoints preserve evidence but do not implement automatic
benchmark resumption; the existing production build resume workflow is preserved.
RSS sampling can miss brief peaks; it is not a cgroup/hard memory limit. The
hollow-grid estimate excludes mesh/runtime overhead. Full-size complex support
quality, multi-plate throughput, proprietary slicer parity, resin-specific
calibration, firmware acceptance and physical print tests remain unproven.
