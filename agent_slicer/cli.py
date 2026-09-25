from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "version":
            print(__version__)
        elif args.command == "printer":
            from .printer import run_cli
            _print(run_cli(args))
        elif args.command == "benchmark":
            from .benchmark import run_benchmark

            _print(run_benchmark(args.manifest, args.output, repeats=args.repeats))
        elif args.command == "tools":
            from .backends import discover_tools

            _print({key: value.to_dict() for key, value in discover_tools().items()})
        elif args.command == "presets":
            from .presets import load_catalog

            _print(load_catalog(args.catalog, search_paths=args.catalog_path))
        elif args.command == "profiles":
            from .presets import (
                add_external_preset,
                load_catalog,
                recommend_settings,
                validate_catalog_directory,
            )

            if args.profiles_command == "list":
                value = load_catalog(args.catalog, search_paths=args.catalog_path)
            elif args.profiles_command == "recommend":
                machines = load_catalog("machines", search_paths=args.catalog_path)
                try:
                    selected_machine = machines[args.machine]
                except KeyError as exc:
                    raise ValueError(f"unknown machine preset: {args.machine}") from exc
                value = recommend_settings(
                    process=args.process or selected_machine["process"],
                    machine=args.machine,
                    material=args.material,
                    purpose=args.purpose,
                    quality=args.quality,
                    output_format=(
                        args.output_format
                        or selected_machine["default_output_format"]
                    ),
                    search_paths=args.catalog_path,
                )
            elif args.profiles_command == "validate":
                value = validate_catalog_directory(args.directory)
            elif args.profiles_command == "add":
                definition = json.loads(
                    Path(args.definition).read_text(encoding="utf-8")
                )
                value = add_external_preset(
                    directory=args.directory,
                    catalog=args.catalog,
                    preset_id=args.preset_id,
                    definition=definition,
                )
            else:  # pragma: no cover - argparse enforces the subcommand.
                parser.error("unsupported profiles command")
            _print(value)
        elif args.command == "project" and args.project_command == "init":
            from .project import init_project
            from .presets import load_catalog, resolve_presets

            machines = load_catalog("machines")
            try:
                selected_machine = machines[args.machine]
            except KeyError as exc:
                raise ValueError(f"unknown machine preset: {args.machine}") from exc

            selected_process = args.process or selected_machine["process"]
            selected_output = (
                args.output_format or selected_machine["default_output_format"]
            )
            resolve_presets(
                process=selected_process,
                machine=args.machine,
                material=args.material,
                purpose=args.purpose,
                quality=args.quality,
                output_format=selected_output,
            )
            root = init_project(
                args.directory,
                name=args.name,
                process=selected_process,
                machine=args.machine,
                material=args.material,
                purpose=args.purpose,
                quality=args.quality,
                output_format=selected_output,
            )
            _print({"status": "created", "project": str(root)})
        elif args.command == "project" and args.project_command == "add":
            from .project import add_models

            added = add_models(
                args.project,
                args.models,
                target_height_mm=args.target_height,
            )
            _print({"status": "updated", "added": [item.id for item in added]})
        elif args.command == "inspect":
            from .project import load_project
            from .stl import inspect_stl

            root, manifest = load_project(args.project)
            _print(
                {
                    "project": str(root),
                    "models": [
                        inspect_stl(root / item.source).to_dict() for item in manifest.models
                    ],
                }
            )
        elif args.command == "gui":
            from .inspection_gui import serve_inspection_gui

            serve_inspection_gui(
                args.project,
                host=args.host,
                port=args.port,
                open_browser=not args.no_open,
            )
        elif args.command == "geometry" and args.geometry_command == "hollow-drain":
            from .blender_adapter import hollow_and_drain

            result = hollow_and_drain(
                args.input,
                args.output,
                args.report,
                wall_mm=args.wall,
                target_height_mm=args.target_height,
                drain_diameter_mm=args.drain_diameter,
                drain_count=args.drain_count,
                blend=args.blend,
                blender_path=args.blender,
            )
            _print(result)
        elif args.command == "geometry" and args.geometry_command == "prepare-resin":
            from .blender_adapter import prepare_resin_model

            result = prepare_resin_model(
                args.input,
                args.output,
                args.report,
                target_height_mm=args.target_height,
                voxel_mm=args.voxel,
                repair_method=args.repair_method,
                max_p95_deviation_mm=args.max_p95_deviation,
                max_deviation_mm=args.max_deviation,
                wall_mm=args.wall,
                hollow_voxel_mm=args.hollow_voxel,
                hollow_max_p95_change_mm=args.hollow_max_p95_change,
                hollow_max_change_mm=args.hollow_max_change,
                drain_diameter_mm=args.drain_diameter,
                drain_count=args.drain_count,
                max_tilt_deg=args.max_tilt,
                orient=not args.no_orient,
                hollow=not args.solid,
                drain=not args.solid,
                blend=args.blend,
                blender_path=args.blender,
            )
            _print(result)
        elif args.command == "geometry" and args.geometry_command == "simplify":
            from .mesh_simplify import simplify_mesh_measured

            result = simplify_mesh_measured(
                args.input,
                args.output,
                args.report,
                minimum_input_triangles=args.minimum_input_triangles,
                target_triangles=args.target_triangles,
                max_p95_deviation_mm=args.max_p95_deviation,
                max_deviation_mm=args.max_deviation,
                max_volume_change_ratio=args.max_volume_change_ratio,
                sample_points=args.sample_points,
                aggression=args.aggression,
                protected_component_triangles=args.protected_component_triangles,
            )
            _print(result)
        elif args.command == "plan":
            from .pipeline import compile_plan, write_plan

            root, plan = compile_plan(args.project)
            path = write_plan(root, plan)
            value = plan.to_dict()
            value["plan_path"] = str(path)
            _print(value)
            return 2 if plan.blockers else 0
        elif args.command == "build":
            from .pipeline import BuildBlocked, BuildRejected, build_project

            try:
                result = build_project(
                    args.project,
                    resin_slicer_path=args.resin_slicer,
                    uvtools_path=args.uvtools,
                    work_root=args.work_root,
                    uvtools_workers=args.uvtools_workers,
                    skip_slice=args.skip_slice,
                    reuse_prepared=args.reuse_prepared,
                    resume_rejected=args.resume_rejected,
                )
            except BuildBlocked as exc:
                _print(exc.plan.to_dict(), stream=sys.stderr)
                return 2
            except BuildRejected as exc:
                _print(exc.result, stream=sys.stderr)
                return 3
            _print(result)
        elif args.command == "validate":
            from .validation import validate_sliced_output

            value = validate_sliced_output(args.path, args.format)
            _print(value)
            return 0 if value["status"] == "passed" else 2
        elif args.command == "revalidate":
            from .revalidation import revalidate_project
            value = revalidate_project(args.project, uvtools_path=args.uvtools, work_root=args.work_root)
            _print(value)
            return 0 if value["status"] == "completed" else 3
        elif args.command == "analyze":
            from .uvtools_adapter import analyze_sliced_file

            value = analyze_sliced_file(
                args.path,
                output=args.output,
                uvtools_path=args.uvtools,
                work_root=args.work_root,
                workers=args.uvtools_workers,
            )
            _print(value)
            return 0 if value["status"] == "passed" else 3
        elif args.command == "generate":
            from .generation import (
                add_reference_images,
                adopt_selected_model,
                approve_generation,
                build_parametric_part,
                generation_status,
                init_generation,
                refresh_generation,
                select_candidate,
                submit_model_generation,
                submit_reference_generation,
            )

            if args.generate_command == "providers":
                from .generation_providers import provider_configuration

                value = provider_configuration()
            elif args.generate_command == "init":
                root = init_generation(
                    args.directory,
                    name=args.name,
                    kind=args.kind,
                    description=args.description,
                    target_height_mm=args.target_height,
                    image_provider=args.image_provider,
                    model_provider=args.model_provider,
                )
                value = generation_status(root)
            elif args.generate_command == "status":
                value = generation_status(args.project)
            elif args.generate_command == "approve":
                value = approve_generation(args.project, args.stage)
            elif args.generate_command == "add-reference":
                value = {
                    "status": "updated",
                    "artifacts": add_reference_images(args.project, args.images),
                }
            elif args.generate_command == "submit-reference":
                value = {
                    "status": "submitted",
                    "tasks": submit_reference_generation(
                        args.project,
                        count=args.count,
                        model=args.model,
                        confirm_credit_use=args.confirm_credit_use,
                    ),
                }
            elif args.generate_command == "submit-model":
                value = {
                    "status": "submitted",
                    "tasks": submit_model_generation(
                        args.project,
                        count=args.count,
                        model=args.model,
                        confirm_credit_use=args.confirm_credit_use,
                    ),
                }
            elif args.generate_command == "refresh":
                value = refresh_generation(args.project, fetch=not args.no_fetch)
            elif args.generate_command == "select":
                value = select_candidate(args.project, args.candidate)
            elif args.generate_command == "build-part":
                value = build_parametric_part(args.project, args.spec)
            elif args.generate_command == "adopt":
                value = adopt_selected_model(args.project, args.agent_project)
            else:  # pragma: no cover - argparse enforces the subcommand.
                parser.error("unsupported generation command")
            _print(value)
        else:
            parser.error("unsupported command")
    except (OSError, ValueError, RuntimeError) as exc:
        _print({"status": "error", "error": type(exc).__name__, "message": str(exc)}, stream=sys.stderr)
        return 2
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-slicer")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("version")
    sub.add_parser("tools")
    from .printer import add_cli
    add_cli(sub)
    benchmark = sub.add_parser("benchmark", help="run a trusted local benchmark manifest sequentially")
    benchmark.add_argument("manifest", type=Path)
    benchmark.add_argument("--output", required=True, type=Path, help="new native-filesystem evidence directory")
    benchmark.add_argument("--repeats", type=int, default=3)

    presets = sub.add_parser("presets")
    presets.add_argument("catalog", choices=["machines", "materials", "purposes", "qualities"])
    presets.add_argument(
        "--catalog-path",
        action="append",
        help="external preset directory; repeat for multiple catalogs",
    )

    profiles = sub.add_parser(
        "profiles",
        help="manage and resolve evidence-backed printer profiles",
    )
    profiles_sub = profiles.add_subparsers(dest="profiles_command", required=True)
    profiles_list = profiles_sub.add_parser("list")
    profiles_list.add_argument(
        "catalog", choices=["machines", "materials", "purposes", "qualities"]
    )
    profiles_list.add_argument(
        "--catalog-path",
        action="append",
        help="external preset directory; repeat for multiple catalogs",
    )
    recommend = profiles_sub.add_parser("recommend")
    recommend.add_argument("--machine", required=True)
    recommend.add_argument("--material", required=True)
    recommend.add_argument("--purpose", required=True)
    recommend.add_argument("--quality", required=True)
    recommend.add_argument("--process", choices=["resin", "fdm"])
    recommend.add_argument("--output-format")
    recommend.add_argument(
        "--catalog-path",
        action="append",
        help="external preset directory; repeat for multiple catalogs",
    )
    validate_profiles = profiles_sub.add_parser("validate")
    validate_profiles.add_argument("directory")
    add_profile = profiles_sub.add_parser("add")
    add_profile.add_argument("--directory", required=True)
    add_profile.add_argument(
        "--catalog",
        required=True,
        choices=["machines", "materials", "purposes", "qualities"],
    )
    add_profile.add_argument("--id", dest="preset_id", required=True)
    add_profile.add_argument(
        "--definition",
        required=True,
        help="JSON file containing one preset definition object",
    )

    project = sub.add_parser("project")
    project_sub = project.add_subparsers(dest="project_command", required=True)
    init = project_sub.add_parser("init")
    init.add_argument("name")
    init.add_argument("--directory", required=True)
    init.add_argument("--process", choices=["resin", "fdm"])
    init.add_argument("--machine", required=True)
    init.add_argument("--material", required=True)
    init.add_argument("--purpose", required=True)
    init.add_argument("--quality", default="standard")
    init.add_argument("--output-format")
    add = project_sub.add_parser("add")
    add.add_argument("project")
    add.add_argument("models", nargs="+")
    add.add_argument("--target-height", type=float)

    inspect = sub.add_parser("inspect")
    inspect.add_argument("project")
    gui = sub.add_parser(
        "gui",
        help="open the read-only project evidence and geometry inspector",
    )
    gui.add_argument("project")
    gui.add_argument("--host", default="127.0.0.1")
    gui.add_argument("--port", type=int, default=8765)
    gui.add_argument("--no-open", action="store_true")
    geometry = sub.add_parser("geometry")
    geometry_sub = geometry.add_subparsers(dest="geometry_command", required=True)
    hollow = geometry_sub.add_parser("hollow-drain")
    hollow.add_argument("input")
    hollow.add_argument("--output", required=True)
    hollow.add_argument("--report", required=True)
    hollow.add_argument("--wall", type=float, default=1.5)
    hollow.add_argument("--target-height", type=float)
    hollow.add_argument("--drain-diameter", type=float, default=3.0)
    hollow.add_argument("--drain-count", type=int, default=2)
    hollow.add_argument("--blend")
    hollow.add_argument("--blender")
    prepare_resin = geometry_sub.add_parser("prepare-resin")
    prepare_resin.add_argument("input")
    prepare_resin.add_argument("--output", required=True)
    prepare_resin.add_argument("--report", required=True)
    prepare_resin.add_argument("--target-height", type=float)
    prepare_resin.add_argument("--repair-method", choices=("local-topology", "measured-voxel"), default="local-topology",
                               help="local surface-preserving repair; measured-voxel explicitly reconstructs the whole surface")
    prepare_resin.add_argument("--voxel", type=float, default=0.12)
    prepare_resin.add_argument("--max-p95-deviation", type=float, default=0.01)
    prepare_resin.add_argument("--max-deviation", type=float, default=0.05)
    prepare_resin.add_argument("--wall", type=float, default=1.8)
    prepare_resin.add_argument("--hollow-voxel", type=float, default=0.15)
    prepare_resin.add_argument("--hollow-max-p95-change", type=float, default=0.20)
    prepare_resin.add_argument("--hollow-max-change", type=float, default=0.60)
    prepare_resin.add_argument("--drain-diameter", type=float, default=3.5)
    prepare_resin.add_argument("--drain-count", type=int, default=2)
    prepare_resin.add_argument("--max-tilt", type=float, default=20.0)
    prepare_resin.add_argument("--no-orient", action="store_true")
    prepare_resin.add_argument(
        "--solid",
        action="store_true",
        help="repair/orient only; do not hollow or add drains",
    )
    prepare_resin.add_argument("--blend")
    prepare_resin.add_argument("--blender")
    simplify = geometry_sub.add_parser(
        "simplify",
        help="measured component-aware quadric mesh reduction",
    )
    simplify.add_argument("input")
    simplify.add_argument("--output", required=True)
    simplify.add_argument("--report", required=True)
    simplify.add_argument("--minimum-input-triangles", type=int, default=500_000)
    simplify.add_argument("--target-triangles", type=int, default=350_000)
    simplify.add_argument("--max-p95-deviation", type=float, default=0.035)
    simplify.add_argument("--max-deviation", type=float, default=0.10)
    simplify.add_argument("--max-volume-change-ratio", type=float, default=0.005)
    simplify.add_argument("--sample-points", type=int, default=20_000)
    simplify.add_argument("--aggression", type=int, default=5)
    simplify.add_argument(
        "--protected-component-triangles",
        type=int,
        default=5_000,
    )
    plan = sub.add_parser("plan")
    plan.add_argument("project")
    build = sub.add_parser("build")
    build.add_argument("project")
    build.add_argument("--resin-slicer")
    build.add_argument(
        "--uvtools",
        help="UVtoolsCmd executable (defaults to AGENT_SLICER_UVTOOLS or discovery)",
    )
    build.add_argument(
        "--work-root",
        help="native-filesystem root for large build and validation intermediates",
    )
    build.add_argument(
        "--uvtools-workers",
        type=int,
        help="requested UVtools workers (recorded; memory-safe scans run serially)",
    )
    build.add_argument("--skip-slice", action="store_true")
    build.add_argument(
        "--reuse-prepared",
        action="store_true",
        help="reuse only hash- and settings-matched passed preparation artifacts",
    )
    build.add_argument(
        "--resume-rejected",
        action="store_true",
        help="resume detector-feedback slicing from a matching rejected build",
    )
    validate = sub.add_parser("validate")
    validate.add_argument("path")
    validate.add_argument("--format", choices=["ctb", "goo"])
    analyze = sub.add_parser("analyze")
    analyze.add_argument("path")
    analyze.add_argument("--output")
    analyze.add_argument("--uvtools")
    revalidate = sub.add_parser("revalidate", help="repeat full production checks without reslicing")
    revalidate.add_argument("project")
    revalidate.add_argument("--uvtools")
    revalidate.add_argument("--work-root")
    analyze.add_argument(
        "--work-root",
        help="native-filesystem root for extracted layers and analysis intermediates",
    )
    analyze.add_argument(
        "--uvtools-workers",
        type=int,
        help="requested UVtools workers (recorded; memory-safe scans run serially)",
    )
    generate = sub.add_parser(
        "generate",
        help="create resumable text/image/CAD generation projects",
    )
    generate_sub = generate.add_subparsers(dest="generate_command", required=True)
    generate_sub.add_parser("providers")
    generate_init = generate_sub.add_parser("init")
    generate_init.add_argument("name")
    generate_init.add_argument("--directory", required=True)
    generate_init.add_argument("--kind", choices=["part", "concept"], required=True)
    generate_init.add_argument("--description", required=True)
    generate_init.add_argument("--target-height", type=float)
    generate_init.add_argument("--image-provider", choices=["meshy", "tripo"], default="meshy")
    generate_init.add_argument("--model-provider", choices=["meshy", "tripo"], default="meshy")
    generate_status_parser = generate_sub.add_parser("status")
    generate_status_parser.add_argument("project")
    generate_approve = generate_sub.add_parser("approve")
    generate_approve.add_argument("project")
    generate_approve.add_argument("stage", choices=["brief", "references", "selection"])
    generate_reference = generate_sub.add_parser("add-reference")
    generate_reference.add_argument("project")
    generate_reference.add_argument("images", nargs="+")
    submit_reference = generate_sub.add_parser("submit-reference")
    submit_reference.add_argument("project")
    submit_reference.add_argument("--count", type=int, default=1)
    submit_reference.add_argument("--model")
    submit_reference.add_argument("--confirm-credit-use", action="store_true")
    submit_model = generate_sub.add_parser("submit-model")
    submit_model.add_argument("project")
    submit_model.add_argument("--count", type=int, default=1)
    submit_model.add_argument("--model")
    submit_model.add_argument("--confirm-credit-use", action="store_true")
    generate_refresh = generate_sub.add_parser("refresh")
    generate_refresh.add_argument("project")
    generate_refresh.add_argument("--no-fetch", action="store_true")
    generate_select = generate_sub.add_parser("select")
    generate_select.add_argument("project")
    generate_select.add_argument("candidate")
    build_part = generate_sub.add_parser("build-part")
    build_part.add_argument("project")
    build_part.add_argument("--spec", required=True)
    adopt = generate_sub.add_parser("adopt")
    adopt.add_argument("project")
    adopt.add_argument("--agent-project", required=True)
    return parser


def _print(value: object, *, stream=sys.stdout) -> None:
    print(json.dumps(value, indent=2, sort_keys=True), file=stream)


if __name__ == "__main__":
    raise SystemExit(main())
