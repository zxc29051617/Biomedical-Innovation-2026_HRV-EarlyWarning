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
        self.assertTrue(summary["audit_rules"]["no_target_or_cohort_selected"])
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
