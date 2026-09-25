# Agent-Slicer agent contract

Agent-Slicer is a deterministic manufacturing pipeline. Agents choose typed
operations; geometry and slicing backends execute them.

## Non-negotiable rules

- Never modify source models in place.
- Never infer units silently. Flag ambiguous units and require a target size.
- Never send, upload, or start a print unless the user explicitly asks for that
  separate action.
- Inspect before repair. Measure and report the effect of every lossy repair.
- Do not claim printability merely because a file was exported.
- Machine limits and format compatibility are hard constraints.
- Material settings and geometry-purpose presets are separate configuration
  layers.
- A missing required capability is a blocking error, not permission to skip a
  stage.
- Preserve manifests, hashes, resolved settings, logs, and validation evidence.
- External applications are replaceable adapters and must not leak into the
  public project or operation schemas.

## Standard workflow

1. Load and validate the project manifest.
2. Resolve machine, material, purpose, and quality presets.
3. Inspect every source model.
4. Compile a deterministic operation plan.
5. Stop if a required capability is unavailable or a hard constraint fails.
6. Execute operations transactionally into `artifacts/`.
7. Validate geometry, placement, sliced format, and settings.
8. Produce machine-readable and human-readable reports.

