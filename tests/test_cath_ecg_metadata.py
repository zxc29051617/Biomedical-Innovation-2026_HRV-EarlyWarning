"""Synthetic metadata only; no download, patients, or waveform dependencies."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/audit_cath_ecg_metadata.py"
SPEC = importlib.util.spec_from_file_location("audit_cath_ecg_metadata", SCRIPT)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def archive_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return buffer.getvalue()


def inspect(text):
    return audit.inspect_csv(io.StringIO(text), "metadata.csv")


class MetadataAggregationTests(unittest.TestCase):
    def test_rows_distinct_patients_duplicates_and_unknown_labels(self):
        summary, schema, patients = inspect(
            " Patient ID ,TIME_INTERVAL,AMI,OMI,PCI,Prior PCI,ecg_row_record,ecg_med_record\n"
            "private-person-a,0,1,0,2,unknown,private-raw-a,private-median-a\n"
            " private-person-a ,60,0,1,1.0,1,,private-median-b\n"
            "private-person-b,61,NA,2,false,0,private-raw-c,\n"
            ",bad,unknown,,0,NaN,,\n")
        self.assertEqual(summary["row_count"], 4)
        self.assertEqual(summary["patient_id"], {
            "column_present": True, "nonempty_rows": 3, "missing_rows": 1,
            "distinct_count": 2, "duplicate_rows_beyond_first_per_id": 1})
        self.assertEqual(summary["labels"]["PCI"]["counts"], {"zero": 1, "one": 1, "missing": 0, "other": 2})
        self.assertEqual(summary["labels"]["AMI"]["counts"], {"zero": 1, "one": 1, "missing": 1, "other": 1})
        self.assertEqual(summary["ecg_filename_fields"]["raw_ecg_filename"][0]["nonempty"], 2)
        self.assertEqual(summary["ecg_filename_fields"]["median_ecg_filename"][0]["nonempty"], 2)
        self.assertEqual(schema["normalization"][0]["normalized"], "patient_id")
        self.assertEqual(patients, {"private-person-a", "private-person-b"})
        public = json.dumps([summary, schema])
        for secret in ("private-person-a", "private-person-b", "private-raw-a", "private-median-b", "unknown"):
            self.assertNotIn(secret, public)

    def test_time_ranges_are_disjoint_and_numeric_minutes_only(self):
        values = ["-1", "0", "60", "60.5", "360", "361", "1440", "1441", "10080", "10081", "", "NA", "1 days 01:00:00", "inf"]
        summary, _, _ = inspect("Time_Interval\n" + "\n".join('"' + x + '"' for x in values) + "\n")
        result = summary["time_interval_minutes"]
        self.assertEqual(result["counts"], {"missing": 2, "invalid": 2, "valid": 10, "noninteger_valid": 1})
        self.assertEqual(result["ranges"], dict(zip(audit.TIME_BINS, [1, 2, 2, 2, 2, 1])))
        self.assertEqual(sum(result["ranges"].values()), result["counts"]["valid"])
        self.assertEqual((result["minimum"], result["maximum"]), (-1, 10081))

    def test_unknown_columns_are_reported_missing_not_inferred(self):
        summary, schema, patients = inspect("case_number,treatment,interval\nsecret-id,yes,10\n")
        self.assertEqual(summary["row_count"], 1)
        self.assertFalse(summary["patient_id"]["column_present"])
        self.assertIsNone(summary["patient_id"]["distinct_count"])
        self.assertIsNone(summary["time_interval_minutes"]["counts"])
        self.assertIsNone(summary["labels"]["PCI"]["counts"])
        self.assertIn("patient_id", schema["missing_expected_fields"])
        self.assertIsNone(patients)
        self.assertNotIn("secret-id", json.dumps([summary, schema]))

    def test_patient_id_text_preserves_leading_zeros_and_case(self):
        summary, _, _ = inspect("Patient_id\n001\n1\nA\na\n")
        self.assertEqual(summary["patient_id"]["distinct_count"], 4)

    def test_cross_csv_overlap_counts_only_and_missing_id_is_unknown(self):
        data = archive_bytes([
            ("a.csv", "Patient_id,PCI\nprivate-a,1\nprivate-b,0\nprivate-b,1\n"),
            ("b.csv", "patient_id,OMI\nprivate-b,1\nprivate-c,0\n"),
            ("c.csv", "unknown_column\nprivate-b\n")])
        summary, schema = audit.audit_archive(data)
        overlaps = summary["cross_csv_patient_id_overlap"]
        self.assertEqual(overlaps[0]["shared_patient_id_count"], 1)
        self.assertIsNone(overlaps[1]["shared_patient_id_count"])
        self.assertEqual(summary["csv_count"], 3)
        self.assertTrue(summary["audit_rules"]["not_a_modeling_cohort_or_waveform_eligibility_audit"])
        self.assertNotIn("private-", json.dumps([summary, schema]))

    def test_ambiguous_normalized_header_and_malformed_row_fail_without_values(self):
        for text, code in [
            ("Patient_id, patient ID\na,a\n", "duplicate_normalized_csv_columns"),
            ("Patient_id,PCI\nprivate-a,1,secret-extra\n", "csv_row_width_mismatch")]:
            with self.subTest(code=code), self.assertRaisesRegex(audit.AuditError, "^" + code + "$"):
                inspect(text)

    def test_label_other_does_not_become_negative(self):
        values = ["0", "0.0", "1", "1e0", "2", "-1", "True", "unknown", "inf", "", "NULL"]
        self.assertEqual([audit.classify_label(x) for x in values],
                         ["zero", "zero", "one", "one", "other", "other", "other", "other", "other", "missing", "missing"])


class SourceLabelFeasibilityTests(unittest.TestCase):
    def test_omi_patient_counts_and_source_stemi_intersection_use_same_row(self):
        summary, _, _ = inspect(
            "Patient_id,OMI,STEMI\n"
            "private-a,1,1\nprivate-a,0,0\n"
            "private-b,1,0\nprivate-b,1,0\n,1,0\n")
        result = summary["source_label_feasibility"]
        self.assertEqual(result["source_omi"]["counts_by_value"]["one"], {
            "row_count": 4, "distinct_patient_count": 2, "missing_patient_id_rows": 1})
        # private-a has each condition on a different row and must not enter the intersection.
        self.assertEqual(result["source_omi_one_and_source_stemi_zero"]["counts"], {
            "row_count": 3, "distinct_patient_count": 1, "missing_patient_id_rows": 1})
        self.assertEqual(result["source_label_variation_within_patient"]["OMI"]["counts"]
                         ["patients_with_both_zero_and_one"], 1)
        self.assertEqual(result["source_label_variation_within_patient"]["STEMI"]["counts"]
                         ["patients_with_both_zero_and_one"], 1)

    def test_record_distribution_and_label_variation_do_not_relabel_patients(self):
        summary, _, _ = inspect(
            "Patient_id,OMI\nprivate-a,0\nprivate-a,1\nprivate-a,2\n"
            "private-b,1\nprivate-b,NA\nprivate-c,other\n,0\n")
        result = summary["source_label_feasibility"]
        distribution = result["patient_record_count_distribution"]
        self.assertEqual(distribution["distribution"], [
            {"record_count": 1, "patient_count": 1},
            {"record_count": 2, "patient_count": 1},
            {"record_count": 3, "patient_count": 1}])
        self.assertEqual(distribution["patients_with_multiple_records"], 2)
        self.assertEqual(sum(cell["record_count"] * cell["patient_count"]
                             for cell in distribution["distribution"]), summary["patient_id"]["nonempty_rows"])
        self.assertEqual(result["source_label_variation_within_patient"]["OMI"]["counts"], {
            "patients_with_any_zero": 1, "patients_with_any_one": 2,
            "patients_with_any_missing": 1, "patients_with_any_other": 2,
            "patients_with_both_zero_and_one": 1})
        self.assertIsNone(result["source_label_variation_within_patient"]["STEMI"]["counts"])
        # Source values remain intact; an any-positive patient is not relabeled on other rows.
        self.assertEqual(summary["labels"]["OMI"]["counts"], {
            "zero": 2, "one": 2, "missing": 1, "other": 2})

    def test_fixed_time_strata_reconcile_rows_but_patient_counts_are_nonadditive(self):
        summary, _, _ = inspect(
            "Patient_id,Time_Interval,OMI\n"
            "private-p,-1,1\nprivate-p,0,1\nprivate-p,60,1\nprivate-p,60.5,1\n"
            "private-q,360,0\nprivate-p,361,0\nprivate-q,1440,NA\n"
            "private-q,1441,2\nprivate-q,10080,0\nprivate-r,10081,1\n"
            "private-s,,1\nprivate-s,NA,0\nprivate-t,1 days 01:00:00,1\n,inf,1\n")
        result = summary["source_label_feasibility"]
        strata = result["time_interval_by_source_omi"]["strata"]
        self.assertEqual(tuple(strata), audit.TIME_STRATA)
        self.assertEqual(strata["zero_to_60_minutes"]["one"], {
            "row_count": 2, "distinct_patient_count": 1, "missing_patient_id_rows": 0})
        expected = {
            "negative": {"one": 1},
            "zero_to_60_minutes": {"one": 2},
            "over_60_to_360_minutes": {"one": 1, "zero": 1},
            "over_360_to_1440_minutes": {"zero": 1, "missing": 1},
            "over_1440_to_10080_minutes": {"other": 1, "zero": 1},
            "over_10080_minutes": {"one": 1},
            "missing": {"one": 1, "zero": 1},
            "invalid": {"one": 2},
        }
        for stratum, by_value in strata.items():
            self.assertEqual({value: cell["row_count"] for value, cell in by_value.items()},
                             {value: expected[stratum].get(value, 0) for value in audit.LABEL_VALUES})
        self.assertEqual(strata["invalid"]["one"]["missing_patient_id_rows"], 1)
        self.assertEqual(sum(cell["row_count"] for by_value in strata.values()
                             for cell in by_value.values()), summary["row_count"])
        for value, omi_counts in result["source_omi"]["counts_by_value"].items():
            self.assertEqual(sum(by_value[value]["row_count"] for by_value in strata.values()),
                             omi_counts["row_count"])
        self.assertGreater(sum(by_value["one"]["distinct_patient_count"] for by_value in strata.values()),
                           result["source_omi"]["counts_by_value"]["one"]["distinct_patient_count"])

    def test_missing_columns_are_unavailable_while_missing_ids_preserve_record_counts(self):
        summary, _, _ = inspect("Patient_id,Time_Interval\nprivate-a,10\n")
        result = summary["source_label_feasibility"]
        self.assertIsNone(result["source_omi"]["counts_by_value"])
        self.assertIsNone(result["source_omi_one_and_source_stemi_zero"]["counts"])
        self.assertIsNone(result["time_interval_by_source_omi"]["strata"])
        for label in result["source_label_variation_within_patient"].values():
            self.assertIsNone(label["counts"])

        summary, _, _ = inspect("Time_Interval,OMI,STEMI\n10,1,0\n")
        result = summary["source_label_feasibility"]
        expected = {"row_count": 1, "distinct_patient_count": None, "missing_patient_id_rows": None}
        self.assertEqual(result["source_omi"]["counts_by_value"]["one"], expected)
        self.assertEqual(result["source_omi_one_and_source_stemi_zero"]["counts"], expected)
        self.assertEqual(result["time_interval_by_source_omi"]["strata"]["zero_to_60_minutes"]["one"], expected)
        self.assertIsNone(result["patient_record_count_distribution"]["distribution"])
        self.assertIsNone(result["source_label_variation_within_patient"]["OMI"]["counts"])

        summary, _, _ = inspect("Patient_id,OMI,STEMI\nprivate-a,1,0\n")
        result = summary["source_label_feasibility"]
        self.assertIsNone(result["time_interval_by_source_omi"]["strata"])
        summary, _, _ = inspect("Patient_id,OMI,Time_Interval\nprivate-a,1,10\n")
        self.assertIsNone(summary["source_label_feasibility"]["source_omi_one_and_source_stemi_zero"]["counts"])

    def test_present_columns_with_no_rows_report_zero_not_unavailable(self):
        summary, _, _ = inspect("Patient_id,Time_Interval,OMI,STEMI\n")
        result = summary["source_label_feasibility"]
        empty = {"row_count": 0, "distinct_patient_count": 0, "missing_patient_id_rows": 0}
        self.assertEqual(result["source_omi"]["counts_by_value"]["one"], empty)
        self.assertEqual(result["source_omi_one_and_source_stemi_zero"]["counts"], empty)
        self.assertEqual(result["time_interval_by_source_omi"]["strata"]["missing"]["one"], empty)
        self.assertEqual(result["patient_record_count_distribution"]["distribution"], [])

    def test_missing_and_nonbinary_stemi_values_do_not_enter_source_zero_intersection(self):
        summary, _, _ = inspect(
            "Patient_id,OMI,STEMI\nprivate-a,1,\nprivate-b,1,2\nprivate-c,1,True\n"
            "private-d,1,NaN\nprivate-e,unknown,0\nprivate-f,0,0\n")
        counts = summary["source_label_feasibility"]["source_omi_one_and_source_stemi_zero"]["counts"]
        self.assertEqual(counts["row_count"], 0)
        self.assertEqual(counts["distinct_patient_count"], 0)

    def test_order_invariance_and_aggregate_only_output(self):
        header = "Patient_id,Time_Interval,OMI,STEMI,ecg_row_record\n"
        rows = ["private-a,10,1,1,private-wave-a", "private-a,61,0,0,private-wave-b",
                "private-b,360,1,0,private-wave-c", ",bad,1,0,private-wave-d"]
        forward, schema, _ = inspect(header + "\n".join(rows) + "\n")
        backward, _, _ = inspect(header + "\n".join(reversed(rows)) + "\n")
        self.assertEqual(forward, backward)
        self.assertNotIn("private-", json.dumps([forward, schema]))
        result, archive_schema = audit.audit_archive(archive_bytes([("CSV/train.csv", header + "\n".join(rows))]))
        self.assertEqual(result["schema_version"], "cath-ecg-metadata-audit-v2")
        self.assertIn("not same-ECG", result["audit_rules"]["source_label_feasibility"]["source_stemi_zero"])
        self.assertTrue(result["audit_rules"]["not_a_modeling_cohort_or_waveform_eligibility_audit"])
        self.assertNotIn("private-", json.dumps([result, archive_schema]))


class ArchiveGuardTests(unittest.TestCase):
    def test_expected_size_and_digest_are_both_required(self):
        data = b"fixed bytes"
        digest = hashlib.md5(data, usedforsecurity=False).hexdigest()
        self.assertEqual(audit.verify_archive(data, len(data), digest)["sha256"], hashlib.sha256(data).hexdigest())
        with self.assertRaisesRegex(audit.AuditError, "archive_size_mismatch"):
            audit.verify_archive(data, len(data) + 1, digest)
        with self.assertRaisesRegex(audit.AuditError, "archive_md5_mismatch"):
            audit.verify_archive(data, len(data), "0" * 32)

    def test_only_csv_members_are_read_and_unsafe_names_are_rejected(self):
        for name, code in [("README.txt", "non_csv_zip_member"), ("../x.csv", "unsafe_zip_member_name"),
                           ("/x.csv", "unsafe_zip_member_name"), ("C:/x.csv", "unsafe_zip_member_name"),
                           ("x\\y.csv", "unsafe_zip_member_name")]:
            with self.subTest(name=name), self.assertRaisesRegex(audit.AuditError, code):
                audit.audit_archive(archive_bytes([(name, "Patient_id\nprivate\n")]))

    def test_symlink_and_duplicate_members_are_rejected(self):
        info = zipfile.ZipInfo("linked.csv")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaisesRegex(audit.AuditError, "symlink_zip_member"):
            audit.audit_archive(archive_bytes([(info, "outside.csv")]))
        with self.assertRaisesRegex(audit.AuditError, "duplicate_zip_member_name"):
            audit.audit_archive(archive_bytes([("A.csv", "Patient_id\n"), ("a.csv", "Patient_id\n")]))

    def test_declared_and_actual_expansion_are_bounded(self):
        data = archive_bytes([("a.csv", "Patient_id\nprivate-a\n")])
        with patch.object(audit, "MAX_UNCOMPRESSED_BYTES", 5):
            with self.assertRaisesRegex(audit.AuditError, "zip_uncompressed_limit_exceeded"):
                audit.audit_archive(data)
            reader = audit.BoundedReader(io.BytesIO(b"123456"), [0])
            with self.assertRaisesRegex(audit.AuditError, "zip_actual_expansion_limit_exceeded"):
                reader.readinto(bytearray(6))

    def test_utf8_bom_and_directory_entries_do_not_change_rows(self):
        summary, _ = audit.audit_archive(archive_bytes([("CSV/", ""), ("CSV/a.csv", "\ufeffPatient_id,PCI\nx,1\n")]))
        self.assertEqual(summary["csv_summaries"][0]["row_count"], 1)
        self.assertEqual(summary["csv_summaries"][0]["patient_id"]["distinct_count"], 1)


class OutputTests(unittest.TestCase):
    def test_existing_outside_and_symlink_outputs_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "allowed"
            root.mkdir()
            (root / "existing").mkdir()
            (root / "alias").symlink_to(root / "existing", target_is_directory=True)
            with patch.object(audit, "OUTPUT_ROOT", root):
                for path, code in [(root / "existing", "output_exists_no_overwrite"),
                                   (Path(directory) / "outside", "output_must_be_under_ignored_cath_feasibility"),
                                   (root / "alias" / "new", "output_symlink_forbidden")]:
                    with self.subTest(code=code), self.assertRaisesRegex(audit.AuditError, code):
                        audit.output_directory(path)

    def test_local_archive_run_saves_only_verified_source_and_aggregate_reports(self):
        data = archive_bytes([("a.csv", "Patient_id,PCI,ecg_row_record\nprivate-subject,1,private-waveform\n")])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "allowed"
            source = Path(directory) / "input.zip"
            source.write_bytes(data)
            with patch.object(audit, "OUTPUT_ROOT", root), patch.object(audit, "EXPECTED_BYTES", len(data)), \
                    patch.object(audit, "EXPECTED_MD5", hashlib.md5(data, usedforsecurity=False).hexdigest()):
                summary = audit.run_audit(root / "one", source)
                self.assertEqual((root / "one/CSV.zip").read_bytes(), data)
                self.assertEqual({p.name for p in (root / "one").iterdir()}, {"CSV.zip", "download.json", "schema.json", "summary.json"})
                for path in (root / "one").iterdir():
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                    if path.suffix == ".json":
                        self.assertNotIn("private-", path.read_text())
                self.assertEqual(summary["csv_summaries"][0]["labels"]["PCI"]["counts"]["one"], 1)
                with self.assertRaisesRegex(audit.AuditError, "output_exists_no_overwrite"):
                    audit.run_audit(root / "one", source)

    def test_failed_checksum_creates_no_output_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "allowed"
            source = Path(directory) / "wrong.zip"
            source.write_bytes(b"wrong")
            with patch.object(audit, "OUTPUT_ROOT", root), self.assertRaises(audit.AuditError):
                audit.run_audit(root / "one", source)
            self.assertFalse(root.exists())

    def test_download_is_fixed_timeout_and_bounded_without_real_network(self):
        data = archive_bytes([("a.csv", "Patient_id\nprivate-a\n")])
        with tempfile.TemporaryDirectory() as directory, patch.object(audit, "OUTPUT_ROOT", Path(directory)), \
                patch.object(audit, "EXPECTED_BYTES", len(data)), \
                patch.object(audit, "EXPECTED_MD5", hashlib.md5(data, usedforsecurity=False).hexdigest()), \
                patch.object(audit.urllib.request, "urlopen", return_value=io.BytesIO(data)) as fetch:
            summary = audit.run_audit(Path(directory) / "one")
            self.assertEqual(fetch.call_args.args[0].full_url, audit.SOURCE_URL)
            self.assertEqual(fetch.call_args.kwargs["timeout"], 30)
            self.assertEqual(summary["source"]["acquisition"], "fixed_official_url")


if __name__ == "__main__":
    unittest.main()
