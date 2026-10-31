"""Synthetic boundary/algorithm tests, not a clinical signal validation set."""

from dataclasses import asdict
import importlib.util
import json
import unittest
from unittest.mock import patch

try:
    import numpy as np
    from hrv_early_warning.signal_quality import (
        QualityConfig, _match_peaks, _rr_features, assess_ecg_epoch,
    )
except ModuleNotFoundError:
    SIGNAL_DEPENDENCIES = False
else:
    SIGNAL_DEPENDENCIES = True


@unittest.skipUnless(SIGNAL_DEPENDENCIES, "requires signal extras (numpy/scipy)")
class SignalQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fs = 125
        cls.time = np.arange(300 * cls.fs) / cls.fs
        cls.signal = 0.02 * np.sin(2 * np.pi * 0.3 * cls.time)
        cls.signal += np.random.default_rng(20261101).normal(0, 0.003, len(cls.time))
        for beat in np.arange(1, 299, 0.8):
            cls.signal += (
                np.exp(-0.5 * ((cls.time - beat) / 0.014) ** 2)
                - 0.2 * np.exp(-0.5 * ((cls.time - beat + 0.025) / 0.008) ** 2)
                + 0.2 * np.exp(-0.5 * ((cls.time - beat - 0.22) / 0.045) ** 2)
            )
        cls.peaks = np.arange(125, 299 * cls.fs, 100, dtype=np.int64)

    def assess_with_peaks(self, primary=None, secondary=None, signal=None):
        primary = self.peaks if primary is None else primary
        secondary = primary if secondary is None else secondary
        with patch("hrv_early_warning.signal_quality._detect_qrs", return_value=(primary, secondary)):
            return assess_ecg_epoch(self.signal if signal is None else signal, self.fs)

    def assert_rejected(self, result, reason):
        self.assertFalse(result.accepted)
        self.assertIn(reason, result.reasons)
        self.assertEqual(result.features, {})

    def test_exact_length_and_units_valid_signal(self):
        result = self.assess_with_peaks()
        self.assertTrue(result.accepted, result.reasons)
        self.assertEqual(result.features["mean_rr_ms"], 800)
        self.assertEqual(result.features["sdrr_ms"], 0)
        self.assertEqual(result.features["rmssd_rr_ms"], 0)
        self.assertEqual(result.features["heart_rate_from_mean_rr_bpm"], 75)
        self.assertEqual(result.metrics["rr_valid_fraction"], 1)
        self.assertFalse(result.metrics["nn_confirmed"])
        self.assertEqual(result.metrics["rhythm_status"], "not_assessed")
        self.assertEqual(result.metrics["af_exclusion_status"], "not_assessed")
        self.assertEqual(result.metrics["pacing_exclusion_status"], "not_assessed")
        json.dumps(asdict(result), allow_nan=False)

    def test_config_is_json_serializable(self):
        self.assertEqual(json.loads(json.dumps(asdict(QualityConfig())))["epoch_seconds"], 300)

    def test_invalid_config(self):
        for kwargs in ({"minimum_beats": 2}, {"rr_median_window": 10},
                       {"minimum_rr_valid_fraction": 1.1}, {"epoch_seconds": float("nan")},
                       {"minimum_rr_seconds": 3}, {"agreement_block_seconds": 17}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                QualityConfig(**kwargs)

    def test_wrong_duration_is_not_padded_or_truncated(self):
        for signal in (self.signal[:-1], np.r_[self.signal, 0]):
            with self.subTest(length=len(signal)):
                self.assert_rejected(assess_ecg_epoch(signal, self.fs), "wrong_epoch_length")

    def test_empty_signal_is_rejected(self):
        self.assert_rejected(assess_ecg_epoch([], self.fs), "empty_signal")

    def test_nan_and_inf_never_reach_detector(self):
        for value in (float("nan"), float("inf"), -float("inf")):
            signal = self.signal.copy()
            signal[100] = value
            with patch("hrv_early_warning.signal_quality._detect_qrs") as detector:
                self.assert_rejected(assess_ecg_epoch(signal, self.fs), "nonfinite_samples")
                detector.assert_not_called()

    def test_bad_sampling_rate_and_shape(self):
        for fs in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(fs=fs), self.assertRaises(ValueError):
                assess_ecg_epoch(self.signal, fs)
        with self.assertRaises(ValueError):
            assess_ecg_epoch(self.signal.reshape(-1, 1), self.fs)

    def test_low_sampling_frequency_rejected(self):
        self.assert_rejected(assess_ecg_epoch(np.zeros(300 * 50), 50),
                             "sampling_frequency_below_project_minimum")

    def test_constant_and_interior_flat_run(self):
        self.assert_rejected(assess_ecg_epoch(np.ones(len(self.signal)), self.fs), "flat_run")
        signal = self.signal.copy()
        signal[1500:1625] = 0.2
        result = assess_ecg_epoch(signal, self.fs)
        self.assert_rejected(result, "flat_run")
        self.assertEqual(result.metrics["longest_flat_seconds"], 1.0)

    def test_low_amplitude_block(self):
        signal = self.signal.copy()
        signal[1250:2500] *= 0.001
        self.assert_rejected(assess_ecg_epoch(signal, self.fs), "low_block_amplitude")

    def test_excessive_amplitude(self):
        signal = self.signal.copy()
        signal[100] = 20
        self.assert_rejected(assess_ecg_epoch(signal, self.fs), "excessive_amplitude_range")

    def test_extrema_plateau_is_only_suspected_clipping(self):
        signal = self.signal.copy()
        signal[100:105] = 2
        result = assess_ecg_epoch(signal, self.fs)
        self.assert_rejected(result, "suspected_clipping")
        self.assertEqual(result.metrics["longest_extrema_plateau_seconds"], 0.04)

    def test_matches_are_one_to_one_and_boundary_is_inclusive(self):
        primary, secondary = _match_peaks(np.array([100, 110]), np.array([105]), 5)
        self.assertEqual(primary.tolist(), [True, False])
        self.assertEqual(secondary.tolist(), [True])

    def test_global_detector_disagreement(self):
        result = self.assess_with_peaks(secondary=self.peaks + 30)
        self.assert_rejected(result, "low_beat_agreement")
        self.assertEqual(result.metrics["beat_agreement"], 0)

    def test_local_disagreement_not_hidden_by_global_average(self):
        keep = (self.peaks < 100 * self.fs) | (self.peaks >= 110 * self.fs)
        result = self.assess_with_peaks(secondary=self.peaks[keep])
        self.assertGreater(result.metrics["beat_agreement"], 0.9)
        self.assert_rejected(result, "low_block_beat_agreement")

    def test_no_beats(self):
        result = self.assess_with_peaks(primary=np.array([], dtype=int))
        self.assert_rejected(result, "too_few_beats")

    def test_beat_span_does_not_cover_epoch(self):
        result = self.assess_with_peaks(primary=self.peaks[self.peaks > 5 * self.fs])
        self.assert_rejected(result, "incomplete_beat_span")

    def test_rr_extremes_are_reported_not_called_arrhythmia(self):
        peaks = np.arange(100, 300 * self.fs - 100, 30, dtype=int)
        result = self.assess_with_peaks(primary=peaks)
        self.assert_rejected(result, "too_many_screened_rr_intervals")
        self.assertGreater(result.metrics["rr_out_of_range_count"], 0)
        self.assertEqual(result.metrics["rhythm_status"], "not_assessed")

    def test_unmatched_qrs_invalidates_both_neighboring_rr_intervals(self):
        result = self.assess_with_peaks(secondary=np.delete(self.peaks, 100))
        self.assertTrue(result.accepted, result.reasons)
        self.assertEqual(result.metrics["rr_unmatched_endpoint_count"], 2)
        self.assertEqual(result.metrics["rr_valid_count"], len(self.peaks) - 3)

    def test_rmssd_never_bridges_removed_rr_intervals(self):
        rr = np.array([1.0, 1.1, 0.5, 1.4, 1.5])
        valid = np.array([True, True, False, True, True])
        features = _rr_features(rr, valid)
        self.assertAlmostEqual(features["rmssd_rr_ms"], 100)
        self.assertAlmostEqual(features["mean_rr_ms"], 1250)
        self.assertAlmostEqual(features["sdrr_ms"], np.std([1000, 1100, 1400, 1500], ddof=1))

    def test_no_adjacent_valid_rr_means_no_features(self):
        self.assertEqual(_rr_features(np.array([0.8, 0.4, 0.9]), np.array([True, False, True])), {})

    def test_malformed_detector_output_rejected(self):
        for peaks in (np.array([10, 10, 20]), np.array([20, 10]),
                      np.array([10.5, 20.5]), np.array([-1, 10]),
                      np.array([0, len(self.signal)])):
            with self.subTest(peaks=peaks.tolist()):
                self.assert_rejected(self.assess_with_peaks(primary=peaks), "detector_error")

    def test_detector_exception_contains_class_only(self):
        with patch("hrv_early_warning.signal_quality._detect_qrs", side_effect=ValueError("sensitive context")):
            result = assess_ecg_epoch(self.signal, self.fs)
        self.assert_rejected(result, "detector_error")
        self.assertEqual(result.metrics["detector_error_class"], "ValueError")
        self.assertNotIn("sensitive", json.dumps(asdict(result)))

    def test_missing_dependency_is_not_bad_ecg(self):
        with patch("hrv_early_warning.signal_quality._detect_qrs", side_effect=ImportError("missing wfdb")):
            with self.assertRaises(ImportError):
                assess_ecg_epoch(self.signal, self.fs)

    def test_actual_wfdb_detectors_accept_regular_synthetic_signal_and_inverse(self):
        if importlib.util.find_spec("wfdb") is None:
            self.skipTest("requires wfdb")
        for polarity in (1, -1):
            with self.subTest(polarity=polarity):
                result = assess_ecg_epoch(polarity * self.signal, self.fs)
                self.assertTrue(result.accepted, result.reasons)
                self.assertAlmostEqual(result.features["mean_rr_ms"], 800, delta=8)
                self.assertLessEqual(result.features["rmssd_rr_ms"], 16)

    def test_actual_wfdb_detectors_reject_white_noise(self):
        if importlib.util.find_spec("wfdb") is None:
            self.skipTest("requires wfdb")
        noise = np.random.default_rng(42).normal(0, 0.2, len(self.signal))
        result = assess_ecg_epoch(noise, self.fs)
        self.assertFalse(result.accepted)
        self.assertEqual(result.features, {})


if __name__ == "__main__":
    unittest.main()
