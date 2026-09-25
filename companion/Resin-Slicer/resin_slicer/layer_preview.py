from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .formats.readers import inspect_sliced_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m resin_slicer.layer_preview",
        description="Read-only bounded CTB v5 / GOO v3 layer preview helper.",
    )
    parser.add_argument("path")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--info-json", action="store_true")
    mode.add_argument("--layer", type=int)
    mode.add_argument("--layers", help="comma-separated zero-based indices or start:stop ranges; at most 256")
    mode.add_argument("--detached-json", help="inspect detached regions on selected zero-based layers")
    parser.add_argument("--attachment-radius", type=int, default=2)
    parser.add_argument("--min-area-px", type=int, default=1)
    parser.add_argument("--support-cache", help="optional saved plan for diagnostic support-overlap hints")
    parser.add_argument("--scale", type=int, default=2)
    parser.add_argument("--output-dir")
    parser.add_argument("--crop", type=int, nargs=4, metavar=("X", "Y", "WIDTH", "HEIGHT"))
    args = parser.parse_args(argv)

    source = inspect_sliced_file(args.path)
    if args.detached_json is not None:
        from .raster import layer_support_component_summaries
        indices = parse_layer_selection(args.detached_json, source.info.layer_count)
        if min(indices) == 0 or not 0 <= args.attachment_radius <= 16 or args.min_area_px < 1:
            parser.error("detached inspection needs layers > 0, radius 0..16 and positive minimum area")
        records = []
        plan = None
        if args.support_cache:
            from .support_cache import support_plan_from_payload
            plan = support_plan_from_payload(json.loads(Path(args.support_cache).read_text())["plan"])
        for index in indices:
            previous = source.read_layer(index-1).raster
            current = source.read_layer(index).raster
            detached, _ = layer_support_component_summaries(
                current, previous, radius=args.attachment_radius,
                min_unsupported_area_px=args.min_area_px)
            support_layer = None
            if plan is not None:
                from .raster import LayerRaster
                from .supports import apply_supports
                support_layer = LayerRaster(current.width, current.height)
                apply_supports(support_layer, index, plan)
            for component in detached:
                if component.area_px >= args.min_area_px:
                    record = {"layer_index_zero_based": index,
                        "area_px": component.area_px,
                        "bounds_pixels": {"x": component.min_x, "y": component.min_y,
                            "width": component.max_x-component.min_x+1,
                            "height": component.max_y-component.min_y+1}}
                    if support_layer is not None:
                        overlap = 0
                        for y in range(component.min_y, component.max_y+1):
                            for a,b in current.row_spans(y):
                                for c,d in support_layer.row_spans(y):
                                    overlap += max(0, min(b,d,component.max_x+1)-max(a,c,component.min_x))
                        record["support_overlap_pixels_in_bounds"] = overlap
                        record["support_hint_limitation"] = "Current renderer applied to cached plan, not proof of original pixel provenance."
                    records.append(record)
        print(json.dumps({"schema": "resin-slicer-detached-layer-inspection/v1",
            "indices_zero_based": indices, "attachment_radius_px": args.attachment_radius,
            "minimum_area_px": args.min_area_px,
            "records": sorted(records, key=lambda r: r["area_px"], reverse=True)}))
        return 0
    if args.info_json:
        print(
            json.dumps(
                {
                    "format": source.info.format,
                    "version": source.info.version,
                    "resolution": [
                        source.info.resolution_x,
                        source.info.resolution_y,
                    ],
                    "layer_count": source.info.layer_count,
                    "layer_height_mm": source.info.layer_height_mm,
                    "bottom_layers": source.info.bottom_layers,
                    "transition_layers": source.info.transition_layers,
                    "file_size": source.info.file_size,
                    "integrity_checks": list(source.info.integrity_checks),
                },
                separators=(",", ":"),
            )
        )
        return 0

    scale = max(1, min(8, int(args.scale)))
    if args.layers is not None:
        from .raster import LayerRaster
        indices = parse_layer_selection(args.layers, source.info.layer_count)
        if not args.output_dir:
            parser.error("--layers requires --output-dir")
        crop = args.crop or (0, 0, source.info.resolution_x, source.info.resolution_y)
        x, y, width, height = crop
        if min(x, y) < 0 or min(width, height) <= 0 or x+width > source.info.resolution_x or y+height > source.info.resolution_y:
            parser.error("crop is outside the encoded raster")
        if width * height * len(indices) > 1_000_000_000:
            parser.error("batch exceeds one billion preview pixels; use a crop or fewer layers")
        output = Path(args.output_dir).resolve()
        output.mkdir(parents=True, exist_ok=False)
        files = []
        for index in indices:
            layer = source.read_layer(index)
            pixels = layer.raster.pixels
            cropped = bytearray()
            for row in range(y, y+height):
                offset = row * source.info.resolution_x + x
                cropped.extend(pixels[offset:offset+width])
            destination = output / f"layer-{index:05d}.png"
            destination.write_bytes(LayerRaster(width, height, cropped).to_png_bytes(scale=scale))
            files.append(str(destination))
        print(json.dumps({"status": "exported", "indices_zero_based": indices,
                          "crop_pixels": list(crop), "scale": scale, "files": files}))
        return 0
    layer = source.read_layer(args.layer)
    sys.stdout.buffer.write(layer.raster.to_png_bytes(scale=scale))
    return 0


def parse_layer_selection(value: str, count: int) -> list[int]:
    indices = set()
    for token in value.split(","):
        parts = token.strip().split(":")
        if len(parts) == 1:
            indices.add(int(parts[0]))
        elif len(parts) == 2:
            first, last = map(int, parts)
            if first < 0 or last <= first or last > count or last-first > 256:
                raise ValueError("Invalid/bounded layer range")
            indices.update(range(first, last))
        else:
            raise ValueError("Invalid layer selection")
        if len(indices) > 256:
            raise ValueError("At most 256 preview layers per batch")
    if not indices or min(indices) < 0 or max(indices) >= count:
        raise ValueError("Preview layer index outside file")
    return sorted(indices)


if __name__ == "__main__":
    raise SystemExit(main())
