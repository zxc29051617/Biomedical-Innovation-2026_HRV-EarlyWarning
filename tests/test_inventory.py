"""Synthetic fixtures only: inventory must not expose patient-level output."""

import csv
import json
from pathlib import Path
import tempfile
import unittest

from hrv_early_warning.inventory import audit_inventory, parse_waveform_subject


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clinical = self.root / "clinical"
        self.waveforms = self.root / "waveforms"
        self.clinical.mkdir()
        self.waveforms.mkdir()

    def table(self, name, columns, rows):
        with (self.clinical / name).open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            writer.writerows(rows)

    def fixture(self):
        self.table("PATIENTS.csv", ["SUBJECT_ID"], [[765432], [765433], [765434]])
        self.table("ADMISSIONS.csv", ["SUBJECT_ID", "HADM_ID"],
                   [[765432, 810001], [765432, 810002], [765433, 810003], [765434, 810004]])
        self.table("ICUSTAYS.csv", ["SUBJECT_ID", "HADM_ID", "ICUSTAY_ID", "DBSOURCE"],
                   [[765432, 810001, 910001, "metavision"],
                    [765432, 810002, 910002, "metavision"],
                    [765433, 810003, 910003, "carevue"],
                    [765434, 810004, 910004, "metavision"]])
        self.table("DIAGNOSES_ICD.csv", ["SUBJECT_ID", "HADM_ID", "ICD9_CODE"],
                   [[765432, 810001, "410.01"], [765432, 810001, "4280"],
                    [765432, 810002, "99592"], [765432, 810002, "78552"],
                    [765432, 810002, "99592"], [765433, 810003, "4275"],
                    [765434, 810004, "4109"]])
        for name in ["RECORDS", "RECORDS-waveforms", "RECORDS-numerics"]:
            (self.waveforms / name).write_text(
                "p76/p765432/record_a\np76/p765432/record_b\np76/p765433/record_c\n"
            )

    def test_subject_parser_uses_directory_and_rejects_unsafe_entries(self):
        self.assertEqual(parse_waveform_subject("p00/p000020/record"), 20)
        self.assertEqual(parse_waveform_subject("p00/p000020/"), 20)
        for entry in ["record_000020", "p00/p00020/record", "../p000020/record",
                      "/p00/p000020/record", "p000020/p000021/record"]:
            with self.subTest(entry=entry):
                self.assertIsNone(parse_waveform_subject(entry))

    def test_multiple_admissions_count_subjects_once_without_implying_time_overlap(self):
        self.fixture()
        report = audit_inventory(self.clinical, self.waveforms)
        admissions = report["clinical_tables"]["ADMISSIONS.csv"]["summary"]
        self.assertEqual(admissions["rows"], 4)
        self.assertEqual(admissions["unique_subjects"], 3)
        self.assertEqual(admissions["unique_admissions"], 4)
        mv = report["clinical_tables"]["ICUSTAYS.csv"]["summary"]["metavision"]
        self.assertEqual(mv["unique_icu_stays"], 3)
        self.assertEqual(mv["waveform_subject_overlap"], 1)
        self.assertEqual(mv["icu_stays_with_subject_in_waveform_index"], 2)
        cohorts = report["diagnosis_cohorts"]
        self.assertEqual(cohorts["myocardial_infarction"]["unique_subjects"], 2)
        self.assertEqual(cohorts["myocardial_infarction"]["waveform_subject_overlap"], 1)
        self.assertEqual(cohorts["sepsis"]["unique_admissions"], 1)
        self.assertIn("does not establish the same admission", report["limitations"][0])
        serialized = json.dumps(report)
        for private in ["765432", "810001", "910001", "record_a"]:
            self.assertNotIn(private, serialized)

    def test_missing_index_is_unknown_overlap_not_zero(self):
        self.fixture()
        (self.waveforms / "RECORDS-waveforms").unlink()
        report = audit_inventory(self.clinical, self.waveforms)
        self.assertEqual(report["waveform_indices"]["RECORDS-waveforms"]["status"], "missing")
        self.assertIsNone(report["diagnosis_cohorts"]["sepsis"]["waveform_subject_overlap"])
        self.assertIsNone(report["clinical_tables"]["ICUSTAYS.csv"]["summary"]["metavision"]["waveform_subject_overlap"])

    def test_invalid_header_is_reported_without_scanning_or_leaking_row(self):
        self.fixture()
        (self.clinical / "DIAGNOSES_ICD.csv").write_text("765432,810001,PRIVATE_DATA\n")
        report = audit_inventory(self.clinical, self.waveforms)
        table = report["clinical_tables"]["DIAGNOSES_ICD.csv"]
        self.assertEqual(table["status"], "invalid_header")
        self.assertNotIn("summary", table)
        self.assertIsNone(report["diagnosis_cohorts"])
        self.assertNotIn("PRIVATE_DATA", json.dumps(report))

    def test_missing_files_do_not_produce_fabricated_cohort_counts(self):
        report = audit_inventory(self.clinical, self.waveforms)
        self.assertIsNone(report["diagnosis_cohorts"])
        self.assertEqual(report["clinical_tables"]["PATIENTS.csv"]["status"], "missing")
        self.assertTrue(report["warnings"])

    def test_large_table_reads_header_only(self):
        self.fixture()
        (self.clinical / "CHARTEVENTS.csv").write_bytes(b"SUBJECT_ID,ITEMID,CHARTTIME\n\xff")
        report = audit_inventory(self.clinical, self.waveforms)
        table = report["clinical_tables"]["CHARTEVENTS.csv"]
        self.assertEqual(table["status"], "ok")
        self.assertEqual(table["read_scope"], "header only")
        self.assertNotIn("summary", table)

    def test_header_sampling_multisegment_and_missing_files(self):
        self.fixture()
        folder = self.waveforms / "p76/p765432"
        folder.mkdir(parents=True)
        (folder / "record_a.hea").write_text("record_a/3 2 125 1000\nmissing 0\nlayout 0\nsegment 1000\n")
        (folder / "layout.hea").write_text("layout 2 125 0\n~ 0 0 0 0 0 0 0 II\n~ 0 0 0 0 0 0 0 ABP\n")
        report = audit_inventory(self.clinical, self.waveforms, header_sample_size=2)
        sample = report["waveform_header_sample"]
        self.assertEqual(sample["requested_records"], 2)
        self.assertEqual(sample["counts"]["records_with_lead_ii"], 1)
        self.assertEqual(sample["counts"]["missing_master_headers"], 1)
        self.assertEqual(sample["counts"]["missing_segment_headers"], 1)
        self.assertEqual(sample["channel_record_counts"], {"ABP": 1, "II": 1})
        self.assertEqual(sample["sampling_rate_hz_record_counts"], {"125": 1})
        self.assertTrue(any("header sample" in message for message in report["warnings"]))

    def test_sample_size_validation(self):
        for value in [-1, 1.5, True]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                audit_inventory(self.clinical, self.waveforms, value)


if __name__ == "__main__":
    unittest.main()
