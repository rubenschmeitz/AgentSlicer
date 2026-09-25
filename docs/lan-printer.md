# Mars 5 Ultra LAN workflow

The Electron Resin-Slicer has **LAN Printer · Connect / Send** in the Job panel.
Install the Agent-Slicer environment (including websocket-client) before use.
For a custom installation, set AGENT_SLICER_PYTHON to its Python executable.
The source-checkout layout is detected automatically; a standalone Electron
bundle does not yet package Agent-Slicer.

1. Find / Connect broadcasts local SDCP discovery. If broadcast is unavailable,
   enter the printer's private IPv4 address for unicast discovery.
2. Select the printer and refresh status. This does not move the machine.
3. Choose an Agent-Slicer project directory containing project.json and a
   completed artifacts/build-manifest.json.
4. Upload only checks both validations and the exact output SHA-256, then sends
   a unique filename. It never overwrites existing files or starts printing.
5. Start uploaded print asks for a separate physical-printer confirmation.
   Check the empty build plate, vat/film, resin and cover first.

Rejected or interrupted builds are deliberately blocked. A normal GUI slice
without the Agent-Slicer production-validation report is not sufficient.
Build the project with the public CLI, review its evidence, then upload it.
Reports predating the corrected suction-cup/boundary detection contract cannot
authorize upload. For an unchanged completed/rejected build, run
`agent-slicer revalidate C:/path/to/project` to repeat all production checks
without reslicing. Geometry, placement, settings, plate and slice hashes must
still match; previous reports are retained in artifacts/validation-history.

In UVtools 4.4.3, the suction option requires the resin-trap detector to be
enabled in the same invocation. Boundary records use the singular
`TouchingBound` name. The adapter records its exact flags in every report.
See [the upstream command](https://github.com/sn4k3/UVtools/blob/v4.4.3/UVtools.Cmd/Symbols/PrintIssuesCommand.cs)
and [issue types](https://github.com/sn4k3/UVtools/blob/v4.4.3/UVtools.Core/Layers/MainIssue.cs).
Grouped trap volumes are converted from UVtools' reported voxel units using
pixel area divided by PpmmMax; layer height is already included upstream.
The configured physical-volume threshold is unchanged.

## CLI

From the repository root in PowerShell:

```powershell
./scripts/agent-slicer.ps1 printer discover
./scripts/agent-slicer.ps1 printer status --address 192.168.1.114
./scripts/agent-slicer.ps1 printer files --address 192.168.1.114
./scripts/agent-slicer.ps1 printer upload C:/path/to/project --address 192.168.1.114
# Only after checking the physical printer and deciding to start:
./scripts/agent-slicer.ps1 printer print C:/path/to/project/artifacts/upload-ID.json --confirm-start
```

The SDK exposes discover, connect_device, SDCPPrinter.status/files,
approved_artifact, upload_project, and start_print.
upload_project accepts a progress callback with sent/total byte counts.

## Safety and evidence

- Discovery pins the UDP source address and board identity; redirects and
  public/non-IPv4 addresses are rejected. Communication stays on the LAN.
- Model and 8520 x 4320 resolution are checked again before writes. Firmware
  can report an incorrect XYZsize: never import that over verified dimensions.
- Busy printers block upload/start. Print start also requires healthy hardware
  self-check flags and no reported print error.
- Upload uses 1 MiB multipart chunks with full-file MD5 verification enabled,
  checks every HTTP acknowledgement and confirms the unique remote filename.
  Firmware V1.4.3 may omit remote size; receipts explicitly record whether size
  could be verified. Remote bytes are not independently downloaded/hashed.
- Transfers do not retry ambiguous chunks automatically. On failure, inspect
  printer status/files. A partial remote file may remain; nothing is deleted.
- Before a start command, an exclusive .start-attempt.json marker is written.
  Retrying the same receipt cannot accidentally start the job again. Do not
  remove that marker until the actual printer state has been established.
- SDCP is unauthenticated LAN traffic. Use only a trusted local network.
- Successful upload is not proof of firmware print acceptance; a start Ack is
  not proof of a successful physical print.

Protocol source:
[CBD Technology SDCP v3](https://github.com/cbd-tech/SDCP-Smart-Device-Control-Protocol-V3.0.0).

## Support changes

Contact spacing now includes physical height, so a contact under the base
cannot suppress a contact on an overhang far above it. Narrow contacts overlap
the model by a configurable 0.1 mm vertical depth (layer-quantized); the added
overlap does not enlarge the contact radius or introduce a contact bulb.
Tip penetration is exposed in support settings and persisted on export.
Model-to-model supports apply the same narrow overlap at their lower contact;
bed feet and branch joints are not extended below their planned bases.
The raft includes offset bed feet and strips joining them to the projected
model footprint. --brace-interval exposes repeated bracing for tall supports.
Bed-route candidates now reserve the full foot/trunk radius and raft expansion,
plus a dark raster border, before accepting a base. This applies to direct,
sloped and dogleg bed routes; moving the foot inward must still pass the usual
collision and shaft-angle checks. It does not replace saved-layer boundary
validation (the model, brace or projected raft can still exceed a poor layout).

These are engineering improvements, not a guarantee of perfect supports:
validate saved layers, check cavity access, and calibrate resin exposure and
support tips with a small physical test. Internal supports can remain trapped
inside a one-piece hollow model even when the cavity has drainage holes.
