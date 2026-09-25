from __future__ import annotations

import unittest

from agent_slicer.blender_adapter import (
    _adaptive_hollow_pitches,
    _is_refinable_hollow_error,
)


class BlenderAdapterTests(unittest.TestCase):
    def test_hollow_refinement_is_bounded_and_deterministic(self) -> None:
        self.assertEqual(_adaptive_hollow_pitches(0.15), [0.15, 0.12, 0.1])
        self.assertEqual(_adaptive_hollow_pitches(0.12), [0.12, 0.1])
        self.assertEqual(_adaptive_hollow_pitches(0.10), [0.1])

    def test_only_surface_deviation_failures_trigger_refinement(self) -> None:
        self.assertTrue(
            _is_refinable_hollow_error(
                RuntimeError("hollow reconstruction maximum outer change exceeds limit")
            )
        )
        self.assertTrue(
            _is_refinable_hollow_error(
                RuntimeError("voxel reconstruction changed outer bounds")
            )
        )
        self.assertFalse(
            _is_refinable_hollow_error(RuntimeError("retained cavity voxels are sealed"))
        )


if __name__ == "__main__":
    unittest.main()
