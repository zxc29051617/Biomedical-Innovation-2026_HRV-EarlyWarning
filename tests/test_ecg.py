"""Official-WFDB generated fixtures: no hand-written binary signal decoder."""

from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    import numpy as np
    import wfdb
except ImportError:
    np = wfdb = None

from hrv_early_warning.ecg import _cached_leaf, decode_ecg_epoch
from hrv_early_warning.waveforms import read_record_metadata


@unittest.skipUnless(np is not None and wfdb is not None, "optional numpy and wfdb required")
class ECGTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root / "p76/p765432"
        self.folder.mkdir(parents=True)
        self.start = datetime(2101, 2, 3)
        self.end = self.start + timedelta(minutes=5)
        _cached_leaf.cache_clear()

    def write_signal(self, name, values, fs=10, unit="mV", start=None, channel="II", gain=1000):
        values = np.asarray(values, dtype=np.float64).reshape(-1, 1)
        wfdb.wrsamp(name, fs=fs, units=[unit], sig_name=[channel], p_signal=values,
                    fmt=["16"], adc_gain=[gain], baseline=[0], base_datetime=start or self.start,
                    write_dir=str(self.folder))
        return read_record_metadata(self.root, f"p76/p765432/{name}")

    def master(self, name, segments, fs=10, start=None):
        clock = start or self.start
        total = sum(count for _, count in segments)
        text = f"{name}/{len(segments)} 1 {fs} {total} {clock:%H:%M:%S} {clock:%d/%m/%Y}\n"
        text += "".join(f"{segment} {count}\n" for segment, count in segments)
        (self.folder / f"{name}.hea").write_text(text)
        return read_record_metadata(self.root, f"p76/p765432/{name}")

    def test_exact_target_samples_and_explicit_mv_calibration(self):
        values = np.arange(4000, dtype=float) / 1000
        record = self.write_signal("full", values)
        start = self.start + timedelta(seconds=17)
        end = start + timedelta(minutes=5)
        with patch("wfdb.rdrecord", wraps=wfdb.rdrecord) as reader:
            epoch = decode_ecg_epoch([record], start, end)
        self.assertEqual(epoch.status, "ok")
        self.assertEqual(epoch.fs, 10)
        self.assertEqual(epoch.sample_start, start)
        np.testing.assert_allclose(epoch.signal, values[170:3170], atol=1e-12)
        self.assertEqual(reader.call_args.kwargs["sampfrom"], 170)
        self.assertEqual(reader.call_args.kwargs["sampto"], 3170)
        self.assertLess(epoch.sample_start + timedelta(seconds=(len(epoch.signal) - 1) / epoch.fs), end)
        self.assertEqual(epoch.meta["signal_units"], "mV")

    def test_cross_segment_gap_stays_nan_and_no_future_leaf_is_opened(self):
        self.write_signal("a", np.ones(1500))
        self.write_signal("b", np.full(1000, 2.0), start=self.start + timedelta(seconds=200))
        record = self.master("multi", [("a", 1500), ("~", 500), ("b", 1000), ("missing_future", 100)])
        epoch = decode_ecg_epoch([record], self.start, self.end)
        self.assertEqual(epoch.status, "partial")
        self.assertEqual(epoch.meta["segments_read"], 2)
        self.assertNotIn("leaf_header_missing", epoch.errors)
        np.testing.assert_allclose(epoch.signal[:1500], 1)
        self.assertTrue(np.isnan(epoch.signal[1500:2000]).all())
        np.testing.assert_allclose(epoch.signal[2000:], 2)
        self.assertEqual(epoch.missing_samples, 500)
        self.assertAlmostEqual(epoch.coverage_fraction, 5 / 6)

    def test_wfdb_missing_values_remain_missing(self):
        values = np.ones(3000)
        values[100:150] = np.nan
        record = self.write_signal("missing", values)
        epoch = decode_ecg_epoch([record], self.start, self.end)
        self.assertEqual(epoch.missing_samples, 50)
        self.assertTrue(np.isnan(epoch.signal[100:150]).all())

    def test_known_voltage_units_convert_to_mv(self):
        for name, unit, amplitude, gain in [("uv", "uV", 1000, 1), ("volt", "V", 0.001, 1_000_000)]:
            with self.subTest(unit=unit):
                record = self.write_signal(name, np.full(3000, amplitude), unit=unit, gain=gain)
                epoch = decode_ecg_epoch([record], self.start, self.end)
                self.assertEqual(epoch.status, "ok")
                np.testing.assert_allclose(epoch.signal, 1, atol=1e-12)

    def test_unknown_unit_and_implicit_calibration_are_rejected(self):
        record = self.write_signal("unknown", np.ones(3000), unit="NU")
        result = decode_ecg_epoch([record], self.start, self.end)
        self.assertEqual(result.status, "unavailable")
        self.assertIn("unsupported_voltage_unit", result.errors)
        record = self.write_signal("uncalibrated", np.ones(3000))
        path = self.folder / "uncalibrated.hea"
        lines = path.read_text().splitlines()
        signal_fields = lines[1].split()
        signal_fields[2] = signal_fields[2].split("/")[0]
        lines[1] = " ".join(signal_fields)
        path.write_text("\n".join(lines) + "\n")
        result = decode_ecg_epoch([record], self.start, self.end)
        self.assertIn("explicit_calibration_required", result.errors)

    def test_mixed_patients_are_rejected_before_any_header_or_signal_read(self):
        first = self.write_signal("same", np.ones(3000))
        second = replace(first, subject_id=765433)
        with patch("wfdb.rdheader") as header, patch("wfdb.rdrecord") as reader:
            result = decode_ecg_epoch([first, second], self.start, self.end)
        header.assert_not_called()
        reader.assert_not_called()
        self.assertEqual(result.status, "rejected")
        self.assertIn("mixed_subjects", result.errors)

    def test_conflicting_rates_are_rejected_without_resampling(self):
        first = self.write_signal("ten", np.ones(3000), fs=10)
        second = self.write_signal("twenty", np.ones(6000), fs=20)
        with patch("wfdb.rdrecord") as reader:
            result = decode_ecg_epoch([first, second], self.start, self.end)
        reader.assert_not_called()
        self.assertEqual(result.status, "rejected")
        self.assertIn("incompatible_sampling_rates", result.errors)

    def test_leading_and_trailing_missing_time_remain_in_full_epoch_grid(self):
        record = self.write_signal("middle", np.ones(1000), start=self.start + timedelta(seconds=100))
        result = decode_ecg_epoch([record], self.start, self.end)
        self.assertEqual(result.sample_start, self.start)
        self.assertEqual(len(result.signal), 3000)
        self.assertTrue(np.isnan(result.signal[:1000]).all())
        np.testing.assert_allclose(result.signal[1000:2000], 1)
        self.assertTrue(np.isnan(result.signal[2000:]).all())
        self.assertAlmostEqual(result.coverage_fraction, 1 / 3)

    def test_record_identity_must_agree_with_patient_directory(self):
        record = self.write_signal("identity", np.ones(3000))
        wrong = replace(record, subject_id=765433)
        with patch("wfdb.rdheader") as header, patch("wfdb.rdrecord") as reader:
            result = decode_ecg_epoch([wrong], self.start, self.end)
        header.assert_not_called()
        reader.assert_not_called()
        self.assertEqual(result.status, "rejected")
        self.assertIn("record_subject_identity_mismatch", result.errors)

    def test_original_sample_phase_is_preserved_and_incompatible_grids_rejected(self):
        shifted = self.start + timedelta(milliseconds=50)
        record = self.write_signal("phase", np.ones(3000), start=shifted)
        result = decode_ecg_epoch([record], self.start, self.end)
        self.assertEqual(result.sample_start, shifted)
        self.assertEqual(result.status, "ok")
        plain = self.write_signal("plain", np.ones(3000))
        result = decode_ecg_epoch([record, plain], self.start, self.end)
        self.assertEqual(result.status, "rejected")
        self.assertIn("incompatible_sample_grids", result.errors)

    def test_overlapping_conflicts_become_nan_without_order_dependence(self):
        first = self.write_signal("one", np.ones(3000))
        second = self.write_signal("two", np.full(3000, 2.0))
        result = decode_ecg_epoch([first, second], self.start, self.end)
        reverse = decode_ecg_epoch([second, first], self.start, self.end)
        self.assertEqual(result.meta["overlap_conflict_samples"], 3000)
        self.assertTrue(np.isnan(result.signal).all())
        np.testing.assert_equal(result.signal, reverse.signal)
        duplicate = decode_ecg_epoch([first, first], self.start, self.end)
        self.assertEqual(duplicate.coverage_fraction, 1)
        self.assertEqual(duplicate.meta["decoded_sample_count"], 3000)

    def test_missing_dat_and_missing_ii_have_stable_reasons(self):
        record = self.write_signal("deleted", np.ones(3000))
        (self.folder / "deleted.dat").unlink()
        result = decode_ecg_epoch([record], self.start, self.end)
        self.assertIn("signal_file_missing", result.errors)
        other = self.write_signal("pressure", np.ones(3000), channel="ABP")
        result = decode_ecg_epoch([other], self.start, self.end)
        self.assertIn("lead_ii_absent", result.errors)
        for private in ["765432", "p76/", "2101", str(self.root)]:
            self.assertNotIn(private, json.dumps(result.meta))

    def test_leaf_header_cache_hits_and_invalidates_on_header_change(self):
        record = self.write_signal("cached", np.ones(3000))
        with patch("wfdb.rdheader", wraps=wfdb.rdheader) as reader:
            decode_ecg_epoch([record], self.start, self.end)
            first_count = reader.call_count
            decode_ecg_epoch([record], self.start, self.end)
            self.assertEqual(reader.call_count, first_count)
            path = self.folder / "cached.hea"
            path.write_text(path.read_text() + "# changed metadata\n")
            decode_ecg_epoch([record], self.start, self.end)
            self.assertEqual(reader.call_count, first_count + 1)

    def test_only_five_minute_epochs_are_accepted(self):
        for duration in [0, -300, 299, 301]:
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                decode_ecg_epoch([], self.start, self.start + timedelta(seconds=duration))


if __name__ == "__main__":
    unittest.main()
