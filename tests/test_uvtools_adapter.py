from __future__ import annotations

import unittest
import inspect
import os
import tempfile
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import numpy as np

from agent_slicer.presets import resolve_presets
from agent_slicer.uvtools_contract import DETECTION_CONTRACT, SCAN_FLAGS
from agent_slicer.uvtools_adapter import (
    DEFAULT_ANALYSIS_TIMEOUT_SECONDS,
    _analysis_workers,
    _attach_record_contact,
    _index_extracted_layers,
    _runtime_kind,
    _uvtools_layer_range,
    _uvtools_environment,
    analyze_sliced_file,
    evaluate_production_analysis,
    parse_issue_record,
    parse_properties,
)


class UvtoolsAdapterTests(unittest.TestCase):
    def test_extract_layer_range_uses_uvtools_colon_syntax(self) -> None:
        self.assertEqual(_uvtools_layer_range(161, 420), "161:420")
        with self.assertRaises(ValueError):
            _uvtools_layer_range(10, 9)

    def test_extracted_layer_index_accepts_uvtools_variable_padding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unpadded = root / "layer162.png"
            padded = root / "layer0163.png"
            unpadded.touch()
            padded.touch()
            indexed = _index_extracted_layers(root)
        self.assertEqual(indexed[162].name, "layer162.png")
        self.assertEqual(indexed[163].name, "layer0163.png")

    def test_uvtools_environment_uses_memory_conserving_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            environment = _uvtools_environment()
        self.assertEqual(environment["DOTNET_GCConserveMemory"], "5")

    def test_uvtools_environment_respects_dotnet_override(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DOTNET_GCConserveMemory": "7",
                "AGENT_SLICER_UVTOOLS_GC_CONSERVE": "3",
            },
            clear=True,
        ):
            environment = _uvtools_environment()
        self.assertEqual(environment["DOTNET_GCConserveMemory"], "7")

    def test_uvtools_defaults_to_one_memory_bounded_worker(self) -> None:
        with (
            patch("agent_slicer.uvtools_adapter._is_wsl", return_value=True),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertEqual(_analysis_workers(None), 1)

    def test_analysis_runs_issue_flags_sequentially_in_isolated_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "sample.ctb"
            executable = Path(temporary) / "UVtoolsCmd"
            source.write_bytes(b"ctb")
            executable.write_bytes(b"\x7fELF")
            calls: list[list[str]] = []

            def run(_executable, arguments, _timeout, **_kwargs):
                calls.append(arguments)
                if "print-properties" in arguments:
                    output = "ResolutionX: 10\nResolutionY: 10\n"
                elif "--version" in arguments:
                    output = "2.0.1\n"
                elif "--core-version" in arguments:
                    output = "4.4.3\n"
                elif "--touching-bounds" in arguments:
                    output = "TouchingBound, 12, 40px2, {X=0,Y=0,Width=4,Height=10}\n"
                else:
                    output = ""
                return CompletedProcess(arguments, 0, output, "")

            with (
                patch("agent_slicer.uvtools_adapter._run", side_effect=run),
                patch("agent_slicer.uvtools_adapter._analysis_work_root", return_value=Path(temporary)),
            ):
                result = analyze_sliced_file(
                    source,
                    uvtools_path=str(executable),
                    workers=4,
                )

        issue_calls = [call for call in calls if "print-issues" in call]
        self.assertEqual(len(issue_calls), 7)
        for flag, _prefix in (
            ("--islands", "Island,"),
            ("--overhangs", "Overhang,"),
            ("--resin-traps", "ResinTrap,"),
            ("--suction-cups", "SuctionCup,"),
            ("--touching-bounds", "TouchingBounds,"),
            ("--print-height", "PrintHeight,"),
            ("--empty-layers", "EmptyLayer,"),
        ):
            matching = [call for call in issue_calls if call[-1] == flag]
            self.assertEqual(len(matching), 1)
            expected_count = 2 if flag == "--suction-cups" else 1
            self.assertEqual(sum(item.startswith("--") for item in matching[0][4:]), expected_count)
            if flag == "--suction-cups":
                self.assertIn("--resin-traps", matching[0])
        self.assertEqual(result["parallel_workers"], 1)
        self.assertEqual(result["requested_workers"], 4)
        self.assertEqual(result["scan_strategy"], "sequential-isolated-processes")
        self.assertEqual(result["issues"]["touching_bounds"], 1)
        self.assertEqual(result["scan_flags"]["suction_cups"], ["--resin-traps", "--suction-cups"])

    def test_uvtools_worker_override_is_bounded(self) -> None:
        self.assertEqual(_analysis_workers(1), 1)
        with self.assertRaises(ValueError):
            _analysis_workers(0)

    def test_runtime_kind_recognizes_linux_elf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "UVtoolsCmd"
            executable.write_bytes(b"\x7fELFfixture")
            self.assertEqual(_runtime_kind(executable), "linux-elf")

    def test_cropped_layer_contact_evidence_matches_prior_pixels(self) -> None:
        record = {"kind": "Island", "measure_pixels": 9}
        current = np.ones((3, 3), dtype=bool)
        previous = np.zeros((3, 3), dtype=bool)
        previous[1, 1] = True
        _attach_record_contact(
            record,
            previous_crop=previous,
            current_crop=current,
            offset_x=20,
            offset_y=30,
        )
        self.assertEqual(record["prior_layer_contact"]["overlap_pixels"], 1)
        self.assertEqual(record["suggested_contact_pixels"], [[22, 32]])

    def test_large_ctb_analysis_timeout_has_headroom(self) -> None:
        default = inspect.signature(analyze_sliced_file).parameters["timeout"].default
        self.assertEqual(default, DEFAULT_ANALYSIS_TIMEOUT_SECONDS)
        self.assertGreaterEqual(default, 1200)

    def test_parses_issue_layer_measure_and_bounds(self) -> None:
        record = parse_issue_record(
            "ResinTrap, 14-122  (109), 765px3, {X=2055,Y=1049,Width=10,Height=6}"
        )
        self.assertEqual(record["first_layer"], 14)
        self.assertEqual(record["last_layer"], 122)
        self.assertEqual(record["measure_pixels"], 765)
        self.assertEqual(record["measure_kind"], "volume")
        self.assertEqual(record["bounds_pixels"]["width"], 10)

    def test_extracts_selected_properties_and_prefers_last_value(self) -> None:
        result = parse_properties(
            "Version: 4\nResolutionX: 4098\nVersion: 5\n"
            "ResolutionY: 2560\nMachineName: ELEGOO Mars 3 Pro\n"
        )
        self.assertEqual(result["Version"], "5")
        self.assertEqual(result["ResolutionX"], "4098")
        self.assertEqual(result["ResolutionY"], "2560")
        self.assertEqual(result["MachineName"], "ELEGOO Mars 3 Pro")

    def test_production_evaluation_rejects_resin_traps(self) -> None:
        resolved = resolve_presets(
            process="resin",
            machine="elegoo-mars-3-pro",
            material="anycubic-water-washable-2-grey",
            purpose="miniature-display-high-detail",
            quality="high-detail",
            output_format="ctb",
        )
        analysis = {
            "detection_contract": DETECTION_CONTRACT, "scan_flags": SCAN_FLAGS,
            "issues": {
                "islands": 0, "overhangs": 0, "resin_traps": 1,
                "suction_cups": 0, "touching_bounds": 0,
                "print_height": 0, "empty_layers": 0,
            },
            "properties": {
                "ResolutionX": "4098", "ResolutionY": "2560",
                "LayerHeight": "0.03", "MachineName": "ELEGOO Mars 3 Pro",
                "ExposureTime": "2.5", "BottomExposureTime": "30",
                "BottomLayerCount": "5", "PrintHeight": "80",
            },
        }
        result = evaluate_production_analysis(analysis, resolved)
        self.assertEqual(result["status"], "rejected")
        failed = {item["id"] for item in result["checks"] if not item["passed"]}
        self.assertIn("uvtools.issue.resin_traps", failed)

    def test_production_evaluation_passes_zero_issue_contract(self) -> None:
        resolved = resolve_presets(
            process="resin",
            machine="elegoo-mars-3-pro",
            material="anycubic-water-washable-2-grey",
            purpose="miniature-display-high-detail",
            quality="high-detail",
            output_format="ctb",
        )
        analysis = {
            "detection_contract": DETECTION_CONTRACT, "scan_flags": SCAN_FLAGS,
            "issues": {name: 0 for name in (
                "islands", "overhangs", "resin_traps", "suction_cups",
                "touching_bounds", "print_height", "empty_layers",
            )},
            "properties": {
                "ResolutionX": "4098", "ResolutionY": "2560",
                "LayerHeight": "0.03", "MachineName": "ELEGOO Mars 3 Pro",
                "ExposureTime": "2.5", "BottomExposureTime": "30",
                "BottomLayerCount": "5", "PrintHeight": "80",
            },
        }
        self.assertEqual(
            evaluate_production_analysis(analysis, resolved)["status"],
            "passed",
        )
        analysis.pop("detection_contract")
        self.assertEqual(evaluate_production_analysis(analysis, resolved)["status"], "rejected")

    def test_production_evaluation_ignores_only_configured_subthreshold_records(self) -> None:
        resolved = resolve_presets(
            process="resin",
            machine="elegoo-mars-3-pro",
            material="anycubic-water-washable-2-grey",
            purpose="miniature-display-high-detail",
            quality="high-detail",
            output_format="ctb",
        )
        properties = {
            "ResolutionX": "4098", "ResolutionY": "2560",
            "LayerHeight": "0.03", "MachineName": "ELEGOO Mars 3 Pro",
            "ExposureTime": "2.5", "BottomExposureTime": "30",
            "BottomLayerCount": "5", "PrintHeight": "80",
        }
        records = {
            name: [] for name in (
                "islands", "overhangs", "resin_traps", "suction_cups",
                "touching_bounds", "print_height", "empty_layers",
            )
        }
        records["islands"] = [
            {"measure_pixels": 5},
            {"measure_pixels": 100},
        ]
        records["resin_traps"] = [{"measure_pixels": 10_000}]
        analysis = {
            "detection_contract": DETECTION_CONTRACT, "scan_flags": SCAN_FLAGS,
            "issues": {name: len(value) for name, value in records.items()},
            "issue_records": records,
            "properties": properties,
        }
        result = evaluate_production_analysis(analysis, resolved)
        checks = {item["id"]: item for item in result["checks"]}
        self.assertEqual(checks["uvtools.issue.islands"]["actual"], 1)
        self.assertEqual(
            checks["uvtools.issue.islands"]["ignored_below_actionable_threshold"],
            1,
        )
        self.assertEqual(checks["uvtools.issue.resin_traps"]["actual"], 0)
        self.assertEqual(result["status"], "rejected")

    def test_production_evaluation_accepts_measured_prior_layer_support_contact(self) -> None:
        resolved = resolve_presets(
            process="resin",
            machine="elegoo-mars-3-pro",
            material="anycubic-water-washable-2-grey",
            purpose="miniature-display-high-detail",
            quality="high-detail",
            output_format="ctb",
        )
        names = (
            "islands", "overhangs", "resin_traps", "suction_cups",
            "touching_bounds", "print_height", "empty_layers",
        )
        records = {name: [] for name in names}
        records["islands"] = [
            {
                "measure_pixels": 10_000,
                "prior_layer_contact": {
                    "overlap_pixels": 100,
                    "contact_components": 2,
                },
            }
        ]
        analysis = {
            "detection_contract": DETECTION_CONTRACT, "scan_flags": SCAN_FLAGS,
            "issues": {name: len(records[name]) for name in names},
            "issue_records": records,
            "properties": {
                "ResolutionX": "4098", "ResolutionY": "2560",
                "LayerHeight": "0.03", "MachineName": "ELEGOO Mars 3 Pro",
                "ExposureTime": "2.5", "BottomExposureTime": "30",
                "BottomLayerCount": "5", "PrintHeight": "80",
            },
        }
        result = evaluate_production_analysis(analysis, resolved)
        check = next(
            item for item in result["checks"]
            if item["id"] == "uvtools.issue.islands"
        )
        self.assertEqual(check["actual"], 0)
        self.assertEqual(check["ignored_with_prior_layer_contact"], 1)
        self.assertEqual(result["status"], "passed")

    def test_reported_trap_volume_already_contains_layer_height(self):
        resolved = resolve_presets(process="resin", machine="elegoo-mars-3-pro",
            material="anycubic-water-washable-2-grey", purpose="miniature-display-high-detail",
            quality="high-detail", output_format="ctb")
        analysis = {"detection_contract": DETECTION_CONTRACT, "scan_flags": SCAN_FLAGS,
            "issues": {"resin_traps": 1},
            "issue_records": {"resin_traps": [{"measure_pixels": 20000}]}}
        result = evaluate_production_analysis(analysis, resolved)
        check = next(c for c in result["checks"] if c["id"] == "uvtools.issue.resin_traps")
        machine = resolved["machine"]
        px, py = [machine["build_volume_mm"][i]/machine["resolution"][i] for i in (0,1)]
        self.assertAlmostEqual(check["largest_actionable_records"][0]["volume_mm3"],
                               20000*px*py*min(px,py))


if __name__ == "__main__":
    unittest.main()
