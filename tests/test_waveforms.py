"""Synthetic WFDB headers test timing, gaps, and metadata-only coverage."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from hrv_early_warning.waveforms import (
    HeaderParseError, coverage_seconds, inspect_ecg_intervals,
    overlap_with_icu, parse_record_header, read_record_metadata,
)


class WaveformTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "p00/p000001"
        self.directory.mkdir(parents=True)
        self.start = datetime(2101, 2, 3, 4, 5, 6)

    def write(self, name, text):
        path = self.directory / name
        path.write_text(text)
        return path

    def signal(self, name, samples, channel="II", rate="125", data=True, clock="04:05:06"):
        self.write(name + ".hea", f"{name} 1 {rate} {samples} {clock}\n{name}.dat 16 200/mV 16 0 0 0 0 {channel}\n")
        if data:
            (self.directory / (name + ".dat")).write_bytes(b"\x01\x02")

    def multisegment(self):
        path = self.write("master.hea", "master/4 2 125 1500 04:05:06 03/02/2101\nlayout 0\na 500\n~ 250\nb 750\n")
        self.write("layout.hea", "layout 2 125 0\n~ 0 200/mV 16 0 0 0 0 II\n~ 0 200/mV 16 0 0 0 0 ABP\n")
        self.signal("a", 500)
        self.signal("b", 750)
        return parse_record_header(path, self.root)

    def test_multisegment_layout_gap_and_sample_offsets(self):
        record = self.multisegment()
        self.assertEqual(record.subject_id, 1)
        self.assertEqual(record.start, self.start)
        self.assertEqual(record.end, self.start + timedelta(seconds=12))
        self.assertTrue(record.segments[0].is_layout)
        self.assertTrue(record.segments[2].is_gap)
        self.assertEqual(record.segments[3].start, self.start + timedelta(seconds=6))
        result = inspect_ecg_intervals(record, self.start, record.end)
        self.assertEqual(result.segments_checked, 2)
        self.assertEqual(coverage_seconds(result.intervals, self.start, record.end), 10)
        self.assertEqual(result.warnings, ())

    def test_lookback_clips_and_does_not_inspect_nonoverlapping_leaf(self):
        record = self.multisegment()
        (self.directory / "b.hea").unlink()
        result = inspect_ecg_intervals(record, self.start + timedelta(seconds=1), self.start + timedelta(seconds=3))
        self.assertEqual(result.segments_checked, 1)
        self.assertEqual(result.warnings, ())
        self.assertEqual(coverage_seconds(result.intervals, self.start, record.end), 2)

    def test_missing_leaf_and_layout_never_prove_signal_coverage(self):
        record = self.multisegment()
        (self.directory / "a.hea").unlink()
        (self.directory / "b.hea").unlink()
        result = inspect_ecg_intervals(record, record.start, record.end)
        self.assertEqual(result.intervals, ())
        self.assertEqual(result.warnings, ("header_missing", "header_missing"))
        self.assertEqual(result.segments_checked, 2)

    def test_no_ii_and_missing_or_empty_dat_are_not_coverage(self):
        record = self.multisegment()
        self.signal("a", 500, channel="ABP")
        (self.directory / "b.dat").unlink()
        result = inspect_ecg_intervals(record, record.start, record.end)
        self.assertEqual(result.intervals, ())
        self.assertIn("segment_has_no_explicit_lead_ii", result.warnings)
        self.assertIn("lead_ii_signal_file_missing", result.warnings)
        (self.directory / "b.dat").touch()
        result = inspect_ecg_intervals(record, record.start, record.end)
        self.assertIn("lead_ii_signal_file_empty", result.warnings)

    def test_header_date_is_day_first_and_fractional_time_is_supported(self):
        path = self.write("single.hea", "single 1 125/1000(0) 125 4:5:6.25 03/02/2101\nsingle.dat 16 200/mV 16 0 0 0 0 II\n")
        record = parse_record_header(path, self.root)
        self.assertEqual(record.start, self.start.replace(microsecond=250000))
        self.assertEqual(record.end - record.start, timedelta(seconds=1))
        path.write_text(path.read_text().replace("03/02/2101", "2101-02-03"))
        self.assertEqual(parse_record_header(path, self.root).start, record.start)

    def test_missing_date_never_uses_filename_or_today(self):
        path = self.write("p000001-2101-02-03-04-05.hea", "p000001-2101-02-03-04-05 1 125 125 04:05:06\na.dat 16 200/mV 16 0 0 0 0 II\n")
        with self.assertRaisesRegex(HeaderParseError, "explicit_header_date"):
            parse_record_header(path, self.root)
        path.write_text(path.read_text().replace("04:05:06\n", "04:05:06 0/0/0\n"))
        with self.assertRaisesRegex(HeaderParseError, "invalid_header_date"):
            parse_record_header(path, self.root)

    def test_filename_disagreement_warns_and_header_remains_authoritative(self):
        path = self.write("p000001-2101-02-02-04-05.hea", "p000001-2101-02-02-04-05 1 125 125 04:05:06 03/02/2101\na.dat 16 200/mV 16 0 0 0 0 II\n")
        record = parse_record_header(path, self.root)
        self.assertEqual(record.start, self.start)
        self.assertIn("record_name_timestamp_mismatch_header_used", record.warnings)

    def test_header_record_name_mismatch_is_visible(self):
        path = self.write("different.hea", "original 1 125 125 04:05:06 03/02/2101\na.dat 16 200/mV 16 0 0 0 0 II\n")
        record = parse_record_header(path, self.root)
        self.assertIn("record_name_differs_from_header_filename", record.warnings)

    def test_noninteger_frequency_and_changed_leaf_frequency(self):
        path = self.write("precise.hea", "precise/1 1 125.123456789123456789 125 04:05:06 03/02/2101\na 125\n")
        self.signal("a", 125, rate="125.123456789123456789")
        record = parse_record_header(path, self.root)
        result = inspect_ecg_intervals(record, record.start, record.end)
        self.assertEqual(result.warnings, ())
        self.assertEqual(len(result.intervals), 1)
        self.signal("a", 125, rate="126")
        result = inspect_ecg_intervals(record, record.start, record.end)
        self.assertEqual(result.intervals, ())
        self.assertIn("segment_sampling_rate_mismatch", result.warnings)

    def test_zero_samples_produce_no_known_coverage(self):
        path = self.write("zero.hea", "zero 1 125 0 04:05:06 03/02/2101\na.dat 16 200/mV 16 0 0 0 0 II\n")
        record = parse_record_header(path, self.root)
        self.assertEqual(record.start, record.end)
        self.assertIn("zero_samples_no_known_span", record.warnings)
        result = inspect_ecg_intervals(record, self.start, self.start + timedelta(hours=6))
        self.assertEqual(result.intervals, ())
        self.assertEqual(result.segments_checked, 0)

    def test_master_and_leaf_count_inconsistency_are_not_silently_accepted(self):
        record = self.multisegment()
        self.signal("a", 499)
        result = inspect_ecg_intervals(record, record.start, record.end)
        self.assertIn("segment_sample_count_mismatch", result.warnings)
        self.assertEqual(coverage_seconds(result.intervals, record.start, record.end), 6)
        master = self.directory / "master.hea"
        master.write_text(master.read_text().replace("125 1500", "125 1501"))
        with self.assertRaisesRegex(HeaderParseError, "master_segment_sample_count_mismatch"):
            parse_record_header(master, self.root)

    def test_icu_overlap_requires_same_subject_and_actual_time_intersection(self):
        record = self.multisegment()
        self.assertIsNone(overlap_with_icu(record, 2, record.start, record.end))
        self.assertIsNone(overlap_with_icu(record, 1, record.end, record.end + timedelta(hours=1)))
        self.assertEqual(overlap_with_icu(record, 1, self.start + timedelta(seconds=2), record.end + timedelta(seconds=2)),
                         (self.start + timedelta(seconds=2), record.end))

    def test_multiple_records_union_overlaps_and_adjacent_intervals_without_double_counting(self):
        s = self.start
        intervals = [(s - timedelta(seconds=5), s + timedelta(seconds=5)),
                     (s + timedelta(seconds=3), s + timedelta(seconds=8)),
                     (s + timedelta(seconds=8), s + timedelta(seconds=10)),
                     (s + timedelta(seconds=12), s + timedelta(seconds=15))]
        self.assertEqual(coverage_seconds(intervals, s, s + timedelta(seconds=13)), 11)
        self.assertEqual(coverage_seconds([], s, s), 0)
        with self.assertRaises(ValueError):
            coverage_seconds([(s + timedelta(seconds=1), s)], s, s + timedelta(seconds=5))

    def test_mixed_naive_aware_rejected_and_different_timezones_compared_by_instant(self):
        s = self.start.replace(tzinfo=timezone.utc)
        other = s.astimezone(timezone(timedelta(hours=8)))
        self.assertEqual(coverage_seconds([(other, other + timedelta(seconds=10))], s, s + timedelta(seconds=5)), 5)
        with self.assertRaises(ValueError):
            coverage_seconds([(self.start, self.start + timedelta(seconds=5))], s, s + timedelta(seconds=5))

    def test_read_entry_and_path_safety(self):
        self.multisegment()
        record = read_record_metadata(self.root, "p00/p000001/master")
        self.assertEqual(record.subject_id, 1)
        for entry in ["../outside", "/tmp/outside", ""]:
            with self.subTest(entry=entry), self.assertRaises(HeaderParseError):
                read_record_metadata(self.root, entry)


if __name__ == "__main__":
    unittest.main()
