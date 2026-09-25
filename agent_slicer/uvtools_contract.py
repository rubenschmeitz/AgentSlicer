"""Small, dependency-free contract shared by validation and printer delivery."""

DETECTION_CONTRACT = "uvtools-coupled-suction-and-boundary-v1"
SCAN_FLAGS = {
    "islands": ["--islands"],
    "overhangs": ["--overhangs"],
    "resin_traps": ["--resin-traps"],
    # UVtools 4.4.3 enables the trap detector separately from its cup option.
    "suction_cups": ["--resin-traps", "--suction-cups"],
    "touching_bounds": ["--touching-bounds"],
    "print_height": ["--print-height"],
    "empty_layers": ["--empty-layers"],
}


def has_complete_detection_contract(analysis: dict) -> bool:
    return (analysis.get("detection_contract") == DETECTION_CONTRACT
            and analysis.get("scan_flags") == SCAN_FLAGS)
