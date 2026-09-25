# CHITUBOX capability and performance audit

Version: 1  
Date: 2026-08-30  
Scope: Agent-Slicer and Resin-Slicer resin workflow

## Conclusion

The workflow is materially faster for repeated and refinement slices, and its
CLI, Python integration, provenance, and fail-closed UVtools validation are
production-useful. It is not yet an honest feature-for-feature or measured
speed peer of CHITUBOX. The current release has stronger automation receipts
than a typical interactive slicer workflow, but still lacks several mature GUI
slicer features listed below.

No local CHITUBOX installation was found and no proprietary binary was installed
because that would require a separate vendor/EULA decision. Therefore the speed
numbers in this report compare Resin-Slicer before and after the optimization on
the same machine and input. They are not CHITUBOX-versus-Resin-Slicer numbers.

## Official CHITUBOX reference surface

The comparison was refreshed against current official documentation before the
implementation:

- [Printer/file compatibility](https://docs.chitubox.com/en-US/chitubox/latest/introduction)
  lists Elegoo Mars 3 Pro output as CTB.
- [Support your model](https://docs.chitubox.com/en-US/chitubox/latest/support-your-model)
  documents automatic, manual, batch, and magic support workflows; adaptive,
  vertical, and upright placement; multiple support structures; and spacing.
- [Model editing](https://docs.chitubox.com/en-US/chitubox/latest/model-editing)
  documents inner/outer/bottom-through hollowing, several infill structures,
  and drilling.
- [Risk detection](https://docs.chitubox.com/en-US/chitubox/latest/risk-detection)
  documents overhang, island, cavity, and collision checks.
- [Orientation and layout](https://docs.chitubox.com/en-US/chitubox/latest/orientation-and-layout)
  documents automatic orientation, automatic layout, and multi-platform work.
- [Print settings](https://docs.chitubox.com/en-US/chitubox/latest/configure-print-settings)
  documents grayscale/blur anti-aliasing, tolerance/bottom compensation,
  masks, and two-stage motion settings.
- [Export](https://docs.chitubox.com/en-US/chitubox/latest/export) documents
  slicing by model, height, and cross-sectional area.

CHITUBOX's own documentation also says support generation can take minutes on
complex models and that volume calculation can slow slicing. Those statements
show that speed depends on the job; they do not provide a quantitative public
benchmark suitable for a parity claim.

## Implemented performance work

### Resin-Slicer

- Binary STL loading now records exact bounds during the existing validity scan.
- Scale/translation transforms carry mathematically exact transformed bounds,
  avoiding repeated full triangle scans during preparation.
- A persistent support-plan cache keys the source SHA-256, source size,
  support-planner implementation fingerprint, transform, printer/layer
  geometry, and every support setting.
- Exact cache hits reuse automatic contacts, routes, trees, braces, pad geometry,
  and full-resolution raft/projection masks.
- Manual-contact prefix hits rebuild collision occupancy and extend the verified
  plan with only the appended contacts. The final topology is reassigned and
  revalidated.
- Cache entries use a strict JSON schema rather than executable serialization.
  Numeric limits, graph references, counts, mask dimensions, mask SHA-256, and
  document SHA-256 are checked before use.
- Writes are atomic and synchronized; stale/corrupt entries trigger full
  recomputation; pruning is bounded to 30 days and 32 entries.
- Slice receipts expose cache status and preparation, support, output-writing,
  and total pipeline timings.

### Agent-Slicer

- Every Resin-Slicer attempt receives the native-Linux persistent cache path
  `~/development-tools/agent-slicer-work/resin-support-plans` by default.
- The exact cache path, decision, and timing evidence are retained in build
  manifests.
- Unknown cache states, absent timing fields, non-finite values, or inconsistent
  totals fail the slicing stage.
- Cache reuse does not bypass decoded CTB validation or UVtools production
  gates.

## Reproducible benchmark

Input and configuration:

- Existing user STL copied to native Linux storage; no random model downloaded.
- 373,722 triangles, 18,721,884 bytes.
- Mars 3 Pro resin profile, scale 0.08, 0.05 mm layer height.
- 203 output layers with automatic primary support analysis.
- Single process, `/usr/bin/time -v`, same Resin-Slicer checkout and virtual
  environment.
- Benchmark artifacts:
  `/home/rschmeitz/development-tools/agent-slicer-work/chitubox-parity-2026-08-30`.

| Series | Wall times | Median wall | Peak RSS values | Median peak RSS |
| --- | ---: | ---: | ---: | ---: |
| Pre-change baseline | 19.58 s, 20.60 s | 20.09 s | 235,756 KiB, 235,824 KiB | 235,790 KiB |
| Post-change cold cache | 14.57 s, 21.29 s, 27.07 s | 21.29 s | 235,996 KiB, 235,168 KiB, 235,812 KiB | 235,812 KiB |
| Post-change exact warm cache | 7.35 s, 13.71 s, 8.31 s | 8.31 s | 128,552 KiB, 127,660 KiB, 127,796 KiB | 127,796 KiB |

The exact-repeat median improved by 58.6%, and median peak RSS improved by
45.8%. Cold runs varied substantially while unrelated workloads were active and
did not establish an improvement. The cache's effect itself is visible in the
phase receipt: exact-hit support planning took 0.000007 to 0.000017 seconds in
these trials, versus 7.46 to 14.31 seconds on cold runs.

All baseline, cold, and exact-hit CTBs had the same SHA-256:

`226cc99b0dc5c48bd2bff93f5ff61a6541bc2c51c6c39cb4733276a1e3d1515b`

After adding the final implementation-fingerprint and graph-bound checks, one
additional cold/warm pair measured 11.82 s / 5.49 s and 236,180 KiB / 128,188
KiB respectively, with the same CTB SHA-256. It is a final-code verification
pair, not folded into the three-run warm median above.

A real manual-feedback prefix run and a complete independent replan also
produced byte-identical output:

`c1d8ed356117fe24bd5753c4ce980413e8696c4fed7b0e98220d2851ed0f70a3`

The small prefix case was not faster in wall time under the sampled system
load, so prefix reuse is accepted on correctness evidence, not advertised as a
universal speed win.

## Capability comparison

| Capability | Current workflow | Assessment versus documented CHITUBOX surface |
| --- | --- | --- |
| Mars 3 Pro output | Native CTB v5 writer, Mars 3 Pro profile, decoded structural/property validation | Implemented in software; firmware/physical acceptance remains untested |
| Other Elegoo output | Native GOO V3 and CTB v5 | Useful subset; not the full CHITUBOX printer/format catalog |
| Automatic supports | Island/overhang analysis, real-pixel contacts, collision routes, trees, load-thickened trunks, pedestals, enforcers, braces, and pad | Strong automated subset; fewer placement/shape modes and interactive tools |
| Support refinement | Manual contacts, GUI painting in Resin-Slicer, UVtools-derived Agent-Slicer retries, exact/prefix cache | Agent project schema does not yet persist painted regions or batch/magic editing |
| Repair | Measured Blender repair, native topology/round-trip/deviation gates, safe simplification | Strong automated evidence; not a broad interactive mesh editing suite |
| Hollow/drain | One connected cavity, wall-resolution gate, two proven drains, exterior flood-fill proof | Safer bounded subset; no multi-cavity graph or selectable lattice infill |
| Orientation/layout | Bounded deterministic orientation and support-aware packing | Partial; no global optimizer or multi-platform editor |
| Risk checks | Independent decoded CTB checks for islands, overhangs, resin traps, suction cups, empty layers, bounds, and height | Strong unattended validation path |
| Exposure/motion | Layer height, bottom/normal exposure, bottom count, lift/retract distances and speeds, two-stage format fields | Core controls implemented |
| Anti-alias/tolerance | Binary exposure masks | Gap: grayscale/blur AA and dimensional/bottom compensation |
| Advanced export | Full-plate CTB/GOO | Gap: slice by model/height/area and multi-platform project export |
| Automation | Stable CLI, Python API, typed projects, manifests, hashes, resumability | Stronger than a GUI-only workflow for agent control |

## Verified quality gates

- Resin-Slicer: 103 tests passed.
- Agent-Slicer: 77 tests and 11 subtests passed; 50 third-party deprecation
  warnings were reported by scikit-image/NumPy and did not fail the suite.
- Resin-Slicer Electron syntax and production Vite build passed; the high-level
  npm dependency audit reported zero vulnerabilities.
- Python byte-compilation and `git diff --check` passed in both repositories.
- Real Agent-Slicer CTB E2E: passed, with support-cache receipt and encrypted CTB
  marker check. A second build reported `hit-exact`, reduced support-planning to
  0.000003 seconds, and reproduced SHA-256
  `5e2863021f2aca7d72af4fd07c54b5c0e069bcd1f2d86692549c2a938d26dd3c`.
- No printer communication or firmware action occurred.

## Work still required before a parity claim

1. Add a compiled/vectorized contour and raster kernel, then benchmark the same
   public models and settings against a user-authorized CHITUBOX installation.
2. Implement grayscale/blur anti-aliasing plus inner, outer, and bottom
   dimensional compensation with decoder round-trip tests.
3. Add a connected multi-cavity graph, independent drain/vent proof per cavity,
   and selectable validated hollow infill.
4. Add persistent support painting/editing to the project schema plus more
   support shape/placement presets and batch operations.
5. Add slice-by-model/height/area, multi-platform projects, 3MF interchange, and
   a broader tested printer-format matrix.
6. Keep physical printer acceptance and resin calibration as explicit human
   gates; software parity cannot prove either one.
