from __future__ import annotations

import unittest
from unittest.mock import patch

from agent_slicer.generation_providers import (
    MeshyProvider,
    ProviderError,
    TripoProvider,
    provider_configuration,
)


class GenerationProviderTests(unittest.TestCase):
    def test_provider_configuration_exposes_no_credentials(self) -> None:
        with patch.dict("os.environ", {"MESHY_API_KEY": "secret"}, clear=True):
            value = provider_configuration()
        self.assertTrue(value["meshy"]["configured"])
        self.assertFalse(value["tripo"]["configured"])
        self.assertNotIn("secret", repr(value))

    def test_missing_credentials_fail_without_network_call(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ProviderError, "MESHY_API_KEY"):
                MeshyProvider()
            with self.assertRaisesRegex(ProviderError, "TRIPO_API_KEY"):
                TripoProvider()

    @patch("agent_slicer.generation_providers._json_request")
    def test_meshy_text_submission_and_status_are_normalised(self, request) -> None:
        request.side_effect = [
            {"result": "task-1"},
            {
                "status": "SUCCEEDED",
                "progress": 100,
                "consumed_credits": 20,
                "model_urls": {"glb": "https://example.test/model.glb"},
            },
        ]
        backend = MeshyProvider("secret")
        submitted = backend.submit_model("a printable owl")
        completed = backend.status(submitted.operation, submitted.task_id)

        self.assertEqual(submitted.operation, "text-to-3d")
        self.assertEqual(completed.status, "succeeded")
        self.assertEqual(completed.consumed_credits, 20.0)
        self.assertEqual(completed.output_urls["model.glb"], "https://example.test/model.glb")
        self.assertNotIn("secret", repr(submitted))

    @patch("agent_slicer.generation_providers._json_request")
    def test_tripo_fractional_credit_receipt_is_preserved(self, request) -> None:
        request.return_value = {
            "data": {
                "task_id": "task-2",
                "status": "success",
                "progress": 100,
                "credits_consumed": 48.5,
                "output": {"model_url": "https://example.test/model.glb"},
            }
        }
        completed = TripoProvider("secret").status("text-to-model", "task-2")
        self.assertEqual(completed.status, "succeeded")
        self.assertEqual(completed.consumed_credits, 48.5)

    @patch("agent_slicer.generation_providers._json_request")
    def test_tripo_generated_image_output_is_captured(self, request) -> None:
        request.return_value = {
            "data": {
                "task_id": "task-image",
                "status": "success",
                "progress": 100,
                "output": {"generated_image": "https://example.test/reference.png"},
            }
        }
        completed = TripoProvider("secret").status("text-to-image", "task-image")
        self.assertEqual(
            completed.output_urls,
            {"image-1.png": "https://example.test/reference.png"},
        )

    @patch("agent_slicer.generation_providers._json_request")
    def test_default_model_is_recorded_in_submission_receipt(self, request) -> None:
        request.return_value = {"result": "task-3"}
        submitted = MeshyProvider("secret").submit_model("a printable owl")
        self.assertEqual(submitted.receipt()["model"], "meshy-7")


if __name__ == "__main__":
    unittest.main()
