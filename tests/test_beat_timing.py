"""Algorithm invariants and synthetic timing arithmetic, not ECG validation."""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    import numpy as np
    from hrv_early_warning.beat_timing import (
        ALGORITHM_VERSION, METRIC_VERSION, TimingConfig, align_fiducials,
        evaluate_timing, match_beats, provenance, quantization_reference,
        resample_for_timing, rr_features,
    )
except ModuleNotFoundError:
    DEPENDENCIES = False
else:
    DEPENDENCIES = True


@unittest.skipUnless(DEPENDENCIES, "requires signal extras numpy/scipy")
class BeatTimingTests(unittest.TestCase):
    @staticmethod
    def pulses(indices=(125, 250, 375), length=500, polarity=1, width=1.5):
        times = np.arange(length)
        return sum(polarity * np.exp(-0.5 * ((times - index) / width) ** 2) for index in indices)

    def test_consistent_positive_fiducial_replaces_alternating_offsets(self):
        result = align_fiducials(self.pulses(), 125, [120, 254, 370])
        np.testing.assert_array_equal(result.indices, [125, 250, 375])
        self.assertEqual(result.polarity, 1)
        self.assertTrue(result.valid.all())
        self.assertEqual(result.metadata["sample_step_ms"], 8)

    def test_inverted_signal_uses_one_negative_polarity(self):
        result = align_fiducials(self.pulses(polarity=-1), 125, [120, 254, 370])
        np.testing.assert_array_equal(result.indices, [125, 250, 375])
        self.assertEqual(result.polarity, -1)

    def test_edges_never_wrap_or_return_negative_corrected_peak(self):
        signal = self.pulses(indices=(2, 125, 498))
        result = align_fiducials(signal, 125, [2, 125, 498])
        np.testing.assert_array_equal(result.indices, [-1, 125, -1])
        self.assertIn("truncated_search_window", result.reasons[0])
        self.assertIn("truncated_search_window", result.reasons[2])

    def test_maximum_on_search_boundary_abstains(self):
        result = align_fiducials(self.pulses(indices=(135,)), 125, [125])
        self.assertFalse(result.valid[0])
        self.assertIn("maximum_on_search_boundary", result.reasons[0])

    def test_duplicate_aligned_peak_rejects_both_seed_identities(self):
        result = align_fiducials(self.pulses(indices=(125,)), 125, [122, 128])
        self.assertFalse(result.valid.any())
        for row in result.reasons:
            self.assertIn("duplicate_or_reordered_peak", row)

    def test_neighbor_collision_does_not_silently_deduplicate(self):
        result = align_fiducials(self.pulses(indices=(125, 145)), 125, [125, 145])
        self.assertFalse(result.valid.any())
        self.assertTrue(all("neighbor_too_close" in row for row in result.reasons))

    def test_disjoint_equal_maxima_and_competing_morphology_abstain(self):
        signal = np.zeros(500)
        signal[[121, 129]] = 1
        result = align_fiducials(signal, 125, [125])
        self.assertFalse(result.valid[0])
        self.assertIn("disjoint_equal_maxima", result.reasons[0])

    def test_wide_plateau_is_not_given_an_arbitrary_first_sample(self):
        signal = np.zeros(500)
        signal[122:129] = 1
        result = align_fiducials(signal, 125, [125])
        self.assertIn("wide_peak_plateau", result.reasons[0])
        self.assertEqual(result.indices[0], -1)

    def test_broad_qrs_check_records_unknown_width(self):
        signal = self.pulses(indices=(125,), width=12)
        config = TimingConfig(baseline_seconds=1)
        result = align_fiducials(signal, 125, [125], config)
        self.assertIn("half_height_width_truncated", result.reasons[0])
        self.assertIn("broad_half_height_peak", result.reasons[0])
        self.assertFalse(result.valid[0])

    def test_global_polarity_ambiguity_abstains(self):
        signal = np.zeros(500)
        signal[[123, 248, 373]] = 1
        signal[[127, 252, 377]] = -1
        result = align_fiducials(signal, 125, [125, 250, 375])
        self.assertEqual(result.polarity, 0)
        self.assertFalse(result.valid.any())

    def test_nonfinite_signal_is_not_imputed(self):
        signal = self.pulses()
        signal[40] = np.nan
        result = align_fiducials(signal, 125, [125, 250, 375])
        self.assertFalse(result.valid.any())
        self.assertTrue(all("nonfinite_signal" in row for row in result.reasons))

    def test_invalid_seed_order_duplicates_and_fractional_indices_rejected(self):
        for seeds in ([250, 125], [125, 125], [125.5], [-1], [500]):
            with self.subTest(seeds=seeds), self.assertRaises(ValueError):
                align_fiducials(self.pulses(), 125, seeds)

    def test_invalid_sampling_and_configuration_rejected(self):
        for fs in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(fs=fs), self.assertRaises(ValueError):
                align_fiducials(self.pulses(), fs, [])
        for arguments in ({"search_seconds": 0}, {"polarity_dominance": 1},
                          {"competing_peak_fraction": 2}, {"baseline_seconds": float("nan")}):
            with self.assertRaises(ValueError):
                TimingConfig(**arguments)

    def test_empty_seeds_are_explicit_empty_result(self):
        result = align_fiducials(self.pulses(), 125, [])
        self.assertEqual(result.summary()["seed_count"], 0)
        self.assertEqual(result.polarity, 0)

    def test_matching_maximizes_count_before_choosing_nearest(self):
        # A nearest-first approach would pair ref .1 to det .11 and miss .2.
        pairs = match_beats([.1, .2], [.01, .11], .1)
        np.testing.assert_array_equal(pairs, [[0, 0], [1, 1]])

    def test_matching_minimizes_error_among_equal_cardinality_solutions(self):
        pairs = match_beats([1, 2], [.93, 1.01, 2.02], .1)
        np.testing.assert_array_equal(pairs, [[0, 1], [1, 2]])

    def test_match_tolerance_is_never_reported_as_timing_accuracy(self):
        result = evaluate_timing([1, 2, 3], [1.09, 2.09, 3.09])
        self.assertEqual(result["detection"]["f1"], 1)
        self.assertAlmostEqual(result["timing_error"]["absolute_median_ms"], 90)
        self.assertFalse(result["matching_tolerance_is_accuracy_threshold"])
        self.assertAlmostEqual(result["shared_adjacent_rr_error"]["mae_ms"], 0)

    def test_alternating_timing_error_has_rr_and_rmssd_error_despite_perfect_detection(self):
        result = evaluate_timing([1, 2, 3, 4], [1.04, 1.96, 3.04, 3.96])
        self.assertEqual(result["detection"]["f1"], 1)
        self.assertAlmostEqual(result["shared_adjacent_rr_error"]["mae_ms"], 80)
        self.assertAlmostEqual(result["shared_adjacent_triplets"]["detected_rmssd_rr_ms"], 160)

    def test_missed_beat_does_not_bridge_original_adjacency(self):
        result = evaluate_timing([1, 2, 3, 4, 5], [1, 2, 4, 5])
        self.assertEqual(result["detection"]["missed_count"], 1)
        self.assertEqual(result["shared_adjacent_rr_error"]["count"], 2)
        self.assertEqual(result["shared_adjacent_triplets"]["count"], 0)
        # Full-window RR deliberately includes the missed-beat consequence.
        self.assertGreater(result["full_window_rr"]["detected"]["rmssd_rr_ms"], 0)

    def test_candidate_rejection_preserves_ids_and_suppresses_full_rmssd(self):
        result = evaluate_timing([1, 2, 3, 4, 5], [1, 2, 4, 5],
                                 detected_ids=[0, 1, 3, 4], full_detected_available=False, duration_seconds=300)
        self.assertEqual(result["shared_adjacent_triplets"]["count"], 0)
        self.assertIsNone(result["full_window_rr"]["detected"])
        self.assertIsNone(rr_features([1, 2, 4, 5], [0, 1, 3, 4])["rmssd_rr_ms"])

    def test_extra_detection_breaks_shared_original_adjacency(self):
        result = evaluate_timing([1, 2, 3, 4], [1, 2, 2.4, 3, 4])
        self.assertEqual(result["detection"]["extra_count"], 1)
        self.assertEqual(result["shared_adjacent_rr_error"]["count"], 2)
        self.assertEqual(result["shared_adjacent_triplets"]["count"], 0)

    def test_empty_inputs_have_null_errors_not_fake_zero_accuracy(self):
        result = evaluate_timing([], [])
        self.assertIsNone(result["detection"]["sensitivity"])
        self.assertIsNone(result["detection"]["f1"])
        self.assertIsNone(result["timing_error"]["absolute_median_ms"])
        json.dumps(result, allow_nan=False)

    def test_half_open_window_and_order_validated(self):
        for times in ([1, 1], [2, 1], [float("nan")]):
            with self.assertRaises(ValueError):
                evaluate_timing(times, [])
        with self.assertRaises(ValueError):
            evaluate_timing([300], [300], duration_seconds=300)
        with self.assertRaises(ValueError):
            evaluate_timing([1], [1], detected_ids=[0, 1])

    def test_resampling_keeps_original_reference_seconds_and_reports_grid_error(self):
        original = np.array([100, 461, 823]) / 360
        before = original.copy()
        signal = np.sin(np.arange(3600) / 360 * 2 * np.pi)
        sampled = resample_for_timing(signal, 360, 125)
        self.assertEqual(len(sampled), 1250)
        grid = quantization_reference(original)
        np.testing.assert_array_equal(original, before)
        self.assertEqual(grid["sample_step_ms"], 8)
        self.assertGreater(grid["timing_error"]["absolute_max_ms"], 0)
        self.assertLessEqual(grid["timing_error"]["absolute_max_ms"], 4)

    def test_metadata_versions_config_and_source_hash_are_reproducible(self):
        metadata = provenance()
        self.assertEqual(metadata["algorithm_version"], ALGORITHM_VERSION)
        self.assertEqual(metadata["metric_version"], METRIC_VERSION)
        self.assertEqual(len(metadata["source_sha256"]), 64)
        self.assertEqual(metadata["config"]["search_seconds"], .08)
        self.assertEqual(metadata["validation_status"], "experimental_not_independently_validated")
        self.assertEqual(metadata, provenance())
        json.dumps(metadata, allow_nan=False)


@unittest.skipUnless(DEPENDENCIES, "requires signal extras numpy/scipy/wfdb")
class ReferenceManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root / "scripts"))
        try:
            import validate_beat_timing
        finally:
            sys.path.pop(0)
        cls.adapter = validate_beat_timing
        cls.plan = json.loads((root / "configs/beat_timing_reference.json").read_text())

    def make_fixture_manifest(self, directory):
        # Byte-only integrity fixture; never represented as annotation evidence.
        files = []
        for row in self.plan["records"]:
            for extension in ("hea", "dat", "atr"):
                filename = f"{row['record']}.{extension}"
                path = directory / filename
                path.write_bytes(b"synthetic unit test bytes, not real ECG")
                files.append({"path": filename, "bytes": path.stat().st_size,
                              "sha256": self.adapter.sha256(path),
                              "source_url": self.plan["download_base"] + filename})
        return {"schema_version": "beat_timing_reference_manifest_v1", "plan": self.plan,
                "plan_sha256": self.adapter.sha256(self.adapter.REPOSITORY / "configs/beat_timing_reference.json"),
                "files": files}

    def test_hash_check_detects_changed_reference_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = self.make_fixture_manifest(root)
            path = root / "manifest.json"
            path.write_text(json.dumps(manifest))
            self.assertEqual(len(self.adapter.checked_manifest(path)["files"]), 18)
            (root / "100.atr").write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "hash_or_size"):
                self.adapter.checked_manifest(path)

    def test_plan_cannot_replace_expert_atr_with_unreviewed_qrs(self):
        plan = json.loads(json.dumps(self.plan))
        plan["records"][0]["annotator"] = "qrs"
        with self.assertRaises(ValueError):
            self.adapter.validate_plan(plan)

    def test_plan_cannot_select_only_favorable_records_or_heldout_role(self):
        plan = json.loads(json.dumps(self.plan))
        plan["records"] = plan["records"][:1]
        with self.assertRaises(ValueError):
            self.adapter.validate_plan(plan)
        plan = json.loads(json.dumps(self.plan))
        plan["records"][0]["role"] = "test"
        with self.assertRaises(ValueError):
            self.adapter.validate_plan(plan)

    def test_manifest_rejects_path_traversal_and_unofficial_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = self.make_fixture_manifest(root)
            path = root / "manifest.json"
            manifest["files"][0]["source_url"] = "https://example.com/100.hea"
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "official_source"):
                self.adapter.checked_manifest(path)
            manifest["files"][0]["path"] = "../100.hea"
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "file_set_mismatch"):
                self.adapter.checked_manifest(path)

    def test_symlink_cannot_escape_verified_data_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = root / "data"
            directory.mkdir()
            manifest = self.make_fixture_manifest(directory)
            path = directory / "manifest.json"
            path.write_text(json.dumps(manifest))
            (directory / "100.dat").rename(root / "outside.dat")
            (directory / "100.dat").symlink_to(root / "outside.dat")
            with self.assertRaisesRegex(ValueError, "outside_data_root"):
                self.adapter.checked_manifest(path)

    def test_output_path_cannot_touch_frozen_m2_or_public_source(self):
        for path in ("artifacts/m2", "docs", "src/hrv_early_warning"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.adapter.output_directory(self.adapter.REPOSITORY / path)

    def test_wfdb_adapter_round_trip_is_synthetic_not_external_validation(self):
        # Deleted temporary fixture exercises decoding/annotation bookkeeping,
        # output JSON, and detector paths. It is not retained as pilot evidence.
        import wfdb
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "output"
            output.mkdir()
            fs = 360
            time = np.arange(300 * fs)
            signal = np.zeros(len(time))
            peaks = np.arange(fs, 299 * fs, fs)
            for peak in peaks:
                signal += np.exp(-0.5 * ((time - peak) / (.012 * fs)) ** 2)
            for row in self.plan["records"]:
                wfdb.wrsamp(row["record"], fs=fs, units=["mV"], sig_name=["SYNTHETIC"],
                            p_signal=signal[:, None], fmt=["16"], write_dir=str(root))
                wfdb.wrann(row["record"], "atr", sample=peaks, symbol=["N"] * len(peaks), fs=fs, write_dir=str(root))
            files = [{"path": path.name, "bytes": path.stat().st_size,
                      "sha256": self.adapter.sha256(path), "source_url": self.plan["download_base"] + path.name}
                     for path in root.iterdir() if path.is_file()]
            manifest = {"schema_version": "beat_timing_reference_manifest_v1", "plan": self.plan,
                        "plan_sha256": self.adapter.sha256(self.adapter.REPOSITORY / "configs/beat_timing_reference.json"),
                        "files": files, "test_fixture_only": True}
            path = root / "manifest.json"
            path.write_text(json.dumps(manifest))
            with patch.object(self.adapter, "output_directory", return_value=output):
                self.adapter.benchmark(path, output)
            report = json.loads((output / "benchmark_summary.json").read_text())
            self.assertTrue(report["complete"])
            self.assertEqual(len(report["records"]), 6)
            self.assertEqual(len(report["windows"]), 12)
            self.assertEqual({row["fs_hz"] for row in report["windows"]}, {360, 125})
            self.assertEqual(report["windows"][0]["reference_beat_count"], len(peaks))
            self.assertEqual(report["windows"][0]["baseline_xqrs"]["detection"]["missed_count"], 0)
            self.assertEqual(report["windows"][0]["nn_qualification"], "unknown_not_inferred_from_N_labels")


if __name__ == "__main__":
    unittest.main()
