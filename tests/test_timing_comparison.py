import copy
import importlib.util
import math
from pathlib import Path
import tempfile
import unittest

from hrv_early_warning.timing_comparison import aggregate_comparisons, compare_shared_support


ROOT = Path(__file__).resolve().parents[1]


def complete(reference, baseline, candidate):
    pairs = [(index, index) for index in range(len(reference))]
    return compare_shared_support(reference, baseline, candidate, pairs, pairs,
                                  candidate_ids=list(range(len(candidate))))


class SharedTimingTests(unittest.TestCase):
    def test_constant_fiducial_shift_changes_timing_but_cancels_in_rr(self):
        result = complete([1, 2, 3, 4], [1.02, 2.02, 3.02, 4.02], [1.01, 2.01, 3.01, 4.01])
        self.assertAlmostEqual(result["timing"]["baseline_xqrs"]["mae_ms"], 20)
        self.assertAlmostEqual(result["timing"]["consistent_fiducial"]["mae_ms"], 10)
        self.assertAlmostEqual(result["rr"]["baseline_xqrs"]["mae_ms"], 0)
        self.assertAlmostEqual(result["rmssd_on_common_triplets"]["baseline_xqrs_ms"], 0)
        self.assertEqual(result["common_support"]["original_adjacent_triplets"], 2)

    def test_alternating_error_compares_same_rr_pairs_and_triplets(self):
        result = complete([1, 2, 3, 4], [1.02, 1.98, 3.02, 3.98], [1.01, 2.01, 3.01, 4.01])
        self.assertAlmostEqual(result["rr"]["baseline_xqrs"]["mae_ms"], 40)
        self.assertAlmostEqual(result["rr"]["paired_absolute_error_delta_ms"]["mean"], -40)
        self.assertAlmostEqual(result["rmssd_on_common_triplets"]["baseline_absolute_error_ms"], 80)
        self.assertAlmostEqual(result["rmssd_on_common_triplets"]["candidate_absolute_error_ms"], 0)
        self.assertAlmostEqual(result["rmssd_on_common_triplets"]["paired_absolute_error_delta_ms"], -80)

    def test_abstaining_on_bad_beat_does_not_fabricate_rmssd_improvement(self):
        result = compare_shared_support([1, 2, 3, 4], [1, 2.09, 3, 4], [1, 3, 4],
            [(0, 0), (1, 1), (2, 2), (3, 3)], [(0, 0), (2, 1), (3, 2)], candidate_ids=[0, 2, 3])
        self.assertEqual(result["common_support"]["beats"], 3)
        self.assertEqual(result["common_support"]["original_adjacent_rr"], 1)
        self.assertEqual(result["rmssd_on_common_triplets"]["count"], 0)
        self.assertIsNone(result["rmssd_on_common_triplets"]["paired_absolute_error_delta_ms"])
        self.assertEqual(result["detection"]["consistent_fiducial"]["missed_count"], 1)
        self.assertEqual(result["candidate_retention"]["abstained_count"], 1)

    def test_gap_splits_two_short_sequences_without_joining_them(self):
        result = compare_shared_support([1, 2, 3, 4, 5], [1, 2, 3, 4, 5], [1, 2, 4, 5],
            list(enumerate(range(5))), [(0, 0), (1, 1), (3, 2), (4, 3)], candidate_ids=[0, 1, 3, 4])
        self.assertEqual(result["common_support"]["original_adjacent_rr"], 2)
        self.assertEqual(result["common_support"]["original_adjacent_triplets"], 0)
        self.assertEqual(result["common_support"]["reference_adjacent_rr_fraction"], .5)

    def test_extra_baseline_detection_breaks_original_seed_adjacency(self):
        result = compare_shared_support([1, 2, 3], [1, 2, 2.05, 3], [1, 2, 3],
            [(0, 0), (1, 1), (2, 3)], [(0, 0), (1, 1), (2, 2)], candidate_ids=[0, 1, 3])
        self.assertEqual(result["common_support"]["original_adjacent_rr"], 1)
        self.assertEqual(result["detection"]["baseline_xqrs"]["extra_count"], 1)

    def test_reference_identity_gap_also_breaks_adjacency(self):
        pairs = list(enumerate(range(4)))
        result = compare_shared_support([1, 2, 4, 5], [1, 2, 4, 5], [1, 2, 4, 5], pairs, pairs,
            candidate_ids=[0, 1, 2, 3], reference_ids=[0, 1, 3, 4])
        self.assertEqual(result["reference_denominators"]["original_adjacent_rr"], 2)
        self.assertEqual(result["common_support"]["original_adjacent_triplets"], 0)

    def test_full_seed_retention_is_not_reference_coverage_or_nn_qualification(self):
        result = compare_shared_support([1, 2, 3], [1, 3], [1, 3], [(0, 0), (2, 1)], [(0, 0), (2, 1)], candidate_ids=[0, 1])
        self.assertTrue(result["candidate_retention"]["all_nonempty_seeds_retained"])
        self.assertEqual(result["detection"]["consistent_fiducial"]["missed_count"], 1)
        self.assertIn("unknown", result["nn_qualification"])

    def test_common_reference_intersection_uses_only_both_methods(self):
        result = compare_shared_support([1, 2, 3], [1, 2, 3], [1, 2, 3],
            [(0, 0), (1, 1)], [(1, 1), (2, 2)], candidate_ids=[0, 1, 2])
        self.assertEqual(result["common_support"]["beats"], 1)
        self.assertEqual(result["timing"]["baseline_xqrs"]["count"], 1)
        self.assertEqual(result["timing"]["consistent_fiducial"]["count"], 1)

    def test_empty_candidate_has_missing_not_zero_error(self):
        result = compare_shared_support([1, 2, 3], [1, 2, 3], [], [(0, 0), (1, 1), (2, 2)], [], candidate_ids=[])
        self.assertEqual(result["candidate_retention"]["abstained_count"], 3)
        self.assertEqual(result["detection"]["consistent_fiducial"]["sensitivity"], 0)
        self.assertIsNone(result["timing"]["baseline_xqrs"]["mae_ms"])
        self.assertIsNone(result["rmssd_on_common_triplets"]["reference_ms"])

    def test_empty_reference_has_no_fake_denominator(self):
        result = compare_shared_support([], [1], [1], [], [], candidate_ids=[0])
        self.assertIsNone(result["detection"]["baseline_xqrs"]["sensitivity"])
        self.assertIsNone(result["common_support"]["reference_adjacent_triplet_fraction"])

    def test_exact_tolerance_boundary_and_beyond_are_distinguished(self):
        result = compare_shared_support([360/360], [396/360], [396/360], [(0, 0)], [(0, 0)], candidate_ids=[0])
        self.assertEqual(result["common_support"]["beats"], 1)
        with self.assertRaisesRegex(ValueError, "outside_fixed_tolerance"):
            complete([1], [1.1 + 2e-12], [1.1 + 2e-12])

    def test_invalid_times_ids_and_duration_rejected(self):
        for times in ([1, 1], [2, 1], [1, math.nan], [True], [-1], [300]):
            with self.subTest(times=times), self.assertRaises(ValueError):
                complete(times, times, times)
        for ids in (None, [0, 0], [0, 1.5], [0, 2], [True, 1], [0]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                compare_shared_support([1, 2], [1, 2], [1, 2], [(0, 0), (1, 1)], [(0, 0), (1, 1)], candidate_ids=ids)
        for duration in (None, 0, True, math.inf):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                compare_shared_support([1], [1], [1], [(0, 0)], [(0, 0)], candidate_ids=[0], duration_seconds=duration)

    def test_invalid_matching_indices_rejected(self):
        for pairs in ([(0, 0), (0, 1)], [(1, 0), (0, 1)], [(0, 2)], [(-1, 0)], [(0., 0)], [(True, 0)]):
            with self.subTest(pairs=pairs), self.assertRaises(ValueError):
                compare_shared_support([1, 2], [1, 2], [1, 2], pairs, [(0, 0)], candidate_ids=[0, 1])

    def test_aggregation_keeps_failures_and_support_denominators(self):
        successful = complete([1, 2, 3], [1, 2, 3], [1, 2, 3])
        windows = [{"fs_hz": 125, "status": "evaluated", "reference_beat_count": 3, "comparison": successful},
                   {"fs_hz": 125, "status": "failed", "reference_beat_count": 10}]
        result = aggregate_comparisons(windows)["by_sampling_rate"]["125.0"]
        self.assertEqual(result["failed_windows"], 1)
        self.assertEqual(result["reference_beats_all_planned_windows"], 13)
        self.assertEqual(result["reference_denominators_evaluated_windows"]["beats"], 3)
        self.assertEqual(result["common_support_fractions"]["original_adjacent_triplets"], 1)

    def test_median_paired_window_delta_is_not_difference_of_medians(self):
        windows = [{"fs_hz": 125, "status": "evaluated", "reference_beat_count": 1,
                    "comparison": complete([1], [1 + before / 1000], [1 + after / 1000])}
                   for before, after in ((0, 9), (10, 10), (12, 1))]
        result = aggregate_comparisons(windows)["by_sampling_rate"]["125.0"]["paired_errors"]["timing"]
        self.assertAlmostEqual(result["baseline_median_of_window_mae_ms"], 10)
        self.assertAlmostEqual(result["candidate_median_of_window_mae_ms"], 9)
        self.assertAlmostEqual(result["median_of_paired_window_mae_delta_ms"], 0)


class AdapterContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("paired_adapter", ROOT / "scripts/compare_reference_timing.py")
        cls.adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.adapter)

    def test_output_cannot_overwrite_v1_or_public_files(self):
        for path in (ROOT / "artifacts/m2/paired", ROOT / "docs/results/paired", ROOT / "src/hrv_early_warning"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.adapter.output_directory(path)

    def test_output_cannot_silently_replace_existing_result(self):
        parent = ROOT / "artifacts/rr_validation"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            self.adapter.output_directory(directory)
            (Path(directory) / "paired_timing_summary.json").write_text("{}")
            with self.assertRaises(FileExistsError):
                self.adapter.output_directory(directory)

    def test_detection_regression_requires_exact_original_denominators(self):
        result = complete([1, 2, 3], [1, 2, 3], [1, 2, 3])
        summary = aggregate_comparisons([{"fs_hz": 125, "status": "evaluated", "reference_beat_count": 3, "comparison": result}])
        previous = {"by_sampling_rate": {"125.0": {"total_windows": 1, "evaluated_windows": 1,
            "failed_windows": 0, "full_candidate_windows": 1, "methods": {name: {"detection_totals": {
                "reference_count": 3, "detected_count": 3, "matched_count": 3}}
                for name in ("baseline_xqrs", "consistent_fiducial")}}}}
        self.adapter.verify_frozen_detection(summary, previous)
        changed = copy.deepcopy(previous)
        changed["by_sampling_rate"]["125.0"]["methods"]["baseline_xqrs"]["detection_totals"]["matched_count"] = 2
        with self.assertRaisesRegex(ValueError, "detection_counts_changed"):
            self.adapter.verify_frozen_detection(summary, changed)


if __name__ == "__main__":
    unittest.main()
