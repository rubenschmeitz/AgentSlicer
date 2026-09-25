# Open-source slicer integration, native saved-file validation v2

Date: 2026-08-31

This version continues the v1 sparse-raster and pre-encode validation work. It
adds bounded native readers for the exact CTB v5 and GOO v3 files produced by
Resin-Slicer and makes a successful decode/validation round trip a prerequisite
for publishing a newly sliced file.

## Implemented

- Pure-Python AES-256 decryption and CBC decoding for CTB settings/signatures.
- CTB v5 metadata parsing, paged layer-reference validation, XOR payload
  decoding, strict RLE length accounting, signature verification, and trailer
  verification.
- GOO v3 fixed-header parsing, sequential layer parsing, record/delimiter
  validation, per-layer checksum verification, strict RLE length accounting,
  and exact trailer/end-of-file verification.
- Sparse reconstruction directly from decoded RLE runs. The reader retains one
  decoded layer for streaming validation and does not allocate a full printer
  resolution pixel buffer.
- Bounds on section sizes, layer counts, pixel counts, offsets, and encoded RLE
  payload sizes. Non-finite/non-increasing Z and invalid exposure values fail
  closed.
- Public Python reader API:

  ```python
  from resin_slicer.formats import inspect_sliced_file

  sliced = inspect_sliced_file("part.ctb")
  print(sliced.info)
  for layer in sliced.iter_layers():
      print(layer.index, layer.z_mm, layer.raster.count_on())
  ```

- `slice_to_file` now writes to a same-directory staging target, decodes and
  structurally validates the saved bytes, requires exact parity with the
  pre-encode raster audit, and only then atomically replaces the requested
  destination. Failure preserves an older destination file.
- Slice reports retain the historical `internal_layer_validation` field and add
  `post_encode_validation` plus `encode_validation_parity`.
- Agent-Slicer validates and retains this decoded-file receipt as
  `verified-parity`. It explicitly marks it as non-authoritative; independent
  Linux-native UVtools analysis remains the production gate.

## Mars 3 Pro smoke result

The unchanged CTB from v1 was inspected again:

- path:
  `/home/rschmeitz/development-tools/resin-slicer-validation-2026-08-31/mars3pro-native-smoke.ctb`
- SHA-256:
  `bbcd9e2d8a9d2a81cde193e383319ec72c3284861ca21665a3ea98907fc97067`
- decoded as encrypted CTB v5, 4098 x 2560, 220 layers;
- 220 nonempty layers and no empty-layer gap;
- no wholly detached component;
- one footprint-deficit component at zero-based layer 100, 29,744 pixels,
  27,967 unsupported pixels, bounds X=1958..2139 and Y=1189..1370.

The decoded result exactly reproduces the v1 pre-encode receipt. UVtools 4.4.3
reported one island at the same layer, with the same 29,744-pixel area and
bounds, and no empty-layer issue on the same SHA-256. This remains a bounded
single-artifact parity result. The smoke CTB deliberately contains an
unsupported footprint and is not a production-ready print candidate.

Native decode plus structural validation of this 4K, 220-layer file completed
in 0.94 seconds wall time with 21,760 KiB maximum RSS in the measured low-priority
run. This is not directly equivalent to the broader UVtools detector suite,
which also searches resin traps, suction cups, overhangs, bounds, and height.

## Verification

- Resin-Slicer: 116/116 tests passed; maximum suite RSS 39,300 KiB.
- Agent-Slicer: 78/78 tests passed; maximum suite RSS 148,280 KiB.
- New tests cover AES-CBC decryption, CTB and GOO sparse decode round trips,
  GOO checksum corruption, truncated CTB rejection, exact pre/post parity, and
  preservation of a prior output after post-encode validation failure.
- Python byte-compilation and `git diff --check` passed in both repositories.

## Remaining limitations

- The native reader intentionally supports the writer contracts currently
  produced here: CTB v5 and GOO v3. It is not a universal parser for every
  historical vendor variant.
- CTB v5 uses the format's existing zero checksum/signature convention; the
  reader verifies that the encrypted signature matches that declared checksum,
  but this is not a cryptographic authenticity guarantee.
- Native validation now proves that saved pixels reproduce the pre-encode
  structural audit. It does not yet reproduce UVtools resin-trap, suction-cup,
  or trapped-volume algorithms.
- One smoke artifact is insufficient to establish broad detector parity.
  UVtools therefore remains mandatory for production validation.
- Firmware acceptance and physical print success remain unproven. No file was
  sent to a printer and no firmware was changed.
