"""Archive guards, review blinding, and official-WFDB synthetic fixtures."""

from datetime import datetime
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

try:
    import numpy as np
except ImportError:
    np = None
try:
    import wfdb
except ImportError:
    wfdb = None

from hrv_early_warning import cath_waveforms as cw


def archive(entries):
    stream = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, content in entries:
                zf.writestr(name, content)
    stream.seek(0)
    return zipfile.ZipFile(stream)


def minimal_header(reference="sample.dat", samples=5000, record="sample"):
    return (f"{record} 12 500 {samples}\n" + "".join(
        f"{reference} 16 1000/mV 16 0 0 0 0 {lead}\n" for lead in cw.LEAD_NAMES))


class ArchiveGuardTests(unittest.TestCase):
    def test_unsafe_member_paths_and_special_files_rejected(self):
        for name in ("../secret.hea", "/abs/file", "a/../b", "a\\b", "https://bad", "a//b", "a/./b"):
            with self.subTest(name=name), archive([(name, b"x")]) as zf:
                with self.assertRaisesRegex(cw.CathWaveformError, "unsafe_archive_path"):
                    cw.ZipRecordReader(zf)
        info = zipfile.ZipInfo("symlink")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with archive([(info, "target")]) as zf:
            with self.assertRaisesRegex(cw.CathWaveformError, "nonregular_archive_member"):
                cw.ZipRecordReader(zf)

    def test_duplicate_paths_are_rejected(self):
        with archive([("same.hea", "x"), ("same.hea", "y")]) as zf:
            with self.assertRaisesRegex(cw.CathWaveformError, "duplicate_archive_member"):
                cw.ZipRecordReader(zf)

    def test_member_and_archive_limits_and_encryption(self):
        with archive([("sample.dat", "0123456789")]) as zf:
            with patch.object(cw, "MAX_DATA_BYTES", 5):
                with self.assertRaisesRegex(cw.CathWaveformError, "archive_member_size_limit"):
                    cw.ZipRecordReader(zf)
            with patch.object(cw, "MAX_ARCHIVE_EXPANDED_BYTES", 5):
                with self.assertRaisesRegex(cw.CathWaveformError, "archive_expansion_limit"):
                    cw.ZipRecordReader(zf)
            with patch.object(cw, "MAX_MEMBERS", 0):
                with self.assertRaisesRegex(cw.CathWaveformError, "archive_member_limit"):
                    cw.ZipRecordReader(zf)
            zf.infolist()[0].flag_bits |= 1
            with self.assertRaisesRegex(cw.CathWaveformError, "encrypted_archive_member"):
                cw.ZipRecordReader(zf)

    def test_header_paths_and_external_references_fail_before_decoder(self):
        for reference in ("../sample.dat", "/tmp/sample.dat", "sub/sample.dat", "https://x.dat", "sample.hea"):
            with self.subTest(reference=reference), archive([("folder/sample.hea", minimal_header(reference))]) as zf:
                with patch.object(cw, "_wfdb") as decoder:
                    with self.assertRaisesRegex(cw.CathWaveformError, "unsafe_signal_reference"):
                        cw.read_record_from_zip(zf, "folder/sample")
                    decoder.assert_not_called()

    def test_length_and_header_limits_fail_before_decoder(self):
        for count in (0, 1, 5001, 10**12):
            with self.subTest(count=count), archive([("sample.hea", minimal_header(samples=count))]) as zf:
                with self.assertRaisesRegex(cw.CathWaveformError, "header_sample_limit"):
                    cw.read_record_from_zip(zf, "sample")
        with archive([("sample.hea", "x" * (cw.MAX_HEADER_BYTES + 1))]) as zf:
            with self.assertRaisesRegex(cw.CathWaveformError, "record_member_size_limit"):
                cw.read_record_from_zip(zf, "sample")

    def test_missing_header_and_multisegment_header_have_safe_errors(self):
        with archive([("sample.hea", "sample/2 12 500 5000\n")]) as zf:
            with self.assertRaisesRegex(cw.CathWaveformError, "record_header_name_mismatch"):
                cw.read_record_from_zip(zf, "sample")
            with self.assertRaisesRegex(cw.CathWaveformError, "record_member_missing"):
                cw.read_record_from_zip(zf, "absent")


@unittest.skipUnless(np is not None, "optional numpy required")
class ReviewCaseTests(unittest.TestCase):
    def setUp(self):
        self.signal = np.arange(120, dtype=np.float64).reshape(10, 12) / 1000
        self.case_id = "cr-" + "b" * 32

    def case(self, **changes):
        args = dict(waveform=self.signal, lead_names=cw.LEAD_NAMES, fs=500,
                    age=55, sex="female", representation="median", case_id=self.case_id,
                    waveform_sha256=cw.waveform_fingerprint(self.signal))
        args.update(changes)
        return cw.build_blinded_review_case(**args)

    def test_exact_whitelist_and_json_round_trip(self):
        result = self.case()
        self.assertEqual(set(result), {"schema_version", "case_id", "age_years", "sex", "sample_rate_hz",
                                       "lead_names", "signals_mv", "representation", "provenance"})
        self.assertEqual(set(result["provenance"]), {"waveform_sha256"})
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)
        self.assertEqual(result["schema_version"], "cath-ecg-review-v1")
        self.assertIsNone(self.case(age=None, sex="unknown")["age_years"])

    def test_source_identifiers_and_unexpected_demographics_cannot_be_used(self):
        cases = [{"case_id": "Patient001_OMI1"}, {"case_id": "cr-" + "0" * 31},
                 {"sex": "F"}, {"age": True}, {"age": -1}, {"age": float("nan")},
                 {"age": 140}, {"representation": "raw_with_label"}]
        for change in cases:
            with self.subTest(change=change), self.assertRaises(cw.CathWaveformError):
                self.case(**change)
        with self.assertRaises(TypeError):
            self.case(OMI=1)

    def test_fingerprint_verifies_samples_units_order_and_rate(self):
        before = cw.waveform_fingerprint(self.signal)
        changed = self.signal.copy()
        changed[0, 0] += 0.001
        self.assertNotEqual(before, cw.waveform_fingerprint(changed))
        with self.assertRaisesRegex(cw.CathWaveformError, "waveform_fingerprint_mismatch"):
            self.case(waveform_sha256="0" * 64)
        with self.assertRaisesRegex(cw.CathWaveformError, "canonical_lead_order"):
            self.case(lead_names=list(reversed(cw.LEAD_NAMES)))
        with self.assertRaisesRegex(cw.CathWaveformError, "unexpected_sampling_rate"):
            self.case(fs=250)

    def test_invalid_samples_dimensions_and_nonfinite_are_rejected(self):
        for signal in (np.zeros((2, 11)), np.zeros((1, 12)), np.zeros((5001, 12)), np.zeros(12)):
            with self.subTest(shape=signal.shape), self.assertRaisesRegex(cw.CathWaveformError, "dimensions"):
                cw.waveform_fingerprint(signal)
        signal = self.signal.copy()
        signal[0, 0] = np.nan
        signal[1, 0] = np.inf
        with self.assertRaises(cw.CathWaveformError) as caught:
            cw.waveform_fingerprint(signal)
        self.assertEqual(caught.exception.code, "nonfinite_samples")
        self.assertEqual(caught.exception.details, {"nonfinite_samples": 2, "affected_leads": 1})


@unittest.skipUnless(np is not None and wfdb is not None, "optional numpy and wfdb required")
class OfficialWFDBTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.name = "sensitive_record_id"

    def write(self, values=None, units=None, leads=None, fs=500, clock=False):
        if values is None:
            values = np.tile(np.linspace(-1, 1, 5000)[:, None], (1, 12))
        units = units or ["mV"] * 12
        gain = [1000 if unit == "mV" else 1 for unit in units]
        wfdb.wrsamp(self.name, fs=fs, units=units, sig_name=leads or list(cw.LEAD_NAMES),
                    p_signal=np.asarray(values, dtype=np.float64), fmt=["16"] * 12,
                    adc_gain=gain, baseline=[0] * 12,
                    comments=["OMI=1 STEMI=1 Patient_id=secret"],
                    base_datetime=datetime(2024, 1, 2, 3, 4, 5) if clock else None,
                    write_dir=str(self.folder))

    def zipped(self):
        return archive([("waveforms/", b"")] + [("waveforms/" + p.name, p.read_bytes())
                        for p in sorted(self.folder.iterdir())])

    def read(self, representation="raw"):
        with self.zipped() as zf:
            return cw.read_record_from_zip(zf, "waveforms/" + self.name + ".hea", representation=representation)

    def test_official_round_trip_and_strict_metadata_blinding(self):
        self.write(clock=True)
        signal, meta = self.read()
        self.assertEqual(signal.shape, (5000, 12))
        self.assertEqual(meta["duration_seconds"], 10)
        self.assertTrue(meta["raw_expected_length_matches"])
        self.assertTrue(meta["header_has_base_time"])
        self.assertTrue(meta["header_has_base_date"])
        self.assertFalse(meta["clinical_quality_assessed"])
        serialized = json.dumps(meta, allow_nan=False)
        for forbidden in ("secret", self.name, "Patient_id", "STEMI", "02/01/2024", "comments"):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(meta["waveform_sha256"], cw.waveform_fingerprint(signal))

    def test_lead_reordering_and_explicit_microvolt_conversion(self):
        names = list(reversed(cw.LEAD_NAMES))
        expected = np.tile(np.arange(1, 13, dtype=float)[None, :] / 10, (500, 1))
        self.write(values=expected[:, ::-1] * 1000, units=["uV"] * 12, leads=names)
        signal, meta = self.read("median")
        np.testing.assert_allclose(signal, expected, atol=1e-12)
        self.assertEqual(meta["unit_scale_to_mv"], [0.001] * 12)
        self.assertEqual(meta["source_lead_names"], names)
        self.assertEqual(meta["duration_seconds"], 1)
        self.assertIsNone(meta["raw_expected_length_matches"])
        self.assertTrue(all(lead["flat_exact"] for lead in meta["per_lead"]))

    def test_raw_short_length_is_reported_not_padded_or_clinically_accepted(self):
        self.write(values=np.zeros((100, 12)))
        signal, meta = self.read()
        self.assertEqual(len(signal), 100)
        self.assertFalse(meta["raw_expected_length_matches"])
        self.assertFalse(meta["header_has_base_time"])
        self.assertFalse(meta["header_has_base_date"])

    def test_reserved_wfdb_missing_samples_are_rejected_with_counts(self):
        values = np.ones((100, 12))
        values[20:30, 3] = np.nan
        self.write(values=values)
        with self.assertRaises(cw.CathWaveformError) as caught:
            self.read()
        self.assertEqual(caught.exception.code, "nonfinite_samples")
        self.assertEqual(caught.exception.details["nonfinite_samples"], 10)

    def test_wrong_rate_duplicate_leads_and_implicit_units_rejected(self):
        self.write(fs=250)
        with self.assertRaisesRegex(cw.CathWaveformError, "unexpected_sampling_rate"):
            self.read()
        self.write()
        p = self.folder / (self.name + ".hea")
        lines = p.read_text().splitlines()
        lines[1] = lines[1].rsplit(" ", 1)[0] + " II"
        p.write_text("\n".join(lines) + "\n")
        with self.assertRaisesRegex(cw.CathWaveformError, "unexpected_leads"):
            self.read()
        self.write()
        p.write_text(p.read_text().replace("/mV", ""))
        with self.assertRaisesRegex(cw.CathWaveformError, "explicit_calibration_required"):
            self.read()

    def test_data_missing_or_corrupt_are_not_silently_filled(self):
        self.write()
        data_path = self.folder / (self.name + ".dat")
        data_path.write_bytes(data_path.read_bytes()[:10])
        with self.assertRaises(cw.CathWaveformError):
            self.read()
        data_path.unlink()
        with self.assertRaisesRegex(cw.CathWaveformError, "record_member_missing"):
            self.read()


if __name__ == "__main__":
    unittest.main()
