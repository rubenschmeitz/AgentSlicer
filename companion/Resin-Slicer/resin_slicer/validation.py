from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .raster import (
    LayerRaster,
    layer_support_component_summaries,
)


@dataclass(frozen=True)
class FootprintDeficitIssue:
    layer_index: int
    component_area_px: int
    unsupported_area_px: int
    min_x: int
    min_y: int
    max_x: int
    max_y: int


@dataclass(frozen=True)
class LayerStackValidation:
    mode: str
    layers_observed: int
    nonempty_layers: int
    first_nonempty_layer: int | None
    last_nonempty_layer: int | None
    leading_empty_layers: int
    internal_empty_layers: int
    trailing_empty_layers: int
    detached_component_count: int
    detached_pixel_count: int
    largest_detached_component_px: int
    layers_with_detached_components: tuple[int, ...]
    attachment_radius_px: int = 0
    footprint_min_unsupported_area_px: int = 1
    ignored_initial_layers: int = 0
    footprint_deficit_component_count: int = 0
    footprint_deficit_component_pixels: int = 0
    unsupported_footprint_pixels: int = 0
    largest_footprint_deficit_component_px: int = 0
    layers_with_footprint_deficit_components: tuple[int, ...] = ()
    footprint_issue_record_limit: int = 0
    footprint_issue_records_truncated: int = 0
    footprint_deficit_issues: tuple[FootprintDeficitIssue, ...] = ()


@dataclass(frozen=True)
class PostEncodeValidation:
    format: str
    format_version: str
    resolution_x: int
    resolution_y: int
    declared_layer_count: int
    file_size: int
    decoded_layer_bytes: int
    integrity_checks: tuple[str, ...]
    layer_validation: LayerStackValidation


@dataclass(frozen=True)
class LayerValidationParity:
    matches: bool
    differing_fields: tuple[str, ...]


class LayerStackValidator:
    """Streaming structural audit of the exact rasters sent to the writer.

    The audit is deliberately independent of support planning.  It identifies
    complete connected regions that have no pixel footprint on the preceding
    layer and records empty gaps, while retaining only one preceding raster.
    """

    def __init__(
        self,
        mode: str = "pre-encode-native-raster-v1",
        attachment_radius_px: int = 0,
        footprint_min_unsupported_area_px: int = 1,
        ignore_initial_layers: int = 0,
        max_footprint_issue_records: int = 1000,
    ) -> None:
        if not mode:
            raise ValueError("validation mode cannot be empty")
        if attachment_radius_px < 0:
            raise ValueError("attachment radius cannot be negative")
        if footprint_min_unsupported_area_px <= 0:
            raise ValueError("footprint minimum area must be positive")
        if ignore_initial_layers < 0:
            raise ValueError("ignored initial layers cannot be negative")
        if max_footprint_issue_records <= 0:
            raise ValueError("footprint issue record limit must be positive")
        self.mode = mode
        self.attachment_radius_px = attachment_radius_px
        self.footprint_min_unsupported_area_px = footprint_min_unsupported_area_px
        self.ignore_initial_layers = ignore_initial_layers
        self.max_footprint_issue_records = max_footprint_issue_records
        self._previous: LayerRaster | None = None
        self._layers_observed = 0
        self._nonempty_layers = 0
        self._first_nonempty_layer: int | None = None
        self._last_nonempty_layer: int | None = None
        self._leading_empty_layers = 0
        self._pending_empty_layers = 0
        self._internal_empty_layers = 0
        self._detached_component_count = 0
        self._detached_pixel_count = 0
        self._largest_detached_component_px = 0
        self._layers_with_detached_components: list[int] = []
        self._footprint_deficit_component_count = 0
        self._footprint_deficit_component_pixels = 0
        self._unsupported_footprint_pixels = 0
        self._largest_footprint_deficit_component_px = 0
        self._layers_with_footprint_deficit_components: list[int] = []
        self._footprint_deficit_issues: list[FootprintDeficitIssue] = []
        self._footprint_issue_records_truncated = 0

    def observe(self, layer_index: int, layer: LayerRaster) -> None:
        if layer_index != self._layers_observed:
            raise ValueError(
                "layer validation requires consecutive indices starting at zero; "
                f"expected {self._layers_observed}, received {layer_index}"
            )
        pixel_count = layer.count_on()
        if pixel_count == 0:
            if self._first_nonempty_layer is None:
                self._leading_empty_layers += 1
            else:
                self._pending_empty_layers += 1
        else:
            self._nonempty_layers += 1
            if self._first_nonempty_layer is None:
                self._first_nonempty_layer = layer_index
            elif self._pending_empty_layers:
                self._internal_empty_layers += self._pending_empty_layers
                self._pending_empty_layers = 0
            self._last_nonempty_layer = layer_index

            # Layer zero rests on the build plate. Every later connected region
            # must overlap the preceding raster to avoid being a true island.
            if (
                layer_index > 0
                and layer_index >= self.ignore_initial_layers
                and self._previous is not None
            ):
                detached, footprint_deficits = layer_support_component_summaries(
                    layer,
                    self._previous,
                    radius=self.attachment_radius_px,
                    min_unsupported_area_px=self.footprint_min_unsupported_area_px,
                )
                if detached:
                    self._layers_with_detached_components.append(layer_index)
                    self._detached_component_count += len(detached)
                    detached_pixels = sum(component.area_px for component in detached)
                    self._detached_pixel_count += detached_pixels
                    self._largest_detached_component_px = max(
                        self._largest_detached_component_px,
                        max(component.area_px for component in detached),
                    )
                if footprint_deficits:
                    self._layers_with_footprint_deficit_components.append(layer_index)
                    self._footprint_deficit_component_count += len(footprint_deficits)
                    self._footprint_deficit_component_pixels += sum(
                        component.area_px for component in footprint_deficits
                    )
                    self._unsupported_footprint_pixels += sum(
                        component.unsupported_area_px for component in footprint_deficits
                    )
                    self._largest_footprint_deficit_component_px = max(
                        self._largest_footprint_deficit_component_px,
                        max(component.area_px for component in footprint_deficits),
                    )
                    for component in footprint_deficits:
                        if (
                            len(self._footprint_deficit_issues)
                            < self.max_footprint_issue_records
                        ):
                            self._footprint_deficit_issues.append(
                                FootprintDeficitIssue(
                                    layer_index=layer_index,
                                    component_area_px=component.area_px,
                                    unsupported_area_px=component.unsupported_area_px,
                                    min_x=component.min_x,
                                    min_y=component.min_y,
                                    max_x=component.max_x,
                                    max_y=component.max_y,
                                )
                            )
                        else:
                            self._footprint_issue_records_truncated += 1

        self._previous = layer
        self._layers_observed += 1

    def result(self) -> LayerStackValidation:
        return LayerStackValidation(
            mode=self.mode,
            attachment_radius_px=self.attachment_radius_px,
            footprint_min_unsupported_area_px=self.footprint_min_unsupported_area_px,
            ignored_initial_layers=self.ignore_initial_layers,
            layers_observed=self._layers_observed,
            nonempty_layers=self._nonempty_layers,
            first_nonempty_layer=self._first_nonempty_layer,
            last_nonempty_layer=self._last_nonempty_layer,
            leading_empty_layers=self._leading_empty_layers,
            internal_empty_layers=self._internal_empty_layers,
            trailing_empty_layers=self._pending_empty_layers,
            detached_component_count=self._detached_component_count,
            detached_pixel_count=self._detached_pixel_count,
            largest_detached_component_px=self._largest_detached_component_px,
            layers_with_detached_components=tuple(self._layers_with_detached_components),
            footprint_deficit_component_count=self._footprint_deficit_component_count,
            footprint_deficit_component_pixels=self._footprint_deficit_component_pixels,
            unsupported_footprint_pixels=self._unsupported_footprint_pixels,
            largest_footprint_deficit_component_px=(
                self._largest_footprint_deficit_component_px
            ),
            layers_with_footprint_deficit_components=tuple(
                self._layers_with_footprint_deficit_components
            ),
            footprint_issue_record_limit=self.max_footprint_issue_records,
            footprint_issue_records_truncated=self._footprint_issue_records_truncated,
            footprint_deficit_issues=tuple(self._footprint_deficit_issues),
        )


def validate_encoded_file(
    path: str | Path,
    fmt: str,
    *,
    attachment_radius_px: int,
    footprint_min_unsupported_area_px: int,
    ignore_initial_layers: int,
    max_footprint_issue_records: int = 1000,
) -> PostEncodeValidation:
    """Decode the saved file and run the same streaming structural audit."""

    # Local import keeps format writers independent from the validation layer.
    from .formats.readers import inspect_sliced_file

    source = inspect_sliced_file(path, fmt)
    validator = LayerStackValidator(
        mode=f"post-encode-native-decoded-{source.info.format}-v1",
        attachment_radius_px=attachment_radius_px,
        footprint_min_unsupported_area_px=footprint_min_unsupported_area_px,
        ignore_initial_layers=ignore_initial_layers,
        max_footprint_issue_records=max_footprint_issue_records,
    )
    decoded_layer_bytes = 0
    for layer in source.iter_layers():
        validator.observe(layer.index, layer.raster)
        decoded_layer_bytes += layer.encoded_bytes
    result = validator.result()
    if result.layers_observed != source.info.layer_count:
        raise ValueError("decoded layer count does not match sliced-file metadata")
    return PostEncodeValidation(
        format=source.info.format,
        format_version=source.info.version,
        resolution_x=source.info.resolution_x,
        resolution_y=source.info.resolution_y,
        declared_layer_count=source.info.layer_count,
        file_size=source.info.file_size,
        decoded_layer_bytes=decoded_layer_bytes,
        integrity_checks=source.info.integrity_checks,
        layer_validation=result,
    )


def compare_layer_validations(
    pre_encode: LayerStackValidation,
    post_encode: LayerStackValidation,
) -> LayerValidationParity:
    """Compare every result field except the deliberately different mode name."""

    differing = tuple(
        field
        for field in pre_encode.__dataclass_fields__
        if field != "mode" and getattr(pre_encode, field) != getattr(post_encode, field)
    )
    return LayerValidationParity(matches=not differing, differing_fields=differing)
