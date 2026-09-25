from __future__ import annotations

from math import floor

from ..config import PrintConfig

FILE_STARTUP_OVERHEAD_SECONDS = 300


def layer_exposure_seconds(config: PrintConfig, layer_index: int) -> float:
    """Return the serialized exposure, including software transition layers."""

    if layer_index < config.bottom_layers:
        return config.bottom_exposure_time_s

    transition_index = layer_index - config.bottom_layers
    if transition_index >= config.transition_layers:
        return config.exposure_time_s

    exposure_delta = config.bottom_exposure_time_s - config.exposure_time_s
    if exposure_delta <= 0 or config.transition_layers <= 0:
        return config.exposure_time_s

    # UVtools rounds the decrement to two decimals, midpoint away from zero,
    # before applying each software transition step.
    raw_decrement = exposure_delta / (config.transition_layers + 1)
    decrement = floor(raw_decrement * 100.0 + 0.5) / 100.0
    exposure = config.bottom_exposure_time_s - decrement * (transition_index + 1)
    return min(config.bottom_exposure_time_s, max(config.exposure_time_s, exposure))


def estimate_print_time_seconds(config: PrintConfig, layer_count: int) -> int:
    """Estimate complete startup, exposure, motion, and configured wait time.

    CTB and GOO both store whole seconds in their global metadata. Keeping the
    calculation here prevents their headers from drifting apart when motion
    controls are extended.
    """

    def motion_seconds(distance_mm: float, speed_mm_min: float) -> float:
        if distance_mm <= 0:
            return 0.0
        return distance_mm / max(1e-9, speed_mm_min) * 60.0

    def cycle_seconds(layer_index: int) -> float:
        bottom = layer_index < config.bottom_layers
        lift = motion_seconds(
            config.lift_distance_for(bottom=bottom),
            config.lift_speed_for(bottom=bottom),
        ) + motion_seconds(config.lift_distance2_mm, config.lift_speed2_mm_min)
        retract = motion_seconds(
            config.retract_distance_for(bottom=bottom),
            config.retract_speed_for(bottom=bottom),
        ) + motion_seconds(config.retract_distance2_mm, config.retract_speed2_mm_min)
        return (
            layer_exposure_seconds(config, layer_index)
            + lift
            + retract
            + config.rest_before_lift_s
            + config.rest_after_lift_s
            + config.wait_after_retract_s
        )

    # UVtools' canonical estimate includes five minutes for preparation/startup
    # in addition to per-layer exposure, motion, and waits. Store the same
    # whole-second estimate so decoded and header metadata agree.
    return int(
        FILE_STARTUP_OVERHEAD_SECONDS
        + sum(cycle_seconds(layer_index) for layer_index in range(layer_count))
    )
