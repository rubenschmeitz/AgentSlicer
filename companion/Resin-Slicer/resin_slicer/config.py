from __future__ import annotations

from dataclasses import dataclass, replace
from math import ceil, isfinite
from numbers import Real

from .errors import ConfigError


@dataclass(frozen=True)
class PrintConfig:
    """Printer and resin settings used by the slicer and file writers."""

    resolution_x: int = 1920
    resolution_y: int = 1080
    size_x_mm: float = 120.0
    size_y_mm: float = 67.5
    size_z_mm: float = 160.0
    layer_height_mm: float = 0.05
    exposure_time_s: float = 2.5
    bottom_exposure_time_s: float = 35.0
    bottom_layers: int = 6
    transition_layers: int = 6
    lift_distance_mm: float = 5.0
    lift_speed_mm_min: float = 65.0
    retract_distance_mm: float = 5.0
    retract_speed_mm_min: float = 150.0
    wait_after_retract_s: float = 0.2
    # Rest times (apply on top of the lift/retract motion). 0 = none.
    rest_before_lift_s: float = 0.0
    rest_after_lift_s: float = 0.0
    # Optional second lift/retract segment (two-stage / TSMC). 0 = single stage.
    lift_distance2_mm: float = 0.0
    lift_speed2_mm_min: float = 0.0
    retract_distance2_mm: float = 0.0
    retract_speed2_mm_min: float = 0.0
    # Bottom-layer lift/retract overrides. 0 = reuse the normal value.
    bottom_lift_distance_mm: float = 0.0
    bottom_lift_speed_mm_min: float = 0.0
    bottom_retract_distance_mm: float = 0.0
    bottom_retract_speed_mm_min: float = 0.0
    light_pwm: int = 255
    bottom_light_pwm: int = 255
    machine_name: str = "Generic MSLA"
    resin_name: str = "Standard"
    resin_density_g_ml: float = 1.1
    center_model: bool = True
    max_pixels_per_layer: int = 100_000_000

    @property
    def pixel_size_x_mm(self) -> float:
        return self.size_x_mm / self.resolution_x

    @property
    def pixel_size_y_mm(self) -> float:
        return self.size_y_mm / self.resolution_y

    @property
    def pixel_area_mm2(self) -> float:
        return self.pixel_size_x_mm * self.pixel_size_y_mm

    def layer_count_for_height(self, height_mm: float) -> int:
        return max(1, int(ceil(height_mm / self.layer_height_mm)))

    def lift_distance_for(self, *, bottom: bool) -> float:
        if bottom and self.bottom_lift_distance_mm > 0:
            return self.bottom_lift_distance_mm
        return self.lift_distance_mm

    def lift_speed_for(self, *, bottom: bool) -> float:
        if bottom and self.bottom_lift_speed_mm_min > 0:
            return self.bottom_lift_speed_mm_min
        return self.lift_speed_mm_min

    def retract_distance_for(self, *, bottom: bool) -> float:
        if bottom and self.bottom_retract_distance_mm > 0:
            return self.bottom_retract_distance_mm
        return self.retract_distance_mm

    def retract_speed_for(self, *, bottom: bool) -> float:
        if bottom and self.bottom_retract_speed_mm_min > 0:
            return self.bottom_retract_speed_mm_min
        return self.retract_speed_mm_min

    def validate(self) -> None:
        ints = {
            "resolution_x": self.resolution_x,
            "resolution_y": self.resolution_y,
            "bottom_layers": self.bottom_layers,
            "transition_layers": self.transition_layers,
            "light_pwm": self.light_pwm,
            "bottom_light_pwm": self.bottom_light_pwm,
            "max_pixels_per_layer": self.max_pixels_per_layer,
        }
        for name, value in ints.items():
            if type(value) is not int:
                raise ConfigError(f"{name} must be an integer")

        positive = {
            "resolution_x": self.resolution_x,
            "resolution_y": self.resolution_y,
            "size_x_mm": self.size_x_mm,
            "size_y_mm": self.size_y_mm,
            "size_z_mm": self.size_z_mm,
            "layer_height_mm": self.layer_height_mm,
            "exposure_time_s": self.exposure_time_s,
            "bottom_exposure_time_s": self.bottom_exposure_time_s,
            "max_pixels_per_layer": self.max_pixels_per_layer,
        }
        for name, value in positive.items():
            if not _is_finite_number(value) or value <= 0:
                raise ConfigError(f"{name} must be positive")

        nonnegative = {
            "bottom_layers": self.bottom_layers,
            "transition_layers": self.transition_layers,
            "lift_distance_mm": self.lift_distance_mm,
            "retract_distance_mm": self.retract_distance_mm,
            "wait_after_retract_s": self.wait_after_retract_s,
            "rest_before_lift_s": self.rest_before_lift_s,
            "rest_after_lift_s": self.rest_after_lift_s,
            "lift_distance2_mm": self.lift_distance2_mm,
            "lift_speed2_mm_min": self.lift_speed2_mm_min,
            "retract_distance2_mm": self.retract_distance2_mm,
            "retract_speed2_mm_min": self.retract_speed2_mm_min,
            "bottom_lift_distance_mm": self.bottom_lift_distance_mm,
            "bottom_lift_speed_mm_min": self.bottom_lift_speed_mm_min,
            "bottom_retract_distance_mm": self.bottom_retract_distance_mm,
            "bottom_retract_speed_mm_min": self.bottom_retract_speed_mm_min,
        }
        for name, value in nonnegative.items():
            if not _is_finite_number(value) or value < 0:
                raise ConfigError(f"{name} cannot be negative")

        for name, value in {
            "lift_speed_mm_min": self.lift_speed_mm_min,
            "retract_speed_mm_min": self.retract_speed_mm_min,
            "resin_density_g_ml": self.resin_density_g_ml,
        }.items():
            if not _is_finite_number(value) or value <= 0:
                raise ConfigError(f"{name} must be positive")

        if self.lift_distance2_mm > 0 and self.lift_speed2_mm_min <= 0:
            raise ConfigError(
                "lift_speed2_mm_min must be positive when lift_distance2_mm is used"
            )
        if self.retract_distance2_mm > 0 and self.retract_speed2_mm_min <= 0:
            raise ConfigError(
                "retract_speed2_mm_min must be positive when retract_distance2_mm is used"
            )

        if not 0 <= self.light_pwm <= 255 or not 0 <= self.bottom_light_pwm <= 255:
            raise ConfigError("light PWM values must be in the 0..255 range")

        pixels = self.resolution_x * self.resolution_y
        if pixels > self.max_pixels_per_layer:
            raise ConfigError(
                f"layer resolution is {pixels:,} pixels, above the configured "
                f"safety limit of {self.max_pixels_per_layer:,}; raise "
                "max_pixels_per_layer only after confirming available memory"
            )

    def with_overrides(self, **kwargs: object) -> "PrintConfig":
        updated = replace(self, **kwargs)
        updated.validate()
        return updated


@dataclass(frozen=True)
class SupportConfig:
    enabled: bool = True
    model_lift_mm: float = 5.0
    min_island_area_mm2: float = 0.08
    overhang_angle_deg: float = 45.0
    overhang_margin_mm: float = 0.0
    support_spacing_mm: float = 3.0
    primary_supports_enabled: bool = False
    primary_density_multiplier: float = 2.0
    primary_area_radius_mm: float = 4.0
    primary_max_extra_per_island: int = 8
    post_radius_mm: float = 0.28
    tip_radius_mm: float = 0.18
    tip_type: str = "cone"
    spherical_contact_enabled: bool = False
    spherical_contact_diameter_mm: float = 0.6
    spherical_contact_inset_mm: float = 0.05
    tip_length_mm: float = 0.8
    # Shallow vertical overlap at the small contact radius, not a larger bulb.
    tip_contact_depth_mm: float = 0.1
    foot_radius_mm: float = 0.8
    bed_interface: str = "raft"
    raft_margin_mm: float = 0.6
    raft_chamfer_width_mm: float = 0.4
    raft_chamfer_angle_deg: float = 45.0
    bed_interface_thickness_mm: float = 0.35
    raft_layers: int = 4
    brace_enabled: bool = True
    brace_radius_mm: float = 0.18
    brace_height_mm: float = 3.0
    brace_max_distance_mm: float = 8.0
    # Vertical distance between repeated brace levels on tall supports.
    # 0 keeps the single brace level at brace_height_mm.
    brace_interval_mm: float = 0.0
    collision_clearance_mm: float = 0.08
    max_base_reach_mm: float = 45.0
    max_support_angle_deg: float = 35.0
    # Build resin-style support trees by preferentially joining nearby contacts
    # to already safe shafts.  The graph only points toward previously accepted
    # anchors, so it stays acyclic and every branch terminates at the bed/model.
    branching_enabled: bool = True
    tree_merge_distance_mm: float = 4.0
    tree_max_tips: int = 12
    # Load-bearing trunks grow toward this radius as branches accumulate.  A
    # single isolated support retains post_radius_mm; this is the capped radius
    # for a shared pillar, not a blanket diameter override.
    tree_trunk_radius_mm: float = 0.65
    pedestal_height_mm: float = 2.0
    enforcers_enabled: bool = False
    enforcer_reach_mm: float = 10.0
    enforcer_min_drop_mm: float = 1.0
    max_supports_per_island: int = 48
    analysis_max_pixels: int = 250_000

    def validate(self) -> None:
        integer_fields = {
            "primary_max_extra_per_island": self.primary_max_extra_per_island,
            "raft_layers": self.raft_layers,
            "max_supports_per_island": self.max_supports_per_island,
            "analysis_max_pixels": self.analysis_max_pixels,
            "tree_max_tips": self.tree_max_tips,
        }
        for name, value in integer_fields.items():
            if type(value) is not int:
                raise ConfigError(f"{name} must be an integer")

        finite_fields = {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if name not in {"enabled", "primary_supports_enabled", "spherical_contact_enabled", "brace_enabled", "branching_enabled", "enforcers_enabled", "tip_type", "bed_interface"}
        }
        for name, value in finite_fields.items():
            if not _is_finite_number(value):
                raise ConfigError(f"{name} must be finite")

        if self.model_lift_mm < 0:
            raise ConfigError("model_lift_mm cannot be negative")
        if self.min_island_area_mm2 < 0:
            raise ConfigError("min island area cannot be negative")
        if not 0 < self.overhang_angle_deg < 90:
            raise ConfigError("overhang_angle_deg must be between 0 and 90 degrees")
        if self.overhang_margin_mm < 0:
            raise ConfigError("overhang_margin_mm cannot be negative")
        if self.primary_density_multiplier < 1.0:
            raise ConfigError("primary_density_multiplier must be at least 1")
        if self.primary_area_radius_mm <= 0:
            raise ConfigError("primary_area_radius_mm must be positive")
        if self.primary_max_extra_per_island < 0:
            raise ConfigError("primary_max_extra_per_island cannot be negative")
        if self.tip_type not in {"cone", "sphere", "cylinder"}:
            raise ConfigError("tip_type must be one of: cone, sphere, cylinder")
        if self.spherical_contact_diameter_mm <= 0:
            raise ConfigError("spherical_contact_diameter_mm must be positive")
        if self.spherical_contact_inset_mm < 0:
            raise ConfigError("spherical_contact_inset_mm cannot be negative")
        if not 0 <= self.tip_contact_depth_mm <= 0.5:
            raise ConfigError("tip_contact_depth_mm must be between 0 and 0.5 mm")
        if self.bed_interface not in {"none", "feet", "raft", "skate"}:
            raise ConfigError("bed_interface must be one of: none, feet, raft, skate")
        if not 0 <= self.max_support_angle_deg < 80:
            raise ConfigError("max_support_angle_deg must be between 0 and 80 degrees")
        for name in (
            "support_spacing_mm",
            "post_radius_mm",
            "tip_radius_mm",
            "tip_length_mm",
            "foot_radius_mm",
            "brace_radius_mm",
            "brace_height_mm",
            "brace_max_distance_mm",
            "max_base_reach_mm",
            "tree_merge_distance_mm",
            "tree_trunk_radius_mm",
            "pedestal_height_mm",
            "enforcer_reach_mm",
            "enforcer_min_drop_mm",
        ):
            if getattr(self, name) <= 0:
                raise ConfigError(f"{name} must be positive")
        if self.brace_interval_mm < 0:
            raise ConfigError("brace_interval_mm cannot be negative")
        if self.raft_margin_mm < 0:
            raise ConfigError("raft_margin_mm cannot be negative")
        if self.raft_chamfer_width_mm < 0:
            raise ConfigError("raft_chamfer_width_mm cannot be negative")
        if not 0 < self.raft_chamfer_angle_deg < 90:
            raise ConfigError("raft_chamfer_angle_deg must be between 0 and 90 degrees")
        if self.bed_interface_thickness_mm <= 0:
            raise ConfigError("bed_interface_thickness_mm must be positive")
        if self.collision_clearance_mm < 0:
            raise ConfigError("collision_clearance_mm cannot be negative")
        if self.raft_layers < 0:
            raise ConfigError("raft_layers cannot be negative")
        if self.max_supports_per_island <= 0:
            raise ConfigError("max_supports_per_island must be positive")
        if self.analysis_max_pixels <= 0:
            raise ConfigError("analysis_max_pixels must be positive")
        if self.tree_max_tips <= 0:
            raise ConfigError("tree_max_tips must be positive")
        if self.tree_trunk_radius_mm < self.post_radius_mm:
            raise ConfigError("tree_trunk_radius_mm cannot be smaller than post_radius_mm")


def _is_finite_number(value: object) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool) and isfinite(float(value))


def _mars_5_ultra_profile(
    *,
    exposure_time_s: float,
    resin_name: str,
    resin_density_g_ml: float,
) -> PrintConfig:
    """Build a Mars 5 Ultra candidate profile from the local start settings.

    The Mars 5 Ultra uses tilt-release semantics. The native output format
    still contains motion fields, so zero first-stage distances are emitted
    until a printer-specific non-physical format test establishes the correct
    values. This is deliberately a candidate profile, not firmware proof.
    """

    return PrintConfig(
        resolution_x=8520,
        resolution_y=4320,
        size_x_mm=153.36,
        size_y_mm=77.76,
        size_z_mm=165.0,
        layer_height_mm=0.05,
        exposure_time_s=exposure_time_s,
        bottom_exposure_time_s=30.0,
        bottom_layers=4,
        transition_layers=5,
        lift_distance_mm=0.0,
        lift_speed_mm_min=65.0,
        retract_distance_mm=0.0,
        retract_speed_mm_min=150.0,
        wait_after_retract_s=0.5,
        machine_name="ELEGOO Mars 5 Ultra",
        resin_name=resin_name,
        resin_density_g_ml=resin_density_g_ml,
    )


PROFILES: dict[str, PrintConfig] = {
    "generic-2k": PrintConfig(),
    "elegoo-mars-5-ultra": _mars_5_ultra_profile(
        exposure_time_s=2.5,
        resin_name="Standard (Mars 5 Ultra)",
        resin_density_g_ml=1.1,
    ),
    "elegoo-mars-5-ultra-photocentric-uv-lcd-hard-black": _mars_5_ultra_profile(
        exposure_time_s=3.0,
        resin_name="Photocentric UV LCD Hard Black",
        resin_density_g_ml=1.19,
    ),
    "elegoo-mars-5-ultra-ifun-if3121-toughness-grey": _mars_5_ultra_profile(
        exposure_time_s=2.5,
        resin_name="iFun iF3121 Toughness Grey",
        resin_density_g_ml=1.16,
    ),
    "elegoo-mars-5-ultra-anycubic-water-washable-2-grey": _mars_5_ultra_profile(
        exposure_time_s=2.5,
        resin_name="ANYCUBIC Water-Washable Resin 2.0 Grey",
        resin_density_g_ml=1.08,
    ),
    "elegoo-mars-5-ultra-anycubic-abs-like-plus": _mars_5_ultra_profile(
        exposure_time_s=2.5,
        resin_name="ANYCUBIC ABS-Like Plus",
        resin_density_g_ml=1.1,
    ),
    "elegoo-mars-5-ultra-anycubic-uv-sensitive-standard": _mars_5_ultra_profile(
        exposure_time_s=2.5,
        resin_name="ANYCUBIC UV Sensitive Standard",
        resin_density_g_ml=1.1,
    ),
    "elegoo-mars-5-ultra-sunlu-standard": _mars_5_ultra_profile(
        exposure_time_s=2.5,
        resin_name="SUNLU Standard",
        resin_density_g_ml=1.1,
    ),
    "elegoo-mars-5-ultra-sunlu-abs-like": _mars_5_ultra_profile(
        exposure_time_s=2.5,
        resin_name="SUNLU ABS-Like",
        resin_density_g_ml=1.1,
    ),
    "elegoo-mars-5-ultra-sunlu-high-tough-toughness": _mars_5_ultra_profile(
        exposure_time_s=2.8,
        resin_name="SUNLU High-Tough / Toughness",
        resin_density_g_ml=1.1,
    ),
    "elegoo-mars-3-pro": PrintConfig(
        resolution_x=4098,
        resolution_y=2560,
        size_x_mm=143.43,
        size_y_mm=89.6,
        size_z_mm=175.0,
        layer_height_mm=0.05,
        lift_distance_mm=5.0,
        machine_name="ELEGOO Mars 3 Pro",
    ),
    "elegoo-mars-3-pro-anycubic-water-washable-2-grey": PrintConfig(
        resolution_x=4098,
        resolution_y=2560,
        size_x_mm=143.43,
        size_y_mm=89.6,
        size_z_mm=175.0,
        layer_height_mm=0.05,
        exposure_time_s=2.5,
        bottom_exposure_time_s=30.0,
        bottom_layers=5,
        transition_layers=5,
        lift_distance_mm=5.0,
        lift_speed_mm_min=90.0,
        retract_speed_mm_min=150.0,
        wait_after_retract_s=0.8,
        rest_before_lift_s=0.5,
        bottom_lift_speed_mm_min=70.0,
        machine_name="ELEGOO Mars 3 Pro",
        resin_name="ANYCUBIC Water-Washable Resin 2.0 Grey",
    ),
    "elegoo-mars-3-pro-photocentric-uv-lcd-hard-black": PrintConfig(
        resolution_x=4098,
        resolution_y=2560,
        size_x_mm=143.43,
        size_y_mm=89.6,
        size_z_mm=175.0,
        layer_height_mm=0.05,
        exposure_time_s=4.3,
        bottom_exposure_time_s=38.0,
        bottom_layers=5,
        transition_layers=5,
        lift_distance_mm=5.0,
        lift_speed_mm_min=90.0,
        retract_speed_mm_min=150.0,
        wait_after_retract_s=0.8,
        rest_before_lift_s=0.5,
        bottom_lift_speed_mm_min=70.0,
        machine_name="ELEGOO Mars 3 Pro",
        resin_name="Photocentric UV LCD Hard Black",
    ),
    "elegoo-mars-3-pro-ifun-if3121-toughness-grey": PrintConfig(
        resolution_x=4098,
        resolution_y=2560,
        size_x_mm=143.43,
        size_y_mm=89.6,
        size_z_mm=175.0,
        layer_height_mm=0.05,
        exposure_time_s=2.5,
        bottom_exposure_time_s=32.0,
        bottom_layers=5,
        transition_layers=5,
        lift_distance_mm=5.0,
        lift_speed_mm_min=100.0,
        retract_speed_mm_min=150.0,
        wait_after_retract_s=0.8,
        rest_before_lift_s=0.5,
        bottom_lift_speed_mm_min=70.0,
        machine_name="ELEGOO Mars 3 Pro",
        resin_name="iFun iF3121 LCD/DLP Toughness Grey",
    ),
    "elegoo-saturn-3-ultra": PrintConfig(
        resolution_x=11520,
        resolution_y=5120,
        size_x_mm=218.88,
        size_y_mm=122.88,
        size_z_mm=260.0,
        machine_name="ELEGOO Saturn 3 Ultra",
        max_pixels_per_layer=70_000_000,
    ),
    "elegoo-jupiter-2-16k": PrintConfig(
        resolution_x=15120,
        resolution_y=6230,
        size_x_mm=302.40,
        size_y_mm=161.98,
        size_z_mm=300.0,
        machine_name="ELEGOO Jupiter 2 16K",
        max_pixels_per_layer=100_000_000,
    ),
    "small-test": PrintConfig(
        resolution_x=160,
        resolution_y=90,
        size_x_mm=80.0,
        size_y_mm=45.0,
        size_z_mm=80.0,
        max_pixels_per_layer=20_000,
    ),
}


def profile(name: str) -> PrintConfig:
    try:
        cfg = PROFILES[name]
    except KeyError as exc:
        available = ", ".join(sorted(PROFILES))
        raise ConfigError(f"unknown profile {name!r}; available profiles: {available}") from exc
    cfg.validate()
    return cfg
