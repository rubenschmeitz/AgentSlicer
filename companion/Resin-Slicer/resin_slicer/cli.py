from __future__ import annotations

import argparse
import json
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .config import PrintConfig, SupportConfig, profile
from .errors import SlicerError
from .mesh import load_mesh
from .pipeline import SliceJob, SliceResult, slice_to_file
from .support_cache import (
    SupportPlanCacheLookup,
    load_support_plan_cache,
    store_support_plan_cache,
    support_plan_identity,
)
from .transform import MeshTransform
from .validation import LayerStackValidation


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        config = _config_from_args(args)
        supports = SupportConfig(
            enabled=not args.no_supports,
            model_lift_mm=args.model_lift,
            overhang_angle_deg=args.overhang_angle,
            support_spacing_mm=args.support_spacing,
            primary_supports_enabled=args.primary_supports,
            primary_density_multiplier=args.primary_density_multiplier,
            primary_area_radius_mm=args.primary_area_radius,
            primary_max_extra_per_island=args.primary_max_extra,
            post_radius_mm=args.support_radius,
            tip_radius_mm=args.tip_radius,
            tip_type=args.tip_type,
            tip_length_mm=args.tip_length,
            tip_contact_depth_mm=args.tip_contact_depth,
            foot_radius_mm=args.foot_radius,
            bed_interface=args.bed_interface,
            raft_margin_mm=args.raft_offset,
            raft_chamfer_width_mm=args.raft_chamfer_width,
            raft_chamfer_angle_deg=args.raft_chamfer_angle,
            bed_interface_thickness_mm=args.bed_interface_thickness,
            brace_enabled=not args.no_braces,
            brace_radius_mm=args.brace_radius,
            brace_height_mm=args.brace_height,
            brace_max_distance_mm=args.brace_distance,
            brace_interval_mm=args.brace_interval,
            collision_clearance_mm=args.collision_clearance,
            max_base_reach_mm=args.max_base_reach,
            max_support_angle_deg=args.max_support_angle,
            branching_enabled=not args.no_tree_branching,
            tree_merge_distance_mm=args.tree_merge_distance,
            tree_max_tips=args.tree_max_tips,
            tree_trunk_radius_mm=args.tree_trunk_radius,
            pedestal_height_mm=args.pedestal_height,
            enforcers_enabled=args.enforcers,
            enforcer_reach_mm=args.enforcer_reach,
            enforcer_min_drop_mm=args.enforcer_min_drop,
            min_island_area_mm2=args.min_island_area,
            analysis_max_pixels=args.support_analysis_pixels,
        )
        transform = MeshTransform(
            rotate_x_deg=args.rotate_x,
            rotate_y_deg=args.rotate_y,
            rotate_z_deg=args.rotate_z,
            scale=args.scale,
            translate_x_mm=args.translate[0],
            translate_y_mm=args.translate[1],
            translate_z_mm=args.translate[2],
        )
        supports.validate()
        transform.validate()
        manual_points = tuple(args.manual_support)
        cache_identity = None
        cache_lookup = SupportPlanCacheLookup(None, "disabled")
        if args.support_plan_cache_dir and supports.enabled:
            cache_identity = support_plan_identity(
                args.input,
                config,
                supports,
                transform,
            )
            cache_lookup = load_support_plan_cache(
                args.support_plan_cache_dir,
                cache_identity,
                manual_points,
                config,
            )
            if args.verbose:
                print(
                    f"support plan cache: {cache_lookup.status}",
                    file=sys.stderr,
                )
        mesh = load_mesh(args.input)
        fmt = args.format or Path(args.output).suffix.lstrip(".")

        def store_plan(plan) -> None:
            if cache_identity is None:
                return
            try:
                store_support_plan_cache(
                    args.support_plan_cache_dir,
                    cache_identity,
                    manual_points,
                    plan,
                )
            except (OSError, TypeError, ValueError):
                # Cache persistence must never invalidate an otherwise valid
                # slice. A later invocation simply performs a full plan.
                if args.verbose:
                    print("support plan cache: store failed", file=sys.stderr)

        result = slice_to_file(
            SliceJob(
                mesh,
                config,
                supports,
                transform,
                manual_support_points=manual_points,
                require_manual_supports=args.require_manual_supports,
                require_auto_supports=args.require_auto_supports,
            ),
            args.output,
            fmt,
            progress=(lambda message: print(message, file=sys.stderr)) if args.verbose else None,
            layer_workers=args.workers,
            precomputed_support_plan=cache_lookup.plan,
            support_plan_callback=store_plan if cache_identity is not None else None,
            support_plan_cache_status=cache_lookup.status,
        )
    except SlicerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    manual = ""
    if result.manual_supports_requested:
        manual = (
            f", {result.manual_supports_routed}/"
            f"{result.manual_supports_requested} manual contacts routed"
        )
    if args.report_json:
        _write_report_json(Path(args.report_json), result, fmt)
    print(
        f"wrote {result.output_path} ({result.layer_count} layers, "
        f"{result.support_count} supports{manual}, "
        f"{result.material_ml:.2f} ml estimated resin)"
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Slice an STL, OBJ, STEP, or STP model for MSLA resin printers.")
    try:
        installed_version = version("resin-slicer")
    except PackageNotFoundError:  # Source checkout before package installation.
        installed_version = "source"
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {installed_version}",
    )
    parser.add_argument("input", help="input STL, OBJ, STEP, or STP file")
    parser.add_argument("-o", "--output", required=True, help="output .goo or .ctb path")
    parser.add_argument("--format", choices=["goo", "ctb"], help="output format; defaults to output extension")
    parser.add_argument("--profile", default="generic-2k", help="printer profile name")
    parser.add_argument("--resolution", help="override resolution as WIDTHxHEIGHT pixels")
    parser.add_argument("--volume", help="override build volume as XxYxZ millimeters")
    parser.add_argument("--layer-height", type=float, help="layer height in millimeters")
    parser.add_argument("--exposure", type=float, help="normal exposure in seconds")
    parser.add_argument("--bottom-exposure", type=float, help="bottom exposure in seconds")
    parser.add_argument("--bottom-layers", type=int, help="number of bottom layers")
    parser.add_argument("--transition-layers", type=int, help="number of transition layers")
    parser.add_argument("--lift-distance", type=float, help="normal lift distance in millimeters")
    parser.add_argument("--lift-speed", type=float, help="normal lift speed in millimeters per minute")
    parser.add_argument("--retract-distance", type=float, help="retract distance in millimeters")
    parser.add_argument("--retract-speed", type=float, help="retract speed in millimeters per minute")
    parser.add_argument("--wait-after-retract", type=float, help="wait time after retract in seconds")
    parser.add_argument("--light-pwm", type=int, help="normal layer light PWM, 0..255")
    parser.add_argument("--bottom-light-pwm", type=int, help="bottom layer light PWM, 0..255")
    parser.add_argument("--machine-name", help="machine name written to the output file")
    parser.add_argument("--resin-name", help="resin/profile name written to the output file")
    parser.add_argument("--resin-density", type=float, help="resin density in grams per milliliter")
    parser.add_argument("--no-center", action="store_true", help="place mesh at its source-file origin instead of centering")
    parser.add_argument("--rotate-x", type=float, default=0.0, help="rotate model around its center on the X axis, degrees")
    parser.add_argument("--rotate-y", type=float, default=0.0, help="rotate model around its center on the Y axis, degrees")
    parser.add_argument("--rotate-z", type=float, default=0.0, help="rotate model around its center on the Z axis, degrees")
    parser.add_argument("--scale", type=float, default=1.0, help="uniform model scale applied before placement")
    parser.add_argument(
        "--translate",
        type=_parse_translate_arg,
        default=(0.0, 0.0, 0.0),
        help="translate model as XxYxZ millimeters before final placement",
    )
    parser.add_argument("--max-pixels-per-layer", type=int, help="raise or lower layer memory safety limit")
    parser.add_argument(
        "--workers",
        type=int,
        help="worker threads for layer rendering; defaults to RESIN_SLICER_LAYER_WORKERS or 1",
    )
    parser.add_argument("--no-supports", action="store_true", help="disable automatic support generation")
    parser.add_argument("--model-lift", type=float, default=5.0, help="raise supported models this many millimeters above the plate")
    parser.add_argument(
        "--overhang-angle",
        type=float,
        default=45.0,
        help="minimum self-supporting angle from the build plate in degrees; higher values create more supports",
    )
    parser.add_argument("--support-spacing", type=float, default=3.0, help="target support spacing in millimeters")
    parser.add_argument("--primary-supports", action="store_true", help="add denser primary supports around each detected island")
    parser.add_argument("--primary-density-multiplier", type=float, default=2.0, help="density multiplier for primary island attachment regions")
    parser.add_argument("--primary-area-radius", type=float, default=4.0, help="primary attachment region radius around each island centroid in millimeters")
    parser.add_argument("--primary-max-extra", type=int, default=8, help="maximum extra primary supports per island")
    parser.add_argument("--support-radius", type=float, default=0.28, help="support post radius in millimeters")
    parser.add_argument("--tip-radius", type=float, default=0.18, help="support contact/tip radius in millimeters")
    parser.add_argument("--tip-type", choices=["cone", "sphere", "cylinder"], default="cone", help="tip radius profile")
    parser.add_argument("--tip-length", type=float, default=0.8, help="tip segment length in millimeters")
    parser.add_argument("--tip-contact-depth", type=float, default=0.1, help="vertical narrow-tip overlap into the model, 0 to 0.5 mm")
    parser.add_argument("--foot-radius", type=float, default=0.8, help="support foot radius in millimeters")
    parser.add_argument(
        "--bed-interface",
        choices=["none", "feet", "raft", "skate"],
        default="raft",
        help="build-plate interface style for support bases",
    )
    parser.add_argument(
        "--raft-offset",
        "--raft-margin",
        dest="raft_offset",
        type=float,
        default=0.6,
        help="offset distance around the projected model footprint for raft/skate interfaces",
    )
    parser.add_argument("--raft-chamfer-width", type=float, default=0.4, help="horizontal width of the raft bottom-edge chamfer in millimeters")
    parser.add_argument("--raft-chamfer-angle", type=float, default=45.0, help="raft bottom-edge chamfer angle in degrees")
    parser.add_argument("--bed-interface-thickness", type=float, default=0.35, help="bed support interface thickness in millimeters")
    parser.add_argument("--no-braces", action="store_true", help="disable cross-bracing between nearby supports")
    parser.add_argument("--brace-radius", type=float, default=0.18, help="cross-brace radius in millimeters")
    parser.add_argument("--brace-height", type=float, default=3.0, help="starting height for diagonal braces above the bed in millimeters")
    parser.add_argument("--brace-distance", type=float, default=8.0, help="maximum distance between braced supports in millimeters")
    parser.add_argument("--brace-interval", type=float, default=0.0, help="repeat braces at this vertical interval in mm; 0 uses one level")
    parser.add_argument("--collision-clearance", type=float, default=0.08, help="clearance around routed supports during collision checks")
    parser.add_argument("--max-base-reach", type=float, default=45.0, help="maximum horizontal search distance for support bases")
    parser.add_argument("--max-support-angle", type=float, default=35.0, help="maximum shaft angle from vertical for generated supports")
    parser.add_argument(
        "--no-tree-branching",
        action="store_true",
        help="disable proactive resin-tree merging into existing safe support shafts",
    )
    parser.add_argument(
        "--tree-merge-distance",
        type=float,
        default=4.0,
        help="maximum local horizontal distance for proactive tree merging, millimeters",
    )
    parser.add_argument(
        "--tree-max-tips",
        type=int,
        default=12,
        help="maximum model-contact tips carried by one generated tree root",
    )
    parser.add_argument(
        "--tree-trunk-radius",
        type=float,
        default=0.65,
        help="maximum load-bearing tree trunk radius in millimeters",
    )
    parser.add_argument(
        "--pedestal-height",
        type=float,
        default=2.0,
        help="height of the tapered support foot above the pad, millimeters",
    )
    parser.add_argument("--enforcers", action="store_true", help="allow part-to-part enforcer supports for bed-inaccessible regions")
    parser.add_argument("--enforcer-reach", type=float, default=10.0, help="maximum horizontal part-to-part enforcer search distance")
    parser.add_argument("--enforcer-min-drop", type=float, default=1.0, help="minimum vertical drop for part-to-part enforcers")
    parser.add_argument("--min-island-area", type=float, default=0.08, help="minimum unsupported island area in mm^2")
    parser.add_argument(
        "--support-analysis-pixels",
        type=int,
        default=250_000,
        help="pixel budget for automatic support analysis before scaling anchors to output resolution",
    )
    parser.add_argument(
        "--support-plan-cache-dir",
        help=(
            "persist content-addressed support topology in this directory; exact "
            "retries reuse it and validation feedback appends only new contacts"
        ),
    )
    parser.add_argument(
        "--manual-support",
        action="append",
        type=_parse_translate_arg,
        default=[],
        metavar="XxYxZ",
        help=(
            "add a routed support contact at build-plate X/Y/Z millimeters; "
            "repeat for multiple contacts"
        ),
    )
    parser.add_argument(
        "--report-json",
        help=(
            "write a machine-readable slice receipt including the exact routed "
            "and unrouted manual contact coordinates"
        ),
    )
    parser.add_argument(
        "--require-manual-supports",
        action="store_true",
        help=(
            "fail instead of silently omitting any manual support contact that "
            "cannot be routed without collision"
        ),
    )
    parser.add_argument(
        "--require-auto-supports",
        action="store_true",
        help=(
            "fail when any actionable automatically detected component has no "
            "nearby accepted contact and cannot be routed safely"
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="print progress to stderr")
    return parser


def _write_report_json(path: Path, result: SliceResult, fmt: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    validation = result.layer_validation
    payload = {
        "schema": "resin-slicer-slice-report-v1",
        "output": str(result.output_path),
        "format": fmt.lower().lstrip("."),
        "layer_count": result.layer_count,
        "trimmed_trailing_empty_layers": result.trimmed_trailing_empty_layers,
        "support_count": result.support_count,
        "material_ml": result.material_ml,
        "output_writer": {
            "mode": result.output_storage_mode,
            "encoded_layer_bytes": result.encoded_layer_bytes,
            "max_encoded_layer_bytes": result.max_encoded_layer_bytes,
        },
        "slicing_index": {
            "triangle_count": result.triangle_count,
            "block_size_layers": result.triangle_index_block_size,
            "stored_references": result.triangle_index_references,
            "dense_equivalent_references": (
                result.dense_triangle_index_references
            ),
        },
        "automatic_supports": {
            "components_detected": result.auto_components_detected,
            "components_supported": result.auto_components_supported,
            "components_excluded": result.auto_components_excluded,
            "components_unrouted": result.auto_components_unrouted,
            "candidates_attempted": result.auto_candidates_attempted,
            "candidates_routed": result.auto_candidates_routed,
            "candidates_unrouted": result.auto_candidates_unrouted,
            "unrouted_area_mm2": result.auto_unrouted_area_mm2,
        },
        "support_tree": {
            "roots": result.tree_root_count,
            "branches": result.tree_branch_count,
            "max_tip_load": result.max_tree_load,
        },
        "support_plan_cache": {
            "status": result.support_plan_cache_status,
        },
        "performance": {
            "preparation_seconds": result.preparation_seconds,
            "support_planning_seconds": result.support_planning_seconds,
            "output_writing_seconds": result.output_writing_seconds,
            "pipeline_total_seconds": result.total_seconds,
        },
        "internal_layer_validation": (
            None
            if validation is None
            else {
                "mode": validation.mode,
                "scope": "pre-encode raster stream",
                "attachment_radius_px": validation.attachment_radius_px,
                "footprint_min_unsupported_area_px": (
                    validation.footprint_min_unsupported_area_px
                ),
                "ignored_initial_layers": validation.ignored_initial_layers,
                "layers_observed": validation.layers_observed,
                "nonempty_layers": validation.nonempty_layers,
                "first_nonempty_layer_zero_based": validation.first_nonempty_layer,
                "last_nonempty_layer_zero_based": validation.last_nonempty_layer,
                "leading_empty_layers": validation.leading_empty_layers,
                "internal_empty_layers": validation.internal_empty_layers,
                "trailing_empty_layers": validation.trailing_empty_layers,
                "detached_component_count": validation.detached_component_count,
                "detached_pixel_count": validation.detached_pixel_count,
                "largest_detached_component_px": validation.largest_detached_component_px,
                "layers_with_detached_components_zero_based": list(
                    validation.layers_with_detached_components
                ),
                "footprint_deficit_component_count": (
                    validation.footprint_deficit_component_count
                ),
                "footprint_deficit_component_pixels": (
                    validation.footprint_deficit_component_pixels
                ),
                "unsupported_footprint_pixels": validation.unsupported_footprint_pixels,
                "largest_footprint_deficit_component_px": (
                    validation.largest_footprint_deficit_component_px
                ),
                "layers_with_footprint_deficit_components_zero_based": list(
                    validation.layers_with_footprint_deficit_components
                ),
                "footprint_issue_record_limit": validation.footprint_issue_record_limit,
                "footprint_issue_records_truncated": (
                    validation.footprint_issue_records_truncated
                ),
                "footprint_deficit_issues": [
                    {
                        "layer_index_zero_based": issue.layer_index,
                        "component_area_px": issue.component_area_px,
                        "unsupported_area_px": issue.unsupported_area_px,
                        "bounds_px": {
                            "min_x": issue.min_x,
                            "min_y": issue.min_y,
                            "max_x": issue.max_x,
                            "max_y": issue.max_y,
                        },
                    }
                    for issue in validation.footprint_deficit_issues
                ],
            }
        ),
        "manual_supports": {
            "requested": result.manual_supports_requested,
            "routed": result.manual_supports_routed,
            "routed_points_mm": [list(point) for point in result.manual_supports_routed_points],
            "actual_contact_points_mm": [
                list(point) for point in result.manual_supports_actual_contact_points
            ],
            "unrouted_points_mm": [list(point) for point in result.manual_supports_unrouted_points],
        },
    }
    post_encode = result.post_encode_validation
    payload["post_encode_validation"] = (
        None
        if post_encode is None
        else {
            "format": post_encode.format,
            "format_version": post_encode.format_version,
            "resolution": [post_encode.resolution_x, post_encode.resolution_y],
            "declared_layer_count": post_encode.declared_layer_count,
            "file_size_bytes": post_encode.file_size,
            "decoded_layer_bytes": post_encode.decoded_layer_bytes,
            "integrity_checks": list(post_encode.integrity_checks),
            "layer_validation": _layer_validation_payload(
                post_encode.layer_validation,
                "decoded saved-file layer stream",
            ),
        }
    )
    parity = result.encode_validation_parity
    payload["encode_validation_parity"] = (
        None
        if parity is None
        else {
            "matches": parity.matches,
            "differing_fields": list(parity.differing_fields),
        }
    )
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _layer_validation_payload(
    validation: LayerStackValidation,
    scope: str,
) -> dict[str, object]:
    # Kept separate from the historical pre-encode key so report consumers can
    # adopt the post-encode receipt without a schema-breaking rename.
    value = validation
    return {
        "mode": value.mode,
        "scope": scope,
        "attachment_radius_px": value.attachment_radius_px,
        "footprint_min_unsupported_area_px": value.footprint_min_unsupported_area_px,
        "ignored_initial_layers": value.ignored_initial_layers,
        "layers_observed": value.layers_observed,
        "nonempty_layers": value.nonempty_layers,
        "first_nonempty_layer_zero_based": value.first_nonempty_layer,
        "last_nonempty_layer_zero_based": value.last_nonempty_layer,
        "leading_empty_layers": value.leading_empty_layers,
        "internal_empty_layers": value.internal_empty_layers,
        "trailing_empty_layers": value.trailing_empty_layers,
        "detached_component_count": value.detached_component_count,
        "detached_pixel_count": value.detached_pixel_count,
        "largest_detached_component_px": value.largest_detached_component_px,
        "layers_with_detached_components_zero_based": list(
            value.layers_with_detached_components
        ),
        "footprint_deficit_component_count": value.footprint_deficit_component_count,
        "footprint_deficit_component_pixels": value.footprint_deficit_component_pixels,
        "unsupported_footprint_pixels": value.unsupported_footprint_pixels,
        "largest_footprint_deficit_component_px": (
            value.largest_footprint_deficit_component_px
        ),
        "layers_with_footprint_deficit_components_zero_based": list(
            value.layers_with_footprint_deficit_components
        ),
        "footprint_issue_record_limit": value.footprint_issue_record_limit,
        "footprint_issue_records_truncated": value.footprint_issue_records_truncated,
        "footprint_deficit_issues": [
            {
                "layer_index_zero_based": issue.layer_index,
                "component_area_px": issue.component_area_px,
                "unsupported_area_px": issue.unsupported_area_px,
                "bounds_px": {
                    "min_x": issue.min_x,
                    "min_y": issue.min_y,
                    "max_x": issue.max_x,
                    "max_y": issue.max_y,
                },
            }
            for issue in value.footprint_deficit_issues
        ],
    }


def _config_from_args(args: argparse.Namespace) -> PrintConfig:
    config = profile(args.profile)
    overrides: dict[str, object] = {}
    if args.resolution:
        x, y = _parse_pair(args.resolution, "resolution")
        overrides["resolution_x"] = int(x)
        overrides["resolution_y"] = int(y)
    if args.volume:
        x, y, z = _parse_triple(args.volume, "volume")
        overrides["size_x_mm"] = x
        overrides["size_y_mm"] = y
        overrides["size_z_mm"] = z
    if args.layer_height is not None:
        overrides["layer_height_mm"] = args.layer_height
    if args.exposure is not None:
        overrides["exposure_time_s"] = args.exposure
    if args.bottom_exposure is not None:
        overrides["bottom_exposure_time_s"] = args.bottom_exposure
    if args.bottom_layers is not None:
        overrides["bottom_layers"] = args.bottom_layers
    if args.transition_layers is not None:
        overrides["transition_layers"] = args.transition_layers
    if args.lift_distance is not None:
        overrides["lift_distance_mm"] = args.lift_distance
    if args.lift_speed is not None:
        overrides["lift_speed_mm_min"] = args.lift_speed
    if args.retract_distance is not None:
        overrides["retract_distance_mm"] = args.retract_distance
    if args.retract_speed is not None:
        overrides["retract_speed_mm_min"] = args.retract_speed
    if args.wait_after_retract is not None:
        overrides["wait_after_retract_s"] = args.wait_after_retract
    if args.light_pwm is not None:
        overrides["light_pwm"] = args.light_pwm
    if args.bottom_light_pwm is not None:
        overrides["bottom_light_pwm"] = args.bottom_light_pwm
    if args.machine_name is not None:
        overrides["machine_name"] = args.machine_name
    if args.resin_name is not None:
        overrides["resin_name"] = args.resin_name
    if args.resin_density is not None:
        overrides["resin_density_g_ml"] = args.resin_density
    if args.no_center:
        overrides["center_model"] = False
    if args.max_pixels_per_layer is not None:
        overrides["max_pixels_per_layer"] = args.max_pixels_per_layer
    return config.with_overrides(**overrides)


def _parse_pair(value: str, label: str) -> tuple[float, float]:
    parts = value.lower().split("x")
    if len(parts) != 2:
        raise SlicerError(f"{label} must be formatted as XxY")
    return float(parts[0]), float(parts[1])


def _parse_triple(value: str, label: str) -> tuple[float, float, float]:
    parts = value.lower().split("x")
    if len(parts) != 3:
        raise SlicerError(f"{label} must be formatted as XxYxZ")
    return float(parts[0]), float(parts[1]), float(parts[2])


def _parse_translate_arg(value: str) -> tuple[float, float, float]:
    return _parse_triple(value, "translate")


if __name__ == "__main__":
    raise SystemExit(main())
