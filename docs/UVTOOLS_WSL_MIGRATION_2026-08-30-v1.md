# UVtools WSL-native validation migration v1

Date: 2026-08-30  
Scope: Agent-Slicer / Resin-Slicer validation for the Elegoo Mars 3 Pro project  
Outcome: migrated for production validation, with the existing CTB still rejected by the unchanged validation contract

## Safety and precondition

The pre-existing Resin-Slicer/Agent-Slicer build had completed before this
migration started. No build, printer process, firmware, or physical printer was
interrupted or changed. The existing audited build and Windows UVtools
installation were retained.

## Host inspection

- Distribution: Ubuntu 24.04.3 LTS (`Ubuntu-24.04`)
- Kernel: `6.6.87.2-microsoft-standard-WSL2`
- Architecture: `x86_64`
- RAM: 15 GiB visible to WSL
- Swap: 8 GiB
- CPU: Intel Core i7-12700H, 20 logical CPUs
- Linux .NET: not installed
- Existing Windows UVtools: `C:\Program Files\UVtools\UVtoolsCmd.exe`
- Existing Windows UVtools core: 4.4.3
- Existing Windows executable type: PE32+ x86-64
- Existing build tools: Git, Python 3, pip, GCC, G++, Make, CMake, curl, wget
- `unzip`: not installed; the standard-library `python3 -m zipfile` extractor was
  used instead, so no global package was added.

## Official guidance and selected release

The current official UVtools README was read before installation. It documents
the Linux installer, manual dependencies, self-contained launch commands, and
the `UVtoolsCmd` interface:

- <https://github.com/sn4k3/UVtools#to-auto-install-on-linux>
- <https://github.com/sn4k3/UVtools#requirements>
- <https://github.com/sn4k3/UVtools#command-line>

The official Linux x64 ZIP for UVtools 4.4.3 was selected from:

- <https://github.com/sn4k3/UVtools/releases/tag/v4.4.3>

UVtools 4.4.3 was selected deliberately rather than the newest release because
the retained Windows validator also reports core version 4.4.3. This gives the
cross-platform parity comparison the same detector implementation and rules.

The release did not publish an official checksum/digest. The downloaded asset
was therefore not represented as officially checksum-verified. Its locally
recorded SHA-256 is:

```text
b2aa99d50456350919525663f7d71fc705f98e57886aa0ff503d128d75a9f250  UVtools_linux-x64_v4.4.3.zip
```

## Installation

- Versioned directory: `/home/rschmeitz/development-tools/uvtools/4.4.3`
- Stable link: `/home/rschmeitz/development-tools/uvtools/current`
- Exact command: `/home/rschmeitz/development-tools/uvtools/current/UVtoolsCmd`
- Executable SHA-256: `e2d00913d1818bbd9524fad027471bedb4b375e2dcc64d9de7e51e6e7134bdf3`
- CLI framework version: `2.0.1`
- UVtools core version: `4.4.3`
- Global dependencies installed: none

The package is self-contained. The absence of a Linux `dotnet` installation did
not prevent execution, and every dependency shown by `ldd` resolved to an
existing Ubuntu base library.

### Linux-native proof

`file` reports:

```text
ELF 64-bit LSB pie executable, x86-64, dynamically linked,
interpreter /lib64/ld-linux-x86-64.so.2, for GNU/Linux 2.6.32
```

`readelf -h` reports `ELF64`, `UNIX - GNU`, and `Advanced Micro Devices X86-64`.
During the Linux validation, the service cgroup contained only the Python
adapter and `/home/rschmeitz/development-tools/uvtools/4.4.3/UVtoolsCmd` ELF
children. No `.exe`, Wine, PowerShell, or Windows UVtools process participated.

Both required commands executed:

```bash
/home/rschmeitz/development-tools/uvtools/current/UVtoolsCmd --version
/home/rschmeitz/development-tools/uvtools/current/UVtoolsCmd --help
```

UVtools 4.4.3 prints help correctly but returns exit status 1 for `--help`; this
is upstream CLI behavior, not an execution failure.

## Candidate identity

The exact candidate used for both reports is:

```text
SHA-256: d8e44b28c6a061de71674c11ce83318c9fe4e3909776da3543808619b7217e81
Size:    348,588,895 bytes
Format:  CTB v5
Machine: ELEGOO Mars 3 Pro
Layers:  3,502 at 0.03 mm
Height:  105.06 mm
```

The source under OneDrive and the ext4 validation copy were hashed separately
and matched exactly.

## Windows/Linux parity

The completed historical Windows analysis embedded in the existing build
manifest was compared with the completed Linux analysis. Both identify the
same CTB SHA-256 above and UVtools core 4.4.3.

| Check | Windows | Linux | Production-equivalent |
|---|---:|---:|---|
| Raw islands | 352 | 352 | yes |
| Actionable islands | 63 | 63 | yes |
| Raw overhangs | 29 | 29 | yes |
| Actionable overhangs | 13 | 13 | yes |
| Raw resin traps | 4,227 | 4,217 | no, explained below |
| Actionable resin traps | 1 | 1 | yes |
| Largest actionable trap | 0.71688225 mm³ | 0.71688225 mm³ | yes |
| Suction cups | 0 | 0 | yes |
| Touching bounds | 0 | 0 | yes |
| Print-height issues | 0 | 0 | yes |
| Empty layers | 0 | 0 | yes |
| Resolution | 4098 x 2560 | 4098 x 2560 | yes |
| Layer height | 0.03 mm | 0.03 mm | yes |
| Exposure / bottom exposure | 2.5 s / 30 s | 2.5 s / 30 s | yes |
| Bottom layers | 5 | 5 | yes |
| Final production status | rejected | rejected | yes |

The actionable island records, actionable overhang records, and actionable
resin-trap record are identical, including layers, pixel counts, bounds, and
contact evidence. All property and structural gates agree.

### Explained non-production differences

The platform builds partition a number of tiny resin-trap connected components
differently. Comparing record signatures finds 92 Windows-only and 82
Linux-only raw fragments; the largest differing Linux fragment is 1,502 voxel
samples, approximately 0.0552 mm³ at this CTB's voxel/layer dimensions. This is
well below the configured 0.5 mm³ actionable threshold. It changes the ignored
raw count by ten but does not change any actionable record or the production
decision.

UVtools reports estimated material as 96.501 ml on Windows and 96.498 ml on
Linux, a 0.003 ml difference. Material estimate is not a production acceptance
gate. Windows also emits replacement characters for superscript units while
Linux emits `px²`/`px³`; parsed numeric evidence is unaffected.

Because the complete production contract and all actionable evidence agree,
Agent-Slicer was switched to the Linux-native default. Raw detector totals must
not be treated as bit-identical across platforms.

## Runtime and memory

Both measured paths used two UVtools workers and the same SHA-256 candidate.
The Linux input and all intermediates were on ext4. Two current Windows reruns
were attempted: one read the ext4 CTB through WSL UNC, and one used the original
local `C:` copy. Both were stopped by the unchanged 1,800-second per-detector
timeout in `--resin-traps`; neither produced a replacement Windows report.

| Path | Result | Wall time | Peak memory |
|---|---|---:|---:|
| Linux-native, ext4 input | completed analysis; status 3 because findings remain | 10:26.84 | 12,365,639,680 bytes (11.52 GiB) cgroup peak |
| Windows UVtools, local `C:` input | resin-trap timeout | >33:49.40 | 17,551,818,752 bytes (16.35 GiB) aggregate working-set peak |
| Windows UVtools, ext4 via UNC | resin-trap timeout | >33:42.75 | 11,582,877,696 bytes (10.79 GiB) aggregate working-set peak |

For the natural old Windows path, Linux completed in less than 31% of the
censored Windows duration (at least 3.24x faster) and used about 29.5% less peak
memory. The Linux cgroup also recorded a 1.4 GiB swap peak during the initial
two-worker scans. A discarded legacy four-worker Linux attempt reached 15.76 GB
and heavy swap, so the WSL default is now two workers.

## Agent-Slicer changes

Agent-Slicer base commit: `fe68f97f4ceee33d2256792bfbf2a77361c9d4cb`
(working tree already contained the ongoing resin-workflow implementation and
remains intentionally uncommitted).

- `AGENT_SLICER_UVTOOLS` and `--uvtools` explicitly configure the executable.
- WSL discovery now prefers
  `~/development-tools/uvtools/current/UVtoolsCmd`, then `PATH`, while retaining
  the Windows executable as a fallback.
- `AGENT_SLICER_WORK_ROOT` and `--work-root` configure the native work root.
- WSL defaults large transactions and UVtools extraction to
  `~/development-tools/agent-slicer-work`.
- Cross-filesystem publication uses a destination-side temporary file and
  atomic replace, so final artifacts can still be written to OneDrive safely.
- `AGENT_SLICER_UVTOOLS_WORKERS` and `--uvtools-workers` configure concurrency;
  WSL defaults to two.
- Analysis receipts now record executable path/hash, core version, ELF/PE
  runtime, worker count, and work root.
- Layer evidence now performs one bounded range extraction and reads only
  issue bounding-box crops, instead of reopening the CTB up to 128 times and
  retaining full-resolution arrays.
- Build and analyze CLI paths pass the same explicit UVtools/work-root settings.
- Documentation, changelog, architecture notes, and focused discovery/runtime
  tests were updated.

## Tests and end-to-end validation

- Agent-Slicer: 42 tests passed.
- Resin-Slicer: 75 tests passed.
- Native smoke E2E: passed; Resin-Slicer produced a 160-layer CTB with three
  supports, and Agent-Slicer basic CTB validation passed.
- Existing-project E2E validation: completed on the 348.6 MB candidate using
  Linux-native UVtools; extraction, islands, overhangs, resin traps, suction
  cups, touching bounds, print height, empty layers, properties, exact layer
  evidence, and production-contract evaluation all ran.
- Existing-project result: `rejected`, matching Windows. The candidate still has
  63 actionable islands, 13 actionable overhangs, and one actionable resin trap.

No CTB was sent to a printer and no firmware was changed.

## Artifacts

Final audited CTB (retained, not overwritten):

```text
C:\Users\20160600\OneDrive - TU Eindhoven\Documents\Prints\Miniatures\Tentoonstelling\Tentoonstelling_AgentSlicer_100mm_2026-08-29\artifacts\rejected\d8e44b28c6a0\Tentoonstelling_AgentSlicer_100mm_2026-08-29-attempt-3.ctb
```

This is an audited **rejected candidate**, not a print-ready CTB.

Native analysis workspace:

```text
/home/rschmeitz/development-tools/uvtools-work/d8e44b28c6a0
```

Linux analysis JSON:

```text
/home/rschmeitz/development-tools/uvtools-work/d8e44b28c6a0/linux-v2-analysis.json
```

## Use after reboot

From a WSL terminal:

```bash
cd /mnt/c/Users/20160600/Documents/Development/agent-slicer
source .venv/bin/activate

# These exports are optional because these values are now the WSL defaults.
export AGENT_SLICER_UVTOOLS=/home/rschmeitz/development-tools/uvtools/current/UVtoolsCmd
export AGENT_SLICER_WORK_ROOT=/home/rschmeitz/development-tools/agent-slicer-work
export AGENT_SLICER_UVTOOLS_WORKERS=2

agent-slicer tools
/home/rschmeitz/development-tools/uvtools/current/UVtoolsCmd --core-version
agent-slicer analyze model.ctb --output model.uvtools-analysis.json
```

For a guarded rejected-build resume:

```bash
agent-slicer build \
  "/mnt/c/Users/20160600/OneDrive - TU Eindhoven/Documents/Prints/Miniatures/Tentoonstelling/Tentoonstelling_AgentSlicer_100mm_2026-08-29" \
  --reuse-prepared \
  --resume-rejected
```

That resume command was not run during this migration because it would create a
new slicing attempt and alter the existing audited project state. The migration
validated the retained CTB without overwriting it.

## Remaining limitations

- The retained CTB is not production-passed or print-ready.
- UVtools raw subthreshold resin-trap segmentation is not bit-identical across
  Linux and Windows even at the same core version.
- The Windows path currently exceeds the 30-minute resin-trap timeout on this
  machine; its historical completed report remains the Windows parity source.
- The official v4.4.3 release publishes no checksum, so only a local SHA-256
  fingerprint could be recorded.
- WSL has 15 GiB RAM although the official UVtools sizing guidance scales above
  that for 20 logical CPUs; two workers are bounded but still peaked at 11.52
  GiB. Avoid raising concurrency without re-measuring.
- Detector equivalence does not prove firmware acceptance or physical
  printability.
