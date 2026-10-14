# Work PC Bambu Studio preset collection

Verified on 2026-10-14 on TUE037612. Active vendor bundle: 02.08.00.06.
The older installed bundle is 02.08.00.04; dependency hashes and sanitized
printing conflicts are recorded separately in installed-profile-comparison.json.
Thuis was absent from the connected project list and this chat had no remote
execution capability. No cross-computer equality or conflict resolution is claimed.

The saved selection is Bambu Lab P2S 0.4 nozzle, Bambu PETG Basic @BBL P2S
0.4 nozzle, and 0.20mm High Quality @BBL P2S. This is selection evidence,
not proof of a successful print. Existing PETG HF and Standard ids stay distinct.

This additive external catalog contains three machine definitions (P2S 0.4,
P2S 0.2 and X1 Carbon 0.4), eight materials (one selected system material and
seven user materials), two qualities and their matching support purposes.
Compatibility does not establish ownership of an X1 Carbon printer.

Use the source support purpose paired with each imported quality. The miniature
process's supports must not be replaced by the bundled general purpose defaults.
Every Bambu vector remains intact under upstream_profile.resolved_settings.
Scalar mapped values use Direct Drive Standard when explicitly labeled. Source
SHA256 receipts record every inherited/include file and the final user override.
The importer resolves explicit file values; implicit Bambu application defaults
are not invented. It follows parent, ordered include templates, then child
precedence, as in the official Bambu Studio PresetBundle.cpp loader:
https://github.com/bambulab/BambuStudio/blob/master/src/libslic3r/PresetBundle.cpp

No FDM slicing adapter is implemented in this revision. fdm.slice and
fdm.validate.basic are unavailable, so no slicing success is claimed. The
installed Windows GUI executable produced no CLI help output. Saved-resolution
checks validate settings/catalogs, not G-code, mechanical quality or calibration.

Each definition reports unmapped_keys (retained in full upstream settings)
and excluded_keys (values withheld). User calibration/support extension keys
are explicitly retained and labeled even when absent from the vendor schema. These settings are reusable for resolution
and auditing; a future FDM adapter must consume them explicitly. G-code
snippets, notes, account/service ids, .info sidecars, network/printer/login
configuration and model files are excluded. The complete BambuStudio.conf and
configuration directory are never copied. Saved flow ratios and pressure
advance are retained; separate calibration history/physical receipts were absent.

From the repository root:

```bash
agent-slicer profiles validate presets/bambu-work-pc-20261014
agent-slicer profiles recommend --catalog-path presets/bambu-work-pc-20261014 \
  --machine tue037612-2026-10-14-02-08-00-06-system-bambu-lab-p2s-0-4-nozzle-5edc3d8b \
  --material tue037612-2026-10-14-02-08-00-06-system-bambu-petg-basic-bbl-p2s-0-4-nozzle-6b9b3ce2 \
  --quality tue037612-2026-10-14-02-08-00-06-system-0-20mm-high-quality-bbl-p2s-8e0fa557 \
  --purpose tue037612-2026-10-14-02-08-00-06-system-0-20mm-high-quality-bbl-p2s-8e0fa557-source-supports
```

Existing user overrides continue through the normal resolver and take precedence
over mapped catalog settings. Source values remain available for comparison.
Host/date/vendor/digest scoped ids prevent silent overwrites. Collect another host
into a fresh directory, inspect provenance and compare full resolved_settings
before choosing either version. Do not merge two hosts by profile name alone.
