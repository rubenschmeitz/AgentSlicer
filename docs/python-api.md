# Python API

The Python API uses the same project contract and fail-closed planner as the CLI.
It is suitable for coding agents that can import a local package.

Printer settings can be selected and audited without starting a build:

```python
from agent_slicer.presets import recommend_settings

recommendation = recommend_settings(
    process="fdm",
    machine="bambu-lab-p2s-0.4",
    material="bambu-pla-basic-p2s",
    purpose="fdm-general",
    quality="p2s-0.20-standard",
    output_format="gcode.3mf",
)
assert recommendation["recommendation_status"] == "evidence-backed-starting-point"
assert recommendation["calibration"]["required"]
print(recommendation["settings"])
```

FDM uses the same build API with an installed OrcaSlicer CLI. Machine presets
must allow the output format and identify matching installed vendor profiles:

```python
from agent_slicer.pipeline import build_project, compile_plan
from agent_slicer.project import add_models, init_project

project = init_project(
    "projects/fdm-part", name="fdm-part", process="fdm",
    machine="bambu-lab-p2s-0.4", material="bambu-pla-basic-p2s",
    purpose="fdm-display-supported", quality="p2s-0.20-standard",
    output_format="gcode.3mf",
)
add_models(project, ["part.stl"])
_, plan = compile_plan(project, fdm_slicer_path="/absolute/path/to/OrcaSlicer")
assert not plan.blockers, plan.blockers
result = build_project(project, fdm_slicer_path="/absolute/path/to/OrcaSlicer")
assert result["validation"]["status"] == "passed"
```

Use `fdm_profile_root` when vendor profiles cannot be discovered beside the
executable. [FDM setup](fdm.md) covers settings translation, validation scope,
evidence, and real verification. A passed software build does not prove physical
printability and never starts a print.

Fleet-specific printers can be kept outside the package and loaded without a
count limit. `add_external_preset` atomically adds one validated definition;
`validate_catalog_directory` audits each catalog and returns its SHA-256.

```python
from agent_slicer.presets import (
    add_external_preset,
    load_catalog,
    validate_catalog_directory,
)

add_external_preset(
    directory="~/.config/agent-slicer/presets",
    catalog="machines",
    preset_id="my-printer-0.4",
    definition={
        "process": "fdm",
        "build_volume_mm": [220, 220, 250],
        "output_formats": ["gcode"],
        "default_output_format": "gcode",
    },
)
validate_catalog_directory("~/.config/agent-slicer/presets")
machines = load_catalog(
    "machines", search_paths=["~/.config/agent-slicer/presets"]
)
```

```python
from agent_slicer.pipeline import BuildBlocked, BuildRejected, build_project, compile_plan
from agent_slicer.project import add_models, init_project

project = init_project(
    "projects/example",
    name="example",
    process="resin",
    machine="elegoo-mars-3-pro",
    material="ifun-if3121-toughness-grey",
    purpose="miniature-display-high-detail",
    quality="high-detail",
    output_format="ctb",
)
add_models(project, ["model.stl"], target_height_mm=50)

_, plan = compile_plan(project)
if plan.blockers:
    raise BuildBlocked(plan)

try:
    result = build_project(project)
except BuildRejected as exc:
    # The candidate is retained under artifacts/rejected, but is not approved.
    print(exc.result["production_validation"])
    raise
else:
    print(result["sliced_output"])
```

A rejected build can be resumed after a support-stage code correction while
reusing only exact, still-passing geometry artifacts:

```python
result = build_project(
    project,
    reuse_prepared=True,
    resume_rejected=True,
)
```

This is not a general "trust cache" switch: current sources and settings are
revalidated, the combined plate hash must match the rejected build, and the
retained CTB is analyzed again to seed feedback.

For a single mesh, the same measured repair/orientation and validated cavity
workflow is available without creating a project:

```python
from agent_slicer.blender_adapter import prepare_resin_model

report = prepare_resin_model(
    "model.stl",
    "work/model-prepared.stl",
    "work/model-prepared.json",
    target_height_mm=65,
    voxel_mm=0.12,
    hollow_voxel_mm=0.15,
    wall_mm=1.8,
    drain_diameter_mm=3.5,
    drain_count=2,
)
assert report["stages"]["hollow_and_drain"]["sealed_cavity_voxels"] == 0
```

The measured simplifier is also directly callable. It never overwrites the
input and returns the original geometry at `output` when no candidate passes:

```python
from agent_slicer.mesh_simplify import simplify_mesh_measured

report = simplify_mesh_measured(
    "work/model-prepared.stl",
    "work/model-reduced.stl",
    "work/model-reduced.json",
    target_triangles=350_000,
)
print(report["decision"], report["reduction_ratio"])
```

Generation projects expose the same operations as the `generate` CLI. For
example, a coding agent can create and build a dimensioned part without shell
parsing:

```python
from agent_slicer.generation import (
    approve_generation,
    build_parametric_part,
    init_generation,
)

generation = init_generation(
    "projects/generation/bracket",
    name="bracket",
    kind="part",
    description="Dimensioned mounting bracket",
)
approve_generation(generation, "brief")
report = build_parametric_part(generation, "bracket-spec.json")
assert report["inspection"]["degenerate_triangles"] == 0
```

Provider-backed calls are available through
`submit_reference_generation`, `submit_model_generation`, and
`refresh_generation`. They require environment credentials and an explicit
`confirm_credit_use=True`; generated geometry must still be selected, approved,
and adopted before manufacture.

The stable entry points in `0.3.x` are:

- `agent_slicer.project.init_project`
- `agent_slicer.presets.load_catalog`
- `agent_slicer.presets.resolve_presets`
- `agent_slicer.presets.recommend_settings`
- `agent_slicer.presets.add_external_preset`
- `agent_slicer.presets.validate_catalog_directory`
- `agent_slicer.project.add_models`
- `agent_slicer.project.load_project`
- `agent_slicer.pipeline.compile_plan`
- `agent_slicer.pipeline.build_project`
- `agent_slicer.blender_adapter.prepare_resin_model`
- `agent_slicer.mesh_simplify.simplify_mesh_measured`
- `agent_slicer.stl.inspect_stl`
- `agent_slicer.uvtools_adapter.analyze_sliced_file`
- `agent_slicer.uvtools_adapter.evaluate_production_analysis`
- `agent_slicer.validation.validate_sliced_output`
- `agent_slicer.generation.init_generation`
- `agent_slicer.generation.approve_generation`
- `agent_slicer.generation.build_parametric_part`
- `agent_slicer.generation.submit_reference_generation`
- `agent_slicer.generation.submit_model_generation`
- `agent_slicer.generation.refresh_generation`
- `agent_slicer.generation.select_candidate`
- `agent_slicer.generation.adopt_selected_model`

All return plain dataclasses, dictionaries, paths, or JSON-compatible values.
Agents should always call `compile_plan` and examine `blockers` before `build_project`.
