"""Temporary synthetic records exercise private caching and stable ordering."""

from dataclasses import asdict
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from hrv_early_warning.waveform_index import load_waveform_index


class WaveformIndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "waveforms"
        self.work = Path(self.temporary.name) / "ignored-cache"
        self.root.mkdir()
        self.entry_a = "p76/p765432/a"
        self.entry_b = "p76/p765433/b"
        self.entry_c = "p76/p765432/c"
        self.header(self.entry_a)
        self.header(self.entry_b)
        self.header(self.entry_c)
        self.index([self.entry_c, self.entry_b, self.entry_a])

    def header(self, entry, valid=True):
        path = self.root / (entry + ".hea")
        path.parent.mkdir(parents=True, exist_ok=True)
        name = Path(entry).name
        path.write_text(f"{name} 1 125 125 04:05:06 03/02/2101\n{name}.dat 16 200/mV 16 0 0 0 0 II\n" if valid else "bad header\n")

    def index(self, entries):
        (self.root / "RECORDS-waveforms").write_text("\n".join(entries) + "\n")

    def test_cache_hit_round_trips_without_master_reads(self):
        records, stats = load_waveform_index(self.root, {765432}, self.work, workers=2)
        self.assertEqual(stats["cache_status"], "rebuilt")
        self.assertEqual(stats["selected_entries"], 2)
        with patch("hrv_early_warning.waveform_index.read_record_metadata", side_effect=AssertionError("must not reopen master")):
            cached_records, cached_stats = load_waveform_index(self.root, {765432}, self.work)
        self.assertEqual([asdict(value) for value in cached_records], [asdict(value) for value in records])
        self.assertEqual(cached_stats["cache_status"], "hit")
        self.assertEqual(cached_stats["workers_used"], 0)

    def test_requested_subject_change_rebuilds(self):
        load_waveform_index(self.root, {765432}, self.work)
        records, stats = load_waveform_index(self.root, {765433}, self.work)
        self.assertEqual(stats["cache_status"], "rebuilt")
        self.assertEqual([record.subject_id for record in records], [765433])

    def test_index_fingerprint_change_rebuilds(self):
        load_waveform_index(self.root, {765432}, self.work)
        self.index([self.entry_a])
        records, stats = load_waveform_index(self.root, {765432}, self.work)
        self.assertEqual(stats["cache_status"], "rebuilt")
        self.assertEqual(len(records), 1)

    def test_missing_header_failure_cached_without_public_identifiers(self):
        self.index([self.entry_a, "p76/p765432/missing"])
        messages = []
        records, stats = load_waveform_index(self.root, {765432}, self.work, progress=messages.append)
        self.assertEqual(len(records), 1)
        self.assertEqual(stats["failed_records"], 1)
        self.assertEqual(stats["error_counts"], {"header_missing": 1})
        self.assertEqual(stats["selected_entries"], stats["parsed_records"] + stats["failed_records"])
        exposed = json.dumps({"stats": stats, "messages": messages})
        for private in ["765432", "p76/", "2101", str(self.root)]:
            self.assertNotIn(private, exposed)
        with patch("hrv_early_warning.waveform_index.read_record_metadata", side_effect=AssertionError("cached failures must not be retried")):
            _, cached = load_waveform_index(self.root, {765432}, self.work)
        self.assertEqual(cached["cache_status"], "hit")
        self.assertEqual(cached["error_counts"], stats["error_counts"])

    def test_stable_order_duplicates_and_worker_cap(self):
        self.index([self.entry_c, self.entry_b, self.entry_a, self.entry_c, "bad-entry"])
        records, stats = load_waveform_index(self.root, {765433, 765432}, self.work, workers=50)
        self.assertEqual([record.record_entry for record in records], sorted([self.entry_a, self.entry_b, self.entry_c]))
        self.assertEqual(stats["duplicate_selected_entries"], 1)
        self.assertEqual(stats["unparsed_index_entries"], 1)
        self.assertLessEqual(stats["workers_used"], 8)
        other, _ = load_waveform_index(self.root, {765432, 765433}, self.work.parent / "other-cache", workers=1)
        self.assertEqual(other, records)

    def test_truncated_cache_rebuilds_instead_of_accepting_partial_records(self):
        load_waveform_index(self.root, {765432}, self.work)
        path = self.work / "waveform_metadata.jsonl"
        lines = path.read_text().splitlines()
        path.write_text("\n".join(lines[:-1]) + "\n")
        records, stats = load_waveform_index(self.root, {765432}, self.work)
        self.assertEqual(stats["cache_status"], "rebuilt")
        self.assertEqual(len(records), 2)

    def test_warnings_retained_and_not_treated_as_parse_failure(self):
        path = self.root / (self.entry_a + ".hea")
        path.write_text(path.read_text().replace("a 1 125", "different 1 125"))
        records, stats = load_waveform_index(self.root, {765432}, self.work)
        self.assertEqual(len(records), 2)
        self.assertEqual(stats["failed_records"], 0)
        self.assertEqual(stats["warning_counts"], {"record_name_differs_from_header_filename": 1})
        cached, cached_stats = load_waveform_index(self.root, {765432}, self.work)
        self.assertEqual(cached, records)
        self.assertEqual(cached_stats["warning_counts"], stats["warning_counts"])

    def test_empty_subject_selection_writes_valid_empty_cache(self):
        records, stats = load_waveform_index(self.root, set(), self.work)
        self.assertEqual(records, [])
        self.assertEqual(stats["selected_entries"], 0)
        self.assertEqual(stats["workers_used"], 0)
        _, cached = load_waveform_index(self.root, set(), self.work)
        self.assertEqual(cached["cache_status"], "hit")


if __name__ == "__main__":
    unittest.main()
