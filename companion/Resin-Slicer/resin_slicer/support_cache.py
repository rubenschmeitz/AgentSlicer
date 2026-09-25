from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import time
import zlib
from dataclasses import asdict, dataclass, fields
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import PrintConfig, SupportConfig
from .supports import SupportAnchor, SupportBrace, SupportPlan
from .transform import MeshTransform

_SCHEMA = "resin-slicer-support-plan-cache/v1"
_CACHE_FORMAT_VERSION = 1
# Bump whenever support topology/routing semantics change without a cache schema
# change.  This is intentionally part of the identity, not merely metadata.
_SUPPORT_ALGORITHM_VERSION = 2
_MAX_CACHE_BYTES = 256 * 1024 * 1024
_MAX_CACHE_ENTRIES = 32
_MAX_ANCHORS = 1_000_000
_MAX_BRACES = 2_000_000


@dataclass(frozen=True)
class SupportPlanCacheLookup:
    plan: SupportPlan | None
    status: str
    key: str | None = None
    path: Path | None = None


def support_plan_identity(
    source: str | Path,
    print_config: PrintConfig,
    support_config: SupportConfig,
    transform: MeshTransform,
    *,
    preserve_coordinates: bool = False,
    manual_only: bool = False,
) -> dict[str, Any]:
    """Build a content-bound identity for reusable support geometry."""

    path = Path(source)
    stat = path.stat()
    identity = {
        "cache_format_version": _CACHE_FORMAT_VERSION,
        "support_algorithm_version": _SUPPORT_ALGORITHM_VERSION,
        "support_implementation_sha256": _support_implementation_sha256(),
        "source": {
            "sha256": _sha256_file(path),
            "size": stat.st_size,
        },
        "print_config": asdict(print_config),
        "support_config": asdict(support_config),
        "transform": asdict(transform),
        "preserve_coordinates": bool(preserve_coordinates),
        "manual_only": bool(manual_only),
    }
    # Normalize tuples and any future JSON-compatible configuration containers
    # now so the in-memory identity compares exactly with its persisted form.
    return json.loads(
        json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False)
    )


def load_support_plan_cache(
    cache_dir: str | Path | None,
    identity: dict[str, Any],
    manual_points: tuple[tuple[float, float, float], ...],
    print_config: PrintConfig,
) -> SupportPlanCacheLookup:
    if cache_dir is None:
        return SupportPlanCacheLookup(None, "disabled")
    key = _identity_key(identity)
    path = Path(cache_dir) / f"{key}.json"
    if not path.is_file():
        return SupportPlanCacheLookup(None, "miss", key, path)
    try:
        if path.stat().st_size > _MAX_CACHE_BYTES:
            raise ValueError("cache entry exceeds safety limit")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema") != _SCHEMA:
            raise ValueError("unsupported cache schema")
        if payload.get("identity") != identity:
            return SupportPlanCacheLookup(None, "miss-identity", key, path)
        cached_points = _points(payload.get("manual_points"), "manual_points")
        requested_points = _points(manual_points, "requested manual points")
        if len(cached_points) > len(requested_points) or cached_points != requested_points[: len(cached_points)]:
            return SupportPlanCacheLookup(None, "miss-manual-prefix", key, path)
        plan_payload = payload.get("plan")
        if not isinstance(plan_payload, dict):
            raise ValueError("cache plan is not an object")
        if payload.get("plan_sha256") != _json_sha256(plan_payload):
            raise ValueError("cache plan checksum mismatch")
        plan = support_plan_from_payload(plan_payload, print_config)
        if plan.manual_requested != len(cached_points):
            raise ValueError("cache manual receipt does not match cached request")
        status = "hit-exact" if len(cached_points) == len(requested_points) else "hit-prefix"
        return SupportPlanCacheLookup(plan, status, key, path)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        # A cache is an optimization, never an authority.  Corrupt or stale
        # entries fall back to a complete plan and are atomically replaced.
        return SupportPlanCacheLookup(None, "miss-corrupt", key, path)


def store_support_plan_cache(
    cache_dir: str | Path | None,
    identity: dict[str, Any],
    manual_points: tuple[tuple[float, float, float], ...],
    plan: SupportPlan,
) -> Path | None:
    if cache_dir is None:
        return None
    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    key = _identity_key(identity)
    destination = directory / f"{key}.json"
    plan_payload = support_plan_to_payload(plan)
    payload = {
        "schema": _SCHEMA,
        "identity": identity,
        "manual_points": [list(point) for point in manual_points],
        "plan_sha256": _json_sha256(plan_payload),
        "plan": plan_payload,
    }
    temporary = directory / f".{key}.{os.getpid()}.tmp"
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        _prune(directory, keep=destination)
        return destination
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def support_plan_to_payload(plan: SupportPlan) -> dict[str, Any]:
    payload = asdict(plan)
    # Binary raster masks compress extremely well and avoiding their
    # triangle-by-triangle reconstruction materially improves exact retries.
    # The decoder verifies codec, size, digest, and an output bound before use.
    payload["raft_mask"] = _encode_mask(plan.raft_mask)
    payload["raft_shadow_mask"] = _encode_mask(plan.raft_shadow_mask)
    return payload


def support_plan_from_payload(
    payload: dict[str, Any],
    print_config: PrintConfig | None = None,
) -> SupportPlan:
    expected = {field.name for field in fields(SupportPlan)}
    if set(payload) != expected:
        raise ValueError("cache plan fields do not match this implementation")
    raw_anchors = payload.get("anchors")
    raw_braces = payload.get("braces")
    if not isinstance(raw_anchors, (list, tuple)) or len(raw_anchors) > _MAX_ANCHORS:
        raise ValueError("invalid cached anchor list")
    if not isinstance(raw_braces, (list, tuple)) or len(raw_braces) > _MAX_BRACES:
        raise ValueError("invalid cached brace list")
    anchor_fields = {field.name for field in fields(SupportAnchor)}
    brace_fields = {field.name for field in fields(SupportBrace)}
    anchors: list[SupportAnchor] = []
    for raw in raw_anchors:
        if not isinstance(raw, dict) or set(raw) != anchor_fields:
            raise ValueError("invalid cached anchor")
        anchors.append(SupportAnchor(**raw))
    braces: list[SupportBrace] = []
    for raw in raw_braces:
        if not isinstance(raw, dict) or set(raw) != brace_fields:
            raise ValueError("invalid cached brace")
        braces.append(SupportBrace(**raw))

    values = dict(payload)
    values["anchors"] = tuple(anchors)
    values["braces"] = tuple(braces)
    for name in (
        "manual_routed_points",
        "manual_actual_contact_points",
        "manual_unrouted_points",
    ):
        values[name] = _points(values[name], name)
    expected_mask_size = None
    if print_config is not None:
        expected_mask_size = print_config.resolution_x * print_config.resolution_y
    values["raft_mask"] = _decode_mask(values.get("raft_mask"), expected_mask_size)
    values["raft_shadow_mask"] = _decode_mask(
        values.get("raft_shadow_mask"),
        expected_mask_size,
    )
    plan = SupportPlan(**values)
    _validate_plan(plan, print_config)
    return plan


def _validate_plan(plan: SupportPlan, config: PrintConfig | None) -> None:
    integer_fields = (
        "post_radius_px",
        "tip_radius_px",
        "foot_radius_px",
        "raft_layers",
        "contact_depth_layers",
        "raft_radius_px",
        "brace_layer_radius",
        "tree_trunk_radius_px",
        "pedestal_height_layers",
        "raft_offset_px",
        "manual_requested",
        "manual_routed",
        "auto_components_detected",
        "auto_components_supported",
        "auto_components_excluded",
        "auto_components_unrouted",
        "auto_candidates_attempted",
        "auto_candidates_routed",
        "auto_candidates_unrouted",
        "tree_root_count",
        "tree_branch_count",
        "max_tree_load",
    )
    for name in integer_fields:
        value = getattr(plan, name)
        if type(value) is not int or value < 0:
            raise ValueError(f"invalid cached integer {name}")
    for name in (
        "joint_sphere_radius_mm",
        "contact_sphere_radius_mm",
        "contact_sphere_inset_mm",
        "pixel_size_x_mm",
        "pixel_size_y_mm",
        "layer_height_mm",
        "raft_chamfer_width_mm",
        "raft_chamfer_angle_deg",
        "auto_unrouted_area_mm2",
    ):
        value = getattr(plan, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
            raise ValueError(f"invalid cached number {name}")
    if type(plan.spherical_contact_enabled) is not bool:
        raise ValueError("invalid cached contact mode")
    if plan.bed_interface not in {"none", "feet", "raft", "skate"}:
        raise ValueError("invalid cached bed interface")

    max_layer = None
    if config is not None:
        max_layer = config.layer_count_for_height(config.size_z_mm)
        for actual, expected in (
            (plan.pixel_size_x_mm, config.pixel_size_x_mm),
            (plan.pixel_size_y_mm, config.pixel_size_y_mm),
            (plan.layer_height_mm, config.layer_height_mm),
        ):
            if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-12):
                raise ValueError("cached plan geometry does not match printer config")

    for index, anchor in enumerate(plan.anchors):
        for name in (
            "x",
            "y",
            "top_layer",
            "base_layer",
            "base_tip_layers",
            "anchor_id",
            "parent_id",
            "tree_id",
            "load_count",
        ):
            if type(getattr(anchor, name)) is not int:
                raise ValueError("cached anchor contains a non-integer index")
        for name in ("base_x", "base_y", "joint_x", "joint_y", "joint_layer"):
            value = getattr(anchor, name)
            if value is not None and type(value) is not int:
                raise ValueError("cached anchor contains a non-integer optional index")
        if anchor.x < 0 or anchor.y < 0 or anchor.top_layer < 0 or anchor.base_layer < 0:
            raise ValueError("cached anchor contains a negative coordinate")
        if anchor.top_layer < anchor.base_layer:
            raise ValueError("cached anchor has inverted layer bounds")
        if (anchor.base_x is None) != (anchor.base_y is None):
            raise ValueError("cached anchor has an incomplete base coordinate")
        if (anchor.joint_x is None) != (anchor.joint_y is None) or (
            (anchor.joint_x is None) != (anchor.joint_layer is None)
        ):
            raise ValueError("cached anchor has an incomplete joint coordinate")
        if anchor.joint_layer is not None and not (
            anchor.base_layer <= anchor.joint_layer <= anchor.top_layer
        ):
            raise ValueError("cached anchor has an invalid joint layer")
        if config is not None:
            for x, y in (
                (anchor.x, anchor.y),
                (anchor.base_x, anchor.base_y),
                (anchor.joint_x, anchor.joint_y),
            ):
                if x is not None and y is not None and not (
                    0 <= x < config.resolution_x and 0 <= y < config.resolution_y
                ):
                    raise ValueError("cached anchor lies outside printer raster")
        if max_layer is not None and anchor.top_layer >= max_layer:
            raise ValueError("cached anchor lies above printer height")
        if anchor.parent_id >= index or anchor.parent_id < -1:
            raise ValueError("cached support graph is not acyclic")
        if anchor.anchor_id not in {-1, index}:
            raise ValueError("cached support graph has inconsistent anchor IDs")
        if anchor.tree_id < -1 or anchor.tree_id >= len(plan.anchors):
            raise ValueError("cached support graph has invalid tree ID")
        if (
            anchor.load_count < 1
            or anchor.kind not in {"bed", "branch", "enforcer"}
            or anchor.role
            not in {"primary", "secondary", "manual", "painted", "manual-trunk"}
            or anchor.tip_type not in {"cone", "sphere", "cylinder"}
            or type(anchor.tree_clearance_ok) is not bool
        ):
            raise ValueError("cached support metadata is invalid")
        for value in (anchor.normal_x, anchor.normal_y, anchor.normal_z):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError("cached anchor normal is invalid")

    for brace in plan.braces:
        for value in asdict(brace).values():
            if type(value) is not int:
                raise ValueError("cached brace contains a non-integer value")
        if min(brace.x0, brace.y0, brace.x1, brace.y1, brace.start_layer, brace.end_layer, brace.radius_px) < 0:
            raise ValueError("cached brace contains a negative value")
        if brace.start_layer > brace.end_layer:
            raise ValueError("cached brace has inverted layer bounds")
        if config is not None and not (
            brace.x0 < config.resolution_x
            and brace.x1 < config.resolution_x
            and brace.y0 < config.resolution_y
            and brace.y1 < config.resolution_y
        ):
            raise ValueError("cached brace lies outside printer raster")
        if max_layer is not None and brace.end_layer >= max_layer:
            raise ValueError("cached brace lies above printer height")

    if plan.manual_requested != plan.manual_routed + len(plan.manual_unrouted_points):
        raise ValueError("cached manual receipt is unbalanced")
    if plan.manual_routed != len(plan.manual_routed_points) or plan.manual_routed != len(plan.manual_actual_contact_points):
        raise ValueError("cached manual routed receipt is inconsistent")
    if plan.auto_components_detected != (
        plan.auto_components_supported
        + plan.auto_components_excluded
        + plan.auto_components_unrouted
    ):
        raise ValueError("cached automatic component receipt is unbalanced")
    if plan.auto_candidates_attempted != plan.auto_candidates_routed + plan.auto_candidates_unrouted:
        raise ValueError("cached automatic candidate receipt is unbalanced")
    if plan.tree_root_count != sum(anchor.parent_id < 0 for anchor in plan.anchors):
        raise ValueError("cached tree root count is inconsistent")
    if plan.tree_branch_count != sum(anchor.kind == "branch" for anchor in plan.anchors):
        raise ValueError("cached tree branch count is inconsistent")
    if plan.max_tree_load != max((anchor.load_count for anchor in plan.anchors), default=0):
        raise ValueError("cached tree load is inconsistent")


def _points(value: Any, label: str) -> tuple[tuple[float, float, float], ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} is not a list")
    points: list[tuple[float, float, float]] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            raise ValueError(f"{label} contains an invalid point")
        point = tuple(float(component) for component in item)
        if not all(math.isfinite(component) for component in point):
            raise ValueError(f"{label} contains a non-finite point")
        points.append(point)  # type: ignore[arg-type]
    return tuple(points)


def _encode_mask(value: bytes | None) -> dict[str, Any] | None:
    if value is None:
        return None
    compressed = zlib.compress(value, level=6)
    return {
        "encoding": "zlib-base64",
        "size": len(value),
        "sha256": hashlib.sha256(value).hexdigest(),
        "data": base64.b64encode(compressed).decode("ascii"),
    }


def _decode_mask(value: Any, expected_size: int | None) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "encoding",
        "size",
        "sha256",
        "data",
    }:
        raise ValueError("invalid cached raster mask")
    if value.get("encoding") != "zlib-base64" or type(value.get("size")) is not int:
        raise ValueError("unsupported cached raster encoding")
    size = value["size"]
    if size < 0 or size > 100_000_000 or (expected_size is not None and size != expected_size):
        raise ValueError("cached raster mask size is invalid")
    try:
        compressed = base64.b64decode(value["data"], validate=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("cached raster mask is not valid base64") from exc
    decompressor = zlib.decompressobj()
    try:
        decoded = decompressor.decompress(compressed, size + 1)
        if decompressor.unconsumed_tail or len(decoded) > size:
            raise ValueError("cached raster mask exceeds its declared size")
        decoded += decompressor.flush(max(1, size + 1 - len(decoded)))
    except zlib.error as exc:
        raise ValueError("cached raster mask is not valid zlib data") from exc
    if (
        not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
        or len(decoded) != size
    ):
        raise ValueError("cached raster mask decompressed to an invalid size")
    if hashlib.sha256(decoded).hexdigest() != value.get("sha256"):
        raise ValueError("cached raster mask checksum mismatch")
    return decoded


def _identity_key(identity: dict[str, Any]) -> str:
    return _json_sha256(identity)


def _json_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=1)
def _support_implementation_sha256() -> str:
    """Fingerprint code that can change planned support geometry.

    The explicit algorithm version remains useful for release contracts, while
    this fingerprint prevents a developer/editable install from silently
    reusing a plan after code changes without a version bump.
    """

    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in (
        "config.py",
        "mesh.py",
        "raster.py",
        "slicing.py",
        "support_cache.py",
        "supports.py",
        "transform.py",
    ):
        path = root / name
        encoded_name = name.encode("utf-8")
        digest.update(len(encoded_name).to_bytes(2, "big"))
        digest.update(encoded_name)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _prune(directory: Path, *, keep: Path) -> None:
    entries: list[tuple[float, Path]] = []
    cutoff = time.time() - 30 * 24 * 60 * 60
    for entry in directory.glob("*.json"):
        try:
            modified = entry.stat().st_mtime
            if entry != keep and modified < cutoff:
                entry.unlink()
            else:
                entries.append((modified, entry))
        except OSError:
            continue
    for _, entry in sorted(entries, reverse=True)[_MAX_CACHE_ENTRIES:]:
        if entry == keep:
            continue
        try:
            entry.unlink()
        except OSError:
            pass
