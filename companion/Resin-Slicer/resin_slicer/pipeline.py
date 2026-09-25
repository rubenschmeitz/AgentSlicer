from __future__ import annotations

import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from math import ceil
from pathlib import Path
from time import perf_counter
from typing import Callable, Iterator

from .config import PrintConfig, SupportConfig
from .errors import ConfigError, FormatError
from .formats.ctb import write_ctb
from .formats.goo import write_goo
from .mesh import Mesh
from .raster import LayerRaster
from .slicing import PreparedMesh, prepare_mesh, render_prepared_layer
from .supports import (
    PaintZone,
    SupportPlan,
    apply_supports,
    attach_raft_masks,
    extend_support_plan,
    plan_supports,
)
from .transform import MeshTransform, apply_transform
from .validation import (
    LayerStackValidation,
    LayerStackValidator,
    LayerValidationParity,
    PostEncodeValidation,
    compare_layer_validations,
    validate_encoded_file,
)

try:
    from .cad_slicing import CadSliceModel, CadSliceSource, prepare_cad_slice_source
except Exception:  # pragma: no cover - CAD dependencies are optional until STEP/B-rep mode is used.
    CadSliceModel = object  # type: ignore[assignment]
    CadSliceSource = object  # type: ignore[assignment]
    prepare_cad_slice_source = None  # type: ignore[assignment]

Progress = Callable[[str], None]


@dataclass(frozen=True)
class SliceJob:
    mesh: Mesh
    print_config: PrintConfig
    support_config: SupportConfig = SupportConfig()
    transform: MeshTransform = MeshTransform()
    preserve_coordinates: bool = False
    raster_mesh: Mesh | None = None
    cad_models: tuple[CadSliceModel, ...] = ()
    cad_slice_mode: str = "tessellated"
    manual_support_points: tuple[tuple[float, float, float], ...] = ()
    support_paint: tuple[PaintZone, ...] = ()
    # Plan only the manual/painted supports (the user never ran the automatic
    # generator, so slicing must not invent supports they did not ask for).
    manual_support_only: bool = False
    # Fail closed when a requested CLI/API contact cannot be routed safely.
    require_manual_supports: bool = False
    # Fail closed when an automatically detected actionable component has no
    # nearby existing support and none of its contacts can be routed safely.
    require_auto_supports: bool = False


@dataclass(frozen=True)
class SliceResult:
    layer_count: int
    support_count: int
    material_ml: float
    output_path: Path
    preview_dir: str | None = None
    manual_supports_requested: int = 0
    manual_supports_routed: int = 0
    manual_supports_routed_points: tuple[tuple[float, float, float], ...] = ()
    manual_supports_actual_contact_points: tuple[tuple[float, float, float], ...] = ()
    manual_supports_unrouted_points: tuple[tuple[float, float, float], ...] = ()
    auto_components_detected: int = 0
    auto_components_supported: int = 0
    auto_components_excluded: int = 0
    auto_components_unrouted: int = 0
    auto_candidates_attempted: int = 0
    auto_candidates_routed: int = 0
    auto_candidates_unrouted: int = 0
    auto_unrouted_area_mm2: float = 0.0
    tree_root_count: int = 0
    tree_branch_count: int = 0
    max_tree_load: int = 0
    encoded_layer_bytes: int = 0
    max_encoded_layer_bytes: int = 0
    output_storage_mode: str = ""
    triangle_count: int = 0
    triangle_index_references: int = 0
    dense_triangle_index_references: int = 0
    triangle_index_block_size: int = 0
    preparation_seconds: float = 0.0
    support_planning_seconds: float = 0.0
    output_writing_seconds: float = 0.0
    total_seconds: float = 0.0
    support_plan_cache_status: str = "disabled"
    layer_validation: LayerStackValidation | None = None
    post_encode_validation: PostEncodeValidation | None = None
    encode_validation_parity: LayerValidationParity | None = None
    trimmed_trailing_empty_layers: int = 0


def slice_to_file(
    job: SliceJob,
    output_path: str | Path,
    fmt: str,
    progress: Progress | None = None,
    layer_workers: int | None = None,
    preview_dir: str | None = None,
    preview_scale: int = 1,
    precomputed_support_plan: SupportPlan | None = None,
    support_plan_callback: Callable[[SupportPlan], None] | None = None,
    support_plan_cache_status: str = "disabled",
) -> SliceResult:
    total_started = perf_counter()
    config = job.print_config
    support_config = job.support_config
    config.validate()
    support_config.validate()
    orientation = MeshTransform(
        rotate_x_deg=job.transform.rotate_x_deg,
        rotate_y_deg=job.transform.rotate_y_deg,
        rotate_z_deg=job.transform.rotate_z_deg,
        scale=job.transform.scale,
    )
    mesh = apply_transform(job.mesh, orientation)
    model_lift = support_config.model_lift_mm if support_config.enabled else 0.0
    prepared, model_prepared, cad_source = _prepare_slice_sources(job, mesh, orientation, config, model_lift)
    preparation_seconds = perf_counter() - total_started
    output = Path(output_path)
    fmt = fmt.lower().lstrip(".")
    if fmt not in {"goo", "ctb"}:
        raise FormatError("output format must be 'goo' or 'ctb'")

    if progress:
        progress(f"prepared mesh: {prepared.layer_count} layers")

    support_plan = SupportPlan((), 0, 0, 0, 0.0, 0)
    supports_enabled = support_config.enabled
    support_started = perf_counter()
    if support_config.enabled:
        if precomputed_support_plan is not None:
            if precomputed_support_plan.manual_requested > len(job.manual_support_points):
                raise ConfigError(
                    "precomputed support plan contains more manual contacts than the job"
                )
            new_manual_points = job.manual_support_points[
                precomputed_support_plan.manual_requested :
            ]
            if new_manual_points:
                if progress:
                    progress(
                        "extending cached support plan with "
                        f"{len(new_manual_points)} new contacts"
                    )
                support_plan = extend_support_plan(
                    prepared,
                    config,
                    support_config,
                    prepared.layer_count,
                    precomputed_support_plan,
                    new_manual_points,
                    progress=progress,
                )
            else:
                support_plan = precomputed_support_plan
            # Preview plans may omit full-resolution raft masks. Persistent
            # plans normally include them so an exact hit avoids projection.
            support_plan = attach_raft_masks(
                support_plan,
                prepared,
                config,
                support_config,
            )
            if progress:
                progress(f"reusing {len(support_plan.anchors)} previously generated supports")
        else:
            if progress:
                progress("planning supports")
            support_plan = plan_supports(
                prepared,
                config,
                support_config,
                prepared.layer_count,
                progress=progress,
                manual_points=job.manual_support_points,
                paint_zones=job.support_paint,
                manual_only=job.manual_support_only,
            )
            if progress:
                progress(f"planned {len(support_plan.anchors)} supports")
        if (
            job.require_manual_supports
            and support_plan.manual_routed < len(job.manual_support_points)
        ):
            raise ConfigError(
                "only "
                f"{support_plan.manual_routed}/{len(job.manual_support_points)} "
                "required manual support contacts could be routed safely"
            )
        if job.require_auto_supports and support_plan.auto_components_unrouted:
            raise ConfigError(
                f"{support_plan.auto_components_unrouted}/"
                f"{support_plan.auto_components_detected} actionable automatic "
                "support components could not be routed safely "
                f"({support_plan.auto_unrouted_area_mm2:.3f} mm^2)"
            )
        supports_enabled = _support_plan_has_geometry(support_plan)
        if not supports_enabled and model_lift > 0:
            if progress:
                progress("no support structure generated; dropping model to build plate")
            model_lift = 0.0
            prepared, model_prepared, cad_source = _prepare_slice_sources(job, mesh, orientation, config, model_lift)

    support_planning_seconds = perf_counter() - support_started
    if support_plan_callback is not None and support_config.enabled:
        support_plan_callback(support_plan)

    if progress:
        progress(f"writing {fmt}")
    writing_started = perf_counter()
    untrimmed_count = prepared.layer_count
    # Height is rounded up, but sections are sampled at layer centers. The
    # final center can therefore lie above every triangle. Test full rasters
    # (including supports/CAD) and remove only a truly empty tail. Never erase
    # internal gaps or any nonzero pixel to make a validation report pass.
    while prepared.layer_count > 0:
        _, tail = _render_layer(prepared, model_prepared, config, supports_enabled,
                                support_plan, prepared.layer_count - 1, cad_source)
        if any(tail.pixels):
            break
        prepared = replace(prepared, layer_count=prepared.layer_count - 1)
    if prepared.layer_count == 0:
        raise ConfigError("No nonempty layers at the selected layer height")
    trimmed_tail = untrimmed_count - prepared.layer_count
    if trimmed_tail and progress:
        progress(f"trimmed {trimmed_tail} empty trailing layers; writing {prepared.layer_count} layers")
    layer_gen = _layer_iter(prepared, model_prepared, config, supports_enabled, support_plan, progress, layer_workers, cad_source)
    layer_validator = LayerStackValidator(
        # A two-pixel (70 um on Mars 3 Pro) footprint tolerance absorbs normal
        # sloped-shaft edge motion. The physical-area floor removes sub-feature
        # specks while keeping substantive first-layer support deficits visible.
        attachment_radius_px=2,
        footprint_min_unsupported_area_px=max(
            1,
            int(ceil(0.02 / config.pixel_area_mm2)),
        ),
        ignore_initial_layers=config.bottom_layers + config.transition_layers,
    )
    layer_gen = _validating_layer_iter(layer_gen, layer_validator)
    if preview_dir:
        layer_gen = _preview_writing_iter(layer_gen, preview_dir, preview_scale)
    # Publish only after the bytes that will be saved have been decoded and
    # audited. A corrupt encoder result therefore cannot replace a prior good
    # printer file.
    output.parent.mkdir(parents=True, exist_ok=True)
    staging_fd, staging_name = tempfile.mkstemp(
        prefix=f".{output.name}.native-validate.",
        suffix=output.suffix,
        dir=output.parent,
    )
    os.close(staging_fd)
    staging = Path(staging_name)
    try:
        if fmt == "goo":
            stats = write_goo(staging, config, prepared.layer_count, layer_gen)
        else:
            stats = write_ctb(staging, config, prepared.layer_count, layer_gen)
        pre_encode_validation = layer_validator.result()
        post_encode_validation = validate_encoded_file(
            staging,
            fmt,
            attachment_radius_px=layer_validator.attachment_radius_px,
            footprint_min_unsupported_area_px=(
                layer_validator.footprint_min_unsupported_area_px
            ),
            ignore_initial_layers=layer_validator.ignore_initial_layers,
            max_footprint_issue_records=layer_validator.max_footprint_issue_records,
        )
        encode_validation_parity = compare_layer_validations(
            pre_encode_validation,
            post_encode_validation.layer_validation,
        )
        if stats.encoded_bytes != post_encode_validation.decoded_layer_bytes:
            raise FormatError(
                "post-encode payload byte count does not match the writer receipt"
            )
        if (
            post_encode_validation.declared_layer_count != prepared.layer_count
            or post_encode_validation.resolution_x != config.resolution_x
            or post_encode_validation.resolution_y != config.resolution_y
        ):
            raise FormatError("post-encode metadata does not match the slice job")
        if not encode_validation_parity.matches:
            raise FormatError(
                "post-encode structural validation differs from pre-encode rasters: "
                + ", ".join(encode_validation_parity.differing_fields)
            )
        os.replace(staging, output)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    output_writing_seconds = perf_counter() - writing_started
    total_seconds = perf_counter() - total_started

    return SliceResult(
        layer_count=prepared.layer_count,
        trimmed_trailing_empty_layers=trimmed_tail,
        support_count=len(support_plan.anchors),
        material_ml=stats.material_mm3 / 1000.0,
        output_path=output,
        preview_dir=preview_dir,
        manual_supports_requested=len(job.manual_support_points),
        manual_supports_routed=support_plan.manual_routed,
        manual_supports_routed_points=support_plan.manual_routed_points,
        manual_supports_actual_contact_points=support_plan.manual_actual_contact_points,
        manual_supports_unrouted_points=support_plan.manual_unrouted_points,
        auto_components_detected=support_plan.auto_components_detected,
        auto_components_supported=support_plan.auto_components_supported,
        auto_components_excluded=support_plan.auto_components_excluded,
        auto_components_unrouted=support_plan.auto_components_unrouted,
        auto_candidates_attempted=support_plan.auto_candidates_attempted,
        auto_candidates_routed=support_plan.auto_candidates_routed,
        auto_candidates_unrouted=support_plan.auto_candidates_unrouted,
        auto_unrouted_area_mm2=support_plan.auto_unrouted_area_mm2,
        tree_root_count=support_plan.tree_root_count,
        tree_branch_count=support_plan.tree_branch_count,
        max_tree_load=support_plan.max_tree_load,
        encoded_layer_bytes=stats.encoded_bytes,
        max_encoded_layer_bytes=stats.max_layer_bytes,
        output_storage_mode=stats.storage_mode,
        triangle_count=len(prepared.mesh.triangles),
        triangle_index_references=prepared.layer_triangles.reference_count,
        dense_triangle_index_references=(
            prepared.layer_triangles.dense_reference_count
        ),
        triangle_index_block_size=prepared.layer_triangles.block_size,
        preparation_seconds=preparation_seconds,
        support_planning_seconds=support_planning_seconds,
        output_writing_seconds=output_writing_seconds,
        total_seconds=total_seconds,
        support_plan_cache_status=support_plan_cache_status,
        layer_validation=pre_encode_validation,
        post_encode_validation=post_encode_validation,
        encode_validation_parity=encode_validation_parity,
    )


def _prepare_slice_sources(
    job: SliceJob,
    mesh: Mesh,
    orientation: MeshTransform,
    config: PrintConfig,
    model_lift: float,
) -> tuple[PreparedMesh, PreparedMesh | None, CadSliceSource | None]:
    z_offset = model_lift + job.transform.translate_z_mm
    prepared = prepare_mesh(
        mesh,
        config,
        z_offset_mm=z_offset,
        xy_offset_mm=(job.transform.translate_x_mm, job.transform.translate_y_mm),
        preserve_coordinates=job.preserve_coordinates,
    )
    model_prepared: PreparedMesh | None = prepared
    if job.raster_mesh is not None:
        raster_mesh = apply_transform(job.raster_mesh, orientation)
        model_prepared = prepare_mesh(
            raster_mesh,
            config,
            z_offset_mm=z_offset,
            xy_offset_mm=(job.transform.translate_x_mm, job.transform.translate_y_mm),
            preserve_coordinates=job.preserve_coordinates,
        )
    elif job.cad_slice_mode == "brep" and job.cad_models:
        model_prepared = None

    cad_source = None
    if job.cad_slice_mode == "brep" and job.cad_models:
        if prepare_cad_slice_source is None:
            raise FormatError("B-rep slicing requires OpenCascade/build123d")
        cad_source = prepare_cad_slice_source(job.cad_models, z_offset_mm=z_offset)
    return prepared, model_prepared, cad_source


def _support_plan_has_geometry(plan: SupportPlan) -> bool:
    return bool(plan.anchors or plan.braces or plan.raft_mask)


def _progress_interval(total: int) -> int:
    return max(1, total // 20)


def _layer_iter(
    support_prepared: PreparedMesh,
    model_prepared: PreparedMesh | None,
    config: PrintConfig,
    supports_enabled: bool,
    support_plan: SupportPlan,
    progress: Progress | None,
    layer_workers: int | None,
    cad_source: CadSliceSource | None = None,
) -> Iterator[tuple[int, LayerRaster]]:
    total = support_prepared.layer_count
    workers = _resolve_layer_workers(layer_workers, total)
    interval = _progress_interval(total)

    if workers <= 1:
        for layer_index in range(total):
            if progress and (layer_index == 0 or (layer_index + 1) % interval == 0):
                progress(f"writing layer {layer_index + 1}/{total}")
            yield _render_layer(support_prepared, model_prepared, config, supports_enabled, support_plan, layer_index, cad_source)
        return

    if progress:
        progress(f"rendering layers with {workers} worker threads")

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="slice-layer") as executor:
        futures = {}
        next_submit = 0
        next_yield = 0

        while next_yield < total:
            while next_submit < total and len(futures) < workers:
                futures[next_submit] = executor.submit(
                    _render_layer,
                    support_prepared,
                    model_prepared,
                    config,
                    supports_enabled,
                    support_plan,
                    next_submit,
                    cad_source,
                )
                next_submit += 1

            layer_index, layer = futures.pop(next_yield).result()
            if progress and (layer_index == 0 or (layer_index + 1) % interval == 0):
                progress(f"writing layer {layer_index + 1}/{total}")
            next_yield += 1
            yield layer_index, layer


def _preview_writing_iter(
    base_iter: Iterator[tuple[int, LayerRaster]],
    preview_dir: str,
    scale: int,
) -> Iterator[tuple[int, LayerRaster]]:
    for layer_index, layer in base_iter:
        png = layer.to_png_bytes(scale)
        path = os.path.join(preview_dir, f"layer_{layer_index + 1:05d}.png")
        with open(path, "wb") as fh:
            fh.write(png)
        yield layer_index, layer


def _validating_layer_iter(
    base_iter: Iterator[tuple[int, LayerRaster]],
    validator: LayerStackValidator,
) -> Iterator[tuple[int, LayerRaster]]:
    for layer_index, layer in base_iter:
        validator.observe(layer_index, layer)
        yield layer_index, layer


def _render_layer(
    support_prepared: PreparedMesh,
    model_prepared: PreparedMesh | None,
    config: PrintConfig,
    supports_enabled: bool,
    support_plan: SupportPlan,
    layer_index: int,
    cad_source: CadSliceSource | None = None,
) -> tuple[int, LayerRaster]:
    if cad_source is not None:
        layer = cad_source.render_layer(layer_index, config)
        if model_prepared is not None:
            layer.or_with(render_prepared_layer(model_prepared, config, layer_index))
    else:
        layer = render_prepared_layer(model_prepared or support_prepared, config, layer_index)
    if supports_enabled:
        apply_supports(layer, layer_index, support_plan)
    return layer_index, layer


def _resolve_layer_workers(requested: int | None, layer_count: int) -> int:
    if layer_count <= 1:
        return 1
    if requested is None:
        requested = _env_layer_workers()
    if requested is None:
        # Layer rendering is Python-heavy and therefore GIL-bound. Multiple
        # threads add full-resolution raster buffers without improving the
        # measured single-process throughput; default to the bounded path.
        requested = 1
    return max(1, min(layer_count, int(requested)))


def _env_layer_workers() -> int | None:
    value = os.environ.get("RESIN_SLICER_LAYER_WORKERS")
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None
