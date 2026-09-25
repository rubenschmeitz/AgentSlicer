# Full WSL-native slicer migration

Date: 2026-08-31

## Result

The active Resin-Slicer and Agent-Slicer development trees, Python
environments, frontend dependencies, geometry backend, slicing backend, and
validation backend now run from WSL's native Linux filesystem. The former
Windows-mounted trees were not deleted or modified during migration and remain
recovery copies.

## Active layout

| Component | WSL-native location |
|---|---|
| Resin-Slicer source | `/home/rschmeitz/development/Resin-Slicer` |
| Resin-Slicer environment | `/home/rschmeitz/development/Resin-Slicer/.venv` |
| Agent-Slicer source | `/home/rschmeitz/development/agent-slicer` |
| Agent-Slicer environment | `/home/rschmeitz/development/agent-slicer/.venv` |
| UVtools | `/home/rschmeitz/development-tools/uvtools/current/UVtoolsCmd` |
| Blender | `/home/rschmeitz/development-tools/blender/current/blender` |
| Node.js | `/home/rschmeitz/development-tools/node/current` |
| Resin intermediates | `/home/rschmeitz/development-tools/resin-slicer-work` |
| Agent/UVtools intermediates | `/home/rschmeitz/development-tools/agent-slicer-work` |

The copied Git bases are unchanged:

- Resin-Slicer: `b7438ddbd66c4f714bfc0031a1f1d3787dbf011f` plus the complete dirty working tree.
- Agent-Slicer: `fe68f97f4ceee33d2256792bfbf2a77361c9d4cb` plus the complete dirty working tree.

Regenerable `.venv`, `node_modules`, build, cache, and `egg-info` directories
were deliberately not copied. They were recreated natively. Historical work
artifacts in Agent-Slicer's `work/` tree were copied.

## Native runtimes

- Python 3.12.3 environments recreated with editable installs.
- Agent-Slicer includes its full optional CAD stack, including CadQuery 2.8.0.
- Node.js 24.20.0 LTS with npm 11.19.0. The official Linux x64 archive matched
  SHA-256 `2f2c0da162318f0de47665410c7c8c2ed3d36c8f3105de4bbc61176c70a7cbf2`.
- Electron 44.0.0 was installed as a Linux x64 binary. Its recovery script was
  corrected to use Electron's cross-platform ZIP extractor instead of GNU
  `tar`, which cannot extract ZIP files.
- Blender 4.5.13 LTS official Linux x64 package, SHA-256
  `da4e69b06b75b9e642d106496c50e7e240218b411d2f6e18271c1d1d819cef91`.
- UVtoolsCmd 2.0.1 / UVtools.Core 4.4.3, native Linux ELF.

`file -L` identifies the Python interpreter, Blender, Electron, and UVtools as
Linux x86-64 ELF executables. Agent-Slicer tool discovery selects only these
WSL-native locations by default. Windows paths remain fallback compatibility
entries, not the selected runtime.

## Native work-directory changes

Resin-Slicer now defaults format spools, STEP cache, UI previews, and support
plan cache to `/home/rschmeitz/development-tools/resin-slicer-work` under WSL.
It ignores inherited Windows `TEMP` and `TMP` for these large intermediates.

Agent-Slicer now defaults preparation, simplification, build, UVtools, and
subprocess temporary data to
`/home/rschmeitz/development-tools/agent-slicer-work`. Cross-filesystem final
publication uses copy-to-neighbouring-temporary plus atomic replace, so final
reports and printer files can still be published to OneDrive or `F:` without
putting intermediate meshes there.

## Verification

- Resin-Slicer: 118/118 tests passed in 2.87 s, peak RSS 41,956 KiB.
- Agent-Slicer: 80/80 tests passed in 2.55 s, peak RSS 154,624 KiB.
- Electron JavaScript checks and the Vite production build passed; npm reported
  zero vulnerabilities.
- A fresh WSL-only CTB build completed at
  `/home/rschmeitz/development-tools/migration-smoke-2026-08-31`:
  - 160 layers and four generated supports;
  - exact native pre/post-encode structural parity;
  - CTB SHA-256 `5e2863021f2aca7d72af4fd07c54b5c0e069bcd1f2d86692549c2a938d26dd3c`;
  - build runtime 1.71 s and peak RSS 97,880 KiB.
- Native Linux UVtools scanned that CTB for islands, overhangs, resin traps,
  suction cups, touching bounds, print height, and empty layers. Every count was
  zero. Runtime was 3.34 s and peak RSS 73,400 KiB.
- Native Blender performed measured solid repair on the test cube successfully.
  Its intermediate mesh and report were under the native work root; runtime was
  1.84 s and peak RSS 472,800 KiB.
- No printer command was issued and no firmware was modified.

The smoke CTB and zero detector counts prove the migrated toolchain executes;
they do not prove physical print success or calibrate a production resin.

## Performance comparison

The suites gained two migration tests each, so the measurements are close but
not bit-for-bit identical workloads.

| Suite | Windows-mounted tree | WSL-native tree | Approximate wall-time improvement |
|---|---:|---:|---:|
| Resin-Slicer | 10.87 s, 116 tests | 2.87 s, 118 tests | 3.8x |
| Agent-Slicer | 26.53 s, 78 tests | 2.55 s, 80 tests | 10.4x |

## Use after reboot

The user-local `/home/rschmeitz/bin` directory already appears on `PATH` and
contains links to every active command:

```bash
resin-slicer --version
agent-slicer version
UVtoolsCmd --version
blender --version
node --version
npm --version
```

For direct development:

```bash
cd /home/rschmeitz/development/Resin-Slicer
source .venv/bin/activate

cd /home/rschmeitz/development/agent-slicer
source .venv/bin/activate
```

Optional explicit work-root overrides are `RESIN_SLICER_WORK_ROOT` and
`AGENT_SLICER_WORK_ROOT`.

## Preserved recovery copies

- `C:\Users\20160600\Documents\Development\Resin-Slicer`
- `C:\Users\20160600\Documents\Development\agent-slicer`

These are snapshots of the pre-migration working trees. They are no longer the
default executable locations and should not be edited in parallel with the WSL
copies unless changes are deliberately synchronized.
