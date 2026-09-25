import unittest

from agent_slicer.models import SourceModel


class ModelContractTests(unittest.TestCase):
    def test_rejects_lossy_quantity_coercion(self):
        for value in (True, False, 1.9, "2", 0, -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SourceModel.from_dict({"id": "a", "source": "a.stl", "sha256": "a" * 64, "quantity": value})

    def test_rejects_nonfinite_or_ambiguous_height(self):
        for value in (True, float("nan"), float("inf"), "10", 0, -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SourceModel.from_dict({"id": "a", "source": "a.stl", "sha256": "a" * 64, "target_height_mm": value})
