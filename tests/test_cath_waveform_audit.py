"""Public waveform audit orchestration; synthetic bytes only and no dependencies."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("cath_waveform_audit_test_target", ROOT / "scripts/audit_cath_waveforms.py")
audit = importlib.util.module_from_spec(SPEC)
with patch.object(sys, "path", [str(ROOT / "scripts"), *sys.path]):
    SPEC.loader.exec_module(audit)


def source_for(data):
    return {"filename": "fixed.zip", "file_id": 62951395, "bytes": len(data),
            "md5": hashlib.md5(data, usedforsecurity=False).hexdigest()}


class RecordingStream(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.requested_sizes = []
        self.returned_bytes = 0

    def read(self, size=-1):
        self.requested_sizes.append(size)
        value = super().read(size)
        self.returned_bytes += len(value)
        return value


def make_zip(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return buffer.getvalue()


HEADER = "Patient_id,ecg_row_record,ecg_med_record,OMI,STEMI\n"
VALID_TRAIN = HEADER + "private-patient,private-raw,private-median,1,0\n"


class ArchiveAcquisitionTests(unittest.TestCase):
    def test_download_uses_fixed_url_timeout_and_verified_bytes(self):
        payload = b"synthetic archive bytes"
        response = RecordingStream(payload)
        calls = []

        def opener(request, timeout):
            calls.append((request.full_url, timeout))
            return response

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "out.zip"
            result = audit.acquire(source_for(payload), destination, opener=opener)
            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        self.assertEqual(calls, [("https://ndownloader.figshare.com/files/62951395", 60)])
        self.assertEqual(result["bytes"], len(payload))
        self.assertEqual(result["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(result["md5"], source_for(payload)["md5"])
        self.assertEqual(result["acquisition"], "official_fixed_url")
        self.assertTrue(response.closed)
        self.assertTrue(all(0 < value <= 1024 * 1024 for value in response.requested_sizes))

    def test_overlong_source_is_bounded_and_partial_output_removed(self):
        payload = b"abc"
        response = RecordingStream(payload + b"extra" * 100)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "out.zip"
            with self.assertRaisesRegex(audit.AuditError, "^waveform_archive_size_exceeded$"):
                audit.acquire(source_for(payload), destination, opener=lambda *a, **k: response)
            self.assertFalse(destination.exists())
        self.assertEqual(response.returned_bytes, len(payload) + 1)
        self.assertTrue(response.closed)

    def test_short_or_wrong_digest_source_is_not_kept(self):
        expected = b"expected"
        for payload in (expected[:-1], b"different"[:len(expected)]):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / "out.zip"
                with self.assertRaisesRegex(audit.AuditError, "^waveform_archive_fingerprint_mismatch$"):
                    audit.acquire(source_for(expected), destination,
                                  opener=lambda *a, **k: RecordingStream(payload))
                self.assertFalse(destination.exists())

    def test_midstream_error_removes_only_new_partial_output(self):
        class InterruptedStream(RecordingStream):
            def read(self, size=-1):
                if self.returned_bytes:
                    raise OSError("private-stream-error")
                return super().read(min(size, 2))

        response = InterruptedStream(b"abcdef")
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "out.zip"
            with self.assertRaises(OSError):
                audit.acquire(source_for(b"abcdef"), destination, opener=lambda *a, **k: response)
            self.assertFalse(destination.exists())
        self.assertTrue(response.closed)

    def test_existing_destination_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "existing.zip"
            destination.write_bytes(b"keep existing source")
            with self.assertRaisesRegex(audit.AuditError, "^archive_destination_exists_no_overwrite$"):
                audit.acquire(source_for(b"new"), destination, opener=lambda *a, **k: RecordingStream(b"new"))
            self.assertEqual(destination.read_bytes(), b"keep existing source")

    def test_existing_symlink_and_target_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target.zip"
            target.write_bytes(b"keep target")
            destination = Path(directory) / "link.zip"
            destination.symlink_to(target)
            with self.assertRaisesRegex(audit.AuditError, "^archive_destination_exists_no_overwrite$"):
                audit.acquire(source_for(b"new"), destination, opener=lambda *a, **k: RecordingStream(b"new"))
            self.assertTrue(destination.is_symlink())
            self.assertEqual(target.read_bytes(), b"keep target")

    def test_destination_created_after_precheck_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "raced.zip"

            def opener(*args, **kwargs):
                destination.write_bytes(b"another writer owns this file")
                return RecordingStream(b"new")

            with self.assertRaises(FileExistsError):
                audit.acquire(source_for(b"new"), destination, opener=opener)
            self.assertEqual(destination.read_bytes(), b"another writer owns this file")

    def test_local_archive_is_verified_without_network_or_source_path_in_report(self):
        payload = b"local archive"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "private-source-name.zip"
            source.write_bytes(payload)
            destination = Path(directory) / "out.zip"
            with patch.object(audit.urllib.request, "urlopen", side_effect=AssertionError("no network")):
                result = audit.acquire(source_for(payload), destination, local_source=source,
                                       opener=lambda *a, **k: self.fail("local source used network"))
            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(source.read_bytes(), payload)
        self.assertEqual(result["acquisition"], "local_verified")
        self.assertNotIn("private-source", json.dumps(result))


class RecordResolutionTests(unittest.TestCase):
    def index(self):
        data = make_zip([(name, "") for name in (
            "raw/one.hea", "other/one.hea", "raw/unique.hea", "raw/unique.dat", "empty/")])
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return audit.record_index(archive)

    def test_exact_path_wins_even_when_basename_is_ambiguous(self):
        index = self.index()
        self.assertEqual(audit.resolve_record("raw/one", index), ("raw/one", "exact_path"))
        self.assertEqual(audit.resolve_record("raw/one.hea", index), ("raw/one", "exact_path"))

    def test_unique_basename_is_explicit_fallback(self):
        self.assertEqual(audit.resolve_record("unique", self.index()), ("raw/unique", "unique_basename"))

    def test_explicit_wfdb_data_member_suffix_resolves_same_record(self):
        self.assertEqual(audit.resolve_record("raw/unique.dat", self.index()), ("raw/unique", "exact_path"))
        self.assertEqual(audit.resolve_record("unique.dat", self.index()), ("raw/unique", "unique_basename"))
        with self.assertRaisesRegex(audit.AuditError, "^record_reference_missing$"):
            audit.resolve_record("unique.csv", self.index())

    def test_missing_and_ambiguous_references_have_value_free_codes(self):
        for value, code in (("private-missing", "record_reference_missing"),
                            ("one", "record_reference_ambiguous")):
            with self.subTest(code=code), self.assertRaisesRegex(audit.AuditError, "^" + code + "$"):
                audit.resolve_record(value, self.index())

    def test_unsafe_reference_never_reaches_basename_fallback(self):
        for value in ("", "/raw/unique", "../unique", "safe/../unique", "raw\\unique", "C:unique", "unique\n"):
            with self.subTest(value=repr(value)), self.assertRaisesRegex(audit.AuditError, "^unsafe_csv_record_reference$"):
                audit.resolve_record(value, self.index())


class TrainingMetadataTests(unittest.TestCase):
    def test_official_archive_verification_precedes_csv_parsing(self):
        payload = b"unverified-private-archive"
        with patch.object(audit, "verify_archive", side_effect=audit.AuditError("fingerprint_mismatch")) as verify:
            with patch.object(audit.zipfile, "ZipFile", side_effect=AssertionError("must verify first")):
                with self.assertRaisesRegex(audit.AuditError, "^fingerprint_mismatch$"):
                    audit.training_rows(payload)
            verify.assert_called_once_with(payload)

    def test_only_exact_training_member_is_read(self):
        payload = make_zip([("CSV/train.csv", "\ufeff" + VALID_TRAIN),
                            ("CSV/test.csv", b"\xffinvalid test content must not be parsed")])
        with patch.object(audit, "verify_archive") as verify:
            rows = audit.training_rows(payload)
        verify.assert_called_once_with(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["OMI"], "1")
        self.assertEqual(rows[0]["Patient_id"], "private-patient")

    def test_training_name_is_not_guessed_from_similar_files(self):
        for name in ("train.csv", "CSV/Train.csv", "CSV/test.csv"):
            payload = make_zip([(name, VALID_TRAIN)])
            with self.subTest(name=name), patch.object(audit, "verify_archive"):
                with self.assertRaisesRegex(audit.AuditError, "^exact_train_csv_missing$"):
                    audit.training_rows(payload)

    def test_missing_columns_empty_training_and_row_width_are_rejected(self):
        fixtures = [
            ("Patient_id,OMI\nprivate-patient,1\n", "train_csv_required_fields_missing"),
            (HEADER, "train_csv_required_fields_missing"),
            (HEADER + "private-patient,raw,median,1\n", "train_row_missing_required_value"),
            (HEADER + "private-patient,raw,median,1,0,private-extra\n", "train_row_missing_required_value"),
            (HEADER + ",raw,median,1,0\n", "train_row_missing_required_value"),
        ]
        for content, code in fixtures:
            with self.subTest(code=code), patch.object(audit, "verify_archive"):
                with self.assertRaisesRegex(audit.AuditError, "^" + code + "$"):
                    audit.training_rows(make_zip([("CSV/train.csv", content)]))

    def test_source_labels_must_be_explicit_binary_values(self):
        for omi, stemi in (("2", "0"), ("1", "unknown"), ("1.0", "0")):
            content = HEADER + f"private-patient,raw,median,{omi},{stemi}\n"
            with self.subTest(omi=omi, stemi=stemi), patch.object(audit, "verify_archive"):
                with self.assertRaisesRegex(audit.AuditError, "^source_label_not_binary$"):
                    audit.training_rows(make_zip([("CSV/train.csv", content)]))

    def test_duplicate_columns_and_whitespace_required_values_are_not_silently_used(self):
        fixtures = [
            ("Patient_id,ecg_row_record,ecg_med_record,OMI,STEMI,OMI\nprivate-a,raw,median,0,0,1\n",
             "train_duplicate_columns"),
            (HEADER + "   ,raw,median,1,0\n", "train_row_missing_required_value"),
            (HEADER + "private-a,   ,median,1,0\n", "train_row_missing_required_value"),
        ]
        for content, code in fixtures:
            with self.subTest(code=code), patch.object(audit, "verify_archive"):
                with self.assertRaisesRegex(audit.AuditError, "^" + code + "$"):
                    audit.training_rows(make_zip([("CSV/train.csv", content)]))


class BoolVector(list):
    def any(self):
        return any(self)


class Vector(list):
    def __eq__(self, other):
        return BoolVector(a == b for a, b in zip(self, other))


class Waveform:
    """Minimal two-dimensional numeric object for aggregate-only tests."""
    def __init__(self, rows):
        self.rows = rows
        self.shape = (len(rows), len(rows[0]))

    def min(self, axis=None):
        return Vector(min(column) for column in zip(*self.rows)) if axis == 0 else min(min(row) for row in self.rows)

    def max(self, axis=None):
        return Vector(max(column) for column in zip(*self.rows)) if axis == 0 else max(max(row) for row in self.rows)


class AggregateTests(unittest.TestCase):
    def fixtures(self):
        return [("private-a", "0", "0", "private-hash-1"),
                ("private-a", "1", "0", "private-hash-1"),
                ("private-b", "1", "1", "private-hash-1"),
                ("private-a", "1", "0", "private-hash-2"),
                ("private-c", "0", "0", "private-hash-3")]

    def aggregate(self, fixtures):
        result = audit.Aggregate("raw")
        for patient, omi, stemi, digest in fixtures:
            result.attempted += 1
            row = {"Patient_id": patient, "OMI": omi, "STEMI": stemi,
                   "ecg_row_record": "private-record-name"}
            metadata = {"source_units": ["mV", "mV"], "format": ["16", "16"],
                        "waveform_sha256": digest, "header_has_base_date": False,
                        "header_has_base_time": True}
            waveform = Waveform([[0, -1], [0 if patient == "private-c" else 1, 2]])
            result.success(row, waveform, metadata, "exact_path")
        result.attempted += 2
        result.errors["record_reference_missing"] += 2
        return result.report()

    def test_counts_keep_ecg_patient_and_hash_denominators_distinct(self):
        report = self.aggregate(self.fixtures())
        self.assertEqual(report["attempted_train_rows"], 7)
        self.assertEqual(report["decoded_train_rows"], 5)
        self.assertEqual(report["decoded_distinct_patients"], 3)
        self.assertEqual(report["decoded_source_omi_one_rows"], 3)
        self.assertEqual(report["decoded_source_omi_one_distinct_patients"], 2)
        self.assertEqual(report["decoded_source_omi_one_source_stemi_zero_rows"], 2)
        self.assertEqual(report["unique_decoded_waveform_hashes"], 3)
        self.assertEqual(report["repeated_waveform_hash_groups"], 1)
        self.assertEqual(report["rows_beyond_first_per_waveform_hash"], 2)
        self.assertEqual(report["waveform_hash_groups_with_multiple_patient_ids"], 1)
        self.assertEqual(report["waveform_hash_groups_with_source_omi_zero_and_one"], 1)
        self.assertEqual(report["same_patient_same_waveform_groups_with_source_omi_zero_and_one"], 1)
        self.assertEqual(report["decoded_train_rows"] + sum(report["failure_codes"].values()), report["attempted_train_rows"])

    def test_signal_statistics_do_not_reclassify_source_labels(self):
        report = self.aggregate(self.fixtures())
        self.assertEqual(report["sample_count_distribution"], {"2": 5})
        self.assertEqual(report["raw_records_not_5000_samples"], 5)
        self.assertEqual(report["records_with_any_exactly_constant_lead"], 1)
        self.assertEqual((report["global_minimum_mv"], report["global_maximum_mv"]), (-1.0, 2.0))
        self.assertEqual(report["header_time_field_presence"], {"header_has_base_date_false": 5, "header_has_base_time_true": 5})
        self.assertEqual(report["decoded_source_omi_one_rows"], 3)

    def test_output_is_order_invariant_and_contains_no_row_identifiers(self):
        report = self.aggregate(self.fixtures())
        self.assertEqual(report, self.aggregate(list(reversed(self.fixtures()))))
        text = json.dumps(report, allow_nan=False)
        for marker in ("private-a", "private-b", "private-c", "private-hash", "private-record"):
            self.assertNotIn(marker, text)

    def test_empty_aggregate_does_not_invent_voltage_or_patient_counts(self):
        report = audit.Aggregate("median").report()
        self.assertEqual(report["decoded_train_rows"], 0)
        self.assertEqual(report["decoded_distinct_patients"], 0)
        self.assertEqual(report["unique_decoded_waveform_hashes"], 0)
        self.assertIsNone(report["global_minimum_mv"])
        self.assertIsNone(report["global_maximum_mv"])
        json.dumps(report, allow_nan=False)


class CommandOutputTests(unittest.TestCase):
    def test_unexpected_error_prints_type_without_private_values(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["audit", "--output-dir", "ignored"]):
            with patch.object(audit, "run", side_effect=ValueError("private-patient and private-record")):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    status = audit.main()
        self.assertEqual(status, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue().strip(), "Cath waveform audit failed: ValueError")

    def test_success_prints_only_aggregate_json(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        summary = {"schema_version": "synthetic", "representations": [audit.Aggregate("raw").report()]}
        with patch.object(sys, "argv", ["audit", "--output-dir", "ignored", "--representation", "raw"]):
            with patch.object(audit, "run", return_value=summary) as run:
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    status = audit.main()
        self.assertEqual(status, 0)
        self.assertEqual(stderr.getvalue(), "")
        marker = "CATH_WAVEFORM_SUMMARY_JSON="
        self.assertTrue(stdout.getvalue().startswith(marker))
        self.assertEqual(json.loads(stdout.getvalue()[len(marker):]), summary)
        self.assertEqual(run.call_args.args[2], ("raw",))


if __name__ == "__main__":
    unittest.main()
